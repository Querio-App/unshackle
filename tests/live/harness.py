"""Live test harness: a real BI tool in Docker, a seeded Postgres warehouse, and the
unshackle CLI run end to end against them.

A tool's live test does:

    with Stack("lightdash") as stack:       # docker-compose up (warehouse + tool), down -v on exit
        stack.load_csvs()                    # tests/live/data/dunder_mifflin/*.csv -> warehouse
        ...bootstrap the tool and create content through its API...
        run_unshackle(repo, "extract", "lightdash", env=...)
        write_case(repo, ...)                # migration/parity/*.yaml (skills/verify format)
        run_unshackle(repo, "verify", env={"UNSHACKLE_WAREHOUSE_URL": stack.warehouse_url})

Secrets for the throwaway containers are generated per run. Nothing here reaches a
remote service. Set UNSHACKLE_LIVE_KEEP=1 to leave the containers up after a run.
"""

from __future__ import annotations

import csv
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

import httpx
import yaml

LIVE = Path(__file__).parent
DATA = LIVE / "data"
ENABLED = os.environ.get("UNSHACKLE_LIVE") == "1"
SKIP_REASON = "live test: set UNSHACKLE_LIVE=1 (needs Docker and docker-compose; see tests/live/README.md)"

INT = re.compile(r"^-?\d+$")
NUM = re.compile(r"^-?\d+\.\d+$")
TS = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(\.\d+)?$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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
    """docker-compose project `unshackle-live-<tool>`: the shared warehouse plus tests/live/<tool>/docker-compose.yml (if any)."""

    def __init__(self, tool: str, dataset: str = "dunder_mifflin"):
        self.tool, self.dataset = tool, dataset
        self.project = f"unshackle-live-{tool}"
        self.pg_port = int(os.environ.get("UNSHACKLE_LIVE_PG_PORT", "55432"))
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

    def load_csvs(self, schema: str = "public") -> dict[str, int]:
        """Load every tests/live/data/<dataset>/*.csv into <schema>.<file stem>; column types are inferred."""
        import psycopg

        counts: dict[str, int] = {}
        dsn = f"host=localhost port={self.pg_port} dbname={self.dataset} user=unshackle password={self.password}"
        conn = wait_for(lambda: psycopg.connect(dsn, autocommit=True), "warehouse", 60)
        with conn:
            for path in sorted((DATA / self.dataset).glob("*.csv")):
                with path.open(newline="") as f:
                    rows = list(csv.reader(f))
                header, body = rows[0], rows[1:]
                types = [_pg_type([r[i] for r in body]) for i in range(len(header))]
                table = f'"{schema}"."{path.stem}"'
                cols = ", ".join(f'"{h}" {t}' for h, t in zip(header, types))
                conn.execute(f"create schema if not exists \"{schema}\"")
                conn.execute(f"drop table if exists {table} cascade")
                conn.execute(f"create table {table} ({cols})")
                with conn.cursor().copy(f"copy {table} from stdin") as copy:
                    for r in body:
                        copy.write_row([v if v != "" else None for v in r])
                counts[path.stem] = len(body)
        return counts


def _pg_type(values: list[str]) -> str:
    vals = [v for v in values if v != ""]
    for pattern, typ in ((INT, "bigint"), (NUM, "numeric"), (TS, "timestamp"), (DATE, "date")):
        if vals and all(pattern.match(v) for v in vals):
            return typ
    if vals and all(v.lower() in ("true", "false") for v in vals):
        return "boolean"
    return "text"


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
