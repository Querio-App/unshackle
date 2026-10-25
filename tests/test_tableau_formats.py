"""Workbooks and data sources from older Tableau versions and other packagings (tests/fixtures/tableau_formats/, see its
README): each one is imported like a user's download and parsed, and the result lists the tables, fields and calculations
the file defines. Expected values are read from the XML in the test, not from unshackle."""

import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from unshackle.raw import RawStore
from unshackle.sources import _manual, get_source
from unshackle.sources.tableau import read_datasource

FIX = Path(__file__).parent / "fixtures" / "tableau_formats"
FILES = sorted(p for p in FIX.rglob("*") if p.suffix in (".twb", ".tds", ".twbx", ".tdsx"))


def _import(tmp_path: Path, *paths: Path):
    src = get_source("tableau")
    _manual.run(src, RawStore(tmp_path, "tableau"), list(paths))
    return src.parse(RawStore(tmp_path, "tableau"))


def _xml(path: Path) -> ET.Element:
    from unshackle.sources.tableau import unpack

    return ET.fromstring(unpack(path.read_bytes(), ".tds" if path.suffix.startswith(".tds") else ".twb"))


def _datasources(root: ET.Element) -> list[ET.Element]:
    found = [root] if root.tag == "datasource" else root.findall("datasources/datasource")
    return [d for d in found if d.get("name") != "Parameters"]


def _tables(ds: ET.Element) -> set[str]:
    """The tables a data source reads: <relation type='table'> under its connection (not its extract's)."""
    conn = ds.find("connection")
    if conn is None or conn.get("class") == "sqlproxy":
        return set()
    return {r.get("table", "").rsplit(".", 1)[-1].strip("[]") for r in conn.iter() if r.tag.endswith("relation") and r.get("type") == "table"}


def test_fixtures_cover_old_and_new_formats():
    versions = {_xml(p).get("version") for p in FILES}
    assert {"8.2", "9.3", "10.0", "10.1", "18.1"} <= versions | {d.get("version") for p in FILES for d in _datasources(_xml(p))}
    builds = {(_xml(p).get("source-build") or "")[:6] for p in FILES}
    assert {"2021.1", "2024.2"} <= builds


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_parses_and_lists_what_the_file_defines(tmp_path: Path, path: Path):
    b = _import(tmp_path, path)
    root = _xml(path)
    assert not [w for w in b.warnings if "did not parse" in w.message or "dashboards skipped" in w.message]
    tables = set().union(*(_tables(d) for d in _datasources(root)))
    assert {sm.table.rsplit(".", 1)[-1].strip('"') for sm in b.semantic_models} == tables
    # every column and calculation of each data source is read
    for el in _datasources(root):
        ds = read_datasource(el, "x", path.name)
        records = {(mr.findtext("local-name") or "").strip("[]") for mr in el.iter("metadata-record") if mr.get("class") == "column"}
        columns = {c.get("name", "").strip("[]") for c in el.findall("column")}
        assert records | columns <= set(ds.fields)
    # every calculation becomes a metric, a dimension or a measure, translated or with a TODO, or the report says why not
    labels = {m.label or m.name for m in b.metrics} | {x.label or x.name for sm in b.semantic_models for x in [*sm.dimensions, *sm.measures]}
    labels |= {w.item.rpartition(" field ")[2] for w in b.warnings if "this calculation was skipped" in w.message or "this one was skipped" in w.message}
    for el in _datasources(root):
        for c in el.findall("column"):
            calc = c.find("calculation")
            if calc is None or calc.get("class") != "tableau" or c.get("param-domain-type"):
                continue
            assert (c.get("caption") or c.get("name", "").strip("[]")) in labels, c.get("caption")
    # a data source with nothing to migrate says so
    if not tables and not b.semantic_models:
        assert any("defines no tables" in w.message or "published datasource" in w.message for w in b.warnings)


def test_sheets_of_older_workbooks_become_tiles(tmp_path: Path):
    for path in [p for p in FILES if p.suffix == ".twb"]:
        root = _xml(path)
        sheets = {w.get("name") for w in root.findall("worksheets/worksheet")}
        b = _import(tmp_path / path.stem, path)
        assert sheets <= {t.title for d in b.dashboards for t in d.tiles}, path.name


def test_count_of_a_logical_table(tmp_path: Path):
    """2020.2+ writes a logical table's row count as [__tableau_internal_object_id__]: on a shelf it counts that table's rows,
    in a formula over related tables it stays a TODO instead of being read as a blend."""
    b = _import(tmp_path, FIX / "workbooks" / "v2024.2-csv-dashboard.twb")
    tiles = {t.title: t for d in b.dashboards for t in d.tiles}
    assert "unknown field" not in tiles["Test Cast Performance by Numbers"].todo
    (pct,) = [m for m in b.metrics if m.label == "% Passed"]
    assert "counts the rows of a logical table" in pct.todo and "blending" not in pct.todo


def test_packaged_data_source(tmp_path: Path):
    b = _import(tmp_path, FIX / "packaged" / "v10.0-sqlserver.tdsx")
    assert [sm.table for sm in b.semantic_models] == ["dbo.Batters"]
    assert RawStore(tmp_path, "tableau").exists("datasources/v10.0-sqlserver.tds")


def test_a_folder_of_mixed_versions(tmp_path: Path):
    folder = tmp_path / "downloads"
    shutil.copytree(FIX, folder)
    b = _import(tmp_path / "repo", folder)
    assert len(b.semantic_models) >= 8
