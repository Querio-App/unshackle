"""Temporary opt-in, fixed-schema MCP App boundary observation. Remove after diagnosis."""
from __future__ import annotations

import datetime
import json
import logging
import re
import uuid

from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS

from .result_ui import UI_MIME, UI_URI

TAG = '[DEBUG-219-ui]'
MAX_BYTES = 64 * 1024
logger = logging.getLogger(__name__)


def _object(data):
    return data if isinstance(data, dict) else {}


def _decode(payload):
    """JSON response or complete bounded SSE events, independent of chunk boundaries."""
    try:
        return json.loads(payload)
    except (ValueError, UnicodeError):
        text = payload.decode('utf-8').replace('\r\n', '\n')
        for event in text.split('\n\n'):
            data = '\n'.join(line[5:].lstrip(' ') for line in event.split('\n') if line.startswith('data:'))
            if data:
                envelope = json.loads(data)
                if isinstance(envelope, dict) and 'result' in envelope:
                    return envelope
        raise ValueError('No response envelope')


class UiDiagnostics:
    """Observes after authentication; never logs caller strings or changes messages."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope.get('method') != 'POST':
            return await self.app(scope, receive, send)
        try:
            record = {'request_id': uuid.uuid4().hex, 'method': 'unknown',
                      'request_state': 'unobserved', 'response_state': 'unobserved',
                      'protocol_supported': None, 'ui_capability': None, 'ui_mime_supported': None,
                      'tools_list_ui_metadata': None, 'ui_resource_read': None}
            request_body, response_body = bytearray(), bytearray()
            request_bytes = response_bytes = 0
        except Exception:
            return await self.app(scope, receive, send)

        def request_complete():
            obj = _object(json.loads(request_body))
            method = obj.get('method')
            record['method'] = {'initialize': 'initialize', 'tools/list': 'tools_list',
                                'resources/read': 'resources_read', 'tools/call': 'tools_call',
                                'resources/list': 'resources_list'}.get(method, 'other')
            params = _object(obj.get('params'))
            if method == 'initialize':
                version = params.get('protocolVersion')
                if isinstance(version, str) and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', version):
                    try:
                        datetime.date.fromisoformat(version)
                        record['protocol_supported'] = version in SUPPORTED_PROTOCOL_VERSIONS
                    except ValueError:
                        pass
                extensions = _object(_object(params.get('capabilities')).get('extensions'))
                record['ui_capability'] = 'io.modelcontextprotocol/ui' in extensions
                ui = _object(extensions.get('io.modelcontextprotocol/ui'))
                mime_types = ui.get('mimeTypes')
                record['ui_mime_supported'] = isinstance(mime_types, list) and UI_MIME in mime_types
            elif method == 'resources/read':
                record['ui_resource_read'] = params.get('uri') == UI_URI
            record['request_state'] = 'parsed'

        async def observed_receive():
            nonlocal request_bytes
            message = await receive()
            try:
                if message['type'] == 'http.request':
                    body = message.get('body', b'')
                    request_bytes += len(body)
                    if request_bytes > MAX_BYTES:
                        request_body.clear()
                        record['request_state'] = 'oversize'
                    else:
                        request_body.extend(body)
                        if not message.get('more_body', False):
                            try:
                                request_complete()
                            finally:
                                request_body.clear()
            except Exception:
                request_body.clear()
                record['request_state'] = 'invalid'
            return message

        async def observed_send(message):
            nonlocal response_bytes
            # Forward first, including SSE chunks; diagnostics never delay stream completion.
            await send(message)
            try:
                if record['method'] != 'tools_list':
                    record['response_state'] = 'not_applicable'
                elif message['type'] == 'http.response.body':
                    body = message.get('body', b'')
                    response_bytes += len(body)
                    if response_bytes > MAX_BYTES:
                        response_body.clear()
                        record['response_state'] = 'oversize'
                    else:
                        response_body.extend(body)
                        if not message.get('more_body', False):
                            envelope = _object(_decode(response_body))
                            tools = _object(envelope.get('result')).get('tools')
                            if not isinstance(tools, list):
                                raise ValueError('No tools list')
                            record['tools_list_ui_metadata'] = any(
                                _object(tool).get('name') == 'run_query'
                                and _object(_object(tool).get('_meta')).get('ui/resourceUri') == UI_URI
                                and _object(_object(_object(tool).get('_meta')).get('ui')).get('resourceUri') == UI_URI
                                for tool in tools)
                            record['response_state'] = 'parsed'
                            response_body.clear()
            except Exception:
                response_body.clear()
                record['response_state'] = 'invalid'

        try:
            await self.app(scope, observed_receive, observed_send)
        finally:
            try:
                logger.info('%s %s', TAG, json.dumps(record, separators=(',', ':')))
            except Exception:
                pass  # Observation failure must not alter an authenticated MCP request.
