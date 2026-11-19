# Interactive query results

`run_query` links to `ui://unshackle/query-results/v1.html`, a bundled MCP App.
An Apps-capable host sends the actual tool result to the view after its handshake.
Other clients, including Claude Code, keep the same JSON/text response:
`{columns, rows, row_count, truncated}`. No query, authentication or execution-limit
behavior changes. The retained team demo keeps its 1,000-row / 15-second limits.

The table supports keyboard sorting (Tab to a column header, Enter or Space),
search across all columns, a returned-row count, truncation notice, and filtered CSV.
Search, sorting and CSV operate on returned rows only. The widget processes at most
the first 1,000 returned rows and explicitly labels this when another server permits
more. There is no pagination, additional query, or full-warehouse export.
Duplicate column names are addressed by position. NULL sorts last in either direction
and displays as `NULL`. Empty results retain their headers.

Values display exactly as received; no guessed currency, percentages or units.
Columns containing only canonical numeric values/strings sort numerically, using
string comparison of decimal components so large integers and high-precision decimals
are not rounded. Leading-zero identifiers stay text. ISO date/datetime columns sort
chronologically; mixed or other strings sort as text. Values are not reformatted.
Host theme and style variables update live, with local system-font fallbacks.

## CSV scope and safety

“Download filtered CSV” exports the filtered rows in their current sort order, including
headers. It appears only when `app.getHostCapabilities()?.downloadFile` is present after
connection. The app sends an embedded `text/csv` resource through `app.downloadFile`,
with one request pending at a time. Cancellation/refusal (`isError`) and request errors
show copyable CSV. A timeout says the download could not be confirmed: the host may
still have an active dialog. There are no blob/data URLs or anchor downloads.
“Show copyable CSV” is always available when valid results exist.

Every field is quoted, embedded quotes are doubled, and records use CRLF. Embedded
newlines remain in their fields. NULL exports as an empty field, indistinguishable
from an empty string in CSV. Before quoting, strings beginning with `=`, `+`, `-` or
`@`, including after whitespace, Unicode format characters or control characters,
receive an apostrophe prefix to mitigate spreadsheet formulas. This also protects
untrusted headers. Typed numeric negatives remain unchanged; negative *strings*,
including Decimal strings, receive that safety prefix. Displayed values remain exact;
exported text otherwise preserves the original characters. Spreadsheet importers may
interpret types differently; choose text import to retain precision and identifiers.

## Host support and wide results

Rendering and downloads depend on the connected host's capabilities; no subscription
plan is assumed. After updating a server, refresh/reconnect the connector and make a
new tool call in a new conversation if cached tool metadata still omits the UI.
Existing result cards may keep their cached resource. Allow the app if Claude prompts.
Claude Code intentionally remains a JSON/text client.

Claude may offload results above approximately 150,000 characters to a file when its
code execution sandbox is active. The UI can then receive a file pointer instead of
rows. Even fewer than 1,000 wide rows can reach this limit. Missing/malformed data clears
the table and suggests fewer selected columns or a smaller SQL LIMIT. This version does
not fetch offloaded files or promise that every 1,000-row payload renders.

No `ui.domain` is set: this widget has no OAuth, cookies, storage or origin-bound API.
Claude's stable domain is optional; omitting it also supports local stdio connectors.
The resource declares empty network/frame allowlists. Its own CSP allows only the
hashed bundled script and inline styles; external assets, network requests, forms,
frames, objects and base URLs are blocked. All cells and headers use `textContent`;
no result enters HTML or executable code. The widget receives no bearer token.
Only the exact registered `ui://` URI bypasses repository resource routing; unknown
URIs and unsafe repository paths remain rejected.

## Build and verify

The Python wheel includes the prebuilt `result-table.html`; Node is not required by
the running server. To change the widget:

```bash
cd src/unshackle/mcp/result_table
npm ci
npm run build
npx playwright install chromium
npm test
```

`CHROMIUM_PATH` can select an already installed Chromium executable. The browser tests
run the bundled SDK and CSP in an iframe with a JSON-RPC host mock, including initial
result delivery, safe text, exact numeric/date sorting, duplicate columns, NULL,
keyboard navigation, search, theme updates, CSV quoting/injection, download capability
absence/success/cancellation/errors/pending state, empty/malformed results and row caps.
They are not proof of actual Claude rendering or download acceptance. Test those on the
real connected web/desktop host after deployment.

Python protocol checks: `pytest tests/test_mcp_result_ui.py tests/test_mcp_stdio.py
 tests/test_mcp_http.py tests/test_mcp_query.py`. Before pushing, run editable dev install,
full pytest and `./examples/tableau-demo/run.sh`. Build a wheel, install it separately,
and read the UI resource from that installed package to verify packaging.

Client dependencies are pinned by `package-lock.json`; license notices ship alongside
the bundle. No Python MCP SDK upgrade is required.

Official references (checked November 19, 2026):

- [MCP Apps lifecycle and resource linkage](https://apps.extensions.modelcontextprotocol.io/api/documents/Overview.html)
- [Claude MCP Apps](https://claude.com/docs/connectors/building/mcp-apps/getting-started)
- [Downloads and cancellation semantics](https://claude.com/docs/connectors/building/mcp-apps/download-files)
- [Host theming](https://claude.com/docs/connectors/building/mcp-apps/transparent-theming)
- [Large results and optional stable domains](https://claude.com/docs/connectors/building/mcp-apps/troubleshooting)
