import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import sqlalchemy as sa
import yaml

from unshackle import build
from unshackle.imports import warehouse
from unshackle.imports.catalog_refresh import SNAPSHOT
from unshackle.ir import Bundle, Column, Table


def sample():
    return Bundle(tool="warehouse", tables=[Table(
        "warehouse", "public", "orders", "Orders from warehouse",
        [Column("id", "INTEGER", nullable=False), Column("amount", "NUMERIC", "Amount")],
        source_ref="warehouse:public.orders", primary_key=["id"],
    )])


def catalog(repo):
    return repo / "catalog/warehouse/public/orders.yml"


def test_reflect_sqlite_and_no_connection_identity(tmp_path):
    url = f"sqlite:///{tmp_path / 'example.db'}"
    engine = sa.create_engine(url)
    with engine.begin() as con:
        con.exec_driver_sql("create table parent (b integer, a integer, primary key (b,a))")
        con.exec_driver_sql("create table child (z integer, y integer, foreign key(z,y) references parent(b,a))")
    engine.dispose()
    bundle = warehouse.introspect(url)
    assert [t.name for t in bundle.tables] == ["child", "parent"]
    assert bundle.tables[1].primary_key == ["b", "a"]
    assert bundle.tables[0].foreign_keys == [{"columns": ["z", "y"], "referred_schema": "main",
                                           "referred_table": "parent", "referred_columns": ["b", "a"]}]
    assert [c.name for c in bundle.tables[0].columns] == ["y", "z"]
    assert str(tmp_path) not in bundle.to_json()
    assert "get_table_comment is unsupported" in bundle.warnings[0].message
    assert warehouse.introspect(url).to_json() == bundle.to_json()


def test_comments_keys_sorting_and_system_schemas():
    inspector = Mock()
    inspector.get_schema_names.return_value = ["public", "pg_catalog", "information_schema", "pg_temp_1"]
    inspector.get_table_names.return_value = ["orders"]
    inspector.get_columns.return_value = [{"name": "z", "type": "TEXT", "nullable": True, "comment": "Last"},
                                         {"name": "a", "type": "INTEGER", "nullable": False}]
    inspector.get_pk_constraint.return_value = {"constrained_columns": ["z", "a"]}
    inspector.get_foreign_keys.return_value = [{"constrained_columns": ["z", "a"], "referred_schema": "lookup",
                                               "referred_table": "keys", "referred_columns": ["second", "first"]}]
    inspector.get_table_comment.return_value = {"text": "Warehouse comments"}
    bundle = warehouse._reflect(inspector, "postgresql")
    assert len(bundle.tables) == 1 and bundle.warehouse == "postgres"
    table = bundle.tables[0]
    assert table.description == "Warehouse comments"
    assert table.columns[1].description == "Last"
    assert table.primary_key == ["z", "a"]
    assert table.foreign_keys[0]["referred_columns"] == ["second", "first"]


def test_permission_failure_does_not_leak_or_return_partial(monkeypatch):
    engine = Mock()
    engine.connect.side_effect = RuntimeError("password=example-secret host=private.example.invalid")
    monkeypatch.setattr(warehouse, "create_engine", lambda *a, **k: engine)
    with pytest.raises(ValueError, match="No partial schema") as error:
        warehouse.introspect("postgresql://example:example-secret@private.example.invalid/db")
    assert "example-secret" not in str(error.value)
    assert "private.example" not in str(error.value)
    assert error.value.__suppress_context__
    engine.dispose.assert_called_once()


def test_refresh_removes_stale_columns_preserves_annotations_and_keys(tmp_path):
    build.apply(sample(), tmp_path)
    path = catalog(tmp_path)
    doc = yaml.safe_load(path.read_text())
    doc["description"] = "Manual table docs"
    doc["owner"] = "example team"
    for column in doc["columns"]:
        column["description"] = "Manual identifier" if column["name"] == "id" else "Manual amount note"
    doc["columns"].append({"name": "dbt_expression", "description": "Other import"})
    path.write_text(yaml.safe_dump(doc))
    fresh = sample()
    fresh.tables[0].columns = [Column("id", "BIGINT", nullable=True), Column("net_amount", "NUMERIC")]
    fresh.tables[0].primary_key = []
    build.apply(fresh, tmp_path)
    updated = yaml.safe_load(path.read_text())
    assert updated["description"] == "Manual table docs"
    assert updated["owner"] == "example team"
    assert [c["name"] for c in updated["columns"]] == ["id", "dbt_expression", "net_amount"]
    assert updated["columns"][0] == {"name": "id", "type": "BIGINT", "nullable": True, "description": "Manual identifier"}
    assert "primary_key" not in updated
    assert updated["removed_columns"][0]["description"] == "Manual amount note"
    assert "TODO(unshackle)" in updated["removed_columns"][0]["todo"]
    before = path.read_bytes(), (tmp_path / SNAPSHOT).read_bytes()
    build.apply(fresh, tmp_path)
    assert before == (path.read_bytes(), (tmp_path / SNAPSHOT).read_bytes())
    snap = json.loads((tmp_path / SNAPSHOT).read_text())
    assert "Manual" not in json.dumps(snap)
    assert "amount" not in [c["name"] for c in next(iter(snap["tables"].values()))["columns"]]


def test_repeated_combined_imports_keep_other_data(tmp_path):
    other = Bundle(tool="dbt", tables=[Table("warehouse", "public", "orders", columns=[
        Column("id", description="dbt identifier"), Column("calculated", description="dbt column")])])
    for _ in range(3):
        build.apply(sample(), tmp_path)
        build.apply(other, tmp_path)
    before = catalog(tmp_path).read_bytes()
    build.apply(sample(), tmp_path)
    assert catalog(tmp_path).read_bytes() == before
    build.apply(other, tmp_path)
    assert catalog(tmp_path).read_bytes() == before
    doc = yaml.safe_load(before)
    assert next(c for c in doc["columns"] if c["name"] == "calculated")["description"] == "dbt column"


def test_layout_override_and_removed_table(tmp_path):
    (tmp_path / "unshackle.toml").write_text('[paths]\ncatalog = "context/schema"\n')
    build.apply(sample(), tmp_path)
    path = tmp_path / "context/schema/warehouse/public/orders.yml"
    assert path.exists()
    build.apply(Bundle(tool="warehouse"), tmp_path)
    assert not path.exists()
    assert json.loads((tmp_path / SNAPSHOT).read_text())["tables"] == {}


def test_collision_fails_before_writes(tmp_path):
    bundle = sample()
    bundle.tables = [Table("warehouse", "public", "a/b"), Table("warehouse", "public", "a_b")]
    with pytest.raises(ValueError, match="collide"):
        build.apply(bundle, tmp_path)
    assert not (tmp_path / SNAPSHOT).exists()


def test_removed_table_annotations_survive_multiple_refreshes(tmp_path):
    build.apply(sample(), tmp_path)
    path = catalog(tmp_path)
    doc = yaml.safe_load(path.read_text())
    doc["owner"] = "example team"
    path.write_text(yaml.safe_dump(doc))
    for _ in range(3):
        build.apply(Bundle(tool="warehouse"), tmp_path)
        retained = yaml.safe_load(path.read_text())
        assert retained["owner"] == "example team"
        assert retained["columns"] == []
        assert "TODO(unshackle)" in retained["todo"]


def test_cli_sqlite_rename_and_deterministic_refresh(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from unshackle.cli import main
    url = f"sqlite:///{tmp_path / 'example.db'}"
    engine = sa.create_engine(url)
    with engine.begin() as con:
        con.exec_driver_sql("create table orders (id integer primary key, amount integer)")
    monkeypatch.setenv("UNSHACKLE_WAREHOUSE_URL", url)
    repo = tmp_path / "repo"
    runner = CliRunner()
    result = runner.invoke(main, ["import", "warehouse", "--repo", str(repo)])
    assert result.exit_code == 0, result.output
    path = repo / "catalog/warehouse/main/orders.yml"
    original = path.read_bytes()
    assert runner.invoke(main, ["import", "warehouse", "--repo", str(repo)]).exit_code == 0
    assert path.read_bytes() == original
    with engine.begin() as con:
        con.exec_driver_sql("alter table orders rename column amount to net_amount")
    engine.dispose()
    assert runner.invoke(main, ["import", "warehouse", "--repo", str(repo)]).exit_code == 0
    assert {c["name"] for c in yaml.safe_load(path.read_text())["columns"]} == {"id", "net_amount"}


def test_combined_import_refreshes_nullability_and_clears_keys(tmp_path):
    wh = sample()
    wh.tables[0].foreign_keys = [{"columns": ["id"], "referred_schema": "public",
                                 "referred_table": "parent", "referred_columns": ["id"]}]
    dbt = Bundle(tool="dbt", tables=[Table("warehouse", "public", "orders", columns=[Column("id")])])
    build.apply(wh, tmp_path)
    build.apply(dbt, tmp_path)
    wh.tables[0].primary_key = []
    wh.tables[0].foreign_keys = []
    wh.tables[0].columns[0].nullable = True
    for _ in range(2):
        build.apply(wh, tmp_path)
        build.apply(dbt, tmp_path)
        doc = yaml.safe_load(catalog(tmp_path).read_text())
        assert "primary_key" not in doc and "foreign_keys" not in doc
        assert next(c for c in doc["columns"] if c["name"] == "id")["nullable"] is True
