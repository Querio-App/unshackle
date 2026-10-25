"""Tableau tile and model SQL per warehouse dialect, and connection settings (Initial SQL).

The warehouse type comes from unshackle.toml ([datasources.warehouse] type), else from the export's connection
classes. Every fixture's SQL parses in every supported dialect. The Redshift, BigQuery and Databricks forms run in
DuckDB (BigQuery and Databricks through sqlglot) and return the same rows as the default form, including NULL join
keys: Tableau groups NULL dimension values as one mark, and a FIXED or EXCLUDE value joins to it (IS NOT DISTINCT FROM).
"""

import re
import shutil
from pathlib import Path

import pytest

from unshackle.raw import RawStore
from unshackle.sources import _dialect as dialect
from unshackle.sources import get_source

FIXTURES = Path(__file__).parent / "fixtures"
DATA = Path(__file__).parent / "data" / "dunder_mifflin"
SQLGLOT = {"postgres": "postgres", "redshift": "redshift", "snowflake": "snowflake", "bigquery": "bigquery", "databricks": "databricks",
           "duckdb": "duckdb"}
NULL_CYCLE = "nullif(payment_cycle, '')"  # accounts with no payment cycle ('' in the data) as NULL
BINDS = {"min_order_value": "500.0", "target_margin": "0.15", "growth_rate": "0.1", "churn_rate": "0.064", "commission_rate": "18.4", "revenue_target": "1500000",
         "measure": "'Quantity'", "group_by": "'Status'", "start_date": "'2025-01-01'", "include_cancelled": "false", "max_days": "14",
         "as_of": "'2026-10-25'", "status": "'COMPLETED'", "since": "'2025-01-01'", "min_quantity": "10"}


def _bind(sql: str) -> str:
    """The parameters' :binds as literals."""
    return re.sub(r"(?<!:):(\w+)\b", lambda m: BINDS.get(m.group(1), "null"), sql)  # dashboard filters: NULL keeps every row


def _parse(tmp_path: Path, fixture: str, warehouse: str = "", edit=None):
    raw = tmp_path / "migration" / "raw" / "tableau"
    shutil.copytree(FIXTURES / fixture, raw)
    if edit:
        for p in raw.rglob("*.t[dw][sb]"):
            p.write_text(edit(p.read_text()))
    if warehouse:
        (tmp_path / "unshackle.toml").write_text(f'[datasources.warehouse]\ntype = "{warehouse}"\n')
    return get_source("tableau").parse(RawStore(tmp_path, "tableau"))


def _sql(b) -> dict[str, str]:
    out = {f"model {sm.name}": sm.sql for sm in b.semantic_models if sm.sql}
    out |= {f"tile {t.title}": t.sql for d in b.dashboards for t in d.tiles if t.sql}
    return out


def _con(null_cycle: bool = False):
    duckdb = pytest.importorskip("duckdb")
    c = duckdb.connect()
    c.execute("create schema public")
    for t in ("orders", "accounts", "products", "shippings"):
        c.execute(f"create table public.{t} as select * from '{DATA / f'{t}.parquet'}'")
    if null_cycle:
        c.execute(f"update public.accounts set payment_cycle = {NULL_CYCLE}")
    return c


def _rows(con, sql: str) -> list[tuple]:
    """Rows sorted on their non-numeric values, numbers as floats."""
    rows = con.sql(_bind(sql)).fetchall()
    rows = [tuple(float(v) if isinstance(v, (int, float)) or type(v).__name__ == "Decimal" else v for v in r) for r in rows]
    return sorted(rows, key=lambda r: repr([v for v in r if not isinstance(v, float)] + [round(v, 3) for v in r if isinstance(v, float)]))


def _same(got: list[tuple], want: list[tuple]) -> bool:
    return len(got) == len(want) and all(
        len(a) == len(b) and all(x == pytest.approx(y, rel=1e-9) if isinstance(y, float) else x == y for x, y in zip(a, b))
        for a, b in zip(got, want))


def test_configured_type_wins_over_the_export(tmp_path: Path):
    exported = _parse(tmp_path / "a", "tableau_lod")
    assert exported.warehouse == "postgres"  # from the connection class
    redshift = _parse(tmp_path / "b", "tableau_lod", "redshift")
    assert redshift.warehouse == "postgres"  # what the export reports is kept; the SQL follows unshackle.toml
    tiles = {t.title: t.sql for d in redshift.dashboards for t in d.tiles}
    assert "avg(cast(account_id_lod.account_revenue as double precision))" in tiles["Average Account Revenue"]
    assert "cast(1.0 as double precision) * base.revenue_share_part_1 / nullif(lod_measure_1_2.value, 0)" in tiles["Revenue Share by Status"]
    assert "is not distinct from" in tiles["Cycle Revenue by Paper Type"]  # Redshift has it, with NULL = NULL
    default = {t.title: t.sql for d in exported.dashboards for t in d.tiles}
    assert "avg(account_id_lod.account_revenue)" in default["Average Account Revenue"]


def test_configured_type_applies_when_the_export_names_none(tmp_path: Path):
    # an extract-only connection (hyper) reports no warehouse type
    b = _parse(tmp_path / "a", "tableau_lod", "redshift", edit=lambda x: x.replace("class='postgres'", "class='hyper'"))
    assert b.warehouse == ""
    assert "avg(cast(" in next(t.sql for d in b.dashboards for t in d.tiles if t.title == "Average Account Revenue")


@pytest.mark.parametrize("warehouse", sorted(SQLGLOT))
@pytest.mark.parametrize("fixture", ["tableau", "tableau_lod", "tableau_published", "tableau_dates", "tableau_params", "tableau_relationships", "tableau_relationship_filters", "tableau_analytics", "tableau_display", "tableau_extract", "tableau_groups", "tableau_dashboards", "tableau_filters", "tableau_cross_db", "tableau_marks", "tableau_tablecalc", "tableau_context", "tableau_reserved", "tableau_lod_nested"])
def test_every_fixture_parses_in_every_dialect(tmp_path: Path, fixture: str, warehouse: str):
    sqlglot = pytest.importorskip("sqlglot")
    b = _parse(tmp_path, fixture, warehouse)
    sqls = _sql(b)
    assert sqls
    for name, sql in sqls.items():
        try:
            sqlglot.parse_one(_bind(sql), read=SQLGLOT[warehouse])
        except sqlglot.errors.ParseError as exc:
            pytest.fail(f"{name} does not parse as {warehouse}: {exc}")
    assert not [t.todo for d in b.dashboards for t in d.tiles if f"{warehouse}:" in (t.todo or "")]


@pytest.mark.parametrize("fixture", ["tableau_lod", "tableau_published", "tableau_dates", "tableau_params", "tableau_relationships", "tableau_relationship_filters", "tableau_analytics", "tableau_display", "tableau_groups", "tableau_dashboards", "tableau_filters", "tableau_cross_db", "tableau_marks", "tableau_tablecalc", "tableau_context", "tableau_reserved", "tableau_lod_nested"])
@pytest.mark.parametrize("null_cycle", [False, True])
def test_dialect_forms_return_the_same_rows(tmp_path: Path, fixture: str, null_cycle: bool):
    sqlglot = pytest.importorskip("sqlglot")
    con = _con(null_cycle)
    base = _sql(_parse(tmp_path / "default", fixture))
    for warehouse in ("redshift", "bigquery", "databricks"):
        forms = _sql(_parse(tmp_path / warehouse, fixture, warehouse))
        assert forms.keys() == base.keys()
        for name, sql in forms.items():
            if warehouse != "redshift":  # DuckDB reads the Redshift form as written
                sql = sqlglot.transpile(_bind(sql), read=warehouse, write="duckdb")[0]
                # their day of week is 1 on Sunday (BigQuery DAYOFWEEK, Databricks DOW); sqlglot keeps the name, and DuckDB's is 0
                sql = re.sub(r"EXTRACT\(DAYOFWEEK FROM ([\w.]+)\)", r"(EXTRACT(DOW FROM \1) + 1)", sql)
            assert _same(_rows(con, sql), _rows(con, base[name])), (warehouse, name)


def test_null_keys_join_like_tableau(tmp_path: Path):
    """NULL payment cycles: one mark, with the FIXED and EXCLUDE values of the NULL group (Tableau's rule)."""
    con = _con(null_cycle=True)
    o, a, p = (con.sql(f"select * from public.{t}").df() for t in ("orders", "accounts", "products"))
    rows = o.merge(a, on="account_id").merge(p, on="product_id")
    rows["payment_cycle"] = rows.payment_cycle.fillna("<null>")
    acct = rows.groupby("account_id").post_discount_revenue.sum()
    per_cycle = rows.groupby("payment_cycle").post_discount_revenue.sum()
    marks = rows[["paper_type", "payment_cycle"]].drop_duplicates()
    domain = rows[["payment_cycle", "account_id"]].drop_duplicates()
    expected = {
        "Cycle Revenue by Paper Type": {(r.paper_type, r.payment_cycle): per_cycle[r.payment_cycle] for r in marks.itertuples()},
        "Account Revenue by Payment Cycle": domain.assign(v=domain.account_id.map(acct)).groupby("payment_cycle").v.mean().to_dict(),
    }
    assert "<null>" in per_cycle.index
    for warehouse in ("", "redshift"):
        b = _parse(tmp_path / (warehouse or "default"), "tableau_lod", warehouse)
        tiles = {t.title: t.sql for d in b.dashboards for t in d.tiles}
        cycle = con.sql(tiles["Cycle Revenue by Paper Type"]).df().fillna({"payment_cycle": "<null>"})
        got = {(r.paper_type, r.payment_cycle): r.cycle_revenue for r in cycle.itertuples()}
        assert got == pytest.approx(expected["Cycle Revenue by Paper Type"], rel=1e-9)
        avg = con.sql(tiles["Account Revenue by Payment Cycle"]).df().fillna({"payment_cycle": "<null>"})
        assert dict(zip(avg.payment_cycle, avg.avg_account_revenue)) == pytest.approx(expected["Account Revenue by Payment Cycle"], rel=1e-9)


def test_reserved_word_names_are_quoted(tmp_path: Path):
    """Fields captioned Group, Order, User, Select, Where and Limit keep those names as columns, written quoted in the SQL:
    plain tiles, a FIXED LOD, table calculations (one dimension, and a padded crosstab), a blend and relationships."""
    con = _con()
    rows = con.sql("select upper(p.paper_type) as g, lower(a.account_manager) as u, o.quantity as q, o.post_discount_revenue as r "
                   "from public.orders o join public.accounts a using (account_id) join public.products p using (product_id)").df()
    accounts = con.sql("select lower(account_manager) as u, account_id from public.accounts").df()
    related = con.sql("select lower(a.account_manager) as u, o.quantity as q from public.orders o join public.accounts a using (account_id)").df()
    per_group = rows.groupby("g").r.sum()
    per_cell = rows.groupby(["u", "g"]).r.sum()
    groups = sorted(rows.g.unique())
    running = {}  # RUNNING_SUM along Group (Table across) per User, over every User x Group mark
    for u in sorted(rows.u.unique()):
        total = 0.0
        for g in groups:
            total += float(per_cell.get((u, g), 0.0))
            running[(u, g)] = total
    expected = {
        "Revenue by Group": {(g,): v for g, v in per_group.items()},
        "Quantity by User and Group": {k: float(v) for k, v in rows.groupby(["u", "g"]).q.sum().items()},
        # MAX of {FIXED [Group] : SUM(revenue)} per user: the largest revenue of the groups the user's orders fall in
        "Order by User": {(u,): per_group[sorted(set(gs))].max() for u, gs in rows.groupby("u").g},
        "Price by Group": {(g,): v for g, v in (per_group / rows.groupby("g").q.sum()).items()},
        "Running Revenue by Group": {(g,): v for g, v in per_group.items()},  # one mark per partition
        "Running Revenue by User and Group": running,
        "Related Quantity by User": {(u,): float(v) for u, v in related.groupby("u").q.sum().items()}
                                    | {(u,): None for u in set(accounts.u) - set(related.u)},
    }
    b = _parse(tmp_path, "tableau_reserved")
    tiles = {t.title: t for d in b.dashboards for t in d.tiles}
    assert not [(t.title, t.todo) for t in tiles.values() if t.todo]
    assert list(tiles["Quantity by User and Group"].source_config["columns"]) == ["user", "group", "quantity"]
    assert list(tiles["Price by Group"].source_config["columns"]) == ["group", "select"]
    assert list(tiles["Quantity and Limit by User"].source_config["columns"]) == ["user", "quantity", "limit"]
    assert 'as "group"' in tiles["Revenue by Group"].sql and 'group_lod."order"' in tiles["Order by User"].sql
    assert 'd."user" is not distinct from m."user"' in tiles["Running Revenue by User and Group"].sql
    assert 'marks."user" is not distinct from measures_orders."user"' in tiles["Related Quantity by User"].sql
    model = next(sm for sm in b.semantic_models if sm.name == "group_lod")
    assert model.sql.startswith('select (upper(products.paper_type)) as "group", sum(orders.post_discount_revenue) as "order"')
    for title, want in expected.items():
        got = {tuple(r[:-1]): None if r[-1] is None else float(r[-1]) for r in con.sql(tiles[title].sql).fetchall()}
        assert got == pytest.approx({k: None if v is None else float(v) for k, v in want.items()}, rel=1e-9), title
    # blend: SUM(quantity) per User of the primary, COUNTD(account_id) of the secondary at the link (all its accounts)
    limit = accounts.groupby("u").account_id.nunique()
    got = {r[0]: (float(r[1]), r[2]) for r in con.sql(tiles["Quantity and Limit by User"].sql).fetchall()}
    assert got == {u: (float(q), limit[u]) for u, q in rows.groupby("u").q.sum().items()}


def test_render_per_dialect():
    sql = ("select \"Order Date\" as d, 'it''s \\ here' as s, date_trunc('week', x) as w, extract(dow from x) as dow, "
           "extract(isodow from x) as iso, extract(week from x) as wk, avg(q) as a, 1.0 * a / nullif(b, 0) as r, median(q) as m from t")
    assert dialect.render(sql, "duckdb") == sql and dialect.render(sql, "") == sql
    assert dialect.render(sql, "snowflake") == sql.replace("\\", "\\\\")  # a backslash escapes in Snowflake and Redshift literals
    assert dialect.render(sql, "postgres") == (sql.replace("median(q)", "percentile_cont(0.5) within group (order by q)")
                                               .replace("1.0 * a", "cast(1.0 as double precision) * a"))
    assert dialect.render(sql, "redshift") == (sql.replace("\\", "\\\\").replace("avg(q)", "avg(cast(q as double precision))")
                                               .replace("1.0 * a", "cast(1.0 as double precision) * a"))
    databricks = dialect.render(sql, "databricks")
    assert "`Order Date` as d" in databricks and r"'it\'s \\ here' as s" in databricks
    assert "(extract(dow from x) - 1) as dow" in databricks and "extract(dow_iso from x) as iso" in databricks
    assert "date_trunc('week', x)" in databricks and "median(q)" in databricks
    with pytest.raises(dialect.Unsupported, match="MEDIAN"):
        dialect.render(sql, "bigquery")
    bigquery = dialect.render(sql.replace(", median(q) as m", ""), "bigquery")
    assert "date_trunc(x, week(monday)) as w" in bigquery and "(extract(dayofweek from x) - 1) as dow" in bigquery
    assert "(mod(extract(dayofweek from x) + 5, 7) + 1) as iso" in bigquery and "extract(isoweek from x) as wk" in bigquery
    assert dialect.render("cast(x as double precision)", "bigquery") == "cast(x as float64)"
    assert dialect.render("cast(x as double precision)", "databricks") == "cast(x as double)"


def test_union_is_distinct_on_bigquery():
    sql = "select a from x\nunion\nselect a from y union all select a from z"
    assert dialect.render(sql, "bigquery") == "select a from x\nunion distinct\nselect a from y union all select a from z"
    assert dialect.render(sql, "postgres") == sql


def test_render_leaves_custom_sql_alone():
    custom = 'SELECT "Region", avg(x) AS a FROM sales WHERE note = \'1.0 * 2\''
    sql = f"with custom_sql as (\n{custom}\n)\nselect \"Region\", avg(a) from custom_sql group by 1"
    out = dialect.render(sql, "redshift", keep=(custom,))
    assert custom in out and out.endswith('select "Region", avg(cast(a as double precision)) from custom_sql group by 1')
    assert dialect.render(sql, "bigquery", keep=(custom,)).endswith("select `Region`, avg(a) from custom_sql group by 1")


def test_extract_dow_matches_postgres_numbering():
    """Postgres numbers days of week 0 (Sunday) to 6, ISO 1 (Monday) to 7; the BigQuery and Databricks forms give the
    same numbers from their own (DAYOFWEEK 1 = Sunday to 7; Databricks DOW 1 = Sunday, DOW_ISO 1 = Monday)."""
    con = _con()
    for day, (dow, isodow) in {"2026-10-25": (0, 7), "2026-10-26": (1, 1), "2026-10-31": (6, 6)}.items():  # Sunday, Monday, Saturday
        one_is_sunday = str(dow + 1)  # BigQuery DAYOFWEEK and Databricks DOW for this day

        def value(warehouse: str, part: str, theirs: str, number: str) -> int:
            return con.sql("select " + dialect.render(f"extract({part} from x)", warehouse).replace(theirs, number)).fetchall()[0][0]

        assert value("bigquery", "dow", "extract(dayofweek from x)", one_is_sunday) == dow, day
        assert value("bigquery", "isodow", "extract(dayofweek from x)", one_is_sunday) == isodow, day
        assert value("databricks", "dow", "extract(dow from x)", one_is_sunday) == dow, day
        assert con.sql(f"select extract(dow from date '{day}'), extract(isodow from date '{day}')").fetchall()[0] == (dow, isodow)


def test_initial_sql_is_reported(tmp_path: Path):
    initial = "SET search_path TO sales; SELECT set_config('app.user', '[TableauServerUser]', false)"
    b = _parse(tmp_path, "tableau_published",
               edit=lambda x: x.replace("class='postgres' dbname=", f"class='postgres' one-time-sql=\"{initial}\" dbname="))
    warns = [w.message for w in b.warnings if "Initial SQL" in w.message]
    assert len(warns) == 1 and initial in warns[0] and "TableauServerUser" in warns[0] and "skills/translate-permissions" in warns[0]
    knowledge = next(k for k in b.knowledge if k.name == "Tableau published datasources")
    assert "Initial SQL" in knowledge.body and initial in knowledge.body
    # without Initial SQL nothing is reported
    plain = _parse(tmp_path / "plain", "tableau_published")
    assert not [w for w in plain.warnings if "Initial SQL" in w.message]


def test_unsupported_warehouse_type_is_reported(tmp_path: Path):
    b = _parse(tmp_path, "tableau_lod", "sqlserver")
    assert any(w.item == "warehouse type" and "'sqlserver'" in w.message for w in b.warnings)
