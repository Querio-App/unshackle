from __future__ import annotations

import importlib

from unshackle.sources.base import Source

TOOLS = {
    "lightdash": "unshackle.sources.lightdash:LightdashSource",
    "omni": "unshackle.sources.omni:OmniSource",
    "sigma": "unshackle.sources.sigma:SigmaSource",
    "thoughtspot": "unshackle.sources.thoughtspot:ThoughtSpotSource",
    "tableau": "unshackle.sources.tableau:TableauSource",
    "powerbi": "unshackle.sources.powerbi:PowerBISource",
    "hex": "unshackle.sources.hex:HexSource",
    "looker": "unshackle.sources.looker:LookerSource",
    "metabase": "unshackle.sources.metabase:MetabaseSource",
    "superset": "unshackle.sources.superset:SupersetSource",
    "redash": "unshackle.sources.redash:RedashSource",
}


def get_source(tool: str) -> Source:
    if tool not in TOOLS:
        raise KeyError(f"unknown tool {tool!r}; choose from {', '.join(TOOLS)}")
    module, _, cls = TOOLS[tool].partition(":")
    return getattr(importlib.import_module(module), cls)()


def source_class(tool: str) -> type[Source]:
    module, _, cls = TOOLS[tool].partition(":")
    return getattr(importlib.import_module(module), cls)
