"""AWS preparation tests: offline contracts plus explicit local-container acceptance."""
import importlib.util
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
AWS = ROOT / "deploy/aws"


def module(name):
    spec = importlib.util.spec_from_file_location("aws_" + name, AWS / (name + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_preflight_refuses_existing_resources_and_unknown_errors(monkeypatch):
    check = module("preflight")
    args = ("cloudformation", "describe-stacks", "--stack-name", "unshackle-example")
    monkeypatch.setattr(check, "aws", lambda *a: subprocess.CompletedProcess(a, 0, "{}", ""))
    with pytest.raises(RuntimeError, match="already exists"):
        check.absent(args, "ValidationError", "does not exist")
    for error in ("ExpiredToken", "AccessDenied", "ThrottlingException"):
        monkeypatch.setattr(check, "aws", lambda *a: subprocess.CompletedProcess(a, 1, "", f"({error})"))
        with pytest.raises(RuntimeError, match="permissions"):
            check.absent(args, "ValidationError", "does not exist")
    monkeypatch.setattr(check, "aws", lambda *a: subprocess.CompletedProcess(a, 1, "", "(ValidationError) Stack does not exist"))
    check.absent(args, "ValidationError", "does not exist")


def test_template_security_and_scoped_lifecycle():
    template = yaml.safe_load((AWS / "stack.yaml").read_text())
    resources = template["Resources"]
    assert template["Parameters"]["DesiredCount"]["AllowedValues"] == [0, 1]
    assert resources["Targets"]["Properties"]["Matcher"] == {"HttpCode": "401"}
    task = resources["Task"]["Properties"]
    assert "TaskRoleArn" not in task
    fixture, mcp = task["ContainerDefinitions"]
    assert fixture["HealthCheck"]["Command"][1].startswith("pg_isready -h 127.0.0.1")
    assert mcp["User"] == "10001:10001" and mcp["ReadonlyRootFilesystem"]
    assert mcp["DependsOn"] == [{"ContainerName": "fixture", "Condition": "HEALTHY"}]
    assert not any(secret.get("Name") == "POSTGRES_PASSWORD" for secret in mcp["Secrets"])
    ingress = resources["TaskSecurityGroup"]["Properties"]["SecurityGroupIngress"]
    assert ingress == [{"IpProtocol": "tcp", "FromPort": 8000, "ToPort": 8000,
                        "SourceSecurityGroupId": {"Ref": "AlbSecurityGroup"}}]
    registry = yaml.safe_load((AWS / "registry.yaml").read_text())["Resources"]["Images"]["Properties"]
    assert registry["EmptyOnDelete"] is True and registry["ImageTagMutability"] == "IMMUTABLE"


def test_private_git_files_url_escaping_and_sanitized_git_environment(tmp_path, monkeypatch):
    from unshackle.mcp.sync import GitRefresh
    start = module("start")
    monkeypatch.setenv("UNSHACKLE_WAREHOUSE_URL", "")
    monkeypatch.setenv("READER_PASSWORD", "synthetic+/@password")
    monkeypatch.setenv("GIT_PRIVATE_KEY", "synthetic-private-key\n")
    monkeypatch.setenv("GIT_KNOWN_HOSTS", "example.invalid ssh-ed25519 synthetic\n")
    original_home = os.environ.get("HOME")
    monkeypatch.setattr(start, "Path", lambda _: tmp_path / "git")
    start.configure()
    assert os.environ.get("HOME") == original_home
    assert "synthetic%2B%2F%40password" in os.environ["UNSHACKLE_WAREHOUSE_URL"]
    assert (tmp_path / "git/id_ed25519").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "git").stat().st_mode & 0o777 == 0o700
    assert "GIT_PRIVATE_KEY" not in os.environ and "READER_PASSWORD" not in os.environ
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin:/bin")
    captured = {}
    def run(args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args, 0, "synthetic-ref", "")
    monkeypatch.setattr(subprocess, "run", run)
    refresh = GitRefresh("ssh://git@example.invalid/context.git", cache_dir=tmp_path)
    try:
        refresh._git("--version")
        assert captured["env"]["PATH"].startswith("/usr/local/bin:")
        assert captured["env"]["GIT_SSH_COMMAND"] == "ssh -oBatchMode=yes"
        assert "UNSHACKLE_WAREHOUSE_URL" not in captured["env"]
    finally:
        refresh.close()


def test_ssh_wrapper_preserves_arguments_and_verifies_host(tmp_path):
    # Capture the /usr/bin/ssh boundary without starting SSH or disabling host checks.
    fake = tmp_path / "ssh-capture"
    fake.write_text(f'#!{sys.executable}\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n')
    fake.chmod(0o700)
    script = (AWS / "ssh").read_text().replace("exec /usr/bin/ssh", 'exec "$TEST_SSH_BINARY"')
    result = subprocess.run(["sh", "-c", script, "ssh", "-oBatchMode=yes", "git@example.invalid", "git-upload-pack 'a b'"],
                            env={**os.environ, "TEST_SSH_BINARY": str(fake)}, capture_output=True, text=True, check=True)
    args = json.loads(result.stdout)
    assert args[:2] == ["-i", "/tmp/unshackle-git/id_ed25519"]
    assert "StrictHostKeyChecking=yes" in args
    assert "UserKnownHostsFile=/tmp/unshackle-git/known_hosts" in args
    assert args[-1] == "git-upload-pack 'a b'"
    assert args[-3] == "-oBatchMode=yes"


def test_build_commands_reuse_root_image_stage_fixture_and_never_push(tmp_path):
    fake = tmp_path / "docker"
    log = tmp_path / "commands.jsonl"
    fake.write_text(f'''#!{sys.executable}
import json,os,sys
from pathlib import Path
with open(os.environ["TEST_LOG"], "a") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[-1] not in ({str(ROOT)!r}, {str(AWS)!r}):
    stage = Path(sys.argv[-1])
    assert (stage / "schema.sql").read_text() == Path({str(ROOT / 'examples/mcp-demo/schema.sql')!r}).read_text()
    assert (stage / "reader.sh").exists()
''')
    fake.chmod(0o700)
    subprocess.run(["bash", str(AWS / "build.sh"), "example.invalid/unshackle-test", "abc123"],
                   env={**os.environ, "PATH": str(tmp_path) + ":" + os.environ["PATH"], "TEST_LOG": str(log)}, check=True)
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(commands) == 3 and all(args[0] == "build" for args in commands)
    assert all("linux/amd64" in args for args in commands)
    assert not Path(commands[-1][-1]).exists()  # scratch build context removed


@pytest.mark.skipif(os.environ.get("UNSHACKLE_AWS_CONTAINER_TESTS") != "1", reason="explicit local Docker acceptance required")
def test_local_containers_real_reader_http_parity_and_cancellation(tmp_path):
    import psycopg
    import httpx
    from sqlalchemy.engine import URL
    from unshackle import build, scaffold
    from unshackle.imports import dbt, sql, warehouse

    suffix = secrets.token_hex(5)
    fixture, mcp = "unshackle-aws-fixture-" + suffix, "unshackle-aws-mcp-" + suffix
    password, reader, token = (secrets.token_urlsafe(32) for _ in range(3))
    env = {**os.environ, "POSTGRES_PASSWORD": password, "READER_PASSWORD": reader, "UNSHACKLE_MCP_TOKEN": token}
    repo = tmp_path / "context"
    def docker(*args, **kwargs):
        return subprocess.run(["docker", *args], check=True, capture_output=True, text=True, **kwargs).stdout.strip()
    try:
        docker("run", "-d", "--name", fixture, "--platform", "linux/amd64", "-p", "127.0.0.1::5432",
               "-p", "127.0.0.1::8000", "-e", "POSTGRES_PASSWORD", "-e", "READER_PASSWORD",
               "-e", "POSTGRES_USER=postgres", "-e", "POSTGRES_DB=example", "unshackle-aws-prep:fixture-local", env=env)
        for _ in range(120):
            ready = subprocess.run(["docker", "exec", fixture, "pg_isready", "-h", "127.0.0.1", "-U", "postgres", "-d", "example"], capture_output=True)
            if ready.returncode == 0:
                break
            time.sleep(.5)
        else:
            pytest.fail("Fixture readiness timed out")
        port = int(docker("port", fixture, "5432/tcp").rsplit(":", 1)[1])
        http_port = int(docker("port", fixture, "8000/tcp").rsplit(":", 1)[1])
        url = URL.create("postgresql+psycopg", username="example_reader", password=reader,
                         host="127.0.0.1", port=port, database="example").render_as_string(hide_password=False)
        with psycopg.connect(host="127.0.0.1", port=port, dbname="example", user="example_reader", password=reader) as con:
            for statement in ("DELETE FROM orders", "CREATE TABLE public.forbidden_probe (id int)"):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    con.execute("BEGIN READ WRITE")
                    con.execute(statement)
                con.rollback()
            assert con.execute("SELECT current_setting('default_transaction_read_only')").fetchone() == ("on",)
        scaffold.init(repo)
        build.apply(warehouse.introspect(url), repo)
        build.apply(dbt.parse(ROOT / "examples/mcp-demo/dbt"), repo)
        build.apply(sql.parse([ROOT / "examples/mcp-demo/inputs"]), repo)
        for args in (("init", "-b", "main"), ("add", "."), ("-c", "user.name=Example", "-c", "user.email=reader@example.invalid", "commit", "-m", "Synthetic context")):
            subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
        # Docker cp avoids macOS temp-path sharing assumptions.
        docker("create", "--name", mcp, "--platform", "linux/amd64", "--network", "container:" + fixture,
               "--read-only", "--volume", "/context", "--tmpfs", "/tmp:rw,mode=1777", "--cap-drop", "ALL",
               "-e", "READER_PASSWORD", "-e", "UNSHACKLE_MCP_TOKEN", "-e", "UNSHACKLE_REPO_URL=file:///context",
               "-e", "UNSHACKLE_REPO_REF=main", "unshackle-aws-prep:mcp-local", env=env)
        docker("cp", str(repo) + "/.", mcp + ":/context")
        docker("run", "--rm", "--platform", "linux/amd64", "--user", "0", "--volumes-from", mcp,
               "--entrypoint", "chown", "unshackle-aws-prep:mcp-local", "-R", "10001:10001", "/context")
        docker("start", mcp)
        endpoint = f"http://127.0.0.1:{http_port}/mcp"
        for _ in range(120):
            if docker("inspect", "--format", "{{.State.Status}}", mcp) == "exited":
                logs = subprocess.run(["docker", "logs", mcp], capture_output=True, text=True)
                safe = logs.stdout + logs.stderr
                for secret in (password, reader, token):
                    safe = safe.replace(secret, "[REDACTED]")
                pytest.fail("MCP exited: " + safe)
            try:
                if httpx.post(endpoint, timeout=2).status_code == 401:
                    break
            except httpx.TransportError:
                pass
            time.sleep(.5)
        else:
            pytest.fail("MCP readiness timed out (inspect sanitized container logs)")
        assert "Synthetic fixture reader checks passed" in docker("logs", mcp)
        result = subprocess.run([sys.executable, str(AWS / "probe.py")],
                                env={**env, "UNSHACKLE_MCP_URL": endpoint}, capture_output=True, text=True, timeout=100)
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        for name in (mcp, fixture):
            subprocess.run(["docker", "rm", "-fv", name], capture_output=True)


def test_preflight_rejects_existing_hostname_and_uses_only_reads(monkeypatch):
    check = module("preflight")
    monkeypatch.setenv("PREFIX", "unshackle-example")
    monkeypatch.setenv("DOMAIN_NAME", "mcp.example.invalid")
    calls = []
    def absent(args, *expected):
        calls.append(args)
    monkeypatch.setattr(check, "absent", absent)
    monkeypatch.setattr(check, "aws", lambda *args: subprocess.CompletedProcess(args, 0, '{"clusters":[],"failures":[{"reason":"MISSING"}]}', ""))
    monkeypatch.setattr(check, "records", lambda: [{"Name": "MCP.EXAMPLE.INVALID."}])
    with pytest.raises(RuntimeError, match="hostname already exists"):
        check.main()
    assert {args[1] for args in calls} == {"describe-stacks", "describe-repositories", "describe-secret"}
    assert calls[0][-1] == "unshackle-example" and calls[1][-1] == "unshackle-example-images"


def test_cleanup_verifier_exact_family_and_api_failures(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(AWS))
    check = module("verify_deleted")
    monkeypatch.setenv("PREFIX", "unshackle-example")
    monkeypatch.setenv("STATE", str(tmp_path))
    monkeypatch.setenv("DOMAIN_NAME", "mcp.example.invalid")
    (tmp_path / "certificate-arn").write_text("example-certificate")
    (tmp_path / "resources.json").write_text('{"StackResourceSummaries":[{"ResourceType":"AWS::SecretsManager::Secret","PhysicalResourceId":"example-secret"}]}')
    (tmp_path / "acm-record.json").write_text('{"Name":"_example.mcp.example.invalid."}')
    checked = []
    monkeypatch.setattr(check, "absent", lambda args, *error: checked.append(args))
    monkeypatch.setattr(check, "records", lambda: [{"Name": "unrelated.example.invalid."}])
    def read(*args):
        if args[:2] == ("ecs", "describe-clusters"):
            return {"clusters": [], "failures": [{"reason": "MISSING"}]}
        if args[:2] == ("logs", "describe-log-groups"):
            return {"logGroups": [{"logGroupName": "/ecs/unshackle-example-neighbor"}]}
        return {"taskDefinitionArns": ["example/task-definition/unshackle-example-neighbor:1"]}
    monkeypatch.setattr(check, "read", read)
    check.main()
    assert ("secretsmanager", "describe-secret", "--secret-id", "example-secret") in checked
    monkeypatch.setattr(check, "read", lambda *args: ({"clusters": []} if args[1] == "describe-clusters" else
                                                    {"taskDefinitionArns": ["example/task-definition/unshackle-example:1"]}))
    with pytest.raises(RuntimeError, match="deletion is not finished"):
        check.main()
    # Check the API boundary separately: permission failure must not count as absence.
    fresh = module("verify_deleted")
    monkeypatch.setattr(fresh, "aws", lambda *a: subprocess.CompletedProcess(a, 1, "", "AccessDenied"))
    with pytest.raises(RuntimeError, match="auth/permissions"):
        fresh.read("ecs", "describe-clusters", "--clusters", "unshackle-example")
