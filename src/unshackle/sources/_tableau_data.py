"""Data that lives only in Tableau (extracts, packaged Excel and CSV files) -> Parquet.

`unshackle extract tableau --with-data` downloads such data sources with includeExtract=true and calls
write_tables() on the package: each table becomes <data>/tableau/<name>.parquet with the column types Tableau
has, and extract records what it wrote in the raw file extracts/<id>.json, which parse() reads (no network, no
Parquet). Extracts (.hyper) are read with Tableau's Hyper API, which writes the Parquet itself; Excel and CSV files
are read as they are and cast to the types in the data source's metadata records. Needs the `tableau` extra
(tableauhyperapi, openpyxl, duckdb).
"""

from __future__ import annotations

import csv
import io
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from unshackle.http import SourceError, missing_extra
from unshackle.ir import snake

SCHEMA = "tableau_data"  # where the copied tables live in the warehouse (DuckDB: tables loaded from the Parquet files)
LOCAL_DB = "tableau.duckdb"  # the DuckDB file `build` loads next to the copies
# Tableau local-type -> DuckDB cast of a text value (Excel and CSV cells), so the Parquet column has Tableau's type
CAST = {
    "string": "{}",
    "integer": "coalesce(try_cast({0} as bigint), cast(try_cast({0} as double) as bigint))",
    "real": "try_cast({} as double)",
    "date": "coalesce(try_cast({0} as date), cast(try_cast({0} as timestamp) as date))",
    "datetime": "try_cast({} as timestamp)",
    "boolean": "try_cast({} as boolean)",
}
# Parquet column types that already hold a Tableau type, and the ones a value converts from as Tableau converts it
SAME = {"string": ("VARCHAR",), "integer": ("BIGINT", "INTEGER", "SMALLINT", "TINYINT", "HUGEINT"),
        "real": ("DOUBLE", "FLOAT", "DECIMAL"), "date": ("DATE",), "datetime": ("TIMESTAMP",), "boolean": ("BOOLEAN",)}
CONVERTS = {"string": ("BIGINT", "INTEGER", "SMALLINT"), "integer": ("VARCHAR",), "real": ("VARCHAR", "BIGINT", "INTEGER", "SMALLINT"),
            "date": ("VARCHAR", "TIMESTAMP"), "datetime": ("VARCHAR", "DATE"), "boolean": ("VARCHAR",)}


def _parts(s: str) -> list[str]:
    return [p.replace("]]", "]") for p in re.findall(r"\[((?:[^\]]|\]\])*)\]", s or "")] or ([s] if s else [])


def _member(zf: zipfile.ZipFile, path: str) -> str:
    """The package member a connection's file path names (exact, else the same file name)."""
    want = PurePosixPath(path.replace("\\", "/"))
    names = [n for n in zf.namelist() if not n.endswith("/")]
    for n in names:
        if PurePosixPath(n) == want:
            return n
    same = [n for n in names if PurePosixPath(n).name.lower() == want.name.lower()]
    if len(same) == 1:
        return same[0]
    raise SourceError(f"{want.name} is not in the downloaded package")


def _columns(el: ET.Element, relation: str) -> list[tuple[int, str, str]]:
    """(ordinal, remote name, Tableau type) of a relation's columns, from the metadata records, in file order. The type
    is the one Tableau shows: a type the author changed (<column datatype>) wins over the record's."""
    changed = {c.get("name", ""): c.get("datatype", "") for c in el.findall("column") if c.get("datatype")}
    out = []
    for mr in el.iter("metadata-record"):
        if mr.get("class") != "column" or (_parts(mr.findtext("parent-name") or "") or [""])[-1] != relation:
            continue
        name = (mr.findtext("remote-name") or "").strip()
        try:
            ordinal = int(mr.findtext("ordinal") or len(out))
        except ValueError:
            ordinal = len(out)
        if name:
            typ = changed.get((mr.findtext("local-name") or "").strip()) or (mr.findtext("local-type") or "string").strip()
            out.append((ordinal, name, typ))
    return sorted(out)


def tables_of(conn: ET.Element) -> list[dict[str, Any]]:
    """The tables a data source connection reads: [{relation, class, file, table, separator, interpreter}].
    `conn` is the data source's <connection> (or its extract's)."""
    named = {nc.get("name", ""): nc.find("connection") for nc in conn.iter("named-connection")}
    out = []
    for rel in conn.iter("relation"):
        if rel.get("type") != "table" or not rel.get("table"):
            continue
        c = named.get(rel.get("connection", "")) if rel.get("connection") else None
        c = c if c is not None else conn
        cls = c.get("class", "")
        if cls in ("hyper", "dataengine"):
            file = c.get("dbname", "")
        else:
            file = "/".join(p for p in (c.get("directory", ""), c.get("filename", "")) if p) if cls == "textscan" else c.get("filename", "")
        cols = rel.find("columns")
        name = rel.get("name") or (_parts(rel.get("table", "")) or ["table"])[-1]
        if any(t["relation"] == name for t in out):
            continue
        out.append({"relation": name, "class": cls, "file": file, "table": _parts(rel.get("table", "")),
                    "separator": (cols.get("separator") if cols is not None else "") or ",",
                    "interpreter": c.get("cleaning") == "yes"})
    return out


def check_installed() -> None:
    """SourceError unless the readers are installed."""
    import importlib.util

    missing = [m for m in ("tableauhyperapi", "openpyxl", "duckdb") if importlib.util.find_spec(m) is None]
    if missing:
        raise SourceError(f"{missing_extra('tableau', '--with-data')} (missing: {', '.join(missing)})")


def write_tables(package: bytes, sources: list[tuple[str, ET.Element, ET.Element, str]], data_root: Path, rel_root: str,
                 taken: set[str]) -> tuple[list[dict[str, Any]], list[str]]:
    """Copy the tables of `sources` [(data source key, its XML, the <connection> holding the data, a name)] out of a
    downloaded .tdsx/.twbx. Returns (written tables, errors). `taken` holds table names already used in this run."""
    tables: list[dict[str, Any]] = []
    errors: list[str] = []
    if package[:2] != b"PK":
        return [], ["the download is not a packaged file: Tableau sent no extract or data file"]
    with tempfile.TemporaryDirectory(prefix="unshackle-tableau-data-") as tmp, zipfile.ZipFile(io.BytesIO(package)) as zf:
        for key, el, conn, label in sources:
            found = tables_of(conn)
            for t in found:
                base = snake(label if len(found) == 1 or snake(label) == snake(t["relation"]) else f"{label}_{t['relation']}")
                name, n = base, 2
                while name in taken:
                    name, n = f"{base}_{n}", n + 1
                rel = f"{rel_root}/{name}.parquet"
                try:
                    member = _member(zf, t["file"])
                    src = Path(tmp, f"{len(taken)}{PurePosixPath(member).suffix}")
                    with zf.open(member) as f, src.open("wb") as w:
                        shutil.copyfileobj(f, w)
                    out = data_root / f"{name}.parquet"
                    out.parent.mkdir(parents=True, exist_ok=True)
                    notes: list[str] = []
                    if t["class"] in ("hyper", "dataengine"):
                        rows, cols, notes = _from_hyper(src, t["table"], _columns(el, t["relation"]), out, Path(tmp))
                    elif t["interpreter"]:
                        raise SourceError("read with Tableau's Data Interpreter, which cleans the file in ways unshackle can't repeat")
                    else:
                        rows, cols = _from_file(src, t, _columns(el, t["relation"]), out, Path(tmp))
                except Exception as exc:  # noqa: BLE001 - Hyper and DuckDB errors too: one unreadable table must not stop the rest
                    errors.append(f"{label} / {t['relation']}: {exc}")
                    continue
                taken.add(name)
                tables.append({"datasource": key, "relation": t["relation"], "class": t["class"], "file": member,
                               "table": name, "parquet": rel, "rows": rows, "columns": cols, "notes": notes})
    return tables, errors


def _from_hyper(path: Path, parts: list[str], columns: list[tuple[int, str, str]], out: Path,
                tmp: Path) -> tuple[int, list[list[str]], list[str]]:
    try:
        from tableauhyperapi import Connection, HyperProcess, TableName, Telemetry, escape_string_literal
    except ImportError as exc:
        raise ImportError(missing_extra("tableau", "reading a .hyper extract")) from exc
    with HyperProcess(Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU, parameters={"log_dir": str(tmp)}) as hyper:
        with Connection(hyper.endpoint, str(path)) as con:
            names = [t for s in con.catalog.get_schema_names() for t in con.catalog.get_table_names(s)]
            want = [p.lower() for p in parts[-2:]]
            match = [t for t in names if [t.schema_name.name.unescaped.lower(), t.name.unescaped.lower()][-len(want):] == want]
            if not match and len(names) == 1:
                match = names
            if len(match) != 1:
                raise SourceError(f"table {'.'.join(parts)} not found in the extract (it has {', '.join(str(n) for n in names) or 'none'})")
            table: TableName = match[0]
            rows = con.execute_scalar_query(f"select count(*) from {table}")
            con.execute_command(f"COPY {table} TO {escape_string_literal(str(out))} WITH (FORMAT PARQUET)")
    cols, notes = _retype(out, columns, tmp)
    return int(rows), cols, notes


def _retype(out: Path, columns: list[tuple[int, str, str]], tmp: Path) -> tuple[list[list[str]], list[str]]:
    """Cast the columns whose type in the file isn't the one Tableau shows (a type the author changed). A change
    unshackle can't repeat the way Tableau does (a number or date shown as text) keeps the file's type, with a note."""
    try:
        import duckdb
    except ImportError as exc:
        raise ImportError(missing_extra("tableau", "copying extracts")) from exc
    want = {name: typ for _, name, typ in columns}
    con = duckdb.connect()
    try:
        have = [(r[0], r[1]) for r in con.execute("describe select * from read_parquet(?)", [str(out)]).fetchall()]
        items, notes, changed = [], [], False
        for name, typ in have:
            q = '"' + name.replace('"', '""') + '"'
            tab, base = want.get(name, ""), typ.split("(")[0]
            if not tab or base in SAME.get(tab, (base,)):
                items.append(q)
            elif base in CONVERTS.get(tab, ()):
                items.append(f"{CAST[tab].format(q if base == 'VARCHAR' else f'cast({q} as varchar)')} as {q}")
                changed = True
            else:
                items.append(q)
                notes.append(f"{name}: Tableau shows it as {tab}, the extract holds {typ}; kept as {typ}, convert it as Tableau does")
        if changed:
            fixed = tmp / f"{out.stem}.retyped.parquet"
            con.execute(f"copy (select {', '.join(items)} from read_parquet(?)) to ? (format parquet)", [str(out), str(fixed)])
            shutil.move(fixed, out)
        cols = [[r[0], r[1]] for r in con.execute("describe select * from read_parquet(?)", [str(out)]).fetchall()]
    finally:
        con.close()
    return cols, notes


def _from_file(path: Path, t: dict[str, Any], columns: list[tuple[int, str, str]], out: Path, tmp: Path) -> tuple[int, list[list[str]]]:
    try:
        import duckdb
    except ImportError as exc:
        raise ImportError(missing_extra("tableau", "copying Excel and CSV data")) from exc
    if not columns:
        raise SourceError("no metadata records for this table, so its columns and types are unknown")
    text = path
    if t["class"] in ("excel-direct", "excel"):
        text = tmp / f"{path.stem}.csv"
        _excel_to_csv(path, t["table"][-1] if t["table"] else "", text)
    elif t["class"] != "textscan":
        raise SourceError(f"{t['class']} files are not read by unshackle")
    con = duckdb.connect()
    try:
        con.execute("create table raw as select * from read_csv(?, header = true, all_varchar = true, delim = ?)", [str(text), t["separator"]])
        have = [r[0] for r in con.execute("describe raw").fetchall()]
        items = []
        for i, (_, name, typ) in enumerate(columns):  # ordinals count across the data source's tables: keep their order only
            col = name if name in have else (have[i] if i < len(have) else None)
            if col is None:
                raise SourceError(f"column {name!r} is not in the file")
            src = '"' + col.replace('"', '""') + '"'
            items.append(f"{CAST.get(typ, '{}').format(src)} as \"{name.replace(chr(34), chr(34) * 2)}\"")
        con.execute(f"copy (select {', '.join(items)} from raw) to ? (format parquet)", [str(out)])
        rows = con.execute("select count(*) from raw").fetchone()[0]
        cols = [[r[0], r[1]] for r in con.execute("describe select * from read_parquet(?)", [str(out)]).fetchall()]
    finally:
        con.close()
    return int(rows), cols


def _excel_to_csv(path: Path, table: str, out: Path) -> None:
    """One sheet (`Orders$`) of an .xlsx as CSV text, header row first. Other Excel formats and named ranges: error."""
    try:
        import openpyxl
    except ImportError as exc:
        raise ImportError(missing_extra("tableau", "reading Excel data")) from exc
    if path.suffix.lower() not in (".xlsx", ".xlsm"):
        raise SourceError(f"{path.suffix} workbooks are not read by unshackle (save the file as .xlsx and re-run)")
    if not table.endswith("$"):
        raise SourceError(f"{table!r} is a named range or a Data Interpreter table, not a sheet; unshackle reads whole sheets only")
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        if table[:-1] not in wb.sheetnames:
            raise SourceError(f"sheet {table[:-1]!r} is not in the workbook")
        rows = [["" if v is None else v for v in row] for row in wb[table[:-1]].iter_rows(values_only=True)]
        while rows and not any(v != "" for v in rows[-1]):
            rows.pop()  # blank rows below the data (a formatted range) hold no records
        with out.open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(rows)
    finally:
        wb.close()


def load_sql(tables: list[dict[str, Any]]) -> str:
    """DuckDB tables loaded from the Parquet copies, in the schema the models read. Tables, not views over the files: a
    view resolves its relative path when it is queried, so it would break in a notebook that runs from notebooks/."""
    lines = [f"create schema if not exists {SCHEMA};"]
    for t in tables:
        lines.append(f"create or replace table {SCHEMA}.{t['table']} as select * from read_parquet('{t['parquet']}');")
    return "\n".join(lines)


def next_steps(data_dir: str) -> str:
    """How to run the notebooks on the copies. `data_dir` is the copies' folder, relative to the repo root."""
    return (f"`unshackle build tableau` loads the copies into {data_dir}/{LOCAL_DB}; the notebooks use that file when "
            f"UNSHACKLE_WAREHOUSE_URL is unset, and `unshackle run` runs them all")
