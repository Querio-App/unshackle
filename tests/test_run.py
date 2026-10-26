"""`build` loads copied data into a local DuckDB file the notebooks fall back to, and `unshackle run` reports per dashboard."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from unshackle import build
from unshackle.cli import main
from unshackle.ir import Bundle, Dashboard, Encoding, Tile

DATA = Path(__file__).parent / "data" / "dunder_mifflin"


def _repo(tmp_path: Path) -> tuple[Path, Bundle]:
    """A dashboard on Dunder Mifflin data copied into the repo (as --with-data does): one tile per outcome."""
    repo = tmp_path / "repo"
    (repo / "data" / "dm").mkdir(parents=True)
    for t in ("orders", "products"):
        shutil.copy(DATA / f"{t}.parquet", repo / "data" / "dm" / f"{t}.parquet")
    bar = {"x": Encoding("paper_type"), "y": Encoding("n", "quantitative")}
    tiles = [
        Tile("by-type", "Products by paper type", mark="bar", encodings=bar,
             sql="select paper_type, count(*) as n from paper.products group by 1 order by 1"),
        Tile("none", "No orders before 2000", kind="table", sql="select order_id from paper.orders where purchased_at < date '2000-01-01'"),
        Tile("broken", "Broken", mark="bar", encodings=bar, sql="select paper_type, count(*) as n from paper.missing_table group by 1"),
        Tile("nosql", "Not translated", mark="bar", encodings=bar),
        Tile("note", kind="text", text="A note"),
    ]
    b = Bundle(tool="tableau", dashboards=[Dashboard("Paper", "paper", tiles=tiles)])
    b.local_db = "data/dm/dm.duckdb"
    b.local_db_sql = ("create schema if not exists paper;\n"
                      "create or replace table paper.orders as select * from read_parquet('data/dm/orders.parquet');\n"
                      "create or replace table paper.products as select * from read_parquet('data/dm/products.parquet');")
    return repo, b


def test_build_loads_the_local_database(tmp_path: Path, monkeypatch):
    duckdb = pytest.importorskip("duckdb")
    repo, b = _repo(tmp_path)
    monkeypatch.chdir(tmp_path)  # the load SQL's paths are relative to the repo, not the working directory
    result = build.apply(b, repo)
    assert result.local_db == "data/dm/dm.duckdb"
    with duckdb.connect(str(repo / "data/dm/dm.duckdb"), read_only=True) as con:
        assert con.execute("select count(*) from paper.orders").fetchone() == (3503,)  # the README's row count
        assert con.execute("select count(*) from paper.products").fetchone() == (15,)
    nb = json.loads((repo / "notebooks/paper.ipynb").read_text())
    assert nb["metadata"]["unshackle"]["local_db"] == "../data/dm/dm.duckdb"  # relative to the notebook's folder
    setup = "".join(next(c["source"] for c in nb["cells"] if c["id"] == "setup"))
    assert 'os.path.abspath(\'../data/dm/dm.duckdb\')' in setup and 'os.environ.get("UNSHACKLE_WAREHOUSE_URL")' in setup


def test_build_without_copied_data_keeps_the_plain_setup(tmp_path: Path):
    repo, b = _repo(tmp_path)
    b.local_db = b.local_db_sql = ""
    assert build.apply(b, repo).local_db == ""
    assert not (repo / "data/dm/dm.duckdb").exists()
    nb = json.loads((repo / "notebooks/paper.ipynb").read_text())
    assert "local_db" not in nb["metadata"]["unshackle"]
    assert 'create_engine(os.environ["UNSHACKLE_WAREHOUSE_URL"])' in "".join(next(c["source"] for c in nb["cells"] if c["id"] == "setup"))


def test_run_reports_each_dashboard_without_a_warehouse_url(tmp_path: Path, monkeypatch):
    pytest.importorskip("duckdb_engine")
    pytest.importorskip("nbclient")
    repo, b = _repo(tmp_path)
    build.apply(b, repo)
    monkeypatch.delenv("UNSHACKLE_WAREHOUSE_URL", raising=False)
    res = CliRunner().invoke(main, ["run", "--repo", str(repo)])
    assert res.exit_code == 1, res.output  # one tile fails
    lines = res.output.splitlines()
    head = lines.index("dashboard  tiles  with SQL  ran  returned rows  errors")
    # 4 query tiles (the text tile is markdown); 3 with SQL; 2 ran; 1 returned rows (no order is from before 2000)
    assert lines[head + 1].split() == ["paper", "4", "3", "2", "1", "1"]
    assert lines[head + 2].startswith("  broken: DatabaseError: ") and "Table with name missing_table does not exist" in lines[head + 2]
    assert lines[-1] == "1 dashboards: 4 tiles, 3 with SQL, 2 ran, 1 returned rows, 1 errors"
    html = (repo / "notebooks/paper.html").read_text()
    assert "unshackle-rows" not in html  # the row probe stays out of the preview
    got = {r["paper_type"]: r["n"] for s in html.split("vegaEmbed(")[1:]
           for d in json.loads(s.split(", ", 1)[1].rsplit(");", 1)[0])["datasets"].values() for r in d}
    duckdb = pytest.importorskip("duckdb")
    want = dict(duckdb.sql(f"select paper_type, count(*) from '{DATA / 'products.parquet'}' group by 1").fetchall())
    assert got == want


def test_run_needs_a_url_without_copied_data(tmp_path: Path, monkeypatch):
    pytest.importorskip("nbclient")
    repo, b = _repo(tmp_path)
    b.local_db = b.local_db_sql = ""
    build.apply(b, repo)
    monkeypatch.delenv("UNSHACKLE_WAREHOUSE_URL", raising=False)
    res = CliRunner().invoke(main, ["run", "--repo", str(repo)])
    assert res.exit_code != 0 and "set UNSHACKLE_WAREHOUSE_URL" in res.output
