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
    formats: {"Profit Ratio": "0.0%"}      # optional: compare at the precision the source shows
    tolerance: 0.0001
    strict: false                          # true: source columns missing from the target fail the case

Passing cases are written to evals/<id or file name>.yaml so they keep running after the migration.
`unshackle verify --init <tool>` writes a case for each saved view CSV that matches a tile.
"""

from __future__ import annotations

import ast
import json
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from unshackle.ir import Bundle, Dashboard, Tile, slugify, snake
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


SCALE = {"%": 0.01, "K": 1e3, "M": 1e6, "B": 1e9}
# A display format: optional currency, digits with optional thousands separators and decimals, optional % or K/M/B.
FORMAT = re.compile(r"^(?P<prefix>[^\d#.,\s]*)\s*(?P<int>[#0,]*0)(?:\.(?P<dec>0+))?\s*(?P<unit>%|[KMB])?$")
# A value as a BI tool displays it: '$1,234', '-11.9%', '0.1M', '(3.2)'.
SHOWN = re.compile(r"^\(?(?P<prefix>-?[^\d\s().,-]*)\s*-?(?P<int>[\d,]*\d)(?:\.(?P<dec>\d+))?\s*(?P<unit>%|[KkMB])?\)?$")


def _to_num(x):
    """Numbers as the BI tool exported them: '1,234.50', '$1,234', '12.5%', '0.1M', '(3.2)'."""
    import pandas as pd

    if x.dtype == object and x.dropna().astype(str).str.match(r"^0\d").any():
        return pd.Series([float("nan")] * len(x), index=x.index)
    direct = pd.to_numeric(x, errors="coerce")
    if direct.notna().sum() == x.notna().sum():
        return direct
    s = x.astype(str).str.strip()
    neg = s.str.startswith("(") & s.str.endswith(")")
    unit = s.str.extract(r"\d\s*([%KkMB])\)?$", expand=False).str.upper().map(SCALE).fillna(1.0)
    cleaned = s.str.replace(r"[,$\u00a3\u20ac%()\s]", "", regex=True).str.replace(r"(?<=\d)[KkMB]$", "", regex=True)
    out = pd.to_numeric(cleaned, errors="coerce") * unit
    out = out.where(~neg, -out)
    out[x.isna()] = float("nan")
    return out


def display_unit(fmt: str) -> float:
    """The smallest step a display format shows: '0.0%' -> 0.001, '$#,##0' -> 1, '0.0M' -> 100000."""
    m = FORMAT.match(fmt.strip())
    if not m:
        raise ValueError(f"display format {fmt!r} not understood; use e.g. 0.0%, $#,##0, 0.00, 0.0M")
    return 10.0 ** -len(m["dec"] or "") * SCALE.get(m["unit"] or "", 1.0)


def infer_format(values) -> str | None:
    """The display format of exported text ('$1,234', '11.9%', '0.1M'); None for plain numbers or mixed units."""
    shown = [str(v).strip() for v in values if isinstance(v, str) and v.strip()]
    parsed = [SHOWN.match(v) for v in shown]
    if not parsed or not all(parsed):
        return None
    units = {(m["unit"] or "").upper() for m in parsed}  # type: ignore[union-attr]
    prefixes = {m["prefix"].lstrip("-") for m in parsed} - {""}  # type: ignore[union-attr]
    commas = any("," in m["int"] for m in parsed)  # type: ignore[union-attr]
    if len(units) > 1 or len(prefixes) > 1 or not (units - {""} or prefixes or commas):
        return None
    dec = max(len(m["dec"] or "") for m in parsed)  # type: ignore[union-attr]
    return "".join(prefixes) + ("#,##0" if commas else "0") + ("." + "0" * dec if dec else "") + units.pop()


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
    formats: dict[str, str] | None = None,
    strict: bool = False,
) -> tuple[str, str, str]:
    """Return (status, max_gap, note). status is pass | differs.

    Source columns are matched to target columns by name; map renamed ones with `columns`
    ({source name: target name}) and leave out deliberate gaps with `ignore`. A source
    column the target doesn't have is noted, not compared (with `strict`, it fails the case);
    at least one column besides the keys must be compared. `formats` ({source name: display
    format such as "0.0%"}) compares a column at the precision the source shows: values pass
    when they are at most half a displayed step apart, so an exact half-way tie passes.
    """
    a, b = _blank_is_null(_norm(source)), _blank_is_null(_norm(target))
    rename = {snake(k): snake(v) for k, v in (columns or {}).items()}
    a = a.rename(columns=rename)
    shown = {rename.get(snake(k), snake(k)): v for k, v in (formats or {}).items()}
    for fmt in shown.values():
        display_unit(fmt)  # an unknown format fails the case before any query result is judged
    for col in ignore or []:
        a = a.drop(columns=[rename.get(snake(col), snake(col))], errors="ignore")
    if len(a) == 0 and len(b) == 0:
        return "differs", "", "no rows on either side; pick a date range that has data"
    if len(a) != len(b):
        return "differs", "", f"row count {len(a)} vs {len(b)}"
    missing = [c for c in a.columns if c not in set(b.columns)]
    note = ""
    if missing:
        note = f"source columns not in the target, not compared: {', '.join(missing)} (map them with `columns:` or list them in `ignore:`)"
        if strict:
            return "differs", "", note
        a = a.drop(columns=missing)
    shared = list(a.columns)
    keys = [snake(k) for k in keys or []]
    absent = [k for k in keys if k not in shared]
    if absent:
        return "differs", "", f"keys not found: {', '.join(absent)}"
    if not [c for c in shared if c not in keys]:
        return "differs", "", note or "no column besides the keys to compare"
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
            half = display_unit(shown[col]) / 2 if col in shown else 0.0
            for u, v, skip in zip(x, y, both_na):
                if skip:
                    continue
                denom = max(abs(u), abs(v), 1e-12)
                gap = 0.0 if u == v else abs(u - v) / denom
                if abs(u - v) <= half * (1 + 1e-9):  # equal as displayed
                    continue
                worst = max(worst, gap)
                if gap > tolerance and not math.isclose(u, v, abs_tol=1e-9):
                    as_shown = f" (shown as {shown[col]})" if half else ""
                    return "differs", f"{gap:.4%}", f"column {col}: {u} vs {v}{as_shown}"
            continue
        xs, ys = x.astype(str).str.strip(), y.astype(str).str.strip()
        bad = (xs != ys) & ~both_na
        if bad.any():
            i = bad.idxmax()
            return "differs", "", f"column {col} row {i}: {x[i]!r} vs {y[i]!r}"
    return "pass", f"{worst:.4%}", note



def _load_side(side: dict[str, Any], repo: Path, url: str | None):
    import pandas as pd

    from unshackle.run import notebook_queries, query

    if "csv" in side:
        return pd.read_csv(repo / side["csv"], dtype=str, keep_default_na=True, encoding="utf-8-sig")
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
                formats=case.get("formats"),
                strict=bool(case.get("strict", False)),
            )
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            results.append(ParityResult(name=name, status="not checked", note=str(exc)[:200]))
            continue
        results.append(ParityResult(name, status, len(src), len(tgt), gap, note))
        if status == "pass":
            write_eval(repo, {"id": path.stem, **case}, tgt)
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "results.json").write_text(json.dumps([asdict(r) for r in results], indent=2) + "\n")
    return results


def write_eval(repo: Path, case: dict[str, Any], result) -> Path:
    from unshackle.target.layout import Layout

    layout = Layout.load(repo)
    sample = result.head(20).to_dict(orient="records")
    doc = {
        "id": eval_id(case),
        "name": case.get("name"),
        "question": case.get("question", case.get("name")),
        "target": case["target"],
        "keys": case.get("keys", []),
        "tolerance": case.get("tolerance", 1e-4),
        "expected": {"rows": len(result), "sample": json.loads(json.dumps(sample, default=str))},
        "source": "unshackle parity check",
    }
    path = repo / layout.rel("evals", f"{eval_id(case)}.yaml")
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_yaml(doc))
    return path


def eval_id(case: dict[str, Any]) -> str:
    """The case's `id:`, else its file name (run_checks passes it as `id`), else a slug of its name, cut to 40 characters."""
    return slugify(str(case.get("id") or case.get("name") or "case"))[:40].strip("-") or "case"


# ------------------------------------------------------------ verify --init


def _tile_idents(nb: dict, tiles) -> dict[str, str]:
    """{tile id: notebook variable} for tiles with SQL, found by the `<ident>_sql = "..."` assignment holding the tile's SQL."""
    by_sql: dict[str, str] = {}
    for cell in nb.get("cells", []):
        if cell.get("cell_type") != "code":
            continue
        src = cell["source"] if isinstance(cell["source"], str) else "".join(cell["source"])
        for node in ast.parse(src).body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) \
                    and node.targets[0].id.endswith("_sql") and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                by_sql.setdefault(node.value.value.strip(), node.targets[0].id[: -len("_sql")])
    return {t.id: by_sql[t.sql.strip()] for t in tiles if t.sql.strip() and t.sql.strip() in by_sql}


def init_cases(repo: Path, tool: str) -> tuple[list[Path], list[str]]:
    """Write migration/parity/<slug>.yaml for each tile whose source view data was saved as CSV.

    Any source can offer this: a tile whose `source_config` has `parity_csv` (a repo-relative
    CSV of the tool's own numbers for that tile, e.g. Tableau's view data) gets a case. Existing
    case files are kept. Returns (written, skipped with a reason).
    """
    import pandas as pd

    from unshackle.target.layout import Layout
    from unshackle.target.notebooks import build_notebook, notebook_files

    ir = repo / "migration" / "ir" / f"{tool}.json"
    if not ir.exists():
        raise FileNotFoundError(f"{ir.relative_to(repo)} not found; run `unshackle build {tool}` first")
    bundle = Bundle.from_json(ir.read_text())
    case_dir = repo / "migration" / "parity"
    written: list[Path] = []
    skipped: list[str] = []
    seen: set[str] = set()
    taken: set[Path] = set()
    for d, nb_rel in zip(bundle.dashboards, notebook_files(bundle, Layout.load(repo))):
        idents = _tile_idents(build_notebook(d, bundle.tool), d.tiles)
        for t in d.tiles:
            csv_rel = t.source_config.get("parity_csv")
            if not csv_rel or csv_rel in seen:
                continue
            seen.add(csv_rel)
            label = f"{d.name} / {t.title or t.id}"
            if t.id not in idents:
                skipped.append(f"{label}: the tile has no SQL yet")
                continue
            try:
                src = pd.read_csv(repo / csv_rel, dtype=str, encoding="utf-8-sig")
            except (OSError, ValueError) as exc:
                skipped.append(f"{label}: {csv_rel} not readable ({str(exc)[:100]})")
                continue
            case = _case_for(label, csv_rel, src, t, d, nb_rel, idents[t.id])
            if case is None:
                skipped.append(f"{label}: no column of {csv_rel} matches the tile's columns")
                continue
            slug = slugify(t.title or t.id)[:40].strip("-") or "case"
            path, n = case_dir / f"{slug}.yaml", 2
            while path in taken:
                path, n = case_dir / f"{slug}-{n}.yaml", n + 1
            taken.add(path)
            if path.exists():
                skipped.append(f"{label}: {path.relative_to(repo)} exists, kept")
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(dump_yaml({"id": path.stem, **case}))
            written.append(path)
    return written, skipped


def _case_for(label: str, csv_rel: str, src, tile: Tile, dash: Dashboard, nb_rel: str, ident: str) -> dict[str, Any] | None:
    """A parity case comparing the saved CSV with the notebook tile, or None when no CSV column matches the tile."""
    encodings = [*tile.encodings.values(), *(e for layer in tile.layers for e in layer.encodings.values())]
    by_display = {snake(display): alias for alias, display in (tile.source_config.get("columns") or {}).items()}
    aliases = set(by_display.values()) | {e.field for e in encodings}
    columns: dict[str, str] = {}
    matched: list[tuple[str, str]] = []  # (CSV header, target column)
    for header in src.columns:
        alias = snake(header) if snake(header) in aliases else by_display.get(snake(header))
        if alias is None:
            continue
        matched.append((header, alias))
        if alias != snake(header):
            columns[header] = alias
    if not matched:
        return None
    measures = {e.field for e in encodings if e.type == "quantitative"}
    dims = {e.field for e in encodings if e.type != "quantitative"}
    keys, formats = [], {}
    for header, alias in matched:
        values = src[header].dropna()
        if alias in dims or (alias not in measures and not _to_num(values).notna().all()):
            keys.append(alias)
        elif fmt := infer_format(values):
            formats[header] = fmt
    binds = set(re.findall(r"(?<!:):(\w+)", tile.sql))
    case: dict[str, Any] = {
        "name": label,
        "source": {"csv": csv_rel},
        "target": {"notebook": nb_rel, "tile": ident},
    }
    params = {f.name: f.default for f in dash.filters if f.name in binds}
    if params:
        case["target"]["params"] = params  # the dashboard's default filter values, pinned
    case["keys"] = keys
    if columns:
        case["columns"] = columns
    if formats:
        case["formats"] = formats
    case["tolerance"] = 0.0001
    return case
