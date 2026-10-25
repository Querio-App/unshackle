"""What the Tableau parser reads from workbook and data source XML, and a count of what it doesn't.

READ lists the elements the parser interprets, each with the attributes it uses. WHOLE elements are read with all
their attributes and content (a table calculation, a set's definition). BY_DESIGN elements are not migrated on purpose
(formatting, window state, file bookkeeping); their content is not looked at further. QUIET attributes of read
elements only place or style them. Any other element or attribute is counted as not read, by kind, so
migration/report.md lists it. A key `parent/tag` takes precedence over `tag`. DEFAULT elements are read only at
their default value (any other value is counted).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

NOT_READ = "not read"
SKIPPED = "skipped: a required attribute is missing"
FORMATTING = "formatting (not migrated)"
LAYOUT = "layout and sizes (notebooks lay out tiles themselves)"
UI_STATE = "Tableau window and UI state"
BOOKKEEPING = "Tableau file bookkeeping"
COLUMN_METADATA = "database column metadata (types come from local-type)"

_TEXT: frozenset[str] = frozenset()  # elements read for their text only

READ: dict[str, frozenset[str]] = {k: frozenset(v) for k, v in {
    # workbook and data sources
    "workbook": {"locale"},
    "datasources": _TEXT,
    "datasource": {"name", "caption", "formatted-name"},
    "connection": {"class", "dbname", "schema", "server", "port", "filename", "directory", "odbc-connect-string-extras",
                   "one-time-sql", "update-time"},
    "named-connections": _TEXT,
    "named-connection": {"name", "caption"},
    "relation": {"connection", "join", "name", "table", "type"},
    "clause": {"type"},
    "expression": {"op"},
    "cols": {"total"},
    "map": {"key", "value"},
    "metadata-records": _TEXT,
    "metadata-record": {"class"},
    "local-name": _TEXT,
    "remote-name": _TEXT,
    "parent-name": _TEXT,
    "local-type": _TEXT,
    "metadata-record/aggregation": _TEXT,
    "filter/min": _TEXT,
    "filter/max": _TEXT,
    "column": {"name", "caption", "datatype", "role", "type", "hidden", "aggregation", "default-format", "semantic-role",
               "param-domain-type", "value"},
    "calculation": {"class", "formula", "column", "default", "peg", "size"},
    "bin": {"value"},
    "value": _TEXT,
    "aliases": _TEXT,
    "alias": {"key", "value"},
    "desc": _TEXT,
    "formatted-text": _TEXT,
    "run": _TEXT,
    "members": _TEXT,
    "member": {"value", "alias"},
    "group": {"name", "caption", "auto-column"},
    "filter": {"class", "column", "context", "filter-group", "first-period", "last-period", "include-future", "include-null",
               "included-values", "period-anchor", "period-type-v2"},
    "extract": {"enabled", "count", "units"},
    "refresh": {"incremental-updates"},
    "drill-paths": _TEXT,
    "drill-path": {"name"},
    "field": _TEXT,
    "folders-common": _TEXT,
    "folder": {"name"},
    "folder-item": {"name", "type"},
    "overridable-settings": _TEXT,
    "date-options": {"start-of-week", "fiscal-year-start"},
    "default-calendar-type": _TEXT,
    "default-sorts": _TEXT,
    "object-graph": _TEXT,
    "objects": _TEXT,
    "object": {"id", "caption"},
    "properties": _TEXT,
    "relationships": _TEXT,
    "relationship": _TEXT,
    "semantic-values": _TEXT,
    "semantic-value": {"key", "value"},
    "datasource-dependencies": {"datasource"},
    "column-instance": {"column", "name", "derivation", "pivot", "type", "forecast-column-type"},
    # worksheets
    "worksheets": _TEXT,
    "worksheet": {"name"},
    "table": _TEXT,
    "view": _TEXT,
    "view/aggregation": {"value"},  # Analysis > Aggregate Measures (off: the tile is a TODO)
    "rows": {"total"},
    "panes": _TEXT,
    "pane": {"x-axis-name", "y-axis-name"},
    "mark": {"class"},
    "encodings": _TEXT,
    "color": {"column"},
    "encodings/size": {"column"},
    "lod": {"column"},
    "text": {"column"},
    "shape": {"column"},
    "tooltip": {"column"},
    "path": {"column"},
    "wedge-size": {"column"},
    "geometry": {"column"},
    "layout-options": _TEXT,
    "title": _TEXT,
    "caption": _TEXT,
    "sort": {"column", "direction", "using"},
    "computed-sort": {"column", "direction", "using"},
    "alphabetic-sort": {"column", "direction", "using"},
    "natural-sort": {"column", "direction", "using"},
    "manual-sort": {"column", "direction", "using"},
    "dictionary": _TEXT,
    "bucket": _TEXT,
    "slices": _TEXT,
    "subtotals": _TEXT,
    "reference-line": {"axis-column", "formula", "label", "label-type", "percentage-bands", "scope", "tile-count", "value-column"},
    "reference-line-value": {"percentage"},
    "trendline": {"enabled", "fit"},
    "forecast-specification": {"enabled", "ignore-last"},
    # dashboards, stories and actions
    "dashboards": _TEXT,
    "dashboard": {"name", "type"},
    "zones": _TEXT,
    "zone": {"id", "name", "param", "type", "type-v2", "x", "y"},
    "devicelayouts": _TEXT,
    "devicelayout": {"name"},
    "story-points": _TEXT,
    "story-point": {"caption", "captured-sheet"},
    "capturedDeltas": _TEXT,
    "actions": _TEXT,
    "action": {"caption", "name"},
    "nav-action": {"caption", "name"},
    "edit-group-action": {"caption", "name"},
    "edit-parameter-action": {"caption", "name"},
    "activation": {"type"},
    "source": {"dashboard", "worksheet"},
    "exclude-sheet": {"name"},
    "command": {"command"},
    "params": _TEXT,
    "param": {"name", "value"},
    "link": {"expression"},
}.items()}

# read whole: every attribute and child counts (each reports what it can't translate itself)
WHOLE = frozenset({"table-calc", "range", "groupfilter", "first-end-point", "second-end-point"})

BY_DESIGN: dict[str, str] = {
    "style": FORMATTING, "format": FORMATTING, "style-rule": FORMATTING, "customized-tooltip": FORMATTING,
    "customized-label": FORMATTING, "tooltip-style": FORMATTING, "mark-sizing": FORMATTING, "zone-style": FORMATTING,
    "color-palette": FORMATTING, "mapsources": FORMATTING, "external": FORMATTING, "datasource/layout": FORMATTING,
    "map-navigation": FORMATTING, "label-data": FORMATTING, "mark-labels": FORMATTING, "hide-sort-controls": UI_STATE, "geo-search-visibility": FORMATTING, "default-map-tool-selection": FORMATTING,
    "size": LAYOUT, "layout-cache": LAYOUT, "device-preview": LAYOUT, "device": LAYOUT, "flipboard": LAYOUT,
    "floating-toolbar-visibility": LAYOUT, "strip": LAYOUT, "zoom": LAYOUT,
    "windows": UI_STATE, "preferences": UI_STATE, "selection-collection": UI_STATE, "highlight": UI_STATE,
    "explain-data": UI_STATE, "field-sort-info": UI_STATE, "shelf-sorts": UI_STATE,
    "document-format-change-manifest": BOOKKEEPING, "repository-location": BOOKKEEPING, "simple-id": BOOKKEEPING,
    "thumbnails": BOOKKEEPING, "refresh-event": BOOKKEEPING,
    **{f"metadata-record/{t}": COLUMN_METADATA for t in (
        "remote-type", "remote-alias", "contains-null", "precision", "scale", "width", "ordinal", "collation",
        "padded-semantics", "family", "approx-count", "attributes", "cast-to-local-type", "object-id", "layered", "hidden")},
    "relation/columns": COLUMN_METADATA,
}

# attributes an element is skipped without
REQUIRED: dict[str, str] = {
    "datasources/datasource": "name", "datasource/column": "name", "worksheet": "name", "metadata-record": "class",
    "column-instance": "column", "member": "value", "param": "name", "drill-path": "name", "folder": "name",
}

# elements whose default value changes nothing: (attribute, default)
DEFAULT: dict[str, tuple[str, str]] = {
    "view/breakdown": ("value", "auto"),  # Analysis > Stack Marks
}

# attributes that only place, size or style a read element
QUIET: dict[str, frozenset[str]] = {k: frozenset(v) for k, v in {
    "workbook": {"version", "original-version", "source-build", "source-platform", "base", "include-phone-layouts"},
    "datasource": {"version", "inline", "hasconnection", "source-platform", "base"},
    "connection": {"authentication", "username", "password", "saved-credentials-viewerid", "server-oauth", "workgroup-auth-mode",
                   "channel", "dataRefreshTime", "validate", "tablename", "default-settings", "extract-engine", "interpretationMode",
                   "access_mode", "compat", "cleaning", "tdvtconnection", "connection-type", "sslmode", "sslcert", "odbc-native-protocol",
                   "minimum-driver-version", "updated-database", "source-charset", "cloudFileRequestURL", "cloudFileProvider", "cloudFileName",
                   "cloudFileMetadata_-_folder", "cloudFileId", "cloudFileExtension"},
    "extract": {"user-specific", "object-id"},
    "column": {"alias", "layered", "default-type", "user-datatype", "pivot", "visual-totals", "datatype-customized", "ordinal", "unnamed",
               "auto-column", "auto-hidden", "SplitFieldIndex", "SplitFieldOrigin"},
    "calculation": {"decimals", "new-bin"},
    "bin": {"default-name"},
    "folder": {"layered", "role"},
    "named-connection": set(),
    "group": {"name-style", "ui-builder", "ui-vit-column", "hidden", "auto-hidden"},
    "pane": {"id", "selection-relaxation-option", "x-index", "y-index", "title", "generated-title"},
    "action": set(),
    "activation": {"auto-clear"},
    "source": {"type"},
    "story-point": {"id"},
    "zone": {"h", "w", "fixed-size", "is-fixed", "forceUpdate", "layout-strategy-id", "leg-item-layout", "mode", "paired-zone-id",
             "pane-specification-id", "removable", "show-all", "show-apply", "show-exclude", "show-filter-state", "show-morefewerbutton",
             "show-null-ctrls", "show-reldate-picker", "show-search", "show-title", "values", "custom-title", "friendly-name", "is-scaled",
             "is-centered", "alt-text"},
    "devicelayout": {"auto-generated"},
    "dashboard": {"enable-sort-zone-taborder"},
    "link": {"caption"},
    "rows": set(),
    "aliases": {"enabled"},
    "run": {"auto-url", "bold", "fontalignment", "fontcolor", "fontname", "fontsize", "hyperlink", "indent", "italic", "underline"},
    "color": {"separate-domains"},
    "lod": {"separate-domains"},
    "text": {"separate-domains"},
    "reference-line": {"id", "z-order", "enable-instant-analytics", "tooltip", "tooltip-type", "fill-above", "fill-below"},
    "layout-options": {"export-center-horz", "export-center-vert", "export-orientation"},
    "trendline": {"enable-instant-analytics", "enable-tooltips"},
}.items()}

_FCP = re.compile(r"^_\.fcp\.[^.]+\.(?:true|false)\.\.\.")
_NAMED = {"worksheet": "worksheet", "dashboard": "dashboard", "datasource": "datasource"}


def local(tag: str) -> str:
    """A tag or attribute without its namespace or Tableau's feature-flag prefix (`_.fcp.<flag>.true...relation`)."""
    return _FCP.sub("", tag.rsplit("}", 1)[-1]) if isinstance(tag, str) else ""


def _rule(parent: str, tag: str) -> tuple[str, object]:
    """("whole" | "design" | "default" | "read" | "", what): a `parent/tag` key wins over a `tag` key."""
    for key in (f"{parent}/{tag}", tag):
        if key in WHOLE:
            return "whole", None
        if key in BY_DESIGN:
            return "design", BY_DESIGN[key]
        if key in DEFAULT:
            return "default", DEFAULT[key]
        if key in READ:
            return "read", READ[key]
    return "", None


def unread(root: ET.Element, where: str) -> dict[tuple[str, str], list]:
    """(kind, why) -> [count, first place] for every element and attribute of `root` the parser doesn't read."""
    out: dict[tuple[str, str], list] = {}

    def add(kind: str, why: str, place: str) -> None:
        hit = out.setdefault((kind, why), [0, place])
        hit[0] += 1

    def visit(el: ET.Element, tag: str, parent: str, place: str) -> None:
        rule, what = _rule(parent, tag)
        if rule == "whole":
            return
        if rule == "design":
            add(f"<{tag}>", str(what), place)
            return
        if rule == "default":
            attr, default = what  # type: ignore[misc]
            if el.get(attr, default) != default:
                add(f"<{tag} {attr}='{el.get(attr)}'> in <{parent}>", NOT_READ, place)
            return
        if rule != "read":
            add(f"<{tag}> in <{parent}>", NOT_READ, place)
            return
        attrs: frozenset[str] = what  # type: ignore[assignment]
        need = REQUIRED.get(f"{parent}/{tag}") or REQUIRED.get(tag)
        if need and not el.get(need):
            add(f"<{tag}> without {need}", SKIPPED, place)
        quiet = QUIET.get(tag, frozenset())
        for a in el.attrib:
            name = local(a)
            if name not in attrs and name not in quiet and name not in ("author-locale", "locale"):
                add(f"<{tag}> attribute {name}", NOT_READ, place)
        if tag in _NAMED and el.get("name") and parent != "datasource-dependencies":
            place = f"{where}: {_NAMED[tag]} {el.get('caption') or el.get('name')}"
        for ch in el:
            if isinstance(ch.tag, str):
                visit(ch, local(ch.tag), tag, place)

    visit(root, local(root.tag), "", where)
    return out
