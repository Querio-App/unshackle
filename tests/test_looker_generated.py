"""Looker's own generated SQL and results (tests/fixtures/looker_generated/, #41) against unshackle.

The evidence views read literal rows, so DuckDB loads the same rows from the LookML and runs
unshackle's translation; the numbers must be Looker's. What unshackle doesn't translate yet must
stay a TODO, and the fix for it turns that assertion into a numeric one.
"""

import json
import re
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
    # #30: the UNNEST join becomes a view of its own, joined on the parent's key
    lines = _by_name(bundle.queries)["ev_nested_lines derived table"].sql
    assert lines == ("SELECT ev_nested.id AS ev_nested_key, ev_nested_lines.* FROM ev_nested AS ev_nested "
                     "LEFT JOIN UNNEST(ev_nested.lines) AS ev_nested_lines")
    assert "ev_nested_lines" in _by_name(bundle.semantic_models)


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


def _parse_evidence2(repo: Path, warehouse: str, dashboard: str = "") -> Bundle:
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "unshackle.toml").write_text(f'[datasources.warehouse]\ntype = "{warehouse}"\n')
    store = RawStore(repo, "looker")
    if dashboard:
        store.put_text("lookml/ev2.dashboard.lookml", dashboard)
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
        + ("    filters:\n" + "".join(f"      {k}: \"{v}\"\n" for k, v in filters.items()) if filters else "")
        for name, (explore, fields, sorts, filters) in ALL_TILES.items()
    )
    store.put_text("lookml/ev.dashboard.lookml", f"- dashboard: ev\n  title: Evidence\n  elements:\n{elements}")
    (d,) = get_source("looker").parse(store).dashboards
    return {t.id: t for t in d.tiles}


def assert_lookers_numbers(case: str, cols: list[str], rows: list[tuple]) -> None:
    """Rows of a tile's SQL against Looker's result for the same query on the same rows."""
    ours = [dict(zip(cols, r)) for r in rows]
    theirs = looker(case)
    key = lambda r: str(next((v for k, v in r.items() if k.endswith("customer")), ""))  # noqa: E731
    in_order = ours
    if ALL_TILES[case][2]:  # NULL sorts first in BigQuery and last in most warehouses: compare by customer
        ours, theirs = sorted(ours, key=key), sorted(theirs, key=key)
    assert len(ours) == len(theirs)
    for o, t in zip(ours, theirs):
        for field, value in t.items():
            col = field.replace(".", "_")
            if col == "ev_orders_shipping_running" or col not in o:
                continue  # a running total follows the warehouse's sort order (checked below); fields the tile doesn't select
            ours_value = float(o[col]) if isinstance(o[col], (int, float, Decimal)) else o[col]
            assert ours_value == (pytest.approx(value) if isinstance(value, (int, float)) else value), (case, field)
    if "ev_orders_shipping_running" in cols:  # adds up the rows in the order they come back
        running = 0.0
        for row in in_order:
            running += float(row["ev_orders_total_shipping"])
            assert float(row["ev_orders_shipping_running"]) == pytest.approx(running)


# Liquid (#29): the derived table's {% condition %} and the measure's {% parameter %} / _parameter_value,
# rendered for the tile's values (Looker's defaults when the tile sets none)
LIQUID_TILES = {
    "liquid_default": ("ev_liquid", ["ev_liquid.customer", "ev_liquid.count", "ev_liquid.chosen_total", "ev_liquid.chosen_label"],
                       ["ev_liquid.customer"], {}),
    "liquid_set": ("ev_liquid", ["ev_liquid.customer", "ev_liquid.count", "ev_liquid.chosen_total", "ev_liquid.chosen_label"],
                   ["ev_liquid.customer"], {"ev_liquid.customer_filter": "-acme", "ev_liquid.metric_choice": "shipping"}),
    "liquid_user_attribute": ("ev_liquid", ["ev_liquid.customer", "ev_liquid.user_locale", "ev_liquid.count"], [], {}),
}
# nested records (#30): the unnested view joined on the parent's key
NESTED_TILES = {
    "nested_by_id": ("ev_nested", ["ev_nested.id", "ev_nested.count", "ev_nested_lines.count", "ev_nested_lines.total_qty"],
                     ["ev_nested.id"], {}),
    "nested_by_sku": ("ev_nested", ["ev_nested_lines.sku", "ev_nested.count", "ev_nested_lines.total_qty"], ["ev_nested_lines.sku"], {}),
    "nested_total": ("ev_nested", ["ev_nested.count", "ev_nested_lines.count", "ev_nested_lines.total_qty"], [], {}),
}
ALL_TILES = {**{k: (*v, {}) for k, v in TILES.items()}, **LIQUID_TILES, **NESTED_TILES}


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
        if case == "liquid_user_attribute":
            continue  # a TODO by design
        assert tile.sql, (case, tile.todo)
        sqlglot.parse_one(tile.sql, read=dialect)


@pytest.mark.parametrize("case", ["liquid_default", "liquid_set"])
def test_liquid_tiles_return_lookers_numbers(tiles, db, case):
    tile = tiles[case]
    assert tile.sql, tile.todo
    assert "{%" not in tile.sql and "{{" not in tile.sql
    cur = db.execute(tile.sql)
    assert_lookers_numbers(case, [c[0] for c in cur.description], cur.fetchall())


def test_liquid_user_attributes_stay_todo_in_tiles(tiles):
    """Looker renders the viewing user's value; a notebook has no user."""
    tile = tiles["liquid_user_attribute"]
    assert not tile.sql and "_user_attributes" in tile.todo


# ---------------------------------------------------------------- filter expressions (round 2)

RELATIVE = {"d_last_7", "d_this_month", "d_last_month", "d_ago"}  # depend on the day: their SQL is evidence, not their counts


def test_every_filter_returns_lookers_count(bundle2, db2):
    """Each measure filter in evidence2 through the semantic translation, on the rows Looker counted."""
    model = _by_name(bundle2.semantic_models)["ev_f"]
    cols = {d.name: d.expr or d.name for d in model.dimensions}
    cols["at"], cols["on"] = "ts", "d"

    def sql(filter_: str) -> str:
        return re.sub(r"\{\{ (?:Time)?Dimension\('ev_f__(\w+)'(?:, '\w+')?\) \}\}", lambda m: f"({cols[m.group(1)]})", filter_)

    counts = looker("filters")[0]
    metrics = _by_name(bundle2.metrics)
    checked = 0
    for field, expected in counts.items():
        name = field.split(".")[1]
        if name == "count":
            continue
        m = metrics[name]
        if name in RELATIVE:
            assert m.todo and "Looker filter expression" in m.todo, name
            continue
        assert not m.todo, (name, m.todo)
        assert db2.execute(f"select count(*) from ev_f where {sql(m.filter)}").fetchone()[0] == expected, (name, m.filter)
        checked += 1
    assert checked == len(counts) - 1 - len(RELATIVE)


def test_tile_filters_on_a_timeframe_filter_the_time(db2, tmp_path):
    """A tile filtered on at_month: "2026-12" counts the rows whose time is in December, as Looker's t_month."""
    dashboard = """- dashboard: ev2
  title: Ev2
  elements:
  - name: december
    model: m
    explore: ev_f
    type: single_value
    fields: [ev_f.count]
    filters:
      ev_f.at_month: "2026-12"
"""
    (d,) = _parse_evidence2(tmp_path, "duckdb", dashboard).dashboards
    (tile,) = d.tiles
    assert tile.sql, tile.todo
    assert db2.execute(tile.sql).fetchone()[0] == looker("filters")[0]["ev_f.t_month"] == 2


@pytest.mark.parametrize("case", sorted(NESTED_TILES))
def test_nested_tiles_return_lookers_numbers(tiles, case):
    """The tiles read the parent and the materialized unnested view (here: Looker's rows, with the one
    NULL row LEFT JOIN UNNEST gives an empty array); the child view's own SQL runs on BigQuery (checks)."""
    con = duckdb.connect()
    con.execute("create table ev_nested as select * from (values (1), (2), (3)) t(id)")
    con.execute("create table ev_nested_lines as select * from (values (1, 'x', 2), (1, 'y', 3), (2, null, null), (3, 'x', 1))"
                " t(ev_nested_key, sku, qty)")
    tile = tiles[case]
    assert tile.sql, tile.todo
    assert "SUM(DISTINCT" not in tile.sql.split("ev_nested_lines.qty")[0].rsplit("\n", 1)[-1]  # the lines' own sum stays a sum
    cur = con.execute(tile.sql)
    rows = [dict(zip([c[0] for c in cur.description], r)) for r in cur.fetchall()]
    theirs = [{k.replace(".", "_"): v for k, v in r.items()} for r in looker(case)]
    if sorts := NESTED_TILES[case][2]:
        key = sorts[0].replace(".", "_")
        rows, theirs = sorted(rows, key=lambda r: str(r[key])), sorted(theirs, key=lambda r: str(r[key]))
    assert len(rows) == len(theirs)
    for o, t in zip(rows, theirs):
        for col, value in t.items():
            assert o[col] == value, (case, col)


# ---------------------------------------------------------------- unshackle's BigQuery SQL, run on Looker's BigQuery (round 3)

CHECK_TILES = [*TILES, "liquid_default", "liquid_set", *NESTED_TILES]


def _check_rows(name: str) -> list[dict]:
    path = FIX / f"check_{name}.json"
    if not path.exists():
        pytest.skip(f"check_{name}.json not captured yet (tests/live/looker/bigquery_checks.py)")
    rows = json.loads(path.read_text())
    if rows and "looker_error" in rows[0]:  # BigQuery rejected the SQL
        pytest.fail(f"check_{name}: {rows[0]['looker_error']}")

    def value(v):  # the check views' dimensions are strings
        return float(v) if isinstance(v, str) and re.fullmatch(r"-?\d+(\.\d+)?", v) else v

    return [{k.split(".", 1)[1]: value(v) for k, v in r.items()} for r in rows]


@pytest.mark.parametrize("case", CHECK_TILES)
def test_bigquery_tile_sql_returns_lookers_numbers(case):
    rows = _check_rows(f"tile_{case}")
    cols = list(rows[0]) if rows else []
    assert_lookers_numbers(case, cols, [tuple(r[c] for c in cols) for r in rows])


@pytest.mark.parametrize("week_start,case", [("monday", "time"), ("sunday", "time_week_sunday")])
def test_bigquery_timeframes_return_lookers_values(week_start, case):
    ours = {r["id"]: r for r in _check_rows(f"time_{week_start}")}
    for r in looker(case):
        for field, value in r.items():
            col = field.split(".")[1]
            if col in ours[r["ev_time.id"]]:
                got = ours[r["ev_time.id"]][col]
                assert (got == value) if isinstance(value, (int, float)) else (str(got) == str(value)), (week_start, r["ev_time.id"], col)


def test_bigquery_filters_return_lookers_counts():
    (ours,) = _check_rows("filters")
    for field, value in looker("filters")[0].items():
        if field.split(".")[1] in ours:
            assert int(ours[field.split(".")[1]]) == value, field
