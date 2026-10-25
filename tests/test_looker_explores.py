"""Timeframe references, fields of joined views, and LookML dashboards written as SQL over explores.

The DuckDB checks build a few rows for the views and compare what unshackle wrote (a tile's SQL,
an Ossie metric) with SQL written by hand from the LookML's meaning.
"""

import json
from pathlib import Path

import pytest
import yaml
from conftest import notebook_source

from unshackle import build, verify
from unshackle.raw import RawStore
from unshackle.sources.looker import LookerSource

MODEL = """connection: "wh"
include: "*.view"
include: "*.dashboard"
explore: orders {
  join: users { sql_on: ${orders.user_id} = ${users.id} ;; relationship: many_to_one }
  join: items { sql_on: ${orders.id} = ${items.order_id} ;; relationship: one_to_many }
}
"""
ORDERS = """view: orders {
  sql_table_name: shop.orders ;;
  dimension: id { primary_key: yes type: number sql: ${TABLE}.id ;; }
  dimension: user_id { type: number sql: ${TABLE}.user_id ;; }
  dimension: status { sql: ${TABLE}.status ;; }
  dimension: amount { type: number sql: ${TABLE}.amount ;; }
  dimension_group: created { type: time timeframes: [raw, date, week, month, quarter, year, month_num, day_of_week] sql: ${TABLE}.created_at ;; }
  dimension: created_day_key { sql: CAST(${created_date} AS VARCHAR) ;; }
  dimension: created_year_num { type: number sql: ${created_year} ;; }
  dimension: in_january { type: yesno sql: ${created_month_num} = 1 ;; }
  dimension: created_weekday { sql: ${created_day_of_week} ;; }
  dimension: user_state { sql: ${users.state} ;; }
  measure: count { type: count }
  measure: revenue { type: sum sql: ${amount} ;; }
  measure: revenue_with_fee { type: sum sql: ${amount} + ${users.fee} ;; }
  measure: revenue_2024 { type: sum sql: CASE WHEN ${created_year} = 2024 THEN ${amount} END ;; }
  measure: ca_revenue { type: sum sql: ${amount} ;; filters: [users.state: "CA"] }
}
"""
USERS = """view: users {
  sql_table_name: shop.users ;;
  dimension: id { primary_key: yes type: number sql: ${TABLE}.id ;; }
  dimension: state { sql: ${TABLE}.state ;; }
  dimension: fee { type: number sql: ${TABLE}.fee ;; }
  measure: count { type: count }
  measure: order_amount { type: sum sql: ${orders.amount} ;; }
}
"""
ITEMS = """view: items {
  sql_table_name: shop.items ;;
  dimension: id { primary_key: yes type: number sql: ${TABLE}.id ;; }
  dimension: order_id { type: number sql: ${TABLE}.order_id ;; }
  dimension: qty { type: number sql: ${TABLE}.qty ;; }
  measure: total_qty { type: sum sql: ${qty} ;; }
}
"""
DASHBOARD = """- dashboard: shop_overview
  title: Shop Overview
  layout: newspaper
  filters:
  - name: State
    type: field_filter
    model: shop
    explore: orders
    field: users.state
  - name: Status
    type: field_filter
    model: shop
    explore: orders
    field: orders.status
    default_value: complete
  - name: Created
    type: date_filter
    default_value: 30 days
  elements:
  - name: header
    type: text
    title_text: Orders
    row: 0
    col: 0
  - name: revenue_by_month
    title: Revenue by month
    model: shop
    explore: orders
    type: looker_line
    fields: [orders.created_month, orders.revenue]
    sorts: [orders.created_month]
    listen:
      State: users.state
      Status: orders.status
      Created: orders.created_date
    row: 2
    col: 0
  - name: revenue_kpi
    title: Revenue with fees
    model: shop
    explore: orders
    type: single_value
    fields: [orders.revenue_with_fee]
    filters:
      orders.status: complete,shipped
    row: 2
    col: 12
  - name: by_state
    title: Orders by state
    model: shop
    explore: orders
    type: looker_bar
    fields: [users.state, orders.count, users.count]
    limit: 10
    row: 4
    col: 0
  - name: qty_by_status
    title: Revenue and quantity
    model: shop
    explore: orders
    type: looker_column
    fields: [orders.status, orders.revenue, items.total_qty]
    row: 4
    col: 12
  - name: state_map
    title: Map
    model: shop
    explore: orders
    type: looker_map
    fields: [users.state, orders.count]
    row: 6
    col: 0
"""
FILES = {"shop.model.lkml": MODEL, "orders.view.lkml": ORDERS, "users.view.lkml": USERS, "items.view.lkml": ITEMS,
         "shop_overview.dashboard.lookml": DASHBOARD}


def _parse(repo: Path, files: dict[str, str], warehouse: str = ""):
    if warehouse:
        (repo / "unshackle.toml").parent.mkdir(parents=True, exist_ok=True)
        (repo / "unshackle.toml").write_text(f'[datasources.warehouse]\ntype = "{warehouse}"\n')
    store = RawStore(repo, "looker")
    for name, text in files.items():
        store.put_text(f"lookml/{name}", text)
    return LookerSource().parse(store)


def _by_name(items):
    return {i.name: i for i in items}


def _warehouse(path: Path) -> str:
    import duckdb

    con = duckdb.connect(str(path))
    con.execute("CREATE SCHEMA shop")
    con.execute("CREATE TABLE shop.users (id INT, state VARCHAR, fee DOUBLE)")
    con.execute("INSERT INTO shop.users VALUES (1, 'CA', 1.5), (2, 'NY', 2.0), (3, 'CA', 0.5)")
    con.execute("CREATE TABLE shop.orders (id INT, user_id INT, status VARCHAR, amount DOUBLE, created_at TIMESTAMP)")
    con.execute("""INSERT INTO shop.orders VALUES
        (10, 1, 'complete', 100, '2024-01-03 10:00'), (11, 1, 'shipped', 50, '2024-01-20 23:30'),
        (12, 2, 'complete', 70, '2024-02-01 00:10'), (13, 3, 'cancelled', 30, '2023-12-31 12:00'),
        (14, 9, 'complete', 20, '2024-02-15 08:00')""")
    con.execute("CREATE TABLE shop.items (id INT, order_id INT, qty INT)")
    con.execute("INSERT INTO shop.items VALUES (1, 10, 2), (2, 10, 1), (3, 12, 4)")
    con.close()
    return f"duckdb:///{path}"


# ---------------------------------------------------------------- timeframes


def test_timeframe_references_resolve_to_the_group_sql(tmp_path: Path):
    b = _parse(tmp_path, FILES)
    dims = _by_name(_by_name(b.semantic_models)["orders"].dimensions)
    assert dims["created_day_key"].expr == "CAST((CAST(created_at AS DATE)) AS VARCHAR)"
    assert dims["created_year_num"].expr == "(EXTRACT(YEAR FROM created_at))"
    assert dims["in_january"].expr == "(EXTRACT(MONTH FROM created_at)) = 1"
    # day_of_week is a name ('Monday') written differently per warehouse
    assert "the day's name ('Monday')" in dims["created_weekday"].todo and "unshackle.toml" in dims["created_weekday"].todo
    meas = _by_name(_by_name(b.semantic_models)["orders"].measures)
    assert meas["revenue_2024"].expr == "CASE WHEN (EXTRACT(YEAR FROM created_at)) = 2024 THEN amount END"


def test_month_and_week_need_the_warehouse_dialect(tmp_path: Path):
    view = """view: t {
  sql_table_name: s.t ;;
  dimension: id { primary_key: yes }
  dimension_group: d { type: time timeframes: [date, week, month] sql: ${TABLE}.ts ;; }
  dimension_group: e { type: time timeframes: [date, hour_of_day] datatype: date sql: ${TABLE}.day ;; }
  dimension_group: n { type: time timeframes: [date] datatype: epoch sql: ${TABLE}.epoch ;; }
  dimension: m { sql: ${d_month} ;; }
  dimension: w { sql: ${d_week} ;; }
  dimension: e_day { sql: ${e_date} ;; }
  dimension: e_hour { sql: ${e_hour_of_day} ;; }
  dimension: n_day { sql: ${n_date} ;; }
}"""
    dims = _by_name(_parse(tmp_path / "a", {"t.view.lkml": view}).semantic_models[0].dimensions)
    assert "'YYYY-MM' string" in dims["m"].todo and "unshackle.toml" in dims["m"].todo and not dims["m"].expr
    assert dims["e_day"].expr == "day"  # datatype: date is already a date
    assert "hour of a date column" in dims["e_hour"].todo
    assert "datatype: epoch" in dims["n_day"].todo
    bq = _by_name(_parse(tmp_path / "b", {"t.view.lkml": view}, "bigquery").semantic_models[0].dimensions)
    assert bq["m"].expr == "(FORMAT_DATE('%Y-%m', CAST(ts AS DATE)))"
    assert bq["w"].expr == "(FORMAT_DATE('%F', DATE_TRUNC(CAST(ts AS DATE), WEEK(MONDAY))))"
    sf = _by_name(_parse(tmp_path / "c", {"t.view.lkml": view}, "snowflake").semantic_models[0].dimensions)
    assert sf["m"].expr == "(TO_CHAR(CAST(ts AS DATE), 'YYYY-MM'))"
    # Snowflake's DATE_TRUNC('week') follows the account's WEEK_START, so weeks are counted back from the weekday
    assert sf["w"].expr == "(TO_CHAR(DATEADD(day, -((DAYOFWEEKISO(CAST(ts AS DATE)) - 1)), CAST(ts AS DATE)), 'YYYY-MM-DD'))"
    # a model whose weeks start on Sunday, as Looker writes it on BigQuery
    sunday = _parse(tmp_path / "d", {"t.view.lkml": view, "m.model.lkml": 'connection: "wh"\nweek_start_day: sunday\ninclude: "*.view"\n'}, "bigquery")
    assert _by_name(sunday.semantic_models[0].dimensions)["w"].expr == "(FORMAT_DATE('%F', DATE_TRUNC(CAST(ts AS DATE), WEEK(SUNDAY))))"


def test_week_and_month_match_looker_values_in_duckdb(tmp_path: Path):
    import duckdb

    view = """view: t {
  sql_table_name: t ;;
  dimension: id { primary_key: yes }
  dimension_group: d { type: time timeframes: [week, month] sql: ${TABLE}.ts ;; }
  dimension: m { sql: ${d_month} ;; }
  dimension: w { sql: ${d_week} ;; }
}"""
    dims = _by_name(_parse(tmp_path, {"t.view.lkml": view}, "duckdb").semantic_models[0].dimensions)
    con = duckdb.connect()
    con.execute("CREATE TABLE t AS SELECT TIMESTAMP '2024-03-03 22:00' AS ts")  # a Sunday
    # Looker: month '2024-03', week starting Monday '2024-02-26'
    assert con.execute(f"SELECT {dims['m'].expr}, {dims['w'].expr} FROM t").fetchone() == ("2024-03", "2024-02-26")


# ---------------------------------------------------------------- fields of joined views


def test_measure_over_a_joined_view_becomes_a_model_scoped_metric(tmp_path: Path):
    b = _parse(tmp_path, FILES)
    orders = _by_name(b.semantic_models)["orders"]
    meas = _by_name(orders.measures)
    assert meas["revenue_with_fee"].expr == "{{ Dimension('orders__amount') }} + {{ Dimension('users__fee') }}"
    m = _by_name(b.metrics)
    assert not m["revenue_with_fee"].todo and m["revenue_with_fee"].measure == "revenue_with_fee"
    # a dimension can't read another table: precise TODO naming the join
    assert "explore orders joins users many_to_one on ${orders.user_id} = ${users.id}" in _by_name(orders.dimensions)["user_state"].todo
    # users -> orders repeats users rows: Looker needs symmetric aggregates, so it stays TODO
    fan = m["order_amount"]
    assert "rows of users repeat" in fan.todo and "symmetric aggregates" in fan.todo and fan.source_expression == "type: sum sql: ${orders.amount}"
    assert "order_amount" not in _by_name(_by_name(b.semantic_models)["users"].measures)

    build.apply(b, tmp_path)
    doc = yaml.safe_load((tmp_path / "semantic/orders.ossie.yaml").read_text())
    model_metrics = {x["name"]: x["expression"]["dialects"][0]["expression"] for x in doc.get("metrics", [])}
    assert model_metrics["revenue_with_fee"] == "SUM(orders.amount + users.fee)"
    ds_metrics = {x["name"]: x["expression"]["dialects"][0]["expression"] for x in doc["datasets"][0]["metrics"]}
    fields = {x["name"]: x["expression"]["dialects"][0]["expression"] for x in doc["datasets"][0]["fields"]}
    assert ds_metrics["revenue_2024"] == "SUM(revenue_2024_value)"
    assert fields["revenue_2024_value"] == "CASE WHEN (EXTRACT(YEAR FROM created_at)) = 2024 THEN amount END"


def test_cross_view_metric_parity_in_duckdb(tmp_path: Path):
    import duckdb

    b = _parse(tmp_path, FILES)
    build.apply(b, tmp_path)
    doc = yaml.safe_load((tmp_path / "semantic/orders.ossie.yaml").read_text())
    expr = {x["name"]: x["expression"]["dialects"][0]["expression"] for x in doc["metrics"]}["revenue_with_fee"]
    url = _warehouse(tmp_path / "wh.duckdb")
    con = duckdb.connect(url.removeprefix("duckdb:///"))
    # the metric evaluated over orders and the dataset its relationship reaches
    got = con.execute(f"SELECT {expr} FROM shop.orders AS orders LEFT JOIN shop.users AS users ON orders.user_id = users.id").fetchone()[0]
    # by hand: each order's amount plus its user's fee; order 14 has no user, so Looker's SUM skips it
    assert got == pytest.approx((100 + 1.5) + (50 + 1.5) + (70 + 2.0) + (30 + 0.5))
    fields = {x["name"]: x["expression"]["dialects"][0]["expression"] for x in doc["datasets"][0]["fields"]}
    assert con.execute(f"SELECT SUM({fields['revenue_2024_value']}) FROM shop.orders").fetchone()[0] == 240  # orders 10, 11, 12, 14


def test_explores_that_join_a_view_differently_keep_the_todo(tmp_path: Path):
    files = dict(FILES)
    files["shop.model.lkml"] = MODEL + """explore: orders_by_buyer {
  from: orders
  join: users { sql_on: ${orders_by_buyer.id} = ${users.id} ;; relationship: many_to_one }
}
"""
    m = _by_name(_parse(tmp_path, files).metrics)
    assert "explores join differently" in m["revenue_with_fee"].todo
    assert "orders: x_to_one" not in m["revenue_with_fee"].todo and "orders (many_to_one)" in m["revenue_with_fee"].todo


# ---------------------------------------------------------------- LookML dashboards


def test_lookml_dashboard_becomes_a_notebook(tmp_path: Path):
    b = _parse(tmp_path, FILES, "duckdb")
    (d,) = b.dashboards
    assert d.name == "Shop Overview" and d.source_ref == "lookml/shop_overview.dashboard.lookml dashboard: shop_overview"
    assert d.layout == [["header"], ["revenue_by_month", "revenue_kpi"], ["by_state", "qty_by_status"], ["state_map"]]
    f = _by_name(d.filters)
    assert (f["state"].default, f["status"].default, f["created"].default, f["created"].kind) == (None, "complete", "30 days", "date_range")
    t = {x.id: x for x in d.tiles}
    assert t["header"].kind == "text" and "## Orders" in t["header"].text
    line = t["revenue_by_month"]
    assert (line.mark, line.encodings["x"].field, line.encodings["x"].type, line.encodings["y"].field) == (
        "line", "orders_created_month", "temporal", "orders_revenue")
    assert line.sql == """SELECT
    (STRFTIME(CAST(orders.created_at AS DATE), '%Y-%m')) AS orders_created_month,
    SUM(orders.amount) AS orders_revenue
FROM shop.orders AS orders
LEFT JOIN shop.users AS users ON orders.user_id = users.id
WHERE (:state IS NULL OR users.state = :state)
  AND (:status IS NULL OR orders.status = :status)
GROUP BY 1
ORDER BY orders_created_month"""
    assert "dashboard filter 'Created' on orders.created_date is not applied (a date filter)" in line.todo
    assert line.source_config["query"]["explore"] == "orders"
    kpi = t["revenue_kpi"]
    assert kpi.kind == "kpi" and "SUM(orders.amount + users.fee)" in kpi.sql and "orders.status IN ('complete', 'shipped')" in kpi.sql
    assert not kpi.todo
    assert "COUNT(DISTINCT users.id) AS users_count" in t["by_state"].sql and t["by_state"].sql.endswith("LIMIT 10")
    # a sum across a one_to_many join: no SQL rather than an inflated number
    fan = t["qty_by_status"]
    assert not fan.sql and "orders.revenue (sum) counts repeated rows because the query joins items one_to_many" in fan.todo
    assert t["state_map"].mark == "unknown" and "looker_map" in t["state_map"].todo and t["state_map"].sql


def test_lookml_dashboard_tiles_match_hand_written_sql(tmp_path: Path):
    """The parity harness: hand-written SQL for what each Looker tile means vs the notebook tile."""
    b = _parse(tmp_path, FILES, "duckdb")
    build.apply(b, tmp_path)
    notebook_source(tmp_path / "notebooks/shop-overview.ipynb")
    url = _warehouse(tmp_path / "wh.duckdb")
    cases = {
        "revenue_by_month": ("SELECT strftime(created_at, '%Y-%m') AS orders_created_month, sum(amount) AS orders_revenue "
                             "FROM shop.orders WHERE status = 'complete' GROUP BY 1", ["orders_created_month"]),
        "revenue_with_fees": ("SELECT sum(o.amount + u.fee) AS orders_revenue_with_fee FROM shop.orders o "
                              "LEFT JOIN shop.users u ON u.id = o.user_id WHERE o.status IN ('complete', 'shipped')", None),
        "orders_by_state": ("SELECT u.state AS users_state, count(DISTINCT o.id) AS orders_count, count(DISTINCT u.id) AS users_count "
                            "FROM shop.orders o LEFT JOIN shop.users u ON u.id = o.user_id GROUP BY 1", ["users_state"]),
    }
    (tmp_path / "migration/parity").mkdir(parents=True)
    for tile, (sql, keys) in cases.items():
        case = {"name": tile, "source": {"sql": sql}, "target": {"notebook": "notebooks/shop-overview.ipynb", "tile": tile}}
        if keys:
            case["keys"] = keys
        (tmp_path / f"migration/parity/{tile}.yaml").write_text(yaml.safe_dump(case))
    results = verify.run_checks(tmp_path, url)
    assert [(r.name, r.status) for r in results] == [(n, "pass") for n in sorted(cases)], [r.note for r in results]


def test_lookml_dashboard_extends_and_api_copies(tmp_path: Path):
    base = """- dashboard: base
  extension: required
  title: Base
  elements:
  - name: kpi
    model: shop
    explore: orders
    type: single_value
    fields: [orders.count]
"""
    child = """- dashboard: child
  extends: [base, missing]
  title: Child
  elements:
  - name: kpi
    title: Orders
"""
    files = {**FILES, "base.dashboard.lookml": base, "child.dashboard.lookml": child}
    b = _parse(tmp_path / "a", files)
    child_d = _by_name(b.dashboards)["Child"]
    assert "Base" not in _by_name(b.dashboards)  # extension: required
    (kpi,) = child_d.tiles
    assert kpi.title == "Orders" and "COUNT(DISTINCT orders.id)" in kpi.sql
    assert any("extends missing" in w.message for w in b.warnings)
    # the same dashboard exported through the API (id model::name) is not parsed twice
    store = RawStore(tmp_path / "a", "looker")
    store.put_json("dashboards/shop::child.json", {"id": "shop::child", "title": "Child", "dashboard_elements": []})
    again = LookerSource().parse(store)
    assert [d.name for d in again.dashboards].count("Child") == 1


def test_notebook_listens_to_dashboard_filters(tmp_path: Path):
    b = _parse(tmp_path, FILES, "duckdb")
    build.apply(b, tmp_path)
    nb = json.loads((tmp_path / "notebooks/shop-overview.ipynb").read_text())
    params = next(c for c in nb["cells"] if "parameters" in c["metadata"].get("tags", []))
    src = "".join(params["source"])
    assert "state = None" in src and "status = 'complete'" in src
    assert "params={'state': state, 'status': status}" in notebook_source(tmp_path / "notebooks/shop-overview.ipynb")


def test_inner_join_keeps_a_cross_view_measure_only_when_nulls_drop_out(tmp_path: Path):
    files = dict(FILES)
    files["shop.model.lkml"] = MODEL.replace("relationship: many_to_one }", "relationship: many_to_one type: inner }", 1)
    files["orders.view.lkml"] = ORDERS.replace(
        "measure: count { type: count }",
        "measure: count { type: count }\n  measure: fee_or_zero { type: sum sql: ${amount} + COALESCE(${users.fee}, 0) ;; }")
    m = _by_name(_parse(tmp_path, files).metrics)
    assert not m["revenue_with_fee"].todo  # amount + fee is NULL without a user, inner or left join alike
    assert "joins type: inner" in m["fee_or_zero"].todo and "dropped" in m["fee_or_zero"].todo
