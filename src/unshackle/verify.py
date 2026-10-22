"""Parity checks: compare the source tool's numbers with the migrated SQL.

Cases live in migration/parity/*.yaml:

    name: Monthly revenue by region
    source:
      csv: migration/raw/tableau/views/abc123.csv   # or  sql: "select ..."
    target:
      sql: "select ..."                             # or  notebook: notebooks/revenue.ipynb  tile: monthly_revenue
    keys: [month, region]
    columns: {"Sum of Revenue": revenue}   # optional: source name -> target name
    ignore: [tooltip_only_col]            # optional: source columns not compared
    tolerance: 0.0001

Passing cases are written to evals/ so they keep running after the migration.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from unshackle.ir import slugify, snake
from unshackle.target.semantic import dump_yaml


@dataclass
class ParityResult:
    name: str
    status: str
    source_rows: int | None = None
    target_rows: int | None = None
    max_gap: str = ""
    note: str = ""


def _norm(df):
    df = df.copy()
    seen: dict[str, int] = {}
    cols = []
    for c in df.columns:
        name = snake(str(c))
        seen[name] = seen.get(name, 0) + 1
        cols.append(name if seen[name] == 1 else f"{name}_{seen[name]}")
    df.columns = cols
    return df


def _as_dates(x):
    """Tableau and friends export 'January 2025' where SQL returns 2025-01-01."""
    import pandas as pd

    try:
        d = pd.to_datetime(x.astype(str), format="mixed")
    except (ValueError, TypeError, OverflowError):
        return None
    if not d.notna().all():
        return None
    return d.dt.tz_localize(None) if d.dt.tz is not None else d


def _to_num(x):
    """Numbers as the BI tool exported them: '1,234.50', '$1,234', '12.5%', '(3.2)'."""
    import pandas as pd

    if x.dtype == object and x.dropna().astype(str).str.match(r"^0\d").any():
        return pd.Series([float("nan")] * len(x), index=x.index)
    direct = pd.to_numeric(x, errors="coerce")
    if direct.notna().sum() == x.notna().sum():
        return direct
    s = x.astype(str).str.strip()
    pct = s.str.endswith("%")
    neg = s.str.startswith("(") & s.str.endswith(")")
    cleaned = s.str.replace(r"[,$\u00a3\u20ac%()\s]", "", regex=True)
    out = pd.to_numeric(cleaned, errors="coerce")
    out = out.where(~pct, out / 100).where(~neg, -out)
    out[x.isna()] = float("nan")
    return out


def _blank_is_null(df):
    """A CSV export writes an empty string and NULL the same way, so compare them as equal."""
    import pandas as pd

    for c in df.columns:
        if df[c].dtype == object or pd.api.types.is_string_dtype(df[c].dtype):
            df[c] = df[c].mask(df[c].map(lambda v: isinstance(v, str) and not v.strip()))
    return df


def _sortable(df, cols):
    key = df[cols].copy()
    for c in cols:
        if key[c].dtype == object:
            key[c] = key[c].astype(str)
    order = key.sort_values(cols, kind="mergesort", na_position="last").index
    return df.loc[order].reset_index(drop=True)


def compare(
    source,
    target,
    keys: list[str] | None = None,
    tolerance: float = 1e-4,
    columns: dict[str, str] | None = None,
    ignore: list[str] | None = None,
) -> tuple[str, str, str]:
    """Return (status, max_gap, note). status is pass | differs.

    Every source column must be compared: map renamed ones with `columns`
    ({source name: target name}) or list deliberate gaps in `ignore`.
    """
    a, b = _blank_is_null(_norm(source)), _blank_is_null(_norm(target))
    if columns:
        a = a.rename(columns={snake(k): snake(v) for k, v in columns.items()})
    for col in ignore or []:
        a = a.drop(columns=[snake(col)], errors="ignore")
    if len(a) == 0 and len(b) == 0:
        return "differs", "", "no rows on either side; pick a date range that has data"
    if len(a) != len(b):
        return "differs", "", f"row count {len(a)} vs {len(b)}"
    missing = [c for c in a.columns if c not in set(b.columns)]
    if missing:
        return "differs", "", f"source columns missing from target: {', '.join(missing)} (map them with `columns:` or list them in `ignore:`)"
    shared = list(a.columns)
    keys = [snake(k) for k in keys or []]
    absent = [k for k in keys if k not in shared]
    if absent:
        return "differs", "", f"keys not found: {', '.join(absent)}"
    numeric: dict[str, tuple] = {}
    for col in shared:
        if any(side[col].dropna().astype(str).str.match(r"^0\d").any() for side in (a, b)):
            a[col], b[col] = a[col].astype(str).str.strip(), b[col].astype(str).str.strip()
            continue
        xn, yn = _to_num(a[col]), _to_num(b[col])
        if xn.notna().sum() == a[col].notna().sum() and yn.notna().sum() == b[col].notna().sum():
            a[col], b[col] = xn, yn
            numeric[col] = (xn, yn)
            continue
        xd, yd = _as_dates(a[col]), _as_dates(b[col])
        if xd is not None and yd is not None:
            a[col], b[col] = xd, yd
    order = keys + [c for c in shared if c not in keys]
    a, b = _sortable(a, order), _sortable(b, order)
    worst = 0.0
    for col in shared:
        x, y = a[col], b[col]
        both_na = x.isna() & y.isna()
        one_na = x.isna() ^ y.isna()
        if one_na.any():
            i = one_na.idxmax()
            return "differs", "", f"column {col} row {i}: {x[i]!r} vs {y[i]!r}"
        if col in numeric:
            for u, v, skip in zip(x, y, both_na):
                if skip:
                    continue
                denom = max(abs(u), abs(v), 1e-12)
                gap = 0.0 if u == v else abs(u - v) / denom
                worst = max(worst, gap)
                if gap > tolerance and not math.isclose(u, v, abs_tol=1e-9):
                    return "differs", f"{gap:.4%}", f"column {col}: {u} vs {v}"
            continue
        xs, ys = x.astype(str).str.strip(), y.astype(str).str.strip()
        bad = (xs != ys) & ~both_na
        if bad.any():
            i = bad.idxmax()
            return "differs", "", f"column {col} row {i}: {x[i]!r} vs {y[i]!r}"
    return "pass", f"{worst:.4%}", ""


def _load_side(side: dict[str, Any], repo: Path, url: str | None):
    import pandas as pd

    from unshackle.run import notebook_queries, query

    if "csv" in side:
        return pd.read_csv(repo / side["csv"], dtype=str, keep_default_na=True)
    if "sql" in side:
        return query(side["sql"], side.get("params", {}), url)
    if "notebook" in side:
        params, sqls = notebook_queries(repo / side["notebook"])
        return query(sqls[side["tile"]], {**params, **side.get("params", {})}, url)
    raise ValueError(f"parity side needs csv, sql or notebook: {side}")


def run_checks(repo: Path, url: str | None = None) -> list[ParityResult]:
    results: list[ParityResult] = []
    case_dir = repo / "migration" / "parity"
    for path in sorted(case_dir.glob("*.yaml")):
        case = yaml.safe_load(path.read_text())
        name = case.get("name", path.stem)
        try:
            src = _load_side(case["source"], repo, url)
            tgt = _load_side(case["target"], repo, url)
            status, gap, note = compare(
                src,
                tgt,
                case.get("keys"),
                float(case.get("tolerance", 1e-4)),
                columns=case.get("columns"),
                ignore=case.get("ignore"),
            )
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            results.append(ParityResult(name=name, status="not checked", note=str(exc)[:200]))
            continue
        results.append(ParityResult(name, status, len(src), len(tgt), gap, note))
        if status == "pass":
            write_eval(repo, case, tgt)
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "results.json").write_text(json.dumps([asdict(r) for r in results], indent=2) + "\n")
    return results


def write_eval(repo: Path, case: dict[str, Any], result) -> Path:
    from unshackle.target.layout import Layout

    layout = Layout.load(repo)
    sample = result.head(20).to_dict(orient="records")
    doc = {
        "name": case.get("name"),
        "question": case.get("question", case.get("name")),
        "target": case["target"],
        "keys": case.get("keys", []),
        "tolerance": case.get("tolerance", 1e-4),
        "expected": {"rows": len(result), "sample": json.loads(json.dumps(sample, default=str))},
        "source": "unshackle parity check",
    }
    path = repo / layout.rel("evals", f"{slugify(str(case.get('name')))}.yaml")
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_yaml(doc))
    return path
