"""SQL seatbelt and JSON result conversion. Warehouse permissions enforce read-only access."""
from __future__ import annotations

import datetime as dt
import decimal
import math
import os

import sqlglot
from sqlglot import exp

DEFAULT_LIMIT = 1000
MAX_LIMIT = 10000
DEFAULT_TIMEOUT = 30.0


def validate_sql(sql: str, dialect: str | None = None) -> str:
    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None and not isinstance(s, exp.Semicolon)]
    except sqlglot.errors.SqlglotError as exc:
        raise ValueError("SQL must parse as one read-only SELECT/WITH query") from exc
    if len(statements) != 1 or not isinstance(statements[0], (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        raise ValueError("Only one SELECT/WITH query is allowed")
    forbidden = (exp.DML, exp.DDL, exp.Command, exp.Into, exp.Lock)
    if any(isinstance(node, forbidden) for node in statements[0].walk()):
        raise ValueError("Writes, commands, SELECT INTO and locking queries are not allowed")
    return statements[0].sql(dialect=dialect, comments=False)


def json_value(value):
    import pandas as pd
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return str(value) if value.is_finite() else None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, bytes):
        return value.hex()
    if hasattr(value, 'item'):
        return json_value(value.item())
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    return value


def run_query(sql: str, limit: int = DEFAULT_LIMIT, *, max_rows: int = MAX_LIMIT,
              timeout: float = DEFAULT_TIMEOUT) -> dict:
    from unshackle.run import query
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= max_rows:
        raise ValueError(f"limit must be between 1 and {max_rows}")
    from sqlalchemy.engine import make_url
    try:
        backend = make_url(os.environ.get('UNSHACKLE_WAREHOUSE_URL', '')).get_backend_name()
    except Exception:
        backend = ''
    dialect = {'postgresql': 'postgres', 'duckdb': 'duckdb', 'sqlite': 'sqlite'}.get(backend)
    sql = validate_sql(sql, dialect)
    try:
        frame = query(sql, {}, limit=limit, timeout=timeout)
    except TimeoutError:
        raise
    except (Exception, SystemExit) as exc:
        # Drivers often include credentials, connection strings or SQL in their exceptions.
        raise ValueError("Warehouse query failed; check the read-only connection and SQL") from None
    return {'columns': [str(c) for c in frame.columns],
            'rows': [[json_value(v) for v in row] for row in frame.itertuples(index=False, name=None)],
            'row_count': len(frame), 'truncated': bool(frame.attrs.get('truncated', False))}
