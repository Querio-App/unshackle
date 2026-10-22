"""Bundled skills and spec: inside the wheel at unshackle/_data, or the repo root in a checkout."""

from __future__ import annotations

import shutil
from pathlib import Path


def data_dir(name: str) -> Path:
    packaged = Path(__file__).parent / "_data" / name
    if packaged.is_dir():
        return packaged
    checkout = Path(__file__).resolve().parents[2] / name
    if checkout.is_dir():
        return checkout
    raise FileNotFoundError(f"bundled {name}/ not found; reinstall unshackle")


def copy_skills(repo: Path, update: bool = False) -> list[str]:
    """Copy the migration skills and the structure spec into <repo>/migration/. Returns files written."""
    written = []
    for name in ("skills", "spec"):
        src = data_dir(name)
        dest = repo / "migration" / name
        for path in sorted(src.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            rel = path.relative_to(src)
            out = dest / rel
            if out.exists() and not update:
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, out)
            written.append(str(Path("migration") / name / rel))
    return written
