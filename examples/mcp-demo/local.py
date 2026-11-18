#!/usr/bin/env python3
"""Create a fresh local Postgres fixture. No cloud deployment or user-config changes."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import time

import psycopg
from psycopg import sql
from sqlalchemy.engine import URL

from unshackle import build, scaffold
from unshackle.imports import dbt, sql as sql_import, warehouse

HERE = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New context directory, outside the source checkout")
    parser.add_argument("--keep", action="store_true", help="Keep local container for client testing; otherwise remove it")
    args = parser.parse_args()
    repo = args.output.resolve()
    if repo.exists():
        parser.error("output must not already exist")
    name = "unshackle-mcp-example-" + secrets.token_hex(4)
    admin_password, reader_password = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    env = {**os.environ, "POSTGRES_PASSWORD": admin_password}
    subprocess.run(["docker", "run", "-d", "--name", name, "-p", "127.0.0.1::5432", "-e", "POSTGRES_PASSWORD",
                    "-e", "POSTGRES_DB=example", "postgres:16-alpine"], env=env, check=True, stdout=subprocess.DEVNULL)
    succeeded = False
    try:
        for _ in range(60):
            if subprocess.run(["docker", "exec", name, "pg_isready", "-U", "postgres", "-d", "example"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                break
            time.sleep(.5)
        else:
            raise RuntimeError("Local Postgres did not become ready")
        port = int(subprocess.check_output(["docker", "port", name, "5432/tcp"], text=True).strip().rsplit(":", 1)[1])
        with psycopg.connect(host="127.0.0.1", port=port, dbname="example", user="postgres", password=admin_password) as con:
            con.execute((HERE / "schema.sql").read_text())
            con.execute(sql.SQL("CREATE ROLE example_reader LOGIN PASSWORD {}").format(sql.Literal(reader_password)))
            con.execute("GRANT CONNECT ON DATABASE example TO example_reader")
            con.execute("GRANT USAGE ON SCHEMA public TO example_reader")
            con.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO example_reader")
            con.execute("ALTER ROLE example_reader SET default_transaction_read_only = on")
        url = URL.create("postgresql+psycopg", username="example_reader", password=reader_password,
                         host="127.0.0.1", port=port, database="example", query={"connect_timeout": "10"}).render_as_string(hide_password=False)
        scaffold.init(repo)
        build.apply(warehouse.introspect(url), repo)
        build.apply(dbt.parse(HERE / "dbt"), repo)
        build.apply(sql_import.parse([HERE / "inputs"]), repo)
        credential_path = repo / ".env"
        descriptor = os.open(credential_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(f"UNSHACKLE_WAREHOUSE_URL={url}\n")
        (repo / "local-container.json").write_text(json.dumps({"name": name, "port": port}, indent=2) + "\n")
        with (repo / ".gitignore").open("a") as stream:
            stream.write("\nlocal-container.json\n")
        print(f"Context ready: {repo}. Credentials are in its ignored .env (mode 0600).")
        print(f"Local container: {name}. Stop with: docker rm -f {name}")
        succeeded = True
    finally:
        if not args.keep or not succeeded:
            subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
