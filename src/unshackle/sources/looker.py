"""Looker source: LookML (views, explores, access rules) + dashboards and Looks via API 4.0.

Raw layout under migration/raw/looker/:

    _meta.json                          {"base_url": ...} (for dashboard links)
    lookml/**/*.lkml                    copy of LOOKER_LOOKML_DIR (the LookML Git repo), untouched
    api/lookml_models.json              GET /lookml_models
    api/explores/<model>/<explore>.json GET /lookml_models/{m}/explores/{e}  (only when no LookML dir)
    dashboards/<id>.json                GET /dashboards/{id}  (elements with `query`, layouts, filters)
    looks/<id>.json                     GET /looks/{id}
    queries/<query_id>.sql              GET /queries/{id}/run/sql  (generated SQL, not executed)
    content_usage.json                  GET /content_view/search  (view_count per dashboard / look)
    _skipped.json                       content the API user could not read (404/403)

parse() prefers raw LookML. Without it, it rebuilds views from the explore JSON
(field `sql` is only present when the API user has `see_lookml`). LookML dashboards
(`*.dashboard.lookml`) become notebooks whose tile SQL is written from the explore
(see _Query); dashboards from the API keep Looker's generated SQL.
"""

from __future__ import annotations

import calendar
import html
import json
import posixpath
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from unshackle.http import Client, SourceError, env
from unshackle.ir import (
    Bundle,
    Column,
    Dashboard,
    DashboardFilter,
    Dimension,
    Encoding,
    Entity,
    KnowledgeItem,
    Measure,
    Metric,
    Permission,
    RowFilter,
    SemanticModel,
    Table,
    Tile,
    VerifiedQuery,
    slugify,
    snake,
)
from unshackle.raw import RawStore
from unshackle.sources import _manual
from unshackle.sources.base import Inventory, Source
from unshackle.target.layout import Layout
from unshackle.target.ossie import FILTER_REF

REF = re.compile(r"\$\{([^}]+)\}")
LIQUID = re.compile(r"\{%|\{\{")
CONSTANT = re.compile(r"@\{(\w+)\}")
_IDENT = r"""(?:[\w$-]+|`[^`(){};']+`|"[^"(){};']+"|\[[^\[\](){};']+\])"""
TABLE_NAME = re.compile(rf"\s*{_IDENT}(?:\s*\.\s*{_IDENT})*\s*")  # a.b.c, `p-1.d.t`, "My Schema"."T"
USER_ATTR = re.compile(r"\{\{\s*_user_attributes\[['\"](\w+)['\"]\]\s*\}\}")
SIMPLE_IDENT = re.compile(r"^[A-Za-z_][\w]*$")
AGG_FN = re.compile(r"\b(sum|count|avg|min|max|median|percentile_cont|approx_\w+)\s*\(", re.I)

MEASURE_AGG = {
    "sum": "sum",
    "average": "average",
    "count": "count",
    "count_distinct": "count_distinct",
    "min": "min",
    "max": "max",
    "median": "median",
    "percentile": "percentile",
}
# smallest first -> MetricFlow granularity
TIMEFRAME_GRAIN = [
    ("time", "second"),
    ("second", "second"),
    ("minute", "minute"),
    ("hour", "hour"),
    ("date", "day"),
    ("week", "week"),
    ("month", "month"),
    ("quarter", "quarter"),
    ("year", "year"),
]
TF_GRAIN = dict(TIMEFRAME_GRAIN)


def _grain(g: dict[str, Any]) -> str:
    """A dimension_group's grain: its smallest timeframe, at least a day for a date."""
    grain = next((gr for tf, gr in TIMEFRAME_GRAIN if tf in (g.get("timeframes") or ["time"])), "day")
    return "day" if g.get("datatype") in ("date", "yyyymmdd") and grain in ("second", "minute", "hour") else grain


VIS_MARK = {
    "looker_column": ("chart", "bar"),
    "looker_bar": ("chart", "bar"),
    "looker_line": ("chart", "line"),
    "looker_area": ("chart", "area"),
    "looker_scatter": ("chart", "point"),
    "looker_pie": ("chart", "arc"),
    "looker_donut_multiples": ("chart", "arc"),
    "single_value": ("kpi", "text"),
    "looker_single_record": ("kpi", "text"),
    "looker_grid": ("table", "table"),
    "table": ("table", "table"),
    "looker_table": ("table", "table"),
}
COLUMN_TYPE = {"number": "number", "string": "string", "yesno": "boolean", "zipcode": "string", "tier": "string"}
# `${created_<tf>}` -> SQL on the dimension_group's expression. These give the same value
# as Looker's SQL in every dialect (a DATE, an integer part or a fixed name).
TIMEFRAME_SQL = {
    "date": "CAST({} AS DATE)",
    "year": "EXTRACT(YEAR FROM {})",
    "month_num": "EXTRACT(MONTH FROM {})",
    "day_of_month": "EXTRACT(DAY FROM {})",
    "hour_of_day": "EXTRACT(HOUR FROM {})",
    "month_name": "CASE EXTRACT(MONTH FROM {}) "
    + " ".join(f"WHEN {i} THEN '{m}'" for i, m in enumerate(calendar.month_name) if i)
    + " END",
}
# The rest are strings or week-based numbers that Looker formats: month '2026-12', quarter
# '2026-10' (its first month), week '2026-12-28' (the day it starts), day_of_week 'Thursday', day_of_week_index
# (0 = week_start_day), week_of_year (ISO, whatever the week start), quarter_of_year 'Q4'.
# Per warehouse: the type for strings, 'YYYY-MM-DD' / 'YYYY-MM' of a date, the day of the week
# with Monday = 0, a date minus n days, the quarter's first day, the ISO week, the day of the year.
DIALECT_DATES: dict[str, dict[str, str]] = {
    "bigquery": {"str": "STRING", "day": "FORMAT_DATE('%F', {})", "month": "FORMAT_DATE('%Y-%m', {})",
                 "dow": "MOD(EXTRACT(DAYOFWEEK FROM {}) + 5, 7)", "minus": "DATE_SUB({0}, INTERVAL {1} DAY)",
                 "quarter": "DATE_TRUNC({}, QUARTER)", "isoweek": "EXTRACT(ISOWEEK FROM {})", "doy": "EXTRACT(DAYOFYEAR FROM {})"},
    "snowflake": {"str": "VARCHAR", "day": "TO_CHAR({}, 'YYYY-MM-DD')", "month": "TO_CHAR({}, 'YYYY-MM')",
                  "dow": "(DAYOFWEEKISO({}) - 1)", "minus": "DATEADD(day, -({1}), {0})",
                  "quarter": "DATE_TRUNC('quarter', {})", "isoweek": "WEEKISO({})", "doy": "DAYOFYEAR({})"},
    "postgres": {"str": "VARCHAR", "day": "TO_CHAR({}, 'YYYY-MM-DD')", "month": "TO_CHAR({}, 'YYYY-MM')",
                 "dow": "(CAST(EXTRACT(ISODOW FROM {}) AS INTEGER) - 1)", "minus": "({0} - CAST({1} AS INTEGER))",
                 "quarter": "CAST(DATE_TRUNC('quarter', {}) AS DATE)", "isoweek": "CAST(EXTRACT(WEEK FROM {}) AS INTEGER)",
                 "doy": "CAST(EXTRACT(DOY FROM {}) AS INTEGER)"},
    "redshift": {"str": "VARCHAR", "day": "TO_CHAR({}, 'YYYY-MM-DD')", "month": "TO_CHAR({}, 'YYYY-MM')",
                 "dow": "MOD(CAST(EXTRACT(DOW FROM {}) AS INTEGER) + 6, 7)", "minus": "DATEADD(day, -({1}), {0})",
                 "quarter": "DATE_TRUNC('quarter', {})", "isoweek": "CAST(EXTRACT(WEEK FROM {}) AS INTEGER)",
                 "doy": "CAST(EXTRACT(DOY FROM {}) AS INTEGER)"},
    "databricks": {"str": "STRING", "day": "DATE_FORMAT({}, 'yyyy-MM-dd')", "month": "DATE_FORMAT({}, 'yyyy-MM')",
                   "dow": "WEEKDAY({})", "minus": "DATE_SUB({0}, {1})",
                   "quarter": "DATE_TRUNC('QUARTER', {})", "isoweek": "WEEKOFYEAR({})", "doy": "DAYOFYEAR({})"},
    "duckdb": {"str": "VARCHAR", "day": "STRFTIME({}, '%Y-%m-%d')", "month": "STRFTIME({}, '%Y-%m')",
               "dow": "(ISODOW({}) - 1)", "minus": "CAST({0} - CAST({1} AS INTEGER) AS DATE)",
               "quarter": "DATE_TRUNC('quarter', {})", "isoweek": "WEEKOFYEAR({})", "doy": "DAYOFYEAR({})"},
}
# hour '2026-12-31 23', minute '2026-12-31 23:30', time_of_day '23:30' (Looker on BigQuery: FORMAT_TIMESTAMP
# '%F %H', '%F %H:%M', '%H:%M'), as each warehouse formats a timestamp
TO_CHAR = ("TO_CHAR({{}}, '{}')", {"hour": "YYYY-MM-DD HH24", "minute": "YYYY-MM-DD HH24:MI", "time_of_day": "HH24:MI"})
TIME_FORMATS = {
    "bigquery": ("FORMAT_TIMESTAMP('{}', CAST({{}} AS TIMESTAMP))", {"hour": "%F %H", "minute": "%F %H:%M", "time_of_day": "%H:%M"}),
    "snowflake": TO_CHAR, "postgres": TO_CHAR, "redshift": TO_CHAR,
    "databricks": ("DATE_FORMAT({{}}, '{}')", {"hour": "yyyy-MM-dd HH", "minute": "yyyy-MM-dd HH:mm", "time_of_day": "HH:mm"}),
    "duckdb": ("STRFTIME({{}}, '{}')", {"hour": "%Y-%m-%d %H", "minute": "%Y-%m-%d %H:%M", "time_of_day": "%H:%M"}),
}
WEEKDAYS = [d.lower() for d in calendar.day_name]  # monday first, as Looker's default
LOOKER_VALUE = {
    "month": "a 'YYYY-MM' string", "quarter": "a 'YYYY-MM' string of the quarter's first month",
    "week": "a 'YYYY-MM-DD' string of the day the week starts", "day_of_week": "the day's name ('Monday')",
    "day_of_week_index": "0-6 from week_start_day", "week_of_year": "the ISO week number",
    "day_of_year": "the day of the year", "quarter_of_year": "'Q1'-'Q4'",
    "hour": "a 'YYYY-MM-DD HH' string", "minute": "a 'YYYY-MM-DD HH:MM' string", "time_of_day": "an 'HH:MM' string",
}


def timeframe_sql(tf: str, expr: str, dialect: str, week_start: str = "monday") -> str | None:
    """Looker's value for a string or week-based timeframe in `dialect`; None when not known."""
    d = DIALECT_DATES.get(dialect)
    if d is None:
        return None
    if tf in ("hour", "minute", "time_of_day"):
        template, formats = TIME_FORMATS[dialect]
        return template.format(formats[tf]).format(expr)
    day = f"CAST({expr} AS DATE)"
    offset = WEEKDAYS.index(week_start)
    dow = d["dow"].format(day)
    index = dow if offset == 0 else f"MOD({dow} + {7 - offset}, 7)"
    if tf == "month":
        return d["month"].format(day)
    if tf == "quarter":
        return d["month"].format(d["quarter"].format(day))
    if tf == "week":
        if dialect == "bigquery":
            return d["day"].format(f"DATE_TRUNC({day}, WEEK({week_start.upper()}))")
        return d["day"].format(d["minus"].format(day, index))
    if tf == "day_of_week_index":
        return index
    if tf == "day_of_week":
        return f"CASE {dow} " + " ".join(f"WHEN {i} THEN '{n}'" for i, n in enumerate(calendar.day_name)) + " END"
    if tf == "week_of_year":
        return d["isoweek"].format(day)
    if tf == "day_of_year":
        return d["doy"].format(day)
    if tf == "quarter_of_year":
        return f"'Q' || CAST(EXTRACT(QUARTER FROM {day}) AS {d['str']})"  # CONCAT drops NULLs in some warehouses
    return None


ALL_TIMEFRAMES = [
    "raw", "time", "time_of_day", "date", "week", "month", "quarter", "year", "hour", "minute", "second", "millisecond",
    "day_of_week", "day_of_week_index", "day_of_month", "day_of_year", "week_of_year", "month_num", "month_name",
    "quarter_of_year", "hour_of_day", "fiscal_month_num", "fiscal_quarter", "fiscal_quarter_of_year", "fiscal_year",
]
UNNEST_JOIN = re.compile(
    r"^\s*(LEFT\s+(?:OUTER\s+)?JOIN|CROSS\s+JOIN|INNER\s+JOIN|JOIN|,)\s*UNNEST\s*\(\s*\$?\{?(\w+)\.(\w+)\}?\s*\)"
    r"\s*(?:AS\s+)?(\w+)\s*$", re.I)
REVERSED = {"many_to_one": "one_to_many", "one_to_many": "many_to_one", "one_to_one": "one_to_one", "many_to_many": "many_to_many"}
FAN_SAFE = ("count_distinct", "min", "max", "sum_distinct")  # same result however often a row repeats


class LookerSource(Source):
    name = "looker"
    label = "Looker"
    env_vars = ("LOOKER_BASE_URL", "LOOKER_CLIENT_ID", "LOOKER_CLIENT_SECRET", "LOOKER_LOOKML_DIR")
    required_env = ("LOOKER_BASE_URL", "LOOKER_CLIENT_ID", "LOOKER_CLIENT_SECRET")
    docs = "https://cloud.google.com/looker/docs/reference/looker-api/latest"
    manual_files = "the LookML project folder (a Git checkout, or its .zip download) or .lkml files"
    manual_gaps = (
        "dashboards and Looks: user-defined dashboards and Looks only come from the API; LookML dashboards (.dashboard.lookml) "
        "are parsed, with tile SQL written from the explore rather than Looker's own",
        "usage: no view counts",
        "generated SQL: no Looker-compiled SQL for saved queries",
        "permissions: access grants in the LookML are kept, but which users and groups hold the user attributes is not",
    )

    def __init__(self, client: Client | None = None, lookml_dir: str | None = None, base_url: str | None = None):
        self._client = client
        self._logged_in = False
        self._lookml_dir = lookml_dir
        self._base_url = base_url

    # ---------------------------------------------------------------- network

    def base_url(self) -> str:
        return (self._base_url or env("LOOKER_BASE_URL")).rstrip("/")

    def client(self) -> Client:
        if self._client is None:
            self._client = Client(f"{self.base_url()}/api/4.0", per_minute=120)
        if not self._logged_in:
            token = self._client.post(
                "/login", data={"client_id": env("LOOKER_CLIENT_ID"), "client_secret": env("LOOKER_CLIENT_SECRET")}
            ).json()["access_token"]
            self._client.set_header("Authorization", f"token {token}")
            self._logged_in = True
        return self._client

    def _try_json(self, path: str, skipped: list[str], **kw: Any) -> Any:
        try:
            return self.client().get_json(path, **kw)
        except SourceError as exc:
            if " -> 404" in str(exc) or " -> 403" in str(exc):
                skipped.append(path)
                return None
            raise

    def _usage(self, skipped: list[str]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset, limit = 0, 1000
        while True:
            page = self._try_json(
                "/content_view/search",
                skipped,
                params={"fields": "dashboard_id,look_id,view_count,last_viewed_at", "limit": limit, "offset": offset},
            )
            if not page:
                break
            rows += page
            if len(page) < limit:
                break
            offset += limit
        return rows

    def inventory(self) -> Inventory:
        inv = Inventory(tool=self.name)
        inv.notes.append(
            "Looker returns 404 for content the API user cannot see, so a non-admin export is silently partial. "
            "Use an admin API key (or one with see_lookml + access to every folder) for a complete migration."
        )
        skipped: list[str] = []
        usage = _usage_by_content(self._usage(skipped))
        if skipped:
            inv.notes.append("Usage (content_view) not readable: needs admin or see_system_activity.")
        base = self.base_url()
        for model in self.client().get_json("/lookml_models", params={"fields": "name,label,explores"}) or []:
            for ex in model.get("explores") or []:
                inv.add("explore", f"explore:{model['name']}/{ex['name']}", ex.get("label") or ex["name"], parent=model["name"])
        for d in self.client().get_json("/dashboards", params={"fields": "id,title,folder"}) or []:
            u = usage.get(("dashboard", str(d["id"])), {})
            inv.add(
                "dashboard", f"dashboard:{d['id']}", d.get("title") or str(d["id"]),
                parent=((d.get("folder") or {}).get("name") or ""),
                views=u.get("views"), last_viewed=u.get("last_viewed", ""), url=f"{base}/dashboards/{d['id']}",
            )
        for lk in self.client().get_json("/looks", params={"fields": "id,title,folder"}) or []:
            u = usage.get(("look", str(lk["id"])), {})
            inv.add(
                "look", f"look:{lk['id']}", lk.get("title") or str(lk["id"]),
                parent=((lk.get("folder") or {}).get("name") or ""),
                views=u.get("views"), last_viewed=u.get("last_viewed", ""), url=f"{base}/looks/{lk['id']}",
            )
        return inv

    def extract(self, store: RawStore, only: set[str] | None = None) -> None:
        want = _only(only)
        skipped: list[str] = []
        store.put_json("_meta.json", {"base_url": self.base_url()})
        lookml_dir = self._lookml_dir or env("LOOKER_LOOKML_DIR", required=False)
        models = self.client().get_json("/lookml_models") or []
        store.put_json("api/lookml_models.json", models)
        if lookml_dir:
            root = Path(lookml_dir).expanduser()
            for p in sorted(root.rglob("*")):
                if p.is_file() and (p.suffix == ".lkml" or p.name.endswith(".lookml")) and ".git" not in p.parts:
                    store.put_bytes(f"lookml/{p.relative_to(root).as_posix()}", p.read_bytes())
        else:
            for m in models:
                for ex in m.get("explores") or []:
                    if want and f"explore:{m['name']}/{ex['name']}" not in want and "explore" in want.get("_kinds", set()):
                        continue
                    data = self._try_json(f"/lookml_models/{m['name']}/explores/{ex['name']}", skipped)
                    if data is not None:
                        store.put_json(f"api/explores/{m['name']}/{ex['name']}.json", data)
        query_ids: set[str] = set()
        for d in self.client().get_json("/dashboards", params={"fields": "id,title"}) or []:
            if not _wanted(want, "dashboard", d["id"]):
                continue
            full = self._try_json(f"/dashboards/{d['id']}", skipped)
            if full is None:
                continue
            store.put_json(f"dashboards/{d['id']}.json", full)
            for el in full.get("dashboard_elements") or []:
                qid = _element_query_id(el)
                if qid:
                    query_ids.add(qid)
        for lk in self.client().get_json("/looks", params={"fields": "id,title"}) or []:
            if not _wanted(want, "look", lk["id"]):
                continue
            full = self._try_json(f"/looks/{lk['id']}", skipped)
            if full is None:
                continue
            store.put_json(f"looks/{lk['id']}.json", full)
            qid = str(full.get("query_id") or (full.get("query") or {}).get("id") or "")
            if qid:
                query_ids.add(qid)
        for qid in sorted(query_ids):
            try:
                store.put_text(f"queries/{qid}.sql", self.client().get(f"/queries/{qid}/run/sql").text)
            except SourceError as exc:
                skipped.append(f"/queries/{qid}/run/sql ({str(exc)[:80]})")
        usage = self._usage(skipped)
        if usage:
            store.put_json("content_usage.json", usage)
        store.put_json("_skipped.json", sorted(set(skipped)))

    # ---------------------------------------------------------------- parse

    def import_path(self, path: Path) -> dict[str, bytes]:
        if path.is_file() and not path.name.endswith((".lkml", ".lookml")):
            raise SourceError(f"{path.name}: Looker import accepts {self.manual_files}")
        return {f"lookml/{rel}": p.read_bytes() for rel, p in _manual.files_of(path) if p.name.endswith((".lkml", ".lookml"))}

    def parse(self, store: RawStore) -> Bundle:
        return _Parser(store).run()


# ==================================================================== helpers


def _only(only: set[str] | None) -> dict[str, Any] | None:
    if not only:
        return None
    out: dict[str, Any] = {"_ids": set(), "_kinds": set()}
    for item in only:
        kind, sep, rest = item.partition(":")
        if sep:
            out["_ids"].add(f"{kind}:{rest}")
            out["_kinds"].add(kind)
        else:
            out["_ids"].add(f"dashboard:{item}")
            out["_ids"].add(f"look:{item}")
            out["_kinds"].update({"dashboard", "look"})
    return out


def _wanted(want: dict[str, Any] | None, kind: str, id: Any) -> bool:
    return want is None or f"{kind}:{id}" in want["_ids"]


def _element_query_id(el: dict[str, Any]) -> str:
    q = el.get("query_id") or (el.get("query") or {}).get("id")
    if not q and el.get("look"):
        q = el["look"].get("query_id") or (el["look"].get("query") or {}).get("id")
    return str(q) if q else ""


def _element_query(el: dict[str, Any]) -> dict[str, Any]:
    if el.get("query"):
        return el["query"]
    if el.get("look") and el["look"].get("query"):
        return el["look"]["query"]
    return {}


def _usage_by_content(rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows or []:
        for kind in ("dashboard", "look"):
            cid = r.get(f"{kind}_id")
            if cid in (None, ""):
                continue
            cur = out.setdefault((kind, str(cid)), {"views": 0, "last_viewed": ""})
            cur["views"] += int(r.get("view_count") or 0)
            cur["last_viewed"] = max(cur["last_viewed"], str(r.get("last_viewed_at") or ""))
    return out


def _constants(docs: dict[str, dict[str, Any]]) -> dict[str, str]:
    """`constant:` values from manifest.lkml, with constants that use other constants expanded."""
    out: dict[str, str] = {}
    for path in sorted(docs, key=lambda p: (p.count("/"), p)):
        if Path(path).name == "manifest.lkml":
            for c in docs[path].get("constants", []):
                out.setdefault(c["name"], str(c.get("value", "")))
    for _ in range(5):
        out = {k: CONSTANT.sub(lambda m: out.get(m.group(1), m.group(0)), v) for k, v in out.items()}
    return out


def _substitute(x: Any, constants: dict[str, str]) -> Any:
    """Replace `@{name}` in every string of a parsed LookML file, as Looker does before evaluating."""
    if isinstance(x, str):
        return CONSTANT.sub(lambda m: constants.get(m.group(1), m.group(0)), x) if "@{" in x else x
    if isinstance(x, list):
        return [_substitute(y, constants) for y in x]
    if isinstance(x, dict):
        return {k: _substitute(v, constants) for k, v in x.items()}
    return x


def _include_regex(pattern: str, from_path: str) -> re.Pattern | None:
    """An `include:` pattern as a regex over project paths. `*` stays in one folder, `**` crosses
    folders, a missing `.lkml` suffix is implied, and paths without a leading `/` are relative."""
    pattern = pattern.strip()
    if not pattern or pattern.startswith("//"):  # another project's files
        return None
    path = pattern[1:] if pattern.startswith("/") else posixpath.normpath(posixpath.join(posixpath.dirname(from_path), pattern))
    out = ""
    for part in re.split(r"(\*\*/|\*\*|\*|\?)", path):
        out += {"**/": "(?:.*/)?", "**": ".*", "*": "[^/]*", "?": "[^/]"}.get(part, re.escape(part))
    return re.compile(out + ("" if path.endswith((".lkml", ".lookml")) else r"(?:\.lkml|\.lookml)?"))


def _include_closure(model_path: str, docs: dict[str, dict[str, Any]]) -> list[str]:
    """Every file a model includes, directly or through included files, in include order."""
    out: list[str] = []
    todo = [model_path]
    while todo:
        path = todo.pop(0)
        for pattern in docs[path].get("includes", []):
            rx = _include_regex(pattern, path)
            for other in sorted(docs) if rx else []:
                if rx.fullmatch(other) and other != model_path and other not in out:
                    out.append(other)
                    todo.append(other)
    return out


def _plain_table(sql_table_name: str) -> str:
    """The table name, or "" when sql_table_name is SQL (a subquery, Liquid, an undefined constant)."""
    return _strip_ident(sql_table_name) if TABLE_NAME.fullmatch(sql_table_name or "") else ""


def _source(field: dict[str, Any]) -> str:
    """What a person needs to translate a field by hand: its SQL, else its definition (case:, sql_latitude, ...)."""
    if field.get("sql"):
        return field["sql"]
    skip = ("name", "label", "description", "hidden", "view_label", "group_label", "drill_fields", "link", "html", "tags")
    return json.dumps({k: v for k, v in field.items() if k not in skip}, default=str)


def _label(field: dict[str, Any]) -> str:
    """A field's label; "" for a Liquid label (it depends on the user or the query, so the name is used)."""
    label = str(field.get("label") or "")
    return "" if LIQUID.search(label) or CONSTANT.search(label) else label


def _yes(v: Any) -> bool:
    return str(v).lower() in ("yes", "true")


def _strip_ident(name: str) -> str:
    return ".".join(p.strip().strip('"`[]') for p in name.strip().split("."))


def _split_table(fq: str) -> tuple[str, str]:
    parts = _strip_ident(fq).split(".")
    return (parts[-2] if len(parts) >= 2 else ""), parts[-1]


def _paren(expr: str) -> str:
    return expr if SIMPLE_IDENT.match(expr) or re.match(r"^[\w.]+$", expr) else f"({expr})"


def _filter_pairs(measure: dict[str, Any]) -> list[tuple[str, str]]:
    """lkml gives filters as [[{f: v}, ...]] (new syntax) or [{field, value}] (old)."""
    raw = measure.get("filters__all") or measure.get("filters") or []
    out: list[tuple[str, str]] = []

    def walk(x: Any) -> None:
        if isinstance(x, list):
            for y in x:
                walk(y)
        elif isinstance(x, dict):
            if "field" in x and "value" in x:
                out.append((x["field"], str(x["value"])))
            else:
                out.extend((k, str(v)) for k, v in x.items())

    walk(raw)
    return out


NUM = r"-?\d+(?:\.\d+)?"


def _number_filter(ref: str, v: str) -> str | None:
    """Looker's number filter expressions: lists, NOT (which drops NULLs), [a,b] / (a,b) ranges, `a to b`, AND / OR."""
    if re.fullmatch(rf"NOT\s+{NUM}(\s*,\s*{NUM})*", v, re.I):  # NOT keeps no NULLs: NOT (n = 5)
        vals = re.findall(NUM, v[3:])
        return f"NOT ({ref} = {vals[0]})" if len(vals) == 1 else f"NOT ({ref} IN ({', '.join(vals)}))"
    if re.fullmatch(rf"{NUM}(\s*,\s*{NUM})+", v):
        return f"{ref} IN ({', '.join(re.findall(NUM, v))})"
    m = re.fullmatch(rf"([\[(])\s*({NUM})\s*,\s*({NUM})\s*([\])])", v)
    if m:
        lo, hi = (">=" if m.group(1) == "[" else ">"), ("<=" if m.group(4) == "]" else "<")
        return f"({ref} {lo} {m.group(2)} AND {ref} {hi} {m.group(3)})"
    m = re.fullmatch(rf"({NUM})\s+to\s+({NUM})", v, re.I)
    if m:
        return f"{ref} BETWEEN {m.group(1)} AND {m.group(2)}"
    for joiner in ("AND", "OR"):
        ms = [re.fullmatch(rf"(>=|<=|<>|!=|>|<|=)\s*({NUM})", p.strip()) for p in re.split(rf"\s+{joiner}\s+", v, flags=re.I)]
        if len(ms) > 1 and all(ms):
            return "(" + f" {joiner} ".join(f"{ref} {m[1]} {m[2]}" for m in ms) + ")"
    return None


def _date_bounds(text: str) -> tuple[datetime, datetime | None] | None:
    """An absolute date in a filter: (start, end of the period) for a year, month or day; (moment, None) for a time."""
    t = text.strip()
    try:
        if re.fullmatch(r"\d{4}", t):
            return datetime(int(t), 1, 1), datetime(int(t) + 1, 1, 1)
        if re.fullmatch(r"\d{4}[-/]\d{2}", t):
            y, mo = int(t[:4]), int(t[5:7])
            return datetime(y, mo, 1), datetime(y + mo // 12, mo % 12 + 1, 1)
        if re.fullmatch(r"\d{4}[-/]\d{2}[-/]\d{2}", t):
            d = datetime(int(t[:4]), int(t[5:7]), int(t[8:10]))
            return d, d + timedelta(days=1)
        if re.fullmatch(r"\d{4}[-/]\d{2}[-/]\d{2} \d{2}:\d{2}(:\d{2})?", t):
            return datetime.fromisoformat(t.replace("/", "-")), None
    except ValueError:
        return None
    return None


def relative_bounds(v: str, datatype: str, dialect: str) -> tuple[str, str | None] | None:
    """(start, end) SQL of a relative date filter as Looker writes it on BigQuery:
    `N days` (today and the N-1 days before), `before N days ago` (end only: start None), `this month`,
    `last month` (dates only). None for other forms, columns and warehouses."""
    if dialect != "bigquery" or datatype not in ("date", "timestamp"):
        return None
    date = datatype == "date"
    today = "CURRENT_DATE()" if date else "TIMESTAMP_TRUNC(CURRENT_TIMESTAMP(), DAY)"
    fn = "DATE_ADD" if date else "TIMESTAMP_ADD"
    add = lambda x, n, unit: f"{fn}({x}, INTERVAL {n} {unit})"  # noqa: E731
    m = re.fullmatch(r"(\d+)\s+days?", v, re.I)
    if m and int(m.group(1)) > 0:
        n = int(m.group(1))
        start = add(today, -(n - 1), "DAY")
        return start, add(start, n, "DAY")
    m = re.fullmatch(r"before\s+(\d+)\s+days?\s+ago", v, re.I)
    if m:
        return "", add(today, -int(m.group(1)), "DAY")
    if date and v.lower() in ("this month", "last month"):
        month = "DATE_TRUNC(CURRENT_DATE(), MONTH)"
        start = month if v.lower() == "this month" else add(month, -1, "MONTH")
        return start, add(start, 1, "MONTH")
    return None


def _date_filter(ref: str, v: str, datatype: str, dialect: str = "") -> str | None:
    """Date filters as Looker writes them: a period is [start, start of the next), `before` is <, `after` is
    >= and `A to B` is [A, B); relative ones in relative_bounds. `day`: a time compared at day grain, with
    dates only (no relative dates: the grain's type is not checked)."""
    rel = relative_bounds(v.strip(), datatype, dialect)
    if rel:
        start, end = rel
        return f"{ref} < {end}" if not start else f"({ref} >= {start} AND {ref} < {end})"

    def lit(d: datetime) -> str | None:
        if datatype in ("date", "day"):
            return None if (d.hour, d.minute, d.second) != (0, 0, 0) else f"'{d:%Y-%m-%d}'"
        return f"'{d:%Y-%m-%d %H:%M:%S}'"

    m = re.fullmatch(r"(before|after)\s+(.+)", v, re.I)
    if m:
        b = _date_bounds(m.group(2))
        x = lit(b[0]) if b else None
        return None if x is None else f"{ref} {'<' if m.group(1).lower() == 'before' else '>='} {x}"
    m = re.fullmatch(r"(.+?)\s+to\s+(.+)", v, re.I)
    if m:
        a, b = _date_bounds(m.group(1)), _date_bounds(m.group(2))
        lo, hi = (lit(a[0]) if a else None), (lit(b[0]) if b else None)
        return None if lo is None or hi is None else f"({ref} >= {lo} AND {ref} < {hi})"
    b = _date_bounds(v)
    if b is None or b[1] is None:
        return None
    lo, hi = lit(b[0]), lit(b[1])
    return None if lo is None or hi is None else f"({ref} >= {lo} AND {ref} < {hi})"


def sql_string(text: str, dialect: str = "") -> str:
    """A string literal: BigQuery and Databricks escape a quote with a backslash (Looker writes 'Levi\\'s'
    on BigQuery, where 'Levi''s' is a syntax error); the others double it."""
    if dialect in ("bigquery", "databricks"):
        return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"
    return "'" + text.replace("'", "''") + "'"


def looker_filter_sql(ref: str, value: str, kind: str = "string", datatype: str = "timestamp", dialect: str = "",
                      relative: bool = True) -> str | None:
    """Translate a Looker filter expression to SQL on `ref`. None when it needs a person.

    As Looker writes them (BigQuery): string "is not" filters keep NULL rows
    (`s <> 'a' OR s IS NULL`), number `<>5` does not, a yesno "no" is `NOT COALESCE(y, FALSE)`,
    and a mixed list "-a,b" is `s = 'b' AND (s <> 'a' OR s IS NULL)`."""
    v = value.strip()
    if kind == "yesno":
        if v.lower() == "yes":
            return f"{ref} = true"
        if v.lower() == "no":
            return f"NOT COALESCE({ref}, false)"
        return None
    if v.upper() == "NULL":
        return f"{ref} IS NULL"
    if v.upper() in ("-NULL", "NOT NULL"):
        return f"{ref} IS NOT NULL"
    if v.upper() == "EMPTY":
        return f"({ref} IS NULL OR {ref} = '')"
    if v.upper() == "-EMPTY":
        return f"({ref} IS NOT NULL AND {ref} <> '')"
    m = re.fullmatch(rf"(>=|<=|<>|!=|>|<|=)?\s*({NUM})", v)
    if m and (kind == "number" or m.group(1)):
        return f"{ref} {m.group(1) or '='} {m.group(2)}"
    if kind == "number":
        return _number_filter(ref, v)
    if kind == "time":
        return _date_filter(ref, v, datatype, dialect if relative else "")
    # dates, number ranges and anything that reads like a relative date need a person; brackets
    # are literal in a string filter ("(5) Purchase")
    if kind in ("time", "number") or (kind != "string" and re.search(r"\b(ago|days?|weeks?|months?|years?|before|after|to|last|this|next)\b", v, re.I)):
        return None
    quoted = re.fullmatch(r'"([^"]*)"', v)
    if quoted:  # "Levi's": the text itself, no list, wildcard or negation
        return f"{ref} = {sql_string(quoted.group(1), dialect)}"
    if '"' in v:  # other quoting in a filter value: leave it to a person
        return None
    parts = [p.strip().replace("^,", ",") for p in re.split(r"(?<!\^),", v)]
    if any(p in ("", "-") or p.startswith("--") for p in parts):
        return None
    include = [p for p in parts if not p.startswith("-")]
    exclude = [p[1:] for p in parts if p.startswith("-")]
    lit = lambda s: sql_string(s, dialect)  # noqa: E731
    if any("%" in p for p in parts):
        if len(parts) > 1:
            return None
        return f"({ref} NOT LIKE {lit(exclude[0])} OR {ref} IS NULL)" if exclude else f"{ref} LIKE {lit(include[0])}"
    # not _semyaml.op_sql: its literals ignore the dialect's quote escaping
    conds = []
    if include:
        conds.append(f"{ref} = {lit(include[0])}" if len(include) == 1 else f"{ref} IN ({', '.join(map(lit, include))})")
    if exclude:
        cond = f"{ref} <> {lit(exclude[0])}" if len(exclude) == 1 else f"{ref} NOT IN ({', '.join(map(lit, exclude))})"
        conds.append(f"({cond} OR {ref} IS NULL)")
    return " AND ".join(conds)


# a quoted string (backslash escapes, unterminated runs to the end) is kept; a `--` comment outside one is dropped
SQL_COMMENT = re.compile(r"""('(?:[^'\\]|\\.)*'?|"(?:[^"\\]|\\.)*"?|`[^`]*`?)|--[^\n]*""")


def strip_sql_comments(sql: str) -> str:
    """Drop `-- ...` line comments outside quotes: resolved SQL is joined onto one line, where a comment
    would swallow everything after it."""
    return SQL_COMMENT.sub(lambda m: m.group(1) or "", sql)


LIQUID_TAG = re.compile(r"(\{%-?.*?-?%\}|\{\{-?.*?-?\}\})", re.S)
LIQUID_COMPARE = re.compile(r"""^([\w.]+)\s*(==|!=)\s*(['"])(.*?)\3$""")
DIALECT_NAME = {"bigquery": "bigquery_standard_sql"}  # _dialect._name, as Looker reported it


class Liquid:
    """Looker's Liquid in SQL, rendered for one query as Looker renders it:
    - `{% parameter p %}` / `{{ p._parameter_value }}`: an unquoted or number parameter as written, a string
      parameter as a quoted literal; `{% if %}` / `elsif` / `else` on `p._parameter_value == '...'`, on
      `_dialect._name` and on `f._is_filtered` / `_in_query` / `_is_selected`, joined by `or` or by `and`;
    - `{% condition f %} sql {% endcondition %}` with the filter's expression (`1=1` without one);
    - `{% date_start f %}` / `{% date_end f %}` of an absolute date filter (`NULL` without one);
    - comments. Anything else is a problem."""

    def __init__(self, parser: "_Parser", q: "_Query"):
        self.p, self.q, self.values = parser, q, q.liquid  # values: "view.field" -> the query's filter value

    def _lookup(self, view: str, name: str, kind: str) -> tuple[str, dict[str, Any]] | None:
        v, _, short = self.q.key(name, view).rpartition(".")
        f = self.p._field(v, short, (kind,))
        return None if f is None else (f"{v}.{short}", f)

    def param(self, view: str, name: str) -> tuple[str | None, str]:
        found = self._lookup(view, name, "parameters")
        if found is None:
            return None, f"Liquid reads {name}, which is not a parameter"
        key, f = found
        value = self.values.get(key, f.get("default_value"))
        if value is None:
            return None, f"parameter {name} has no value in this query and no default_value"
        ptype = str(f.get("type", "string"))
        if ptype == "unquoted":
            return str(value), ""
        if ptype == "number" and re.fullmatch(NUM, str(value).strip()):
            return str(value).strip(), ""
        if ptype == "string":
            return sql_string(str(value), self.p.dialect), ""
        return None, f"parameter {name} is type: {ptype}, which is not translated"

    def _date(self, view: str, name: str, end: bool) -> tuple[str | None, str]:
        found = self._lookup(view, name, "filters")
        if found is None or str(found[1].get("type")) != "date":
            return None, f"{{% date_{'end' if end else 'start'} {name} %}} reads a field that is not a date filter"
        value = str(self.values.get(found[0]) or "").strip()
        if not value:
            return "NULL", ""
        m = re.fullmatch(r"(.+?)\s+to\s+(.+)", value, re.I)
        if m:  # A to B: from the start of A to the start of B
            a, b = _date_bounds(m.group(1)), _date_bounds(m.group(2))
            first, last = (a[0] if a else None), (b[0] if b else None)
        else:  # one period: its start and the start of the next
            a = _date_bounds(value)
            first, last = (a[0], a[1]) if a else (None, None)
        bound = last if end else first
        rel = relative_bounds(value, "timestamp", self.p.dialect)
        if rel and rel[0]:  # a relative range: Looker's TIMESTAMP_ADD(TIMESTAMP_TRUNC(CURRENT_TIMESTAMP(), DAY), ...)
            return rel[1] if end else rel[0], ""
        if bound is None:
            return None, f"date filter {name}: {value!r} is not an absolute range (relative dates are not translated)"
        text = f"'{bound:%Y-%m-%d %H:%M:%S}'"
        return (f"TIMESTAMP({text})" if self.p.dialect == "bigquery" else f"CAST({text} AS TIMESTAMP)"), ""

    def _dialect_name(self) -> tuple[str | None, str]:
        value = DIALECT_NAME.get(self.p.dialect)
        return value, "" if value else "_dialect._name is only known for BigQuery (set the warehouse type in unshackle.toml)"

    def render(self, view: str, sql: str) -> tuple[str, list[str]]:
        problems: list[str] = []
        parts = LIQUID_TAG.split(sql)
        out, _ = self._block(view, parts, 0, problems, ())
        return out, problems

    def _block(self, view: str, parts: list[str], i: int, problems: list[str], stop: tuple[str, ...]) -> tuple[str, int]:
        out = []
        while i < len(parts):
            part = parts[i]
            if not LIQUID_TAG.fullmatch(part):
                out.append(part)
                i += 1
                continue
            body = part.strip("{}%-").strip() if part.startswith("{%") else ""
            word, arg = (body.split() + ["", ""])[:2]
            if word in stop:
                return "".join(out), i
            i += 1
            if word == "comment":
                _, i = self._block(view, parts, i, [], ("endcomment",))
                i += 1
            elif word == "condition":
                inner, i = self._block(view, parts, i, problems, ("endcondition",))
                i += 1
                found = self._lookup(view, arg, "filters")
                if found is None:
                    problems.append(f"{{% condition {arg} %}} reads a field that is not a filter")
                    continue
                key, f = found
                value = self.values.get(key)
                if value in (None, ""):
                    out.append("1=1")
                    continue
                cond = looker_filter_sql(inner.strip(), str(value), str(f.get("type", "string")), dialect=self.p.dialect)
                if cond is None:
                    problems.append(f"filter {arg}: {value!r} is not translated (a Looker filter expression)")
                else:
                    out.append(f"({cond})")
            elif word == "if":
                taken = None
                cond = body[2:].strip()
                while True:
                    text, i = self._block(view, parts, i, problems, ("elsif", "else", "endif"))
                    if taken is None and self._test(view, cond, problems):
                        taken = text
                    tag = parts[i].strip("{}%-").strip() if i < len(parts) else "endif"
                    i += 1
                    if tag.startswith("endif"):
                        break
                    cond = tag[5:].strip() if tag.startswith("elsif") else "true"
                out.append(taken or "")
            else:
                if part.startswith("{{"):
                    expr = part.strip("{}-").strip()
                    if expr.endswith("._parameter_value"):
                        value, why = self.param(view, expr[: -len("._parameter_value")])
                    elif expr == "_dialect._name":
                        value, why = self._dialect_name()
                    else:
                        value, why = None, f"Liquid output {part} is not translated"
                elif word == "parameter":
                    value, why = self.param(view, arg)
                elif word in ("date_start", "date_end"):
                    value, why = self._date(view, arg, word == "date_end")
                else:
                    value, why = None, f"Liquid tag {{% {word} %}} is not translated"
                out.append(value if value is not None else "")
                problems += [why] if why else []
        if stop:
            problems.append(f"Liquid block is missing {{% {stop[-1]} %}}")
        return "".join(out), i

    def _test(self, view: str, cond: str, problems: list[str]) -> bool:
        joiners = set(re.findall(r"\s(or|and)\s", cond))
        if len(joiners) > 1:
            problems.append(f"Liquid condition {cond!r} mixes and / or")
            return False
        atoms = re.split(r"\s+(?:or|and)\s+", cond)
        results = [self._atom(view, a.strip(), problems) for a in atoms]
        if any(r is None for r in results):
            return False
        return all(results) if joiners == {"and"} else any(results)

    def _atom(self, view: str, atom: str, problems: list[str]) -> bool | None:
        if atom == "true":
            return True
        m = LIQUID_COMPARE.match(atom)
        if m:
            subject, same = m.group(1), m.group(2) == "=="
            if subject == "_dialect._name" or subject.endswith("._parameter_value"):
                value, why = self._dialect_name() if subject == "_dialect._name" else self.param(view, subject[: -len("._parameter_value")])
                if value is None:
                    problems.append(why)
                    return None
                return (value == m.group(4)) == same
        m = re.fullmatch(r"([\w.]+)\._(is_filtered|in_query|is_selected)", atom)
        if m:
            key = self.q.key(m.group(1), view)
            sets = {"is_filtered": self.q.filtered, "is_selected": self.q.selected,
                    "in_query": self.q.selected | self.q.filtered | self.q.sorted}
            return key in sets[m.group(2)]
        problems.append(f"Liquid condition {atom!r} is not translated")
        return None


def tier_sql(expr: str, field: dict[str, Any]) -> str | None:
    """Looker's CASE for a tier dimension (integer and classic styles, as Looker labels them); None otherwise."""
    try:
        tiers = [float(t) for t in field.get("tiers") or []]
    except (TypeError, ValueError):
        return None
    style = str(field.get("style", "classic"))
    if not tiers or tiers != sorted(tiers):
        return None
    if style == "integer":
        if not all(t.is_integer() for t in tiers):
            return None
        b = [int(t) for t in tiers]
        whens = [f"WHEN {expr} < {b[0]} THEN 'Below {b[0]}'"]
        whens += [f"WHEN {expr} >= {lo} AND {expr} < {hi} THEN '{lo} to {hi - 1}'" for lo, hi in zip(b, b[1:])]
        whens.append(f"WHEN {expr} >= {b[-1]} THEN '{b[-1]} or Above'")
        other = "Undefined"
    elif style == "classic":
        if any(t and not 1e-3 <= abs(t) < 1e7 for t in tiers):  # where Looker would print 1.0E7
            return None
        f = [repr(t) for t in tiers]
        whens = [f"WHEN {expr} < {f[0]} THEN 'T00 (-inf,{f[0]})'"]
        whens += [f"WHEN {expr} >= {lo} AND {expr} < {hi} THEN 'T{i + 1:02d} [{lo},{hi})'" for i, (lo, hi) in enumerate(zip(f, f[1:]))]
        whens.append(f"WHEN {expr} >= {f[-1]} THEN 'T{len(f):02d} [{f[-1]},inf)'")
        other = "TXX Undefined"
    else:
        return None
    return f"CASE {' '.join(whens)} ELSE '{other}' END"


# ==================================================================== parser


class _Parser:
    def __init__(self, store: RawStore):
        self.store = store
        self.b = Bundle(tool="looker")
        self.views: dict[str, dict[str, Any]] = {}
        self.view_file: dict[str, str] = {}
        self.models: dict[str, dict[str, Any]] = {}
        self.grants: dict[str, dict[str, Any]] = {}
        self.fields: dict[str, tuple[str, str, str]] = {}  # "view.field" -> (kind, group/field, timeframe)
        self.metric_names: dict[tuple[str, str], str] = {}
        self.meta: dict[str, Any] = store.get_json("_meta.json") if store.exists("_meta.json") else {}
        # warehouse type from unshackle.toml: some timeframes are written in the warehouse's dialect
        self.dialect = Layout.load(store.repo_root).warehouses.get(self.b.datasource, "")
        self.week_start = ""  # week_start_day the models agree on ("" = Looker's default, Monday)
        self.cross: list[tuple[str, dict[str, Any], Measure, Metric]] = []  # measures over other views, checked after joins

    def run(self) -> Bundle:
        if self.store.glob("lookml/**/*.lkml"):
            self._load_lookml()
        elif self.store.glob("api/explores/**/*.json"):
            self.b.warn("lookml", "no raw LookML; rebuilt views from explore API JSON (set LOOKER_LOOKML_DIR for full fidelity)", "info")
            self._load_explores_json()
        else:
            self.b.warn("lookml", "no LookML or explore JSON in raw; only dashboards and looks were parsed", "warn")
        self._unnest_joins()
        self._index_fields()
        self._assign_metric_names()
        for vname, view in self.views.items():
            self._view(vname, view)
        for mname, model in self.models.items():
            self._model(mname, model)
        self._check_cross()
        # an explore file included by several models yields the same policies once per model
        self.b.row_filters = list({(r.name, r.table, r.predicate, r.todo): r for r in self.b.row_filters}.values())
        self.b.permissions = list({(p.principal, p.action, p.resource_type, p.resource_id, p.todo): p for p in self.b.permissions}.values())
        if self.store.glob("lookml/**/*.lookml"):
            self._lookml_dashboards()
        usage = _usage_by_content(self.store.get_json("content_usage.json")) if self.store.exists("content_usage.json") else {}
        for rel in self.store.glob("dashboards/*.json"):
            try:
                self._dashboard(rel, self.store.get_json(rel), usage)
            except Exception as exc:  # noqa: BLE001 - one bad dashboard must not stop the rest
                self.b.warn(rel, f"skipped: {exc}")
        for rel in self.store.glob("looks/*.json"):
            try:
                self._look(rel, self.store.get_json(rel))
            except Exception as exc:  # noqa: BLE001
                self.b.warn(rel, f"skipped: {exc}")
        if self.store.exists("_skipped.json"):
            for path in self.store.get_json("_skipped.json"):
                self.b.warn(path, "not readable by the API user during extract (404/403); export is partial")
        return self.b.settle_time_dimensions()

    def _unnest_joins(self) -> None:
        """`join: c { sql: LEFT JOIN UNNEST(${p.items}) AS c ;; }` over a view with no table: c becomes a
        view with its own SQL (the parent's key and each array element, LEFT JOIN keeping Looker's one
        row for an empty array) joined on the parent's key, so it is modelled like any other view."""
        plans: dict[str, tuple[str, str, bool] | None] = {}
        rewrites = []
        for model in self.models.values():
            for ex in model["explores"]:
                aliases = {ex["name"]: ex.get("from") or ex.get("view_name") or ex["name"]}
                aliases.update({j["name"]: j.get("from") or j["name"] for j in ex.get("joins", [])})
                for j in ex.get("joins", []):
                    m = UNNEST_JOIN.match(str(j.get("sql") or ""))
                    if not m or j.get("sql_on") or m.group(4) != j["name"]:
                        continue
                    parent_alias, field = m.group(2), m.group(3)
                    child, parent = aliases[j["name"]], aliases.get(parent_alias)
                    cv = self.views.get(child)
                    if parent is None or cv is None or cv.get("sql_table_name") or cv.get("derived_table") or self._pk(parent) is None:
                        continue
                    plan = (parent, field, m.group(1).upper().startswith("LEFT"))
                    plans[child] = plan if plans.get(child, plan) == plan else None  # one parent per child view
                    rewrites.append((j, child, parent_alias))
        built: dict[str, tuple[str, str, bool]] = {}
        for child, plan in plans.items():
            if plan is None:
                continue
            parent, field, outer = plan
            pk = self._pk(parent)
            key = self.dim_expr(parent, pk)[0]
            f = self._field(parent, field)
            col = self.resolve(parent, f.get("sql") or f"${{TABLE}}.{field}")[0] if f else field
            table = self.query_table(parent)[0]
            if not (SIMPLE_IDENT.match(key) and SIMPLE_IDENT.match(col)):
                continue
            fk = f"{snake(parent)}_key"
            self.views[child]["derived_table"] = {"sql": f"SELECT {parent}.{key} AS {fk}, {child}.* FROM {table} AS {parent} "
                                                         f"{'LEFT JOIN' if outer else 'CROSS JOIN'} UNNEST({parent}.{col}) AS {child}"}
            self.views[child].setdefault("dimensions", []).append({"name": fk, "hidden": "yes", "sql": f"${{TABLE}}.{fk}"})
            built[child] = (fk, pk["name"], outer)
            self.b.warn(f"view {child}", f"nested field {parent}.{field} (UNNEST): modelled as a view of its own, joined on {parent}.{pk['name']}", "info")
        for j, child, parent_alias in rewrites:
            if child in built:
                fk, pk_name, outer = built[child]
                j.pop("sql")
                j["sql_on"] = f"${{{parent_alias}.{pk_name}}} = ${{{j['name']}.{fk}}}"
                j["type"] = "left_outer" if outer else "inner"

    # ------------------------------------------------------------ loading

    def _load_lookml(self) -> None:
        try:
            import lkml
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise SourceError("parsing LookML needs the `looker` extra: uv pip install 'unshackle[looker]'") from exc
        docs: dict[str, dict[str, Any]] = {}  # path inside the project -> parsed file
        for rel in self.store.glob("lookml/**/*.lkml"):
            try:
                docs[rel.removeprefix("lookml/")] = lkml.load(self.store.get_text(rel))
            except Exception as exc:  # noqa: BLE001
                self.b.warn(rel, f"LookML did not parse: {exc}")
        constants = _constants(docs)
        if constants:
            docs = {path: _substitute(doc, constants) for path, doc in docs.items()}
        closures = {p: _include_closure(p, docs) for p, d in sorted(docs.items()) if p.endswith(".model.lkml") or d.get("connection")}
        used = set(closures).union(*closures.values())
        refinements: list[tuple[str, dict[str, Any]]] = []
        for path, doc in docs.items():
            for v in doc.get("views", []):
                if v["name"].startswith("+"):
                    refinements.append((path, v))
                    continue
                other = self.view_file.get(v["name"])
                if other:
                    keep = other if other in used and path not in used else path
                    self.b.warn(f"view {v['name']}", f"defined in both {other} and {path}; using {keep} (the one a model includes, else the last)")
                    if keep == other:
                        continue
                self.views[v["name"]] = v
                self.view_file[v["name"]] = path
            for g in doc.get("access_grants", []):
                self.grants[g["name"]] = g
        self._load_models(docs, closures)
        # Looker applies refinements to the view first, then resolves extends, so a view that
        # extends a refined view gets the refinement, and a refinement may add `extends`.
        for path, ref in refinements:
            base = ref["name"][1:]
            if base not in self.views:
                self.b.warn(f"lookml/{path}", f"refinement +{base} has no base view")
                continue
            self.views[base] = _merge_view(self.views[base], ref, refine=True)
        for name, v in list(self.views.items()):
            if v.get("extends") or v.get("extends__all"):
                self.views[name] = self._extend(v, {name}, self.views, "view")

    def _load_models(self, docs: dict[str, dict[str, Any]], closures: dict[str, list[str]]) -> None:
        """Models and the explores they include (explores often live in their own files)."""
        included = set().union(*closures.values())
        starts: set[str] = set()
        explores: dict[str, list[dict[str, Any]]] = {}  # model file -> explores, in include order
        for mpath, paths in closures.items():
            explores[mpath] = list(docs[mpath].get("explores", []))
            for path in paths:
                if path not in closures:
                    explores[mpath] += docs[path].get("explores", [])
        for path, doc in docs.items():
            if path not in closures and doc.get("explores") and path not in included:
                explores[path] = list(doc["explores"])  # explores no model includes: keep them under their file
        for path, exs in explores.items():
            name = Path(path).name.removesuffix(".lkml").removesuffix(".model")
            model = self.models.setdefault(name, {"explores": [], "file": path})
            doc = docs[path]
            model["connection"] = doc.get("connection", model.get("connection", ""))
            model["label"] = doc.get("label", model.get("label", ""))
            if doc.get("week_start_day"):
                starts.add(str(doc["week_start_day"]).lower())
            model["explores"] += self._resolve_explores(path, exs)
        if starts:
            self.week_start = starts.pop() if len(starts) == 1 else "different days per model"

    def _resolve_explores(self, path: str, explores: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Apply explore refinements (`explore: +x`) and `extends`; drop `extension: required` explores."""
        by_name: dict[str, dict[str, Any]] = {}
        refinements = []
        for ex in explores:
            if ex["name"].startswith("+"):
                refinements.append(ex)
            else:
                by_name[ex["name"]] = ex
        for ref in refinements:
            base = ref["name"][1:]
            if base not in by_name:
                self.b.warn(f"lookml/{path}", f"explore refinement +{base} has no base explore")
                continue
            by_name[base] = _merge_view(by_name[base], ref, refine=True)
        for name, ex in list(by_name.items()):
            if ex.get("extends") or ex.get("extends__all"):
                by_name[name] = self._extend(ex, {name}, by_name, "explore")
        return [ex for ex in by_name.values() if ex.get("extension") != "required"]

    def _extend(self, obj: dict[str, Any], seen: set[str], pool: dict[str, dict[str, Any]], kind: str) -> dict[str, Any]:
        parents = obj.get("extends") or [p for group in obj.get("extends__all", []) for p in group]
        out: dict[str, Any] = {}
        for p in parents:
            if p in seen or p not in pool:
                self.b.warn(f"{kind} {obj['name']}", f"extends {p} which was not found")
                continue
            out = _merge_view(out, self._extend(pool[p], seen | {p}, pool, kind))
        merged = _merge_view(out, obj)
        merged["name"] = obj["name"]
        if obj.get("extension"):
            merged["extension"] = obj["extension"]
        return merged

    def _load_explores_json(self) -> None:
        for rel in self.store.glob("api/explores/**/*.json"):
            ex = self.store.get_json(rel)
            model = self.models.setdefault(ex.get("model_name") or Path(rel).parent.name, {"explores": [], "file": rel})
            model["connection"] = ex.get("connection_name", "")
            base = ex.get("view_name") or ex["name"]
            tables = {base: ex.get("sql_table_name") or ""}
            joins = []
            for j in ex.get("joins") or []:
                tables[j.get("from") or j["name"]] = j.get("sql_table_name") or ""
                joins.append({k: j.get(k) for k in ("name", "from", "sql_on", "relationship", "foreign_key") if j.get(k)})
            explore = {
                "name": ex["name"],
                "label": ex.get("label", ""),
                "description": ex.get("description", ""),
                "joins": joins,
                "access_filters": ex.get("access_filters") or [],
                "sql_always_where": ex.get("sql_always_where") or "",
            }
            if base != ex["name"]:
                explore["from"] = base
            model["explores"].append(explore)
            fields = ex.get("fields") or {}
            for kind in ("dimensions", "measures"):
                for f in fields.get(kind) or []:
                    vname = f.get("view") or f["name"].split(".")[0]
                    view = self.views.setdefault(vname, {"name": vname, "dimensions": [], "dimension_groups": [], "measures": []})
                    if tables.get(vname) and not view.get("sql_table_name"):
                        view["sql_table_name"] = tables[vname]
                    self.view_file.setdefault(vname, rel)
                    short = f["name"].split(".", 1)[-1]
                    item: dict[str, Any] = {"name": short}
                    for k in ("sql", "description", "label_short", "hidden", "primary_key"):
                        if f.get(k) not in (None, "", False):
                            item["label" if k == "label_short" else k] = "yes" if f[k] is True else f[k]
                    if kind == "measures":
                        item["type"] = f.get("type") or "number"
                        if any(m["name"] == short for m in view["measures"]):
                            continue
                        view["measures"].append(item)
                    elif f.get("dimension_group"):
                        gname = f["dimension_group"].split(".", 1)[-1]
                        group = next((g for g in view["dimension_groups"] if g["name"] == gname), None)
                        if group is None:
                            group = {"name": gname, "type": "time", "timeframes": [], "sql": f.get("sql", "")}
                            view["dimension_groups"].append(group)
                        tf = short.removeprefix(gname + "_")
                        if tf not in group["timeframes"]:
                            group["timeframes"].append(tf)
                    else:
                        item["type"] = f.get("type") or "string"
                        if not any(d["name"] == short for d in view["dimensions"]):
                            view["dimensions"].append(item)

    # ------------------------------------------------------------ indexing

    def _index_fields(self) -> None:
        for vname, v in self.views.items():
            for d in v.get("dimensions", []):
                self.fields[f"{vname}.{d['name']}"] = ("dimension", d["name"], "")
            for g in v.get("dimension_groups", []):
                tfs = g.get("timeframes") or ALL_TIMEFRAMES  # Looker adds every timeframe when none are listed
                for tf in tfs:
                    self.fields[f"{vname}.{g['name']}_{tf}"] = ("time", g["name"], tf)
            for m in v.get("measures", []):
                self.fields[f"{vname}.{m['name']}"] = ("measure", m["name"], "")
            for kind in ("parameter", "filter"):  # values for Liquid, not columns
                for f in v.get(f"{kind}s", []):
                    self.fields.setdefault(f"{vname}.{f['name']}", (kind, f["name"], ""))

    def _assign_metric_names(self) -> None:
        counts: dict[str, int] = {}
        for v in self.views.values():
            for m in v.get("measures", []):
                counts[snake(m["name"])] = counts.get(snake(m["name"]), 0) + 1
        for vname, v in self.views.items():
            for m in v.get("measures", []):
                n = snake(m["name"])
                if counts[n] > 1 or n == "count":
                    n = f"{snake(vname)}_{n}"
                self.metric_names[(vname, m["name"])] = n
        # a qualified name can meet a measure already called that (`count` next to `orders_count`)
        taken = {n for (_, m), n in self.metric_names.items() if n == snake(m)}
        for key, n in self.metric_names.items():
            if n != snake(key[1]) and n in taken:
                i = 2
                while f"{n}_{i}" in taken:
                    i += 1
                self.b.warn(f"view {key[0]} measure {key[1]}", f"named {n}_{i}: {n} is another measure's name", "info")
                n = self.metric_names[key] = f"{n}_{i}"
            taken.add(n)

    def _field(self, vname: str, name: str, kinds: tuple[str, ...] = ("dimensions", "measures", "dimension_groups")) -> dict[str, Any] | None:
        v = self.views.get(vname) or {}
        for kind in kinds:
            for f in v.get(kind, []):
                if f["name"] == name:
                    return f
        return None

    def _group(self, vname: str, name: str) -> dict[str, Any]:
        return next((x for x in (self.views.get(vname) or {}).get("dimension_groups", []) if x["name"] == name), {})

    def _pk(self, vname: str) -> dict[str, Any] | None:
        for d in (self.views.get(vname) or {}).get("dimensions", []):
            if _yes(d.get("primary_key")):
                return d
        return None

    def table_name(self, vname: str) -> str:
        return _plain_table((self.views.get(vname) or {}).get("sql_table_name", "") or "")

    def query_table(self, vname: str, q: _Query | None = None) -> tuple[str, str, str]:
        """(table, note, problem): the table to read in a query, as LookML writes it (quoting kept).
        A derived table is read under the name it is materialized as; one with Liquid is rendered for
        the query and inlined, as Looker does."""
        v = self.views.get(vname) or {}
        raw = str(v.get("sql_table_name") or "")
        if raw and TABLE_NAME.fullmatch(raw) and not LIQUID.search(raw):
            return raw.strip(), "", ""
        dt_sql = str((v.get("derived_table") or {}).get("sql") or "")
        if q is not None and LIQUID.search(dt_sql):
            rendered, problems = Liquid(self, q).render(vname, dt_sql)
            if problems or LIQUID.search(rendered) or "${" in rendered:
                return snake(vname), "", f"derived table {vname}: " + ("; ".join(problems) or "reads other views (${...}) inside Liquid")
            return f"({rendered.strip()})", "", ""
        if v.get("derived_table") or raw:
            return snake(vname), f"reads {vname}, a derived table: materialize it as `{snake(vname)}` first (see its query in queries/)", ""
        return snake(vname), "", f"view {vname} has no table"

    def model_table(self, vname: str) -> str:
        """The table the view's semantic model reads: its table, or the view name for SQL-backed views."""
        v = self.views.get(vname) or {}
        return self.table_name(vname) or (snake(vname) if v.get("derived_table") or v.get("sql_table_name") else "")

    # ------------------------------------------------------------ SQL resolution

    def resolve(self, vname: str, sql: str, seen: frozenset[str] = frozenset()) -> tuple[str, list[str]]:
        """Turn LookML dimension SQL into a column expression on the view's table."""
        return self._expand(vname, sql, "table", seen)

    def cross_expr(self, vname: str, sql: str) -> tuple[str, list[str]]:
        """LookML SQL that reads fields of other views, as an expression over dimensions:
        `${sale_price} - ${inventory_items.cost}` -> `{{ Dimension('order_items__sale_price') }} - {{ Dimension('inventory_items__cost') }}`.
        Fields that are themselves expressions over other views are inlined."""
        return self._expand(vname, sql, "cross", frozenset())

    def _expand(self, vname: str, sql: str, mode: str, seen: frozenset[str], q: _Query | None = None, alias: str = "") -> tuple[str, list[str]]:
        """Resolve `${...}` references in LookML SQL.

        mode "table": columns of the view's own table (`${TABLE}.x` -> `x`); other views are a problem.
        mode "cross": every field reference becomes a `{{ Dimension(...) }}` template (see cross_expr).
        mode "query": inside explore query `q`, where the view is aliased `alias` (`${TABLE}.x` -> `alias.x`)
        and `${other.f}` reads the joined view `other`, which `q` then joins.
        """
        problems: list[str] = []
        sql = strip_sql_comments(sql or "")
        if LIQUID.search(sql) and q is not None:
            sql, problems = Liquid(self, q).render(vname, sql)
            if problems:
                return sql, problems
        if LIQUID.search(sql):
            return sql, ["uses Liquid templating"]
        if CONSTANT.search(sql):
            return sql, [f"uses constant {CONSTANT.search(sql).group(0)} that manifest.lkml does not define"]

        def sub(m: re.Match) -> str:
            ref = m.group(1).strip()
            if ref == "TABLE":
                if mode == "query":
                    return alias
                if mode == "cross":
                    problems.append("reads a column of the table directly (${TABLE}) next to fields of another view; use the dimension instead")
                    return m.group(0)
                return "\x00TABLE\x00"
            qual, _, name = ref.rpartition(".")
            if mode == "query":
                assert q is not None
                at = q.alias(qual) if qual else alias
                view = q.view_of(at)
                if view is None:
                    problems.append(f"references {ref}, but {at} is not in explore {q.explore['name']}")
                    return m.group(0)
            else:
                at, view = qual or vname, qual or vname
                if mode == "cross" and qual and qual != vname:
                    targets = {v for _, a, v, _, _, _ in self._links(vname) if a == qual}
                    if len(targets) > 1:
                        problems.append(f"references {ref}, but explores join {qual} as different views ({', '.join(sorted(targets))})")
                        return m.group(0)
                    view = targets.pop() if targets else qual
            if name == "SQL_TABLE_NAME":
                t = self.table_name(view)
                if not t:
                    problems.append(f"references derived table {view}.SQL_TABLE_NAME")
                    return snake(view)
                return t
            if view != vname and mode == "table":
                problems.append(self._other_view_problem(vname, at, name))
                return m.group(0)
            key = f"{at}.{name}"
            if key in seen:
                problems.append(f"circular reference {ref}")
                return m.group(0)
            if mode == "query" and at != alias:
                problems.extend(q.use(at))
            expr, p = self._field_sql(view, name, mode, seen | {key}, q, at)
            problems.extend(p)
            return expr

        out = REF.sub(sub, sql)
        out = out.replace("\x00TABLE\x00.", "")
        if "\x00TABLE\x00" in out:
            problems.append("uses ${TABLE} without a column")
            out = out.replace("\x00TABLE\x00", "${TABLE}")
        return " ".join(out.split()), list(dict.fromkeys(problems))

    def _field_sql(self, view: str, name: str, mode: str, seen: frozenset[str], q: _Query | None, at: str) -> tuple[str, list[str]]:
        """SQL for a referenced field `view.name` (`at` is its alias in a query)."""
        ref = name if mode == "table" else f"{at}.{name}"
        f = self._field(view, name)
        if f is None:
            kind = self.fields.get(f"{view}.{name}")
            if kind and kind[0] == "time":
                return self._timeframe(view, kind[1], kind[2], mode, seen, q, at)
            return "", [f"unknown field {ref}"]
        if any(f is x for x in self.views[view].get("measures", [])):
            if mode == "query" and q is not None and q.in_measure:
                return q.measure(at, view, f, seen)
            return "", [f"references measure {ref}"]
        if mode == "cross" and not self.dim_expr(view, f)[1]:
            # a dimension that translates on its own is a field of its dataset; otherwise it is inlined
            if _yes(f.get("primary_key")):
                return "", [f"references primary key {ref}, which is an entity rather than a dimension"]
            return f"{{{{ Dimension('{snake(view)}__{snake(name)}') }}}}", []
        if f.get("case"):
            expr, problems = self._case(view, f["case"], mode, seen, q, at)
            return f"({expr})", problems
        expr, problems = self._expand(view, f.get("sql") or f"${{TABLE}}.{name}", mode, seen, q, at)
        return _paren(expr), problems

    def _timeframe(self, view: str, group: str, tf: str, mode: str, seen: frozenset[str], q: _Query | None, at: str) -> tuple[str, list[str]]:
        """`${created_month}`: the dimension_group's SQL with Looker's truncation for the timeframe."""
        ref = f"{group}_{tf}" if mode == "table" else f"{at}.{group}_{tf}"
        g = self._group(view, group)
        datatype = str(g.get("datatype") or "timestamp")
        if datatype in ("epoch", "yyyymmdd"):
            return "", [f"references timeframe {ref} of a datatype: {datatype} dimension_group (the column is a number, not a date)"]
        if mode == "cross":
            if self.resolve(view, g.get("sql") or f"${{TABLE}}.{group}")[1]:
                return "", [f"references timeframe {ref} of a dimension_group that is not translated"]
            expr, problems = f"{{{{ Dimension('{snake(view)}__{snake(group)}') }}}}", []
        else:
            expr, problems = self._expand(view, g.get("sql") or f"${{TABLE}}.{group}", mode, seen, q, at)
            expr = _paren(expr)
        if problems:
            return "", problems
        if q is not None and tf != "raw" and datatype != "date" and str(g.get("convert_tz", "yes")).lower() != "no":
            q.tz.add(f"{at}.{group}_{tf}")
        if tf in ("raw", "time"):
            return expr, []
        if tf == "date" and datatype == "date":
            return expr, []
        if tf == "hour_of_day" and datatype == "date":
            return "", [f"references timeframe {ref}, an hour of a date column"]
        if tf in TIMEFRAME_SQL:
            return f"({TIMEFRAME_SQL[tf].format(expr)})", []
        if tf in ("hour", "minute", "time_of_day") and datatype == "date":
            return "", [f"references timeframe {ref}, a time of a date column"]
        if tf in LOOKER_VALUE:
            if self.week_start not in ("", *WEEKDAYS):
                return "", [f"references timeframe {ref}; weeks start on {self.week_start} in this project, write the truncation by hand"]
            sql = timeframe_sql(tf, expr, self.dialect, self.week_start or "monday")
            if sql:
                return f"({sql})", []
            return "", [f"references timeframe {ref}: Looker returns {LOOKER_VALUE[tf]}, written in the warehouse's dialect; "
                        f"set [datasources.{self.b.datasource}] type in unshackle.toml and re-run build"]
        return "", [f"references timeframe {ref}: Looker's {tf} timeframe is not translated"]

    def _other_view_problem(self, vname: str, alias: str, name: str) -> str:
        """Why `${alias.name}` in view `vname` stays a TODO, naming the explores that join them."""
        links = [x for x in self._links(vname) if x[1] == alias]
        if not links:
            return f"references {alias}.{name} in another view, but no explore joins {alias} with {vname}"
        ex, _, _, rel, on, _ = links[0]
        more = f" and {len(links) - 1} other explore(s)" if len(links) > 1 else ""
        return (f"references {alias}.{name} in another view (explore {ex} joins {alias} {rel} on {on}{more}); a dimension "
                f"reads one table, so group by {alias}.{name} through the join, or compute it in a SQL model")

    def _links(self, vname: str) -> list[tuple[str, str, str, str, str, str]]:
        """(explore, alias, view, relationship, on, join type) for each view an explore puts next to `vname`: its joins
        (relationship as declared), and the explore's base when `vname` is joined (relationship reversed).
        `on` names views rather than aliases, with the two sides of an equality in a fixed order."""
        out = []
        for model in self.models.values():
            for ex in model["explores"]:
                base = _base_view(ex)
                alias = {base: base} | _aliases(ex)

                def on(j: dict[str, Any]) -> str:
                    text = REF.sub(lambda m: "${" + ".".join([alias.get(m.group(1).split(".")[0], m.group(1).split(".")[0]),
                                                              *m.group(1).split(".")[1:]]) + "}", _join_on_text(j))
                    sides = re.fullmatch(r"\s*(\$\{[\w.]+\})\s*=\s*(\$\{[\w.]+\})\s*", text)
                    return " = ".join(sorted(sides.groups())) if sides else text

                mine = [j for j in ex.get("joins", []) if (j.get("from") or j["name"]) == vname]
                if base != vname and not mine:
                    continue
                for j in ex.get("joins", []):
                    if (j.get("from") or j["name"]) != vname:
                        out.append((ex["name"], j["name"], j.get("from") or j["name"], str(j.get("relationship", "many_to_one")), on(j),
                                    str(j.get("type", "left_outer"))))
                for j in mine if base != vname else []:
                    out.append((ex["name"], ex["name"], base, REVERSED.get(str(j.get("relationship", "many_to_one")), "many_to_many"), on(j),
                                str(j.get("type", "left_outer"))))
        return out

    def _other_views(self, vname: str, expr: str) -> set[str]:
        """Views (snake-cased) other than `vname` whose dimensions a cross expression reads."""
        return {v for v, _, _ in FILTER_REF.findall(expr) if v != snake(vname)}

    def _join_signatures(self, vname: str, target: str) -> dict[str, str]:
        """How explores put view `target` next to `vname`: "x_to_one|<relationship> on <condition>" -> explore.
        For rows of `vname`, many_to_one and one_to_one are the same: no row repeats."""
        out: dict[str, str] = {}
        for ex, _, view, rel, on, _ in self._links(vname):
            if view == target:
                kind = "x_to_one" if rel in ("many_to_one", "one_to_one") else rel
                out.setdefault(f"{kind} on {on}", f"{ex} ({rel})")
        return out

    def _check_cross(self) -> None:
        """A measure over fields of other views is faithful when every explore joins those views the
        same way, x_to_one on their primary key (no fan-out of the measure's rows): the semantic models
        then relate through entities and the metric sums joined rows the way Looker does."""
        models = {snake(sm.name): sm for sm in self.b.semantic_models}
        for vname, _, measure, metric in self.cross:
            reach = _reachable(snake(vname), models)
            problem = ""
            for other in sorted(self._other_views(vname, measure.expr)):
                view = next((v for v in self.views if snake(v) == other), other)
                sigs = self._join_signatures(vname, view)
                if not sigs:
                    problem = f"reads fields of {view}, but no explore joins {view} with {vname}"
                elif len(sigs) > 1:
                    problem = (f"reads fields of {view}, which explores join differently ("
                               + "; ".join(f"{ex}: {sig}" for sig, ex in sigs.items()) + "), so the number depends on the explore")
                elif other not in reach:
                    (sig, ex), = sigs.items()
                    if sig.startswith("x_to_one"):
                        problem = (f"reads fields of {view}, joined in explore {ex} on {sig.split(' on ', 1)[1]}, which is not a key of "
                                   f"{vname} to {view}'s primary key, so the semantic models have no relationship; "
                                   f"add the entity by hand if each {vname} row matches one {view} row")
                    else:
                        problem = (f"reads fields of {view}, which explore {ex} joins on {sig.split(' on ', 1)[1]}: rows of {vname} repeat "
                                   f"and Looker dedupes them with symmetric aggregates. Pre-aggregate {view} per {vname} row in a "
                                   f"SQL model, or define the measure on {view}")
                if not problem:
                    other_types = sorted({(t, ex) for ex, _, v, _, _, t in self._links(vname) if v == view and t != "left_outer"})
                    if other_types and not _null_propagating(measure.expr, vname, view):
                        t, ex = other_types[0]
                        problem = (f"reads fields of {view}, which explore {ex} joins type: {t}: rows without a match are "
                                   f"{'dropped' if t == 'inner' else 'added'} there, and this expression doesn't ignore them")
                if problem:
                    break
            if problem:
                models[snake(vname)].measures.remove(measure)
                metric.measure, metric.filter, metric.todo = "", "", problem
                metric.source_expression, metric.source_language = measure.source_expression, "lookml"

    def dim_expr(self, vname: str, field: dict[str, Any]) -> tuple[str, list[str]]:
        if field.get("case"):
            return self._case(vname, field["case"])
        return self.resolve(vname, field.get("sql") or f"${{TABLE}}.{field['name']}")

    def _case(self, vname: str, case: dict[str, Any], mode: str = "table", seen: frozenset[str] = frozenset(),
              q: _Query | None = None, at: str = "") -> tuple[str, list[str]]:
        parts, problems = ["CASE"], []
        for w in case.get("whens", []):
            expr, p = self._expand(vname, w.get("sql", ""), mode, seen, q, at)
            problems += p
            parts.append(f"WHEN {expr} THEN {sql_string(str(w.get('label', '')), self.dialect)}")
        if case.get("else"):
            parts.append(f"ELSE {sql_string(str(case['else']), self.dialect)}")
        parts.append("END")
        return " ".join(parts), list(dict.fromkeys(problems))

    def field_ref(self, vname: str, ref: str) -> tuple[str, str, str] | None:
        """Filter field -> ("{{ Dimension('v__f') }}", kind, datatype of a time)."""
        view, _, name = ref.rpartition(".")
        view = view or vname
        kind = self.fields.get(f"{view}.{name}")
        if not kind:
            return None
        if kind[0] == "time":
            # Looker filters the time itself, whatever the timeframe: the dimension at second grain when it
            # has one (the time itself), else at day grain, compared with dates
            exact = _grain(self._group(view, kind[1])) == "second"
            grain, datatype = ("second", "timestamp") if exact else ("day", "day")
            return f"{{{{ TimeDimension('{snake(view)}__{snake(kind[1])}', '{grain}') }}}}", "time", datatype
        if kind[0] != "dimension":
            return None
        f = self._field(view, name) or {}
        t = f.get("type", "string")
        kind = "string" if t == "tier" else _type_kind(t, "other")  # a tier is its label: '5 to 9'
        return f"{{{{ Dimension('{snake(view)}__{snake(name)}') }}}}", kind, ""

    # ------------------------------------------------------------ views

    def _view(self, vname: str, v: dict[str, Any]) -> None:
        item = f"view {vname}"
        src = self.view_file.get(vname, "")
        if v.get("extension") == "required":
            self.b.warn(item, "extension: required view (only extended by other views); skipped", "info")
            return
        table = self.table_name(vname)
        dt = v.get("derived_table")
        what = "derived table"
        if not dt and not table and v.get("sql_table_name"):
            dt, what = {"sql": v["sql_table_name"]}, "sql_table_name that is SQL rather than a table name"
        if dt and not table:
            table = snake(vname)
            if dt.get("sql"):
                sql, problems = self.resolve(vname, dt["sql"])
            elif dt.get("explore_source"):
                sql, problems = "", ["native derived table (explore_source)"]
            else:
                sql, problems = "", ["no `sql` (sql_create or create_process runs custom DDL, e.g. a BigQuery ML model)"]
            pdt = [k for k in ("datagroup_trigger", "persist_for", "sql_trigger_value", "materialized_view", "increment_key") if dt.get(k)]
            todo = (
                f"Looker {'PDT (' + ', '.join(f'{k}: {dt[k]}' for k in pdt) + ')' if pdt else what}: "
                f"materialize this SQL as a table or dbt model named `{table}` (the semantic model `{snake(vname)}` points at it)"
            )
            if problems:
                todo += "; also " + "; ".join(problems)
            body = sql or json.dumps(dt.get("explore_source") or dt, indent=2, default=str)
            self.b.queries.append(
                VerifiedQuery(
                    name=f"{vname} derived table",
                    sql=body if sql else "-- " + body.replace("\n", "\n-- "),
                    description=f"Looker {what} {vname}",
                    datasource=self.b.datasource,
                    source_ref=f"{src} view: {vname}",
                    todo=todo,
                )
            )
        elif not table:
            self.b.warn(item, "view has no sql_table_name or derived_table; skipped (probably a field-only or extension view)", "info")
            return
        schema, tname = _split_table(table)
        tbl = Table(self.b.datasource, schema, tname, source_ref=f"{src} view: {vname}") if not dt else None
        sm = SemanticModel(name=vname, table=table, datasource=self.b.datasource, source_ref=f"{src} view: {vname}")
        pk = self._pk(vname)
        if pk:
            expr, problems = self.dim_expr(vname, pk)
            if problems:
                self.b.warn(item, f"primary key {pk['name']} {'; '.join(problems)}; no primary entity, add it by hand")
                pk = None
            else:
                sm.entities.append(Entity(snake(vname), "primary", "" if expr == snake(vname) else expr))
        for d in v.get("dimensions", []):
            if d is pk:
                if tbl is not None:
                    tbl.columns.append(Column(self.dim_expr(vname, d)[0], COLUMN_TYPE.get(d.get("type", "string"), ""), d.get("description", ""), _label(d)))
                continue
            expr, problems = self.dim_expr(vname, d)
            dtype = d.get("type", "string")
            dim = Dimension(
                name=d["name"],
                expr="" if expr == d["name"] else expr,
                description=d.get("description", ""),
                label=_label(d),
            )
            if dtype == "tier" and not problems:
                tier = tier_sql(_paren(expr), d)
                if tier:
                    dim.expr = tier
                else:
                    problems.append(f"type: tier with style: {d.get('style', 'classic')} and tiers {d.get('tiers')} is not translated")
            elif dtype in ("location", "distance", "duration"):
                problems.append(f"type: {dtype} has no MetricFlow equivalent")
            if problems:
                dim.todo, dim.source_expression, dim.source_language = "; ".join(problems), _source(d), "lookml"
                dim.expr = ""
            sm.dimensions.append(dim)
            if tbl is not None and SIMPLE_IDENT.match(expr) and not problems and dtype != "tier":
                tbl.columns.append(Column(expr, COLUMN_TYPE.get(dtype, ""), d.get("description", ""), _label(d), _yes(d.get("hidden"))))
        for g in v.get("dimension_groups", []):
            if g.get("type", "time") != "time":
                sm.dimensions.append(
                    Dimension(g["name"], todo=f"dimension_group type: {g.get('type')} (interval between two times)",
                              source_expression=json.dumps({k: g[k] for k in g if k != "name"}), source_language="lookml")
                )
                continue
            expr, problems = self.resolve(vname, g.get("sql") or f"${{TABLE}}.{g['name']}")
            grain = _grain(g)
            dim = Dimension(g["name"], "time", "" if expr == g["name"] else expr, g.get("description", ""), _label(g), grain)
            if g.get("datatype") in ("epoch", "yyyymmdd"):
                problems.append(f"datatype: {g['datatype']} (the column is a number, not a date); convert it to a date or timestamp in the expression")
            if problems:
                dim.todo, dim.source_expression, dim.source_language, dim.expr = "; ".join(problems), _source(g), "lookml", ""
            sm.dimensions.append(dim)
            if tbl is not None and SIMPLE_IDENT.match(expr) and not problems:
                ctype = "date" if g.get("datatype") == "date" else "timestamp"
                tbl.columns.append(Column(expr, ctype, g.get("description", ""), _label(g), _yes(g.get("hidden"))))
        taken = {e.name for e in sm.entities}
        for d in sm.dimensions:
            if snake(d.name) in taken:
                self.b.warn(item, f"dimension {d.name} clashes with entity name; renamed {d.name}_dim")
                d.name = f"{d.name}_dim"
        for m in v.get("measures", []):
            self._measure(vname, m, sm, pk)
        if sm.measures and not any(d.type == "time" for d in sm.dimensions):
            self.b.warn(item, "has measures but no time dimension; MetricFlow needs defaults.agg_time_dimension", "info")
        self.b.semantic_models.append(sm)
        if tbl is not None:
            self.b.tables.append(tbl)
        for fld in [*v.get("dimensions", []), *v.get("dimension_groups", []), *v.get("measures", [])]:
            if fld.get("required_access_grants"):
                is_dim = any(fld is d for d in v.get("dimensions", []))
                column = self.dim_expr(vname, fld)[0] if is_dim else fld["name"]
                extra = ""
                if is_dim and not SIMPLE_IDENT.match(column):
                    extra = f"; the field is an expression ({column}), so protect each column it reads"
                    column = fld["name"]
                rtype = "Column" if is_dim else "Metric"
                rid = f"{table}.{column}" if is_dim else self.metric_names.get((vname, fld["name"]), snake(fld["name"]))
                self._grant_permissions(fld["required_access_grants"], rtype, rid, f"{src} view: {vname} field: {fld['name']}", extra)
        if v.get("required_access_grants"):
            self._grant_permissions(v["required_access_grants"], "Table", table, f"{src} view: {vname}")

    def _measure(self, vname: str, m: dict[str, Any], sm: SemanticModel, pk: dict[str, Any] | None) -> None:
        mtype = m.get("type", "count")
        name = self.metric_names[(vname, m["name"])]
        src = f"{self.view_file.get(vname, '')} view: {vname} measure: {m['name']}"
        raw_sql = m.get("sql", "")
        metric = Metric(name=name, label=_label(m) or m["name"].replace("_", " ").title(),
                        description=m.get("description", ""), semantic_model=vname, source_ref=src)
        source_expr = f"type: {mtype}" + (f" sql: {raw_sql}" if raw_sql else "") + "".join(
            f" {k}: {m[k]}" for k in ("sql_distinct_key", "list_field", "direction", "percentile") if m.get(k)
        )

        def todo(msg: str) -> None:
            metric.todo, metric.source_expression, metric.source_language = msg, source_expr, "lookml"
            self.b.metrics.append(metric)

        if LIQUID.search(raw_sql):
            return todo("uses Liquid templating; rewrite as a plain SQL measure (or one metric per branch)")
        if CONSTANT.search(raw_sql):
            return todo(f"uses constant {CONSTANT.search(raw_sql).group(0)} that manifest.lkml does not define")
        pct = float(m.get("percentile", 50)) if mtype == "percentile" else 50.0
        if pct != 50 and self.dialect in ("", "bigquery"):
            return todo(
                f"percentile {pct:g} on BigQuery: Looker averages the two values around position n * {pct / 100:g} of the sorted "
                "values (APPROX_QUANTILES above 10,000 rows), which is neither PERCENTILE_CONT nor PERCENTILE_DISC; "
                "write the measure in SQL" + ("" if self.dialect else " (or set the warehouse type in unshackle.toml if it isn't BigQuery)")
            )
        if mtype in MEASURE_AGG:
            if mtype == "count":
                expr = self.dim_expr(vname, pk)[0] if pk else "1"
                problems: list[str] = []
                if not pk:
                    self.b.warn(src, "count without a primary key; counting rows with expr 1", "info")
            else:
                expr, problems = self.resolve(vname, raw_sql or f"${{TABLE}}.{m['name']}")
                if problems:
                    # reads fields of joined views: an expression over dimensions of several datasets,
                    # checked against the explores' joins once they are known (_check_cross)
                    cross, cross_problems = self.cross_expr(vname, raw_sql)
                    if not cross_problems and self._other_views(vname, cross):
                        expr, problems = cross, []
            if problems:
                return todo("; ".join(problems))
            measure = Measure(name, MEASURE_AGG[mtype], expr, m.get("description", ""), metric.label)
            if self._other_views(vname, expr):
                measure.source_expression, measure.source_language = source_expr, "lookml"
                self.cross.append((vname, m, measure, metric))
            if mtype == "percentile":
                measure.agg_params = {"percentile": pct / 100, "use_discrete_percentile": False}
            if mtype in ("median", "percentile") and self.dialect == "bigquery":
                self.b.warn(src, "Looker computes this with APPROX_QUANTILES above 10,000 rows, so it can differ slightly", "info")
            if mtype == "sum":
                metric.fill_nulls_with = 0  # Looker writes COALESCE(SUM(x), 0)
            filters, bad = [], []
            for field, value in _filter_pairs(m):
                ref = self.field_ref(vname, field)
                # metrics: no relative dates (MetricFlow's type for a time grain is not checked on BigQuery)
                sql = looker_filter_sql(ref[0], value, ref[1], ref[2] or "date", self.dialect, relative=False) if ref else None
                if sql is None:
                    bad.append(f"{field}: {value!r}")
                else:
                    filters.append(sql)
            sm.measures.append(measure)
            metric.measure = name
            metric.filter = " AND ".join(filters)
            if bad:
                source_expr += f" filters: [{', '.join(bad)}]"
                return todo(f"Looker filter expression(s) not translated: {', '.join(bad)}; add them to the metric filter")
            self.b.metrics.append(metric)
            return
        if mtype == "number":
            refs = REF.findall(raw_sql)
            names, problems = {}, []
            for ref in refs:
                view, _, fname = ref.strip().rpartition(".")
                view = view or vname
                if (view, fname) in self.metric_names:
                    names[ref] = self.metric_names[(view, fname)]
                else:
                    problems.append(f"references non-measure {ref}")
            if problems or not refs:
                return todo("; ".join(problems) or "number measure with inline aggregate SQL; split into simple measures")
            expr = REF.sub(lambda mm: names[mm.group(1)], raw_sql).strip()
            if AGG_FN.search(expr):
                return todo("mixes aggregate SQL with measure references")
            ratio = re.fullmatch(
                r"(?:1(?:\.0*)?\s*\*\s*)?(\w+)\s*(?:\*\s*1(?:\.0*)?\s*)?/\s*(?:NULLIF\s*\(\s*(\w+)\s*,\s*0\s*\)|(\w+))(?:\s*\*\s*1(?:\.0*)?)?",
                expr, re.I,
            )
            if ratio:
                metric.type, metric.numerator, metric.denominator = "ratio", ratio.group(1), ratio.group(2) or ratio.group(3)
            else:
                metric.type, metric.expr = "derived", " ".join(expr.split())
                metric.metrics = list(dict.fromkeys(names.values()))
            self.b.metrics.append(metric)
            return
        if mtype == "running_total":
            return todo("running_total is computed over result rows in Looker; use a cumulative metric or a window in the tile SQL")
        return todo(f"measure type {mtype} has no direct MetricFlow agg")

    # ------------------------------------------------------------ models / explores

    def _model(self, mname: str, model: dict[str, Any]) -> None:
        src = model.get("file", f"{mname}.model.lkml")
        lines = [f"LookML model `{mname}`" + (f" (connection `{model['connection']}`)" if model.get("connection") else "") + ".", ""]
        table_explores: dict[str, list[str]] = {}
        for ex in model["explores"]:
            base = ex.get("from") or ex.get("view_name") or ex["name"]
            table_explores.setdefault(self.model_table(base), []).append(ex["name"])
            for j in ex.get("joins", []):
                table_explores.setdefault(self.model_table(j.get("from") or j["name"]), []).append(ex["name"])
        for ex in model["explores"]:
            base = ex.get("from") or ex.get("view_name") or ex["name"]
            alias = {base: base, ex["name"]: base}
            for j in ex.get("joins", []):
                alias[j["name"]] = j.get("from") or j["name"]
            lines.append(f"## Explore: {ex.get('label') or ex['name']}")
            if ex.get("description"):
                lines.append("")
                lines.append(ex["description"])
            lines += ["", f"- Base view: `{base}` (`{self.model_table(base)}`)"]
            for j in ex.get("joins", []):
                self._join(ex, base, j, alias)
                lines.append(f"- Joins `{j['name']}` ({j.get('relationship', 'many_to_one')}, {j.get('type', 'left_outer')}) on `{j.get('sql_on') or j.get('foreign_key', '')}`")
            for af in ex.get("access_filters", []):
                self._access_filter(src, ex, base, af, alias, table_explores)
            if ex.get("sql_always_where"):
                self._always_where(src, ex, base, alias)
                lines.append(f"- Always filtered: `{ex['sql_always_where']}`")
            if ex.get("always_filter"):
                lines.append(f"- Default filter users must fill: `{json.dumps(ex['always_filter'])}`")
            if ex.get("required_access_grants"):
                self._grant_permissions(ex["required_access_grants"], "Table", self.model_table(base), f"{src} explore: {ex['name']}")
            lines.append("")
        self.b.knowledge.append(
            KnowledgeItem(f"Looker model {mname}", "\n".join(lines), description=f"Explores and joins from the Looker model {mname}",
                          datasource=self.b.datasource, source_ref=src)
        )

    def _join_cols(self, sql_on: str, alias: dict[str, str]) -> list[tuple[str, str]] | None:
        """`${a.x} = ${b.y}` -> [(view_a, field_x), (view_b, field_y)]"""
        m = re.fullmatch(r"\s*\$\{(\w+)\.(\w+)\}\s*=\s*\$\{(\w+)\.(\w+)\}\s*", sql_on or "")
        if not m:
            return None
        return [(alias.get(m.group(1), m.group(1)), m.group(2)), (alias.get(m.group(3), m.group(3)), m.group(4))]

    def _join(self, ex: dict[str, Any], base: str, j: dict[str, Any], alias: dict[str, str]) -> None:
        target = j.get("from") or j["name"]
        sql_on = j.get("sql_on") or (f"${{{base}.{j['foreign_key']}}} = ${{{j['name']}.{(self._pk(target) or {}).get('name', 'id')}}}" if j.get("foreign_key") else "")
        cols = self._join_cols(sql_on, alias)
        if not sql_on and j.get("sql") is not None:
            self.b.warn(f"explore {ex['name']} join {j['name']}",
                        f"join uses `sql:` instead of sql_on ({j['sql'].strip()!r}), e.g. UNNEST of a nested field or a field-only view; "
                        "model it by hand (MetricFlow joins only through entities)")
            return
        if not sql_on:
            self.b.warn(f"explore {ex['name']} join {j['name']}",
                        f"join has no sql_on or foreign_key (type: {j.get('type', 'left_outer')}, so a cross join); add the entity by hand if it is needed")
            return
        if not cols:
            self.b.warn(f"explore {ex['name']} join {j['name']}", f"join is not a single equality ({sql_on!r}); add the entity by hand")
            return
        if j.get("from") and j["from"] != j["name"]:
            self.b.warn(f"explore {ex['name']} join {j['name']}", f"join aliases view {target}; MetricFlow uses one entity per model, so role-playing joins need a second entity by hand", "info")
        for (v1, f1), (v2, f2) in (cols, cols[::-1]):
            pk = self._pk(v2)
            if pk and pk["name"] == f2 and v1 != v2:
                sm = next((s for s in self.b.semantic_models if s.name == v1), None)
                f = self._field(v1, f1)
                if sm is None or f is None:
                    continue
                expr, problems = self.dim_expr(v1, f)
                if problems:
                    self.b.warn(f"explore {ex['name']} join {j['name']}", "; ".join(problems))
                    return
                if any(e.name == snake(v2) for e in sm.entities):
                    return
                etype = "unique" if j.get("relationship") == "one_to_one" else "foreign"
                sm.entities.append(Entity(snake(v2), etype, expr))
                return
        self.b.warn(f"explore {ex['name']} join {j['name']}", "neither side of the join is a primary key; add entities by hand")

    def _access_filter(self, src, ex, base, af, alias, table_explores) -> None:
        field = af.get("field", "")
        view, _, fname = field.rpartition(".")
        view = alias.get(view, view)
        f = self._field(view, fname)
        attr = af.get("user_attribute", "")
        source = f"explore: {ex['name']} access_filter: {{field: {field} user_attribute: {attr}}}"
        name = f"{ex['name']}_{snake(field)}"
        if f is None:
            self.b.row_filters.append(RowFilter(name, self.model_table(view), source_expression=source, source_ref=src,
                                                todo=f"field {field} not found in LookML; write the predicate by hand"))
            return
        expr, problems = self.dim_expr(view, f)
        table = self.model_table(view)
        others = list(dict.fromkeys(e for e in table_explores.get(table, []) if e != ex["name"]))
        todo = "; ".join(problems)
        if others:
            todo = (todo + "; " if todo else "") + (
                f"Looker applied this only inside explore {ex['name']}; explore(s) {', '.join(others)} expose {table} "
                "without it. unshackle row filters apply to every query on the table, so confirm that is intended"
            )
        rf = RowFilter(name, table, f"{expr} = {{{{ user.{attr} }}}}", source_expression=source, source_ref=src, todo=todo)
        self.b.row_filters.append(rf)
        if view == base:
            return
        path = next((j for j in ex.get("joins", []) if (j.get("from") or j["name"]) == view), None)
        cols = self._join_cols((path or {}).get("sql_on", ""), alias) if path else None
        if cols and base in (cols[0][0], cols[1][0]):
            (bv, bf), (jv, jf) = cols if cols[0][0] == base else cols[::-1]
            bexpr, p1 = self.dim_expr(bv, self._field(bv, bf) or {"name": bf})
            jexpr, p2 = self.dim_expr(jv, self._field(jv, jf) or {"name": jf})
            if not (p1 or p2):
                self.b.row_filters.append(
                    RowFilter(f"{name}_via_{snake(base)}", self.model_table(base),
                              f"{bexpr} IN (SELECT {jexpr} FROM {table} WHERE {expr} = {{{{ user.{attr} }}}})",
                              source_expression=source + f" (through join {path['name']})", source_ref=src)
                )
                return
        self.b.row_filters.append(
            RowFilter(f"{name}_via_{snake(base)}", self.model_table(base), source_expression=source, source_ref=src,
                      todo=f"Looker filtered explore {ex['name']} (base {self.model_table(base)}) through joins to {view}; write a predicate on the base table")
        )

    def _always_where(self, src, ex, base, alias) -> None:
        raw = ex["sql_always_where"]
        sql = USER_ATTR.sub(lambda m: f"\x01{m.group(1)}\x02", raw)

        def unalias(m: re.Match) -> str:
            view, dot, name = m.group(1).partition(".")
            return "${" + (alias.get(view, view) + dot + name if dot else view) + "}"

        # refs to the base view resolve; refs to joined views stay TODO (row filters apply to one table)
        expr, problems = self.resolve(base, REF.sub(unalias, sql))
        expr = expr.replace("\x01", "{{ user.").replace("\x02", " }}")
        self.b.row_filters.append(
            RowFilter(f"{ex['name']}_always_where", self.model_table(base), "" if problems else expr, ["*"],
                      source_expression=f"explore: {ex['name']} sql_always_where: {raw}", source_ref=src,
                      todo="; ".join(problems))
        )

    def _grant_permissions(self, grant_names: list[str], rtype: str, rid: str, src: str, extra: str = "") -> None:
        for gname in grant_names:
            g = self.grants.get(gname)
            if not g:
                self.b.warn(src, f"access_grant {gname} not found")
                continue
            attr, values = g.get("user_attribute", ""), g.get("allowed_values") or []
            expr = f"access_grant: {gname} {{user_attribute: {attr} allowed_values: {json.dumps(values)}}}"
            note = (
                f"Looker allowed users whose `{attr}` user attribute is one of {values}; one Cedar group per value was assumed. "
                f"Map each group to your IdP (or sync the attribute), and add a forbid for everyone else if a broader permit exists"
            )
            if len(grant_names) > 1:
                note += f". Looker required ALL of {grant_names}"
            note += extra
            for value in values:
                self.b.permissions.append(
                    Permission(f"group:{value}", "query" if rtype != "Notebook" else "view", rtype, rid,
                               source_expression=expr, source_ref=src, todo=note)  # type: ignore[arg-type]
                )

    # ------------------------------------------------------------ dashboards

    def _classify(self, field: str, explore_alias: dict[str, str]) -> tuple[str, str]:
        view, _, name = field.rpartition(".")
        kind = self.fields.get(f"{explore_alias.get(view, view)}.{name}")
        return (kind[0], kind[2]) if kind else ("unknown", "")

    def _tile(self, el: dict[str, Any], dash_filters: dict[str, DashboardFilter]) -> Tile:
        eid = str(el.get("id"))
        etype = el.get("type", "vis")
        if etype == "text" or (etype != "vis" and (el.get("body_text") or el.get("title_text"))):
            return _text_tile(eid, el)
        q = _element_query(el)
        vis = q.get("vis_config") or el.get("result_maker", {}).get("vis_config") or {}
        vtype = vis.get("type") or q.get("vis_config", {}).get("type") or "unknown"
        kind, mark = VIS_MARK.get(vtype, ("unknown", "unknown"))
        todos: list[str] = []
        if (kind, mark) == ("unknown", "unknown"):
            todos.append(f"Looker visualization {vtype!r} (map or custom viz) has no Altair equivalent here; pick a chart")
        qid = _element_query_id(el)
        sql = self.store.get_text(f"queries/{qid}.sql") if qid and self.store.exists(f"queries/{qid}.sql") else ""
        fields = list(q.get("fields") or [])
        pivots = list(q.get("pivots") or [])
        hidden = set(vis.get("hidden_fields") or [])
        for f in fields + pivots:
            sql = sql.replace(f'"{f}"', snake(f)).replace(f"`{f}`", snake(f))
        if not sql:
            todos.append("no generated SQL saved for this element" + (" (merged results)" if el.get("merge_result_id") else ""))
        alias = self._explore_alias(q.get("model", ""), q.get("view", ""))
        todos += _dynamic_field_todos(q.get("dynamic_fields"))
        enc, more = self._encodings(kind, mark, fields, pivots, hidden, alias)
        todos += more
        listen = [
            (lf.get("dashboard_filter_name"), lf.get("field"))
            for fl in (el.get("result_maker") or {}).get("filterables") or []
            for lf in fl.get("listen") or []
        ]
        applied = [(n, f) for n, f in listen if n in dash_filters]
        if applied:
            todos.append(
                "dashboard filter(s) "
                + ", ".join(f"{n} on {f} (:{dash_filters[n].name})" for n, f in applied)
                + " are not in the generated SQL; add WHERE clauses with those parameters"
            )
        title = el.get("title") or vis.get("title") or ", ".join(fields[:3]) or eid
        return Tile(
            eid, title, kind, mark, sql.strip(), enc, source_type=vtype,
            source_config={"query": {k: q.get(k) for k in ("model", "view", "fields", "pivots", "filters", "sorts", "limit") if q.get(k)}, "vis_config": vis},
            todo="; ".join(todos),
        )

    def _encodings(self, kind: str, mark: str, fields: list[str], pivots: list[str], hidden: set[str],
                   alias: dict[str, str]) -> tuple[dict[str, Encoding], list[str]]:
        """Chart channels: first dimension -> x (temporal for timeframes), first measure -> y, first pivot -> color."""
        dims = [f for f in fields if self._classify(f, alias)[0] in ("dimension", "time", "unknown") and f not in pivots and f not in hidden]
        measures = [f for f in fields if self._classify(f, alias)[0] == "measure" and f not in hidden]
        enc: dict[str, Encoding] = {}
        todos: list[str] = []

        def e(f: str, q_type: str | None = None) -> Encoding:
            k, _ = self._classify(f, alias)
            t = q_type or ("temporal" if k == "time" else "quantitative" if k == "measure" else "nominal")
            return Encoding(snake(f), t)  # type: ignore[arg-type]

        if kind == "kpi":
            if measures:
                enc["text"] = e(measures[0])
        elif mark == "arc":
            if measures:
                enc["theta"] = e(measures[0])
            if dims:
                enc["color"] = e(dims[0], "nominal")
        elif kind == "chart":
            if dims:
                enc["x"] = e(dims[0])
            if measures:
                enc["y"] = e(measures[0])
            if pivots:
                enc["color"] = e(pivots[0], "nominal")
            elif len(dims) > 1:
                enc["color"] = e(dims[1], "nominal")
            if len(measures) > 1:
                todos.append(f"Looker plotted {len(measures)} measures ({', '.join(measures)}); only the first is encoded, fold the rest or add tiles")
        return enc, todos

    def _explore_alias(self, model: str, explore: str) -> dict[str, str]:
        for ex in (self.models.get(model) or {}).get("explores", []):
            if ex["name"] == explore:
                return _aliases(ex)
        return {}

    def _dashboard(self, rel: str, d: dict[str, Any], usage: dict) -> None:
        did = str(d.get("id") or Path(rel).stem)
        filters: dict[str, DashboardFilter] = {}
        for f in d.get("dashboard_filters") or []:
            dim = f.get("dimension") or ""
            k, _ = self._classify(dim, self._explore_alias(f.get("model", ""), f.get("explore", ""))) if dim else ("", "")
            ftype = f.get("type", "")
            kind = "date_range" if ftype == "date_filter" or k == "time" else "number" if ftype == "number_filter" else "value"
            filters[f["name"]] = DashboardFilter(snake(f.get("name") or f.get("title") or dim), snake(dim.replace(".", "_")), f.get("default_value") or None, kind)  # type: ignore[arg-type]
        tiles = [self._tile(el, filters) for el in d.get("dashboard_elements") or []]
        layout: list[list[str]] = []
        layouts = d.get("dashboard_layouts") or []
        active = next((lay for lay in layouts if lay.get("active")), layouts[0] if layouts else None)
        if active:
            rows: dict[int, list[tuple[int, str]]] = {}
            for c in active.get("dashboard_layout_components") or []:
                if c.get("deleted"):
                    continue
                rows.setdefault(int(c.get("row") or 0), []).append((int(c.get("column") or 0), str(c.get("dashboard_element_id"))))
            known = {t.id for t in tiles}
            layout = [[eid for _, eid in sorted(r) if eid in known] for _, r in sorted(rows.items())]
            layout = [r for r in layout if r]
            placed = {i for r in layout for i in r}
            layout += [[t.id] for t in tiles if t.id not in placed]
        base = self.meta.get("base_url", "")
        u = usage.get(("dashboard", did), {})
        self.b.dashboards.append(
            Dashboard(
                name=d.get("title") or did,
                slug=slugify(d.get("title") or did),
                description=d.get("description") or "",
                tiles=tiles,
                layout=layout,
                filters=list(filters.values()),
                datasource=self.b.datasource,
                source_ref=f"looker dashboard {did}",
                url=f"{base}/dashboards/{did}" if base else "",
                views=u.get("views"),
            )
        )

    # ------------------------------------------------------------ LookML dashboards

    def _lookml_dashboards(self) -> None:
        """`*.dashboard.lookml` (YAML): elements -> tiles with SQL written from the explore, filters -> parameters."""
        import yaml

        api = set()
        for rel in self.store.glob("dashboards/*.json"):
            did = str(self.store.get_json(rel).get("id") or "")
            if "::" in did:
                api.add(did.split("::", 1)[1])
        found: dict[str, tuple[str, dict[str, Any]]] = {}
        for rel in self.store.glob("lookml/**/*.lookml"):
            try:
                docs = yaml.safe_load(self.store.get_text(rel))
            except Exception as exc:  # noqa: BLE001
                self.b.warn(rel, f"LookML dashboard did not parse as YAML: {exc}")
                continue
            for d in docs if isinstance(docs, list) else [docs]:
                if isinstance(d, dict) and d.get("dashboard"):
                    found[str(d["dashboard"])] = (rel, d)
        for name, (rel, raw) in found.items():
            if raw.get("extension") == "required":
                continue
            if name in api:
                self.b.warn(rel, f"LookML dashboard {name} was also exported through the API; the API copy is used", "info")
                continue
            try:
                self._lookml_dashboard(rel, name, self._extend_dashboard(raw, found, {name}, rel))
            except Exception as exc:  # noqa: BLE001 - one bad dashboard must not stop the rest
                self.b.warn(rel, f"LookML dashboard {name} skipped: {exc}")

    def _extend_dashboard(self, d: dict[str, Any], found: dict[str, tuple[str, dict[str, Any]]], seen: set[str], rel: str) -> dict[str, Any]:
        parents = d.get("extends") or []
        out: dict[str, Any] = {}
        for p in [parents] if isinstance(parents, str) else parents:
            if p in seen or p not in found:
                self.b.warn(rel, f"dashboard {d['dashboard']} extends {p}, which is not in the LookML; its elements and filters are missing")
                continue
            out = _merge_view(out, self._extend_dashboard(found[p][1], found, seen | {p}, rel), named=DASHBOARD_LISTS)
        return _merge_view(out, d, named=DASHBOARD_LISTS)

    def _lookml_dashboard(self, rel: str, name: str, d: dict[str, Any]) -> None:
        fdefs = {str(f["name"]): f for f in d.get("filters") or [] if isinstance(f, dict) and f.get("name")}
        filters: dict[str, DashboardFilter] = {}
        binds: dict[str, tuple[bool, Any, str]] = {}  # filter -> (bindable, value, kind of value)
        for fname, f in fdefs.items():
            field = str(f.get("field") or f.get("dimension") or "")
            fkind = self._filter_field_kind(f, field)
            ok, value = _bind_value(f.get("default_value"), fkind)
            binds[fname] = (ok, value, fkind)
            kind = "date_range" if fkind == "time" else "number" if fkind == "number" else "value"
            default = value if ok else (f.get("default_value") or None)
            filters[fname] = DashboardFilter(snake(fname), snake(field.replace(".", "_")), default, kind)  # type: ignore[arg-type]
        elements = [el for el in d.get("elements") or [] if isinstance(el, dict)]
        tiles = [self._lookml_tile(el, d, fdefs, filters, binds) for el in elements]
        ids = [t.id for t in tiles]
        for i, t in enumerate(tiles):
            t.id = f"{t.id}_{i + 1}" if ids.count(t.id) > 1 else t.id
        layout: list[list[str]] = []
        if d.get("layout") == "grid" and d.get("rows"):
            by_name = {str(el.get("name")): t.id for el, t in zip(elements, tiles)}
            layout = [[by_name[str(e)] for e in r.get("elements") or [] if str(e) in by_name] for r in d["rows"] if isinstance(r, dict)]
        elif elements and all("row" in el for el in elements):
            rows: dict[int, list[tuple[int, str]]] = {}
            for el, t in zip(elements, tiles):
                rows.setdefault(int(el.get("row") or 0), []).append((int(el.get("col") or 0), t.id))
            layout = [[i for _, i in sorted(r)] for _, r in sorted(rows.items())]
        placed = {i for r in layout for i in r}
        layout = [r for r in layout if r] + [[t.id] for t in tiles if t.id not in placed]
        title = str(d.get("title") or name)
        self.b.dashboards.append(
            Dashboard(name=title, slug=slugify(title), description=str(d.get("description") or ""), tiles=tiles, layout=layout,
                      filters=list(filters.values()), datasource=self.b.datasource, source_ref=f"{rel} dashboard: {name}")
        )

    def _find_explore(self, model: str, explore: str) -> dict[str, Any] | None:
        pools = [self.models[model]] if model in self.models else list(self.models.values())
        found = [ex for m in pools for ex in m["explores"] if ex["name"] == explore]
        return found[0] if len(found) == 1 or (found and model in self.models) else None

    def _filter_field_kind(self, f: dict[str, Any], field: str) -> str:
        """time / number / yesno / string for a dashboard filter, from its type and its field."""
        if f.get("type") == "date_filter":
            return "time"
        if f.get("type") == "number_filter":
            return "number"
        ex = self._find_explore(str(f.get("model") or ""), str(f.get("explore") or ""))
        if ex and field:
            q = _Query(self, ex)
            _, kind, problems = q.field(field)
            if not problems:
                return kind
        return "string" if f.get("type") == "string_filter" else "unknown"

    def _lookml_tile(self, el: dict[str, Any], d: dict[str, Any], fdefs: dict[str, Any], filters: dict[str, DashboardFilter],
                     binds: dict[str, tuple[bool, Any, str]]) -> Tile:
        eid = str(el.get("name") or el.get("title") or "element")
        etype = str(el.get("type") or "")
        if etype == "text" or (not el.get("fields") and (el.get("body_text") or el.get("title_text"))):
            return _text_tile(eid, el)
        kind, mark = VIS_MARK.get(etype, ("unknown", "unknown"))
        query = {k: el[k] for k in ("model", "explore", "fields", "pivots", "filters", "sorts", "limit", "listen") if el.get(k)}
        vis = {k: v for k, v in el.items() if k not in query and k not in ("name", "title", "row", "col", "width", "height")}
        problems: list[str] = []
        notes: list[str] = []
        if (kind, mark) == ("unknown", "unknown") and etype:
            notes.append(f"Looker visualization {etype!r} has no Altair equivalent here; pick a chart")
        fields = [str(f) for f in el.get("fields") or []]
        pivots = [str(f) for f in el.get("pivots") or []]
        model, explore = str(el.get("model") or ""), str(el.get("explore") or "")
        ex = self._find_explore(model, explore) if explore else None
        sql = ""
        if el.get("merged_queries"):
            problems.append("merged results (merged_queries): write the joined queries as one SQL statement")
        elif not fields:
            problems.append(f"element of type {etype or 'unknown'} has no query")
        elif ex is None:
            problems.append(f"explore {model}/{explore} is not in the LookML")
        else:
            sql, p, n = self._element_sql(ex, el, d, fields, pivots, fdefs, filters, binds)
            problems += p
            notes += n
        notes += _dynamic_field_todos(el.get("dynamic_fields"))
        enc, more = self._encodings(kind, mark, fields, pivots, set(el.get("hidden_fields") or []), self._explore_alias(model, explore))
        notes += more
        todo = list(dict.fromkeys(problems + notes))
        if problems:
            todo.insert(0, "no SQL written, so nothing shows a wrong number")
            sql = ""
        return Tile(eid, str(el.get("title") or eid), kind, mark, sql, enc, source_type=etype,
                    source_config={"query": query, "vis_config": vis}, todo="; ".join(todo))

    def _element_sql(self, ex: dict[str, Any], el: dict[str, Any], d: dict[str, Any], fields: list[str], pivots: list[str],
                     fdefs: dict[str, Any], filters: dict[str, DashboardFilter],
                     binds: dict[str, tuple[bool, Any, str]]) -> tuple[str, list[str], list[str]]:
        """The element's query as SQL over the explore: (sql, problems, notes)."""
        q = _Query(self, ex)
        # parameters and templated filters first: Liquid in the fields reads them
        tile_filters = el.get("filters") or {}
        templated = set()
        key = lambda f: q.key(str(f), q.view_of(ex["name"]))  # noqa: E731
        q.selected = {key(f) for f in fields}
        q.sorted = {key(str(s).split()[0]) for s in el.get("sorts") or [] if str(s).split()} | {key(pv) for pv in pivots}
        q.filtered = {key(f) for f, v in (tile_filters.items() if isinstance(tile_filters, dict) else []) if str(v) != ""}
        q.filtered |= {key(f) for n, f in (el.get("listen") or {}).items() if str(n) in fdefs and fdefs[str(n)].get("default_value") not in (None, "")}
        filter_sql: list[tuple[str, str, str]] = []  # filter fields with their own sql: (view, alias, sql)

        def kind_of(field: Any) -> tuple[str | None, str, str]:
            qual, _, name = str(field).rpartition(".")
            view = q.view_of(qual or ex["name"])
            return view, name, self.fields.get(f"{view}.{name}", ("",))[0]

        for field, value in (tile_filters.items() if isinstance(tile_filters, dict) else []):
            view, name, kind = kind_of(field)
            if kind in ("parameter", "filter"):
                templated.add(str(field))
                fsql = (self._field(view or "", name, ("filters",)) or {}).get("sql")
                if kind == "filter" and fsql and str(value) != "":
                    filter_sql.append((view, q.alias(str(field).rpartition(".")[0] or ex["name"]), fsql))
                q.liquid[f"{view}.{name}"] = str(value)
        for fname, field in (el.get("listen") or {}).items():
            view, name, kind = kind_of(field)
            if kind in ("parameter", "filter") and str(fname) in fdefs:
                templated.add(str(field))
                default = fdefs[str(fname)].get("default_value")
                if default not in (None, ""):
                    q.liquid.setdefault(f"{view}.{name}", str(default))
                q.notes.append(f"dashboard filter {fname!r} sets {field}, which Liquid reads: the SQL is written for its default "
                               f"({default!r}); changing the filter in the notebook does not change it")
        dims: list[tuple[str, str]] = []
        meas: list[tuple[str, str]] = []
        kinds: dict[str, str] = {}
        for f in fields:
            sql, kind, p = q.field(f)
            q.problems += p
            kinds[f] = kind
            if not sql:
                continue
            if kind == "measure":
                meas.append((sql, snake(f)))
            else:
                dims.append((f"CASE WHEN {sql} THEN 'Yes' ELSE 'No' END" if kind == "yesno" else sql, snake(f)))
        q.problems += [f"pivot {pv} is not one of the fields" for pv in pivots if pv not in fields]
        where: list[str] = []
        having: list[str] = []

        def add(field: str, value: Any, origin: str) -> None:
            if value is None or str(value) == "":
                return
            sql, kind, p = q.field(field)
            q.problems.extend(p)
            if not sql:
                return
            datatype = "timestamp"
            if kind == "time":  # Looker filters the time itself, whatever the timeframe
                sql, datatype = q.raw_time(field) or (sql, "date")
            cond = looker_filter_sql(_paren(sql), str(value), "number" if kind == "measure" else kind, datatype, self.dialect)
            if cond is None:
                q.problems.append(f"{origin} filter {field}: {str(value)!r} is not translated (a Looker filter expression)")
            else:
                (having if kind == "measure" else where).append(cond)

        for field, value in (tile_filters.items() if isinstance(tile_filters, dict) else []):
            if str(field) not in templated:
                add(str(field), value, "tile")
        listen = {k: v for k, v in (el.get("listen") or {}).items() if str(v) not in templated}
        filtered = set(map(str, tile_filters)) | set(map(str, listen.values()))
        qualify = lambda f: f if "." in f else f"{ex['name']}.{f}"  # noqa: E731
        for field, value in _filter_pairs(ex.get("always_filter") or {}):
            if qualify(field) not in filtered:
                add(qualify(field), value, "explore always_filter")
        cf = ex.get("conditionally_filter") or {}
        pairs = _filter_pairs(cf)
        if pairs and not filtered & ({qualify(f) for f, _ in pairs} | {qualify(str(u)) for u in cf.get("unless") or []}):
            for field, value in pairs:
                add(qualify(field), value, "explore conditionally_filter")
        if ex.get("sql_always_where"):
            expr, p = self._expand(q.base_view, ex["sql_always_where"], "query", frozenset(), q, q.base)
            q.problems += [f"sql_always_where: {x}" for x in p]
            where.append(f"({expr})")
        for view, alias, fsql in filter_sql:  # Looker adds a filter field's own sql to the WHERE clause
            q.problems += q.use(alias)
            expr, p = self._expand(view, fsql, "query", frozenset(), q, alias)
            q.problems += [f"filter field sql: {x}" for x in p]
            where.append(f"({expr})")
        if ex.get("sql_always_having"):
            q.in_measure += 1
            expr, p = self._expand(q.base_view, ex["sql_always_having"], "query", frozenset(), q, q.base)
            q.in_measure -= 1
            q.problems += [f"sql_always_having: {x}" for x in p]
            having.append(f"({expr})")
        if ex.get("access_filters"):
            q.notes.append(f"explore {ex['name']} filters rows per user (access_filter on "
                           + ", ".join(str(a.get("field")) for a in ex["access_filters"])
                           + "); this SQL reads every row unless the warehouse applies the row filters in policies/")
        for fname, field in listen.items():
            fname, field = str(fname), str(field)
            if fname not in fdefs:
                q.notes.append(f"listens to dashboard filter {fname!r}, which the dashboard does not define")
                continue
            ok, value, fkind = binds[fname]
            snap = q.snapshot()
            sql, kind, p = q.field(field)
            risky = q.risky_joins(snap[0])
            param = filters[fname].name
            default = fdefs[fname].get("default_value")
            fits = ((fkind,) if fkind in ("string", "number", "yesno") else ()) if value is not None else ("string", "dimension", "number", "yesno", "measure")
            if p or not sql or risky:
                q.restore(snap)  # Looker joins for a filter only when it has a value
            if p or not sql:
                q.notes.append(f"dashboard filter {fname!r} on {field} is not applied: {'; '.join(p) or 'field not found'}")
            elif risky:
                q.notes.append(f"dashboard filter {fname!r} on {field} is not applied: it needs join(s) "
                               + ", ".join(f"{a} ({q.joins[a].get('type', 'left_outer')}, {q.joins[a].get('relationship', 'many_to_one')})" for a in risky)
                               + f", which change the rows even when the filter is empty; add the join and a WHERE clause on :{param} by hand")
            elif ok and kind in fits:
                cond = f"(:{param} IS NULL OR {_paren(sql)} = :{param})"
                (having if kind == "measure" else where).append(cond)
            else:
                q.restore(snap)
                what = "a date filter" if kind == "time" or fkind == "time" else f"default {default!r} is a Looker filter expression"
                q.notes.append(f"dashboard filter {fname!r} on {field} is not applied ({what}); add a WHERE clause on :{param}")
        order: list[str] = []
        for s in el.get("sorts") or []:
            parts = str(s).split()
            if not parts:
                continue
            if parts[0] not in fields:
                # Looker adds a sorted measure to the query and orders by it; it drops a sorted dimension
                snap = q.snapshot()
                sql, kind, p = q.field(parts[0])
                if kind != "measure" or p or not sql:
                    q.restore(snap)
                    if kind == "measure":
                        q.notes.append(f"sort on {parts[0]}, a measure the tile doesn't show, is not applied: {'; '.join(p) or 'not found'}")
                    continue
                meas.append((sql, snake(parts[0])))
                fields = [*fields, parts[0]]
            desc = len(parts) > 1 and parts[1].lower() == "desc"
            if len(parts) > 2:
                q.notes.append(f"sort {s!r} orders by one pivoted column; the SQL sorts by {parts[0]}{' desc' if desc else ''}")
            order.append(snake(parts[0]) + (" DESC" if desc else ""))
        if not el.get("sorts"):  # Looker's default: newest time first, else largest measure, else first dimension
            first = next((f for f in fields if kinds.get(f) == "time"), None)
            if first:
                order = [f"{snake(first)} DESC"]
            elif meas:
                order = [f"{meas[0][1]} DESC"]
            elif dims:
                order = [dims[0][1]]
        limit = str(el.get("limit") or "")
        if pivots and limit:
            q.notes.append(f"Looker's limit {limit} counts rows after pivoting on {', '.join(pivots)}; the long-format SQL has no LIMIT")
            limit = ""
        tz = el.get("query_timezone") or d.get("query_timezone")
        if tz and str(tz) not in ("UTC", "Etc/UTC") and q.tz:
            q.notes.append(f"Looker converts {', '.join(sorted(q.tz))} to the query time zone ({tz}) when the connection has one; "
                           "the SQL uses the stored time zone")
        q.problems += q.fanout()
        if pivots and q.windows:
            q.problems.append("percent_of_total / running_total with pivots: Looker computes them per pivoted column")
        sql = q.sql(dims + meas, where, having, len(dims), order, limit)
        return sql, list(dict.fromkeys(q.problems)), list(dict.fromkeys(q.notes))

    def _look(self, rel: str, lk: dict[str, Any]) -> None:
        lid = str(lk.get("id") or Path(rel).stem)
        qid = str(lk.get("query_id") or (lk.get("query") or {}).get("id") or "")
        sql = self.store.get_text(f"queries/{qid}.sql").strip() if qid and self.store.exists(f"queries/{qid}.sql") else ""
        q = lk.get("query") or {}
        for f in list(q.get("fields") or []) + list(q.get("pivots") or []):
            sql = sql.replace(f'"{f}"', snake(f)).replace(f"`{f}`", snake(f))
        dyn = q.get("dynamic_fields")
        todo = "" if sql else "no generated SQL saved for this Look"
        if dyn and dyn not in ("[]", []):
            todo = (todo + "; " if todo else "") + "Look has table calculations or custom fields (see source dynamic_fields); add them to the SQL"
        self.b.queries.append(
            VerifiedQuery(
                name=lk.get("title") or f"look {lid}",
                sql=sql or "-- no SQL",
                description=lk.get("description") or "",
                datasource=self.b.datasource,
                owner=str((lk.get("user") or {}).get("display_name") or ""),
                source_ref=f"looker look {lid}",
                todo=todo,
            )
        )


def _type_kind(t: str, other: str) -> str:
    """A dimension's LookML type as a filter kind; `other` for the rest."""
    if t in ("yesno", "number"):
        return t
    if t.startswith("date") or t == "time":
        return "time"
    return "string" if t in ("string", "zipcode") else other


def _base_view(ex: dict[str, Any]) -> str:
    return ex.get("from") or ex.get("view_name") or ex["name"]


def _aliases(ex: dict[str, Any]) -> dict[str, str]:
    """Explore alias -> view: the explore's name for its base view, then each join."""
    return {ex["name"]: _base_view(ex)} | {j["name"]: j.get("from") or j["name"] for j in ex.get("joins", [])}


def _bind_value(value: Any, kind: str) -> tuple[bool, Any]:
    """A dashboard filter default as a parameter value, when it is one plain value (None: no filter)."""
    if value is None or str(value).strip() == "":
        return kind != "time", None
    v = str(value).strip()
    if kind == "yesno":
        return (v.lower() in ("yes", "no")), v.lower() == "yes"
    if kind == "number":
        if re.fullmatch(r"-?\d+", v):
            return True, int(v)
        if re.fullmatch(r"-?\d+\.\d+", v):
            return True, float(v)
        return False, None
    if kind in ("string", "unknown") and not re.search(r"[,%_^]", v) and not v.startswith("-") and v.upper() not in ("NULL", "EMPTY", "NOT NULL"):
        return True, v
    return False, None


def _dynamic_field_todos(dyn: Any) -> list[str]:
    """Table calculations and custom fields (`dynamic_fields`) are Looker expressions over the result."""
    if isinstance(dyn, str) and dyn.strip():
        try:
            dyn = json.loads(dyn)
        except ValueError:
            dyn = []
    out = []
    for d in dyn if isinstance(dyn, list) else []:
        if d.get("table_calculation"):
            out.append(f"table calculation {d['table_calculation']} = {d.get('expression', '')} (Looker expression); add it to the SQL as a window/column")
        elif d.get("measure") or d.get("dimension"):
            out.append(f"custom field {d.get('measure') or d.get('dimension')}: {d.get('expression') or d.get('based_on', '')} ({d.get('type', '')}); add it to the SQL")
    return out


def _null_propagating(expr: str, vname: str, other: str) -> bool:
    """Arithmetic over fields of both views: NULL when either side has no match, so an inner or full
    join and a left join aggregate the same rows (SUM, AVG, MIN, MAX and COUNT(DISTINCT) skip NULL)."""
    refs = {v for v, _, _ in FILTER_REF.findall(expr)}
    rest = FILTER_REF.sub("", expr)
    return {snake(vname), snake(other)} <= refs and bool(re.fullmatch(r"[\s\d.+\-*/()]*", rest))


def _text_tile(eid: str, el: dict[str, Any]) -> Tile:
    """A text element: the notebook writes the title as the heading, so the text holds only subtitle and body."""
    parts = [_html_to_markdown(str(el.get(k) or "")) for k in ("subtitle_text", "body_text")]
    title = " ".join(_html_to_markdown(str(el.get("title_text") or ""), plain=True).split())
    return Tile(eid, title, "text", "text", text="\n\n".join(p for p in parts if p), source_type="text")


HTML_TAG = re.compile(r"</?[a-zA-Z][^>]*>")


def _html_to_markdown(text: str, plain: bool = False) -> str:
    """Simple HTML in text tiles (b, i, br, p, links) as Markdown; other tags are dropped, keeping their text."""
    if not plain:
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
        text = re.sub(r"</p\s*>", "\n\n", text, flags=re.I)
        text = re.sub(r"<(b|strong)\b[^>]*>(.*?)</\1\s*>", lambda m: f"**{m.group(2).strip()}**", text, flags=re.I | re.S)
        text = re.sub(r"<(i|em)\b[^>]*>(.*?)</\1\s*>", lambda m: f"*{m.group(2).strip()}*", text, flags=re.I | re.S)
        text = re.sub(r"""<a\b[^>]*href=["']([^"']*)["'][^>]*>(.*?)</a\s*>""", r"[\2](\1)", text, flags=re.I | re.S)
    text = html.unescape(HTML_TAG.sub("", text))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(line.rstrip() for line in text.splitlines())).strip()


def _join_on_text(j: dict[str, Any]) -> str:
    if j.get("sql_on"):
        return " ".join(str(j["sql_on"]).split())
    if j.get("foreign_key"):
        return f"foreign_key: {j['foreign_key']}"
    if j.get("sql") is not None:
        return "sql: " + " ".join(str(j["sql"]).split())
    return "no condition"


def _reachable(start: str, models: dict[str, SemanticModel]) -> set[str]:
    """Models reachable from `start` through foreign/unique entities (x_to_one joins), as Ossie relates datasets."""
    primary = {e.name: k for k, sm in models.items() for e in sm.entities if e.type == "primary"}
    unique = {e.name: k for k, sm in models.items() for e in sm.entities if e.type == "unique"}
    seen, todo = {start}, [start]
    while todo:
        sm = models.get(todo.pop())
        for e in sm.entities if sm else []:
            if e.type not in ("foreign", "unique"):
                continue
            to = primary.get(e.name) or (unique.get(e.name) if e.type == "foreign" else None)
            if to and to not in seen:
                seen.add(to)
                todo.append(to)
    return seen


# ==================================================================== explore queries


JOIN_TYPE = {"left_outer": "LEFT JOIN", "inner": "INNER JOIN", "full_outer": "FULL OUTER JOIN", "cross": "CROSS JOIN"}
# Symmetric aggregates, as Looker writes them: SUM(DISTINCT hash(key) + x) - SUM(DISTINCT hash(key)) in
# exact decimals counts each key's x once, however often a join repeats its row. hash: the first 15 hex
# digits of the key's MD5 as an integer.
SYM_HASH = {
    "bigquery": "CAST(CONCAT('0x', SUBSTR(TO_HEX(MD5(CAST({} AS STRING))), 1, 15)) AS INT64)",
    "snowflake": "TO_NUMBER(SUBSTR(MD5(CAST({} AS VARCHAR)), 1, 15), 'XXXXXXXXXXXXXXX')",
    "postgres": "CAST(CAST('x' || SUBSTR(MD5(CAST({} AS TEXT)), 1, 15) AS BIT(60)) AS BIGINT)",
    "redshift": "STRTOL(SUBSTRING(MD5(CAST({} AS VARCHAR)), 1, 15), 16)",
    "databricks": "CAST(CONV(SUBSTRING(MD5(CAST({} AS STRING)), 1, 15), 16, 10) AS BIGINT)",
    "duckdb": "CAST('0x' || SUBSTR(MD5(CAST({} AS VARCHAR)), 1, 15) AS BIGINT)",
}
FLOAT = {"bigquery": "FLOAT64", "postgres": "DOUBLE PRECISION", "redshift": "DOUBLE PRECISION"}


def symmetric_sql(agg: str, x: str, key: str, dialect: str) -> str | None:
    """Looker's symmetric sum / average of `x` counted once per `key`; None for an unknown warehouse."""
    if dialect not in SYM_HASH:
        return None
    dec = "NUMERIC" if dialect == "bigquery" else "DECIMAL(38,9)"  # BigQuery: NUMERIC is (38, 9), and CAST takes no parameters
    h, fl = f"CAST({SYM_HASH[dialect].format(key)} AS {dec})", FLOAT.get(dialect, "DOUBLE")
    # rounded as a decimal (Postgres has no ROUND(double precision, int)), to 6 places as Looker does
    total = (f"CAST(ROUND(COALESCE(SUM(DISTINCT CAST(COALESCE({x}, 0) AS {dec}) + {h}) - SUM(DISTINCT {h}), 0), 6)"
             f" AS {fl})")
    if agg == "sum":
        return total
    return f"{total} / NULLIF(CAST(COUNT(DISTINCT CASE WHEN {x} IS NOT NULL THEN {key} END) AS {fl}), 0)"


QUERY_AGG = {"sum": "COALESCE(SUM({}), 0)", "average": "AVG({})", "min": "MIN({})", "max": "MAX({})", "count_distinct": "COUNT(DISTINCT {})"}


class _Query:
    """One explore query written as SQL the way Looker writes it: the base view aliased as the explore,
    only the joins the fields need (and what those joins need), dimensions grouped, measures aggregated.

    `problems` make the SQL wrong (it is not written); `notes` leave it right but incomplete
    (a dashboard filter that isn't bound, a time zone). Measures whose rows repeat across a join
    are problems: Looker computes them with symmetric aggregates."""

    def __init__(self, p: _Parser, ex: dict[str, Any]):
        self.p, self.explore = p, ex
        self.base = ex["name"]
        self.base_view = _base_view(ex)
        self.aliases = _aliases(ex)
        self.joins: dict[str, dict[str, Any]] = {j["name"]: j for j in ex.get("joins", [])}
        self.order: list[str] = []  # joins in SQL order (a join after the joins its ON reads)
        self.join_sql: dict[str, str] = {}
        self.join_reads: dict[str, set[str]] = {}
        self.busy: set[str] = set()
        self.aggs: list[tuple[str, str, str, str]] = []  # (alias, aggregation, field, placeholder) for the fan-out check
        self.held: dict[str, tuple[str, str]] = {}  # placeholder -> (SQL used, symmetric SQL); fanout() picks symmetric when rows repeat
        self.windows: dict[str, tuple[str, str]] = {}  # placeholder -> (percent_of_total / running_total, measure SQL)
        self.liquid: dict[str, str] = {}  # "view.parameter_or_filter" -> the query's value, for Liquid
        self.selected: set[str] = set()  # "view.field" selected, filtered, sorted: for _is_selected and the like
        self.filtered: set[str] = set()
        self.sorted: set[str] = set()
        self.tz: set[str] = set()  # timeframes Looker converts to the query time zone
        self.in_measure = 0
        self.problems: list[str] = []
        self.notes: list[str] = []

    def alias(self, a: str) -> str:
        """The SQL alias for a name in a field reference (the base view's name means the explore)."""
        return self.base if a not in self.joins and a in (self.base, self.base_view) else a

    def view_of(self, a: str) -> str | None:
        return self.aliases.get(self.alias(a))

    def key(self, ref: str, view: str) -> str:
        """"view.field" of a field reference; `view` when it names none."""
        qual, _, name = ref.rpartition(".")
        return f"{(self.view_of(qual) or qual) if qual else view}.{name}"

    def use(self, a: str) -> list[str]:
        a = self.alias(a)
        if a == self.base or a in self.join_sql or a in self.busy:
            return []
        j = self.joins.get(a)
        if j is None:
            return [f"{a} is not joined in explore {self.base}"]
        self.busy.add(a)
        problems: list[str] = []
        for r in j.get("required_joins") or []:
            problems += self.use(r)
        view = self.aliases[a]
        jtype = str(j.get("type", "left_outer"))
        on = ""
        if j.get("sql_on"):
            on, p = self.p._expand(view, j["sql_on"], "query", frozenset(), self, a)
            problems += p
        elif j.get("foreign_key"):
            pk = self.p._pk(view)
            if pk is None:
                problems.append(f"join {a} uses foreign_key, but {view} has no primary key")
            else:
                on, p = self.p._expand(view, f"${{{self.base}.{j['foreign_key']}}} = ${{{a}.{pk['name']}}}", "query", frozenset(), self, a)
                problems += p
        elif j.get("sql") is not None:
            problems.append(f"join {a} is written as SQL (`sql: {' '.join(str(j['sql']).split())}`)")
        elif jtype != "cross":
            problems.append(f"join {a} has no sql_on or foreign_key")
        if j.get("sql_where"):
            problems.append(f"join {a} has sql_where")
        if jtype not in JOIN_TYPE:
            problems.append(f"join {a} has type {jtype}")
        table = self._table(view, problems)
        self.join_reads[a] = (set(re.findall(r"\b(\w+)\.", on)) & set(self.joins)) - {a}
        self.join_sql[a] = f"{JOIN_TYPE.get(jtype, 'LEFT JOIN')} {table} AS {a}" + (f" ON {on}" if on and jtype != "cross" else "")
        self.order.append(a)
        self.busy.discard(a)
        return problems

    def field(self, ref: str) -> tuple[str, str, list[str]]:
        """(SQL, kind, problems) for `alias.field`; kind is dimension / yesno / number / time / measure."""
        qual, _, name = ref.rpartition(".")
        a = self.alias(qual)
        view = self.aliases.get(a)
        if view is None:
            return "", "", [f"{ref} is not a field of explore {self.base}"]
        problems = self.use(a)
        kind = self.p.fields.get(f"{view}.{name}")
        if kind is None:
            return "", "", problems + [f"{ref} is not a field of explore {self.base}"]
        if kind[0] == "time":
            sql, p = self.p._timeframe(view, kind[1], kind[2], "query", frozenset(), self, a)
            return sql, "time", problems + p
        if kind[0] in ("parameter", "filter"):
            return "", kind[0], problems
        f = self.p._field(view, name) or {}
        if kind[0] == "measure":
            sql, p = self.measure(a, view, f, frozenset({f"{a}.{name}"}))
            return sql, "measure", problems + p
        ftype = f.get("type", "string")
        if ftype in ("location", "distance", "duration"):
            return "", "", problems + [f"{ref} is a type: {ftype} dimension"]
        sql, p = self.p._field_sql(view, name, "query", frozenset({f"{a}.{name}"}), self, a)
        if ftype == "tier":
            tier = tier_sql(_paren(sql), f) if sql else None
            if tier is None:
                return "", "", problems + p + [f"{ref} is a type: tier dimension with style: {f.get('style', 'classic')}"]
            return tier, "string", problems + p
        return sql, _type_kind(ftype, "dimension"), problems + p

    def measure(self, a: str, view: str, m: dict[str, Any], seen: frozenset[str]) -> tuple[str, list[str]]:
        mtype = m.get("type", "count")
        ref = f"{a}.{m['name']}"
        problems: list[str] = []
        conds = []
        for field, value in _filter_pairs(m):
            sql, kind, p = self.field(field if "." in field else f"{a}.{field}")
            problems += p
            cond = looker_filter_sql(_paren(sql), value, kind, dialect=self.p.dialect) if sql and kind != "measure" else None
            if cond is None:
                problems.append(f"measure {ref} filter {field}: {value!r} not translated")
            else:
                conds.append(cond)
        cond = " AND ".join(conds)
        self.in_measure += 1
        try:
            if mtype == "count":
                pk = self.p._pk(view)
                if pk is not None:
                    key, p = self.p._field_sql(view, pk["name"], "query", seen, self, a)
                    problems += p
                    self.aggs.append((a, "count_distinct", ref, ""))
                    return f"COUNT(DISTINCT {f'CASE WHEN {cond} THEN {key} END' if cond else key})", problems
                self.aggs.append((a, "count", ref, ""))
                return (f"COUNT(CASE WHEN {cond} THEN 1 END)" if cond else "COUNT(*)"), problems
            if mtype in QUERY_AGG or mtype in ("sum_distinct", "average_distinct"):
                expr, p = self.p._expand(view, m.get("sql") or f"${{TABLE}}.{m['name']}", "query", seen, self, a)
                problems += p
                x = f"CASE WHEN {cond} THEN {expr} END" if cond else expr
            if mtype in QUERY_AGG:
                plain, token = QUERY_AGG[mtype].format(x), ""
                pk = self.p._pk(view) if mtype in ("sum", "average") else None
                if pk is not None:
                    key, kp = self.p._field_sql(view, pk["name"], "query", seen, self, a)
                    sym = None if kp else symmetric_sql(mtype, x, key, self.p.dialect)
                    if sym:
                        token = f"__held{len(self.held)}__"
                        self.held[token] = (plain, sym)
                self.aggs.append((a, mtype, ref, token))
                return token or plain, problems
            if mtype in ("sum_distinct", "average_distinct"):
                pk = self.p._pk(view)
                key_sql = m.get("sql_distinct_key") or (f"${{{pk['name']}}}" if pk else "")
                if not key_sql:
                    return "", problems + [f"measure {ref} is type: {mtype} without sql_distinct_key or a primary key"]
                key, kp = self.p._expand(view, key_sql, "query", seen, self, a)
                sym = symmetric_sql(mtype.split("_")[0], x, key, self.p.dialect)
                if sym is None:
                    return "", problems + kp + [f"measure {ref} is type: {mtype}; set the warehouse type in unshackle.toml to write it"]
                self.aggs.append((a, "sum_distinct", ref, ""))
                return sym, problems + kp
            if mtype in ("percent_of_total", "running_total"):
                refs = REF.findall(m.get("sql") or "")
                inner_types = {str((self.p._field(self.view_of(r.rpartition(".")[0] or a) or view, r.rpartition(".")[2]) or {}).get("type", ""))
                               for r in refs}
                if conds or not refs or not inner_types <= {"sum", "count"}:
                    return "", problems + [f"measure {ref} is type: {mtype} over {', '.join(sorted(inner_types)) or 'SQL'}; "
                                           "Looker computes it over the result rows, write it in the notebook"]
                inner, p = self.p._expand(view, m.get("sql") or "", "query", seen, self, a)
                token = f"__window{len(self.windows)}__"
                self.windows[token] = (mtype, inner)
                return token, problems + p
            if mtype in ("number", "string", "date"):  # the SQL as written; Looker casts a date measure to a date
                if conds:
                    problems.append(f"measure {ref} is type: {mtype} with filters")
                expr, p = self.p._expand(view, m.get("sql") or "", "query", seen, self, a)
                if AGG_FN.search(REF.sub("", m.get("sql") or "")):
                    self.aggs.append((a, "sql", ref, ""))  # aggregates written in SQL: assume rows must not repeat
                return (f"CAST(({expr}) AS DATE)" if mtype == "date" else f"({expr})"), problems + p
            return "", problems + [f"measure {ref} is type: {mtype}"]
        finally:
            self.in_measure -= 1

    def _table(self, view: str, problems: list[str]) -> str:
        """The view's table; its note goes to `notes`, its problem to `problems`."""
        table, note, problem = self.p.query_table(view, self)
        self.notes += [note] if note else []
        problems += [problem] if problem else []
        return table

    def raw_time(self, ref: str) -> tuple[str, str] | None:
        """(SQL, datatype) of the time behind a timeframe field `alias.group_tf`; None for other fields."""
        qual, _, name = ref.rpartition(".")
        a = self.alias(qual)
        view = self.aliases.get(a)
        kind = self.p.fields.get(f"{view}.{name}") if view else None
        if not kind or kind[0] != "time":
            return None
        sql, p = self.p._timeframe(view, kind[1], "raw", "query", frozenset(), self, a)
        g = self.p._group(view, kind[1])
        return (sql, "date" if g.get("datatype") == "date" else "timestamp") if sql and not p else None

    def snapshot(self) -> tuple:
        return list(self.order), dict(self.join_sql), dict(self.join_reads), list(self.aggs), set(self.tz)

    def restore(self, snap: tuple) -> None:
        self.order, self.join_sql, self.join_reads, self.aggs, self.tz = snap

    def risky_joins(self, since: list[str]) -> list[str]:
        """Joins added after `since` that change rows even when nothing is filtered (not a left x_to_one join)."""
        return [a for a in self.order if a not in since and (str(self.joins[a].get("type", "left_outer")) != "left_outer"
                or str(self.joins[a].get("relationship", "many_to_one")) not in ("many_to_one", "one_to_one"))]

    def fanout(self) -> list[str]:
        rel = {a: str(self.joins[a].get("relationship", "many_to_one")) for a in self.order}
        many = [a for a, r in rel.items() if r in ("one_to_many", "many_to_many")]
        outer = [a for a in self.order if self.joins[a].get("type") == "full_outer"]
        out = []
        for a, agg, ref, token in self.aggs:
            if agg in ("count", "sql") and outer:
                out.append(f"{ref} counts rows, and the full outer join of {', '.join(outer)} adds rows without a {self.base}")
                continue
            if agg in FAN_SAFE:
                continue
            direct = rel.get(a) == "one_to_one" and not self.join_reads.get(a)
            if not many and (a == self.base or direct):
                continue
            if many == [a] and not self.join_reads.get(a):  # the many side's own rows are not repeated by its join
                continue
            if token:
                self.held[token] = (self.held[token][1],) * 2  # Looker's symmetric aggregate
                continue
            why = (f"the query joins {', '.join(f'{x} {rel[x]}' for x in many)}" if many
                   else f"{a} is joined {rel.get(a)}" + ("" if rel.get(a) != "one_to_one" else " through another join"))
            out.append(f"{ref} ({agg}) counts repeated rows because {why}; Looker uses symmetric aggregates here")
        return list(dict.fromkeys(out))

    def sql(self, select: list[tuple[str, str]], where: list[str], having: list[str], dims: int, order: list[str], limit: str) -> str:
        """`select` is (expression, column) with the dimensions first."""
        by_column = {c: e for e, c in select}
        order_sql = ", ".join(by_column.get(o.split()[0], o.split()[0]) + (" DESC" if o.endswith(" DESC") else "") for o in order)

        def resolve(text: str) -> str:
            for token, (kind, inner) in self.windows.items():
                if kind == "percent_of_total":
                    window = f"100.0 * {inner} / NULLIF(SUM({inner}) OVER (), 0)"
                elif order_sql and not any(t in order_sql for t in self.windows):
                    window = f"SUM({inner}) OVER (ORDER BY {order_sql} ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)"
                else:
                    self.problems.append("a running_total needs the query sorted by something other than a running total")
                    window = ""
                text = text.replace(token, window)
            for token, (used, _) in self.held.items():
                text = text.replace(token, used)
            return text

        select = [(resolve(e), c) for e, c in select]
        having = [resolve(h) for h in having]
        table = self._table(self.aliases[self.base], self.problems)
        lines = ["SELECT"]
        lines += [f"    {e} AS {c}{',' if i < len(select) - 1 else ''}" for i, (e, c) in enumerate(select)]
        lines.append(f"FROM {table} AS {self.base}")
        lines += [self.join_sql[a] for a in self.order]
        if where:
            lines.append("WHERE " + "\n  AND ".join(where))
        if dims:
            lines.append("GROUP BY " + ", ".join(str(i + 1) for i in range(dims)))
        if having:
            lines.append("HAVING " + "\n  AND ".join(having))
        if order:
            lines.append("ORDER BY " + ", ".join(order))
        if limit:
            lines.append(f"LIMIT {limit}")
        return "\n".join(lines)


VIEW_LISTS = ("dimensions", "dimension_groups", "measures", "filters", "parameters", "sets", "joins")
DASHBOARD_LISTS = ("elements", "filters")


def _merge_view(base: dict[str, Any], override: dict[str, Any], refine: bool = False, named: tuple[str, ...] = VIEW_LISTS) -> dict[str, Any]:
    """Merge a view, explore or LookML dashboard into another: `named` children (fields, joins; dashboard
    elements and filters) by name, other keys replaced. `extends` from a refinement adds to the base's;
    through `extends` it is not inherited."""
    out = {k: v for k, v in base.items()}
    for key, value in override.items():
        if key == "name":
            continue
        if key in named and isinstance(value, list):
            by_name = {str(f.get("name")): dict(f) for f in out.get(key) or [] if isinstance(f, dict)}
            for f in value:
                if isinstance(f, dict):
                    by_name[str(f.get("name"))] = {**by_name.get(str(f.get("name")), {}), **f}
            out[key] = list(by_name.values())
        elif key == "access_filters":
            out[key] = [*out.get(key, []), *(f for f in value if f not in out.get(key, []))]
        elif key in ("extends", "extends__all"):
            if refine:
                parents = [p for g in (base.get("extends__all") or [base.get("extends") or []]) for p in g]
                parents += [p for g in (override.get("extends__all") or [override.get("extends") or []]) for p in g]
                out.pop("extends", None)
                out["extends__all"] = [list(dict.fromkeys(parents))]
        elif key != "extension" or refine:
            out[key] = value
    out.setdefault("name", base.get("name") or override.get("name"))
    return out
