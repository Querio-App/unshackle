"""Regression tests for bugs found in review before the first release."""

from conftest import todo_for
import json
import runpy
import shutil
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from unshackle import build
from unshackle.cli import main
from unshackle.ir import Bundle, Column, Dashboard, DashboardFilter, Encoding, Table, Tile, VerifiedQuery
from unshackle.raw import RawStore
from unshackle.run import run_notebook
from unshackle.sources import get_source
from unshackle.target.notebooks import render_dashboard
from unshackle.target.writer import Target, UnsafePath

FIXTURES = Path(__file__).parent / "fixtures"


def _seed(repo: Path, tool: str) -> RawStore:
    shutil.copytree(FIXTURES / tool, repo / "migration" / "raw" / tool)
    store = RawStore(repo, tool)
    store.rescan()
    store.save()
    return store


def test_failed_extract_never_prunes_earlier_raw_files(tmp_path: Path):
    store = RawStore(tmp_path, "demo")
    store.put_text("workbooks/a.twb", "<a/>")
    store.put_text("workbooks/b.twb", "<b/>")
    store.save()
    store = RawStore(tmp_path, "demo")
    store.begin()
    store.put_text("workbooks/a.twb", "<a/>")
    store.put_json("_errors.json", [{"path": "/workbooks", "error": "503"}])
    store.save()
    assert (store.root / "workbooks/b.twb").exists()
    assert "workbooks/b.twb" in store.manifest


def test_successful_extract_prunes_deleted_items(tmp_path: Path):
    store = RawStore(tmp_path, "demo")
    store.put_text("workbooks/a.twb", "<a/>")
    store.put_text("workbooks/b.twb", "<b/>")
    store.save()
    store = RawStore(tmp_path, "demo")
    store.begin()
    store.put_text("workbooks/a.twb", "<a/>")
    store.save()
    assert not (store.root / "workbooks/b.twb").exists()


def test_path_traversal_is_contained(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    b = Bundle(tool="demo")
    b.tables.append(Table("warehouse", "../../..", "../../pwned", columns=[Column("x")]))
    build.apply(b, repo)
    assert not (tmp_path / "pwned.yml").exists()
    assert not list(tmp_path.glob("*.yml"))
    with pytest.raises(UnsafePath):
        Target(repo).write("../escape.txt", "x")


def test_raw_store_rejects_paths_outside_its_root(tmp_path: Path):
    store = RawStore(tmp_path, "demo")
    with pytest.raises(ValueError, match="outside"):
        store.put_text("../../../escape.txt", "bad")
    with pytest.raises(ValueError, match="outside"):
        store.put_text(str(tmp_path / "absolute.txt"), "bad")
    store.root.mkdir(parents=True)
    (store.root / "link").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="outside"):
        store.put_text("link/escape.txt", "bad")
    assert not (tmp_path / "escape.txt").exists()

    repo = tmp_path / "repo"
    redirected = RawStore(repo, "demo")
    (repo / "elsewhere").mkdir(parents=True)
    redirected.root.parent.mkdir(parents=True)
    redirected.root.symlink_to(repo / "elsewhere", target_is_directory=True)
    with pytest.raises(ValueError, match="redirects"):
        redirected.put_text("escape.txt", "bad")


def _run_cells_offline(d: Dashboard) -> dict:
    """Run every code cell but the setup one, with read_sql stubbed out."""
    import types

    import altair as alt
    import pandas as pd

    nb = json.loads(render_dashboard(d, "demo"))
    fake_pd = types.SimpleNamespace(read_sql=lambda *a, **k: pd.DataFrame({"x": [1]}), DataFrame=pd.DataFrame)
    ns = {"alt": alt, "pd": fake_pd, "text": str, "engine": None}
    for cell in nb["cells"]:
        if cell["cell_type"] == "code" and cell["id"] != "setup":
            exec("".join(cell["source"]), ns)
    return ns


def test_source_metadata_cannot_inject_generated_notebook_code():
    d = Dashboard(
        "Sales\nraise RuntimeError('dashboard injected')",
        "sales",
        tiles=[Tile("t", "Chart\nraise RuntimeError('title injected')", mark="line", sql="select 1")],
        filters=[DashboardFilter("x\nraise RuntimeError('filter injected')", default="a\nraise RuntimeError('default injected')")],
    )
    _run_cells_offline(d)


def test_source_todo_cannot_inject_generated_notebook_code():
    d = Dashboard("Sales", "sales", tiles=[Tile("t", "Chart", mark="line", sql="select 1", todo="unfinished\n    raise RuntimeError('tile injected')")])
    assert "chart_chart" in _run_cells_offline(d)


def test_source_metadata_cannot_inject_python_query_code(tmp_path: Path):
    q = VerifiedQuery(
        "Report\nraise RuntimeError('name injected')",
        "pass",
        language="python",
        todo="unfinished\nraise RuntimeError('todo injected')",
    )
    sql = VerifiedQuery("SQL", "select 1", todo="unfinished */ DROP TABLE x")
    build.apply(Bundle(tool="demo", queries=[q, sql]), tmp_path)
    runpy.run_path(str(next((tmp_path / "queries").glob("*.py"))))
    assert (tmp_path / "queries/sql.sql").read_text().count("*/") == 1


def test_preview_escapes_html_and_embedded_chart_data(tmp_path: Path):
    import duckdb

    db = tmp_path / "wh.duckdb"
    duckdb.connect(str(db)).close()
    name = "<img src=x onerror=alert(1)>"
    d = Dashboard(name, "preview", tiles=[
        Tile("c", name, mark="bar", sql="select '</script><script>alert(1)</script>' as label, 1 as v",
             encodings={"x": Encoding("label"), "y": Encoding("v", "quantitative")}),
        Tile("t", "", kind="text", text="<script>alert(2)</script>"),
    ])
    path = tmp_path / "preview.ipynb"
    path.write_text(render_dashboard(d, "demo"))
    html = run_notebook(path, url=f"duckdb:///{db}").read_text()
    assert "<h2>&lt;img src=x onerror=alert(1)&gt;</h2>" in html and "vegaEmbed(" in html
    assert "</script><script>alert(1)</script>" not in html and "<script>alert(2)" not in html


def test_second_tool_never_overwrites_first(tmp_path: Path):
    a = Bundle(tool="alpha", dashboards=[Dashboard("Sales overview", "sales-overview", tiles=[Tile("t", "T", sql="select 1")])])
    b = Bundle(tool="beta", dashboards=[Dashboard("Sales overview", "sales-overview", tiles=[Tile("t", "T", sql="select 2")])])
    build.apply(a, tmp_path)
    first = (tmp_path / "notebooks/sales-overview.ipynb").read_text()
    result = build.apply(b, tmp_path)
    assert (tmp_path / "notebooks/sales-overview.ipynb").read_text() == first
    assert "select 2" in (tmp_path / "notebooks/sales-overview-beta.ipynb").read_text()
    assert "notebooks/sales-overview-beta.ipynb" in result.writes.written


def test_same_name_items_in_one_tool_get_unique_files(tmp_path: Path):
    b = Bundle(tool="demo", queries=[VerifiedQuery("Top", "select 1"), VerifiedQuery("Top", "select 2")])
    build.apply(b, tmp_path)
    assert (tmp_path / "queries/top.sql").exists() and (tmp_path / "queries/top-2.sql").exists()


def test_crlf_sources_rebuild_idempotently(tmp_path: Path):
    b = Bundle(tool="demo", queries=[VerifiedQuery("Custom", "select 1\r\nfrom t\r\n")])
    build.apply(b, tmp_path)
    again = build.apply(b, tmp_path)
    assert again.writes.kept_hand_edits == [] and again.writes.written == []


@pytest.mark.parametrize("tool", ["tableau", "powerbi", "lightdash", "looker", "qlik"])
def test_rebuild_twice_is_a_no_op(tool: str, tmp_path: Path):
    store = _seed(tmp_path, tool)
    src = get_source(tool)
    build.apply(src.parse(store), tmp_path)
    again = build.apply(src.parse(RawStore(tmp_path, tool)), tmp_path)
    assert again.writes.kept_hand_edits == [] and again.writes.written == []


def test_report_keeps_hand_edit_section_after_report_command(tmp_path: Path):
    b = Bundle(tool="demo", queries=[VerifiedQuery("Q", "select 1")])
    build.apply(b, tmp_path)
    (tmp_path / "queries/q.sql").write_text("-- mine\nselect 2\n")
    build.apply(b, tmp_path)
    runner = CliRunner()
    assert runner.invoke(main, ["report", "--repo", str(tmp_path)]).exit_code == 0
    assert "edited them by hand" in (tmp_path / "migration/report.md").read_text()


def test_build_refuses_when_only_errors_were_extracted(tmp_path: Path):
    store = RawStore(tmp_path, "tableau")
    store.put_json("_errors.json", [{"error": "401"}])
    store.save()
    run = CliRunner().invoke(main, ["build", "tableau", "--repo", str(tmp_path)])
    assert run.exit_code != 0 and "_errors.json" in run.output


def test_verify_exits_nonzero_on_difference(tmp_path: Path):
    (tmp_path / "migration/parity").mkdir(parents=True)
    (tmp_path / "a.csv").write_text("k,v\na,1\n")
    (tmp_path / "b.csv").write_text("k,v\na,2\n")
    (tmp_path / "migration/parity/x.yaml").write_text(yaml.safe_dump({"name": "x", "source": {"csv": "a.csv"}, "target": {"csv": "b.csv"}, "keys": ["k"]}))
    run = CliRunner().invoke(main, ["verify", "--repo", str(tmp_path)])
    assert run.exit_code == 1 and "differs" in run.output


def test_init_copies_skills_and_points_agents_md_at_them(tmp_path: Path):
    run = CliRunner().invoke(main, ["init", "--repo", str(tmp_path)])
    assert run.exit_code == 0
    assert (tmp_path / "migration/skills/migrate/SKILL.md").exists()
    assert "migration/skills/migrate/SKILL.md" in (tmp_path / "AGENTS.md").read_text()


def test_init_creates_only_folders_it_writes_to(tmp_path: Path):
    from unshackle import scaffold

    scaffold.init(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir() if p.is_dir()) == ["migration"]  # skills and spec copies
    assert not list(tmp_path.rglob(".gitkeep"))
    agents = (tmp_path / "AGENTS.md").read_text()
    assert "automations" not in agents and "MEMORY.md" not in agents and "`evals/`" in agents
    from test_core import sample_bundle

    build.apply(sample_bundle(), tmp_path)
    assert not (tmp_path / "memory").exists() and not (tmp_path / "evals").exists()


def test_network_errors_are_clean_messages(tmp_path: Path, monkeypatch):
    import httpx

    from unshackle.sources import lightdash

    def boom(*a, **k):
        raise httpx.ConnectError("no route")

    monkeypatch.setenv("LIGHTDASH_URL", "https://example.invalid")
    monkeypatch.setenv("LIGHTDASH_API_KEY", "x")
    monkeypatch.setenv("LIGHTDASH_PROJECT", "p")
    monkeypatch.setattr(lightdash.LightdashSource, "inventory", boom)
    run = CliRunner().invoke(main, ["inventory", "lightdash", "--repo", str(tmp_path)])
    assert run.exit_code != 0 and "network error" in run.output and "Traceback" not in run.output


def test_cedar_ids_are_canonical(tmp_path: Path):
    for tool in ("lightdash", "sigma", "omni", "looker", "qlik"):
        repo = tmp_path / tool
        store = _seed(repo, tool)
        build.apply(get_source(tool).parse(store), repo)
        cedar = repo / f"policies/{tool}.cedar"
        if not cedar.exists():
            continue
        for line in cedar.read_text().splitlines():
            if "Column::" in line:
                rid = line.split('Column::"')[1].split('"')[0]
                assert len(rid.split(".")) == 4, (tool, rid)


def test_writer_still_guards_other_tools_files(tmp_path: Path):
    t = Target(tmp_path, tool="alpha")
    t.write("notebooks/x.py", "a = 1\n")
    t.save()
    t2 = Target(tmp_path, tool="beta")
    t2.write("notebooks/x.py", "a = 2\n")
    assert (tmp_path / "notebooks/x.py").read_text() == "a = 1\n"
    assert (tmp_path / "notebooks/x.beta.py").read_text() == "a = 2\n"


def test_reserved_and_colliding_names(tmp_path: Path):
    from unshackle.ir import Dimension, Measure, Metric, SemanticModel

    b = Bundle(tool="demo")
    b.semantic_models.append(
        SemanticModel(
            "targets",
            "fin.targets",
            dimensions=[Dimension("month", "time"), Dimension("Order Date", "time"), Dimension("order_date", "time"), Dimension("2024 flag")],
            measures=[Measure("rev", "sum", "amount"), Measure("Rev", "sum", "amount2")],
            default_time_dimension="month",
        )
    )
    b.metrics.append(Metric("monthly", measure="rev", semantic_model="targets", filter="{{ Dimension('targets__month') }} > '2024-01-01'"))
    (tmp_path / "dbt_project.yml").write_text("name: x\n")  # MetricFlow is written in dbt projects only
    build.apply(b, tmp_path)
    doc = yaml.safe_load((tmp_path / "models/semantic/targets.yml").read_text())
    dims = {d["name"]: d for d in doc["semantic_models"][0]["dimensions"]}
    assert dims["targets_month"]["expr"] == "month"
    assert "order_date" in dims and "order_date_2" in dims and "n_2024_flag" in dims
    assert {m["name"] for m in doc["semantic_models"][0]["measures"]} == {"rev", "targets_rev"}
    assert dims["n_2024_flag"]["expr"] == '"2024 flag"'
    assert doc["metrics"][0]["filter"] == "{{ Dimension('targets__targets_month') }} > '2024-01-01'"


def test_metric_references_bind_to_the_right_measure(tmp_path: Path):
    from unshackle.ir import Dimension, Measure, Metric, SemanticModel

    b = Bundle(tool="demo")
    b.semantic_models.append(
        SemanticModel(
            "m",
            "s.t",
            dimensions=[Dimension("day_at", "time")],
            measures=[Measure("rev", "sum", "a", todo="untranslatable"), Measure("Rev", "sum", "b"), Measure("month", "sum", "c"), Measure("d", "sum", "e")],
        )
    )
    b.metrics += [
        Metric("rev", measure="rev", semantic_model="m"),
        Metric("Rev", measure="Rev", semantic_model="m"),
        Metric("share", type="ratio", numerator="Rev", denominator="Rev", semantic_model="m"),
        Metric("double", type="derived", expr="Rev * 2", metrics=["Rev"], semantic_model="m"),
        Metric("month", measure="month", semantic_model="m"),
    ]
    (tmp_path / "dbt_project.yml").write_text("name: x\n")  # MetricFlow is written in dbt projects only
    build.apply(b, tmp_path)
    text = (tmp_path / "models/semantic/m.yml").read_text()
    doc = yaml.safe_load(text)
    measures = {m["name"]: m for m in doc["semantic_models"][0]["measures"]}
    assert "m_month" in measures and "m_d" in measures and measures["m_d"]["expr"] == "e"
    metrics = {m["name"]: m for m in doc["metrics"]}
    assert "rev" not in metrics
    assert metrics["rev_2"]["type_params"]["measure"] == measures["m_rev"]["name"] if "m_rev" in measures else True
    assert metrics["share"]["type_params"] == {"numerator": "rev_2", "denominator": "rev_2"}
    assert metrics["double"]["type_params"]["expr"] == "rev_2 * 2"
    assert "metric_month" in metrics


# ---------------------------------------------------------------- warehouse type and default time dimension


def _time_bundle(*times: str, default: str = "") -> Bundle:
    from unshackle.ir import Dimension, Entity, Measure, Metric, SemanticModel

    b = Bundle(tool="demo")
    b.semantic_models.append(
        SemanticModel(
            "orders",
            "shop.orders",
            entities=[Entity("orders", "primary", "id")],
            dimensions=[Dimension(t, "time", time_granularity="day") for t in times] + [Dimension("status")],
            measures=[Measure("revenue", "sum", "amount")],
            default_time_dimension=default,
        )
    )
    b.metrics.append(Metric("revenue", measure="revenue", semantic_model="orders"))
    return b.settle_time_dimensions()


def test_init_assumes_no_warehouse(tmp_path: Path):
    import tomllib

    from unshackle import scaffold
    from unshackle.target.layout import Layout

    scaffold.init(tmp_path)
    cfg = tomllib.loads((tmp_path / "unshackle.toml").read_text())
    assert "type" not in cfg["datasources"]["warehouse"] and Layout.load(tmp_path).warehouses == {}


def test_warehouse_type_detected_from_the_export(tmp_path: Path):
    def parse(tool: str, edit=None) -> Bundle:
        repo = tmp_path / f"{tool}{len(list(tmp_path.iterdir()))}"
        shutil.copytree(Path(__file__).parent / "fixtures" / tool, repo / "migration" / "raw" / tool)
        if edit:
            edit(repo / "migration" / "raw" / tool)
        return get_source(tool).parse(RawStore(repo, tool))

    assert parse("lightdash").warehouse == "snowflake"  # explores' targetDatabase

    def postgres_project(raw: Path) -> None:
        p = raw / "project.json"
        p.write_text(json.dumps({**json.loads(p.read_text()), "warehouseConnection": {"type": "postgres"}}))

    assert parse("lightdash", postgres_project).warehouse == "postgres"  # the project's connection wins
    assert parse("tableau").warehouse == "postgres"  # connection class
    pbi = parse("powerbi")  # Sql.Database is SQL Server: not a type we know, so not detected
    assert pbi.warehouse == "" and any("Sql.Database" in w.message for w in pbi.warnings)
    assert parse("looker").warehouse == ""  # only a connection name in the export


def test_warehouse_type_config_then_export_then_ansi(tmp_path: Path):
    def dialects(repo: Path, b: Bundle) -> set[str]:
        build.apply(b, repo)
        doc = yaml.safe_load((repo / "semantic/orders.ossie.yaml").read_text())
        return {d["dialect"] for ds in doc["datasets"] for f in ds["fields"] for d in f["expression"]["dialects"]}

    b = _time_bundle("created_at")
    b.warehouse = "bigquery"
    assert dialects(tmp_path / "a", b) == {"BIGQUERY"}
    assert any("taken from the demo export" in w.message for w in b.warnings)

    (tmp_path / "b").mkdir()
    (tmp_path / "b/unshackle.toml").write_text('[datasources.warehouse]\ntype = "snowflake"\n')
    b = _time_bundle("created_at")
    b.warehouse = "bigquery"
    assert dialects(tmp_path / "b", b) == {"SNOWFLAKE"}  # an explicit config wins
    assert not [w for w in b.warnings if w.item.startswith("datasource")]

    b = _time_bundle("created_at")
    assert dialects(tmp_path / "c", b) == {"ANSI_SQL"}
    (w,) = [w for w in b.warnings if w.item == "datasource warehouse"]
    assert w.severity == "warn" and "[datasources.warehouse] type" in w.message
    assert "[datasources.warehouse] type" in (tmp_path / "c/migration/report.md").read_text()


def test_default_time_dimension_is_never_guessed(tmp_path: Path):
    def out(b: Bundle, name: str) -> tuple[str, dict, str, dict]:
        repo = tmp_path / name
        repo.mkdir()
        (repo / "dbt_project.yml").write_text("name: x\n")
        build.apply(b, repo)
        mf_text = (repo / "models/semantic/orders.yml").read_text()
        os_text = (repo / "models/semantic/orders.ossie.yaml").read_text()
        return mf_text, yaml.safe_load(mf_text), os_text, yaml.safe_load(os_text)

    # several time columns, none declared: unset, measures held back with a TODO naming the candidates
    b = _time_bundle("cancelled_at", "created_at")
    assert b.semantic_models[0].default_time_dimension == ""
    mf_text, mf, os_text, osd = out(b, "several")
    model = mf["semantic_models"][0]
    assert "defaults" not in model and "measures" not in model and not mf.get("metrics")
    assert "set defaults.agg_time_dimension to one of cancelled_at, created_at" in todo_for(mf_text, "measure", "revenue")
    assert "default_time_dimension" not in os_text.split("# TODO")[0]
    assert "default_time_dimension to one of cancelled_at, created_at" in os_text

    # declared by the source: used
    mf_text, mf, os_text, osd = out(_time_bundle("cancelled_at", "created_at", default="created_at"), "declared")
    assert mf["semantic_models"][0]["defaults"] == {"agg_time_dimension": "created_at"}
    assert '"default_time_dimension": "created_at"' in os_text and "one of" not in os_text

    # exactly one time column: not a guess
    b = _time_bundle("created_at")
    assert b.semantic_models[0].default_time_dimension == "created_at"
    mf_text, mf, os_text, osd = out(b, "single")
    assert mf["semantic_models"][0]["defaults"] == {"agg_time_dimension": "created_at"}
    assert {m["name"] for m in mf["metrics"]} == {"revenue"}


def test_semantic_yaml_sources_do_not_pick_the_first_time_column(tmp_path: Path):
    from unshackle.sources._semyaml import NField, NTable, build_semantic

    def model(default: str = ""):
        b = Bundle(tool="demo")
        t = NTable("orders", "orders", primary_key="id", default_time=default,
                   dims={"id": NField("id", type="number"), "cancelled_at": NField("cancelled_at", type="timestamp"),
                         "created_at": NField("created_at", type="timestamp")},
                   metrics={"revenue": NField("revenue", sql="${TABLE}.amount", type="sum")})
        build_semantic(b, [t], "lightdash")
        return b.settle_time_dimensions().semantic_models[0]

    assert model().default_time_dimension == ""
    assert model("created_at").default_time_dimension == "created_at"


@pytest.mark.parametrize("tool", ["metabase", "superset", "redash"])
def test_new_sources_detect_postgres(tmp_path: Path, tool: str):
    assert get_source(tool).parse(_seed(tmp_path, tool)).warehouse == "postgres"


@pytest.mark.parametrize("tool", ["metabase", "superset", "powerbi", "qlik"])
def test_optional_filter_binds_are_typed_before_the_null_check(tmp_path: Path, tool: str):
    """`(:p IS NULL OR col = :p)` fails on Postgres with server-side binding (psycopg 3): the type of
    :p can't be determined from `IS NULL`. The comparison comes first so the parameter takes the column's type."""
    import re

    b = get_source(tool).parse(_seed(tmp_path, tool))
    sqls = [t.sql for d in b.dashboards for t in d.tiles] + [q.sql for q in b.queries]
    optional = [s for s in sqls if "IS NULL" in s and re.search(r":\w+ IS NULL", s)]
    assert optional
    for sql in optional:
        assert not re.search(r"\(:\w+ IS NULL OR", sql), sql
        assert re.search(r":(\w+)(?: AS DATE\))? OR :\1 IS NULL\)", sql), sql
