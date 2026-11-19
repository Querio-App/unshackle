"""Railway preparation checks; optional isolated Docker, never Railway API calls."""
import importlib.util
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "deploy/railway" / f"{name}.py")
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_fixture_rejects_non_postgres_and_nonlocal_test_connections():
    fixture = module("fixture")
    with pytest.raises(ValueError):
        fixture.connection_url("sqlite:///example.db")
    with pytest.raises(ValueError):
        fixture.connection_url("postgresql://example_reader:example@example.test/example", local=True)
    url = fixture.connection_url("postgres://example_reader:example@example.test/example?sslmode=disable")
    assert url.drivername == "postgresql+psycopg"
    assert url.query == {"sslmode": "require", "connect_timeout": "10"}


def test_probe_rejects_unsafe_endpoint_without_exposing_token(monkeypatch, capsys):
    probe = module("check")
    monkeypatch.setenv("UNSHACKLE_MCP_TOKEN", "example-private-token-never-print")
    for url in ("http://example.test/mcp", "https://token@example.test/mcp", "https://example.test/mcp?token=example"):
        monkeypatch.setenv("UNSHACKLE_MCP_URL", url)
        assert probe.main() == 1
    output = capsys.readouterr()
    assert not output.out and "token" not in output.err and "example.test" not in output.err


def test_fixture_failure_does_not_emit_database_credentials(tmp_path):
    result = subprocess.run([sys.executable, str(ROOT / "deploy/railway/fixture.py"), "--output", str(tmp_path / "fixture")],
                            env={**os.environ, "DATABASE_PUBLIC_URL": "invalid-secret-url",
                                 "DATABASE_URL": "another-secret-url"}, capture_output=True, text=True)
    assert result.returncode == 1
    assert "Fixture failed" in result.stderr
    assert "secret-url" not in result.stdout + result.stderr and "Traceback" not in result.stderr


@pytest.mark.skipif(os.environ.get("UNSHACKLE_RAILWAY_LOCAL_TEST") != "1", reason="explicit isolated Docker opt-in required")
def test_real_postgres_seed_permissions_context_and_http(tmp_path, monkeypatch):
    import psycopg
    from sqlalchemy.engine import URL
    from starlette.testclient import TestClient
    from unshackle.mcp.http import create_app

    fixture, probe = module("fixture"), module("check")
    name = "unshackle-railway-test-" + secrets.token_hex(5)
    password = secrets.token_urlsafe(24)
    output = tmp_path / "fixture"
    started = False
    try:
        subprocess.run(["docker", "run", "-d", "--name", name, "-p", "127.0.0.1::5432", "-e", "POSTGRES_PASSWORD",
                        "-e", "POSTGRES_DB=example", "postgres:16-alpine"],
                       env={**os.environ, "POSTGRES_PASSWORD": password}, check=True, capture_output=True)
        started = True
        for _ in range(100):
            if subprocess.run(["docker", "exec", name, "pg_isready", "-h", "127.0.0.1", "-U", "postgres", "-d", "example"],
                              capture_output=True).returncode == 0:
                break
            time.sleep(.2)
        else:
            pytest.fail("Isolated Postgres did not start")
        port = int(subprocess.check_output(["docker", "port", name, "5432/tcp"], text=True).strip().rsplit(":", 1)[1])
        url = URL.create("postgresql", username="postgres", password=password, host="127.0.0.1", port=port, database="example")
        for key in ("DATABASE_URL", "DATABASE_PUBLIC_URL"):
            monkeypatch.setenv(key, url.render_as_string(hide_password=False))
        fixture.seed(output, local=True)
        reader_url = (output / "warehouse-url").read_text()
        token = (output / "mcp-token").read_text()
        assert output.stat().st_mode & 0o777 == 0o700
        for secret_name in ("warehouse-url", "reader-public-url", "mcp-token"):
            assert (output / secret_name).stat().st_mode & 0o777 == 0o600
        from sqlalchemy.engine import make_url
        reader = make_url(reader_url)
        for path in (output / "context").rglob("*"):
            if path.is_file():
                content = path.read_bytes()
                assert password.encode() not in content
                assert reader.password.encode() not in content
                assert token.encode() not in content
        with fixture.connect(reader) as con:
            assert callable(con.cancel)  # psycopg native cancellation, not an unsupported driver
            for statement in ("DELETE FROM orders WHERE false", "CREATE TABLE forbidden(id int)", "CREATE TEMP TABLE forbidden(id int)"):
                con.execute("SET TRANSACTION READ WRITE")
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    con.execute(statement)
                con.rollback()
        with pytest.raises(ValueError, match="already exists"):
            fixture.seed(tmp_path / "rerun", local=True)
        monkeypatch.setenv("UNSHACKLE_WAREHOUSE_URL", reader_url)
        monkeypatch.setenv("UNSHACKLE_MCP_TOKEN", token)
        with TestClient(create_app(output / "context", timeout=.3), base_url="http://localhost:8000") as client:
            probe.check(client, "/mcp", token, deadline=.3)
    finally:
        if started:
            subprocess.run(["docker", "rm", "-fv", name], check=True, capture_output=True)
        shutil.rmtree(output, ignore_errors=True)
        shutil.rmtree(tmp_path / "rerun", ignore_errors=True)


def test_documented_shell_syntax_and_railway_build_input(tmp_path):
    import re
    document = (ROOT / "docs/deploy/railway.md").read_text()
    for block in re.findall(r"```bash\n(.*?)\n```", document, re.S):
        subprocess.run(["bash", "-n"], input=block, text=True, check=True)
    original = (ROOT / "Dockerfile").read_text()
    assert original.count('VOLUME ["/tmp"]\n') == 1
    # Execute the exact provider adaptation: all other image instructions survive.
    result = subprocess.run(["sed", '/^VOLUME \\["\\/tmp"\\]$/d'], input=original,
                            text=True, check=True, capture_output=True)
    assert result.stdout == original.replace('VOLUME ["/tmp"]\n', '')
    assert 'USER 10001:10001' in result.stdout
    assert 'ENTRYPOINT ["/usr/local/bin/unshackle-http"]' in result.stdout


def test_documented_secret_commands_use_stdin_not_argv(tmp_path):
    import json
    import re
    state = tmp_path / "state"
    (state / "fixture").mkdir(parents=True)
    (state / "fixture/warehouse-url").write_text("example-private-warehouse")
    (state / "fixture/mcp-token").write_text("example-private-token")
    executable = tmp_path / "railway"
    executable.write_text(f'''#!{sys.executable}
import json, os, sys
with open(os.environ['CAPTURE'], 'a') as f:
    f.write(json.dumps({{"argv": sys.argv[1:], "stdin": sys.stdin.read()}}) + '\\n')
''')
    executable.chmod(0o700)
    document = (ROOT / "docs/deploy/railway.md").read_text()
    commands = re.findall(r'railway variable set[^\n]*\\\n  UNSHACKLE_(?:WAREHOUSE_URL|MCP_TOKEN) --stdin[^\n]*', document)
    assert len(commands) == 2
    capture = tmp_path / "commands.jsonl"
    subprocess.run(["bash", "-e", "-c", "\n".join(commands)], check=True,
                   env={**os.environ, "PATH": str(tmp_path)+os.pathsep+os.environ["PATH"],
                        "STATE": str(state), "RW_SERVICE": "example-mcp", "RW_ENVIRONMENT": "production",
                        "CAPTURE": str(capture)})
    entries = [json.loads(line) for line in capture.read_text().splitlines()]
    assert [e['stdin'] for e in entries] == ["example-private-warehouse", "example-private-token"]
    assert all('example-private' not in ' '.join(e['argv']) for e in entries)
    assert all('--skip-deploys' in e['argv'] and '--stdin' in e['argv'] for e in entries)
