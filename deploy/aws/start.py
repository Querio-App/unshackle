#!/usr/bin/env python3
"""Provider-only fixture URL and optional private Git files; never log secrets."""
import os
from pathlib import Path
from urllib.parse import quote


def configure():
    password = os.environ.pop("READER_PASSWORD")
    os.environ["UNSHACKLE_WAREHOUSE_URL"] = (
        "postgresql+psycopg://example_reader:" + quote(password, safe="")
        + "@127.0.0.1:5432/example?connect_timeout=10"
    )
    private_key = os.environ.pop("GIT_PRIVATE_KEY", "")
    known_hosts = os.environ.pop("GIT_KNOWN_HOSTS", "")
    if private_key:
        if not known_hosts:
            raise SystemExit("Private Git requires verified known_hosts")
        ssh = Path("/tmp/unshackle-git")
        ssh.mkdir(parents=True, mode=0o700, exist_ok=True)
        for name, value in (("id_ed25519", private_key), ("known_hosts", known_hosts)):
            fd = os.open(ssh / name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(value)


def verify_reader():
    """Check database grants directly, including after disabling the role default."""
    import psycopg
    from sqlalchemy.engine import make_url
    url = make_url(os.environ["UNSHACKLE_WAREHOUSE_URL"])
    try:
        with psycopg.connect(host=url.host, port=url.port, dbname=url.database,
                             user=url.username, password=url.password, connect_timeout=10) as con:
            for statement in ("DELETE FROM orders", "CREATE TABLE public.forbidden_probe (id int)"):
                try:
                    con.execute("BEGIN READ WRITE")
                    con.execute(statement)
                except psycopg.errors.InsufficientPrivilege:
                    pass
                else:
                    raise RuntimeError("Reader unexpectedly has write privileges")
                finally:
                    con.rollback()
            assert con.execute("SELECT COUNT(*) FROM orders").fetchone() == (6,)
        print("Synthetic fixture reader checks passed", flush=True)
    except Exception:
        raise SystemExit("Synthetic fixture reader checks failed") from None


if __name__ == "__main__":
    configure()
    verify_reader()
    os.execv("/usr/local/bin/unshackle-http", ["unshackle-http"])
