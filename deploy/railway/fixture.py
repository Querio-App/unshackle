#!/usr/bin/env python3
"""Seed a NEW dedicated Railway Postgres service and generate synthetic context.

Reads DATABASE_PUBLIC_URL and DATABASE_URL from `railway run`; never prints them.
This writes database objects. Run only after authorizing the deployment phase.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import secrets
import sys

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

from unshackle import build, scaffold
from unshackle.imports import dbt, sql as sql_import, warehouse

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "examples/mcp-demo"
ROLE = "example_reader"


def private_file(path: Path, value: str):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        stream.write(value)


def connection_url(value: str, *, local: bool = False):
    url = make_url(value)
    if url.drivername not in {"postgres", "postgresql", "postgresql+psycopg"}:
        raise ValueError("PostgreSQL required")
    if local and url.host not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Local test must use loopback")
    return url.set(drivername="postgresql+psycopg").update_query_dict(
        {"sslmode": "disable" if local else "require", "connect_timeout": "10"}
    )


def connect(url):
    return psycopg.connect(url.set(drivername="postgresql").render_as_string(hide_password=False))


def seed(output: Path, *, local: bool = False):
    output = output.resolve()
    if output.is_relative_to(ROOT):
        raise ValueError("Keep credentials outside the source checkout")
    public = connection_url(os.environ["DATABASE_PUBLIC_URL"], local=local)
    private = connection_url(os.environ["DATABASE_URL"], local=local)
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    password = secrets.token_urlsafe(32)
    reader_public = public.set(username=ROLE, password=password)
    reader_private = private.set(username=ROLE, password=password)
    # Save before DB changes so a failed import does not lose the generated credentials.
    private_file(output / "warehouse-url", reader_private.render_as_string(hide_password=False))
    private_file(output / "reader-public-url", reader_public.render_as_string(hide_password=False))
    private_file(output / "mcp-token", secrets.token_urlsafe(32))
    with connect(public) as con:
        if con.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (ROLE,)).fetchone():
            raise ValueError("Reader role already exists; use a fresh dedicated service")
        if con.execute("SELECT 1 FROM pg_tables WHERE schemaname NOT IN ('pg_catalog', 'information_schema')").fetchone():
            raise ValueError("Database is not empty; use a fresh dedicated service")
        con.execute((DEMO / "schema.sql").read_text())
        con.execute(sql.SQL("CREATE ROLE example_reader LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD {}")
                    .format(sql.Literal(password)))
        database = sql.Identifier(public.database)
        con.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(database))
        con.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO example_reader").format(database))
        con.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        con.execute("GRANT USAGE ON SCHEMA public TO example_reader")
        con.execute("GRANT SELECT ON customers, orders TO example_reader")
        con.execute("ALTER ROLE example_reader SET default_transaction_read_only = on")
    # A transaction default is not a permission boundary. Check grants even with it off.
    with connect(reader_public) as con:
        assert con.execute("SHOW transaction_read_only").fetchone() == ("on",)
        con.execute("SET TRANSACTION READ WRITE")
        try:
            con.execute("UPDATE orders SET amount = amount WHERE false")
        except psycopg.errors.InsufficientPrivilege:
            con.rollback()
        else:
            raise RuntimeError("Reader unexpectedly has write access")
    repo = output / "context"
    scaffold.init(repo)
    build.apply(warehouse.introspect(reader_public.render_as_string(hide_password=False)), repo)
    build.apply(dbt.parse(DEMO / "dbt"), repo)
    build.apply(sql_import.parse([DEMO / "inputs"]), repo)
    print("Synthetic context generated; reader grants checked; secrets saved outside context (0600).")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New directory outside source checkout")
    parser.add_argument("--local-test", action="store_true", help="Loopback-only Postgres without TLS")
    args = parser.parse_args()
    try:
        seed(args.output, local=args.local_test)
    except Exception:
        # Driver/SQL exceptions can contain passwords. Never emit their text or traceback.
        print("Fixture failed. Check fresh database, TLS/connectivity and output path. Keep private files for recovery.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
