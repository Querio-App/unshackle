import os
import sys

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def test_stdio_protocol_tools_resources_and_query(tmp_path):
    (tmp_path / 'knowledge').mkdir()
    (tmp_path / 'knowledge/note.md').write_text('Revenue TODO(unshackle): reconcile')
    async def check():
        params = StdioServerParameters(command=sys.executable, args=['-c', 'from unshackle.cli import main; main()', 'mcp', '--repo', str(tmp_path)],
                                      env={**os.environ, 'UNSHACKLE_WAREHOUSE_URL': 'duckdb://'})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                # Both discovery surfaces must reach a real stdio client; check protocol
                # concepts, not exact editorial wording. Visibility remains host-dependent.
                assert all(term in initialized.instructions for term in ('run_query', 'MCP App', 'JSON', 'Claude Code'))
                tools = (await session.list_tools()).tools
                query_tool = next(t for t in tools if t.name == 'run_query')
                assert all(term in query_tool.description for term in ('MCP App', 'JSON', 'text-only'))
                assert query_tool.meta['ui']['resourceUri'].startswith('ui://')
                assert {t.name for t in tools} == {'overview', 'search', 'read', 'run_query'}
                resources = (await session.list_resources()).resources
                assert len(resources) == 2
                assert 'Revenue' in (await session.read_resource(resources[0].uri)).contents[0].text
                result = await session.call_tool('read', {'path': 'knowledge/note.md'})
                assert result.structuredContent['has_todo']
                result = await session.call_tool('run_query', {'sql': 'select 42 as answer', 'limit': 1})
                assert result.structuredContent['rows'] == [[42]]
                assert (await session.call_tool('run_query', {'sql': 'delete from x'})).isError
    anyio.run(check)
