"""No GCP access. Mock deployment argv and opt-in isolated local Postgres/HTTP."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time

import pytest
from sqlalchemy.engine import URL, make_url

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "deploy/gcp" / (name + ".py"))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


@pytest.fixture
def private_state(tmp_path):
    state = tmp_path / "state"
    module("fixture").prepare(state)
    try:
        yield state
    finally:
        for path in state.iterdir():
            path.unlink()
        state.rmdir()


def test_private_state_and_socket_url(private_state):
    fixture = module("fixture")
    state = private_state
    assert state.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in state.iterdir())
    assert len((state / "token").read_text()) >= 32
    with pytest.raises(FileExistsError):
        fixture.prepare(state)


@pytest.mark.parametrize("private", [False, True])
def test_deploy_command_flow_has_no_secret_values(tmp_path, private_state, private):
    state = private_state
    module("fixture").private_write(state / "project-id", "example-gcp-project\n")
    module("fixture").private_write(state / "project-created", "example-gcp-project\n")
    commands = tmp_path / "commands"
    commands.mkdir()
    log = tmp_path / "commands.jsonl"
    for name in ("docker", "gcloud"):
        path = commands / name
        path.write_text(f"#!{sys.executable}\nimport json, os, sys\nwith open(os.environ['MOCK_LOG'], 'a') as f: f.write(json.dumps(sys.argv) + '\\n')\n")
        path.chmod(0o755)
    env = {**os.environ, "PATH": str(commands) + os.pathsep + str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
           "PROJECT": "example-gcp-project", "REGION": "europe-west1", "PREFIX": "ush193", "GCP_STATE": str(state),
           "REPO_URL": "ssh://git@example.com/context.git" if private else "https://example.com/context.git",
           "REPO_REF": "main", "IMAGE_TAG": "example-source", "MOCK_LOG": str(log)}
    result = subprocess.run(["bash", str(ROOT / "deploy/gcp/deploy.sh")], env=env, cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(calls) == 5
    deploy = next(call for call in calls if call[1:3] == ["run", "deploy"])
    assert "--no-invoker-iam-check" in deploy and "--no-cpu-throttling" in deploy
    assert deploy[deploy.index("--project") + 1] == "example-gcp-project"
    assert deploy[deploy.index("--add-cloudsql-instances") + 1] == "example-gcp-project:europe-west1:ush193-pg"
    refs = deploy[deploy.index("--set-secrets") + 1]
    assert "UNSHACKLE_MCP_TOKEN=ush193-token:1" in refs
    assert ("/var/run/git-key/key=ush193-git-key:1" in refs) == private
    output = log.read_text() + result.stdout + result.stderr
    for name in ("token", "reader-password"):
        assert (state / name).read_text() not in output
    assert json.loads((state / "run-env.json").read_text())["UNSHACKLE_REPO_URL"] == env["REPO_URL"]
    assert (state / "token-version").read_text().strip() == "1"
    # A successful rotation must survive a fresh invocation without overrides.
    env["TOKEN_VERSION"] = "2"
    subprocess.run(["bash", str(ROOT / "deploy/gcp/deploy.sh")], env=env, cwd=ROOT, check=True, capture_output=True)
    del env["TOKEN_VERSION"]
    log.unlink()
    subprocess.run(["bash", str(ROOT / "deploy/gcp/deploy.sh")], env=env, cwd=ROOT, check=True, capture_output=True)
    assert "UNSHACKLE_MCP_TOKEN=ush193-token:2" in log.read_text()
    # Scope mismatches must fail before building or touching cloud resources.
    log.unlink()
    for marker in ("project-id", "project-created"):
        (state / marker).write_text("another-project\n")
        denied = subprocess.run(["bash", str(ROOT / "deploy/gcp/deploy.sh")], env=env, cwd=ROOT, capture_output=True)
        assert denied.returncode != 0 and not log.exists()
        (state / marker).write_text("example-gcp-project\n")
    # Reject embedded credentials before any build/push/deployment.
    env["REPO_URL"] = "https://user:secret@example.com/context.git"
    denied = subprocess.run(["bash", str(ROOT / "deploy/gcp/deploy.sh")], env=env, cwd=ROOT, capture_output=True)
    assert denied.returncode != 0 and not log.exists()


def test_probe_rejects_cleartext_remote_before_network():
    with pytest.raises(ValueError, match="HTTPS"):
        asyncio.run(module("verify").verify("http://example.com/mcp", "example-token"))


@pytest.mark.skipif(os.environ.get("UNSHACKLE_GCP_LOCAL_TEST") != "1", reason="explicit opt-in to isolated local Docker fixture")
def test_real_postgres_seed_and_http_probe(tmp_path, private_state, monkeypatch):
    """Actual psycopg role, native cancellation and public probe; always remove container."""
    fixture = module("fixture")
    state = private_state
    name = "unshackle-gcp-test-" + secrets.token_hex(4)
    admin = json.loads((state / "admin.json").read_text())["--password"]
    env = {**os.environ, "POSTGRES_PASSWORD": admin}
    server = None
    volumes = []
    try:
        subprocess.run(["docker", "run", "-d", "--name", name, "-p", "127.0.0.1::5432", "-e", "POSTGRES_PASSWORD", "-e", "POSTGRES_DB=example", "postgres:16-alpine"], env=env, check=True, stdout=subprocess.DEVNULL)
        mounts = json.loads(subprocess.check_output(["docker", "inspect", "--format", "{{json .Mounts}}", name], text=True))
        volumes = [mount["Name"] for mount in mounts if mount["Type"] == "volume"]
        assert volumes, "Expected the Postgres data volume to be included in cleanup"
        for _ in range(120):
            ready = subprocess.run(["docker", "exec", name, "pg_isready", "-h", "127.0.0.1", "-U", "postgres", "-d", "example"], capture_output=True)
            if ready.returncode == 0:
                break
            time.sleep(0.25)
        else:
            pytest.fail("Postgres fixture failed to start")
        port = int(subprocess.check_output(["docker", "port", name, "5432/tcp"], text=True).strip().rsplit(":", 1)[1])
        context = tmp_path / "context"
        monkeypatch.setenv("UNSHACKLE_WAREHOUSE_URL", "")
        fixture.seed(state, context, "example-gcp-project:europe-west1:ush193-pg", port)
        cloud = make_url((state / "warehouse-url").read_text())
        assert cloud.drivername == "postgresql+psycopg"
        assert cloud.query["host"] == "/cloudsql/example-gcp-project:europe-west1:ush193-pg"
        assert cloud.username == "example_reader"
        local = URL.create("postgresql+psycopg", username=cloud.username, password=cloud.password, host="127.0.0.1", port=port, database="example", query={"connect_timeout": "10"})
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            http_port = sock.getsockname()[1]
        token = (state / "token").read_text()
        server_env = {**os.environ, "UNSHACKLE_MCP_TOKEN": token, "UNSHACKLE_WAREHOUSE_URL": local.render_as_string(hide_password=False)}
        server = subprocess.Popen([sys.executable, "-c", "from unshackle.cli import main; main()", "mcp", "--transport", "http", "--host", "127.0.0.1", "--port", str(http_port), "--repo", str(context)], env=server_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(120):
            try:
                with socket.create_connection(("127.0.0.1", http_port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("HTTP fixture failed to start")
        asyncio.run(module("verify").verify(f"http://127.0.0.1:{http_port}/mcp", token))
        for path in context.rglob("*"):
            if path.is_file():
                assert cloud.password not in path.read_text(errors="replace")
                assert token not in path.read_text(errors="replace")
    finally:
        try:
            if server is not None:
                server.terminate()
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=10)
        finally:
            # -v removes only anonymous volumes owned by this exact container.
            subprocess.run(["docker", "rm", "-fv", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            for volume in volumes:
                assert subprocess.run(["docker", "volume", "inspect", volume], capture_output=True).returncode != 0
