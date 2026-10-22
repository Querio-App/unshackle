"""`unshackle import`: files a user downloaded by hand -> migration/raw/<tool>/.

Each source's `import_path()` maps one downloaded file or folder to the raw paths its
parse() reads. This module walks folders, unpacks .zip downloads safely and writes
the result plus `_import.json` (origin "manual" and what a manual export can't carry,
which build lists in migration/report.md). Nothing inside the files is ever run.
"""

from __future__ import annotations

import os
import re
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Iterator

from unshackle.http import SourceError
from unshackle.raw import RawStore

IMPORT_META = "_import.json"
MAX_UNZIPPED = 2 * 1024**3  # refuse archives that expand past 2 GiB


def walk(root: Path) -> Iterator[tuple[str, Path]]:
    """(posix path relative to root, file) for every regular file under root.
    Skips .git and never follows symlinks, so nothing outside root is read."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        for name in sorted(filenames):
            p = Path(dirpath, name)
            if not p.is_symlink() and p.is_file():
                yield p.relative_to(root).as_posix(), p


def files_of(path: Path) -> Iterator[tuple[str, Path]]:
    """walk() for a folder; (name, path) for a single file."""
    if path.is_dir():
        yield from walk(path)
    else:
        yield path.name, path


def member_name(name: str) -> str:
    """A zip member name that stays inside the folder it is unpacked into, or SourceError."""
    pure = PurePosixPath(name)
    if not name or "\\" in name or "\x00" in name or pure.is_absolute() or ".." in pure.parts or re.match(r"^[A-Za-z]:", name):
        raise SourceError(f"unsafe path in archive: {name!r}")
    return pure.as_posix()


def unzip(archive: Path, dest: Path) -> None:
    """Unpack a .zip into dest, rejecting absolute paths, `..`, symlinks and zip bombs."""
    try:
        with zipfile.ZipFile(archive) as zf:
            infos = zf.infolist()
            if sum(i.file_size for i in infos) > MAX_UNZIPPED:
                raise SourceError(f"{archive.name}: expands to more than {MAX_UNZIPPED // 1024**3} GiB")
            for info in infos:
                rel = member_name(info.filename)
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise SourceError(f"{archive.name}: symlink in archive: {info.filename!r}")
                if info.is_dir():
                    continue
                out = dest / rel
                if not out.resolve().is_relative_to(dest.resolve()):
                    raise SourceError(f"unsafe path in archive: {info.filename!r}")
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(zf.read(info))
    except zipfile.BadZipFile as exc:
        raise SourceError(f"{archive.name}: not a valid zip ({exc})") from exc


def safe_name(value: str) -> str:
    """A file-name-safe id for a raw path segment."""
    return re.sub(r"[^\w.() -]+", "_", value).strip(" .") or "_"


def run(src, store: RawStore, paths: list[Path]) -> list[str]:
    """Map every input with src.import_path(), then replace migration/raw/<tool>/ with the result."""
    out: dict[str, bytes] = {}
    with tempfile.TemporaryDirectory(prefix="unshackle-import-") as tmp:
        for i, path in enumerate(paths):
            path = path.expanduser()
            if not path.exists():
                raise SourceError(f"{path}: not found")
            if path.is_file() and path.suffix.lower() == ".zip":
                dest = Path(tmp, str(i), safe_name(path.stem))
                unzip(path, dest)
                inner = list(dest.iterdir()) if dest.exists() else []
                path = inner[0] if len(inner) == 1 and inner[0].is_dir() else dest  # GitHub-style zips wrap one folder
            found = src.import_path(path)
            if not found:
                raise SourceError(f"{path.name}: nothing {src.label} can use here. `unshackle import {src.name}` accepts {src.manual_files}")
            out.update(found)
    for rel in out:
        try:
            store.path(rel)
        except ValueError as exc:
            raise SourceError(str(exc)) from exc
    store.begin()
    for rel, data in sorted(out.items()):
        store.put_bytes(rel, data)
    store.put_json(IMPORT_META, {
        "origin": "manual",
        "inputs": [p.name for p in paths],
        "files": sorted(out),
        "gaps": list(src.manual_gaps),
    })
    store.save()
    return sorted(out)
