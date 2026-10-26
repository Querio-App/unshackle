"""`migrate` flow: one TODO line per reason, tiles with SQL per dashboard, and data that lives only in the tool.

The data tests reuse the Tableau REST mock and packages of test_tableau_extract_data (fixtures/tableau_extract): four
data sources there keep their data only in Tableau (two published ones on Hyper and an extract, and a workbook's Excel
and CSV files).
"""

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner
from conftest import notebook_source
from test_tableau_extract_data import HIST, LINES, WB, _client, frames, packages  # noqa: F401  (fixtures)

from unshackle import build, cli
from unshackle.build import scan_todos
from unshackle.ir import (
    Bundle,
    Dashboard,
    Dimension,
    Entity,
    Permission,
    RowFilter,
    SemanticModel,
    Tile,
    VerifiedQuery,
    join_todos,
    todo_reasons,
)
from unshackle.raw import RawStore
from unshackle.sources.tableau import TableauSource

TWO = join_todos(["table calc RANK() not translated", "tooltip field dropped; add it to the tooltip"])


# ------------------------------------------------------------------ one TODO per reason


def test_join_todos_keeps_reasons_apart():
    assert TWO == "table calc RANK() not translated\n\ntooltip field dropped; add it to the tooltip"
    assert todo_reasons(TWO) == ["table calc RANK() not translated", "tooltip field dropped; add it to the tooltip"]
    # a reason may span lines (SQL), a joined todo adds its own reasons, and repeats and blanks drop out
    sql = "select a,\n  sum(b)\nfrom t"
    joined = join_todos([TWO, "", "totals not added; the automatic form is\n" + sql, "table calc RANK() not translated"])
    assert todo_reasons(joined) == ["table calc RANK() not translated", "tooltip field dropped; add it to the tooltip",
                                    "totals not added; the automatic form is select a, sum(b) from t"]
    assert sql in joined
    assert todo_reasons("") == [] and todo_reasons("one reason") == ["one reason"]


def _bundle() -> Bundle:
    b = Bundle(tool="demo")
    b.semantic_models.append(SemanticModel(
        name="orders", table="analytics.orders", entities=[Entity("order", "primary", "id")],
        dimensions=[Dimension("region"), Dimension("tier", expr="tier_of(x)", todo=join_todos(["function tier_of not translated", "parameter p dropped"]))],
    ))
    b.dashboards.append(Dashboard("Sales", "sales", tiles=[
        Tile("t1", "Revenue", "kpi", "text", sql="select sum(amount) as revenue from analytics.orders"),
        Tile("t2", "Rank", "table", "table", sql="select region from analytics.orders", todo=TWO),
        Tile("t3", "Mix", "chart", "bar", todo=join_todos(["table calc WINDOW_SUM() not translated", "filter on [Region] not applied"])),
        Tile("t4", "Notes", "text", text="Read me"),
    ]))
    b.dashboards.append(Dashboard("Empty", "empty", tiles=[Tile("e1", "Lost", "chart", "bar")]))
    b.queries.append(VerifiedQuery("Top regions", "select 1", todo=join_todos(["parameter :n not bound", "sort not kept"])))
    b.permissions.append(Permission("group:Sales", "view", "Notebook", "sales", todo=join_todos(["user attribute dropped", "group renamed"])))
    b.row_filters.append(RowFilter("mine", "analytics.orders", "region = 'East'", ["Sales"],
                                   todo=join_todos(["USERNAME() not translated", "check the group"])))
    return b


def test_every_reason_is_its_own_todo_line(tmp_path: Path):
    build.apply(_bundle(), tmp_path)
    found = [(rel, msg) for rel, _, msg in scan_todos(tmp_path, build.all_generated(tmp_path))]
    reasons = [msg for _, msg in found]
    assert not [m for m in reasons if "\n" in m]
    for reason in ("table calc RANK() not translated", "tooltip field dropped; add it to the tooltip",
                   "table calc WINDOW_SUM() not translated", "filter on [Region] not applied",
                   "parameter :n not bound", "sort not kept", "user attribute dropped", "group renamed",
                   "USERNAME() not translated", "check the group"):
        assert [m for m in reasons if m.endswith(reason)], reason
    # the model's dimension: one commented-out TODO per reason, the source expression on the last one
    model = next(msg for rel, msg in found if rel.startswith("semantic/") and "function tier_of" in msg)
    assert model == "dimension orders.tier: function tier_of not translated"
    assert any(rel.startswith("semantic/") and msg.startswith("dimension orders.tier: parameter p dropped") and "tier_of(x)" in msg
               for rel, msg in found)
    nb = notebook_source(tmp_path / "notebooks/sales.ipynb")
    assert "# TODO(unshackle): table calc RANK() not translated\n# TODO(unshackle): tooltip field dropped; add it to the tooltip\n" in nb
    rows = yaml.safe_load((tmp_path / "policies/row_filters.demo.yml").read_text())["row_filters"]
    assert rows[0]["todo"] == ["TODO(unshackle): USERNAME() not translated", "TODO(unshackle): check the group"]


# ------------------------------------------------------------------ coverage per dashboard


def test_report_counts_tiles_with_sql_per_dashboard(tmp_path: Path):
    b = _bundle()
    build.apply(b, tmp_path)
    report = (tmp_path / "migration/report.md").read_text()
    assert "## Dashboards (2)" in report and "| Tool | Dashboard | Tiles | With SQL | Top blockers |" in report
    # Sales: three tiles (the text tile doesn't count), two with SQL; each tile counts once per kind of TODO
    assert "| demo | Sales | 3 | 2 | table calculation (2), filter (1), tooltip (1) |" in report
    assert "| demo | Empty | 1 | 0 | tile without SQL (1) |" in report
    assert build.coverage_line(b) == "2 of 4 tiles in 2 dashboards have SQL (per dashboard: migration/report.md)"


def test_build_prints_the_coverage_line(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cli, "get_source", lambda tool: type("S", (), {"parse": lambda self, store: _bundle()})())
    store = RawStore(tmp_path, "tableau")
    store.put_json("workbooks.json", [])
    store.save()
    res = CliRunner().invoke(cli.main, ["build", "tableau", "--repo", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert "2 of 4 tiles in 2 dashboards have SQL" in res.output


# ------------------------------------------------------------------ data that lives only in the tool


@pytest.fixture
def tableau(monkeypatch, packages):  # noqa: F811
    for var, value in (("TABLEAU_SERVER_URL", "https://tableau.example.com"), ("TABLEAU_SITE", "example"),
                       ("TABLEAU_PAT_NAME", "unshackle"), ("TABLEAU_PAT_SECRET", "s3cret")):
        monkeypatch.setenv(var, value)
    seen: list[str] = []
    monkeypatch.setattr(cli, "get_source", lambda tool: TableauSource(client=_client(packages, seen), server_url="https://tableau.example.com",
                                                                      site="example"))
    return seen


def _copied(seen: list[str]) -> list[str]:
    return sorted(s.split()[0] for s in seen if s.endswith("includeExtract=true"))


def test_data_only_counts_from_the_raw_files(tmp_path: Path, tableau):
    src = cli.get_source("tableau")
    store = RawStore(tmp_path, "tableau")
    src.extract(store)
    store.save()
    found = {item["id"]: n for _, item, n in src.data_only(store)}
    assert found == {WB: 2, LINES: 1, HIST: 1}  # the workbook's Excel and CSV, and the two published data sources
    assert not _copied(tableau)


def test_without_a_terminal_migrate_ends_with_the_command(tmp_path: Path, tableau, monkeypatch):
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    res = CliRunner().invoke(cli.main, ["migrate", "tableau", "--repo", str(tmp_path)], input="y\n")
    assert res.exit_code == 0, res.output
    assert "Copy it to Parquet" not in res.output and not _copied(tableau)
    last = res.output.strip().splitlines()[-1]
    assert last == f"4 data sources keep their data only in Tableau; to copy it to Parquet run: unshackle migrate tableau --with-data --repo {tmp_path}"
    assert not (tmp_path / "data/tableau").exists()


def test_in_a_terminal_no_copies_nothing(tmp_path: Path, tableau, monkeypatch):
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    res = CliRunner().invoke(cli.main, ["migrate", "tableau", "--repo", str(tmp_path)], input="n\n")
    assert res.exit_code == 0, res.output
    assert "4 data sources keep their data only in Tableau. Copy it to Parquet in `data/tableau/` (git-ignored)? [y/N]" in res.output
    assert not _copied(tableau) and not (tmp_path / "data/tableau").exists()
    assert "unshackle migrate tableau --with-data" in res.output.strip().splitlines()[-1]


def test_in_a_terminal_yes_copies_like_with_data(tmp_path: Path, tableau, monkeypatch):
    pytest.importorskip("duckdb")
    pytest.importorskip("tableauhyperapi")
    pytest.importorskip("openpyxl")
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    res = CliRunner().invoke(cli.main, ["migrate", "tableau", "--repo", str(tmp_path)], input="y\n")
    assert res.exit_code == 0, res.output
    assert _copied(tableau) == sorted([WB, LINES, HIST])
    store = RawStore(tmp_path, "tableau")
    assert sorted(store.glob("extracts/*.json")) == sorted(f"extracts/{i}.json" for i in (WB, LINES, HIST))
    assert store.get_json("_errors.json") == [] and "extracts/" + WB + ".json" in store.manifest
    assert list((tmp_path / "data/tableau").rglob("*.parquet"))
    assert "--with-data" not in res.output.strip().splitlines()[-1]
    # the build that followed reads the copies, as after `migrate --with-data`
    assert json.loads((tmp_path / "migration/ir/tableau.json").read_text())["queries"]
