"""Temporary diagnosis: real authenticated HTTP seam plus lossless ASGI observation."""
import json
import logging
import re

import anyio
import pytest
from starlette.testclient import TestClient

from unshackle.mcp.http import create_app
from unshackle.mcp.result_ui import UI_URI, UI_MIME
from unshackle.mcp.ui_diagnostics import UiDiagnostics, MAX_BYTES, TAG

TOKEN = 'example-diagnostic-token-not-a-secret-12345'
HEADERS = {'Authorization': 'Bearer ' + TOKEN, 'Accept': 'application/json, text/event-stream'}


def records(caplog):
    return [json.loads(r.getMessage().split(TAG + ' ', 1)[1]) for r in caplog.records if TAG in r.getMessage()]


def rpc(method, params=None):
    return {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}}


def test_actual_http_observation_privacy_and_disabled_fallback(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv('UNSHACKLE_MCP_TOKEN', TOKEN)
    monkeypatch.setenv('UNSHACKLE_WAREHOUSE_URL', 'duckdb://')
    monkeypatch.delenv('UNSHACKLE_MCP_UI_DIAGNOSTICS', raising=False)
    caplog.set_level(logging.INFO, logger='unshackle.mcp.ui_diagnostics')
    with TestClient(create_app(tmp_path), base_url='http://localhost:8000') as client:
        plain = client.post('/mcp', headers=HEADERS, json=rpc('tools/list')).content
    assert records(caplog) == []
    monkeypatch.setenv('UNSHACKLE_MCP_UI_DIAGNOSTICS', '1')
    with TestClient(create_app(tmp_path), base_url='http://localhost:8000') as client:
        # Unauthorized requests never enter the observer.
        assert client.post('/mcp', json=rpc('tools/list')).status_code == 401
        assert records(caplog) == []
        response = client.post('/mcp', headers=HEADERS, json=rpc('initialize', {
            'protocolVersion': '2025-11-25',
            'clientInfo': {'name': 'PRIVATE_CLIENT_NAME', 'version': 'PRIVATE_VERSION'},
            'capabilities': {'extensions': {'io.modelcontextprotocol/ui': {'mimeTypes': [UI_MIME]},
                                           'PRIVATE_EXTENSION': {'value': 'PRIVATE_CAPABILITY'}}}}))
        assert response.status_code == 200
        init = records(caplog)[-1]
        assert init['method'] == 'initialize' and init['protocol_supported'] is True
        assert init['ui_capability'] is True and init['ui_mime_supported'] is True
        assert client.post('/mcp', headers=HEADERS, json=rpc('tools/list')).content == plain
        listed = records(caplog)[-1]
        assert listed['tools_list_ui_metadata'] is True and listed['response_state'] == 'parsed'
        for uri, expected in [(UI_URI, True), ('ui://PRIVATE_CALLER_URI', False)]:
            client.post('/mcp', headers=HEADERS, json=rpc('resources/read', {'uri': uri}))
            assert records(caplog)[-1]['ui_resource_read'] is expected
        result = client.post('/mcp', headers=HEADERS, json=rpc('tools/call', {
            'name': 'run_query', 'arguments': {'sql': "SELECT 'PRIVATE_SQL_AND_RESULT' AS n"}})).json()['result']
        assert result['structuredContent']['rows'] == [['PRIVATE_SQL_AND_RESULT']]
    diagnostic_text = json.dumps(records(caplog))
    assert TOKEN not in diagnostic_text and 'PRIVATE_' not in diagnostic_text
    assert UI_URI not in diagnostic_text and UI_MIME not in diagnostic_text
    assert '2025-11-25' not in diagnostic_text
    assert all(re.fullmatch('[0-9a-f]{32}', r['request_id']) for r in records(caplog))
    assert len({r['request_id'] for r in records(caplog)}) == len(records(caplog))


def observe(request, response, caplog, *, request_chunk=17, response_chunk=19):
    """A real ASGI stream; compare object identity, order, bytes and flags."""
    incoming = [{'type': 'http.request', 'body': request[i:i+request_chunk],
                 'more_body': i+request_chunk < len(request)} for i in range(0, len(request), request_chunk)]
    outgoing = [{'type': 'http.response.start', 'status': 200, 'headers': []}] + [
        {'type': 'http.response.body', 'body': response[i:i+response_chunk],
         'more_body': i+response_chunk < len(response)} for i in range(0, len(response), response_chunk)]
    received, sent = [], []
    async def app(scope, receive, send):
        for _ in incoming:
            received.append(await receive())
        for message in outgoing:
            await send(message)
    iterator = iter(incoming)
    async def receive():
        return next(iterator)
    async def send(message):
        sent.append(message)
    anyio.run(UiDiagnostics(app), {'type': 'http', 'method': 'POST'}, receive, send)
    assert all(a is b for a,b in zip(received,incoming)) and len(received) == len(incoming)
    assert all(a is b for a,b in zip(sent,outgoing)) and len(sent) == len(outgoing)
    return records(caplog)[-1]


@pytest.mark.parametrize('extensions,expected,mime', [({},False,False),
    ({'io.modelcontextprotocol/ui': {}},True,False),
    ({'io.modelcontextprotocol/ui': {'mimeTypes':[UI_MIME]}},True,True)])
def test_initialize_absence_vs_support(extensions, expected, mime, caplog):
    caplog.set_level(logging.INFO, logger='unshackle.mcp.ui_diagnostics')
    request=json.dumps(rpc('initialize', {'protocolVersion':'2026-01-26', 'capabilities':{'extensions':extensions}})).encode()
    record=observe(request,b'{}',caplog)
    assert record['ui_capability'] is expected and record['ui_mime_supported'] is mime
    assert record['protocol_supported'] is False


@pytest.mark.parametrize('sse',[False,True])
def test_tools_metadata_across_arbitrary_response_chunks(sse, caplog):
    caplog.set_level(logging.INFO, logger='unshackle.mcp.ui_diagnostics')
    response=json.dumps({'jsonrpc':'2.0','id':1,'result':{'tools':[{'name':'run_query','_meta':{
        'ui':{'resourceUri':UI_URI},'ui/resourceUri':UI_URI}}]}}).encode()
    if sse:
        response=b': keepalive\r\n\r\nevent: message\r\ndata: '+response+b'\r\n\r\n'
    record=observe(json.dumps(rpc('tools/list')).encode(),response,caplog,response_chunk=1)
    assert record['tools_list_ui_metadata'] is True and record['response_state']=='parsed'


def test_unknown_overflow_and_absence_are_distinct(caplog):
    caplog.set_level(logging.INFO, logger='unshackle.mcp.ui_diagnostics')
    request=json.dumps(rpc('tools/list')).encode()
    missing=observe(request,b'{"result":{"tools":[]}}',caplog)
    assert missing['tools_list_ui_metadata'] is False
    invalid=observe(request,b'PRIVATE_BAD_RESPONSE',caplog)
    assert invalid['tools_list_ui_metadata'] is None and invalid['response_state']=='invalid'
    large=observe(request,b' '*(MAX_BYTES+1),caplog,response_chunk=1000)
    assert large['tools_list_ui_metadata'] is None and large['response_state']=='oversize'
    large_request=observe(b' '*(MAX_BYTES+1),b'{}',caplog,request_chunk=1000)
    assert large_request['method']=='unknown' and large_request['request_state']=='oversize'
    assert large_request['ui_capability'] is None
    assert 'PRIVATE_' not in json.dumps(records(caplog))


def test_diagnostic_exception_cannot_change_response(monkeypatch,caplog):
    caplog.set_level(logging.INFO, logger='unshackle.mcp.ui_diagnostics')
    monkeypatch.setattr('unshackle.mcp.ui_diagnostics.logger.info',lambda *args,**kwargs: (_ for _ in ()).throw(RuntimeError('PRIVATE_LOG_FAILURE')))
    async def app(scope,receive,send):
        assert (await receive())['body']==b'not-json'
        await send({'type':'http.response.body','body':b'unchanged'})
    async def receive(): return {'type':'http.request','body':b'not-json'}
    sent=[]
    async def send(message): sent.append(message)
    anyio.run(UiDiagnostics(app),{'type':'http','method':'POST'},receive,send)
    assert sent==[{'type':'http.response.body','body':b'unchanged'}]
    assert records(caplog)==[]
