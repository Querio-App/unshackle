#!/usr/bin/env python3
"""Prepare private secrets, then seed a fresh synthetic Postgres through a local proxy."""
import argparse
import json
import os
from pathlib import Path
import secrets
import sys

import psycopg
from psycopg import sql
from sqlalchemy.engine import URL

from unshackle import build, scaffold
from unshackle.imports import dbt, sql as sql_import, warehouse
from unshackle.run import query

DEMO = Path(__file__).resolve().parents[2] / "examples/mcp-demo"


def private_write(path, text):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as file:
        file.write(text)


def prepare(state):
    state.mkdir(mode=0o700, parents=True, exist_ok=False)
    private_write(state / "admin.json", json.dumps({"--password": secrets.token_urlsafe(32)}))
    private_write(state / "reader-password", secrets.token_urlsafe(32))
    private_write(state / "token", secrets.token_urlsafe(32))


def seed(state, output, connection_name, port):
    if output.exists():
        raise ValueError("Context output must be a new directory")
    admin = json.loads((state / "admin.json").read_text())["--password"]
    password = (state / "reader-password").read_text()
    with psycopg.connect(host="127.0.0.1", port=port, dbname="example", user="postgres", password=admin) as con:
        # One transaction; an existing fixture fails without changing it.
        con.execute((DEMO / "schema.sql").read_text())
        con.execute(sql.SQL("CREATE ROLE example_reader LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD {}").format(sql.Literal(password)))
        con.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        con.execute("REVOKE TEMPORARY ON DATABASE example FROM PUBLIC")
        con.execute("GRANT CONNECT ON DATABASE example TO example_reader")
        con.execute("GRANT USAGE ON SCHEMA public TO example_reader")
        con.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO example_reader")
        con.execute("ALTER ROLE example_reader SET default_transaction_read_only = on")
    url = URL.create("postgresql+psycopg", username="example_reader", password=password,
                     host="127.0.0.1", port=port, database="example", query={"connect_timeout": "10"})
    # Check privileges even with the convenience default switched off.
    with psycopg.connect(host="127.0.0.1", port=port, dbname="example", user="example_reader", password=password) as con:
        con.execute("SET default_transaction_read_only = off")
        con.commit()
        try:
            con.execute("DELETE FROM orders")
        except psycopg.errors.InsufficientPrivilege:
            con.rollback()
        else:
            con.rollback()
            raise AssertionError("Reader unexpectedly has DELETE privileges")
    os.environ["UNSHACKLE_WAREHOUSE_URL"] = url.render_as_string(hide_password=False)
    try:
        query("SELECT pg_sleep(5)", {}, limit=1, timeout=0.2)
    except TimeoutError:
        pass
    else:
        raise AssertionError("Native Postgres cancellation failed")
    assert query("SELECT 1 AS healthy", {}, limit=1, timeout=5).iloc[0, 0] == 1
    scaffold.init(output)
    build.apply(warehouse.introspect(os.environ["UNSHACKLE_WAREHOUSE_URL"]), output)
    build.apply(dbt.parse(DEMO / "dbt"), output)
    build.apply(sql_import.parse([DEMO / "inputs"]), output)
    cloud_url = URL.create("postgresql+psycopg", username="example_reader", password=password,
                           database="example", query={"host": "/cloudsql/" + connection_name, "connect_timeout": "10"})
    private_write(state / "warehouse-url", cloud_url.render_as_string(hide_password=False))
    print("Seeded synthetic data; DELETE denied, native cancellation and recovery passed; context generated.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["secrets", "seed"])
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--connection-name")
    parser.add_argument("--port", type=int, default=15432)
    args = parser.parse_args()
    try:
        if args.action == "secrets":
            prepare(args.state)
        else:
            if not args.output or not args.connection_name:
                parser.error("seed requires --output and --connection-name")
            seed(args.state, args.output, args.connection_name, args.port)
    except Exception:
        # Driver exceptions can contain SQL/passwords; do not emit their traceback.
        sys.exit("Fixture failed. Check proxy, fresh example database and private state files; no credentials logged.")


if __name__ == "__main__":
    main()
