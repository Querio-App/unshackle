"""Tableau filter semantics: relative dates, excluded values and NULLs, top N, conditions, wildcards, and extract and
data source filters, in Tableau's order of operations.

A hand-written workbook (tests/fixtures/tableau_filters) over the Dunder Mifflin orders joined to accounts and products,
one sheet per filter case, on two embedded data sources: "Orders" (Week start Sunday, no filters) and "Orders (filtered)"
(a data source filter on status and an extract filter on quantity). The tile SQL runs in DuckDB, and the expected rows are
computed here from Tableau's documented rules:

- order of operations: extract filters, data source filters, context filters, then FIXED LODs, top N and conditions,
  then the sheet's dimension filters, then measure filters. Top N and condition filters are computed over the rows the
  context leaves, independently of the sheet's other dimension filters, and over the whole view (not per pane);
- a relative date filter keeps whole periods from first-period to last-period, period 0 being the one holding the anchor
  date (period-anchor, else today: the :as_of parameter); weeks start on the data source's Week start; "to date"
  (include-future='false') stops at the anchor's day; NULL dates are kept only with include-null='true';
- excluding values keeps NULL rows unless NULL is one of the excluded values;
- a top N keeps exactly N members: a tie at the boundary goes to the members that come first in ascending order;
- the nested groupfilters apply from the inside out: a list, then a wildcard, then a condition, then the top N.
"""

import datetime as dt
from collections import defaultdict
from pathlib import Path

import pytest

from unshackle.raw import RawStore
from unshackle.sources import get_source

DATA = Path(__file__).parent / "data" / "dunder_mifflin"
FIXTURE = Path(__file__).parent / "fixtures" / "tableau_filters" / "workbooks" / "f7400000-0000-4000-8000-000000000001.twb"
AS_OF = dt.date(2026, 10, 25)  # "today" for the filters without an anchor (a Sunday)


def _parse(tmp_path: Path, edit=None):
    store = RawStore(tmp_path, "tableau")
    xml = FIXTURE.read_text()
    store.put_text(f"workbooks/{FIXTURE.name}", edit(xml) if edit else xml)
    return get_source("tableau").parse(store)


def _tiles(b) -> dict:
    return {t.title: t for d in b.dashboards for t in d.tiles}


@pytest.fixture(scope="module")
def con():
    duckdb = pytest.importorskip("duckdb")
    c = duckdb.connect()
    c.execute("create schema public")
    for t in ("orders", "accounts", "products"):
        c.execute(f"create table public.{t} as select * from '{DATA / f'{t}.parquet'}'")
    return c


@pytest.fixture(scope="module")
def rows(con) -> list[dict]:
    """The data source's rows: orders inner join accounts and products."""
    rel = con.sql("select o.*, a.account_manager, a.payment_cycle, p.paper_type from public.orders o "
                  "join public.accounts a using (account_id) join public.products p using (product_id)")
    return [dict(zip(rel.columns, r)) for r in rel.fetchall()]


@pytest.fixture(scope="module")
def tiles(tmp_path_factory) -> dict:
    return _tiles(_parse(tmp_path_factory.mktemp("filters")))


def run(con, tile, as_of: dt.date | None = AS_OF) -> set[tuple]:
    """The tile's rows, dimensions first, numbers rounded, days as dates."""
    sql = tile.sql.replace(":as_of", f"'{as_of}'" if as_of else "null")

    def norm(v):
        if type(v).__name__ in ("Decimal", "float", "int"):
            return round(float(v), 2)
        return v.date() if isinstance(v, dt.datetime) else v

    return {tuple(norm(v) for v in r) for r in con.sql(sql).fetchall()}


def by(rows: list[dict], dims, value) -> set[tuple]:
    """Sum of `value(row)` (or a set of values for count distinct) per dims."""
    acc: dict = defaultdict(list)
    for r in rows:
        acc[tuple(d(r) if callable(d) else r[d] for d in dims)].append(value(r))
    out = set()
    for k, vs in acc.items():
        total = len(set(vs)) if isinstance(vs[0], str) else sum(float(v) for v in vs)
        out.add((*k, round(total, 2)))
    return out


QTY = lambda r: r["quantity"]  # noqa: E731
REV = lambda r: r["post_discount_revenue"]  # noqa: E731
ACCOUNTS = lambda r: r["account_id"]  # noqa: E731
ORDERS = lambda r: r["order_id"]  # noqa: E731


def tier(r: dict) -> str | None:
    d = r["discount_percentage"]
    return None if d is None else "High" if d >= 10 else "Low" if d > 0 else None


# ------------------------------------------------------------------ relative dates


def week_start(d: dt.date) -> dt.date:  # Sunday weeks
    return d - dt.timedelta(days=(d.weekday() + 1) % 7)


def period_start(d: dt.date, period: str) -> dt.date:
    if period == "day":
        return d
    if period == "week":
        return week_start(d)
    if period == "month":
        return d.replace(day=1)
    if period == "quarter":
        return dt.date(d.year, (d.month - 1) // 3 * 3 + 1, 1)
    return dt.date(d.year, 1, 1)


def shift(d: dt.date, n: int, period: str) -> dt.date:
    if period in ("day", "week"):
        return d + dt.timedelta(days=n * (7 if period == "week" else 1))
    months = d.year * 12 + d.month - 1 + n * {"month": 1, "quarter": 3, "year": 12}[period]
    return dt.date(months // 12, months % 12 + 1, 1)


def window(anchor: dt.date, period: str, first: int, last: int, to_date: bool):
    start = period_start(anchor, period)
    lo, hi = shift(start, first, period), shift(start, last + 1, period)
    return lambda d: d is not None and lo <= d < hi and (not to_date or d <= anchor)


# sheet -> (date column, anchor (None: today), period, first, last, to date, keep NULL)
RELATIVE = {
    "rel_today_last3m": ("purchased_at", None, "month", -2, 0, False, False),
    "rel_today_last2y": ("purchased_at", None, "year", -1, 0, False, False),
    "rel_today_last30d": ("purchased_at", None, "day", -29, 0, False, False),
    "rel_today_thisweek": ("purchased_at", None, "week", -1, 0, False, False),
    "rel_anchor_last3m": ("purchased_at", "2025-03-15", "month", -2, 0, False, False),
    "rel_anchor_mtd": ("purchased_at", "2025-03-15", "month", 0, 0, True, False),
    "rel_anchor_ytd": ("purchased_at", "2025-03-15", "year", 0, 0, True, False),
    "rel_anchor_qtd": ("purchased_at", "2025-03-15", "quarter", 0, 0, True, False),
    "rel_anchor_prevq": ("purchased_at", "2025-03-15", "quarter", -1, -1, False, False),
    "rel_anchor_next3m": ("purchased_at", "2025-03-15", "month", 1, 3, False, False),
    "rel_anchor_last7d": ("purchased_at", "2025-03-15", "day", -6, 0, False, False),
    "rel_anchor_next2w": ("purchased_at", "2025-03-12", "week", 1, 2, False, False),
    "rel_anchor_wtd": ("purchased_at", "2025-03-12", "week", 0, 0, True, False),
    "rel_anchor_last2w": ("purchased_at", "2025-03-12", "week", -1, 0, False, False),
    "rel_null_excl": ("cancelled_at", "2025-12-31", "day", -364, 0, False, False),
    "rel_null_incl": ("cancelled_at", "2025-12-31", "day", -364, 0, False, True),
}


def test_rules_on_known_dates():
    assert week_start(dt.date(2025, 3, 12)) == dt.date(2025, 3, 9)  # a Wednesday's week starts on Sunday
    keep = window(dt.date(2025, 3, 15), "year", 0, 0, True)  # year to date
    assert keep(dt.date(2025, 1, 1)) and keep(dt.date(2025, 3, 15)) and not keep(dt.date(2025, 3, 16))
    keep = window(dt.date(2025, 3, 12), "week", 1, 2, False)  # the next two weeks
    assert not keep(dt.date(2025, 3, 15)) and keep(dt.date(2025, 3, 16)) and keep(dt.date(2025, 3, 29)) and not keep(dt.date(2025, 3, 30))


@pytest.mark.parametrize("sheet", sorted(RELATIVE))
def test_relative_dates(con, rows, tiles, sheet):
    col, anchor, period, first, last, to_date, keep_null = RELATIVE[sheet]
    keep = window(dt.date.fromisoformat(anchor) if anchor else AS_OF, period, first, last, to_date)
    tile = tiles[sheet]
    assert not tile.todo
    expected = by([r for r in rows if keep(r[col]) or (keep_null and r[col] is None)], [col], QTY)
    assert run(con, tile) == expected
    assert (":as_of" in tile.sql) == (anchor is None)


def test_relative_dates_count_from_the_as_of_parameter(con, rows, tmp_path):
    b = _parse(tmp_path)
    dash = next(d for d in b.dashboards)
    assert [(f.name, f.default, f.kind) for f in dash.filters] == [("as_of", None, "date")]
    tile = _tiles(b)["rel_today_last3m"]
    assert "coalesce(cast(:as_of as date), current_date)" in tile.sql
    keep = window(dt.date(2025, 6, 1), "month", -2, 0, False)
    assert run(con, tile, dt.date(2025, 6, 1)) == by([r for r in rows if keep(r["purchased_at"])], ["purchased_at"], QTY)
    keep = window(dt.date.today(), "month", -2, 0, False)  # unset: the warehouse's current date
    assert run(con, tile, None) == by([r for r in rows if keep(r["purchased_at"])], ["purchased_at"], QTY)


# ------------------------------------------------------------------ categorical filters and NULLs


def test_excluding_values_keeps_nulls(con, rows, tiles):
    year = lambda r: r["cancelled_at"].year if r["cancelled_at"] else None  # noqa: E731
    cases = {
        "excl_high": by([r for r in rows if tier(r) != "High"], [tier], QTY),
        "excl_null": by([r for r in rows if tier(r) is not None], [tier], QTY),
        "incl_low_null": by([r for r in rows if tier(r) in ("Low", None)], [tier], QTY),
        "excl_year": by([r for r in rows if year(r) != 2025], [year], QTY),
        "excl_two": by([r for r in rows if r["status"] not in ("CANCELLED", "RETURNED")], ["status"], QTY),
    }
    for sheet, expected in cases.items():
        assert not tiles[sheet].todo, sheet
        assert run(con, tiles[sheet]) == expected, sheet
    assert any(None in r for r in cases["excl_high"]) and any(None in r for r in cases["excl_year"])


# ------------------------------------------------------------------ top N, conditions, wildcards


def top(rows: list[dict], member, n: int, value, bottom: bool = False) -> set:
    """Tableau's top N: exactly n members; a tie goes to the members that come first in ascending order."""
    totals = {m: v for m, *_, v in by(rows, [member], value)}
    ranked = sorted(totals, key=lambda m: ((totals[m] if bottom else -totals[m]), (m is None, m or "")))
    return set(ranked[:n])


M = "account_manager"
COMPLETED = lambda r: r["status"] == "COMPLETED"  # noqa: E731


def test_top_n(con, rows, tiles):
    completed = [r for r in rows if COMPLETED(r)]
    cases = {  # sheet -> (members kept, rows shown, dimensions, measure)
        "top5_ties": (top(rows, M, 5, ACCOUNTS), rows, [M], ACCOUNTS),
        "top4_ties": (top(rows, M, 4, ACCOUNTS), rows, [M], ACCOUNTS),
        "top9_ties": (top(rows, M, 9, ACCOUNTS), rows, [M], ACCOUNTS),
        "bottom3_ties": (top(rows, M, 3, ACCOUNTS, bottom=True), rows, [M], ACCOUNTS),
        "top3_by_qty": (top(rows, M, 3, QTY), rows, [M], REV),  # ranked by another measure than the one shown
        # computed over all rows, then the sheet's status filter: not the top 4 of the completed orders
        "top4_dimfilter": (top(rows, M, 4, REV), completed, [M], REV),
        "top4_context": (top(completed, M, 4, REV), completed, [M], REV),  # a context filter comes first
        "top2_panes": (top(rows, M, 2, REV), rows, ["paper_type", M], REV),  # the same two in every pane
        "top2_tier_null": (top(rows, tier, 2, ORDERS), rows, [tier], ORDERS),  # NULL is a member
    }
    for sheet, (keep, shown, dims, value) in cases.items():
        member = tier if sheet == "top2_tier_null" else (lambda r: r[M])
        expected = by([r for r in shown if member(r) in keep], dims, value)
        tile = tiles[sheet]
        assert not tile.todo, sheet
        assert run(con, tile) == expected, sheet
    # the data has ties at the boundary: Tableau keeps exactly N, not every tied member
    counts = {m: v for m, v in by(rows, [M], ACCOUNTS)}
    assert sorted(counts.values(), reverse=True)[3:6] == [13, 13, 13] and len(top(rows, M, 5, ACCOUNTS)) == 5
    assert top(rows, M, 4, REV) != top(completed, M, 4, REV)
    assert "row_number() over (order by sum(orders.post_discount_revenue) desc, accounts.account_manager)" in tiles["top4_dimfilter"].sql


def test_conditions_and_wildcards(con, rows, tiles):
    qty = {m: v for m, v in by(rows, [M], QTY)}
    per_account = {m: qty[m] / n for m, n in by(rows, [M], ACCOUNTS)}
    e_under = {m: v for m, v in qty.items() if "e" in m.lower() and v < 30000}
    cases = {  # sheet -> (members kept, rows shown)
        "cond_qty": ({m for m, v in qty.items() if v > 25000}, rows),
        "cond_dimfilter": ({m for m, v in qty.items() if v > 25000}, [r for r in rows if COMPLETED(r)]),  # before the status filter
        "cond_formula": ({m for m, v in per_account.items() if v >= 2000}, rows),
        "wild_lower": ({m for m in qty if "an" in m.lower()}, rows),
        "wild_case": ({m for m in qty if "an" in m}, rows),  # CONTAINS is case-sensitive; the newer form wraps both in LOWER()
        "wild_starts": ({m for m in qty if m.startswith("K")}, rows),
        "wild_ends": ({m for m in qty if m.endswith("n")}, rows),
        "wild_excl": ({m for m in qty if "Scott" not in m}, rows),
        "wild_exact": ({"Pam Beesly"}, rows),
        # contains 'e', then SUM(quantity) < 30000, then the top 2 by quantity
        "cond_top_wild": (set(sorted(e_under, key=lambda m: -e_under[m])[:2]), rows),
    }
    for sheet, (keep, shown) in cases.items():
        tile = tiles[sheet]
        assert not tile.todo, sheet
        assert run(con, tile) == by([r for r in shown if r[M] in keep], [M], QTY), sheet
    assert cases["wild_lower"][0] != cases["wild_case"][0]


# ------------------------------------------------------------------ extract and data source filters


def test_extract_and_data_source_filters_come_first(con, rows, tiles):
    """Every sheet on "Orders (filtered)" sees only rows with status other than CANCELLED (data source filter) and quantity
    >= 2 (extract filter), FIXED LODs and top N included."""
    kept = [r for r in rows if r["status"] != "CANCELLED" and r["quantity"] >= 2]
    revenue = {a: v for a, v in by(kept, ["account_id"], REV)}
    pairs = {(r["payment_cycle"], r["account_id"]) for r in kept}  # FIXED values once per (cycle, account)
    fixed = defaultdict(list)
    for cycle, account in pairs:
        fixed[cycle].append(revenue[account])
    cases = {
        "ds_rev_by_cycle": by(kept, ["payment_cycle"], REV),
        "ds_fixed_by_cycle": {(c, round(sum(v) / len(v), 2)) for c, v in fixed.items()},
        "ds_top3": by([r for r in kept if r[M] in top(kept, M, 3, REV)], [M], REV),
        "ds_status": by(kept, ["status"], QTY),
    }
    for sheet, expected in cases.items():
        tile = tiles[sheet]
        assert not tile.todo, sheet
        assert run(con, tile) == expected, sheet
    assert len(kept) < len(rows) and {r[0] for r in cases["ds_status"]} == {"COMPLETED", "RETURNED"}


def test_data_source_filters_in_the_semantic_layer(tmp_path):
    b = _parse(tmp_path)
    rules = {rf.name: rf for rf in b.row_filters}
    status = rules["orders_filtered_status_filter"]
    assert (status.table, status.predicate) == ("public.orders", "(status <> 'CANCELLED' or status is null)")
    assert status.groups == ["*"] and "data source filter" in status.todo and "SQL model" in status.todo
    extract = rules["orders_filtered_quantity_extract"]
    assert extract.predicate == "quantity >= 2" and "the extract holds only these rows" in extract.todo
    # inner joins: accounts and products rows are visible only through the visible orders
    assert rules["orders_filtered_quantity_extract_accounts"].predicate == (
        "account_id in (select account_id from public.orders where quantity >= 2)")
    lods = {sm.name: sm.sql for sm in b.semantic_models if sm.sql}  # the FIXED grain of each data source
    assert "where" not in lods["account_id_lod"]
    assert "where orders.quantity >= 2 and (orders.status <> 'CANCELLED' or orders.status is null)\ngroup by 1" in lods["account_id_lod_2"]


def test_a_disabled_extract_keeps_every_row(con, rows, tmp_path):
    tiles = _tiles(_parse(tmp_path, lambda x: x.replace("<extract count='-1' enabled='true'", "<extract count='-1' enabled='false'")))
    kept = [r for r in rows if r["status"] != "CANCELLED"]
    assert run(con, tiles["ds_status"]) == by(kept, ["status"], QTY)


# ------------------------------------------------------------------ what stays TODO


def test_unknown_week_start_stays_todo(con, rows, tmp_path):
    tiles = _tiles(_parse(tmp_path, lambda x: x.replace("<date-options start-of-week='sunday' />", "")))
    assert "Week start" in tiles["rel_anchor_next2w"].todo and "relative-date filter on [purchased_at]" in tiles["rel_anchor_next2w"].todo
    assert not tiles["rel_anchor_last3m"].todo  # months don't depend on it


def test_fiscal_year_start_leaves_quarters_and_years_todo(tmp_path):
    tiles = _tiles(_parse(tmp_path, lambda x: x.replace("start-of-week='sunday'", "start-of-week='sunday' fiscal-year-start='february'")))
    assert "Fiscal year start" in tiles["rel_anchor_ytd"].todo and "Fiscal year start" in tiles["rel_anchor_qtd"].todo
    assert not tiles["rel_anchor_mtd"].todo


@pytest.mark.parametrize("edit, sheet, todo", [
    (lambda x: x.replace("period-type-v2='day' />", "period-type-v2='hour' />"), "rel_anchor_last7d", "relative hour periods"),
    (lambda x: x.replace("period-type-v2='week' />", "period-type-v2='iso-week' />"), "rel_anchor_next2w", "relative iso-week periods"),
    (lambda x: x.replace("count='5' end='top' function='end' units='records'", "count='5' end='top' function='end' units='percent'"),
     "top5_ties", "top N filter on [account_manager] not translated (top N by percent)"),
    (lambda x: x.replace("datatype='date' />", "datatype='datetime' />").replace("<local-type>date</local-type>", "<local-type>datetime</local-type>"),
     "rel_anchor_mtd", "a period to date on a date and time field"),
])
def test_untranslated_filters_stay_todo(tmp_path, edit, sheet, todo):
    tile = _tiles(_parse(tmp_path, edit))[sheet]
    assert todo in tile.todo


def test_top_n_count_from_a_parameter(tmp_path):
    param = ("<datasource hasconnection='false' inline='true' name='Parameters' version='18.1'>"
             "<column caption='Top Managers' datatype='integer' name='[Parameter 1]' param-domain-type='range' role='measure' "
             "type='quantitative' value='4'><calculation class='tableau' formula='4' /></column></datasource>\n")
    b = _parse(tmp_path, lambda x: x.replace("  <datasources>\n", "  <datasources>\n" + param, 1)
               .replace("count='4' end='top'", "count='[Parameters].[Parameter 1]' end='top'"))
    tile = _tiles(b)["top4_ties"]
    assert "where filter_rank <= :top_managers" in tile.sql and not tile.todo


def test_a_data_source_filter_that_cant_be_written_leaves_the_sheets_todo(tmp_path):
    """A top N as a data source filter: every sheet on the data source (and its FIXED LODs) would be wrong without it."""
    flt = ("<filter class='categorical' column='[account_manager]'><groupfilter count='3' end='top' function='end' units='records'>"
           "<groupfilter direction='DESC' expression='SUM([quantity])' function='order'><groupfilter function='level-members' "
           "level='[account_manager]' /></groupfilter></groupfilter></filter>\n      <extract ")
    tiles = _tiles(_parse(tmp_path, lambda x: x.replace("<extract ", flt, 1)))
    assert not tiles["ds_status"].sql and "data source filter on [account_manager] not translated" in tiles["ds_status"].todo
    assert not tiles["ds_fixed_by_cycle"].sql
    assert not tiles["excl_two"].todo  # sheets on the other data source are unaffected
