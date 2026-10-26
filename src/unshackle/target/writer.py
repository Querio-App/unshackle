"""Writes files into the target repo, respecting hand edits and other tools' files.

- A file unshackle wrote and nobody touched since is updated.
- A file someone edited (hash differs from the lock), or that unshackle didn't create, is kept.
- A file another tool's build owns is never overwritten: the new one gets a `.<tool>` suffix.
- Paths are always inside the repo, whatever names the source used.
- A file this tool's earlier build wrote and this build didn't is removed, unless edited by hand
  or it existed before unshackle first wrote it (merged into, so "adopted").
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from unshackle.lock import Lock
from unshackle.target.layout import Layout


class UnsafePath(ValueError):
    pass


@dataclass
class WriteResult:
    written: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    kept_hand_edits: list[str] = field(default_factory=list)
    renamed: dict[str, str] = field(default_factory=dict)
    removed: list[str] = field(default_factory=list)
    kept_stale: list[str] = field(default_factory=list)


def normalize(content: str) -> str:
    return content.replace("\r\n", "\n").replace("\r", "\n")


class Target:
    def __init__(self, repo: Path, dry_run: bool = False, tool: str = ""):
        self.repo = repo
        self.root = repo.resolve()
        self.layout = Layout.load(repo)
        self.lock = Lock.load(repo)
        self.dry_run = dry_run
        self.tool = tool
        self.result = WriteResult()

    def _safe(self, rel: str) -> Path:
        path = (self.root / rel).resolve()
        if path != self.root and self.root not in path.parents:
            raise UnsafePath(f"refusing to write outside the repo: {rel}")
        return path

    def _rename_for_tool(self, rel: str) -> str:
        p = Path(rel)
        return str(p.with_name(f"{p.stem}.{self.tool}{p.suffix}"))

    def write(self, rel: str, content: str, managed: bool = True, merged: bool = False) -> bool:
        """Write `content` to `rel`. Returns False when an existing file was kept.

        managed=False files (AGENTS.md, unshackle.toml) are only created, never updated.
        merged=True means the caller already folded the existing file into `content`
        (catalog descriptions), so overwriting loses nothing.
        """
        content = normalize(content)
        owner = self.lock.owners.get(rel, "")
        if self.tool and owner and owner != self.tool and not merged and self.read(rel) != content:
            new_rel = self._rename_for_tool(rel)
            self.result.renamed[rel] = new_rel
            rel = new_rel
        path = self._safe(rel)
        if path.exists():
            current = path.read_bytes().decode("utf-8", errors="replace")
            if rel not in self.lock.files and rel not in self.lock.adopted:
                self.lock.adopted.append(rel)
            if current == content:
                self.result.unchanged.append(rel)
                self.lock.record_file(rel, content, self.tool)
                return True
            blocked = self.lock.is_hand_edited(rel, current) or rel not in self.lock.files
            if not managed or (blocked and not merged):
                self.result.kept_hand_edits.append(rel)
                return False
        if not self.dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content.encode("utf-8"))
        self.lock.record_file(rel, content, self.tool)
        self.result.written.append(rel)
        return True

    def read(self, rel: str) -> str | None:
        path = self._safe(rel)
        return normalize(path.read_bytes().decode("utf-8", errors="replace")) if path.exists() else None

    def prune(self) -> None:
        """Remove files this tool's earlier builds wrote that this build didn't (kept if edited by hand)."""
        r = self.result
        touched = set(r.written) | set(r.unchanged) | set(r.kept_hand_edits)
        for rel, owner in sorted(self.lock.owners.items()):
            if owner != self.tool or not self.tool or rel in touched or rel in self.lock.adopted:
                continue
            path = self._safe(rel)
            if path.exists():
                if self.lock.is_hand_edited(rel, path.read_bytes().decode("utf-8", errors="replace")):
                    r.kept_stale.append(rel)
                    continue
                r.removed.append(rel)
                if not self.dry_run:
                    path.unlink()
                    self._remove_empty_dirs(path.parent)
            self.lock.files.pop(rel, None)
            self.lock.owners.pop(rel, None)

    def _remove_empty_dirs(self, folder: Path) -> None:
        """Remove folders a removal left empty, below the layout's directories (never those or the repo root)."""
        stops = {self.root} | {(self.root / p).resolve() for p in self.layout.paths.values()}
        while folder not in stops and self.root in folder.parents and not any(folder.iterdir()):
            folder.rmdir()
            folder = folder.parent

    def save(self) -> None:
        if self.dry_run:
            return
        self.lock.save()
        last = self.repo / "migration" / "last_build.json"
        last.parent.mkdir(parents=True, exist_ok=True)
        last.write_text(json.dumps({"tool": self.tool, **asdict(self.result)}, indent=2) + "\n")


def load_last_result(repo: Path) -> WriteResult | None:
    path = repo / "migration" / "last_build.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    data.pop("tool", None)
    return WriteResult(**data)
