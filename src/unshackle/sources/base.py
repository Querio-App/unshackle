"""The contract every BI source implements.

A source has three steps, all deterministic:
  inventory(): list what exists (models, dashboards, charts...) with usage where the tool has it
  extract(store): pull raw definitions into migration/raw/<tool>/ untouched
  parse(store): turn the raw files into a neutral Bundle (unshackle.ir)

parse() never calls the network, so it can be re-run and tested on fixtures.

Without API access, `unshackle import` takes files downloaded by hand instead:
import_path() maps them to the same raw layout, so parse() doesn't care where they came from.
Anything parse() cannot translate keeps its source expression and a `todo`,
which the agent finishes using skills/translate-*.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from unshackle.http import SourceError
from unshackle.ir import Bundle
from unshackle.raw import RawStore


@dataclass
class InventoryItem:
    kind: str
    id: str
    name: str
    parent: str = ""
    views: int | None = None
    last_viewed: str = ""
    url: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Inventory:
    tool: str
    items: list[InventoryItem] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(self, kind: str, id: Any, name: str, **kw: Any) -> None:
        self.items.append(InventoryItem(kind=kind, id=str(id), name=name, **kw))

    def to_dict(self) -> dict[str, Any]:
        return {"tool": self.tool, "items": [asdict(i) for i in self.items], "notes": self.notes}

    def to_markdown(self) -> str:
        lines = [f"# Inventory: {self.tool}", ""]
        for note in self.notes:
            lines.append(f"> {note}")
        if self.notes:
            lines.append("")
        kinds: dict[str, list[InventoryItem]] = {}
        for item in self.items:
            kinds.setdefault(item.kind, []).append(item)
        for kind, items in kinds.items():
            items = sorted(items, key=lambda i: (-(i.views or 0), i.name.lower()))
            lines += [f"## {kind} ({len(items)})", "", "| Name | Id | Views | Last viewed |", "| --- | --- | --- | --- |"]
            for i in items:
                name = f"[{i.name}]({i.url})" if i.url else i.name
                views = "" if i.views is None else str(i.views)
                lines.append(f"| {name.replace('|', '/')} | `{i.id}` | {views} | {i.last_viewed} |")
            lines.append("")
        return "\n".join(lines)


class Source(ABC):
    name: str = ""
    label: str = ""
    env_vars: tuple[str, ...] = ()
    required_env: tuple[str, ...] = ()  # env_vars the API path can't run without; `migrate` checks them first
    extra: str = ""  # the pyproject extra this source needs beyond `run`, shown by `unshackle tools`
    docs: str = ""
    manual_files: str = ""  # what `unshackle import` accepts, named in errors
    manual_gaps: tuple[str, ...] = ()  # what those files can't carry; listed in migration/report.md

    @abstractmethod
    def inventory(self) -> Inventory: ...

    @abstractmethod
    def extract(self, store: RawStore, only: set[str] | None = None) -> None:
        """Write raw files. `only` limits to these inventory ids when given."""

    @abstractmethod
    def parse(self, store: RawStore) -> Bundle: ...

    def check(self) -> str:
        """One cheap authenticated call (`unshackle check`, and before `inventory`). Returns what it reached;
        errors propagate and unshackle.connect turns them into what to fix."""
        raise SourceError(f"{self.label} has no connection check yet; run `unshackle inventory {self.name}`")

    def import_path(self, path: Path) -> dict[str, bytes]:
        """Map one downloaded file or folder (zips already unpacked) to {raw path: bytes}.
        Return {} when nothing in it is usable; raise SourceError for a file type this tool can't read."""
        raise SourceError(f"{self.label} has no file download unshackle can read; use `unshackle extract {self.name}`")
