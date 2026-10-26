"""Tableau string members with quotes, % and backslashes, in filters, manual sorts and groups.

Tableau writes a string member as `"..."` with a backslash before each `"`, `%` and `\\` inside it (`50% off` ->
`"50\\% off"`); older files double a quote instead (`""`). The hand-written fixtures' sheets are edited to name one member
holding all of them, the same member is put in the Dunder Mifflin data (in DuckDB), and the expected rows are computed
here from Tableau's rules: Keep Only keeps that member's rows, Exclude keeps every other row (NULL too), a manual sort
puts it first, and a group maps it to the group's name.
"""

import re
import shutil
from pathlib import Path

import pytest

from unshackle.raw import RawStore
from unshackle.sources import get_source
from unshackle.sources.tableau import _member_sql

FIXTURES = Path(__file__).parent / "fixtures"
DATA = Path(__file__).parent / "data" / "dunder_mifflin"
ODD = 'Copy "A4" 50% C:\\x a\\"b'  # a quote, %, a backslash, and a backslash before a quote


def _tableau(v: str) -> str:
    """A string member as Tableau writes it, in an XML attribute or element."""
    return ('"' + re.sub(r'(["%\\])', r"\\\1", v) + '"').replace('"', "&quot;")


def _parse(tmp_path: Path, fixture: str, edit) -> dict:
    raw = tmp_path / "migration" / "raw" / "tableau"
    shutil.copytree(FIXTURES / fixture, raw)
    for p in raw.rglob("*.twb"):
        text = p.read_text()
        p.write_text(edit(text))
        assert p.read_text() != text
    b = get_source("tableau").parse(RawStore(tmp_path, "tableau"))
    return {t.title: t for d in b.dashboards for t in d.tiles}


@pytest.fixture(scope="module")
def con():
    """The Dunder Mifflin data in schema q, with ODD in place of status CANCELLED, payment cycle WEEK and paper Copy."""
    duckdb = pytest.importorskip("duckdb")
    c = duckdb.connect()
    c.execute("create schema q")
    odd = "'" + ODD.replace("'", "''") + "'"
    swap = {"orders": "status", "accounts": "payment_cycle", "products": "paper_type"}
    for t, col in swap.items():
        was = {"orders": "CANCELLED", "accounts": "WEEK", "products": "Copy"}[t]
        c.execute(f"create table q.{t} as select * replace (case when {col} = '{was}' then {odd} else {col} end as {col}) "
                  f"from '{DATA / f'{t}.parquet'}'")
    return c


@pytest.fixture(scope="module")
def rows(con) -> list[dict]:
    rel = con.sql("select o.*, a.payment_cycle, p.paper_type from q.orders o join q.accounts a using (account_id) "
                  "join q.products p using (product_id)")
    return [dict(zip(rel.columns, r)) for r in rel.fetchall()]


def _run(con, tile) -> list[tuple]:
    assert tile.sql and not tile.todo, tile.todo
    return con.sql(tile.sql.replace("public.", "q.")).fetchall()


def _sum(rows: list[dict], key: str, value: str) -> dict:
    out: dict = {}
    for r in rows:
        out[r[key]] = out.get(r[key], 0) + (r[value] or 0)
    return out


def test_member_literals_are_unescaped():
    assert _member_sql(_tableau(ODD).replace("&quot;", '"')) == "'" + ODD + "'"
    assert _member_sql('"50\\% off"') == "'50% off'"
    assert _member_sql('"Copy ""A4"""') == "'Copy \"A4\"'"  # older files double the quote


FILTER = re.compile(r"(<worksheet name=\"excl_two\">.*?<filter class='categorical' column='[^']*'>).*?(</filter>)", re.S)


@pytest.mark.parametrize("keep", [True, False], ids=["keep_only", "exclude"])
def test_filter_on_a_member_with_escapes(tmp_path, con, rows, keep):
    level, member = "[none:status:nk]", _tableau(ODD)
    one = f"<groupfilter function='member' level='{level}' member='{member}' />"
    gf = one if keep else (f"<groupfilter function='except' user:ui-enumeration='exclusive'><groupfilter function='level-members' "
                           f"level='{level}' />{one}</groupfilter>")
    tiles = _parse(tmp_path, "tableau_filters", lambda x: FILTER.sub(lambda m: m.group(1) + gf + m.group(2), x, count=1))
    want = _sum([r for r in rows if (r["status"] == ODD) == keep], "status", "quantity")
    assert (ODD in want) == keep and want
    assert dict(_run(con, tiles["excl_two"])) == pytest.approx(want)


def test_manual_sort_on_a_member_with_escapes(tmp_path, con, rows):
    tiles = _parse(tmp_path, "tableau_display", lambda x: x.replace("<bucket>&quot;WEEK&quot;</bucket>", f"<bucket>{_tableau(ODD)}</bucket>"))
    got = [r[0] for r in _run(con, tiles["Manual"])]
    rest = sorted({"(no cycle)" if r["payment_cycle"] == "" else r["payment_cycle"] for r in rows} - {ODD, "MONTH", "ANNUAL"}, key=str.lower)
    assert got == [ODD, "MONTH", "ANNUAL", *rest]


def test_group_of_a_member_with_escapes(tmp_path, con, rows):
    """The group is named after the member (as Tableau names a one-member group) and also holds Printer."""
    old = "<bin default-name='false' value='\"Office\"'><value>\"Copy\"</value><value>\"Printer\"</value></bin>"
    new = f"<bin default-name='true' value='{_tableau(ODD)}'><value>{_tableau(ODD)}</value><value>\"Printer\"</value></bin>"
    tiles = _parse(tmp_path, "tableau_groups", lambda x: x.replace(f"new-bin='true'>{old}", f"new-bin='true'>{new}"))
    grouped = [{**r, "g": ODD if r["paper_type"] in (ODD, "Printer") else r["paper_type"]} for r in rows]
    want = _sum(grouped, "g", "post_discount_revenue")
    got = {r[0]: r[1] for r in _run(con, tiles["G Paper Keep"])}
    assert set(got) == set(want) and ODD in got
    assert {k: float(v) for k, v in got.items()} == pytest.approx({k: float(v) for k, v in want.items()})
