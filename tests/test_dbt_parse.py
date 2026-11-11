"""Every source's fixtures must produce MetricFlow YAML that `dbt parse` accepts.

Needs a dbt with an adapter (e.g. `uv pip install dbt-core dbt-duckdb` in any venv).
Point UNSHACKLE_DBT at its `dbt` binary; the test is skipped otherwise. CI sets it.
"""

import os
import re
import shutil
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from unshackle import build
from unshackle.ir import Measure, Metric
from unshackle.raw import RawStore
from unshackle.sources import TOOLS, get_source

FIXTURES = Path(__file__).parent / "fixtures"
DBT = os.environ.get("UNSHACKLE_DBT") or ""

TIME_SPINE_SQL = "select cast(range as date) as date_day from range(date '2020-01-01', date '2030-01-01', interval 1 day)\n"
TIME_SPINE_YML = """\
models:
  - name: metricflow_time_spine
    time_spine:
      standard_granularity_column: date_day
    columns:
      - name: date_day
        granularity: day
"""


needs_dbt = pytest.mark.skipif(not DBT or not Path(DBT).exists(), reason="set UNSHACKLE_DBT to a dbt binary to run")


def _dbt_repo(tmp_path: Path, name: str, tools: list[str]) -> Path:
    repo = tmp_path / name
    (repo / "models").mkdir(parents=True)
    (repo / "dbt_project.yml").write_text(f"name: p_{name}\nversion: '1.0'\nprofile: p\nmodel-paths: [models]\n")
    (repo / "profiles.yml").write_text(f"p:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n      path: {tmp_path / 'p.duckdb'}\n")
    for tool in tools:
        shutil.copytree(FIXTURES / tool, repo / "migration" / "raw" / tool)
        store = RawStore(repo, tool)
        store.rescan()
        build.apply(get_source(tool).parse(store), repo)
    return repo


@needs_dbt
@pytest.mark.parametrize("tool", sorted(TOOLS))
def test_dbt_parse(tool: str, tmp_path: Path):
    _parse(_dbt_repo(tmp_path, tool, [tool]))


@needs_dbt
def test_dbt_parse_two_tools_one_repo(tmp_path: Path):
    _parse(_dbt_repo(tmp_path, "multi", ["lightdash", "looker", "tableau"]))


@needs_dbt
def test_dbt_parse_metabase_duplicate_dimension_labels(tmp_path: Path):
    repo = _dbt_repo(tmp_path, "dimension_labels", [])
    shutil.copytree(FIXTURES / "metabase_dimension_labels", repo / "migration/raw/metabase")
    bundle = get_source("metabase").parse(RawStore(repo, "metabase"))
    # dbt skips semantic validation entirely when a project has no metrics.
    bundle.semantic_models[0].measures.append(Measure("order_count", "count", "id"))
    bundle.metrics.append(Metric("order_count", measure="order_count", semantic_model="orders_model"))
    original = deepcopy(bundle)
    build.apply(bundle, repo)
    assert bundle.semantic_models == original.semantic_models
    assert bundle.queries == original.queries
    _parse(repo)
    model = yaml.safe_load((repo / "models/semantic/orders_model.yml").read_text())["semantic_models"][0]
    assert {d["name"]: d["label"] for d in model["dimensions"]} == {
        "id": "ID", "user_id": "User ID", "product_id": "Product ID", "created_at": "Created At",
        "name": "User ID (name)", "title": "Product ID (title)",
    }


@needs_dbt
@pytest.mark.parametrize("order", ["forward", "reverse"])
def test_dbt_parse_all_tools_one_repo(order: str, tmp_path: Path):
    tools = sorted(TOOLS)
    _parse(_dbt_repo(tmp_path, order, tools if order == "forward" else tools[::-1]))


def test_generated_sql_models_have_unique_names(tmp_path: Path):
    """Each generated SQL model is written once, so dbt never sees two models with one name (no dbt needed)."""
    repo = _dbt_repo(tmp_path, "all", sorted(TOOLS))
    models = [p.stem for p in (repo / "models").rglob("*.sql")]
    assert "customer_name_lod" in models  # the Tableau FIXED grain, a SQL model the stubs in _parse must not duplicate
    assert len(models) == len(set(models))


def _parse(repo: Path) -> None:
    refs = set()
    for p in (repo / "models").rglob("*.yml"):
        refs |= set(re.findall(r"ref\('([^']+)'\)", p.read_text()))
    for r in refs - {p.stem for p in (repo / "models").rglob("*.sql")}:  # stand-ins for warehouse tables, not generated SQL models
        (repo / "models" / f"{r}.sql").write_text("select 1 as x\n")
    (repo / "models" / "metricflow_time_spine.sql").write_text(TIME_SPINE_SQL)
    (repo / "models" / "time_spine.yml").write_text(TIME_SPINE_YML)
    run = subprocess.run([DBT, "parse", "--profiles-dir", str(repo), "--project-dir", str(repo)], capture_output=True, text=True, timeout=300)
    assert run.returncode == 0, run.stdout[-3000:] + run.stderr[-2000:]
