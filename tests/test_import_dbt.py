"""Plain dbt imports are offline and preserve unresolved source semantics."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

from unshackle import build
from unshackle.imports.dbt import parse

FIXTURE = Path(__file__).parent / "fixtures/import_dbt/plain"


def project(tmp_path):
    dest = tmp_path / "source"
    shutil.copytree(FIXTURE, dest)
    return dest


def test_plain_project_and_metricflow(tmp_path):
    bundle = parse(project(tmp_path))
    assert bundle.tool == "dbt"
    assert bundle.tables[0].description == "Synthetic paper orders."
    assert bundle.tables[0].columns[0].name == "amount"
    assert bundle.queries[0].source_ref == "dbt:transforms/orders.sql"
    assert "{{ ref('stg_orders') }}" in bundle.queries[0].sql
    assert "Jinja" in bundle.queries[0].todo
    assert bundle.semantic_models[0].measures[0].agg == "sum"
    assert not bundle.metrics[0].todo
    assert bundle.metrics[1].todo and "7 days" in bundle.metrics[1].source_expression
    out = tmp_path / "output"
    build.apply(bundle, out)
    assert "rolling_revenue" in (out / "semantic/orders_semantic.ossie.yaml").read_text()


def test_manifest_matches_original_path_and_local_package(tmp_path):
    root = project(tmp_path)
    target = root / "target"
    target.mkdir()
    node = dict(resource_type="model", package_name="dunder_example", original_file_path="transforms/orders.sql",
                schema="analytics", alias="fct_orders", compiled_code="select 1 as order_id", columns={})
    foreign = {**node, "package_name": "foreign_package", "compiled_code": "select 99"}
    (target / "manifest.json").write_text(json.dumps({"nodes": {"model.foreign.orders": foreign, "model.dunder.orders": node}}))
    b = parse(root)
    assert b.queries[0].sql == "select 1 as order_id"
    assert not b.queries[0].todo
    assert (b.tables[0].schema, b.tables[0].name) == ("analytics", "fct_orders")
    assert "analytics.fct_orders" in b.semantic_models[0].todo
    assert b.semantic_models[0].table == "orders"  # MetricFlow ref remains the dbt node name.
    assert any("ref('orders')" in k.body for k in b.knowledge)
    out = tmp_path / "output"
    build.apply(b, out)
    text = (out / "semantic/orders_semantic.ossie.yaml").read_text()
    assert "TODO(unshackle)" in text and "analytics.fct_orders" in text


def test_compiled_file_and_custom_target(tmp_path):
    root = project(tmp_path)
    with (root / "dbt_project.yml").open("a") as f:
        f.write("target-path: artifacts\n")
    compiled = root / "artifacts/compiled/dunder_example/transforms/orders.sql"
    compiled.parent.mkdir(parents=True)
    compiled.write_text("select 2 as order_id")
    assert parse(root).queries[0].sql == "select 2 as order_id"


def test_duplicate_names_remain_distinct_without_guessing(tmp_path):
    root = project(tmp_path)
    (root / "transforms/nested").mkdir()
    (root / "transforms/nested/orders.sql").write_text("select 2")
    bundle = parse(root)
    assert len({q.name for q in bundle.queries}) == 2
    assert len({t.name for t in bundle.tables}) == 2
    assert all("duplicate" in q.todo for q in bundle.queries)
    assert bundle.knowledge


def test_rerun_preserves_hand_edits_and_layout(tmp_path):
    root = project(tmp_path)
    out = tmp_path / "output"
    out.mkdir()
    (out / "unshackle.toml").write_text('[paths]\nqueries = "reference"\ncatalog = "tables"\n')
    build.apply(parse(root), out)
    before = {p.relative_to(out): p.read_bytes() for p in out.rglob("*") if p.is_file()}
    build.apply(parse(root), out)
    after = {p.relative_to(out): p.read_bytes() for p in out.rglob("*") if p.is_file()}
    # Report distinguishes new writes from unchanged writes; imported artifacts stay stable.
    for p in before:
        if str(p).startswith(("reference/", "tables/", "semantic/", "knowledge/")):
            assert after[p] == before[p]
    q = out / "reference/dbt-orders.sql"
    q.write_text("-- hand edit\nselect 42\n")
    build.apply(parse(root), out)
    assert q.read_text() == "-- hand edit\nselect 42\n"


def test_unsupported_semantics_preserve_full_source(tmp_path):
    root = project(tmp_path)
    p = root / "transforms/schema.yml"
    doc = yaml.safe_load(p.read_text())
    doc["semantic_models"][0]["measures"][0]["non_additive_dimension"] = {"name": "ordered_at"}
    doc["semantic_models"][0]["entities"][0]["expr"] = "{{ var('key') }}"
    p.write_text(yaml.safe_dump(doc))
    b = parse(root)
    assert b.semantic_models[0].todo
    assert b.semantic_models[0].measures[0].todo
    assert "non_additive_dimension" in b.semantic_models[0].measures[0].source_expression
    assert "var(" in b.knowledge[0].body and "key" in b.knowledge[0].body


def test_paths_cannot_escape_and_profiles_are_not_loaded(tmp_path):
    root = project(tmp_path)
    (root / "profiles.yml").write_text("!!python/object/apply:os.system ['false']")
    assert parse(root).tables
    (root / "dbt_project.yml").write_text("model-paths: [../outside]\n")
    with pytest.raises(ValueError, match="escapes"):
        parse(root)


def test_generated_semantic_files_are_not_reimported(tmp_path):
    root = project(tmp_path)
    (root / "transforms/generated.yml").write_text("# Generated by unshackle from dbt.\nsemantic_models:\n- name: duplicate\n")
    assert len(parse(root).semantic_models) == 1


@pytest.mark.skipif(not os.environ.get("UNSHACKLE_DBT"), reason="set UNSHACKLE_DBT to a dbt binary")
def test_generated_metricflow_passes_dbt_parse(tmp_path):
    root = project(tmp_path)
    out = tmp_path / "output"
    out.mkdir()
    (out / "dbt_project.yml").write_text("name: example\nversion: '1.0'\nprofile: example\n")
    (out / "profiles.yml").write_text("example:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n      path: ':memory:'\n")
    build.apply(parse(root), out)
    (out / "models/orders.sql").write_text("select 1 as order_id, current_date as ordered_at, 10 as amount")
    (out / "models/metricflow_time_spine.sql").write_text("select current_date as date_day")
    (out / "models/time_spine.yml").write_text("models:\n- name: metricflow_time_spine\n  time_spine:\n    standard_granularity_column: date_day\n  columns:\n  - name: date_day\n    granularity: day\n")
    result = subprocess.run([os.environ["UNSHACKLE_DBT"], "parse", "--project-dir", str(out), "--profiles-dir", str(out)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_cli_import_writes_immediately(tmp_path):
    from click.testing import CliRunner
    from unshackle.cli import main

    root = project(tmp_path)
    out = tmp_path / "output"
    result = CliRunner().invoke(main, ["import", "dbt", str(root), "--repo", str(out)])
    assert result.exit_code == 0, result.output
    assert (out / "catalog/warehouse/main/orders.yml").is_file()
    assert (out / "queries/dbt-orders.sql").is_file()


def test_metrics_in_separate_earlier_file_resolve(tmp_path):
    root = project(tmp_path)
    p = root / "transforms/schema.yml"
    doc = yaml.safe_load(p.read_text())
    (root / "transforms/a_metrics.yml").write_text(yaml.safe_dump({"metrics": doc.pop("metrics")}))
    p.write_text(yaml.safe_dump(doc))
    assert not parse(root).metrics[0].todo


def test_unsupported_or_orphan_metric_always_keeps_source(tmp_path):
    root = project(tmp_path)
    (root / "transforms/orphan.yml").write_text("metrics:\n- name: orphan\n  type: simple\n  type_params:\n    measure: missing\n")
    b = parse(root)
    assert any("orphan" in k.body and "missing" in k.body for k in b.knowledge)


def test_supported_metric_of_unsupported_measure_stays_pending(tmp_path):
    root = project(tmp_path)
    p = root / "transforms/schema.yml"
    doc = yaml.safe_load(p.read_text())
    doc["semantic_models"][0]["measures"][0]["agg"] = "custom_aggregate"
    p.write_text(yaml.safe_dump(doc))
    out = tmp_path / "output"
    build.apply(parse(root), out)
    text = (out / "semantic/orders_semantic.ossie.yaml").read_text()
    rendered = yaml.safe_load(text)
    assert not any(m.get("name") == "revenue" for m in rendered.get("metrics", []))
    assert "TODO(unshackle)" in text and "custom_aggregate" in text
