"""Non-BI imports. Adapters return a Bundle; only warehouse introspection uses the network."""

from pathlib import Path
from typing import Sequence

from unshackle.ir import Bundle



def parse(tool: str, paths: Sequence[Path], *, url: str | None = None) -> Bundle:
    if tool == "warehouse":
        if paths:
            raise ValueError("warehouse import takes no paths")
        if not url:
            raise ValueError("set UNSHACKLE_WAREHOUSE_URL to import warehouse")
        from .warehouse import introspect
        return introspect(url)
    if tool == "dbt":
        if len(paths) != 1:
            raise ValueError("dbt import needs exactly one project path")
        from .dbt import parse as parse_dbt
        return parse_dbt(paths[0])
    if tool == "sql":
        if not paths:
            raise ValueError("sql import needs at least one path")
        from .sql import parse as parse_sql
        return parse_sql(paths)
    raise ValueError("unknown import source")
