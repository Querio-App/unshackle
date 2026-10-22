"""Where each kind of file lives in the target repo. Paths come from unshackle.toml [paths], warehouse types from [datasources.*]."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PATHS = {
    "semantic": "semantic",
    "catalog": "catalog",
    "knowledge": "knowledge",
    "queries": "queries",
    "notebooks": "notebooks",
    "skills": "skills",
    "memory": "memory",
    "policies": "policies",
    "automations": "automations",
    "evals": "evals",
    "sql_models": "queries/models",
}

DBT_OVERRIDES = {
    "semantic": "models/semantic",
    "sql_models": "models/unshackle",
}


@dataclass
class Layout:
    repo: Path
    paths: dict[str, str]
    is_dbt: bool
    warehouses: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, repo: Path) -> Layout:
        is_dbt = (repo / "dbt_project.yml").exists()
        paths = dict(DEFAULT_PATHS)
        if is_dbt:
            paths.update(DBT_OVERRIDES)
        warehouses: dict[str, str] = {}
        cfg = repo / "unshackle.toml"
        if cfg.exists():
            data = tomllib.loads(cfg.read_text())
            for name, ds in data.get("datasources", {}).items():
                if isinstance(ds, dict) and ds.get("type"):
                    warehouses[name] = str(ds["type"]).lower()
            for key, value in data.get("paths", {}).items():
                if isinstance(value, list):
                    value = value[0] if value else paths.get(key, key)
                paths[key] = str(value)
        return cls(repo=repo, paths=paths, is_dbt=is_dbt, warehouses=warehouses)

    def rel(self, kind: str, *parts: str) -> str:
        return str(Path(self.paths[kind], *parts))

    def migration(self, *parts: str) -> str:
        return str(Path("migration", *parts))
