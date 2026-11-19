import json

import anyio
import httpx
import pytest
from starlette.testclient import TestClient

from unshackle.mcp.http import create_app
from unshackle.mcp.server import create_server

TOKEN = 'example-synthetic-token-not-a-secret-123456'
HEADERS = {'Authorization': 'Bearer ' + TOKEN, 'Accept': 'application/json, text/event-stream',
           'MCP-Protocol-Version': '2025-11-25'}


def rpc(method, params=None, id=1):
    return {'jsonrpc': '2.0', 'id': id, 'method': method, **({'params': params} if params is not None else {})}


def test_auth_required_for_every_http_request(tmp_path, monkeypatch):
    monkeypatch.setenv('UNSHACKLE_MCP_TOKEN', TOKEN)
    app = create_app(tmp_path)
    with TestClient(app, base_url='http://localhost:8000') as client:
        for path in ('/mcp', '/mcp/', '/anything'):
            assert client.get(path).status_code == 401
            assert client.post(path, json=rpc('tools/list'), headers={'Authorization': 'Bearer wrong'}).status_code == 401
        assert client.post('/mcp', headers=[('Authorization', 'Bearer ' + TOKEN), ('Authorization', 'Bearer wrong')], json=rpc('tools/list')).status_code == 401
        result = client.post('/mcp', json=rpc('tools/list'), headers=HEADERS)
        assert result.status_code == 200
        tools = {t['name']: t for t in result.json()['result']['tools']}
        assert set(tools) == {'overview', 'search', 'read', 'run_query'}
        for name in ('overview', 'search', 'read'):
            assert tools[name]['annotations']['readOnlyHint'] is True
            assert tools[name]['annotations']['destructiveHint'] is False
        assert not (tools['run_query'].get('annotations') or {}).get('readOnlyHint', False)


def test_missing_or_weak_token_refuses_startup(tmp_path, monkeypatch):
    for token in ('', 'short', 'x' * 32 + '\n'):
        monkeypatch.setenv('UNSHACKLE_MCP_TOKEN', token)
        with pytest.raises(ValueError, match='UNSHACKLE_MCP_TOKEN'):
            create_app(tmp_path)


def test_http_tool_and_resource_parity_across_refresh(tmp_path, monkeypatch):
    monkeypatch.setenv('UNSHACKLE_MCP_TOKEN', TOKEN)
    monkeypatch.setenv('UNSHACKLE_WAREHOUSE_URL', 'duckdb://')
    first = tmp_path / 'first'
    second = tmp_path / 'second'
    for root, content in ((first, 'first TODO(unshackle): check'), (second, 'second')):
        (root / 'knowledge').mkdir(parents=True)
        (root / 'knowledge/note.md').write_text(content)
    current = [first]
    app = create_app(lambda: current[0])
    with TestClient(app, base_url='http://localhost:8000') as client:
        def call(name, args):
            response = client.post('/mcp', json=rpc('tools/call', {'name': name, 'arguments': args}), headers=HEADERS)
            assert response.status_code == 200
            return response.json()['result']
        before = call('read', {'path': 'knowledge/note.md'})['structuredContent']
        server = create_server(first)
        async def local_read():
            return await server.call_tool('read', {'path': 'knowledge/note.md'})
        local = anyio.run(local_read)
        assert before == local[1]
        assert call('run_query', {'sql': 'select * from range(4)', 'limit': 2})['structuredContent']['truncated']
        current[0] = second
        assert call('read', {'path': 'knowledge/note.md'})['structuredContent']['content'] == 'second'
        resource = client.post('/mcp', headers=HEADERS, json=rpc('resources/read', {'uri': 'unshackle://repo/knowledge/note.md'})).json()['result']
        assert json.loads(resource['contents'][0]['text'])['content'] == 'second'
        assert call('read', {'path': '.env'})['isError']
