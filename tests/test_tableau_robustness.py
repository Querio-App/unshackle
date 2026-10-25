"""Tableau robustness: any workbook parses and builds, and what is skipped or fails is reported where it happened.

A hand-written workbook over the Dunder Mifflin orders and accounts (tests/fixtures/tableau_robustness) has odd parts:
a column, a worksheet and a metadata record missing a required attribute, an unknown element and attribute, an unknown
derivation, empty shelves, unicode captions and sheet names, two calculations with one caption, a 3,000-term formula,
a calculation with no formula, a dashboard zone naming a missing sheet and a zone of an unknown type. Its plain sheets
still run in DuckDB and match the same numbers computed in pandas.

A failure in one workbook, data source, calculation, worksheet or dashboard is injected: it becomes a warning naming
that element, and the rest migrates. Every Tableau fixture is also parsed and built with mutations (attributes and
elements removed, emptied or duplicated, formulas replaced by broken or very long ones, unknown derivations, data
types, roles, elements and attributes added): nothing raises, and what was added is reported where it was.
"""

import copy
import random
import re
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from unshackle import build
from unshackle.raw import RawStore
from unshackle.sources import get_source
from unshackle.sources import tableau as tab

FIXTURES = Path(__file__).parent / "fixtures"
FIX = FIXTURES / "tableau_robustness"
DATA = Path(__file__).parent / "data" / "dunder_mifflin"
WB = "workbooks/e8400000-0000-4000-8000-000000000001.twb"


def _parse(tmp: Path, fixture: Path = FIX):
    shutil.copytree(fixture, tmp / "migration" / "raw" / "tableau")
    return get_source("tableau").parse(RawStore(tmp, "tableau"))


def _tiles(b):
    return {t.title: t for d in b.dashboards for t in d.tiles}


def _board(b, name):
    return next((d for d in b.dashboards if d.name == name), None)


@pytest.fixture(scope="module")
def con():
    duckdb = pytest.importorskip("duckdb")
    c = duckdb.connect()
    c.execute("create schema public")
    for t in ("orders", "accounts"):
        c.execute(f"create table public.{t} as select * from '{DATA / f'{t}.parquet'}'")
    return c


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("robust")
    b = _parse(tmp)
    build.apply(b, tmp)
    return b, tmp


def test_plain_sheets_still_migrate(built, con):
    b, _ = built
    tiles = _tiles(b)
    orders = con.execute(f"select * from '{DATA / 'orders.parquet'}'").df()
    accounts = con.execute(f"select * from '{DATA / 'accounts.parquet'}'").df()

    got = con.execute(tiles["Revenue by Status"].sql).df()
    exp = orders.groupby("status", dropna=False)["post_discount_revenue"].sum().astype(float)
    assert dict(zip(got.iloc[:, 0], got.iloc[:, 1].astype(float))) == pytest.approx(dict(exp))

    # unicode sheet name and caption: a slug and SQL that run
    t = tiles["Stück nach Status ✓"]
    assert re.fullmatch(r"[\w-]+", t.id)
    got = con.execute(t.sql).df().set_index("status")["quantity"]
    assert dict(got) == dict(orders.groupby("status", dropna=False)["quantity"].sum())
    assert any(m.label == "Umsatz ✓ 数据" for m in b.metrics)

    # the second data source: Count Distinct of account ids per payment cycle (NULL ids don't count)
    got = con.execute(tiles["Accounts by Cycle"].sql).df()
    got = dict(zip(got.iloc[:, 0], got.iloc[:, 1]))
    exp = accounts.groupby("payment_cycle", dropna=False)["account_id"].nunique()
    assert got == dict(exp)


def test_odd_parts_are_todos_not_numbers(built):
    b, _ = built
    tiles = _tiles(b)
    assert "derivation 'zzz'" in tiles["Unknown Derivation"].todo
    assert "no fields" in tiles["Empty Shelves"].todo
    # two calculations with one caption: the second is not given the first one's SQL
    dup = tiles["Duplicate Captions"]
    assert "another field with the same name" in dup.todo
    assert "sum(post_discount_revenue) as revenue_check_2" not in (dup.sql or "")
    metrics = {m.name: m for m in b.metrics}
    assert metrics["no_formula"].todo
    assert "frobnicate" in metrics["unknown_function"].todo
    # the long formula is either translated or a TODO that keeps it
    long = metrics["long_sum"]
    assert long.source_expression.count("[Quantity]") == 3000 or not long.todo
    if long.todo:
        assert any(w.item.endswith("field Long Sum") for w in b.warnings)


def test_skipped_xml_is_counted_where_it_is(built):
    b, tmp = built
    skipped = {(s.kind, s.why): s for s in b.skipped}
    where = "Dunder Mifflin Robustness: worksheet Unknown Things"
    assert skipped[("<frobnicate> in <view>", "not read")].where == where
    assert ("<gizmo> in <frobnicate>", "not read") not in skipped  # inside an element already counted
    assert skipped[("<column> attribute wobble", "not read")].where.endswith("datasource Dunder Mifflin Orders")
    assert skipped[("<column> without name", "skipped: a required attribute is missing")].count == 1
    assert ("<worksheet> without name", "skipped: a required attribute is missing") in skipped
    assert ("<metadata-record> without class", "skipped: a required attribute is missing") in skipped
    assert skipped[("<style>", "formatting (not migrated)")].count == 3
    assert not any(s.kind.startswith("<aggregation") for s in b.skipped)  # read at its default value
    report = (tmp / "migration" / "report.md").read_text()
    assert "## Not read from the source" in report
    assert f"| tableau | `<frobnicate> in <view>` | 1 | not read | {where} |" in report
    board = _board(b, "Board")
    assert "zone Ghost Sheet: no worksheet of that name in the workbook" in board.not_migrated
    assert "frobnicator zone" in board.not_migrated


def test_report_counts_todos_by_kind(built):
    _, tmp = built
    report = (tmp / "migration" / "report.md").read_text()
    table = report.split("## Still to translate", 1)[1].split("| File |", 1)[0]
    counts = dict(re.findall(r"^\| ([^|]+?) \| (\d+) \| \d+ \|$", table, re.M))
    todos = build.scan_todos(tmp, build.all_generated(tmp))
    assert sum(map(int, counts.values())) == len(todos)
    assert int(counts["tile without SQL"]) >= 2
    assert build.todo_kind("Tableau table calculation INDEX() depends on the view") == "table calculation"
    assert build.todo_kind("Tableau FIXED LOD not translated") == "level of detail"
    assert build.todo_kind("uses parameter(s) [Churn Rate]") == "parameter"
    assert build.todo_kind("something new") == "other"


def _raise_for(method, test):
    orig = getattr(tab._Parser, method)

    def wrapped(self, *args, **kw):
        if test(*args):
            raise ValueError("injected")
        return orig(self, *args, **kw)
    return wrapped


@pytest.mark.parametrize("method, test, item", [
    ("_tile", lambda wb, ws: ws.get("name") == "Revenue by Status", f"{WB} worksheet Revenue by Status"),
    ("_dashboard", lambda wb, db, dname, *a: dname == "Board", f"{WB} dashboard Board"),
    ("_assign_models", lambda ds: ds.caption == "Dunder Mifflin Accounts", f"{WB} datasource Dunder Mifflin Accounts"),
    ("_translate", lambda ds, f, stack: f.label == "Unknown Function", f"{WB} datasource Dunder Mifflin Orders field Unknown Function"),
    ("_knowledge", lambda wb: True, f"{WB} workbook Dunder Mifflin Robustness"),
    ("_read_wb_datasource", lambda wb, rel, root, el: el.get("name") == "federated.robust1", f"{WB} datasource Dunder Mifflin Accounts"),
    ("_read_workbook", lambda rel, root: True, WB),
])
def test_a_failure_is_isolated_and_reported(tmp_path, monkeypatch, method, test, item):
    monkeypatch.setattr(tab._Parser, method, _raise_for(method, test))
    b = _parse(tmp_path)
    build.apply(b, tmp_path)
    failed = [w for w in b.warnings if "injected" in w.message]
    assert [w.item for w in failed] == [item]
    tiles = _tiles(b)
    if method == "_read_workbook":
        assert not b.dashboards
        return
    assert tiles["Stück nach Status ✓"].sql  # the rest migrates
    if method == "_tile":
        assert "worksheet not parsed" in tiles["Revenue by Status"].todo
        assert tiles["Accounts by Cycle"].sql
    if method == "_dashboard":
        assert _board(b, "Board") is None
        sheets = _board(b, "Dunder Mifflin Robustness sheets")
        assert {"Revenue by Status", "Accounts by Cycle"} <= {t.title for t in sheets.tiles}  # its sheets are not lost
    if method in ("_assign_models", "_read_wb_datasource"):
        assert tiles["Revenue by Status"].sql and not tiles["Accounts by Cycle"].sql
    if method == "_translate":
        assert "unshackle failed" in next(m for m in b.metrics if m.name == "unknown_function").todo
    if method == "_knowledge":
        assert _board(b, "Board") is not None


# ------------------------------------------------------------------ mutated fixtures

FORMULAS = ["", "SUM(", "{FIXED : SUM([x])}", "IF [a] THEN 1", "ZZFUNC([Sales])", "SUM(SUM([Sales]))", "[Nope] + 1",
            "WINDOW_SUM(SUM([Sales]), -1, 1)", "'" + "a" * 5000 + "'", "(" * 400 + "1" + ")" * 400, " + ".join(["[Sales]"] * 2000),
            "{INCLUDE [a] : {EXCLUDE [b] : AVG([c])}}", "RAWSQL_REAL(\"%1\", [Sales])", "DATEPART('zz', [d])", "[数据] / [Ünï]",
            "1/0", "#2024-13-45#", "// only a comment", "SCRIPT_REAL('x', SUM([a]))", "CASE [x] WHEN 1 THEN 'a' END"]
HOT = {"column", "calculation", "datasource", "worksheet", "rows", "cols", "filter", "zone", "connection", "relation",
       "named-connection", "datasource-dependencies", "column-instance", "encodings", "pane", "mark", "table-calc",
       "groupfilter", "dashboard", "zones", "action", "param", "alias", "member", "object", "relationship", "expression",
       "metadata-record", "view", "panes", "color", "text", "story-point", "reference-line", "range", "bin"}


def _mutate(root: ET.Element, rng: random.Random) -> None:
    els = [e for e in root.iter() if tab.txml.local(e.tag) in HOT] or list(root.iter())
    el = rng.choice(els)
    parent = {c: p for p in root.iter() for c in p}.get(el)
    kind = rng.choice(["formula", "deriv", "datatype", "role", "captions", "drop_attr", "drop_el", "empty_el", "unicode",
                       "long", "dup", "bad_attr", "unknown_tag", "text"])
    attrs = list(el.attrib)
    if kind == "formula" and (calcs := list(root.iter("calculation"))):
        rng.choice(calcs).set("formula", rng.choice(FORMULAS))
    elif kind == "deriv" and (shelves := [e for e in root.iter() if e.tag in ("rows", "cols") and e.text]):
        e = rng.choice(shelves)
        e.text = re.sub(r"\]\.\[(\w+):", lambda m: "].[" + rng.choice(["zzz", "", "sum", "tqr", "ctd"]) + ":", e.text, count=1)
    elif kind == "datatype" and (cols := list(root.iter("column"))):
        rng.choice(cols).set("datatype", rng.choice(["", "spatial", "zz", "table", "boolean", "date"]))
    elif kind == "role" and (cols := list(root.iter("column"))):
        c = rng.choice(cols)
        c.set("role", rng.choice(["measure", "dimension", "", "zz"]))
        c.set("type", rng.choice(["quantitative", "nominal", "ordinal", "zz"]))
    elif kind == "captions":
        for c in root.iter("column"):
            c.set("caption", "Same ✓")
    elif kind == "drop_attr" and attrs:
        del el.attrib[rng.choice(attrs)]
    elif kind == "drop_el" and parent is not None:
        parent.remove(el)
    elif kind == "empty_el":
        el[:] = []
        el.text = None
    elif kind == "unicode" and attrs:
        a = rng.choice(attrs)
        el.set(a, el.get(a, "") + " ✓ Ünï 数据 ​'\"")
    elif kind == "long" and attrs:
        a = rng.choice(attrs)
        el.set(a, (el.get(a, "") + " + ") * 300 + "1")
    elif kind == "dup" and parent is not None:
        parent.append(copy.deepcopy(el))
    elif kind == "bad_attr" and attrs:
        el.set(rng.choice(attrs), rng.choice(["", "[", "]]", "x.y.z", "-1", "nan", "(", "[a].[b:c:d:e]"]))
    elif kind == "unknown_tag":
        ET.SubElement(el, "zz-unknown", {"foo": "bar"})
    elif kind == "text":
        el.text = rng.choice(["", "((", "SUM(", "{FIXED : }", "IF THEN END", "[x]]"])


TABLEAU_FIXTURES = sorted(p for p in FIXTURES.iterdir() if p.is_dir() and p.name.startswith("tableau"))


def _sources(fixture: Path) -> list[Path]:
    return sorted(p for p in fixture.rglob("*") if p.suffix in (".twb", ".tds"))


@pytest.mark.parametrize("fixture", TABLEAU_FIXTURES, ids=lambda p: p.name)
def test_every_fixture_survives_mutation(tmp_path, fixture):
    rng = random.Random(84)
    for i, src in enumerate(_sources(fixture)):
        for j in range(6):
            tmp = tmp_path / f"{i}-{j}"
            raw = tmp / "migration" / "raw" / "tableau"
            shutil.copytree(fixture, raw)
            target = raw / src.relative_to(fixture)
            root = ET.parse(target).getroot()
            for _ in range(rng.randint(1, 3)):
                _mutate(root, rng)
            ET.ElementTree(root).write(target, encoding="utf-8", xml_declaration=True)
            b = get_source("tableau").parse(RawStore(tmp, "tableau"))
            if j == 0:
                build.apply(b, tmp)
            for w in b.warnings:  # a failure names the element it happened in
                if "unshackle failed" in w.message:
                    assert w.item.startswith(("workbooks/", "datasources/")), w


@pytest.mark.parametrize("fixture", TABLEAU_FIXTURES, ids=lambda p: p.name)
def test_unknown_xml_is_reported_where_it_is(tmp_path, fixture):
    for i, src in enumerate(_sources(fixture)):
        tmp = tmp_path / str(i)
        raw = tmp / "migration" / "raw" / "tableau"
        shutil.copytree(fixture, raw)
        target = raw / src.relative_to(fixture)
        root = ET.parse(target).getroot()
        ws = root.find("worksheets/worksheet")
        host = ws.find("table") if ws is not None and ws.find("table") is not None else root
        ET.SubElement(host, "zz-unknown")
        col = next(iter(root.iter("column")), None)
        if col is not None:
            col.set("zz-attr", "1")
        ET.ElementTree(root).write(target, encoding="utf-8", xml_declaration=True)
        b = get_source("tableau").parse(RawStore(tmp, "tableau"))
        skipped = {s.kind: s for s in b.skipped}
        hit = skipped[f"<zz-unknown> in <{tab.txml.local(host.tag)}>"]
        assert hit.count == 1
        if host is not root:
            assert hit.where.endswith(f"worksheet {ws.get('name')}")
        if col is not None:
            assert skipped["<column> attribute zz-attr"].count >= 1


def test_unreadable_files_and_metadata(tmp_path):
    raw = tmp_path / "migration" / "raw" / "tableau"
    shutil.copytree(FIXTURES / "tableau", raw)
    (raw / "datasources" / "broken.tds").write_bytes(b"<datasource><column")
    (raw / "workbooks" / "not-a-workbook.twb").write_bytes(b"<foo><bar/></foo>")
    (raw / "views.json").write_text('[{"workbook": "x", "name": 3}, 5]')
    (raw / "workbooks.json").write_text('[{"id": "3f2a9c1e-7b4d-4e8a-9c21-5d6e7f809a1b", "name": 5, "project": "x"}]')
    (raw / "_site.json").write_text("[1, 2]")
    b = get_source("tableau").parse(RawStore(tmp_path, "tableau"))
    build.apply(b, tmp_path)
    warned = {w.item: w.message for w in b.warnings}
    assert "XML did not parse" in warned["datasources/broken.tds"]
    assert "not a Tableau workbook" in warned["workbooks/not-a-workbook.twb"]
    assert any(d.tiles for d in b.dashboards)  # the good workbook still migrates
