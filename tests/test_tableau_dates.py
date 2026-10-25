"""Tableau Date Properties: weeks start on the datasource's Week start (or its locale's first day), and fiscal years and
quarters start on its Fiscal year start, in DATETRUNC / DATEPART / DATENAME and in shelf dates.

Hand-written workbooks over the Dunder Mifflin orders (a date for almost every day from 2024-01-01 to 2026-10-22, so every
week, quarter and year boundary), one per Date Properties setting. The generated SQL runs in DuckDB, and the expected
values are computed here from Tableau's documented rules:

- a week starts on the Week start (or the function's start_of_week);
- DATEPART('weekday') is 1 on Sunday to 7 on Saturday, whatever the Week start and start_of_week;
- DATEPART('week') is 1 for the week holding the first day of the (fiscal) year, and counts whole weeks from it;
- a datasource's Fiscal year start changes nothing for a date field on the standard calendar; Tableau applies it only to
  fields set to the fiscal calendar (Default Properties > Fiscal Year Start), which the files don't show. So with a Fiscal
  year start, quarter, year and week-number results stay TODO with the SQL for both: for a fiscal field, quarters start in
  month m and every third month after it, week 1 holds the fiscal year's first day, and the fiscal year is named after the
  calendar year it ends in (with July, 2024-07-01 is in fiscal 2025).
"""

import datetime as dt
import re
from pathlib import Path

import pytest

from unshackle.raw import RawStore
from unshackle.sources import get_source
from unshackle.sources.tableau import date_opts, read_datasource

DATA = Path(__file__).parent / "data" / "dunder_mifflin"
WEEKDAYS = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]
DS = "federated.dates0demo"

CALCS = {
    "Week": "DATETRUNC('week', [purchased_at])",
    "Monday Week": "DATETRUNC('week', [purchased_at], 'monday')",
    "Quarter Start": "DATETRUNC('quarter', [purchased_at])",
    "Year Start": "DATETRUNC('year', [purchased_at])",
    "Week Number": "DATEPART('week', [purchased_at])",
    "Weekday": "DATEPART('weekday', [purchased_at])",
    "Weekday Monday Arg": "DATEPART('weekday', [purchased_at], 'monday')",
    "Monday Week Number": "DATEPART('week', [purchased_at], 'monday')",
    "Quarter Number": "DATEPART('quarter', [purchased_at])",
    "Year Number": "DATEPART('year', [purchased_at])",
    "Weekday Name": "DATENAME('weekday', [purchased_at])",
    "Week Name": "DATENAME('week', [purchased_at])",
    "Quarter Name": "DATENAME('quarter', [purchased_at])",
    "Year Name": "DATENAME('year', [purchased_at])",
    "Cancelled Week": "DATETRUNC('week', [cancelled_at])",
}
SHELF = ["twk", "tqr", "tyr", "wk", "qr", "yr", "wd"]


def _workbook(date_options: str = "", locale: str = "") -> str:
    cols = "\n".join(
        f"""    <column caption="{cap}" datatype="{'date' if 'DATETRUNC' in f else 'string' if 'DATENAME' in f else 'integer'}" name="[Calculation_{i}]" role="dimension" type="ordinal">
      <calculation class="tableau" formula="{f.replace("'", '&apos;')}" />
    </column>"""
        for i, (cap, f) in enumerate(CALCS.items(), 100)
    )
    sheets = "\n".join(
        f"""    <worksheet name="{d}">
      <table>
        <view><datasources><datasource caption="Orders" name="{DS}" /></datasources></view>
        <panes><pane><mark class="Bar" /></pane></panes>
        <rows>[{DS}].[sum:quantity:qk]</rows>
        <cols>[{DS}].[{d}:purchased_at:ok]</cols>
      </table>
    </worksheet>"""
        for d in SHELF
    )
    loc = f" author-locale='{locale}'" if locale else ""
    return f"""<?xml version='1.0' encoding='utf-8' ?>
<workbook version='18.1' xmlns:user='http://www.tableausoftware.com/xml/user'>
  <datasources>
    <datasource caption='Orders' inline='true' name='{DS}' version='18.1'>
      <connection class='federated'>
        <named-connections>
          <named-connection caption='warehouse' name='postgres.dates'>
            <connection class='postgres' dbname='dunder_mifflin' server='warehouse.example.com'{loc} />
          </named-connection>
        </named-connections>
        <relation connection='postgres.dates' name='orders' table='[public].[orders]' type='table' />
        <metadata-records>
          <metadata-record class='column'><remote-name>order_id</remote-name><local-name>[order_id]</local-name><parent-name>[orders]</parent-name><local-type>string</local-type></metadata-record>
          <metadata-record class='column'><remote-name>purchased_at</remote-name><local-name>[purchased_at]</local-name><parent-name>[orders]</parent-name><local-type>date</local-type></metadata-record>
          <metadata-record class='column'><remote-name>cancelled_at</remote-name><local-name>[cancelled_at]</local-name><parent-name>[orders]</parent-name><local-type>date</local-type></metadata-record>
          <metadata-record class='column'><remote-name>quantity</remote-name><local-name>[quantity]</local-name><parent-name>[orders]</parent-name><local-type>integer</local-type></metadata-record>
        </metadata-records>
      </connection>
      <aliases enabled='yes' />
{cols}
      {date_options}
    </datasource>
  </datasources>
  <worksheets>
{sheets}
  </worksheets>
</workbook>"""


VARIANTS = {
    "us": ("", "en_US", "sunday", 1),
    "monday": ("<date-options start-of-week='monday' />", "en_GB", "monday", 1),
    "fiscal_july": ("<date-options fiscal-year-start='july' start-of-week='sunday' />", "", "sunday", 7),
    "wednesday_february": ("<date-options fiscal-year-start='february' start-of-week='wednesday' />", "", "wednesday", 2),
}


def _parse(tmp_path: Path, xml: str):
    store = RawStore(tmp_path, "tableau")
    store.put_text("workbooks/wb-dates.twb", xml)
    return get_source("tableau").parse(store)


@pytest.fixture(scope="module")
def con():
    duckdb = pytest.importorskip("duckdb")
    c = duckdb.connect()
    c.execute("create schema public")
    c.execute(f"create table public.orders as select * from '{DATA / 'orders.parquet'}'")
    return c


# --------------------------------------------------------------- Tableau's rules


def _dow(d: dt.date) -> int:  # 0 = Sunday
    return (d.weekday() + 1) % 7


def week_start(d: dt.date, start: str) -> dt.date:
    return d - dt.timedelta(days=(_dow(d) - WEEKDAYS.index(start)) % 7)


def year_start(d: dt.date, m: int) -> dt.date:
    return dt.date(d.year if d.month >= m else d.year - 1, m, 1)


def quarter_start(d: dt.date, m: int) -> dt.date:
    months = (d.year * 12 + d.month - 1) - ((d.month - m) % 3)
    return dt.date(months // 12, months % 12 + 1, 1)


def expected(d: dt.date, start: str, m: int) -> dict:
    ys = year_start(d, m)
    week_no = (week_start(d, start) - week_start(ys, start)).days // 7 + 1
    fy = ys.year + (1 if m > 1 else 0)
    q = (d.month - m) % 12 // 3 + 1
    return {
        "Week": week_start(d, start), "Monday Week": week_start(d, "monday"), "Quarter Start": quarter_start(d, m),
        "Year Start": ys, "Week Number": week_no, "Weekday": _dow(d) + 1, "Quarter Number": q,
        "Weekday Monday Arg": _dow(d) + 1,
        "Monday Week Number": (week_start(d, "monday") - week_start(ys, "monday")).days // 7 + 1,
        "Year Number": fy, "Weekday Name": WEEKDAYS[_dow(d)].capitalize(), "Week Name": str(week_no), "Quarter Name": str(q),
        "Year Name": str(fy),
    }


def test_rules_on_known_dates():
    # 2024-01-01 is a Monday; 2024-12-29 a Sunday
    assert week_start(dt.date(2024, 1, 1), "sunday") == dt.date(2023, 12, 31)
    e = expected(dt.date(2024, 12, 31), "sunday", 1)
    assert (e["Week Number"], e["Weekday"]) == (53, 3)
    assert expected(dt.date(2024, 12, 29), "monday", 1)["Weekday"] == 1  # Sunday, also with Monday weeks
    e = expected(dt.date(2024, 7, 1), "sunday", 7)
    assert (e["Year Number"], e["Quarter Number"], e["Week Number"], e["Year Start"]) == (2025, 1, 1, dt.date(2024, 7, 1))
    assert expected(dt.date(2024, 6, 30), "sunday", 7)["Year Number"] == 2024
    assert expected(dt.date(2024, 1, 31), "wednesday", 2)["Quarter Start"] == dt.date(2023, 11, 1)


# --------------------------------------------------------------- the translation


def _dims(b) -> dict:
    return {d.label: d for sm in b.semantic_models for d in sm.dimensions}


def _value(v):
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


# results that depend on the Fiscal year start, which Tableau applies only to date fields set to the fiscal calendar
FISCAL = {"Quarter Start", "Year Start", "Week Number", "Monday Week Number", "Quarter Number", "Year Number", "Week Name",
          "Quarter Name", "Year Name"}
FISCAL_SHELF = {"tqr", "tyr", "wk", "qr", "yr"}


def _fiscal_todo(m: int, cap: str) -> set:
    # with a July start fiscal quarters begin with the calendar ones, so DATETRUNC('quarter') / tqr stay translated
    return set() if m == 1 or (m - 1) % 3 == 0 and cap in ("Quarter Start", "tqr") else {cap}


@pytest.mark.parametrize("variant", list(VARIANTS))
def test_calculations_follow_date_properties(tmp_path: Path, con, variant):
    xml, locale, start, m = VARIANTS[variant]
    b = _parse(tmp_path, _workbook(xml, locale))
    dims = _dims(b)
    # day names are in the locale's language: with no locale recorded they stay TODO
    named = {"Weekday Name"} if not locale else set()
    fiscal = {c for cap in FISCAL for c in _fiscal_todo(m, cap)}
    assert {cap for cap in CALCS if dims[cap].todo} == named | fiscal
    assert all("language" in dims[cap].todo for cap in named)
    exprs = {cap: dims[cap].expr for cap in CALCS if cap not in named | fiscal}
    # a fiscal TODO names the SQL for a fiscal field and for a calendar one
    hints = {cap: re.findall(r"`([^`]*)`", dims[cap].todo) for cap in fiscal}
    sql = list(exprs.values()) + [h.replace("[date]", "purchased_at") for f, c in hints.values() for h in (f, c)]
    rows = con.sql(f"select purchased_at, {', '.join(f'{e} as c{i}' for i, e in enumerate(sql))} from public.orders").fetchall()
    assert len(rows) > 1000
    caps = list(exprs) + [(cap, kind) for cap in hints for kind in ("fiscal", "calendar")]
    for row in rows:
        exp, cal = expected(row[0], start, m), expected(row[0], start, 1)
        for cap, v in zip(caps, row[1:]):
            if isinstance(cap, tuple):
                cap, kind = cap
                assert _value(v) == (exp if kind == "fiscal" else cal)[cap], (variant, row[0], cap, kind)
            elif cap in exp:
                # a datasource's Fiscal year start doesn't change a calendar field: these follow the calendar
                assert _value(v) == cal[cap], (variant, row[0], cap, exprs[cap])
    # NULL dates stay NULL
    nulls = con.sql(f"select {exprs['Cancelled Week']} from public.orders where cancelled_at is null").fetchall()
    assert nulls and all(v is None for (v,) in nulls)


@pytest.mark.parametrize("variant", list(VARIANTS))
def test_shelf_dates_follow_date_properties(tmp_path: Path, con, variant):
    xml, locale, start, m = VARIANTS[variant]
    b = _parse(tmp_path, _workbook(xml, locale))
    tiles = {t.title: t for d in b.dashboards for t in d.tiles}
    orders = con.sql("select purchased_at, quantity from public.orders").fetchall()
    key = {
        "twk": lambda d: week_start(d, start), "tqr": lambda d: quarter_start(d, 1), "tyr": lambda d: year_start(d, 1),
        "wk": lambda d: expected(d, start, 1)["Week Number"], "qr": lambda d: expected(d, start, 1)["Quarter Number"],
        "yr": lambda d: expected(d, start, 1)["Year Number"], "wd": lambda d: expected(d, start, 1)["Weekday"],
    }
    fiscal = {c for deriv in FISCAL_SHELF for c in _fiscal_todo(m, deriv)}
    for deriv in SHELF:
        tile = tiles[deriv]
        if deriv in fiscal:
            assert not tile.sql and "Fiscal year start" in str(tile.todo), deriv
            continue
        assert tile.sql and not tile.todo, (deriv, tile.todo)
        got = {_value(k): v for k, v in con.sql(tile.sql).fetchall()}
        exp: dict = {}
        for d, q in orders:
            exp[key[deriv](d)] = exp.get(key[deriv](d), 0) + q
        assert got == exp, (variant, deriv, tile.sql)


def test_unknown_week_start_is_todo(tmp_path: Path):
    """No Week start and no locale: Tableau uses the viewer's locale, so weeks stay TODO (never Monday by default); weekday
    numbers, quarters and years are translated, day names need the locale's language."""
    b = _parse(tmp_path, _workbook())
    dims = _dims(b)
    for cap in ("Week", "Week Number", "Week Name", "Cancelled Week"):
        assert "Week start" in dims[cap].todo and "Sunday" in dims[cap].todo, cap
    assert "language" in dims["Weekday Name"].todo
    for cap in ("Monday Week", "Monday Week Number", "Weekday", "Weekday Monday Arg", "Quarter Start", "Year Start", "Quarter Number", "Year Number", "Quarter Name", "Year Name"):
        assert not dims[cap].todo, cap
    tiles = {t.title: t for d in b.dashboards for t in d.tiles}
    for deriv in ("twk", "wk"):
        assert not tiles[deriv].sql and "Week start" in " ".join(tiles[deriv].todo if isinstance(tiles[deriv].todo, list) else [tiles[deriv].todo])
    for deriv in ("tqr", "tyr", "qr", "yr", "wd"):
        assert tiles[deriv].sql, deriv


def test_date_properties_are_read():
    import xml.etree.ElementTree as ET

    def opts(inner: str, attrs: str = ""):
        el = ET.fromstring(f"<datasource name='x'{attrs}><connection class='postgres' />{inner}</datasource>")
        return date_opts(read_datasource(el, "x", "x"))

    assert opts("") == opts("<date-options />")
    assert (opts("").week_start, opts("").fiscal) == ("", 1)
    assert (opts("<date-options start-of-week='monday' fiscal-year-start='july' />").week_start,
            opts("<date-options start-of-week='monday' fiscal-year-start='july' />").fiscal) == ("monday", 7)
    # a workbook's copy of a published datasource keeps them under <overridable-settings>
    o = opts("<overridable-settings><date-options fiscal-year-start='april' start-of-week='sunday' /><default-calendar-type>Gregorian</default-calendar-type></overridable-settings>")
    assert (o.week_start, o.fiscal, o.calendar) == ("sunday", 4, "")
    # no Week start: the locale's first day of the week; an unknown locale stays unknown
    assert opts("<extract><connection class='hyper' author-locale='en_GB' /></extract>").week_start == "monday"
    assert opts("<extract><connection class='hyper' author-locale='en_US' /></extract>").week_start == "sunday"
    assert opts("<extract><connection class='hyper' author-locale='xx_ZZ' /></extract>").week_start == ""
    # an explicit Week start wins over the locale
    assert opts("<date-options start-of-week='saturday' /><extract><connection author-locale='en_US' /></extract>").week_start == "saturday"
    # the ISO-8601 week-based calendar is not translated
    assert opts("<overridable-settings><default-calendar-type>ISO8601</default-calendar-type></overridable-settings>").calendar


def test_iso_calendar_weeks_are_todo(tmp_path: Path):
    xml = "<overridable-settings><date-options start-of-week='monday' /><default-calendar-type>ISO8601</default-calendar-type></overridable-settings>"
    dims = _dims(_parse(tmp_path, _workbook(xml)))
    assert "ISO8601 calendar" in dims["Week Number"].todo
    assert not dims["Quarter Number"].todo


def test_sigma_and_thoughtspot_weeks_unchanged():
    from unshackle.sources import _aggexpr as ax

    assert ax.ROW_FUNCS["start_of_week"][0] == "date_trunc('week', {0})"
    node = ax.Call("datetrunc", [ax.Str("week"), ax.Ref("d")])
    assert ax.to_sql(node, lambda r: r) == "date_trunc('week', d)"
