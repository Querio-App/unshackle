"""Open-source LookML projects (tests/fixtures/looker_real/README.md) through `unshackle import looker` + build."""

import json
import re
from functools import cache
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner
from conftest import notebook_source
from test_ossie import _check

from unshackle.cli import main
from unshackle.ir import Bundle

FIX = Path(__file__).parent / "fixtures" / "looker_real"
PROJECTS = sorted(p.name for p in FIX.iterdir() if p.is_dir())
LOOKML = re.compile(r"\$\{|\{%|\{\{\s*_|@\{")  # field refs, Liquid, constants


@cache
def _build(project: str, root: Path) -> tuple[Path, Bundle]:
    repo = root / project
    for args in (["import", "looker", str(FIX / project)], ["build", "looker"]):
        r = CliRunner().invoke(main, [*args, "--repo", str(repo)])
        assert r.exit_code == 0, r.output
    return repo, Bundle.from_json((repo / "migration/ir/looker.json").read_text())


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    root = tmp_path_factory.mktemp("looker_real")
    return lambda project: _build(project, root)


def _by_name(items):
    return {i.name: i for i in items}


def test_every_project_has_a_license_and_a_readme_row():
    readme = (FIX / "README.md").read_text()
    for project in PROJECTS:
        assert (FIX / project / "LICENSE").exists() and f"`{project}`" in readme


@pytest.mark.parametrize("project", PROJECTS)
def test_import_build_and_validate(project: str, built):
    repo, b = built(project)
    files = json.loads((repo / "migration/raw/looker/_import.json").read_text())["files"]
    assert files and all(f.startswith("lookml/") and f.endswith((".lkml", ".lookml")) for f in files)
    assert b.semantic_models and b.metrics and b.knowledge
    docs = sorted((repo / "semantic").glob("*.ossie.yaml"))
    assert len(docs) == len(b.semantic_models)
    for doc in docs:
        _check(yaml.safe_load(doc.read_text()))
    for nb in (repo / "notebooks").glob("*.ipynb"):
        notebook_source(nb)
    # what was translated carries no LookML syntax; what was not is a TODO with the original
    for sm in b.semantic_models:
        assert sm.table and not LOOKML.search(sm.table)
        assert not any(LOOKML.search(e.expr) for e in sm.entities)
        for d in sm.dimensions:
            assert d.source_expression or not d.todo, d.name
            assert not LOOKML.search(d.expr + d.label), d.name
        assert not any(LOOKML.search(m.expr) for m in sm.measures)
    for m in b.metrics:
        assert m.source_expression if m.todo else not LOOKML.search(f"{m.filter} {m.expr} {m.label}"), m.name
    for r in b.row_filters:
        assert r.table and (r.todo or not LOOKML.search(r.predicate.replace("{{ user.", ""))), r.name
    # each project's LookML dashboard is a notebook; tile SQL is plain SQL, and a tile without SQL says why
    assert len(b.dashboards) == 1 and any((repo / "notebooks").glob("*.ipynb"))
    for t in b.dashboards[0].tiles:
        assert not LOOKML.search(t.sql), t.id
        assert t.sql or t.kind == "text" or t.todo.startswith("no SQL written"), t.id
    assert "## Still to translate" in (repo / "migration/report.md").read_text()


def test_thelook_event(built):
    _, b = built("thelook_event")
    models = _by_name(b.semantic_models)
    # view extends: kitten_order_items { extends: [order_items] } gets the parent's table and fields
    kitten = models["kitten_order_items"]
    assert kitten.table == models["order_items"].table == "looker-private-demo.ecomm.order_items"
    assert {d.name for d in kitten.dimensions} == {d.name for d in models["order_items"].dimensions}
    m = _by_name(b.metrics)
    assert (m["kitten_order_items_return_rate"].numerator, m["kitten_order_items_return_rate"].denominator) == (
        "kitten_order_items_returned_count", "kitten_order_items_count")
    # explore refinements (`explore: +order_items` in queries/) merge into the model's explore
    (k,) = b.knowledge
    assert "+order_items" not in k.body and "## Explore: Order Items (Kittens)" in k.body
    # a Liquid measure stays a TODO with its source
    assert "Liquid" in m["primary_metric"].todo and "{% parameter primary_metric_filter %}" in m["primary_metric"].source_expression
    # string filters with brackets are literals; relative dates are not
    assert m["sessions_count_purchase"].filter == "{{ Dimension('sessions__furthest_funnel_step') }} = '(5) Purchase'"
    assert "28 days" in m["order_items_count_last_28d"].todo
    q = _by_name(b.queries)
    assert "native derived table" in q["order_facts derived table"].todo and "datagroup_trigger" in q["order_facts derived table"].todo
    assert "create_process" in q["ecomm_fit derived table"].todo


def test_cortex_sap(built):
    _, b = built("cortex_sap")
    models = _by_name(b.semantic_models)
    # manifest constants: sql_table_name: `@{GCP_PROJECT_ID}.@{REPORTING_DATASET}.ProfitAndLoss`
    assert models["profit_and_loss"].table == "gcp project id.reporting dataset.ProfitAndLoss"
    # view: +profit_and_loss { extends: [common_fields_finance_ext] } - fields from the extension arrive
    dims = _by_name(models["profit_and_loss"].dimensions)
    assert dims["fiscal_year_period_negative_number"].expr == "-1 * (PARSE_NUMERIC(CONCAT(FiscalYear,FiscalPeriod)))"
    assert "common_fields_finance_ext" not in models  # extension: required
    # @{sign_change_multiplier} expands to Liquid, so the amount stays TODO
    assert "Liquid" in dims["amount_in_local_currency"].todo
    m = _by_name(b.metrics)
    assert m["profit_and_loss_list_companies"].source_expression == "type: list list_field: company_text"
    # explores live in explores/*.explore.lkml and belong to the model that includes them
    (k,) = b.knowledge
    assert k.name == "Looker model cortex_sap_operational_v2" and "## Explore: Income Statement" in k.body


def test_looker_skills(built):
    _, b = built("looker_skills")
    assert {k.name for k in b.knowledge} == {"Looker model enterprise_analytics", "Looker model app"}
    perms = {(p.principal, p.resource_type, p.resource_id) for p in b.permissions}
    assert ("group:hr", "Column", "warehouse.thelook_ecommerce.users.email") in perms  # datasource-qualified by build
    assert len(perms) == len(b.permissions)
    full_name = [p for p in b.permissions if p.resource_id.endswith(".full_name")]
    assert full_name and all("CONCAT(first_name, ' ', last_name)" in p.todo for p in full_name)
    # `count` next to a measure already named `orders_count`
    orders = _by_name(_by_name(b.semantic_models)["orders"].measures)
    assert {"orders_count", "orders_count_2"} <= set(orders)
    # the refinement in tests/orders.test.lkml adds a dimension_group to orders
    assert "test_created" in {d.name for d in _by_name(b.semantic_models)["orders"].dimensions}


def test_financial_services(built):
    _, b = built("financial_services")
    models = _by_name(b.semantic_models)
    m = _by_name(b.metrics)
    assert "sql_distinct_key: ${card.card_id}" in m["total_annual_fee"].source_expression and m["total_annual_fee"].todo
    # primary key built from a timeframe: ${balance_date} is the group's SQL cast to a date
    bal = models["balances_fact"]
    (pk,) = [e for e in bal.entities if e.type == "primary"]
    assert pk.expr == "CONCAT(account_id,cast((CAST((cast(date_field as timestamp)) AS DATE)) as string))"
    dims = _by_name(bal.dimensions)
    assert dims["is_yesterday"].expr == "(CAST((cast(date_field as timestamp)) AS DATE)) = date_add(current_date(), interval -1 day)"
    assert "Liquid" in dims["is_beginning_of_period"].todo
    pct = [x for x in b.metrics if "percentile" in x.source_expression]
    assert pct and all("on BigQuery" in x.todo for x in pct)


def test_retail(built):
    _, b = built("retail")
    rf = _by_name(b.row_filters)
    # access_filter on a field of a derived-table view: the filter targets the view's model table
    assert rf["transactions_stores_name"].table == "stores"
    assert rf["transactions_stores_name_via_transactions"].predicate == (
        "store_id IN (SELECT ID FROM stores WHERE NAME = {{ user.store }})")
    assert "Liquid" in rf["transactions_always_where"].todo and not rf["transactions_always_where"].predicate
    m = _by_name(b.metrics)
    assert m["transactions_number_of_customer_transactions"].filter == "{{ Dimension('transactions__customer_id') }} IS NOT NULL"
    dims = _by_name(_by_name(b.semantic_models)["transactions"].dimensions)
    assert "duration" in dims["since_first_customer_transaction"].todo
    # a SQL comment line before the column (`-- spectacles: ignore`) no longer swallows it
    assert (dims["line_items"].expr, dims["line_items"].todo) == ("", "")  # the column itself
    # nested records joined with LEFT JOIN UNNEST: a view of their own, related to the parent by its key
    models = _by_name(b.semantic_models)
    q = _by_name(b.queries)["transactions__line_items derived table"]
    assert q.sql == ("SELECT transactions.transaction_id AS transactions_key, transactions__line_items.* "
                     "FROM `looker-private-demo.retail.transaction_detail` AS transactions "
                     "LEFT JOIN UNNEST(transactions.line_items) AS transactions__line_items")
    for child, parent in (("transactions__line_items", "transactions"),
                          ("omni_channel_transactions__transaction_details", "omni_channel_transactions")):
        assert (parent, "foreign") in [(e.name, e.type) for e in models[child].entities], child
        assert models[child].measures and not any(x.todo for x in models[child].measures), child


def test_cloud_sql_observability(built):
    repo, b = built("cloud_sql_observability")
    models = _by_name(b.semantic_models)
    # sql_table_name: {% if _dialect._name == ... %} (SELECT ...) {% else %} performance_schema.global_status {% endif %}
    assert models["global_status"].table == "global_status"
    q = _by_name(b.queries)["global_status derived table"]
    assert "sql_table_name that is SQL" in q.todo and q.sql.startswith("{% if")
    assert not any("SELECT" in p.name for p in (repo / "catalog").rglob("*"))
    # one explore file included by three models: one row filter, three model notes
    assert len(b.knowledge) == 3 and [r.name for r in b.row_filters] == ["statement_history_snapshot_always_where"]
    assert "create_process" in _by_name(b.queries)["statement_history_snapshot derived table"].todo
    # -NULL excludes NULLs; only "is not <value>" filters keep them
    m = _by_name(b.metrics)
    assert m["running_threads_count"].filter == "{{ Dimension('threads__processlist_state') }} IS NOT NULL"


# ---------------------------------------------------------------- DuckDB parity on the real projects


def _duckdb(tmp_path, statements: list[str]):
    import duckdb

    path = tmp_path / "wh.duckdb"
    con = duckdb.connect(str(path))
    for s in statements:
        con.execute(s)
    con.close()
    return f"duckdb:///{path}"


def test_looker_skills_dashboard_tiles_match_hand_written_sql(built, tmp_path):
    """The LookML dashboard's tiles, run on a few rows, against SQL written by hand from the LookML."""
    from unshackle import verify
    from unshackle.run import notebook_queries, query

    repo, b = built("looker_skills")
    (d,) = b.dashboards
    assert d.name == "eCommerce Operations Overview"
    params, sqls = notebook_queries(repo / "notebooks/ecommerce-operations-overview.ipynb")
    url = _duckdb(tmp_path, [
        "CREATE TABLE orders (order_id INT, user_id INT, status VARCHAR, created_at TIMESTAMP)",
        "INSERT INTO orders VALUES (1, 1, 'Complete', '2024-01-01'), (2, 1, 'Shipped', '2024-01-02'), "
        "(3, 2, 'Complete', '2024-01-02'), (4, 3, NULL, '2024-01-03')",
        "CREATE TABLE users (id INT, first_name VARCHAR, last_name VARCHAR)",
        "INSERT INTO users VALUES (1, 'Ada', 'Lovelace'), (2, 'Alan', 'Turing'), (3, 'Grace', 'Hopper'), (4, 'No', 'Orders')",
        "CREATE TABLE order_items (id INT, order_id INT, sale_price DOUBLE)",
        "INSERT INTO order_items VALUES (1, 1, 10), (2, 1, 5), (3, 2, 7), (4, 3, 20)",
    ])

    def run(tile: str):
        return query(sqls[tile].replace("bigquery-public-data.thelook_ecommerce.", ""), params, url)

    # users.users_count through the many_to_one join: users who have orders
    assert verify.compare(run("total_users"), query("SELECT count(DISTINCT user_id) AS users_users_count FROM orders", {}, url))[0] == "pass"
    ref = query("SELECT status AS orders_status, count(*) AS orders_orders_count FROM orders WHERE status IS NOT NULL GROUP BY 1", {}, url)
    assert verify.compare(run("order_status_breakdown"), ref, ["orders_status"])[0] == "pass"
    # dimensions only: one row per distinct combination, the order_items join brings each item's price
    ref = query("""SELECT DISTINCT o.order_id AS orders_id, u.first_name || ' ' || u.last_name AS users_full_name,
                   o.status AS orders_status, i.sale_price AS orders_sale_price
                   FROM orders o LEFT JOIN users u ON u.id = o.user_id LEFT JOIN order_items i ON i.order_id = o.order_id""", {}, url)
    got = run("recent_orders_detail")
    assert len(got) == len(ref) == 5
    assert verify.compare(got, ref, ["orders_id", "orders_sale_price"])[0] == "pass"


def test_thelook_gross_margin_metric_matches_looker_sql(built, tmp_path):
    """`sum(${sale_price} - ${inventory_items.cost})`: the Ossie metric over the related datasets equals
    Looker's SQL over the explore's full outer join."""
    import duckdb

    repo, b = built("thelook_event")
    doc = yaml.safe_load((repo / "semantic/order_items.ossie.yaml").read_text())
    expr = {m["name"]: m["expression"]["dialects"][0]["expression"] for m in doc["metrics"]}["order_items_total_gross_margin"]
    assert expr == "COALESCE(SUM((order_items.sale_price - inventory_items.cost)), 0)"
    con = duckdb.connect()
    con.execute("CREATE TABLE order_items AS SELECT * FROM (VALUES (1, 100, 30.0), (2, 101, 12.5), (3, NULL, 9.0)) t(id, inventory_item_id, sale_price)")
    con.execute("CREATE TABLE inventory_items AS SELECT * FROM (VALUES (100, 10.0), (101, 2.5), (102, 4.0)) t(id, cost)")
    got = con.execute(f"SELECT {expr} FROM order_items LEFT JOIN inventory_items ON order_items.inventory_item_id = inventory_items.id").fetchone()[0]
    looker = con.execute("SELECT SUM(order_items.sale_price - inventory_items.cost) FROM order_items "
                         "FULL OUTER JOIN inventory_items ON inventory_items.id = order_items.inventory_item_id").fetchone()[0]
    assert got == looker == 30.0
