"""Packaged, network-free MCP Apps query table; independent of query execution."""
from importlib.resources import files

UI_URI = 'ui://unshackle/query-results/v1.html'
UI_MIME = 'text/html;profile=mcp-app'


def html() -> str:
    return files('unshackle.mcp').joinpath('result_table/result-table.html').read_text(encoding='utf-8')


def metadata() -> dict:
    # No OAuth, cookies or origin-bound storage: no host-specific ui.domain needed.
    return {'ui': {'prefersBorder': False, 'csp': {
        'connectDomains': [], 'resourceDomains': [], 'frameDomains': [], 'baseUriDomains': []}}}
