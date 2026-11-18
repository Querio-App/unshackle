"""Explicit, read-only SQLAlchemy reflection. Never serialize the connection URL."""
from __future__ import annotations

import json

from sqlalchemy import create_engine, inspect

from unshackle.ir import Bundle, Column, Table, warehouse_type

SYSTEM_SCHEMAS = {"information_schema", "pg_catalog", "mysql", "performance_schema", "sys"}


def introspect(url: str) -> Bundle:
    """Read visible base tables. Fail closed on access errors; do not publish a partial schema.

    Dialects may explicitly decline optional metadata with NotImplementedError.
    Such omissions become warnings rather than invented comments/constraints.
    """
    engine = None
    try:
        engine = create_engine(url, echo=False, hide_parameters=True)
        with engine.connect() as connection:
            return _reflect(inspect(connection), engine.dialect.name)
    except Exception:
        # Driver messages can contain connection strings, passwords and hostnames.
        raise ValueError(
            "Warehouse introspection failed. Check UNSHACKLE_WAREHOUSE_URL, the installed "
            "SQLAlchemy driver, and metadata permissions. No partial schema was imported."
        ) from None
    finally:
        if engine is not None:
            engine.dispose()


def _reflect(inspector, dialect: str) -> Bundle:
    bundle = Bundle(tool="warehouse", datasource="warehouse", warehouse=warehouse_type(dialect))

    def optional(method: str, table: str, schema: str, default):
        try:
            return getattr(inspector, method)(table, schema=schema)
        except NotImplementedError:
            bundle.warn(f"{schema}.{table}", f"{method} is unsupported by this dialect; metadata is unknown")
            return default

    schemas = sorted(s for s in inspector.get_schema_names()
                     if s not in SYSTEM_SCHEMAS and not s.startswith(("pg_toast", "pg_temp_")))
    for schema in schemas:
        for name in sorted(inspector.get_table_names(schema=schema)):
            columns = []
            for raw in sorted(inspector.get_columns(name, schema=schema), key=lambda c: c["name"]):
                columns.append(Column(
                    name=raw["name"], type=str(raw["type"]), description=raw.get("comment") or "",
                    nullable=raw.get("nullable"),
                ))
            primary = optional("get_pk_constraint", name, schema, {}) or {}
            foreign = optional("get_foreign_keys", name, schema, []) or []
            comment = optional("get_table_comment", name, schema, {}) or {}
            keys = [{
                "columns": list(key.get("constrained_columns") or []),
                "referred_schema": key.get("referred_schema") or schema,
                "referred_table": key["referred_table"],
                "referred_columns": list(key.get("referred_columns") or []),
            } for key in foreign]
            bundle.tables.append(Table(
                datasource="warehouse", schema=schema, name=name,
                description=comment.get("text") or "", columns=columns,
                primary_key=list(primary.get("constrained_columns") or []),
                foreign_keys=sorted(keys, key=lambda k: json.dumps(k, sort_keys=True)),
                source_ref=f"warehouse:{schema}.{name}",
            ))
    return bundle
