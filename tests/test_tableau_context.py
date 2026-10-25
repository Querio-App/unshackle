"""Tableau context filters with FIXED LODs and top N, data blending, and custom SQL with parameters.

A hand-written workbook (tests/fixtures/tableau_context) on embedded data sources over the Dunder Mifflin tables:
"Orders" (orders joined to accounts and products), "Orders (completed)" (the same with a data source filter on status),
"Accounts" and "Accounts (mapped)" (the accounts table; the second has a custom blend relationship), and "Orders (custom
SQL)", whose custom SQL reads three parameters. The tile SQL runs in DuckDB, and the expected rows are computed here from
Tableau's documented rules:

- context filters come before FIXED LODs and top N: a FIXED value on a sheet with context filters is computed over the
  rows they leave; the sheet's other dimension filters don't change it. Aggregated in the view, a FIXED value counts once
  per distinct combination of the view's and the FIXED dimensions;
- blending: the first data source on a sheet is the primary. Each secondary is queried on its own (with its own data source
  filters, not the primary's), aggregated at the linking fields (the sheet's dimensions linked to it: the workbook's blend
  relationship, else fields with the same name), and left joined to the primary's marks; a NULL link value matches NULL;
- custom SQL: Tableau writes a parameter's value into the query as a literal of its type.
"""

import datetime as dt
import re
import shutil
from collections import defaultdict
from pathlib import Path

import pytest

from unshackle.raw import RawStore
from unshackle.sources import get_source

FIX = Path(__file__).parent / "fixtures" / "tableau_context"
DATA = Path(__file__).parent / "data" / "dunder_mifflin"


def _parse(tmp_path: Path, edit=None):
    raw = tmp_path / "migration" / "raw" / "tableau"
    shutil.copytree(FIX, raw)
    if edit:
        for p in raw.rglob("*.twb"):
            p.write_text(edit(p.read_text()))
    return get_source("tableau").parse(RawStore(tmp_path, "tableau"))


def _tiles(b) -> dict:
    return {t.title: t for d in b.dashboards for t in d.tiles}


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    return _parse(tmp_path_factory.mktemp("context"))


@pytest.fixture(scope="module")
def tiles(bundle) -> dict:
    return _tiles(bundle)


@pytest.fixture(scope="module")
def con():
    duckdb = pytest.importorskip("duckdb")
    c = duckdb.connect()
    c.execute("create schema public")
    for t in ("orders", "accounts", "products"):
        c.execute(f"create table public.{t} as select * from '{DATA / f'{t}.parquet'}'")
    return c


def _dicts(con, sql: str) -> list[dict]:
    rel = con.sql(sql)
    return [dict(zip(rel.columns, r)) for r in rel.fetchall()]


@pytest.fixture(scope="module")
def rows(con) -> list[dict]:
    return _dicts(con, "select o.*, a.account_manager, a.payment_cycle, p.paper_type from public.orders o "
                       "join public.accounts a using (account_id) join public.products p using (product_id)")


@pytest.fixture(scope="module")
def accounts(con) -> list[dict]:
    return _dicts(con, "select * from public.accounts")


def norm(v):
    if type(v).__name__ in ("Decimal", "float", "int"):
        return round(float(v), 4)
    return v.date() if isinstance(v, dt.datetime) else v


def run(con, tile, binds: dict | None = None) -> set[tuple]:
    sql = tile.sql
    for k, v in (binds or {}).items():
        sql = re.sub(rf":{k}\b", v, sql)
    return {tuple(norm(v) for v in r) for r in con.sql(sql).fetchall()}


def tier(r: dict) -> str | None:
    d = r["discount_percentage"]
    return None if d is None else "High" if d >= 10 else "Low" if d > 0 else None


def key(r: dict, dims) -> tuple:
    return tuple(d(r) if callable(d) else r[d] for d in dims)


def agg(rows: list[dict], dims, value, how: str = "sum") -> dict:
    acc: dict = defaultdict(list)
    for r in rows:
        acc[key(r, dims)].append(value(r) if callable(value) else r[value])
    out = {}
    for k, vs in acc.items():
        vs = [v for v in vs if v is not None]
        out[k] = (len(set(vs)) if how == "countd" else min(vs) if how == "min" else sum(float(v) for v in vs) / len(vs) if how == "avg"
                  else sum(float(v) for v in vs))
    return out


def avg_fixed(marks: list[dict], view, fixed_dims, values: dict) -> dict:
    """AVG of a FIXED value in the view: one value per distinct combination of the view's and the FIXED dimensions."""
    domain = {(key(r, view), key(r, fixed_dims)) for r in marks}
    acc: dict = defaultdict(list)
    for v, f in domain:
        acc[v].append(values[f])
    return {k: sum(vs) / len(vs) for k, vs in acc.items()}


def rounded(d: dict) -> set[tuple]:
    return {(*k, *(round(float(x), 4) for x in (v if isinstance(v, tuple) else (v,)))) for k, v in d.items()}


REV = "post_discount_revenue"
ACCT, MGR, CYCLE = ("account_id",), ("account_manager",), ("payment_cycle",)


# ------------------------------------------------------------------ context filters


def test_fixed_lods_follow_the_context(con, rows, tiles):
    completed = [r for r in rows if r["status"] == "COMPLETED"]
    in_context = agg(completed, ACCT, REV)
    everywhere = agg(rows, ACCT, REV)
    cardstock = [r for r in rows if r["paper_type"] == "Cardstock"]
    first = agg(cardstock, ACCT, "purchased_at", "min")
    not_high = [r for r in rows if tier(r) != "High"]
    copy = [r for r in completed if r["paper_type"] == "Copy"]
    top4 = sorted(agg(completed, MGR, REV).items(), key=lambda kv: (-kv[1], kv[0]))[:4]
    top = [r for r in completed if (r["account_manager"],) in dict(top4)]
    paper_qty = agg(completed, ("account_id", "paper_type"), "quantity")
    expected = {
        "ctx_fixed_cycle": avg_fixed(completed, CYCLE, ACCT, in_context),
        "dim_fixed_cycle": avg_fixed(completed, CYCLE, ACCT, everywhere),  # a dimension filter comes after FIXED
        "ctx_first_order": agg(cardstock, (lambda r: first[(r["account_id"],)].year,), "account_id", "countd"),
        "ctx_top_fixed": avg_fixed(top, MGR, ACCT, in_context),
        "ctx_tier_except": avg_fixed(not_high, (tier,), ACCT, agg(not_high, ACCT, REV)),  # excluding High keeps NULL tiers
        "ctx_two_dims": avg_fixed(completed, MGR, ("account_id", "paper_type"), paper_qty),
        "ctx_and_dim": avg_fixed(copy, CYCLE, ACCT, in_context),  # context first, then the paper type filter
    }
    assert expected["ctx_fixed_cycle"] != expected["dim_fixed_cycle"]
    for sheet, want in expected.items():
        assert run(con, tiles[sheet]) == rounded(want), sheet


def test_fixed_grain_with_context_is_the_tiles_own(bundle, tiles):
    sql = tiles["ctx_fixed_cycle"].sql
    assert "account_id_lod as (" in sql and "where orders.status = 'COMPLETED'" in sql.split("account_id_lod as (")[1].split("\n)")[0]
    (lod,) = [sm for sm in bundle.semantic_models if sm.name == "account_id_lod"]
    assert "context filters" in lod.description  # the shared grain stays over all rows
    assert "status" not in tiles["dim_fixed_cycle"].sql.split("account_id_lod as (")[1].split("\n)")[0]


def test_top_n_in_the_context_with_a_fixed_lod_stays_todo(tiles):
    t = tiles["ctx_top_in_context"]
    assert not t.sql and "computes the FIXED LODs over its members" in t.todo


# ------------------------------------------------------------------ blending


def test_blends_aggregate_the_secondary_at_the_links(con, rows, accounts, tiles):
    completed = [r for r in rows if r["status"] == "COMPLETED"]
    year = lambda r: r["purchased_at"].year  # noqa: E731

    def blend(primary: dict, secondary: dict, link) -> dict:
        return {k: (*(v if isinstance(v, tuple) else (v,)), *(secondary.get(link(k), (None,)))) for k, v in primary.items()}

    def tup(*ds: dict) -> dict:
        return {k: tuple(d.get(k) for d in ds) for k in ds[0]}

    accts_per_mgr = {k: (v,) for k, v in agg(accounts, MGR, "account_id", "countd").items()}
    marks_mgr_paper = {k: () for k in agg(rows, ("account_manager", "paper_type"), "quantity")}
    marks_mgr_cycle = {k: () for k in agg(rows, ("account_manager", "payment_cycle"), "quantity")}
    expected = {
        "blend_mgr": blend(agg(rows, MGR, REV), accts_per_mgr, lambda k: k),
        "blend_mgr_paper": blend(marks_mgr_paper, accts_per_mgr, lambda k: k[:1]),  # repeated on every paper type
        # the secondary's data source filter (completed only) applies to it alone; the NULL tier links to NULL
        "blend_tier_null": blend(agg(rows, (tier,), "quantity"),
                                 tup(agg(completed, (tier,), "quantity", "avg"), agg(completed, (tier,), "order_id", "countd")), lambda k: k),
        # the primary's filter doesn't reach the secondary
        "blend_primary_filter": blend(agg(completed, CYCLE, REV), {k: (v,) for k, v in agg(accounts, CYCLE, "account_id", "countd").items()},
                                      lambda k: k),
        "blend_year": blend(agg(rows, (year,), REV), {k: (v,) for k, v in agg(completed, (year,), "order_id", "countd").items()}, lambda k: k),
        # the workbook's relationship maps account_manager to the secondary's Manager; payment_cycle, though in both, doesn't link
        "blend_mapped": blend(marks_mgr_cycle, accts_per_mgr, lambda k: k[:1]),
    }
    assert any(k == (None,) for k in expected["blend_tier_null"])
    for sheet, want in expected.items():
        assert run(con, tiles[sheet]) == {(*k, *(None if x is None else round(float(x), 4) for x in v)) for k, v in want.items()}, sheet


def test_blend_sql_joins_one_row_per_link(tiles):
    sql = tiles["blend_mgr_paper"].sql
    assert "blend_1 as (" in sql and "group by 1" in sql.split("blend_1 as (")[1].split("\n)")[0]
    assert "is not distinct from blend_1.blend_key_1" in sql and "max(blend_1.blend_value_1)" in sql


def test_a_secondary_dimension_stays_todo(tiles):
    t = tiles["blend_secondary_dim"]
    assert not t.sql and "a dimension of the secondary data source Accounts" in t.todo and "*" in t.todo


@pytest.mark.parametrize("edit,sheet,todo", [
    # a filter on a field of the secondary
    (lambda x: x.replace("<datasource-dependencies datasource='federated.ctx0accounts'><column datatype='string' name='[account_id]'",
                         "<filter class='categorical' column='[federated.ctx0accounts].[none:payment_cycle:nk]'><groupfilter function='member' "
                         "level='[none:payment_cycle:nk]' member='&quot;WEEK&quot;' /></filter>"
                         "<datasource-dependencies datasource='federated.ctx0accounts'><column datatype='string' name='[account_id]'", 1),
     "blend_mgr", "a filter on a field of a secondary data source"),
    # the relationship maps a field the sheet doesn't show: no active link
    (lambda x: x.replace("<map key='[federated.ctx0orders].[none:account_manager:nk]'", "<map key='[federated.ctx0orders].[none:paper_type:nk]'"),
     "blend_mapped", "no linking field among the sheet's dimensions"),
])
def test_blends_that_stay_todo(tmp_path, edit, sheet, todo):
    t = _tiles(_parse(tmp_path, edit))[sheet]
    assert not t.sql and todo in t.todo


# ------------------------------------------------------------------ custom SQL with parameters


@pytest.mark.parametrize("values", [
    {"status": "'COMPLETED'", "since": "'2025-01-01'", "min_quantity": "10"},
    {"status": "'RETURNED'", "since": "'2025-06-01'", "min_quantity": "5"},
    {"status": "'CANCELLED'", "since": "'2024-01-01'", "min_quantity": "50"},
])
def test_custom_sql_parameters_are_binds(con, rows, tiles, values):
    t = tiles["csql_by_paper"]
    assert "o.status = :status" in t.sql and "o.purchased_at >= cast(:since as date)" in t.sql and "o.quantity >= :min_quantity" in t.sql
    assert "<[Parameters]" not in t.sql and "parameter" not in (t.todo or "")
    status, since, minimum = values["status"].strip("'"), dt.date.fromisoformat(values["since"].strip("'")), int(values["min_quantity"])
    kept = [r for r in rows if r["status"] == status and r["purchased_at"] >= since and r["quantity"] >= minimum]
    assert run(con, t, values) == rounded(agg(kept, ("paper_type",), "quantity"))


def test_custom_sql_parameters_in_the_parameters_cell(bundle):
    defaults = {f.name: f.default for d in bundle.dashboards for f in d.filters}
    assert {k: defaults.get(k) for k in ("status", "since", "min_quantity")} == {"status": "COMPLETED", "since": "2025-01-01", "min_quantity": 10}


def test_a_quoted_parameter_in_custom_sql_stays_todo(tmp_path):
    t = _tiles(_parse(tmp_path, lambda x: x.replace("o.status = &lt;[Parameters].[Parameter 1]&gt;", "o.status = '&lt;[Parameters].[Parameter 1]&gt;'")))["csql_by_paper"]
    assert "'<[Parameters].[Parameter 1]>'" in t.sql and "custom SQL uses Tableau parameters" in t.todo
    assert ":since" in t.sql  # the others are bound
