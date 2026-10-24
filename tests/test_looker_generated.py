"""Looker's own generated SQL and results (tests/fixtures/looker_generated/, #41) against unshackle.

The evidence views read literal rows, so DuckDB loads the same rows from the LookML and runs
unshackle's translation; the numbers must be Looker's. What unshackle doesn't translate yet must
stay a TODO, and the fix for it turns that assertion into a numeric one.
"""

import json
import shutil
from decimal import Decimal
from pathlib import Path

import duckdb
import lkml
import pytest
from test_looker import _by_name

from unshackle.ir import Bundle
from unshackle.raw import RawStore
from unshackle.sources import get_source
from unshackle.sources.looker import DIALECT_DATES, SYM_HASH, TF_GRAIN, TIMEFRAME_SQL, timeframe_sql
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


# ---------------------------------------------------------------- round 2: dimension types and statistics


def _parse_evidence2(repo: Path, warehouse: str) -> Bundle:
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "unshackle.toml").write_text(f'[datasources.warehouse]\ntype = "{warehouse}"\n')
    store = RawStore(repo, "looker")
    store.put_text("lookml/unshackle_evidence2.view.lkml", (FIX / "evidence2.view.lkml").read_text())
    store.put_text("lookml/m.model.lkml", 'connection: "bq"\ninclude: "/unshackle_evidence2.view"\n' + (FIX / "evidence2.explores.lkml").read_text())
    return get_source("looker").parse(store)


@pytest.fixture(scope="module")
def bundle2(tmp_path_factory) -> Bundle:
    return _parse_evidence2(tmp_path_factory.mktemp("bq"), "bigquery")


@pytest.fixture(scope="module")
def db2() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    view = lkml.load((FIX / "evidence2.view.lkml").read_text())["views"][0]
    con.execute(f"create table ev_f as {view['derived_table']['sql'].replace('FLOAT64', 'DOUBLE')}")
    return con


def test_tier_and_case_dimensions_return_lookers_labels(bundle2, db2):
    dims = _by_name(_by_name(bundle2.semantic_models)["ev_f"].dimensions)
    for name in ("n_tier", "n_tier_classic", "s_case"):
        assert not dims[name].todo, name
        ours = dict(db2.execute(f"select id, {dims[name].expr} from ev_f").fetchall())
        assert ours == {r["ev_f.id"]: r[f"ev_f.{name}"] for r in looker("dimension_types")}, name


def test_percentiles_follow_lookers_bigquery_formula(bundle2, db2, tmp_path):
    metrics = _by_name(bundle2.metrics)
    for name in ("n_p25", "n_p90"):
        assert "on BigQuery" in metrics[name].todo, name
    # the median is the continuous median in Looker's formula too
    median = _by_name(_by_name(bundle2.semantic_models)["ev_f"].measures)["n_median"]
    assert median.agg == "median"
    by_big = dict(db2.execute("select case when n > 3 then 'Yes' else 'No' end, quantile_cont(n, 0.5) from ev_f group by 1").fetchall())
    assert by_big == {r["ev_f.n_big"]: r["ev_f.n_median"] for r in looker("stats_by_big")}
    assert db2.execute("select quantile_cont(n, 0.5) from ev_f").fetchone()[0] == looker("stats_total")[0]["ev_f.n_median"]
    # and what a translation would get wrong: PERCENTILE_CONT is not Looker's p25 / p90
    assert db2.execute("select quantile_cont(n, 0.25), quantile_cont(n, 0.9) from ev_f").fetchone() != (1, 10)
    # on another warehouse the percentile stays translated
    other = _by_name(_by_name(_parse_evidence2(tmp_path, "snowflake").semantic_models)["ev_f"].measures)
    assert other["n_p25"].agg_params == {"percentile": 0.25, "use_discrete_percentile": False}


def test_sums_show_zero_for_an_empty_group(bundle2):
    assert _by_name(bundle2.metrics)["n_sum_big"].fill_nulls_with == 0
    assert {r["ev_f.n_big"]: r["ev_f.n_sum_big"] for r in looker("stats_by_big")}["No"] == 0


# ---------------------------------------------------------------- tile SQL: symmetric aggregates, distinct and window measures (#31)

TILES = {
    "fanout_by_customer": ("ev_orders", ["ev_orders.customer", "ev_orders.count", "ev_orders.total_shipping", "ev_orders.avg_shipping",
                                         "ev_items.count", "ev_items.total_price", "ev_items.shipping_sum_distinct",
                                         "ev_items.shipping_avg_distinct"], ["ev_orders.customer"]),
    "fanout_total": ("ev_orders", ["ev_orders.count", "ev_orders.total_shipping", "ev_orders.avg_shipping", "ev_items.count",
                                   "ev_items.total_price"], []),
    "distinct_total": ("ev_items", ["ev_items.count", "ev_items.total_price", "ev_items.shipping_sum_distinct",
                                    "ev_items.shipping_avg_distinct", "ev_items.shipping_naive_sum"], []),
    "measure_types": ("ev_orders", ["ev_orders.customer", "ev_orders.total_shipping", "ev_orders.shipping_pct",
                                    "ev_orders.shipping_running"], ["ev_orders.customer"]),
}


def evidence_tiles(repo: Path, dialect: str) -> dict:
    """The TILES queries as a LookML dashboard over the evidence views: unshackle's tile per case."""
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "unshackle.toml").write_text(f'[datasources.warehouse]\ntype = "{dialect}"\n')
    store = RawStore(repo, "looker")
    store.put_text("lookml/unshackle_evidence.view.lkml", (FIX / "evidence.view.lkml").read_text())
    store.put_text("lookml/evidence.model.lkml", 'connection: "bq"\ninclude: "/unshackle_evidence.view"\n' + (FIX / "evidence.explores.lkml").read_text())
    elements = "".join(
        f"  - name: {name}\n    model: evidence\n    explore: {explore}\n    type: looker_grid\n    fields: [{', '.join(fields)}]\n"
        + (f"    sorts: [{', '.join(sorts)}]\n" if sorts else "")
        for name, (explore, fields, sorts) in TILES.items()
    )
    store.put_text("lookml/ev.dashboard.lookml", f"- dashboard: ev\n  title: Evidence\n  elements:\n{elements}")
    (d,) = get_source("looker").parse(store).dashboards
    return {t.id: t for t in d.tiles}


def assert_lookers_numbers(case: str, cols: list[str], rows: list[tuple]) -> None:
    """Rows of a tile's SQL against Looker's result for the same query on the same rows."""
    ours = [dict(zip(cols, r)) for r in rows]
    theirs = looker(case)
    key = lambda r: str(r.get("ev_orders_customer", r.get("ev_orders.customer")))  # noqa: E731
    in_order = list(ours)
    if TILES[case][2]:  # NULL sorts first in BigQuery and last in most warehouses: compare by customer
        ours, theirs = sorted(ours, key=key), sorted(theirs, key=key)
    assert len(ours) == len(theirs)
    for o, t in zip(ours, theirs):
        for field, value in t.items():
            col = field.replace(".", "_")
            if col == "ev_orders_shipping_running" or col not in o:
                continue  # a running total follows the warehouse's sort order (checked below); fields the tile doesn't select
            ours_value = float(o[col]) if isinstance(o[col], (int, float, Decimal)) else o[col]
            assert ours_value == (pytest.approx(value) if isinstance(value, (int, float)) else value), (case, field)
    running = 0.0
    for row in in_order if "ev_orders_shipping_running" in cols else []:  # adds up the rows in the order they come back
        running += float(row["ev_orders_total_shipping"])
        assert float(row["ev_orders_shipping_running"]) == pytest.approx(running)


@pytest.fixture(scope="module")
def tiles(tmp_path_factory) -> dict:
    return evidence_tiles(tmp_path_factory.mktemp("tiles"), "duckdb")


@pytest.mark.parametrize("case", sorted(TILES))
def test_tile_sql_returns_lookers_numbers(tiles, db, case):
    tile = tiles[case]
    assert tile.sql, tile.todo
    cur = db.execute(tile.sql)
    assert_lookers_numbers(case, [c[0] for c in cur.description], cur.fetchall())


@pytest.mark.parametrize("dialect", sorted(SYM_HASH))
def test_tile_sql_parses_in_each_dialect(tmp_path, dialect):
    import sqlglot

    for case, tile in evidence_tiles(tmp_path, dialect).items():
        assert tile.sql, (case, tile.todo)
        sqlglot.parse_one(tile.sql, read=dialect)
