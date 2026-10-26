"""migration/unshackle.lock: what was generated (and by which tool), so re-runs never overwrite hand edits."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from unshackle.raw import sha256

LOCK_VERSION = 2


def _norm(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


@dataclass
class Lock:
    path: Path
    files: dict[str, str] = field(default_factory=dict)
    owners: dict[str, str] = field(default_factory=dict)
    sources: dict[str, dict[str, str]] = field(default_factory=dict)
    adopted: list[str] = field(default_factory=list)  # existed before unshackle first wrote them: never removed

    @classmethod
    def load(cls, repo: Path) -> Lock:
        path = repo / "migration" / "unshackle.lock"
        if not path.exists():
            return cls(path=path)
        data = json.loads(path.read_text())
        return cls(
            path=path,
            files=data.get("files", {}),
            owners=data.get("owners", {}),
            sources=data.get("sources", {}),
            adopted=data.get("adopted", []),
        )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": LOCK_VERSION,
            "sources": self.sources,
            "files": dict(sorted(self.files.items())),
            "owners": dict(sorted(self.owners.items())),
        }
        if self.adopted:
            data["adopted"] = sorted(set(self.adopted))
        self.path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")

    def record_source(self, tool: str, manifest: dict[str, str]) -> None:
        self.sources[tool] = dict(manifest)

    def changed_sources(self, tool: str, manifest: dict[str, str]) -> dict[str, str]:
        before = self.sources.get(tool, {})
        out = {}
        for key, digest in manifest.items():
            if key not in before:
                out[key] = "added"
            elif before[key] != digest:
                out[key] = "changed"
        for key in before:
            if key not in manifest:
                out[key] = "removed"
        return out

    def is_hand_edited(self, rel: str, current: str) -> bool:
        recorded = self.files.get(rel)
        return recorded is not None and recorded != sha256(_norm(current))

    def record_file(self, rel: str, content: str, tool: str = "") -> None:
        self.files[rel] = sha256(_norm(content))
        if tool and rel not in self.owners:
            self.owners[rel] = tool
