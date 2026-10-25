"""Looker's own generated SQL and results (tests/fixtures/looker_generated/, #41) against unshackle.

The evidence views read literal rows, so DuckDB loads the same rows from the LookML and runs
unshackle's translation; the numbers must be Looker's. What unshackle doesn't translate yet must
stay a TODO, and the fix for it turns that assertion into a numeric one.
"""

import json
import shutil
from pathlib import Path

import duckdb
import lkml
import pytest
from test_looker import _by_name

from unshackle.ir import Bundle
from unshackle.raw import RawStore
from unshackle.sources import get_source
from unshackle.sources.looker import DIALECT_DATES, TF_GRAIN, TIMEFRAME_SQL, timeframe_sql
from unshackle.target.ossie import AGGS

FIX = Path(__file__).parent / "fixtures" / "looker_generated"


def looker(case: str) -> list[dict]:
    return json.loads((FIX / f"{case}.json").read_text())


@pytest.fixture(scope="module")
def bundle(tmp_path_factory) -> Bundle:
    repo = tmp_path_factory.mktemp("looker_generated")
    lookml = repo / "migration" / "raw" / "looker" / "lookml"
    lookml.mkdir(parents=True)
    shutil.copy(FIX / "evidence.view.lkml", lookml / "unshackle_evidence.view.lkml")
    explores = (FIX / "evidence.explores.lkml").read_text()
    (lookml / "evidence.model.lkml").write_text(f'connection: "bq"\ninclude: "/unshackle_evidence.view"\n{explores}')
    return get_source("looker").parse(RawStore(repo, "looker"))


@pytest.fixture(scope="module")
def db() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    for view in lkml.load((FIX / "evidence.view.lkml").read_text())["views"]:
        if view["name"] in ("ev_time", "ev_orders", "ev_items"):
            con.execute(f"create table {view['name']} as {view['derived_table']['sql']}")
    return con


def test_fixtures_hold_no_instance_details():
    for f in FIX.iterdir():
        text = f.read_text()
        assert "qwiklabs" not in text and "@" not in text.replace("@{", ""), f.name


def test_time_grains_match_looker(bundle, db):
    """MetricFlow truncates to the grain; Looker formats the truncated value. Same day, week start
    (Monday) and quarter start as Looker on BigQuery."""
    dims = _by_name(_by_name(bundle.semantic_models)["ev_time"].dimensions)
    assert (dims["created"].type, dims["created"].expr) == ("time", "ts")
    fmt = {"date": "%Y-%m-%d", "week": "%Y-%m-%d", "month": "%Y-%m", "quarter": "%Y-%m", "year": "%Y"}
    for tf, f in fmt.items():
        ours = dict(db.execute(f"select id, strftime(date_trunc('{TF_GRAIN[tf]}', ts), '{f}') from ev_time").fetchall())
        theirs = {r["ev_time.id"]: r[f"ev_time.created_{tf}"] for r in looker("time")}
        assert {k: (str(v) if v is not None else None) for k, v in theirs.items()} == ours, tf


def test_plain_measures_match_looker(bundle, db):
    measures = _by_name(_by_name(bundle.semantic_models)["ev_orders"].measures)
    cols = {name: AGGS[m.agg].format(m.expr) for name, m in measures.items()}
    rows = db.execute(f"select customer, {', '.join(cols.values())} from ev_orders group by 1 order by 1 nulls first").fetchall()
    ours = [dict(zip(["customer", *cols], r)) for r in rows]
    theirs = looker("measure_types")
    assert [r["customer"] for r in ours] == [r["ev_orders.customer"] for r in theirs]
    for o, t in zip(ours, theirs):
        assert o["ev_orders_count"] == t["ev_orders.count"]
        assert o["total_shipping"] == pytest.approx(t["ev_orders.total_shipping"])
        assert o["avg_shipping"] == pytest.approx(t["ev_orders.avg_shipping"])


def test_distinct_and_fanout_numbers(db):
    """What a translation has to reproduce: sum_distinct counts each key once (#31), and measures on
    the one side of a one-to-many join are not inflated (symmetric aggregates)."""
    total = looker("distinct_total")[0]
    distinct = db.execute("select sum(s), avg(s) from (select distinct order_id, order_shipping s from ev_items)").fetchone()
    assert (total["ev_items.shipping_sum_distinct"], total["ev_items.shipping_avg_distinct"]) == pytest.approx(distinct)
    assert total["ev_items.shipping_naive_sum"] == db.execute("select sum(order_shipping) from ev_items").fetchone()[0]
    fan = looker("fanout_total")[0]
    joined = db.execute("select count(distinct o.id), sum(o.shipping) from ev_orders o left join ev_items i on o.id = i.order_id").fetchone()
    assert fan["ev_orders.count"] == joined[0]
    assert fan["ev_orders.total_shipping"] == db.execute("select sum(shipping) from ev_orders").fetchone()[0] != joined[1]


def test_untranslated_stay_todo(bundle):
    metrics = _by_name(bundle.metrics)
    for name in ("shipping_pct", "shipping_running", "order_ids", "shipping_sum_distinct", "shipping_avg_distinct"):  # #31
        assert metrics[name].todo, name
    for name in ("chosen_total", "chosen_label"):  # #29
        assert "Liquid" in metrics[name].todo, name
    assert "Liquid" in _by_name(_by_name(bundle.semantic_models)["ev_liquid"].dimensions)["user_locale"].todo
    # #30: the UNNEST join is not modelled, and says so
    assert any("UNNEST" in w.message for w in bundle.warnings if "ev_nested_lines" in w.item)


# ---------------------------------------------------------------- timeframes (#18)

TIMEFRAMES = ["date", "week", "month", "quarter", "year", "day_of_week", "day_of_week_index", "month_num", "month_name",
              "quarter_of_year", "week_of_year", "day_of_month", "day_of_year", "hour_of_day"]


def _timeframe(tf: str, col: str, dialect: str = "duckdb", week_start: str = "monday") -> str:
    return TIMEFRAME_SQL[tf].format(col) if tf in TIMEFRAME_SQL else timeframe_sql(tf, col, dialect, week_start)


@pytest.mark.parametrize("case,week_start,fields", [
    ("time", "monday", [(f"created_{tf}", tf, "ts") for tf in TIMEFRAMES]
     + [(f"day_{tf}", tf, "d") for tf in ("date", "week", "month", "quarter", "year", "day_of_week")]),
    ("time_week_sunday", "sunday", [("created_week", "week", "ts"), ("created_day_of_week_index", "day_of_week_index", "ts"),
                                    ("created_week_of_year", "week_of_year", "ts"), ("day_week", "week", "d")]),
])
def test_timeframes_return_lookers_values(db, case, week_start, fields):
    for field, tf, col in fields:
        ours = dict(db.execute(f"select id, {_timeframe(tf, col, week_start=week_start)} from ev_time").fetchall())
        theirs = {r["ev_time.id"]: r[f"ev_time.{field}"] for r in looker(case)}
        assert {k: None if v is None else str(v) for k, v in ours.items()} == {k: None if v is None else str(v) for k, v in theirs.items()}, field


@pytest.mark.parametrize("dialect", sorted(DIALECT_DATES))
def test_timeframe_sql_parses_in_each_dialect(dialect):
    import sqlglot

    for week_start in ("monday", "sunday", "saturday"):
        for tf in TIMEFRAMES:
            sql = _timeframe(tf, "t.created_at", dialect, week_start)
            assert sql, (dialect, tf)
            sqlglot.parse_one(f"SELECT {sql} FROM t", read=dialect)
