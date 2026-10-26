"""Looker's documented semantics, checked on unshackle's translation of hand-written LookML.

The LookML is in tests/fixtures/looker_semantics/. Its views read the literal rows below (the
`example` schema) or the fictional Dunder Mifflin tables; DuckDB runs the SQL unshackle writes, and
every expected value is computed here in Python from the same rows and Looker's documented rules.
BigQuery SQL runs in DuckDB through `bigquery_in_duckdb`. What unshackle doesn't translate must stay
a TODO.
"""

import calendar
import re
import statistics
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from math import floor
from pathlib import Path

import duckdb
import lkml
import pytest
import sqlglot
from dateutil.relativedelta import relativedelta
from sqlglot import exp
from test_looker import _by_name

from unshackle.ir import Bundle
from unshackle.raw import RawStore
from unshackle.sources import get_source
from unshackle.sources.looker import (DIALECT_DATES, SYM_HASH, TF_GRAIN, TIMEFRAME_SQL, WEEKDAYS, looker_filter_sql,
                                      relative_bounds, timeframe_sql)
from unshackle.target.ossie import AGGS

FIX = Path(__file__).parent / "fixtures" / "looker_semantics"
DATA = Path(__file__).parent / "data" / "dunder_mifflin"

# ---------------------------------------------------------------- the rows the views read

TS, D = datetime, date
TABLES: dict[str, tuple[str, list[tuple]]] = {
    "events": ("id INTEGER, ts TIMESTAMP, d DATE", [
        (1, TS(2027, 12, 31, 18, 45, 10), D(2027, 12, 31)),  # a Friday in ISO week 52
        (2, TS(2028, 1, 2, 7, 0), D(2028, 1, 2)),  # a Sunday, still ISO week 52 of 2027
        (3, TS(2028, 1, 3, 0, 5), D(2028, 1, 3)),  # a Monday, ISO week 1
        (4, TS(2028, 2, 29, 12, 0), D(2028, 2, 29)),  # leap day
        (5, TS(2028, 7, 1, 0, 0), D(2028, 7, 1)),  # first day of a quarter, a Saturday
        (6, TS(2026, 1, 1, 9, 30), D(2026, 1, 1)),  # a Thursday in ISO week 1 of 2026
        (7, None, None),
    ]),
    "shop_orders": ("id INTEGER, customer VARCHAR, shipping DOUBLE, ordered DATE", [
        (1, "scranton", 12.5, D(2027, 3, 4)),
        (2, "scranton", 7.5, D(2027, 3, 18)),
        (3, "nashua", 4.0, D(2027, 4, 2)),
        (4, "stamford", 9.0, D(2027, 4, 9)),  # no items
        (5, "utica", 6.0, D(2027, 5, 1)),  # no items
        (6, None, 4.0, D(2027, 5, 5)),  # same shipping as order 3
    ]),
    "shop_items": ("item_id INTEGER, order_id INTEGER, price DOUBLE, order_shipping DOUBLE", [
        (101, 1, 3.0, 12.5), (102, 1, 2.0, 12.5), (103, 2, 5.0, 7.5),
        (104, 3, 1.5, 4.0), (105, 3, 1.5, 4.0), (106, 3, 2.0, 4.0), (107, 6, 4.0, 4.0),
    ]),
    "pick_rows": ("customer VARCHAR, shipping DOUBLE, amount DOUBLE", [
        ("scranton", 4.0, 40.0), ("scranton", 6.0, 25.0), ("nashua", 3.0, 90.0), ("utica", 1.0, 15.0), (None, 2.0, 10.0),
    ]),
    "facts": ("id INTEGER, s VARCHAR, n DOUBLE, d DATE, ts TIMESTAMP, y BOOLEAN", [
        (1, "paper", 2.0, D(2027, 6, 30), TS(2027, 6, 30, 22, 15), True),
        (2, "Paper", 4.0, D(2027, 7, 1), TS(2027, 7, 1, 6, 0), False),
        (3, "stapler", 6.0, D(2027, 1, 15), TS(2027, 1, 15, 12, 0), None),
        (4, "", -2.0, D(2027, 2, 1), TS(2027, 2, 1, 0, 0), True),
        (5, "ink,toner", 10.0, D(2027, 7, 31), TS(2027, 7, 31, 23, 59, 59), False),
        (6, "sandpaper", 8.0, D(2028, 1, 1), TS(2028, 1, 1, 0, 30), False),
        (7, None, None, None, None, None),
    ]),
    "quotes": ("id INTEGER, s VARCHAR, n INTEGER, ts TIMESTAMP", [
        (1, "paper", 1, TS(2027, 9, 30, 21, 40, 15)),
        (2, "Michael's", 3, TS(2027, 9, 1, 7, 5)),
        (3, "D'Angelo", 8, TS(2027, 10, 2, 11, 0)),
        (4, None, None, None),
    ]),
    "billing_rows": ("id INTEGER, account VARCHAR, amount DOUBLE, export_time TIMESTAMP", [
        (1, "scranton", 120.0, TS(2027, 9, 1, 2, 0)),
        (2, "scranton", 80.0, TS(2027, 9, 2, 2, 0)),
        (3, "nashua", 45.5, TS(2027, 9, 2, 3, 0)),
        (4, "utica", 30.0, TS(2027, 9, 3, 2, 0)),
        (5, None, 10.0, TS(2027, 9, 3, 4, 0)),
    ]),
}
BASKETS = [(1, [("a4", 2), ("legal", 1)]), (2, []), (3, [("a4", 5)]), (4, [("legal", 3), ("a4", 1), ("ledger", 2)])]


def rows(table: str) -> list[dict]:
    cols = [c.split()[0] for c in TABLES[table][0].split(", ")]
    return [dict(zip(cols, r)) for r in TABLES[table][1]]


def basket_lines() -> list[tuple]:
    """`LEFT JOIN UNNEST(lines)`: a row per element, and one row of NULLs for an empty array."""
    return [(i, sku, qty) for i, lines in BASKETS for sku, qty in (lines or [(None, None)])]


@pytest.fixture(scope="module")
def db() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    con.execute("create schema example")
    for name, (cols, data) in TABLES.items():
        con.execute(f"create table example.{name} ({cols})")
        con.executemany(f"insert into example.{name} values ({', '.join('?' * len(data[0]))})", data)
    con.execute("create table example.baskets (id INTEGER, lines STRUCT(sku VARCHAR, qty INTEGER)[])")
    con.executemany("insert into example.baskets values (?, ?)", [(i, [{"sku": s, "qty": q} for s, q in lines]) for i, lines in BASKETS])
    con.execute("create table basket_lines (baskets_key INTEGER, sku VARCHAR, qty INTEGER)")  # the unnested view, materialized
    con.executemany("insert into basket_lines values (?, ?, ?)", basket_lines())
    con.execute("create schema dunder_mifflin")
    for table in ("accounts", "orders"):
        con.execute(f"create view dunder_mifflin.{table} as select * from read_parquet('{DATA / table}.parquet')")
    return con


def fetch(con: duckdb.DuckDBPyConnection, sql: str) -> list[dict]:
    cur = con.execute(sql)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def bigquery_in_duckdb(sql: str) -> str:
    """BigQuery SQL for DuckDB: sqlglot's transpilation, plus documented BigQuery rules it doesn't carry
    over: EXTRACT(DAYOFWEEK) numbers Sunday 1 to Saturday 7 (DuckDB's DAYOFWEEK: Sunday 0),
    EXTRACT(ISOWEEK) is the ISO 8601 week, and NUMERIC is a DECIMAL(38, 9)."""

    def fix(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Extract) and node.this.name.upper() == "DAYOFWEEK":
            return exp.Paren(this=exp.Add(this=exp.Anonymous(this="DAYOFWEEK", expressions=[node.expression]),
                                          expression=exp.Literal.number(1)))
        if isinstance(node, exp.Extract) and node.this.name.upper() == "ISOWEEK":
            return exp.Anonymous(this="WEEKOFYEAR", expressions=[node.expression])
        if isinstance(node, exp.DataType) and node.this == exp.DataType.Type.DECIMAL and not node.expressions:
            return exp.DataType.build("DECIMAL(38, 9)")
        return node

    return sqlglot.parse_one(sql, read="bigquery").transform(fix).sql("duckdb")


def run(con: duckdb.DuckDBPyConnection, sql: str, dialect: str) -> list[dict]:
    return fetch(con, bigquery_in_duckdb(sql) if dialect == "bigquery" else sql)


# ---------------------------------------------------------------- parsing the fixtures

def parse(repo: Path, dialect: str, week_start: str = "") -> Bundle:
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "unshackle.toml").write_text(f'[datasources.warehouse]\ntype = "{dialect}"\n')
    store = RawStore(repo, "looker")
    for f in sorted(FIX.iterdir()):
        if f.suffix in (".lkml", ".lookml"):
            text = f.read_text()
            if week_start and f.name.endswith(".model.lkml"):
                text = f"week_start_day: {week_start}\n{text}"
            store.put_text(f"lookml/{f.name}", text)
    return get_source("looker").parse(store)


@pytest.fixture(scope="module")
def bundle_for(tmp_path_factory):
    """The fixtures parsed for a warehouse (and a week_start_day in the model), once per module."""
    bundles: dict[tuple[str, str], Bundle] = {}

    def get(dialect: str, week_start: str = "") -> Bundle:
        if (dialect, week_start) not in bundles:
            bundles[dialect, week_start] = parse(tmp_path_factory.mktemp(f"{dialect}{week_start}"), dialect, week_start)
        return bundles[dialect, week_start]

    return get


def tiles_of(b: Bundle) -> dict:
    (d,) = b.dashboards
    return {t.id: t for t in d.tiles}


@pytest.fixture(scope="module")
def tiles(bundle_for):
    return tiles_of(bundle_for("duckdb"))


def test_fixtures_hold_only_example_names():
    for f in FIX.iterdir():
        text = f.read_text()
        assert "@" not in text and "http" not in text, f.name
        assert set(re.findall(r"sql_table_name: (\w+)\.", text)) <= {"example", "dunder_mifflin"}, f.name


# ---------------------------------------------------------------- timeframes

WEEKDAY = {name.lower(): i for i, name in enumerate(calendar.day_name)}  # Monday 0, as Python's weekday()


def looker_timeframe(tf: str, v: datetime | date | None, week_start: str = "monday"):
    """Looker's documented value of a timeframe: week is the day the week starts ('YYYY-MM-DD'),
    month 'YYYY-MM', quarter the 'YYYY-MM' of its first month, day_of_week_index counts from
    week_start_day, week_of_year is the ISO week, hour / minute / time_of_day are formatted strings."""
    if v is None:
        return None
    day = v.date() if isinstance(v, datetime) else v
    index = (day.weekday() - WEEKDAY[week_start]) % 7
    return {
        "date": day.isoformat(),
        "week": (day - timedelta(days=index)).isoformat(),
        "month": f"{day:%Y-%m}",
        "quarter": f"{day.year}-{(day.month - 1) // 3 * 3 + 1:02d}",
        "year": day.year,
        "day_of_week": f"{day:%A}",
        "day_of_week_index": index,
        "month_num": day.month,
        "month_name": f"{day:%B}",
        "quarter_of_year": f"Q{(day.month - 1) // 3 + 1}",
        "week_of_year": day.isocalendar()[1],
        "day_of_month": day.day,
        "day_of_year": day.timetuple().tm_yday,
        "hour_of_day": getattr(v, "hour", None),
        "hour": f"{v:%Y-%m-%d %H}" if isinstance(v, datetime) else None,
        "minute": f"{v:%Y-%m-%d %H:%M}" if isinstance(v, datetime) else None,
        "time_of_day": f"{v:%H:%M}" if isinstance(v, datetime) else None,
        "time": f"{v:%Y-%m-%d %H:%M:%S}" if isinstance(v, datetime) else None,
    }[tf]


def norm(v):
    if v is None:
        return None
    if isinstance(v, (float, Decimal)) and float(v).is_integer():
        return str(int(v))
    return str(v)


def assert_timeframes(got: list[dict], source: list[dict], key: str, columns: dict[str, tuple[str, str]], week_start: str):
    """Each column (view_group_tf -> (row field, timeframe)) against Looker's value for the row."""
    columns = dict(columns)
    got_key = columns.pop("_key")
    by_key = {r[key]: r for r in source}
    assert sorted(r[got_key] for r in got) == sorted(by_key)
    for r in got:
        src = by_key[r[got_key]]
        for col, (field, tf) in columns.items():
            assert norm(r[col]) == norm(looker_timeframe(tf, src[field], week_start)), (col, src)


EVENT_TIMEFRAMES = {
    **{f"events_created_{tf}": ("ts", tf) for tf in (
        "date", "week", "month", "quarter", "year", "day_of_week", "day_of_week_index", "month_num", "month_name",
        "quarter_of_year", "week_of_year", "day_of_month", "day_of_year", "hour_of_day", "hour", "minute", "time_of_day")},
    **{f"events_day_{tf}": ("d", tf) for tf in (
        "date", "week", "month", "quarter", "year", "day_of_week", "day_of_week_index", "week_of_year")},
}


@pytest.mark.parametrize("dialect", ["duckdb", "bigquery"])
@pytest.mark.parametrize("week_start", ["", "sunday"])
def test_timeframes_follow_lookers_rules(bundle_for, db, dialect, week_start):
    """Every timeframe of a timestamp and of a date, with weeks starting Monday (Looker's default) and
    with week_start_day: sunday in the model."""
    tile = tiles_of(bundle_for(dialect, week_start))["event_timeframes"]
    assert tile.sql and not tile.todo, tile.todo
    if dialect == "bigquery":
        assert f"WEEK({(week_start or 'monday').upper()})" in tile.sql
    got = run(db, tile.sql, dialect)
    assert_timeframes(got, rows("events"), "id", {"_key": "events_id", **EVENT_TIMEFRAMES}, week_start or "monday")


@pytest.mark.parametrize("dialect", ["duckdb", "bigquery"])
def test_timeframes_over_three_years_of_orders(bundle_for, db, dialect):
    tile = tiles_of(bundle_for(dialect))["order_timeframes"]
    assert tile.sql and not tile.todo, tile.todo
    got = run(db, tile.sql, dialect)
    source = fetch(db, "select order_id, purchased_at from dunder_mifflin.orders")
    assert len(got) == len(source) > 3000
    tfs = ("date", "week", "month", "quarter", "year", "day_of_week", "day_of_week_index", "month_num", "month_name",
           "quarter_of_year", "week_of_year", "day_of_month", "day_of_year")
    assert_timeframes(got, source, "order_id",
                      {"_key": "dm_orders_order_id", **{f"dm_orders_purchased_{tf}": ("purchased_at", tf) for tf in tfs}}, "monday")


def test_hour_timeframes(bundle_for, db):
    """hour 'YYYY-MM-DD HH', minute 'YYYY-MM-DD HH:MM', time_of_day 'HH:MM'; a measure filtered on a time
    range counts [start, end)."""
    for dialect in ("duckdb", "bigquery"):
        got = run(db, tiles_of(bundle_for(dialect))["hour_timeframes"].sql, dialect)
        cols = {"_key": "quotes_id", **{f"quotes_at_{tf}": ("ts", tf) for tf in ("time", "hour", "minute", "time_of_day", "hour_of_day")}}
        assert_timeframes([{k: v for k, v in r.items() if k != "quotes_in_range"} for r in got], rows("quotes"), "id", cols, "monday")
        in_range = {r["id"]: int(r["ts"] is not None and TS(2027, 9, 1) <= r["ts"] < TS(2027, 10, 1)) for r in rows("quotes")}
        assert {r["quotes_id"]: r["quotes_in_range"] for r in got} == in_range, dialect


def test_metricflow_time_grains_match_lookers_periods(bundle_for, db):
    """MetricFlow truncates to the grain; truncated, a value starts the same day, Monday week and
    quarter as Looker's period."""
    dims = _by_name(_by_name(bundle_for("duckdb").semantic_models)["events"].dimensions)
    assert (dims["created"].type, dims["created"].expr) == ("time", "ts")
    fmt = {"date": "%Y-%m-%d", "week": "%Y-%m-%d", "month": "%Y-%m", "quarter": "%Y-%m", "year": "%Y"}
    for tf, f in fmt.items():
        ours = dict(db.execute(f"select id, strftime(date_trunc('{TF_GRAIN[tf]}', ts), '{f}') from example.events").fetchall())
        assert ours == {r["id"]: norm(looker_timeframe(tf, r["ts"])) for r in rows("events")}, tf


TIMEFRAMES = ["date", "week", "month", "quarter", "year", "day_of_week", "day_of_week_index", "month_num", "month_name",
              "quarter_of_year", "week_of_year", "day_of_month", "day_of_year", "hour_of_day", "hour", "minute", "time_of_day"]


@pytest.mark.parametrize("dialect", sorted(DIALECT_DATES))
def test_timeframe_sql_parses_in_each_dialect(dialect):
    for week_start in ("monday", "sunday", "saturday"):
        for tf in TIMEFRAMES:
            sql = TIMEFRAME_SQL[tf].format("t.created_at") if tf in TIMEFRAME_SQL else timeframe_sql(tf, "t.created_at", dialect, week_start)
            assert sql, (dialect, tf)
            sqlglot.parse_one(f"SELECT {sql} FROM t", read=dialect)


# ---------------------------------------------------------------- measures, symmetric aggregates, sum_distinct

def test_plain_measures(bundle_for, db):
    measures = _by_name(_by_name(bundle_for("duckdb").semantic_models)["shop_orders"].measures)
    cols = {name: AGGS[m.agg].format(m.expr) for name, m in measures.items()}
    got = fetch(db, f"select customer, {', '.join(f'{c} as {n}' for n, c in cols.items())} from example.shop_orders group by 1")
    assert len(got) == len({r["customer"] for r in rows("shop_orders")})
    for r in got:
        mine = [o for o in rows("shop_orders") if o["customer"] == r["customer"]]
        assert r["shop_orders_count"] == len(mine)
        assert r["total_shipping"] == pytest.approx(sum(o["shipping"] for o in mine))
        assert r["avg_shipping"] == pytest.approx(statistics.mean(o["shipping"] for o in mine))


def _group_expected(orders: list[dict], items: list[dict]) -> dict:
    """Looker's numbers for one group of a one-to-many join: measures of the one side count each order once
    (symmetric aggregates), sum_distinct adds each order's value once (sql_distinct_key)."""
    ids = {o["id"] for o in orders}
    mine = [i for i in items if i["order_id"] in ids]
    shipping = [o["shipping"] for o in orders if o["shipping"] is not None]
    per_order = {i["order_id"]: i["order_shipping"] for i in mine if i["order_shipping"] is not None}
    return {
        "shop_orders_count": len(orders),
        "shop_orders_total_shipping": sum(shipping),
        "shop_orders_avg_shipping": statistics.mean(shipping) if shipping else None,
        "shop_items_count": len(mine),
        "shop_items_total_price": sum(i["price"] for i in mine),
        "shop_items_shipping_sum_distinct": sum(per_order.values()),
        "shop_items_shipping_avg_distinct": statistics.mean(per_order.values()) if per_order else None,
        "shop_items_shipping_naive_sum": sum(i["order_shipping"] for i in mine),
    }


def assert_row(got: dict, expected: dict, context) -> None:
    for col, value in got.items():
        if col in expected:
            want = expected[col]
            if want is None or isinstance(want, str):
                assert value == want, (context, col)
            else:
                assert float(value) == pytest.approx(want), (context, col)


@pytest.mark.parametrize("dialect", ["duckdb", "bigquery"])
def test_one_to_many_join_counts_each_order_once(bundle_for, db, dialect):
    t = tiles_of(bundle_for(dialect))
    orders, items = rows("shop_orders"), rows("shop_items")
    for r in run(db, t["fanout_by_customer"].sql, dialect):
        group = [o for o in orders if o["customer"] == r["shop_orders_customer"]]
        assert_row(r, _group_expected(group, items), r["shop_orders_customer"])
    (total,) = run(db, t["fanout_total"].sql, dialect)
    assert_row(total, _group_expected(orders, items), "total")
    # what a plain SUM over the join would show instead
    joined = sum(o["shipping"] * max(1, sum(i["order_id"] == o["id"] for i in items)) for o in orders)
    assert total["shop_orders_total_shipping"] != pytest.approx(joined)


@pytest.mark.parametrize("dialect", ["duckdb", "bigquery"])
def test_sum_distinct_adds_each_key_once(bundle_for, db, dialect):
    (got,) = run(db, tiles_of(bundle_for(dialect))["distinct_total"].sql, dialect)
    items = rows("shop_items")
    expected = _group_expected(rows("shop_orders"), items)
    assert_row(got, expected, "distinct_total")
    # distinct by key, not by value: orders 3 and 6 ship for the same amount
    assert expected["shop_items_shipping_sum_distinct"] != sum({i["order_shipping"] for i in items})


@pytest.mark.parametrize("dialect", ["duckdb", "bigquery"])
def test_one_to_many_join_on_dunder_mifflin(bundle_for, db, dialect):
    """Accounts joined to their orders: an account's size counts once per account manager, however many
    orders it has."""
    got = run(db, tiles_of(bundle_for(dialect))["fanout_by_manager"].sql, dialect)
    accounts = fetch(db, "select account_id, account_manager, business_size from dunder_mifflin.accounts")
    orders = fetch(db, "select order_id, account_id, post_discount_revenue from dunder_mifflin.orders")
    assert len(got) == len({a["account_manager"] for a in accounts})
    for r in got:
        mine = [a for a in accounts if a["account_manager"] == r["dm_accounts_account_manager"]]
        ids = {a["account_id"] for a in mine}
        sizes = [a["business_size"] for a in mine if a["business_size"] is not None]
        theirs = [o for o in orders if o["account_id"] in ids]
        assert_row(r, {
            "dm_accounts_count": len(mine),
            "dm_accounts_total_business_size": sum(sizes),
            "dm_accounts_avg_business_size": statistics.mean(sizes) if sizes else None,
            "dm_orders_count": len(theirs),
            "dm_orders_total_revenue": float(sum(o["post_discount_revenue"] or 0 for o in theirs)),
        }, r["dm_accounts_account_manager"])


def test_percent_of_total_and_running_total(tiles, db):
    """percent_of_total is 100 * the row's share of the column total; running_total adds the rows in the
    order they are shown."""
    got = fetch(db, tiles["measure_types"].sql)
    orders = rows("shop_orders")
    grand = sum(o["shipping"] for o in orders)
    running = 0.0
    for r in got:
        total = sum(o["shipping"] for o in orders if o["customer"] == r["shop_orders_customer"])
        running += total
        assert float(r["shop_orders_total_shipping"]) == pytest.approx(total)
        assert float(r["shop_orders_shipping_pct"]) == pytest.approx(100 * total / grand)
        assert float(r["shop_orders_shipping_running"]) == pytest.approx(running)
    assert running == pytest.approx(grand)


def test_untranslated_stay_todo(bundle_for):
    b = bundle_for("duckdb")
    metrics = _by_name(b.metrics)
    for name in ("shipping_pct", "shipping_running", "order_ids", "shipping_sum_distinct", "shipping_avg_distinct"):
        assert metrics[name].todo, name
    for name in ("chosen_total", "chosen_label"):
        assert "Liquid" in metrics[name].todo, name
    assert "Liquid" in _by_name(_by_name(b.semantic_models)["pick"].dimensions)["user_locale"].todo
    # the UNNEST join becomes a view of its own, joined on the parent's key
    lines = _by_name(b.queries)["basket_lines derived table"].sql
    assert lines == ("SELECT baskets.id AS baskets_key, basket_lines.* FROM example.baskets AS baskets "
                     "LEFT JOIN UNNEST(baskets.lines) AS basket_lines")
    assert "basket_lines" in _by_name(b.semantic_models)


def test_sort_on_a_measure_the_tile_does_not_show(tiles, db):
    """Looker adds a sorted measure to the query and orders by it; a sorted dimension that isn't selected
    it drops."""
    tile = tiles["top_by_hidden_measure"]
    assert "shop_orders_total_shipping" in tile.sql and "ordered_month" not in tile.sql, tile.sql
    assert "sort" not in tile.todo
    (row,) = fetch(db, tile.sql)
    totals = {}
    for o in rows("shop_orders"):
        totals.setdefault(o["customer"], []).append(o["shipping"])
    top = max(totals, key=lambda c: sum(totals[c]))
    assert (row["shop_orders_customer"], float(row["shop_orders_total_shipping"]), float(row["shop_orders_avg_shipping"])) == (
        top, sum(totals[top]), statistics.mean(totals[top]))


@pytest.mark.parametrize("dialect", sorted(SYM_HASH))
def test_tile_sql_parses_in_each_dialect(bundle_for, dialect):
    without_sql = {"liquid_user_attribute"} | ({"dialect", "date_start_relative", *RELATIVE_TILES} if dialect != "bigquery" else set())
    for name, tile in tiles_of(bundle_for(dialect)).items():
        if name in without_sql:
            assert not tile.sql, (dialect, name)
            continue
        assert tile.sql, (dialect, name, tile.todo)
        sqlglot.parse_one(tile.sql, read=dialect)


# ---------------------------------------------------------------- Liquid

def _pick_expected(exclude: str | None, column: str) -> dict:
    out: dict = {}
    for r in rows("pick_rows"):
        if exclude is not None and r["customer"] == exclude:  # "-x" keeps NULL
            continue
        n, total = out.get(r["customer"], (0, 0.0))
        out[r["customer"]] = (n + 1, total + r[column])
    return out


@pytest.mark.parametrize("dialect", ["duckdb", "bigquery"])
@pytest.mark.parametrize("case,exclude,column", [("liquid_default", None, "amount"), ("liquid_set", "nashua", "shipping")])
def test_liquid_renders_for_the_tiles_values(bundle_for, db, dialect, case, exclude, column):
    """{% condition %} applies the tile's filter in the derived table (1=1 without one); {% parameter %} and
    _parameter_value take the tile's value, else the parameter's default_value."""
    tile = tiles_of(bundle_for(dialect))[case]
    assert tile.sql and not tile.todo, tile.todo
    assert "{%" not in tile.sql and "{{" not in tile.sql
    got = run(db, tile.sql, dialect)
    expected = _pick_expected(exclude, column)
    assert sorted(map(str, (r["pick_customer"] for r in got))) == sorted(map(str, expected))
    for r in got:
        count, total = expected[r["pick_customer"]]
        assert (r["pick_count"], float(r["pick_chosen_total"]), r["pick_chosen_label"]) == (count, total, column)


def test_liquid_user_attributes_stay_todo_in_tiles(tiles):
    """Looker renders the viewing user's value; a notebook has no user."""
    tile = tiles["liquid_user_attribute"]
    assert not tile.sql and "_user_attributes" in tile.todo


def _bq_timestamp_string(t: datetime) -> str:
    return f"{t:%Y-%m-%d %H:%M:%S}+00"  # BigQuery's CAST(TIMESTAMP AS STRING), in UTC


def _today() -> date:
    return datetime.now(timezone.utc).date()


QUOTES_LIQUID = {
    # case: {column: value on every row}
    "dialect": {"quotes_dialect_name": "bigquery_standard_sql", "quotes_dialect_branch": "other"},
    "liquid_none": {"quotes_range_start": None, "quotes_range_end": None, "quotes_flag_filtered": "no",
                    "quotes_flag_in_query": "no", "quotes_flag_selected": "no", "quotes_p_str_param": "plain",
                    "quotes_p_str_branch": "quoted", "quotes_p_num_param": 3},
    "liquid_values": {"quotes_range_start": _bq_timestamp_string(TS(2027, 9, 1)),
                      "quotes_range_end": _bq_timestamp_string(TS(2027, 10, 1)), "quotes_flag_filtered": "yes",
                      "quotes_flag_in_query": "yes", "quotes_flag_selected": "yes", "quotes_p_str_param": "D'Angelo",
                      "quotes_p_str_branch": "neither", "quotes_p_num_param": 8},
    "date_start_month": {"quotes_range_start": _bq_timestamp_string(TS(2027, 9, 1)),
                         "quotes_range_end": _bq_timestamp_string(TS(2027, 10, 1))},
}


@pytest.mark.parametrize("case", [*QUOTES_LIQUID, "date_start_relative"])
def test_liquid_projects_use(bundle_for, db, case):
    """On BigQuery: _dialect._name is 'bigquery_standard_sql'; date_start / date_end are the bounds of the
    date filter (NULL without one; `7 days` is today and the 6 days before, in UTC); _is_filtered,
    _in_query and _is_selected follow the query; a string parameter is a quoted literal, so its
    _parameter_value compares equal to "'plain'"; a number parameter is the number."""
    tile = tiles_of(bundle_for("bigquery"))[case]
    assert tile.sql and not tile.todo, tile.todo
    got = run(db, tile.sql, "bigquery")
    assert [r["quotes_id"] for r in got] == sorted(r["id"] for r in rows("quotes"))
    if case == "date_start_relative":
        start = datetime.combine(_today() - timedelta(days=6), datetime.min.time())
        expected = {"quotes_range_start": _bq_timestamp_string(start),
                    "quotes_range_end": _bq_timestamp_string(start + timedelta(days=7))}
    else:
        expected = QUOTES_LIQUID[case]
    if case == "liquid_values":  # the tile's filter on s: -nobody keeps every row
        assert [r["quotes_s"] for r in got] == [r["s"] for r in rows("quotes")]
    for r in got:
        assert {k: r[k] for k in expected} == expected, (case, r["quotes_id"])


@pytest.mark.parametrize("dialect", ["duckdb", "bigquery"])
@pytest.mark.parametrize("case,keep", [
    ("filter_field_sql", lambda s: s != "paper"),  # "-paper" keeps NULL
    ("filter_quoted", lambda s: s == "Michael's"),  # a quoted value is the text itself
    ("filter_apostrophe", lambda s: s in ("Michael's", "D'Angelo")),
])
def test_filters_with_quotes_and_filter_fields(bundle_for, db, dialect, case, keep):
    """A filter field's own sql goes in the WHERE clause; an apostrophe is escaped the warehouse's way
    (a backslash on BigQuery, where '' is not a quote)."""
    sql = tiles_of(bundle_for(dialect))[case].sql
    if case != "filter_field_sql":
        assert ("Michael\\'s" if dialect == "bigquery" else "Michael''s") in sql
    (got,) = run(db, sql, dialect)
    assert got["quotes_count"] == sum(keep(r["s"]) for r in rows("quotes"))


# ---------------------------------------------------------------- persistent derived tables

@pytest.mark.parametrize("dialect", ["duckdb", "bigquery"])
def test_incremental_pdt(bundle_for, db, dialect):
    """Looker queries a persistent derived table under its materialized name; its SQL, and the
    {% incrementcondition %} in it, only run when the table is rebuilt. The derived-table query is
    the full rebuild (the condition is TRUE), with a TODO about loading on increment_key."""
    b = bundle_for(dialect)
    q = next(x for x in b.queries if x.name == "billing derived table")
    assert "{%" not in q.sql and "TRUE" in q.sql
    assert "increment_key `export_date`" in q.todo and "increment_offset" in q.todo and "Liquid" not in q.todo
    tile = tiles_of(b)["pdt_incremental"]
    assert tile.sql and "{%" not in tile.sql and "example.billing_rows" not in tile.sql, tile.todo
    assert "materialize it as `billing`" in tile.todo
    db.execute(f"create or replace table billing as {q.sql}")  # the PDT, materialized from its query
    got = run(db, tile.sql, dialect)
    expected: dict = {}
    for r in rows("billing_rows"):
        if r["account"] == "utica":  # "-utica" keeps NULL
            continue
        n, total = expected.get(r["account"], (0, 0.0))
        expected[r["account"]] = (n + 1, total + r["amount"])
    assert {r["billing_account"]: (r["billing_count"], float(r["billing_total_amount"])) for r in got} == expected


# ---------------------------------------------------------------- nested records

@pytest.mark.parametrize("case", ["nested_by_id", "nested_by_sku", "nested_total"])
def test_nested_records(tiles, db, case):
    """The tiles read the parent and the unnested view joined on the parent's key. A count of the
    unnested view counts its rows (an empty array gives one row of NULLs)."""
    tile = tiles[case]
    assert tile.sql, tile.todo
    assert "SUM(DISTINCT" not in tile.sql.split("basket_lines.qty")[0].rsplit("\n", 1)[-1]  # the lines' own sum stays a sum
    got = fetch(db, tile.sql)
    lines = basket_lines()
    if case == "nested_total":
        expected = [{"baskets_count": len(BASKETS), "basket_lines_count": len(lines),
                     "basket_lines_total_qty": sum(q or 0 for _, _, q in lines)}]
    elif case == "nested_by_id":
        expected = [{"baskets_id": i, "baskets_count": 1, "basket_lines_count": sum(k == i for k, _, _ in lines),
                     "basket_lines_total_qty": sum(q or 0 for k, _, q in lines if k == i)} for i, _ in BASKETS]
    else:
        skus = {s for _, s, _ in lines}
        expected = [{"basket_lines_sku": s, "baskets_count": len({k for k, x, _ in lines if x == s}),
                     "basket_lines_total_qty": sum(q or 0 for _, x, q in lines if x == s)} for s in skus]
    key = lambda r: str(next(iter(r.values())))  # noqa: E731
    assert sorted(got, key=key) == sorted(expected, key=key)


# ---------------------------------------------------------------- filter expressions

FILTERS = {
    # strings: % is a wildcard, -x excludes x and keeps NULL, ^, is a comma, EMPTY is NULL or ''
    "s_starts": lambda r: r["s"] is not None and r["s"].startswith("pap"),
    "s_ends": lambda r: r["s"] is not None and r["s"].endswith("per"),
    "s_contains": lambda r: r["s"] is not None and "ap" in r["s"],
    "s_not_starts": lambda r: r["s"] is None or not r["s"].startswith("pap"),
    "s_eq_lower": lambda r: r["s"] == "paper",
    "s_eq_upper": lambda r: r["s"] == "PAPER",
    "s_list": lambda r: r["s"] in ("paper", "stapler"),
    "s_not_list": lambda r: r["s"] not in ("paper", "stapler"),
    "s_escaped_comma": lambda r: r["s"] == "ink,toner",
    "s_empty": lambda r: r["s"] in (None, ""),
    "s_null": lambda r: r["s"] is None,
    # numbers: [ ] include the bound, ( ) exclude it, `a to b` includes both, NOT drops NULL
    "n_closed": lambda r: r["n"] is not None and 2 <= r["n"] <= 6,
    "n_open": lambda r: r["n"] is not None and 2 < r["n"] < 6,
    "n_half_open": lambda r: r["n"] is not None and 2 <= r["n"] < 6,
    "n_to": lambda r: r["n"] is not None and 2 <= r["n"] <= 6,
    "n_and": lambda r: r["n"] is not None and 2 <= r["n"] <= 6,
    "n_or": lambda r: r["n"] is not None and (r["n"] > 6 or r["n"] < 3),
    "n_not": lambda r: r["n"] is not None and r["n"] != 6,
    "n_minus": lambda r: r["n"] == -2,
    "n_list": lambda r: r["n"] in (4, 6),
    "n_not_list": lambda r: r["n"] is not None and r["n"] not in (4, 6),
    "n_null": lambda r: r["n"] is None,
    "n_not_null": lambda r: r["n"] is not None,
    "n_gt": lambda r: r["n"] is not None and r["n"] > 4,
    # dates: a day, month or year is [its start, the next one's start); before is <, after is >=, `a to b` is [a, b)
    "d_day": lambda r: r["d"] == D(2027, 6, 30),
    "d_month": lambda r: r["d"] is not None and (r["d"].year, r["d"].month) == (2027, 7),
    "d_year": lambda r: r["d"] is not None and r["d"].year == 2027,
    "d_before": lambda r: r["d"] is not None and r["d"] < D(2027, 7, 1),
    "d_after": lambda r: r["d"] is not None and r["d"] >= D(2027, 7, 1),
    "d_range": lambda r: r["d"] is not None and D(2027, 6, 1) <= r["d"] < D(2027, 7, 1),
    "d_null": lambda r: r["d"] is None,
    "d_not_null": lambda r: r["d"] is not None,
    "t_day": lambda r: r["ts"] is not None and r["ts"].date() == D(2027, 7, 1),
    "t_before": lambda r: r["ts"] is not None and r["ts"] < TS(2027, 7, 1, 6, 0),
    "t_month": lambda r: r["ts"] is not None and (r["ts"].year, r["ts"].month) == (2027, 7),
    # a tier filters on its label
    "tier_filtered": lambda r: r["n"] is not None and 5 <= r["n"] < 10,
    "n_sum_big": lambda r: r["n"] is not None and r["n"] > 3,
}
# relative to today: [today - 6, today + 1), this calendar month, the one before, before today - 30
RELATIVE = {
    "d_last_7": ("7 days", lambda d, t: t - timedelta(days=6) <= d <= t),
    "d_this_month": ("this month", lambda d, t: (d.year, d.month) == (t.year, t.month)),
    "d_last_month": ("last month", lambda d, t: (d.year, d.month) == ((t.replace(day=1) - timedelta(days=1)).year,
                                                                      (t.replace(day=1) - timedelta(days=1)).month)),
    "d_ago": ("before 30 days ago", lambda d, t: d < t - timedelta(days=30)),
}


def test_every_filtered_measure_is_checked():
    view = lkml.load((FIX / "facts.view.lkml").read_text())["views"][0]
    filtered = {m["name"] for m in view["measures"] if m.get("filters__all")}
    assert filtered == set(FILTERS) | set(RELATIVE)


@pytest.mark.parametrize("dialect", ["duckdb", "bigquery"])
def test_filter_expressions_count_lookers_rows(bundle_for, db, dialect):
    """Each measure filter through the semantic translation, counted on the rows."""
    b = bundle_for(dialect)
    cols = {d.name: d.expr or d.name for d in _by_name(b.semantic_models)["facts"].dimensions} | {"at": "ts", "on": "d"}
    metrics = _by_name(b.metrics)
    for name, keep in FILTERS.items():
        m = metrics[name]
        assert not m.todo, (name, m.todo)
        cond = re.sub(r"\{\{ (?:Time)?Dimension\('facts__(\w+)'(?:, '\w+')?\) \}\}", lambda x: f"({cols[x.group(1)]})", m.filter)
        (got,) = run(db, f"SELECT COUNT(*) AS n FROM example.facts WHERE {cond}", dialect)
        assert got["n"] == sum(map(keep, rows("facts"))), (name, cond)


def test_relative_date_filters(bundle_for, db):
    """Relative dates stay TODO in the semantic layer (MetricFlow's type for the day grain is not checked
    on BigQuery); in tile SQL on BigQuery they count the days relative to today."""
    metrics = _by_name(bundle_for("bigquery").metrics)
    today = _today()
    days = [today + timedelta(days=k) for k in range(-70, 5)]
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    con.execute("create table days (d DATE)")
    con.executemany("insert into days values (?)", [(d,) for d in days])
    for name, (value, keep) in RELATIVE.items():
        assert "Looker filter expression" in metrics[name].todo, name
        cond = looker_filter_sql("d", value, "time", "date", "bigquery")
        assert cond and "CURRENT_DATE()" in cond, (name, cond)
        (got,) = run(con, f"SELECT COUNT(*) AS n FROM days WHERE {cond}", "bigquery")
        assert got["n"] == sum(keep(d, today) for d in days), (name, cond)


def _unit_start(t: datetime, unit: str, week_start: str = "monday") -> datetime:
    """The start of the unit `t` is in: weeks begin on week_start_day, quarters in January, April, July, October;
    seconds are not truncated (Looker counts them from the current moment)."""
    if unit == "second":
        return t
    finer = ["second", "minute", "hour"][: ["second", "minute", "hour"].index(unit) if unit in ("second", "minute", "hour") else 3]
    t = t.replace(microsecond=0, **{f: 0 for f in finer if f != unit})
    if unit == "week":
        return t - timedelta(days=(t.weekday() - WEEKDAYS.index(week_start)) % 7)
    if unit in ("month", "quarter", "year"):
        t = t.replace(day=1, month=1 if unit == "year" else t.month - (t.month - 1) % 3 if unit == "quarter" else t.month)
    return t


def _shift(t: datetime, n: int, unit: str) -> datetime:
    return t + (relativedelta(months=3 * n) if unit == "quarter" else relativedelta(**{f"{unit}s": n}))


def _for(start: datetime, n: int, unit: str) -> tuple[datetime, datetime]:
    return start, _shift(start, n, unit)


# Looker's relative date filters (filter expressions documentation), as [start, end) from the moment `now`:
# every period starts at the start of its unit (a second at the current moment); `N <unit>` is the current
# unit and the N-1 before it,
# `N <unit> ago for M <unit>` starts N units before the current one and lasts M units,
# `before` / `after N <unit> ago` are < / >= that start, `this / last / next <unit>` are one unit.
RELATIVE_BOUNDS = {
    "12 months ago for 12 months": lambda now, ws="monday": _for(_shift(_unit_start(now, "month"), -12, "month"), 12, "month"),
    "2 years ago for 2 years": lambda now, ws="monday": _for(_shift(_unit_start(now, "year"), -2, "year"), 2, "year"),
    "52 weeks ago for 52 weeks": lambda now, ws="monday": _for(_shift(_unit_start(now, "week", ws), -52, "week"), 52, "week"),
    "3 years ago for 3 years": lambda now, ws="monday": _for(_shift(_unit_start(now, "year"), -3, "year"), 3, "year"),
    "2 weeks ago for 3 days": lambda now, ws="monday": _for(_shift(_unit_start(now, "week", ws), -2, "week"), 3, "day"),
    "3 days ago": lambda now, ws="monday": _for(_shift(_unit_start(now, "day"), -3, "day"), 1, "day"),
    "1 years": lambda now, ws="monday": _for(_unit_start(now, "year"), 1, "year"),
    "1 year": lambda now, ws="monday": _for(_unit_start(now, "year"), 1, "year"),
    "2 quarters": lambda now, ws="monday": _for(_shift(_unit_start(now, "quarter"), -1, "quarter"), 2, "quarter"),
    "7 days": lambda now, ws="monday": _for(_shift(_unit_start(now, "day"), -6, "day"), 7, "day"),
    "48 hours": lambda now, ws="monday": _for(_shift(_unit_start(now, "hour"), -47, "hour"), 48, "hour"),
    "15 minutes": lambda now, ws="monday": _for(_shift(_unit_start(now, "minute"), -14, "minute"), 15, "minute"),
    "before 0 seconds ago": lambda now, ws="monday": (None, _unit_start(now, "second")),
    "before 3 months ago": lambda now, ws="monday": (None, _shift(_unit_start(now, "month"), -3, "month")),
    "after 2 weeks ago": lambda now, ws="monday": (_shift(_unit_start(now, "week", ws), -2, "week"), None),
    "this week": lambda now, ws="monday": _for(_unit_start(now, "week", ws), 1, "week"),
    "last week": lambda now, ws="monday": _for(_shift(_unit_start(now, "week", ws), -1, "week"), 1, "week"),
    "next month": lambda now, ws="monday": _for(_shift(_unit_start(now, "month"), 1, "month"), 1, "month"),
    "last quarter": lambda now, ws="monday": _for(_shift(_unit_start(now, "quarter"), -1, "quarter"), 1, "quarter"),
    "this year": lambda now, ws="monday": _for(_unit_start(now, "year"), 1, "year"),
    "next year": lambda now, ws="monday": _for(_shift(_unit_start(now, "year"), 1, "year"), 1, "year"),
    "Last  Day": lambda now, ws="monday": _for(_shift(_unit_start(now, "day"), -1, "day"), 1, "day"),
    "30 seconds": lambda now, ws="monday": _for(_shift(now, -29, "second"), 30, "second"),
    "10 seconds ago for 5 seconds": lambda now, ws="monday": _for(_shift(now, -10, "second"), 5, "second"),
    "after 60 seconds ago": lambda now, ws="monday": (_shift(now, -60, "second"), None),
}
RELATIVE_TILES = {"fact_relative_years", "fact_relative_hours", "fact_relative_weeks"}


def _bounds_now(con: duckdb.DuckDBPyConnection, sql: list[str]) -> tuple[list, datetime, datetime]:
    before = datetime.now(timezone.utc).replace(tzinfo=None)
    (got,) = run(con, "SELECT " + ", ".join(f"CAST({x} AS STRING) AS b{i}" for i, x in enumerate(sql)), "bigquery")
    return list(got.values()), before, datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.mark.parametrize("week_start", ["monday", "sunday"])
@pytest.mark.parametrize("datatype", ["timestamp", "date"])
@pytest.mark.parametrize("value", RELATIVE_BOUNDS)
def test_relative_bounds_follow_lookers_periods(value, datatype, week_start):
    """Each relative form's bounds on BigQuery, for times and dates (sub-day units on a date stay TODO)."""
    sub_day = any(u in value.lower() for u in ("hour", "minute", "second"))
    rel = relative_bounds(value, datatype, "bigquery", week_start)
    if datatype == "date" and sub_day:
        assert rel is None
        return
    assert rel, value
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    got, before, after = _bounds_now(con, [b for b in rel if b])
    # each bound lies between the bounds at the moments before and after the query (equal unless the unit
    # is a second or the query straddles the start of a unit); DATE_ADD stays a DATE in BigQuery
    got = [datetime.fromisoformat(g.split("+")[0]) for g in got]
    lo, hi = ([b for b in RELATIVE_BOUNDS[value](now, week_start) if b is not None] for now in (before, after))
    if datatype == "date":
        got, lo, hi = ([datetime.combine(x.date(), datetime.min.time()) for x in xs] for xs in (got, lo, hi))
    assert all(a <= g <= b for a, g, b in zip(lo, got, hi)), (value, rel, got, lo, hi)
    if "second" in value:
        assert "TIMESTAMP_TRUNC" not in str(rel), rel
    if "week" not in value:
        assert relative_bounds(value, datatype, "bigquery", "different days per model") == rel


def test_relative_date_filters_beyond_the_grammar_stay_todo():
    for value in ("3 complete days", "today", "2 days from now", "before 2 days ago for 1 day", "0 days",
                  "1 fiscal quarter", "3 days ago for 0 days",
                  # invalid in Looker
                  "this second", "this minute", "this hour", "next second", "next minute", "next hour",
                  # Looker's own range for these is empty
                  "last second", "last minute", "last hour"):
        assert looker_filter_sql("d", value, "time", "timestamp", "bigquery") is None, value
    assert looker_filter_sql("d", "3 days", "time", "timestamp", "snowflake") is None
    assert looker_filter_sql("d", "this week", "time", "date", "bigquery", week_start="different days per model") is None
    assert looker_filter_sql("d", "3 days,soon", "time", "date", "bigquery") is None  # every part or none


def test_comma_separated_relative_dates_are_or(bundle_for, db):
    """`A,B` keeps the rows either part keeps, in tile SQL as in a single filter."""
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    times = [now + timedelta(hours=k) for k in range(-24 * 365 * 4, 24 * 400, 7)]
    con.execute("create table times (t TIMESTAMP)")
    con.executemany("insert into times values (?)", [(t,) for t in times])
    a, b = RELATIVE_BOUNDS["3 years ago for 3 years"](now), RELATIVE_BOUNDS["1 years"](now)
    cond = looker_filter_sql("t", "3 years ago for 3 years,1 years", "time", "timestamp", "bigquery")
    assert cond and " OR " in cond
    (got,) = run(con, f"SELECT COUNT(*) AS n FROM times WHERE {cond}", "bigquery")
    assert got["n"] == sum(a[0] <= t < a[1] or b[0] <= t < b[1] for t in times)

    tiles = tiles_of(bundle_for("bigquery"))
    facts = [r for r in rows("facts") if r["ts"] is not None]
    keep = {
        "fact_relative_years": lambda r, now: any(s <= r["ts"] < e for s, e in (RELATIVE_BOUNDS["3 years ago for 3 years"](now),
                                                                              RELATIVE_BOUNDS["1 years"](now))),
        "fact_relative_hours": lambda r, now: (RELATIVE_BOUNDS["48 hours"](now)[0] <= r["ts"] < RELATIVE_BOUNDS["48 hours"](now)[1]
                                               or r["ts"] < RELATIVE_BOUNDS["before 0 seconds ago"](now)[1]
                                               or r["ts"] >= RELATIVE_BOUNDS["after 2 weeks ago"](now)[0]),
        "fact_relative_weeks": lambda r, now: any(s.date() <= r["d"] < e.date() for s, e in
                                                  (RELATIVE_BOUNDS["52 weeks ago for 52 weeks"](now), RELATIVE_BOUNDS["next year"](now))),
    }
    for name in RELATIVE_TILES:
        tile = tiles[name]
        assert tile.sql and not tile.todo, (name, tile.todo)
        (got,) = run(db, tile.sql, "bigquery")
        assert got["facts_count"] == sum(keep[name](r, now) for r in facts), name


@pytest.mark.parametrize("dialect", ["duckdb", "bigquery"])
def test_tile_filter_on_a_timeframe_filters_the_time(bundle_for, db, dialect):
    """A tile filtered on at_month: "2027-07" counts the rows whose time is in July."""
    tile = tiles_of(bundle_for(dialect))["fact_month_filter"]
    assert tile.sql, tile.todo
    (got,) = run(db, tile.sql, dialect)
    assert got["facts_count"] == sum(map(FILTERS["t_month"], rows("facts")))


# ---------------------------------------------------------------- dimension types and statistics

def tier_label(n: float | None, tiers: list[float], style: str) -> str:
    """Looker's tier labels: integer 'Below 0', '0 to 4', '10 or Above'; classic 'T00 (-inf,0.0)',
    'T01 [0.0,5.0)', 'T03 [10.0,inf)'; NULL is 'Undefined' / 'TXX Undefined'."""
    if n is None:
        return "Undefined" if style == "integer" else "TXX Undefined"
    i = sum(n >= t for t in tiers)
    lo, hi = (tiers[i - 1] if i else None), (tiers[i] if i < len(tiers) else None)
    if style == "integer":
        return f"Below {tiers[0]:g}" if lo is None else f"{lo:g} or Above" if hi is None else f"{lo:g} to {hi - 1:g}"
    return f"T{i:02d} " + ("(-inf" if lo is None else f"[{lo:.1f}") + "," + ("inf)" if hi is None else f"{hi:.1f})")


@pytest.mark.parametrize("dialect", ["duckdb", "bigquery"])
def test_yesno_tier_and_case_dimensions(bundle_for, db, dialect):
    """yesno is 'Yes' / 'No' with NULL as 'No'; case takes the first matching when, else the else label."""
    got = run(db, tiles_of(bundle_for(dialect))["fact_types"].sql, dialect)
    by_id = {r["id"]: r for r in rows("facts")}
    assert sorted(r["facts_id"] for r in got) == sorted(by_id)
    for r in got:
        f = by_id[r["facts_id"]]
        assert r["facts_y"] == ("Yes" if f["y"] else "No")
        assert r["facts_n_big"] == ("Yes" if f["n"] is not None and f["n"] > 3 else "No")
        assert r["facts_n_tier"] == tier_label(f["n"], [0, 5, 10], "integer")
        assert r["facts_n_tier_classic"] == tier_label(f["n"], [0, 5, 10], "classic")
        assert r["facts_s_case"] == ("Paper" if f["s"] == "paper" else "Big" if f["n"] is not None and f["n"] > 5 else "Other")


@pytest.mark.parametrize("dialect", ["duckdb", "bigquery"])
def test_sums_show_zero_for_an_empty_group(bundle_for, db, dialect):
    b = bundle_for(dialect)
    assert _by_name(b.metrics)["n_sum_big"].fill_nulls_with == 0
    got = {r["facts_n_big"]: r for r in run(db, tiles_of(b)["fact_stats_by_big"].sql, dialect)}
    facts = rows("facts")
    big = [r["n"] for r in facts if FILTERS["n_sum_big"](r)]
    assert (got["Yes"]["facts_count"], float(got["Yes"]["facts_n_sum_big"])) == (len(big), sum(big))
    assert (got["No"]["facts_count"], float(got["No"]["facts_n_sum_big"])) == (len(facts) - len(big), 0)


N = sorted(r["n"] for r in rows("facts") if r["n"] is not None)


def looker_bigquery_percentile(values: list[float], p: float) -> float:
    """Looker's percentile on BigQuery (up to 10,000 values): the mean of the sorted values at offsets
    floor(n * p - 0.0000001) and floor(n * p)."""
    v = sorted(values)
    return (v[floor(len(v) * p - 0.0000001)] + v[floor(len(v) * p)]) / 2


def percentile_cont(values: list[float], p: float) -> float:
    v = sorted(values)
    pos = (len(v) - 1) * p
    lo = floor(pos)
    return v[lo] + (v[min(lo + 1, len(v) - 1)] - v[lo]) * (pos - lo)


def test_percentiles(bundle_for):
    """On BigQuery Looker's p25 / p90 are neither PERCENTILE_CONT nor PERCENTILE_DISC, so they stay TODO;
    its median is the continuous median. On other warehouses the percentile is PERCENTILE_CONT."""
    metrics = _by_name(bundle_for("bigquery").metrics)
    for name in ("n_p25", "n_p90"):
        assert "on BigQuery" in metrics[name].todo, name
    assert looker_bigquery_percentile(N, 0.5) == statistics.median(N) == percentile_cont(N, 0.5)
    for p in (0.25, 0.9):  # why: on these rows the two formulas disagree
        assert looker_bigquery_percentile(N, p) != percentile_cont(N, p)
    median = _by_name(_by_name(bundle_for("bigquery").semantic_models)["facts"].measures)["n_median"]
    assert median.agg == "median"
    other = _by_name(_by_name(bundle_for("snowflake").semantic_models)["facts"].measures)
    assert other["n_p25"].agg_params == {"percentile": 0.25, "use_discrete_percentile": False}


# ---------------------------------------------------------------- the Ossie semantic layer

def ossie_doc(tmp: Path, dialect: str) -> dict:
    import yaml

    from unshackle.target import ossie
    from unshackle.target.layout import Layout

    b = parse(tmp, dialect)
    files = ossie.render(b, Layout.load(tmp))
    return yaml.safe_load(next(v for k, v in files.items() if k.endswith("facts.ossie.yaml")))


def ossie_query(doc: dict, quote: str, skip: set[str] = frozenset()) -> tuple[str, list[str]]:
    """The dataset as a consumer runs it: its fields over the rows, its metrics over those fields."""
    ds = next(x for x in doc["datasets"] if x["name"] == "facts")

    def expr(item: dict) -> str:
        return item["expression"]["dialects"][0]["expression"]

    fields = ", ".join(f"{expr(f)} AS {quote}{f['name']}{quote}" for f in ds["fields"])
    metrics = [m for m in ds.get("metrics", []) if m["name"] not in skip]
    selects = ", ".join(f"{expr(m)} AS {m['name']}" for m in metrics)
    return f"WITH fields AS (SELECT {fields} FROM example.facts) SELECT {selects} FROM fields", [m["name"] for m in metrics]


def ossie_expected() -> dict:
    facts = rows("facts")
    out = {name: sum(map(keep, facts)) for name, keep in FILTERS.items() if name != "n_sum_big"}
    return out | {
        "facts_count": len(facts), "n_median": statistics.median(N), "n_p25": percentile_cont(N, 0.25),
        "n_p90": percentile_cont(N, 0.9), "s_distinct": len({r["s"] for r in facts if r["s"] is not None}),
        "n_sum": sum(N), "n_avg": statistics.mean(N), "n_min": min(N),
        "n_max_date": max(r["d"] for r in facts if r["d"] is not None), "n_per_row": sum(N) / len(facts),
        "n_sum_big": sum(r["n"] for r in facts if FILTERS["n_sum_big"](r)),
    }


def assert_metrics(got: dict, names: list[str]) -> None:
    expected = ossie_expected()
    assert set(names) <= set(expected), set(names) - set(expected)
    for name in names:
        want = expected[name]
        assert (got[name] == want) if isinstance(want, date) else (float(got[name]) == pytest.approx(want)), name


def test_ossie_metrics_are_bigquery_sql(tmp_path):
    """On BigQuery: no MEDIAN aggregate (Looker's median formula instead), no DATE_TRUNC('grain', x)
    (TIMESTAMP_TRUNC), no PERCENTILE_CONT (skipped); a field named like a reserved word is quoted."""
    doc = ossie_doc(tmp_path, "bigquery")
    metrics = {m["name"]: m["expression"]["dialects"][0] for ds in doc["datasets"] for m in ds.get("metrics", [])}
    assert {d["dialect"] for d in metrics.values()} == {"BIGQUERY"}
    assert not {"n_p25", "n_p90"} & set(metrics)
    for name, d in metrics.items():
        sqlglot.parse_one(f"SELECT {d['expression']} FROM t", read="bigquery")
        assert "DATE_TRUNC('" not in d["expression"] and "MEDIAN(" not in d["expression"], name
    x = r"`?n`?"
    median = " ".join(metrics["n_median"]["expression"].split())
    agg = rf"ARRAY_AGG\({x} IGNORE NULLS ORDER BY {x} LIMIT 10000\)"
    assert re.fullmatch(
        rf"CASE WHEN COUNT\({x}\) <= 10000 THEN \({agg}\[OFFSET\(CAST\(FLOOR\(COUNT\({x}\) \* 0\.5 - 0\.0000001\) AS INT64\)\)\] \+ "
        rf"{agg}\[OFFSET\(CAST\(FLOOR\(COUNT\({x}\) \* 0\.5\) AS INT64\)\)\]\) / 2 "
        rf"ELSE APPROX_QUANTILES\({x}, 1000\)\[OFFSET\(500\)\] END", median), median
    assert "TIMESTAMP_TRUNC(CAST(`at` AS TIMESTAMP), SECOND)" in metrics["t_before"]["expression"]


@pytest.mark.parametrize("dialect", ["duckdb", "bigquery"])
def test_ossie_metrics_return_lookers_numbers(tmp_path, db, dialect):
    """Every metric of the Ossie document, run as a consumer runs it, is Looker's number for the measure.
    (On BigQuery the median's ARRAY_AGG ... LIMIT has no DuckDB equivalent: its formula is checked above.)"""
    doc = ossie_doc(tmp_path, dialect)
    bigquery = dialect == "bigquery"
    sql, names = ossie_query(doc, "`" if bigquery else '"', {"n_median"} if bigquery else set())
    (got,) = run(db, sql, dialect)
    assert_metrics(got, names)
    assert len(names) >= 40
