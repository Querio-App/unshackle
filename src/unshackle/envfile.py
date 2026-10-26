""".env support: credentials in a git-ignored file at the repo root instead of pasted into a terminal or chat.

Lines are `NAME=value`, optionally `export NAME=value`, with `#` comments and single or double quotes.
Variables already set in the environment win over the file.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def find(start: Path) -> Path | None:
    """The .env in start or the nearest parent, stopping at the Git root."""
    for d in (start.resolve(), *start.resolve().parents):
        if (d / ".env").is_file():
            return d / ".env"
        if (d / ".git").exists():
            return None
    return None


def parse(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = _LINE.match(line)
        if not m:
            continue
        name, value = m.groups()
        if value[:1] in ("'", '"') and value[1:].find(value[0]) >= 0:
            quote, value = value[0], value[1:value.index(value[0], 1)]
            if quote == '"':
                value = value.replace("\\n", "\n")
        else:
            value = re.sub(r"\s+#.*$", "", value)
        out[name] = value
    return out


def load(start: Path) -> Path | None:
    """Set variables from the nearest .env that aren't set already. Returns the file read, if any."""
    path = find(start)
    if path is None:
        return None
    for name, value in parse(path.read_text(encoding="utf-8")).items():
        os.environ.setdefault(name, value)
    return path


def write(path: Path, values: dict[str, str]) -> None:
    """Set values in a .env file, replacing lines for the same names and keeping everything else."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    lines = [ln for ln in lines if not ((m := _LINE.match(ln)) and m.group(1) in values)]
    lines += [f"{name}='{value}'" if "'" not in value else f'{name}="{value}"' for name, value in values.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)
