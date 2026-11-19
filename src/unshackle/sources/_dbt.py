"""Neutral dbt YAML loading and named-node traversal (no Lightdash semantics)."""
from __future__ import annotations

from typing import Any, Iterator

import yaml


def load_yaml(text: str) -> dict[str, Any]:
    value = yaml.safe_load(text)
    return value if isinstance(value, dict) else {}


def nodes(document: dict[str, Any], key: str) -> Iterator[dict[str, Any]]:
    value = document.get(key) or []
    if not isinstance(value, list):
        return
    for node in value:
        if isinstance(node, dict) and node.get("name"):
            yield node
