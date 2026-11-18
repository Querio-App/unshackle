"""Offline schema drift, SQL references and combined import refresh behavior."""
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest
import yaml

from unshackle.drift import markdown, query_references, refresh, schema_diff

FIXTURE = Path(__file__).parent / "fixtures/drift"
BEFORE = json.loads((FIXTURE / "before.json").read_text())
AFTER = json.loads((FIXTURE / "after.json").read_text())
TABLE = "catalog/warehouse/main/orders.yml"


def test_rename_is_removed_plus_added_without_guess():
    changes = schema_diff(BEFORE, AFTER)
    assert [(c["kind"], c["column"]) for c in changes] == [("column_removed", "amount"), ("column_added", "revenue")]
    assert schema_diff(AFTER, AFTER) == []


def test_type_keys_comments_and_table_changes():
    after = json.loads(json.dumps(AFTER))
    after["tables"][TABLE]["columns"][0]["nullable"] = True
    after["tables"][TABLE]["primary_key"] = ["order_id"]
    assert {c["kind"] for c in schema_diff(AFTER, after)} == {"column_changed", "table_metadata_changed"}
    assert schema_diff(AFTER, {"tables": {}})[0]["kind"] == "table_removed"


@pytest.mark.parametrize("sql", [
    "select amount from orders",
    "select o.amount from main.orders o",
    "with totals as (select sum(amount) as total from main.orders) select total from totals",
    "select sum(amount) from main.orders union all select sum(amount) from main.orders",
])
def test_missing_column_static_scopes(sql):
    assert not any(f["kind"] == "missing_column" for f in query_references(sql, BEFORE))
    assert any(f.get("column") == "amount" and f["kind"] == "missing_column" for f in query_references(sql, AFTER))


def test_output_alias_and_star_are_not_missing_columns():
    assert query_references("select revenue as amount from orders order by amount", AFTER) == []
    assert query_references("select * from orders", AFTER) == []


@pytest.mark.parametrize("sql", ["select {{ var('column') }} from orders", "select x from unknown_table", "select 1; select 2", "delete from orders", "select ("])
def test_uncertain_queries_are_explicit(sql):
    findings = query_references(sql, AFTER)
    assert findings and all(f["kind"] == "uncertain" for f in findings)


def test_ambiguous_tables_do_not_claim_missing_column():
    snapshot = json.loads(json.dumps(AFTER))
    snapshot["tables"]["catalog/warehouse/other/orders.yml"] = AFTER["tables"][TABLE]
    assert all(f["kind"] == "uncertain" for f in query_references("select x from orders", snapshot))


def setup_repo(tmp_path, monkeypatch):
    repo = tmp_path / "context"
    repo.mkdir()
    db = tmp_path / "warehouse.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("create table orders (order_id integer primary key, amount numeric)")
    monkeypatch.setenv("UNSHACKLE_WAREHOUSE_URL", f"sqlite:///{db}")
    (repo / "input").mkdir()
    (repo / "input/revenue.sql").write_text("select sum(amount) as total from main.orders")
    (repo / "input/dynamic.py").write_text('column = "amount"\nquery = f"select {column} from orders"\nraise RuntimeError("must never execute")\n')
    shutil.copytree(Path(__file__).parent / "fixtures/import_dbt/plain", repo / "dbt")
    config = {"imports": [
        {"source": "warehouse"}, {"source": "dbt", "paths": ["dbt"]}, {"source": "sql", "paths": ["input"]},
    ]}
    return repo, db, config


def test_combined_import_repeat_then_rename(tmp_path, monkeypatch):
    repo, db, config = setup_repo(tmp_path, monkeypatch)
    first = refresh(repo, config)
    assert first["changed"]
    assert any("notebooks/" in p for p in first["paths"])
    assert "migration/warehouse-schema.json" in first["paths"]
    assert "migration/unshackle.lock" in first["paths"]
    assert "migration/report.md" not in first["paths"]
    second = refresh(repo, config)
    assert not second["changed"], second["paths"]
    assert second["changes"] == []
    assert any(r["kind"] == "uncertain" for r in second["references"])
    with sqlite3.connect(db) as conn:
        conn.execute("alter table orders rename column amount to revenue")
    third = refresh(repo, config)
    assert third["changed"]
    assert {(c["kind"], c.get("column")) for c in third["changes"]} >= {("column_removed", "amount"), ("column_added", "revenue")}
    assert any(r.get("column") == "amount" and r["path"].endswith(".ipynb") for r in third["references"])
    body = markdown(third)
    assert "missing column amount" in body and "uncertain" in body
    assert "sqlite:///" not in body
    assert not refresh(repo, config)["changed"]


def test_layout_hand_edits_and_no_report_churn(tmp_path, monkeypatch):
    repo, _, config = setup_repo(tmp_path, monkeypatch)
    (repo / "unshackle.toml").write_text('[paths]\ncatalog="tables"\nnotebooks="reports"\nqueries="reference"\n')
    (repo / "migration").mkdir()
    (repo / "migration/report.md").write_text("Human migration notes\n")
    refresh(repo, config)
    query = repo / "reference/dbt-orders.sql"
    query.write_text("-- hand edited\nselect 42")
    result = refresh(repo, config)
    assert not result["changed"], result["paths"]
    assert query.read_text() == "-- hand edited\nselect 42"
    assert (repo / "migration/report.md").read_text() == "Human migration notes\n"
    assert (repo / "tables/warehouse/main/orders.yml").is_file()


@pytest.mark.parametrize("config", [
    {"imports": []},
    {"imports": [{"source": "warehouse"}, {"source": "warehouse"}]},
    {"imports": [{"source": "warehouse", "url": "secret"}]},
    {"imports": [{"source": "warehouse"}, {"source": "sql", "paths": ["../outside"]}]},
])
def test_invalid_config_rejected_before_writes(tmp_path, config):
    with pytest.raises(ValueError):
        refresh(tmp_path, config)
    assert not list(tmp_path.iterdir())


def test_cli_report_outside_repo_and_github_outputs(tmp_path, monkeypatch):
    repo, _, config = setup_repo(tmp_path, monkeypatch)
    (repo / "migration").mkdir()
    (repo / "migration/imports.json").write_text(json.dumps(config))
    output = tmp_path / "outputs"
    report = tmp_path / "report.md"
    cmd = [sys.executable, "-m", "unshackle.drift", "--repo", str(repo), "--report", str(report), "--github-output", str(output)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert output.read_text().startswith("changed=true\npaths<<")
    assert "# Warehouse drift" in report.read_text()
    output.unlink()
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert output.read_text().startswith("changed=false\n")


def test_cli_errors_do_not_expose_credentials(tmp_path, monkeypatch):
    (tmp_path / "migration").mkdir()
    (tmp_path / "migration/imports.json").write_text('{"imports":[{"source":"warehouse"}]}')
    monkeypatch.setenv("UNSHACKLE_WAREHOUSE_URL", "unknown://example:SECRET@example.invalid/db")
    result = subprocess.run([sys.executable, "-m", "unshackle.drift", "--repo", str(tmp_path), "--report", str(tmp_path.parent / "drift-report.md")], capture_output=True, text=True)
    assert result.returncode == 1
    assert "SECRET" not in result.stdout + result.stderr
    assert "unknown://" not in result.stderr


def test_workflow_is_inactive_and_uses_one_branch_and_precise_paths():
    root = Path(__file__).parents[1]
    path = root / "examples/workflows/unshackle-drift.yml"
    assert not (root / ".github/workflows/unshackle-drift.yml").exists()
    workflow = yaml.safe_load(path.read_text())
    triggers = workflow.get("on", workflow.get(True))
    assert "schedule" in triggers and "pull_request" not in triggers
    steps = workflow["jobs"]["drift"]["steps"]
    action = next(s for s in steps if "create-pull-request" in s.get("uses", ""))
    assert action["with"]["branch"] == "automation/unshackle-drift"
    assert action["with"]["add-paths"] == "${{ steps.refresh.outputs.paths || 'migration/warehouse-schema.json' }}"
    assert "if" not in action
    assert action["with"]["delete-branch"] is True
    assert "branch-suffix" not in action["with"]


def test_case_normalization_and_table_column_alias_uncertainty():
    assert query_references("SELECT REVENUE FROM MAIN.ORDERS", AFTER, dialect="postgres") == []
    findings = query_references("select renamed from orders as o(renamed, revenue)", AFTER)
    assert findings and all(f["kind"] == "uncertain" for f in findings)


def test_join_missing_column_is_checked_against_its_table():
    snapshot = json.loads(json.dumps(AFTER))
    snapshot["tables"]["catalog/warehouse/main/customers.yml"] = {"name": "customers", "columns": [{"name": "name"}, {"name": "order_id"}]}
    sql = "select o.amount, c.name from orders o join customers c on c.order_id = o.order_id"
    assert query_references(sql, snapshot) == [{"kind": "missing_column", "column": "amount", "tables": [TABLE]}]


def test_plain_sql_other_datasource_header_is_uncertain(tmp_path, monkeypatch):
    from unshackle.drift import reference_report
    from unshackle.mcp.repository import Repository

    (tmp_path / "queries").mkdir()
    (tmp_path / "queries/other.sql").write_text("-- name: Another connection\n-- datasource: other\nselect amount from orders\n")
    original = Repository.read

    def read_without_datasource(self, path):
        result = original(self, path)
        result.pop("datasource", None)
        return result

    monkeypatch.setattr(Repository, "read", read_without_datasource)
    findings = reference_report(tmp_path, AFTER)
    assert findings == [{"path": "queries/other.sql", "kind": "uncertain", "reason": "query belongs to another datasource"}]


def test_invalid_alias_scope_does_not_abort_report():
    findings = query_references("select o.amount from orders o join orders o on true", AFTER)
    assert findings and all(f["kind"] == "uncertain" for f in findings)


def test_return_to_committed_baseline_has_no_diff_and_narrow_cleanup_path(tmp_path, monkeypatch):
    repo, db, config = setup_repo(tmp_path, monkeypatch)
    refresh(repo, config)
    # Scheduled runs start from the default branch, not the open PR branch.
    baseline = {p.relative_to(repo): p.read_bytes() for p in repo.rglob("*") if p.is_file()}
    with sqlite3.connect(db) as conn:
        conn.execute("alter table orders rename column amount to revenue")
    assert refresh(repo, config)["changed"]
    for path in repo.rglob("*"):
        if path.is_file() and path.relative_to(repo) not in baseline:
            path.unlink()
    for path, content in baseline.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_bytes(content)
    with sqlite3.connect(db) as conn:
        conn.execute("alter table orders rename column revenue to amount")
    (repo / ".env").write_text("EXAMPLE_SECRET=not-a-real-secret\n")
    result = refresh(repo, config)
    assert not result["changed"] and result["paths"] == []
    assert (repo / "migration/warehouse-schema.json").is_file()
    # The template's explicit fallback stages only this unchanged snapshot, so
    # create-pull-request receives no diff and can close its stale branch PR.


def test_join_using_key_is_explicitly_uncertain_before_and_after_rename():
    import duckdb

    conn = duckdb.connect()
    try:
        conn.execute("create table orders(order_id int, amount int)")
        conn.execute("create table customers(order_id int, amount int)")
        conn.execute("insert into orders values (1, 10)")
        conn.execute("insert into customers values (1, 10)")
        sql = "select * from orders join customers using (amount)"
        assert conn.execute(sql).fetchall() == [(1, 10, 1)]
        before = json.loads(json.dumps(BEFORE))
        before["tables"]["catalog/warehouse/main/customers.yml"] = BEFORE["tables"][TABLE] | {"name": "customers"}
        assert any("JOIN USING" in f.get("reason", "") for f in query_references(sql, before, dialect="duckdb"))
        conn.execute("alter table orders rename column amount to revenue")
        with pytest.raises(duckdb.BinderException):
            conn.execute(sql)
        after = json.loads(json.dumps(before))
        after["tables"][TABLE] = AFTER["tables"][TABLE]
        findings = query_references(sql, after, dialect="duckdb")
        assert any(f["kind"] == "uncertain" and "JOIN USING" in f["reason"] for f in findings)
    finally:
        conn.close()


def test_valid_whole_row_projection_is_uncertain_not_missing():
    import duckdb

    with duckdb.connect() as conn:
        conn.execute("create table orders(order_id int, revenue int)")
        conn.execute("insert into orders values (1, 10)")
        assert conn.execute("select o from orders o").fetchall() == [({"order_id": 1, "revenue": 10},)]
    findings = query_references("select o from orders o", AFTER, dialect="duckdb")
    assert findings and all(f["kind"] == "uncertain" for f in findings)
    assert "whole-row" in findings[0]["reason"]


@pytest.mark.parametrize("column", ["ctid", "tableoid", "xmin", "cmin", "xmax", "cmax"])
def test_postgres_implicit_system_columns_are_not_claimed_missing(column):
    findings = query_references(f"select o.{column} from orders o", AFTER, dialect="postgres")
    assert findings and all(f["kind"] == "uncertain" for f in findings)
    assert "system column" in findings[0]["reason"]
