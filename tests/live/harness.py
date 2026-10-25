"""Live test harness: a real BI tool in Docker, a seeded Postgres warehouse, and the
unshackle CLI run end to end against them.

A tool's live test does:

    with Stack("lightdash") as stack:       # docker-compose up (warehouse + tool), down -v on exit
        stack.load_data()                    # tests/live/data/dunder_mifflin/*.parquet -> warehouse
        ...bootstrap the tool and create content through its API...
        run_unshackle(repo, "extract", "lightdash", env=...)
        write_case(repo, ...)                # migration/parity/*.yaml (skills/verify format)
        run_unshackle(repo, "verify", env={"UNSHACKLE_WAREHOUSE_URL": stack.warehouse_url})

Secrets for the throwaway containers are generated per run. Nothing here reaches a
remote service. Set UNSHACKLE_LIVE_KEEP=1 to leave the containers up after a run.
"""

from __future__ import annotations

import csv
import importlib.util
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

import httpx
import yaml

LIVE = Path(__file__).parent
DATA = LIVE / "data"
ENABLED = os.environ.get("UNSHACKLE_LIVE") == "1"
SKIP_REASON = "live test: set UNSHACKLE_LIVE=1 (needs Docker and docker-compose; see tests/live/README.md)"


def compose_cmd() -> list[str]:
    if os.environ.get("UNSHACKLE_LIVE_COMPOSE"):
        return os.environ["UNSHACKLE_LIVE_COMPOSE"].split()
    return ["docker-compose"] if shutil.which("docker-compose") else ["docker", "compose"]


def wait_for(
    check: Callable[[], Any], what: str, timeout: float = 300, every: float = 2, retry_on: tuple[type[Exception], ...] = (Exception,)
) -> Any:
    """Call check() until it returns something truthy; exceptions in `retry_on` mean "not yet"."""
    deadline, last = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except retry_on as exc:  # keep polling, report the last error
            last = exc
        time.sleep(every)
    raise TimeoutError(f"timed out after {timeout:.0f}s waiting for {what} (last error: {last})")


def wait_http(url: str, timeout: float = 300) -> None:
    wait_for(lambda: httpx.get(url, timeout=5).status_code < 500, url, timeout)


class Stack:
    """docker-compose project `unshackle-live-<tool>`: the shared warehouse plus tests/live/<tool>/docker-compose.yml (if any).

    With a `run` id the project is `unshackle-live-<tool>-<run>` and the warehouse gets a free host port (unless
    UNSHACKLE_LIVE_PG_PORT is set), so runs of the same test can go on at the same time on one machine."""

    def __init__(self, tool: str, dataset: str = "dunder_mifflin", run: str = ""):
        self.tool, self.dataset = tool, dataset
        self.project = f"unshackle-live-{tool}" + (f"-{run.lower()}" if run else "")
        port = os.environ.get("UNSHACKLE_LIVE_PG_PORT")
        self.pg_port = int(port) if port else (_free_port() if run else 55432)
        self.env = {
            **os.environ,
            "UNSHACKLE_LIVE_PG_PORT": str(self.pg_port),
            "UNSHACKLE_LIVE_PG_PASSWORD": os.environ.get("UNSHACKLE_LIVE_PG_PASSWORD") or secrets.token_hex(16),
            "UNSHACKLE_LIVE_SECRET": os.environ.get("UNSHACKLE_LIVE_SECRET") or secrets.token_hex(32),
        }
        self.files = [LIVE / "docker-compose.warehouse.yml"]
        if (LIVE / tool / "docker-compose.yml").exists():
            self.files.append(LIVE / tool / "docker-compose.yml")

    @property
    def password(self) -> str:
        return self.env["UNSHACKLE_LIVE_PG_PASSWORD"]

    @property
    def warehouse_url(self) -> str:
        return f"postgresql+psycopg://unshackle:{self.password}@localhost:{self.pg_port}/{self.dataset}"

    def compose(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        cmd = [*compose_cmd(), "-p", self.project]
        for f in self.files:
            cmd += ["-f", str(f)]
        return subprocess.run([*cmd, *args], env=self.env, check=check, text=True, capture_output=True)

    def logs(self, service: str, tail: int = 80) -> str:
        return self.compose("logs", "--no-color", "--tail", str(tail), service, check=False).stdout

    def __enter__(self) -> "Stack":
        self.down()  # start clean, even after a run with UNSHACKLE_LIVE_KEEP=1
        try:
            self.compose("up", "-d", "--wait", "warehouse")
            self.compose("up", "-d")
        except subprocess.CalledProcessError as exc:
            self.down()
            raise RuntimeError(f"docker-compose up failed:\n{exc.stderr}") from exc
        return self

    def __exit__(self, *exc: Any) -> None:
        if os.environ.get("UNSHACKLE_LIVE_KEEP") != "1":
            self.down()

    def down(self) -> None:
        self.compose("down", "-v", "--remove-orphans", check=False)

    @property
    def dsn(self) -> str:
        return f"host=localhost port={self.pg_port} dbname={self.dataset} user=unshackle password={self.password}"

    def load_data(self, schema: str = "public") -> dict[str, int]:
        """Load every tests/live/data/<dataset>/*.parquet into <schema>.<file stem>, then add the keys in KEYS."""
        import duckdb
        import psycopg

        counts: dict[str, int] = {}
        conn = wait_for(lambda: psycopg.connect(self.dsn, autocommit=True), "warehouse", 60)
        with conn:
            conn.execute(f'create schema if not exists "{schema}"')
            for path in sorted((DATA / self.dataset).glob("*.parquet")):
                rel = duckdb.read_parquet(str(path))
                table = f'"{schema}"."{path.stem}"'
                cols = ", ".join(f'"{c}" {_pg_type(str(t))}' for c, t in zip(rel.columns, rel.types))
                conn.execute(f"drop table if exists {table} cascade")
                conn.execute(f"create table {table} ({cols})")
                rows = rel.fetchall()
                with conn.cursor().copy(f"copy {table} from stdin") as copy:
                    for row in rows:
                        copy.write_row(row)
                counts[path.stem] = len(rows)
            keys = KEYS.get(self.dataset, {})
            for table, pk in keys.get("primary", {}).items():
                conn.execute(f'alter table "{schema}"."{table}" add primary key ("{pk}")')
            for table, col, ref in keys.get("foreign", []):
                conn.execute(f'alter table "{schema}"."{table}" add foreign key ("{col}") '
                             f'references "{schema}"."{ref}" ("{keys["primary"][ref]}")')
        return counts


# Parquet has no keys. Tools that join through foreign keys (Metabase's implicit joins) need them.
# orders.shipping_id is not a foreign key: some orders have no shipping row.
KEYS: dict[str, dict[str, Any]] = {
    "dunder_mifflin": {
        "primary": {"accounts": "account_id", "products": "product_id", "orders": "order_id",
                    "shippings": "shipping_id", "reviews": "review_id"},
        "foreign": [("orders", "account_id", "accounts"), ("orders", "product_id", "products"), ("reviews", "order_id", "orders")],
    },
}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _pg_type(duck: str) -> str:
    """Postgres type for a DuckDB (Parquet) column type."""
    if duck.startswith("DECIMAL"):
        return duck.replace("DECIMAL", "numeric")
    types = {"VARCHAR": "text", "INTEGER": "integer", "BIGINT": "bigint", "SMALLINT": "smallint", "DOUBLE": "double precision",
             "FLOAT": "real", "BOOLEAN": "boolean", "DATE": "date", "TIMESTAMP": "timestamp", "TIMESTAMP WITH TIME ZONE": "timestamptz"}
    if duck not in types:
        raise ValueError(f"no Postgres type for Parquet column type {duck}")
    return types[duck]


def bootstrap_module(tool: str) -> ModuleType:
    """tests/live/<tool>/bootstrap.py, imported under a name of its own (each tool has one)."""
    spec = importlib.util.spec_from_file_location(f"{tool}_bootstrap", LIVE / tool / "bootstrap.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_unshackle(repo: Path, *args: str, env: dict[str, str] | None = None, check: bool = True) -> str:
    """Run the unshackle CLI as a user would and return its output; raise if it fails (unless check=False)."""
    proc = subprocess.run(
        [sys.executable, "-m", "unshackle.cli", *args, "--repo", str(repo)],
        env={**os.environ, **(env or {})}, text=True, capture_output=True,
    )
    out = proc.stdout + proc.stderr
    if check and proc.returncode != 0:
        raise AssertionError(f"unshackle {' '.join(args)} failed ({proc.returncode}):\n{out}")
    return out


def write_case(repo: Path, slug: str, case: dict[str, Any]) -> Path:
    """Write migration/parity/<slug>.yaml (format: skills/verify/SKILL.md)."""
    path = repo / "migration" / "parity" / f"{slug}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(case, sort_keys=False, allow_unicode=True))
    return path


def write_csv(path: Path, columns: list[str], rows: list[list[Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(columns)
        w.writerows(rows)
    return path
