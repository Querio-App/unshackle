"""`unshackle import`: files downloaded by hand build the same output as the API path, for what files carry."""

import dataclasses
import json
import os
import shutil
import zipfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from unshackle.cli import main
from unshackle.ir import Bundle

FIX = Path(__file__).parent / "fixtures"


def _cli(*args: str):
    return CliRunner().invoke(main, [str(a) for a in args])


def _ir(repo: Path, tool: str) -> Bundle:
    return Bundle.from_json((repo / "migration" / "ir" / f"{tool}.json").read_text())


def api_build(tmp_path: Path, tool: str, only: list[str] | None = None) -> Bundle:
    """The API path: the saved extract in tests/fixtures/<tool> (or some of its folders), built."""
    repo = tmp_path / "api"
    raw = repo / "migration" / "raw" / tool
    if only is None:
        shutil.copytree(FIX / tool, raw)
    for rel in only or []:
        shutil.copytree(FIX / tool / rel, raw / rel)
    r = _cli("build", tool, "--repo", repo)
    assert r.exit_code == 0, r.output
    return _ir(repo, tool)


def manual_build(tmp_path: Path, tool: str, *inputs: Path) -> tuple[Path, Bundle]:
    repo = tmp_path / "manual"
    repo.mkdir(exist_ok=True)
    r = _cli("import", tool, *inputs, "--repo", repo)
    assert r.exit_code == 0, r.output
    r = _cli("build", tool, "--repo", repo)
    assert r.exit_code == 0, r.output
    return repo, _ir(repo, tool)


def plain(items, drop=("source_ref", "url", "views")):
    def clean(x):
        if isinstance(x, dict):
            return {k: clean(v) for k, v in x.items() if k not in drop}
        if isinstance(x, list):
            return [clean(v) for v in x]
        return x

    return clean([dataclasses.asdict(i) for i in items])


def same(a: Bundle, b: Bundle, *parts: str) -> None:
    for part in parts:
        assert plain(getattr(a, part)) == plain(getattr(b, part)), part


def report(repo: Path) -> str:
    return (repo / "migration" / "report.md").read_text()


# ------------------------------------------------------------ per tool


def test_tableau_twbx_and_tdsx(tmp_path: Path):
    dl = tmp_path / "downloads"
    dl.mkdir()
    with zipfile.ZipFile(dl / "Superstore Sales.twbx", "w") as z:
        z.write(next((FIX / "tableau/workbooks").glob("*.twb")), "Superstore Sales.twb")
        z.writestr("Data/Extracts/orders.hyper", b"extract data")
    with zipfile.ZipFile(dl / "Sales Targets.tdsx", "w") as z:
        z.write(next((FIX / "tableau/datasources").glob("*.tds")), "Sales Targets.tds")
    api = api_build(tmp_path, "tableau")
    repo, man = manual_build(tmp_path, "tableau", dl / "Superstore Sales.twbx", dl / "Sales Targets.tdsx")

    raw = repo / "migration/raw/tableau"
    assert sorted(p.relative_to(raw).as_posix() for p in raw.rglob("*") if p.is_file()) == [
        "_import.json", "_manifest.json", "datasources/Sales Targets.tds", "workbooks/Superstore Sales.twb"]
    assert json.loads((raw / "_import.json").read_text())["origin"] == "manual"
    same(api, man, "tables", "semantic_models", "metrics", "queries", "row_filters")
    no_ids = ("source_ref", "url", "views", "source_config")  # source_config holds the workbook / view ids
    assert plain(api.dashboards, no_ids) == plain(man.dashboards, no_ids)
    assert api.permissions and not man.permissions
    text = report(repo)
    assert "## tableau: imported manually" in text and "- permissions:" in text and "- usage:" in text


def test_tableau_rejects_other_files(tmp_path: Path):
    (tmp_path / "book.xlsx").write_bytes(b"x")
    r = _cli("import", "tableau", tmp_path / "book.xlsx", "--repo", tmp_path)
    assert r.exit_code != 0 and ".twbx" in r.output and ".tdsx" in r.output


def _pbip(root: Path, model_files: str = "definition") -> Path:
    """A .pbip project laid out like Power BI Desktop saves it, built from the API fixture."""
    model = "8d2f4c10-5e6b-4a7d-b1c3-9e0f2a4b6c01"
    proj = root / "Sales"
    sm = proj / "Sales Model.SemanticModel"
    shutil.copytree(FIX / f"powerbi/models/{model}", sm)
    (sm / ".pbi").mkdir()
    (sm / ".pbi/cache.abf").write_bytes(b"cached data")
    for rid, name in (("c41a9e20-7b3d-4f5e-8a1c-0d2e3f4a5b01", "Sales Overview"), ("c41a9e20-7b3d-4f5e-8a1c-0d2e3f4a5b02", "Executive Summary")):
        shutil.copytree(FIX / f"powerbi/reports/{rid}", proj / f"{name}.Report")
        ref = {"version": "4.0", "datasetReference": {"byPath": {"path": "../Sales Model.SemanticModel"}}}
        (proj / f"{name}.Report/definition.pbir").write_text(json.dumps(ref))
        (proj / f"{name}.pbip").write_text(json.dumps({"artifacts": [{"report": {"path": f"{name}.Report"}}]}))
    return proj


def test_powerbi_pbip_project(tmp_path: Path):
    proj = _pbip(tmp_path / "downloads")
    api = api_build(tmp_path, "powerbi")
    repo, man = manual_build(tmp_path, "powerbi", proj / "Sales Overview.pbip")

    raw = repo / "migration/raw/powerbi"
    assert (raw / "models/Sales Model/definition/tables/Sales.tmdl").exists()
    assert (raw / "reports/Sales Overview/definition/pages/pages.json").exists()
    assert not list(raw.rglob("cache.abf"))
    same(api, man, "tables", "semantic_models", "metrics", "queries", "permissions", "row_filters")
    no_paths = ("source_ref", "url", "views", "source_config")  # source_config holds the raw file path
    by_name = lambda b: sorted(plain(b.dashboards, no_paths), key=lambda d: d["name"])  # noqa: E731
    assert by_name(api) == by_name(man)  # reports are linked to their model through byPath
    assert "## powerbi: imported manually" in report(repo)


def test_powerbi_rejects_pbix_and_model_bim(tmp_path: Path):
    (tmp_path / "Sales.pbix").write_bytes(b"PK\x03\x04")
    r = _cli("import", "powerbi", tmp_path / "Sales.pbix", "--repo", tmp_path)
    assert r.exit_code != 0 and "Power BI project (.pbip)" in r.output
    sm = tmp_path / "Old" / "Old.SemanticModel"
    sm.mkdir(parents=True)
    (sm / "model.bim").write_text("{}")
    r = _cli("import", "powerbi", tmp_path / "Old", "--repo", tmp_path)
    assert r.exit_code != 0 and "TMDL" in r.output
    assert not (tmp_path / "migration/raw/powerbi").exists()


def test_looker_lookml_checkout(tmp_path: Path):
    checkout = tmp_path / "lookml-repo"
    shutil.copytree(FIX / "looker/lookml", checkout)
    (checkout / ".git").mkdir()
    (checkout / ".git/config").write_text("[core]\n")
    (checkout / "README.md").write_text("# project\n")
    api = api_build(tmp_path, "looker")
    repo, man = manual_build(tmp_path, "looker", checkout)

    imported = json.loads((repo / "migration/raw/looker/_import.json").read_text())["files"]
    assert imported and all(f.startswith("lookml/") and f.endswith(".lkml") for f in imported)
    same(api, man, "tables", "semantic_models", "metrics", "knowledge", "permissions", "row_filters")
    assert api.dashboards and not man.dashboards
    assert "dashboards and Looks" in report(repo)


def test_lightdash_dbt_project_and_download(tmp_path: Path):
    proj = tmp_path / "jaffle"
    proj.mkdir()
    (proj / "dbt_project.yml").write_text("name: jaffle\n")
    shutil.copytree(FIX / "lightdash/dbt/models", proj / "models")
    for kind in ("charts", "dashboards"):  # `lightdash download` writes lightdash/<kind>/<slug>.yml
        (proj / "lightdash" / kind).mkdir(parents=True)
        for p in (FIX / "lightdash" / kind).glob("*.yaml"):
            shutil.copy(p, proj / "lightdash" / kind / f"{p.stem}.yml")
    dbt_only = api_build(tmp_path / "dbt", "lightdash", only=["dbt"])  # extract with only LIGHTDASH_DBT_PROJECT_DIR
    api = api_build(tmp_path, "lightdash")
    repo, man = manual_build(tmp_path, "lightdash", proj)

    assert (repo / "migration/raw/lightdash/dbt/models/marts/schema.yml").exists()
    assert (repo / "migration/raw/lightdash/charts/monthly-revenue.yaml").exists()
    same(dbt_only, man, "tables", "semantic_models", "metrics", "permissions", "row_filters")
    [a], [m] = api.dashboards, man.dashboards
    assert [(t.id, t.title, t.kind) for t in a.tiles] == [(t.id, t.title, t.kind) for t in m.tiles]
    assert a.layout == m.layout and plain(a.filters) == plain(m.filters)
    charts = [t for t in m.tiles if t.kind != "text"]
    assert charts and all(not t.sql and "no compiled SQL" in t.todo for t in charts)
    assert "## lightdash: imported manually" in report(repo)


def test_omni_model_folder(tmp_path: Path):
    model = tmp_path / "ecommerce"
    shutil.copytree(FIX / "omni/models/a1b2c3d4-5e6f-4a0b-9c8d-111111111111", model,
                    ignore=shutil.ignore_patterns("_meta.json", "sample_queries"))
    api = api_build(tmp_path, "omni")
    repo, man = manual_build(tmp_path, "omni", model)

    assert (repo / "migration/raw/omni/models/ecommerce/ecomm/order_items.view.yaml").exists()
    same(api, man, "tables", "semantic_models", "metrics", "permissions", "row_filters")
    assert api.dashboards and not man.dashboards
    assert "## omni: imported manually" in report(repo)


def test_thoughtspot_tml_zip(tmp_path: Path):
    with zipfile.ZipFile(tmp_path / "Sales Overview.zip", "w") as z:  # what Export TML downloads
        for p in (FIX / "thoughtspot/tml").glob("*/*.tml"):
            z.write(p, f"{p.stem}.{p.parent.name}.tml")
    api = api_build(tmp_path, "thoughtspot")
    repo, man = manual_build(tmp_path, "thoughtspot", tmp_path / "Sales Overview.zip")

    raw = repo / "migration/raw/thoughtspot"
    assert sorted(p.parent.name for p in raw.glob("tml/*/*.tml")) == sorted(p.parent.name for p in FIX.glob("thoughtspot/tml/*/*.tml"))
    same(api, man, "tables", "semantic_models", "metrics", "knowledge", "row_filters")
    [a], [m] = api.dashboards, man.dashboards
    assert [(t.id, t.title, t.kind) for t in a.tiles] == [(t.id, t.title, t.kind) for t in m.tiles]
    assert not any(t.sql for t in m.tiles)  # SQL only comes from the API; the notebook gets a TODO per tile
    assert len(man.permissions) < len(api.permissions)  # sharing is API-only; column security from TML stays
    assert "## thoughtspot: imported manually" in report(repo)


def test_thoughtspot_rejects_non_tml(tmp_path: Path):
    (tmp_path / "x.tml").write_text("hello: world\n")
    r = _cli("import", "thoughtspot", tmp_path / "x.tml", "--repo", tmp_path)
    assert r.exit_code != 0 and "not a TML object" in r.output


def test_hex_project_export_and_semantic(tmp_path: Path):
    api = api_build(tmp_path, "hex")
    repo, man = manual_build(tmp_path, "hex", FIX / "hex/export/revenue_overview.yaml", FIX / "hex/semantic")

    assert (repo / "migration/raw/hex/export/revenue_overview.yaml").exists()
    assert (repo / "migration/raw/hex/semantic/ecommerce.yaml").exists()
    same(api, man, "tables", "semantic_models", "metrics")
    exported = [d for d in api.dashboards if d.name == "Revenue Overview"]
    assert plain(exported) == plain(man.dashboards)
    assert "## hex: imported manually" in report(repo)


def test_hex_nothing_usable(tmp_path: Path):
    (tmp_path / "notes.yaml").write_text("just: notes\n")
    r = _cli("import", "hex", tmp_path / "notes.yaml", "--repo", tmp_path)
    assert r.exit_code != 0 and "nothing Hex can use" in r.output


def test_sigma_has_no_manual_export(tmp_path: Path):
    (tmp_path / "workbook.csv").write_text("a\n1\n")
    r = _cli("import", "sigma", tmp_path / "workbook.csv", "--repo", tmp_path)
    assert r.exit_code != 0 and "unshackle extract sigma" in r.output


# ------------------------------------------------------------ safety


@pytest.mark.parametrize("member", ["../evil.tml", "/tmp/evil.tml", "a/../../evil.tml", "C:/evil.tml", "a\\..\\evil.tml"])
def test_zip_slip_rejected(tmp_path: Path, member: str):
    with zipfile.ZipFile(tmp_path / "export.zip", "w") as z:
        z.writestr("ok.liveboard.tml", (FIX / "thoughtspot/tml/liveboard/c4d5e6f7-1111-4a2b-9c3d-4e5f6a7b3001.tml").read_text())
        z.writestr(member, "guid: x\ntable: {name: t}\n")
    repo = tmp_path / "repo"
    r = _cli("import", "thoughtspot", tmp_path / "export.zip", "--repo", repo)
    assert r.exit_code != 0 and "unsafe path in archive" in r.output
    assert not (tmp_path / "evil.tml").exists() and not (repo / "migration/raw/thoughtspot").exists()


def test_zip_symlink_rejected(tmp_path: Path):
    link = zipfile.ZipInfo("model/link.view.yaml")
    link.external_attr = (0o120777 << 16)
    with zipfile.ZipFile(tmp_path / "model.zip", "w") as z:
        z.writestr("model/model.yaml", "{}\n")
        z.writestr(link, "/etc/passwd")
    r = _cli("import", "omni", tmp_path / "model.zip", "--repo", tmp_path / "repo")
    assert r.exit_code != 0 and "symlink in archive" in r.output


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
def test_folder_symlinks_not_followed(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.view.lkml").write_text("view: secret {}\n")
    checkout = tmp_path / "lookml"
    shutil.copytree(FIX / "looker/lookml", checkout)
    (checkout / "linked.view.lkml").symlink_to(outside / "secret.view.lkml")
    (checkout / "linked_dir").symlink_to(outside, target_is_directory=True)
    repo = tmp_path / "repo"
    assert _cli("import", "looker", checkout, "--repo", repo).exit_code == 0
    assert not list((repo / "migration/raw/looker").rglob("*secret*")) and not (repo / "migration/raw/looker/lookml/linked.view.lkml").exists()


def test_reimport_replaces_raw(tmp_path: Path):
    wb = next((FIX / "tableau/workbooks").glob("*.twb"))
    shutil.copy(wb, tmp_path / "A.twb")
    shutil.copy(wb, tmp_path / "B.twb")
    repo = tmp_path / "repo"
    assert _cli("import", "tableau", tmp_path / "A.twb", "--repo", repo).exit_code == 0
    r = _cli("import", "tableau", tmp_path / "B.twb", "--repo", repo)
    assert r.exit_code == 0 and "replacing 1 earlier" in r.output
    assert [p.name for p in (repo / "migration/raw/tableau/workbooks").iterdir()] == ["B.twb"]


def _superset_zip(tmp_path: Path) -> Path:
    """What Export on Superset's Dashboards list downloads: the bundle under one dated top folder."""
    top = "dashboard_export_20261022T104713"
    archive = tmp_path / f"{top}.zip"
    with zipfile.ZipFile(archive, "w") as z:
        for p in sorted((FIX / "superset/export").rglob("*.yaml")):
            z.write(p, f"{top}/{p.relative_to(FIX / 'superset/export').as_posix()}")
    return archive


def test_superset_export_zip(tmp_path: Path):
    api = api_build(tmp_path, "superset")
    repo, man = manual_build(tmp_path, "superset", _superset_zip(tmp_path))

    raw = repo / "migration/raw/superset"
    assert sorted(p.relative_to(raw).as_posix() for p in raw.rglob("*.yaml")) == \
        sorted("export/" + p.relative_to(FIX / "superset/export").as_posix() for p in (FIX / "superset/export").rglob("*.yaml"))
    same(api, man, "tables", "semantic_models", "metrics")
    [a], [m] = api.dashboards, man.dashboards
    assert [(t.id, t.title, t.kind, t.mark) for t in a.tiles] == [(t.id, t.title, t.kind, t.mark) for t in m.tiles]
    assert [f.name for f in a.filters] == [f.name for f in m.filters]
    charts = [t for t in m.tiles if t.kind != "text"]
    assert charts and all(not t.sql and "export zip has no compiled SQL" in t.todo for t in charts)
    assert api.permissions and not man.permissions and not man.row_filters  # roles and RLS are API-only
    text = report(repo)
    assert "## superset: imported manually" in text and "row-level security" in text


def test_superset_unzipped_folder(tmp_path: Path):
    folder = tmp_path / "downloads" / "dashboard_export_20261022T104713"
    shutil.copytree(FIX / "superset/export", folder)
    (folder.parent / "notes.txt").write_text("not part of the export\n")
    repo, man = manual_build(tmp_path, "superset", folder.parent)
    assert (repo / "migration/raw/superset/export/metadata.yaml").exists()
    assert not list((repo / "migration/raw/superset").rglob("notes.txt"))
    assert [d.name for d in man.dashboards] == ["Sales overview"]


def test_superset_drops_database_secrets(tmp_path: Path):
    folder = tmp_path / "export"
    shutil.copytree(FIX / "superset/export", folder)
    db = folder / "databases/Dunder_Mifflin.yaml"
    db.write_text(db.read_text() + "encrypted_extra: '{\"credentials\": \"x\"}'\nssh_tunnel:\n  password: x\n")
    repo = tmp_path / "repo"
    assert _cli("import", "superset", folder, "--repo", repo).exit_code == 0
    text = (repo / "migration/raw/superset/export/databases/Dunder_Mifflin.yaml").read_text()
    assert "encrypted_extra" not in text and "ssh_tunnel" not in text


def test_superset_rejects_other_files(tmp_path: Path):
    (tmp_path / "dashboard.json").write_text("{}")
    r = _cli("import", "superset", tmp_path / "dashboard.json", "--repo", tmp_path)
    assert r.exit_code != 0 and "accepts" in r.output
    (tmp_path / "empty").mkdir()
    (tmp_path / "empty/readme.md").write_text("hi\n")
    r = _cli("import", "superset", tmp_path / "empty", "--repo", tmp_path)
    assert r.exit_code != 0 and "nothing Apache Superset can use" in r.output
