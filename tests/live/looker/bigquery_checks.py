"""Write unshackle's BigQuery SQL for the evidence as LookML, to run on a Looker lab's BigQuery.

Each check is a view whose derived table is the SQL unshackle writes for BigQuery (tiles, timeframes,
filters), with the evidence rows inlined as CTEs; its columns are dimensions. Paste the written
view file and explores like the evidence packs, then capture with:

    python tests/live/looker/bigquery_checks.py            # writes evidence3_checks.*.lkml and the case list
    python tests/live/looker/evidence_links.py --console checks | pbcopy

tests/test_looker_generated.py compares what comes back with Looker's own numbers.
"""

import json
import re
import sys
import tempfile
from pathlib import Path

import lkml

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tests"))

from test_looker_generated import (  # noqa: E402
    FIX, NESTED_TILES, RELATIVE, TILES, TIMEFRAMES, _by_name, _parse_evidence2, _timeframe, evidence_tiles,
)


def _rows(path: Path) -> dict[str, str]:
    return {v["name"]: v["derived_table"]["sql"] for v in lkml.load(path.read_text())["views"] if v.get("derived_table")}


def checks() -> dict[str, str]:
    rows = {**_rows(FIX / "evidence.view.lkml"), **_rows(FIX / "evidence2.view.lkml")}
    out: dict[str, str] = {}
    with tempfile.TemporaryDirectory() as tmp:
        tiles = evidence_tiles(Path(tmp) / "t", "bigquery")
        # the unnested view as unshackle writes it, over the parent's rows
        from unshackle.raw import RawStore
        from unshackle.sources import get_source

        store = RawStore(Path(tmp) / "t", "looker")
        rows["ev_nested_lines"] = next(q.sql for q in get_source("looker").parse(store).queries if q.name == "ev_nested_lines derived table")
        for case in [*TILES, "liquid_default", "liquid_set", *NESTED_TILES]:
            used = [v for v in ("ev_orders", "ev_items", "ev_nested", "ev_nested_lines") if re.search(rf"\b{v}\b", tiles[case].sql)]
            out[f"tile_{case}"] = "WITH " + ", ".join(f"{v} AS ({rows[v]})" for v in used) + "\n" + tiles[case].sql if used else tiles[case].sql
        for week_start in ("monday", "sunday"):
            cols = ", ".join(f"{_timeframe(tf, 'ts', 'bigquery', week_start)} AS created_{tf}" for tf in TIMEFRAMES)
            out[f"time_{week_start}"] = f"WITH ev_time AS ({rows['ev_time']})\nSELECT id, {cols} FROM ev_time"
        b = _parse_evidence2(Path(tmp) / "f", "bigquery")
        model = _by_name(b.semantic_models)["ev_f"]
        names = {d.name: d.expr or d.name for d in model.dimensions} | {"at": "ts", "on": "d"}
        counts = []
        for m in b.metrics:
            if m.semantic_model == "ev_f" and m.filter and not m.todo and m.name not in RELATIVE:
                cond = re.sub(r"\{\{ (?:Time)?Dimension\('ev_f__(\w+)'(?:, '\w+')?\) \}\}", lambda x: f"({names[x.group(1)]})", m.filter)
                counts.append(f"COUNTIF({cond}) AS {m.name}")
        out["filters"] = f"WITH ev_f AS ({rows['ev_f']})\nSELECT {', '.join(counts)} FROM ev_f"
    return out


def columns(sql: str) -> list[str]:
    """The output columns of the query."""
    import sqlglot

    return sqlglot.parse_one(sql, read="bigquery").named_selects


def main() -> None:
    views, explores, cases = [], ["# Paste at the bottom of the model file, next to an include of the checks view file.\n"], {}
    for name, sql in checks().items():
        view = f"chk_{name}"
        cols = columns(sql)
        dims = "\n".join(f"  dimension: {c} {{ sql: ${{TABLE}}.{c} ;; }}" for c in cols)
        views.append(f"view: {view} {{\n  derived_table: {{\n    sql:\n{sql} ;;\n  }}\n{dims}\n}}\n")
        explores.append(f"explore: {view} {{}}\n")
        cases[name] = {"view": view, "fields": [f"{view}.{c}" for c in cols]}
    head = "# unshackle's BigQuery SQL for the evidence, written by tests/live/looker/bigquery_checks.py: do not edit.\n\n"
    (FIX / "evidence3_checks.view.lkml").write_text(head + "\n".join(views))
    (FIX / "evidence3_checks.explores.lkml").write_text("\n".join(explores))
    (FIX / "evidence3_checks.cases.json").write_text(json.dumps(cases, indent=1) + "\n")
    print(f"{len(cases)} checks: {', '.join(cases)}")


if __name__ == "__main__":
    main()
