"""Offline tests on a real Tableau Cloud export of LOD sheets (tests/fixtures/tableau_live_lod/, see its README).

The view CSVs are Tableau's own numbers for FIXED, INCLUDE and EXCLUDE sheets. Tiles with SQL must match them
on the same Dunder Mifflin data in DuckDB; sheets with no faithful translation must stay TODO. The last test pins
how Tableau weighs aggregated FIXED values, from its numbers, for the sheets that are still TODO.
"""

import shutil
from pathlib import Path

import pandas as pd
import pytest

from unshackle import build
from unshackle.raw import RawStore
from unshackle.sources import get_source
from unshackle.verify import compare

FIX = Path(__file__).parent / "fixtures" / "tableau_live_lod"
DATA = Path(__file__).parent / "live" / "data" / "dunder_mifflin"
VIEW = {  # sheet -> (view CSV saved by `unshackle extract`, the sheet's dimensions)
    "Revenue by Account Tier": ("00000000-0000-4000-8000-000000000004", ["account_tier"]),
    "Average Account Revenue": ("00000000-0000-4000-8000-000000000006", []),
    "Account Revenue by Payment Cycle": ("00000000-0000-4000-8000-000000000007", ["payment_cycle"]),
    "Order Size by Paper Type": ("00000000-0000-4000-8000-000000000008", ["paper_type"]),
    "Cycle Revenue by Paper Type": ("00000000-0000-4000-8000-000000000009", ["paper_type", "payment_cycle"]),
    "New Accounts by Month": ("00000000-0000-4000-8000-000000000010", ["month_of_first_order_date"]),
    "Top Account per Paper Type": ("00000000-0000-4000-8000-000000000011", ["paper_type"]),
    "Share of Account Revenue by Status": ("00000000-0000-4000-8000-000000000012", ["status"]),
    "Account Revenue by Payment Cycle (Completed)": ("00000000-0000-4000-8000-000000000016", ["payment_cycle"]),
}
TODO = {  # sheet -> view CSV: Tableau's numbers for what is not translated yet
    "Revenue Share by Status": "00000000-0000-4000-8000-000000000013",
    "Large Account Orders by Payment Cycle": "00000000-0000-4000-8000-000000000014",
    "Cycle Avg Account Revenue": "00000000-0000-4000-8000-000000000015",
}


@pytest.fixture
def bundle(tmp_path: Path):
    shutil.copytree(FIX, tmp_path / "migration" / "raw" / "tableau")
    return get_source("tableau").parse(RawStore(tmp_path, "tableau"))


@pytest.fixture(scope="module")
def warehouse(tmp_path_factory):
    duckdb = pytest.importorskip("duckdb")
    db = tmp_path_factory.mktemp("wh") / "wh.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create schema public")
    for t in ("orders", "accounts", "products"):
        con.execute(f"create table public.{t} as select * from '{DATA / f'{t}.parquet'}'")
    con.close()
    return db


def _tiles(b):
    (d,) = b.dashboards
    return {t.title: t for t in d.tiles if t.kind != "text"}


def _view(name: str) -> pd.DataFrame:
    view = VIEW[name][0] if name in VIEW else TODO[name]
    return pd.read_csv(FIX / "views" / f"{view}.csv", dtype=str, keep_default_na=False)


def test_translated_sheets_match_tableau(tmp_path: Path, bundle, warehouse):
    from unshackle.run import notebook_queries, query

    build.apply(bundle, tmp_path)
    params, sqls = notebook_queries(tmp_path / "notebooks/lod-checks.ipynb")
    tiles = _tiles(bundle)
    assert set(tiles) == set(VIEW) | set(TODO)
    for name, (view, keys) in VIEW.items():
        source = pd.read_csv(FIX / "views" / f"{view}.csv")
        target = query(sqls[tiles[name].id.replace("-", "_")], params, url=f"duckdb:///{warehouse}")
        status, gap, note = compare(source, target, keys=keys)
        assert status == "pass", (name, note)


def test_untranslated_sheets_stay_todo(bundle):
    tiles = _tiles(bundle)
    for name in TODO:
        assert not tiles[name].sql and tiles[name].todo, name
    assert "inside a larger expression" in tiles["Revenue Share by Status"].todo
    assert "nested LOD" in tiles["Cycle Avg Account Revenue"].todo


def test_tableau_weighs_aggregated_fixed_values_once_per_lod_group(warehouse):
    """Tableau's numbers for the TODO sheets: an aggregate over FIXED [account_id] values counts each account once per
    mark (distinct view dimensions x account_id), not once per row; that holds for a row calc over the FIXED value
    (SUM(IF [Account Revenue] >= 1000000 THEN 1 ELSE 0 END)), inside a ratio (SUM([Revenue]) / SUM([Account Revenue]))
    and for a FIXED nested in another FIXED. Per row would give 914 instead of 43 large-account rows in ANNUAL."""
    import duckdb

    con = duckdb.connect(str(warehouse), read_only=True)
    con.execute("""create temp view r as select o.*, a.payment_cycle from public.orders o
                   join public.accounts a using (account_id) join public.products p using (product_id)""")
    con.execute("create temp view acct as select account_id, sum(post_discount_revenue) as v from r group by 1")

    def per_group(dim: str, agg: str, num: str = "") -> dict:
        sql = (f"select {dim}, {agg} from (select distinct {dim}, account_id from r) join acct using (account_id) group by 1"
               if not num else
               f"select {dim}, {num} / (select sum(v) from (select distinct {dim} as d, account_id from r) g join acct using (account_id) "
               f"where g.d = r.{dim}) from r group by 1")
        return {k or "": float(v) for k, v in con.execute(sql).fetchall()}

    def tableau(name: str) -> dict:
        df = _view(name)
        return {k: float(v.replace(",", "")) for k, v in zip(df.iloc[:, 0], df.iloc[:, 1])}

    expected = {
        "Large Account Orders by Payment Cycle": per_group("payment_cycle", "sum(case when v >= 1000000 then 1 else 0 end)"),
        "Cycle Avg Account Revenue": per_group("payment_cycle", "avg(v)"),
        "Revenue Share by Status": per_group("status", "", num="sum(post_discount_revenue)"),
    }
    for name, exp in expected.items():
        got = tableau(name)
        assert got.keys() == exp.keys(), name
        assert [got[k] for k in exp] == pytest.approx([exp[k] for k in exp], rel=1e-6), name
