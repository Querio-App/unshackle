"""Tableau calculation semantics: every case in fixtures/tableau_calc/cases.yaml, run through the Tableau source.

Each case is a formula over a table `t` with typed columns, its input rows and the result Tableau's rules give
(`rule` says which). The cases go into hand-written published data sources as calculated fields, one on a live
connection and one with an extract (`connection: extract`); parse() turns them into dimensions (row-level formulas)
and measures (aggregates). Their SQL runs on the rows in DuckDB, and in Postgres when UNSHACKLE_TEST_POSTGRES_URL
points at a database (a temporary table; the database's locale should be C.UTF-8).

Case keys: formula, rows, expected (a list for a row-level formula, one value for an aggregate), rule; optionally
expected_postgres / expected_duckdb (where the result follows the warehouse, as on Tableau's live connection),
connection (live | extract), notes (text the semantic model's description must contain) and todo (text of the
TODO the formula must get instead of SQL).

To add a function: add its row to `FUNCS` in src/unshackle/sources/_tableau_calc.py and its cases to cases.yaml,
with at least one NULL input.
"""

from __future__ import annotations

import datetime as dt
import math
import os
import re
from decimal import Decimal
from pathlib import Path
from xml.sax.saxutils import quoteattr

import pytest
import yaml

from unshackle.raw import RawStore
from unshackle.sources import _dialect, _tableau_calc as tc
from unshackle.sources import get_source
from unshackle.sources.tableau import AGG_SQL

CASES = yaml.safe_load((Path(__file__).parent / "fixtures" / "tableau_calc" / "cases.yaml").read_text())
# column -> (Tableau datatype, metadata remote-type, DuckDB type, Postgres type). `n` is a real stored as a decimal.
COLUMNS = {
    "i": ("integer", 20, "integer", "integer"),
    "j": ("integer", 20, "integer", "integer"),
    "r": ("real", 5, "double", "double precision"),
    "n": ("real", 131, "decimal(18, 6)", "numeric(18, 6)"),
    "s": ("string", 129, "varchar", "text"),
    "u": ("string", 129, "varchar", "text"),
    "d": ("date", 7, "date", "date"),
    "e": ("date", 7, "date", "date"),
    "b": ("boolean", 11, "boolean", "boolean"),
}
CONNECTIONS = ("live", "extract")


def _is_agg(case: dict) -> bool:
    return bool(case.get("aggregate")) or ("expected" in case and not isinstance(case["expected"], list))


def _conn(case: dict) -> str:
    return case.get("connection", "live")


def _tds(engine: str, connection: str) -> str:
    """A published data source on table t with every case of `connection` as a calculated field."""
    records = "".join(
        f"<metadata-record class='column'><remote-name>{c}</remote-name><remote-type>{rt}</remote-type><local-name>[{c}]</local-name>"
        f"<parent-name>[t]</parent-name><local-type>{typ}</local-type><aggregation>Sum</aggregation></metadata-record>"
        for c, (typ, rt, _, _) in COLUMNS.items()
    )
    calcs = "".join(
        f"<column caption='case_{k}' datatype='{'real' if _is_agg(c) else 'string'}' name='[Calculation_{k}]' "
        f"role='{'measure' if _is_agg(c) else 'dimension'}' type='{'quantitative' if _is_agg(c) else 'nominal'}'>"
        f"<calculation class='tableau' formula={quoteattr(c['formula'])} /></column>"
        for k, c in enumerate(CASES) if _conn(c) == connection
    )
    extract = ("<extract count='-1' enabled='true' units='records'><connection class='hyper' dbname='Data/Extracts/t.hyper' "
               "schema='Extract' tablename='Extract'><relation name='Extract' table='[Extract].[Extract]' type='table' /></connection>"
               "</extract>") if connection == "extract" else ""
    return (
        "<?xml version='1.0' encoding='utf-8' ?>"
        "<datasource formatted-name='federated.calc' inline='true' version='18.1'><connection class='federated'>"
        "<named-connections><named-connection caption='warehouse.example.com' name='wh.1'>"
        f"<connection class='{engine}' dbname='analytics' server='warehouse.example.com' /></named-connection></named-connections>"
        "<relation connection='wh.1' name='t' table='[public].[t]' type='table' />"
        f"<metadata-records>{records}</metadata-records></connection>{calcs}{extract}</datasource>"
    )


def _sql(tmp_path: Path, engine: str) -> dict[int, str]:
    """Each case's SQL as the Tableau source writes it for `engine` (cases that expect a TODO are checked here)."""
    out: dict[int, str] = {}
    for connection in CONNECTIONS:
        root = tmp_path / engine / connection
        raw = root / "migration" / "raw" / "tableau" / "datasources"
        raw.mkdir(parents=True)
        (raw / "calc.tds").write_text(_tds(engine, connection))
        b = get_source("tableau").parse(RawStore(root, "tableau"))
        assert b.warehouse == engine
        sm = next(m for m in b.semantic_models if m.name == "t")
        dims = {d.name: d for d in sm.dimensions}
        measures = {m.name: m for m in sm.measures}
        todos = {m.name: m.todo for m in b.metrics if m.todo} | {d.name: d.todo for d in sm.dimensions if d.todo}
        for k, case in enumerate(CASES):
            name = f"case_{k}"
            if _conn(case) != connection:
                continue
            for note in case.get("notes", []):
                assert note in sm.description, f"{case['formula']}: no {note!r} in the model description"
            if "todo" in case:
                assert case["todo"] in todos.get(name, ""), f"{case['formula']}: expected a TODO, got {todos.get(name)!r}"
                continue
            assert name not in todos, f"{case['formula']}: {todos[name]}"
            if _is_agg(case):
                m = measures[name]
                # the measure's expression is in the warehouse's dialect already; the aggregate as the semantic layer writes it
                out[k] = _dialect.render(AGG_SQL[m.agg].format("unshackle_x"), b.warehouse).replace("unshackle_x", m.expr or m.name)
            else:
                out[k] = dims[name].expr
    return out


def _value(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, dt.datetime):
        return v.date() if v.time() == dt.time() else v
    return v


def _same(got, want) -> bool:
    got = _value(got)
    if want is None or got is None:
        return got is None and want is None
    if isinstance(want, bool) or isinstance(got, bool):
        return got is want
    if isinstance(want, (int, float)) and isinstance(got, (int, float)):
        return math.isclose(got, want, rel_tol=1e-9, abs_tol=1e-9)
    return got == want


def _literal(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, dt.date):
        return f"date '{v.isoformat()}'"
    return "'" + str(v).replace("'", "''") + "'"


def _run(engine: str, execute, create: str, sqls: dict[int, str]) -> list[str]:
    failures = []
    for k, case in enumerate(CASES):
        if k not in sqls:
            continue
        execute("drop table if exists t")
        execute(create)
        for idx, row in enumerate(case.get("rows") or [{}]):
            unknown = set(row) - set(COLUMNS)
            assert not unknown, f"{case['formula']}: unknown columns {unknown}"
            execute(f"insert into t values ({', '.join([str(idx)] + [_literal(row.get(c)) for c in COLUMNS])})")
        sql = sqls[k]
        want = case.get(f"expected_{engine}", case["expected"])
        try:
            if _is_agg(case):
                got, want = [execute(f"select {sql} from t")[0][0]], [want]
            else:
                got = [r[0] for r in execute(f"select {sql} from t order by k")]
        except Exception as exc:  # noqa: BLE001 - report every failing case at once
            failures.append(f"{case['formula']}: {sql}: {exc}")
            continue
        if len(got) != len(want) or not all(_same(g, w) for g, w in zip(got, want)):
            failures.append(f"{case['formula']}: {sql}: got {[_value(g) for g in got]}, want {want}")
    return failures


def _has_null(case: dict) -> bool:
    """Some row has a NULL (missing) value in a column the formula reads."""
    cols = set(re.findall(r"\[(\w+)\]", case["formula"]))
    return any(row.get(c) is None for row in case.get("rows") or [] for c in cols)


def test_every_function_has_a_case_with_a_null():
    for case in CASES:
        assert case.get("rule"), case["formula"]
        assert "expected" in case or "todo" in case, case["formula"]
    for name in set(tc.FUNCS) | {"round", "iif"}:
        mine = [c for c in CASES if f"{name.upper()}(" in c["formula"].upper() and "todo" not in c]
        assert mine, f"no case for {name.upper()}"
        assert any(_has_null(c) for c in mine), f"no NULL case for {name.upper()}"


def test_cases_in_duckdb(tmp_path: Path):
    duckdb = pytest.importorskip("duckdb")
    sqls = _sql(tmp_path, "duckdb")
    con = duckdb.connect()
    create = "create table t (k integer, " + ", ".join(f"{c} {t[2]}" for c, t in COLUMNS.items()) + ")"
    failures = _run("duckdb", lambda q: con.execute(q).fetchall() if q.lstrip().startswith("select") else con.execute(q), create, sqls)
    assert not failures, "\n".join(failures)


def test_cases_in_postgres(tmp_path: Path):
    url = os.environ.get("UNSHACKLE_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("UNSHACKLE_TEST_POSTGRES_URL is not set")
    psycopg = pytest.importorskip("psycopg")
    sqls = _sql(tmp_path, "postgres")
    with psycopg.connect(url, autocommit=True) as con:
        def execute(q: str):
            cur = con.execute(q)
            return cur.fetchall() if cur.description else None

        # String order and case mapping follow the database's collation, as on Tableau's live connection; the cases
        # assume a binary order with Unicode case mapping (a database created with --locale=C.UTF-8).
        create = "create temporary table t (k integer, " + ", ".join(f"{c} {t[3]}" for c, t in COLUMNS.items()) + ")"
        failures = _run("postgres", execute, create, sqls)
    assert not failures, "\n".join(failures)


def test_postgres_round_and_division():
    # Tableau's Postgres connection sends ROUND with digits on a numeric and divides in double precision
    assert _dialect.render("round(x, 1)", "postgres") == "round(cast(x as numeric), 1)"
    assert _dialect.render("round(x)", "postgres") == "round(x)"
    assert _dialect.render("1.0 * a / nullif(b, 0)", "postgres") == "cast(1.0 as double precision) * a / nullif(b, 0)"
    assert _dialect.render("round(x, 1)", "duckdb") == "round(x, 1)"
