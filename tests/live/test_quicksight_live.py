"""QuickSight, live: a real Amazon QuickSight account over the Dunder Mifflin data in Athena.

Unlike the Docker-based live tests this one needs an AWS account (see tests/live/README.md, "QuickSight"):

1. the Dunder Mifflin Parquet files (plus a small RLS rules table) uploaded to S3 and registered as Glue tables
   through Athena DDL: Athena is the QuickSight data source, in direct query mode (no SPICE)
2. QuickSight content created through the API from the offline fixture (tests/fixtures/quicksight/bundle):
   CreateDataSource, CreateDataSet x3, CreateAnalysis and CreateDashboard with `Definition`, groups for its permissions
3. `unshackle init / extract --only dashboard:... --only analysis:... / build quicksight` in a temp repo: the extract
   runs the real asset bundle export (StartAssetBundleExportJob, QUICKSIGHT_JSON)
4. parity with `unshackle verify` on Athena (PyAthena's SQLAlchemy dialect). Source numbers: reference SQL over the same
   Parquet files in DuckDB, written from QuickSight's documented semantics, because QuickSight has no API that returns a
   visual's data without the Pixel Perfect add-on. With QUICKSIGHT_LIVE_SNAPSHOTS=1 (needs that add-on and capacity
   pricing for anonymous users) the table visual is also compared with QuickSight's own CSV (StartDashboardSnapshotJob).

Skipped unless UNSHACKLE_LIVE=1 and QUICKSIGHT_ACCOUNT_ID are set. It has not run yet: no account was available
when it was written. QUICKSIGHT_LIVE_SAVE_FIXTURE=1 copies the real bundle to tests/live/fixtures/quicksight/, which
should replace the hand-written fixture.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import pytest
import yaml
from harness import DATA, ENABLED, LIVE, SKIP_REASON, run_unshackle, wait_for, write_case

ACCOUNT = os.environ.get("QUICKSIGHT_ACCOUNT_ID", "")
REGION = os.environ.get("QUICKSIGHT_REGION", "us-east-1")
IDENTITY_REGION = os.environ.get("QUICKSIGHT_IDENTITY_REGION", REGION)
BUCKET = os.environ.get("QUICKSIGHT_LIVE_BUCKET", "")
PREFIX = os.environ.get("QUICKSIGHT_LIVE_PREFIX", "unshackle-live")
WORKGROUP = os.environ.get("QUICKSIGHT_LIVE_WORKGROUP", "primary")
OWNER = os.environ.get("QUICKSIGHT_LIVE_OWNER", "")  # arn:aws:quicksight:<identity region>:<account>:user/default/<name>
DATABASE = "dunder_mifflin"  # the fixture's schema
FIXTURE = Path(__file__).parents[1] / "fixtures" / "quicksight" / "bundle"
GROUPS = ("Sales", "Analysts", "Finance")  # principals the fixture shares with

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not ENABLED, reason=SKIP_REASON),
    pytest.mark.skipif(not (ACCOUNT and BUCKET and OWNER),
                       reason="QuickSight live test: set QUICKSIGHT_ACCOUNT_ID, QUICKSIGHT_LIVE_BUCKET and QUICKSIGHT_LIVE_OWNER "
                              "(tests/live/README.md)"),
]

OWNER_ACTIONS = {
    "datasource": ["DescribeDataSource", "DescribeDataSourcePermissions", "PassDataSource", "UpdateDataSource",
                   "DeleteDataSource", "UpdateDataSourcePermissions"],
    "dataset": ["DescribeDataSet", "DescribeDataSetPermissions", "PassDataSet", "DescribeIngestion", "ListIngestions",
                "UpdateDataSet", "DeleteDataSet", "CreateIngestion", "CancelIngestion", "UpdateDataSetPermissions"],
    "analysis": ["RestoreAnalysis", "UpdateAnalysisPermissions", "DeleteAnalysis", "DescribeAnalysisPermissions",
                 "QueryAnalysis", "DescribeAnalysis", "UpdateAnalysis"],
    "dashboard": ["DescribeDashboard", "ListDashboardVersions", "UpdateDashboardPermissions", "QueryDashboard",
                  "UpdateDashboard", "DeleteDashboard", "DescribeDashboardPermissions", "UpdateDashboardPublishedVersion"],
}
RULES = [("jhalpert", None, "Northeast"), (None, "Sales", None)]  # region_access_rules: UserName, GroupName, shipping_region

# What QuickSight computes for each visual, over the Parquet files (views named after the tables). {status} is where
# the dashboard's Status parameter filter goes; the bar is on the Shipped orders dataset, which that filter doesn't reach.
REFERENCE = {
    "total_revenue": "select sum(o.post_discount_revenue) as post_discount_revenue from orders o where o.status <> 'TEST' {status}",
    "monthly_revenue": "select date_trunc('month', o.purchased_at) as purchased_at, sum(o.post_discount_revenue) as post_discount_revenue "
                       "from orders o where o.status <> 'TEST' {status} group by 1",
    "revenue_by_account": "select a.account_name, sum(o.post_discount_revenue) as revenue, "
                          "sum(o.post_discount_revenue) / count(o.order_id) as average_order_value, "
                          "count(distinct o.order_id) as order_id "
                          "from orders o left join accounts a on o.account_id = a.account_id "
                          "where o.status <> 'TEST' and o.quantity >= 1 {status} group by 1",
    "revenue_and_orders": "select date_trunc('month', o.purchased_at) as purchased_at, sum(o.post_discount_revenue) as post_discount_revenue, "
                          "count(o.order_id) as order_id from orders o where o.status <> 'TEST' {status} group by 1",
    "revenue_by_region": "select s.shipping_region, s.shipping_mode, sum(o.post_discount_revenue) as post_discount_revenue "
                         "from orders o join shippings s on o.shipping_id = s.shipping_id where s.shipping_mode is not null group by 1, 2",
}
KEYS = {"monthly_revenue": ["purchased_at"], "revenue_by_account": ["account_name"], "revenue_and_orders": ["purchased_at"],
        "revenue_by_region": ["shipping_region", "shipping_mode"]}
METRICS = {
    "revenue": "select sum(post_discount_revenue) as revenue from orders where status <> 'TEST'",
    "order_count": "select count(order_id) as order_count from orders where status <> 'TEST'",
    "completed_revenue": "select sum(case when status = 'COMPLETED' then post_discount_revenue end) as completed_revenue "
                         "from orders where status <> 'TEST'",
    "average_order_value": "select sum(post_discount_revenue) / count(order_id) as average_order_value from orders where status <> 'TEST'",
}


def test_quicksight_end_to_end(tmp_path: Path):
    import boto3

    session = boto3.Session(region_name=REGION)
    aws = _Aws(session)
    try:
        aws.load_data(tmp_path)
        aws.create_content()
        _extract_build_verify(aws, tmp_path)
    finally:
        if os.environ.get("UNSHACKLE_LIVE_KEEP") != "1":
            aws.cleanup()


class _Aws:
    def __init__(self, session: Any):
        self.qs = session.client("quicksight", region_name=REGION)
        self.qs_identity = session.client("quicksight", region_name=IDENTITY_REGION)
        self.athena = session.client("athena", region_name=REGION)
        self.s3 = session.client("s3", region_name=REGION)
        self.results = f"s3://{BUCKET}/{PREFIX}/athena-results/"

    # ------------------------------------------------------------------ S3 + Glue (through Athena DDL)

    def athena_sql(self, sql: str) -> None:
        qid = self.athena.start_query_execution(QueryString=sql, WorkGroup=WORKGROUP,
                                                ResultConfiguration={"OutputLocation": self.results})["QueryExecutionId"]
        state = wait_for(lambda: _done(self.athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]),
                         f"Athena: {sql[:60]}", 300, 1)
        assert state == "SUCCEEDED", f"Athena {state}: {sql}"

    def load_data(self, tmp: Path) -> None:
        import duckdb

        rules = tmp / "region_access_rules.parquet"
        con = duckdb.connect()
        con.execute("create table r (\"UserName\" varchar, \"GroupName\" varchar, shipping_region varchar)")
        con.executemany("insert into r values (?, ?, ?)", RULES)
        con.execute(f"copy r to '{rules}' (format parquet)")
        files = sorted(DATA.glob("dunder_mifflin/*.parquet")) + [rules]
        self.athena_sql(f"create database if not exists {DATABASE}")
        for path in files:
            table = path.stem
            key = f"{PREFIX}/data/{table}/{path.name}"
            self.s3.upload_file(str(path), BUCKET, key)
            described = con.execute(f"describe select * from read_parquet('{path}')").fetchall()  # (name, type, ...)
            cols = ", ".join(f"`{row[0]}` {_hive_type(str(row[1]))}" for row in described)
            self.athena_sql(f"drop table if exists {DATABASE}.{table}")
            self.athena_sql(f"create external table {DATABASE}.{table} ({cols}) stored as parquet "
                            f"location 's3://{BUCKET}/{PREFIX}/data/{table}/'")
        con.close()

    # ------------------------------------------------------------------ QuickSight content

    def assets(self) -> dict[str, list[dict[str, Any]]]:
        """The fixture's assets as Create* requests for this account (keys in the model's casing)."""
        import botocore.session

        model = botocore.session.get_session().get_service_model("quicksight")
        ops = {"datasource": "CreateDataSource", "dataset": "CreateDataSet", "analysis": "CreateAnalysis", "dashboard": "CreateDashboard"}
        out: dict[str, list[dict[str, Any]]] = {k: [] for k in ops}
        for path in sorted(FIXTURE.rglob("*.json")):
            kind = path.parent.name
            if kind not in ops:
                continue
            text = path.read_text().replace("111122223333", ACCOUNT).replace(":us-east-1:", f":{REGION}:")
            raw = json.loads(text)
            raw.pop("resourceType", None)
            doc = _to_model(raw, model.operation_model(ops[kind]).input_shape)
            perms = []
            for p in doc.get("Permissions") or []:
                principal = re.sub(r"^arn:aws:quicksight:[^:]+:", f"arn:aws:quicksight:{IDENTITY_REGION}:", p["Principal"])
                if ":user/" not in principal:
                    perms.append({**p, "Principal": principal})
            perms.append({"Principal": OWNER, "Actions": [f"quicksight:{a}" for a in OWNER_ACTIONS[kind]]})
            doc["Permissions"] = perms
            for rule in doc.get("ColumnLevelPermissionRules") or []:
                rule["Principals"] = [re.sub(r"^arn:aws:quicksight:[^:]+:", f"arn:aws:quicksight:{IDENTITY_REGION}:", p)
                                      for p in rule["Principals"]]
            if kind == "datasource":
                doc["DataSourceParameters"] = {"AthenaParameters": {"WorkGroup": WORKGROUP}}
            out[kind].append(doc)
        out["dataset"].sort(key=lambda d: d.get("UseAs") != "RLS_RULES")  # the rules dataset before the dataset it protects
        return out

    def create_content(self) -> None:
        for g in GROUPS:
            try:
                self.qs_identity.create_group(AwsAccountId=ACCOUNT, Namespace="default", GroupName=g)
            except self.qs_identity.exceptions.ResourceExistsException:
                pass
        assets = self.assets()
        self.cleanup(quiet=True)
        for doc in assets["datasource"]:
            self.qs.create_data_source(AwsAccountId=ACCOUNT, **doc)
            wait_for(lambda d=doc: _status(self.qs.describe_data_source(AwsAccountId=ACCOUNT, DataSourceId=d["DataSourceId"])["DataSource"]),
                     f"data source {doc['DataSourceId']}", 300)
        for doc in assets["dataset"]:
            self.qs.create_data_set(AwsAccountId=ACCOUNT, **doc)
        for kind, create, describe, key, inner in (
                ("analysis", self.qs.create_analysis, self.qs.describe_analysis, "AnalysisId", lambda r: r["Analysis"]),
                ("dashboard", self.qs.create_dashboard, self.qs.describe_dashboard, "DashboardId", lambda r: r["Dashboard"]["Version"])):
            for doc in assets[kind]:
                create(AwsAccountId=ACCOUNT, **doc)
                status = wait_for(lambda d=doc: _status(inner(describe(AwsAccountId=ACCOUNT, **{key: d[key]}))),
                                  f"{kind} {doc[key]}", 600, 5)
                assert status.endswith("SUCCESSFUL"), f"{kind} {doc[key]}: {inner(describe(AwsAccountId=ACCOUNT, **{key: doc[key]}))}"

    def cleanup(self, quiet: bool = False) -> None:
        assets = self.assets()
        calls: list[Callable[[], Any]] = []
        calls += [lambda d=d: self.qs.delete_dashboard(AwsAccountId=ACCOUNT, DashboardId=d["DashboardId"]) for d in assets["dashboard"]]
        calls += [lambda d=d: self.qs.delete_analysis(AwsAccountId=ACCOUNT, AnalysisId=d["AnalysisId"], ForceDeleteWithoutRecovery=True)
                  for d in assets["analysis"]]
        calls += [lambda d=d: self.qs.delete_data_set(AwsAccountId=ACCOUNT, DataSetId=d["DataSetId"]) for d in reversed(assets["dataset"])]
        calls += [lambda d=d: self.qs.delete_data_source(AwsAccountId=ACCOUNT, DataSourceId=d["DataSourceId"]) for d in assets["datasource"]]
        for call in calls:
            try:
                call()
            except self.qs.exceptions.ResourceNotFoundException:
                pass
        if quiet:
            time.sleep(5)  # deletes are eventually consistent; ids are reused right after
            return
        for g in GROUPS:
            try:
                self.qs_identity.delete_group(AwsAccountId=ACCOUNT, Namespace="default", GroupName=g)
            except self.qs_identity.exceptions.ResourceNotFoundException:
                pass
        for path in sorted(DATA.glob("dunder_mifflin/*.parquet")):
            self.athena_sql(f"drop table if exists {DATABASE}.{path.stem}")
        self.athena_sql(f"drop table if exists {DATABASE}.region_access_rules")
        self.athena_sql(f"drop database if exists {DATABASE}")
        listed = self.s3.list_objects_v2(Bucket=BUCKET, Prefix=f"{PREFIX}/")
        keys = [{"Key": o["Key"]} for o in listed.get("Contents") or []]
        if keys:
            self.s3.delete_objects(Bucket=BUCKET, Delete={"Objects": keys})

    # ------------------------------------------------------------------ QuickSight's own numbers (optional)

    def snapshot_csv(self, dashboard: str, sheet: str, visual: str) -> str:
        """StartDashboardSnapshotJob: a table visual as CSV, as an anonymous user (needs the Pixel Perfect add-on)."""
        job = f"unshackle-{int(time.time())}"
        self.qs.start_dashboard_snapshot_job(
            AwsAccountId=ACCOUNT, DashboardId=dashboard, SnapshotJobId=job,
            UserConfiguration={"AnonymousUsers": [{"RowLevelPermissionTags": [{"Key": "account_manager", "Value": "*"}]}]},
            SnapshotConfiguration={
                "FileGroups": [{"Files": [{"FormatType": "CSV", "SheetSelections": [
                    {"SheetId": sheet, "SelectionScope": "SELECTED_VISUALS", "VisualIds": [visual]}]}]}],
                "DestinationConfiguration": {"S3Destinations": [{"BucketConfiguration": {
                    "BucketName": BUCKET, "BucketPrefix": f"{PREFIX}/snapshots", "BucketRegion": REGION}}]}})
        status = wait_for(lambda: _done(self.qs.describe_dashboard_snapshot_job(AwsAccountId=ACCOUNT, DashboardId=dashboard,
                                                                                SnapshotJobId=job), "JobStatus"), "snapshot job", 600, 5)
        result = self.qs.describe_dashboard_snapshot_job_result(AwsAccountId=ACCOUNT, DashboardId=dashboard, SnapshotJobId=job)
        assert status == "COMPLETED", result
        uri = result["Result"]["AnonymousUsers"][0]["FileGroups"][0]["S3Results"][0]["S3Uri"]
        bucket, _, key = uri.removeprefix("s3://").partition("/")
        return self.s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8-sig")


def _extract_build_verify(aws: _Aws, tmp_path: Path) -> None:
    import duckdb

    env = {"QUICKSIGHT_ACCOUNT_ID": ACCOUNT, "QUICKSIGHT_REGION": REGION, "QUICKSIGHT_IDENTITY_REGION": IDENTITY_REGION}
    repo = tmp_path / "repo"
    run_unshackle(repo, "init", env=env)
    print(run_unshackle(repo, "inventory", "quicksight", env=env))
    run_unshackle(repo, "extract", "quicksight", "--only", "dashboard:sales-overview", "--only", "analysis:sales-analysis", env=env)
    raw = repo / "migration/raw/quicksight"
    assert json.loads((raw / "_errors.json").read_text()) == []
    assert list(raw.glob("bundle/**/*.json")), "the asset bundle export didn't run"
    if os.environ.get("QUICKSIGHT_LIVE_SAVE_FIXTURE") == "1":
        dest = LIVE / "fixtures" / "quicksight"
        shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(raw / "bundle", dest / "bundle")
        for p in dest.rglob("*.json"):
            p.write_text(p.read_text().replace(ACCOUNT, "111122223333"))
    print(run_unshackle(repo, "build", "quicksight", env=env))

    from conftest import notebook_source
    from test_ossie import _check

    for path in sorted((repo / "semantic").glob("*.ossie.yaml")):
        _check(yaml.safe_load(path.read_text()))
    nb = "notebooks/sales-overview.ipynb"
    notebook_source(repo / nb)

    data = repo / "migration/parity/data"
    data.mkdir(parents=True)
    con = duckdb.connect()
    for p in sorted(DATA.glob("dunder_mifflin/*.parquet")):
        con.execute(f"create view {p.stem} as select * from read_parquet('{p}')")
    cases = []
    for tile, sql in REFERENCE.items():
        for suffix, status, params in (("", "", {}), ("_completed", "and o.status = 'COMPLETED'", {"status_param": "COMPLETED"})):
            if suffix and "{status}" not in sql:
                continue
            con.execute(sql.format(status=status)).df().to_csv(data / f"{tile}{suffix}.csv", index=False)
            cases.append({"name": f"{tile}{', Status = COMPLETED' if suffix else ''}", "source": {"csv": f"migration/parity/data/{tile}{suffix}.csv"},
                          "target": {"notebook": nb, "tile": tile, **({"params": params} if params else {})},
                          **({"keys": KEYS[tile]} if tile in KEYS else {})})
    ossie = yaml.safe_load((repo / "semantic/orders.ossie.yaml").read_text())
    for metric, sql in METRICS.items():
        con.execute(sql).df().to_csv(data / f"metric_{metric}.csv", index=False)
        cases.append({"name": f"{metric} metric (Ossie)", "source": {"csv": f"migration/parity/data/metric_{metric}.csv"},
                      "target": {"sql": _ossie_sql(ossie, metric) + " WHERE status <> 'TEST'"},
                      "tolerance": 1e-6})  # Athena divides decimals at the operands' scale
    if os.environ.get("QUICKSIGHT_LIVE_SNAPSHOTS") == "1":
        csv = aws.snapshot_csv("sales-overview", "overview", "table-accounts")
        lines = csv.splitlines()
        lines[0] = "account_name,revenue,average_order_value,order_id"  # QuickSight's headers are display names; same order
        (data / "snapshot_revenue_by_account.csv").write_text("\n".join(lines) + "\n")
        cases.append({"name": "revenue_by_account (QuickSight's own CSV)", "source": {"csv": "migration/parity/data/snapshot_revenue_by_account.csv"},
                      "target": {"notebook": nb, "tile": "revenue_by_account"}, "keys": ["account_name"]})
    for i, case in enumerate(cases):
        write_case(repo, f"{i:02d}", case)
    url = (f"awsathena+rest://:@athena.{REGION}.amazonaws.com:443/{DATABASE}"
           f"?s3_staging_dir={quote(aws.results, safe='')}&work_group={WORKGROUP}")
    out = run_unshackle(repo, "verify", env={**env, "UNSHACKLE_WAREHOUSE_URL": url}, check=False)
    print(out)
    results = json.loads((repo / "migration/parity/results.json").read_text())
    assert len(results) == len(cases) and all(r["status"] == "pass" for r in results), out


# ------------------------------------------------------------------ helpers


def _to_model(value: Any, shape: Any) -> Any:
    """Fixture JSON (camelCase) -> the service model's member names."""
    if shape.type_name == "structure":
        members = {k.lower(): (k, s) for k, s in shape.members.items()}
        return {members[k.lower()][0]: _to_model(v, members[k.lower()][1]) for k, v in value.items()}
    if shape.type_name == "list":
        return [_to_model(v, shape.member) for v in value]
    if shape.type_name == "map":
        return {k: _to_model(v, shape.value) for k, v in value.items()}
    return value


def _ossie_sql(ossie: dict, metric: str) -> str:
    """The metric's Ossie expression with derived fields inlined, as a query on the dataset's table."""
    ds = ossie["datasets"][0]
    fields = {f["name"]: f["expression"]["dialects"][0]["expression"] for f in ds["fields"]}
    expr = next(m for m in ds["metrics"] if m["name"] == metric)["expression"]["dialects"][0]["expression"]
    for name, sql in fields.items():
        if sql != name:
            expr = re.sub(rf"\b{name}\b", f"({sql})", expr)
    return f"SELECT {expr} AS {metric} FROM {ds['source']}"


def _done(status: dict[str, Any], key: str = "State") -> str | None:
    state = status.get(key)
    return state if state in ("SUCCEEDED", "FAILED", "CANCELLED", "COMPLETED") else None


def _status(resource: dict[str, Any]) -> str | None:
    status = str(resource.get("Status") or "")
    return status if status.endswith(("SUCCESSFUL", "FAILED")) else None


def _hive_type(duck: str) -> str:
    if duck.startswith("DECIMAL"):
        return duck.lower()
    types = {"VARCHAR": "string", "INTEGER": "int", "BIGINT": "bigint", "DOUBLE": "double", "DATE": "date",
             "TIMESTAMP": "timestamp", "BOOLEAN": "boolean", "SMALLINT": "smallint", "FLOAT": "float"}
    if duck not in types:
        raise ValueError(f"no Athena type for Parquet column type {duck}")
    return types[duck]
