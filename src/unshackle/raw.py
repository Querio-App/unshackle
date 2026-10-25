"""Raw export store: migration/raw/<tool>/. Untouched source files, hashed for diffing."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def sha256(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


class RawStore:
    def __init__(self, repo: Path, tool: str):
        if Path(tool).name != tool or tool in ("", ".", ".."):
            raise ValueError(f"invalid raw tool: {tool}")
        self.tool = tool
        self.repo_root = repo.resolve()
        self.root = self.repo_root / "migration" / "raw" / tool
        self.manifest_path = self.path("_manifest.json")
        self.manifest: dict[str, str] = {}
        if self.manifest_path.exists():
            self.manifest = json.loads(self.manifest_path.read_text())

    def warehouse(self, datasource: str = "warehouse") -> str:
        """The warehouse type unshackle.toml sets for `datasource` ([datasources.<name>] type), or "".
        parse() reads it to write SQL in that warehouse's dialect; it wins over what the export reports."""
        from unshackle.target.layout import Layout

        try:
            return Layout.load(self.repo_root).warehouses.get(datasource, "")
        except (OSError, ValueError):
            return ""

    def begin(self) -> None:
        """Start a fresh extract: files not written again are removed on save()."""
        self._fresh = True
        self.manifest = {}

    def path(self, rel: str) -> Path:
        root = self.root.resolve()
        if root != self.root:
            raise ValueError(f"raw root redirects outside its directory: {self.root}")
        path = (root / rel).resolve()
        if Path(rel).is_absolute() or not path.is_relative_to(root):
            raise ValueError(f"raw path outside {root}: {rel}")
        return path

    def put_bytes(self, rel: str, data: bytes) -> Path:
        p = self.path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        self.manifest[rel] = sha256(data)
        return p

    def put_text(self, rel: str, text: str) -> Path:
        return self.put_bytes(rel, text.encode("utf-8"))

    def put_json(self, rel: str, obj: Any) -> Path:
        return self.put_text(rel, json.dumps(obj, indent=2, sort_keys=True, default=str) + "\n")

    def exists(self, rel: str) -> bool:
        return self.path(rel).exists()

    def get_text(self, rel: str) -> str:
        return self.path(rel).read_text(encoding="utf-8")

    def get_json(self, rel: str) -> Any:
        return json.loads(self.get_text(rel))

    def glob(self, pattern: str) -> list[str]:
        root = self.path(".")
        if not root.exists():
            return []
        return sorted(
            str(p.relative_to(root))
            for p in root.glob(pattern)
            if p.is_file() and p.name != "_manifest.json"
        )

    def rescan(self) -> None:
        """Rebuild the manifest from files on disk (raw files copied in by hand or via MCP)."""
        root = self.path(".")
        self.manifest = {
            str(p.relative_to(root)): sha256(self.path(str(p.relative_to(root))).read_bytes())
            for p in sorted(root.rglob("*"))
            if p.is_file() and p.name != "_manifest.json"
        } if root.exists() else {}

    def had_errors(self) -> bool:
        """True when the extract recorded failures in a _errors*.json file."""
        for rel in self.manifest:
            name = rel.rsplit("/", 1)[-1]
            if name.startswith("_errors") or name.startswith("_skipped"):
                try:
                    if json.loads(self.get_text(rel)):
                        return True
                except (ValueError, OSError):
                    return True
        return False

    def content_files(self) -> list[str]:
        """Raw files that hold definitions (not _errors.json and other bookkeeping)."""
        return [rel for rel in self.manifest if not rel.rsplit("/", 1)[-1].startswith("_")]

    def save(self) -> None:
        """Write the manifest. After begin(), files not re-written are pruned,
        unless the extract recorded errors: a failed list call must never wipe
        an earlier good export."""
        root = self.path(".")
        root.mkdir(parents=True, exist_ok=True)
        if getattr(self, "_fresh", False) and not self.had_errors():
            for p in root.rglob("*"):
                rel = str(p.relative_to(root))
                if p.is_file() and p.name != "_manifest.json" and rel not in self.manifest:
                    p.unlink()
        elif getattr(self, "_fresh", False):
            for p in root.rglob("*"):
                rel = str(p.relative_to(root))
                if p.is_file() and p.name != "_manifest.json" and rel not in self.manifest:
                    self.manifest[rel] = sha256(self.path(rel).read_bytes())
        self.path("_manifest.json").write_text(json.dumps(self.manifest, indent=2, sort_keys=True) + "\n")
