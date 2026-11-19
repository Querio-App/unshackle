"""Exercise the actual MCP wire representation and exact resource routing."""
import json

import anyio
from starlette.testclient import TestClient

from unshackle.mcp import result_ui
from unshackle.mcp.server import create_server


def test_result_ui_protocol_and_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv('UNSHACKLE_WAREHOUSE_URL', 'duckdb://')
    server = create_server(tmp_path, max_rows=1000, timeout=15, stateless_http=True, json_response=True)
    headers = {'Accept': 'application/json, text/event-stream', 'Content-Type': 'application/json'}
    with TestClient(server.streamable_http_app(), base_url="http://localhost:8000") as client:
        def rpc(method, params=None):
            return client.post('/mcp', headers=headers, json={
                'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}}).json()
        tool = next(t for t in rpc('tools/list')['result']['tools'] if t['name'] == 'run_query')
        assert tool['_meta']['ui']['resourceUri'] == result_ui.UI_URI
        resource = rpc('resources/list')['result']['resources'][0]
        assert resource['uri'] == result_ui.UI_URI
        content = rpc('resources/read', {'uri': result_ui.UI_URI})['result']['contents'][0]
        assert content['mimeType'] == result_ui.UI_MIME
        assert content['text'].startswith('<!doctype html>')
        assert 'domain' not in content['_meta']['ui']
        assert content['_meta']['ui']['csp']['connectDomains'] == []
        for uri in [result_ui.UI_URI + '?x', result_ui.UI_URI + '#x', 'ui://unshackle/other',
                    'ui://unshackle/query-results/../v1.html', 'file:///etc/passwd',
                    'unshackle://repo/../../secret', 'unshackle://other/knowledge/a.md']:
            assert 'error' in rpc('resources/read', {'uri': uri})
        result = rpc('tools/call', {'name': 'run_query', 'arguments': {'sql': 'select 42 as answer'}})['result']
        expected = {'columns': ['answer'], 'rows': [[42]], 'row_count': 1, 'truncated': False}
        assert result['structuredContent'] == expected
        assert json.loads(result['content'][0]['text']) == expected
        assert rpc('tools/call', {'name': 'run_query', 'arguments': {'sql': 'select 1', 'limit': 1001}})['result']['isError']


def test_ui_preserves_execution_limits(tmp_path, monkeypatch):
    calls = []
    def execute(sql, limit, **kwargs):
        calls.append((sql, limit, kwargs))
        return {'columns': [], 'rows': [], 'row_count': 0, 'truncated': False}
    monkeypatch.setattr('unshackle.mcp.server.execute', execute)
    async def check():
        await create_server(tmp_path, max_rows=1000, timeout=15).call_tool('run_query', {'sql': 'select 1'})
    anyio.run(check)
    assert calls == [('select 1', 1000, {'max_rows': 1000, 'timeout': 15})]


def test_bigint_protocol_and_text_fallback_preserve_precision_with_null(tmp_path, monkeypatch):
    monkeypatch.setenv('UNSHACKLE_WAREHOUSE_URL', 'duckdb://')
    server = create_server(tmp_path, stateless_http=True, json_response=True)
    values = [-(2**53 + 1), -(2**53), -(2**53 - 1), 2**53 - 1, 2**53, 2**53 + 1, None]
    sql = ' UNION ALL '.join('SELECT CAST(' + ('NULL' if n is None else str(n)) + ' AS BIGINT) AS n' for n in values)
    with TestClient(server.streamable_http_app(), base_url='http://localhost:8000') as client:
        response = client.post('/mcp', headers={'Accept': 'application/json, text/event-stream'}, json={
            'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': 'run_query', 'arguments': {'sql': sql}}})
        result = response.json()['result']
    rows = [[str(n) if n is not None and abs(n) > 2**53 - 1 else n] for n in values]
    assert result['structuredContent']['rows'] == rows
    assert json.loads(result['content'][0]['text'])['rows'] == rows
    assert type(result['structuredContent']['rows'][2][0]) is int
    assert type(result['structuredContent']['rows'][3][0]) is int
