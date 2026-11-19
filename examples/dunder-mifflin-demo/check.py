#!/usr/bin/env python3
"""Authenticated HTTPS acceptance using the real MCP SDK; no secrets in output."""
from __future__ import annotations
import asyncio
from decimal import Decimal, InvalidOperation
import json
import os
import sys
from pathlib import Path
import time
from urllib.parse import urlsplit
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

HERE=Path(__file__).resolve().parent


def normalize(rows):
    def value(v):
        try: return Decimal(str(v))
        except InvalidOperation: return v
    return [[value(v) for v in row] for row in rows]


async def check(endpoint,token,deadline=15):
    url=urlsplit(endpoint)
    assert url.scheme=='https' and url.path=='/mcp' and not (url.username or url.password or url.query or url.fragment)
    async with httpx.AsyncClient(timeout=40) as http:
        for headers in ({},{'Authorization':'Bearer deliberately-invalid'}):
            assert (await http.post(endpoint,headers=headers,json={})).status_code==401
    async with httpx.AsyncClient(headers={'Authorization':'Bearer '+token},timeout=40) as http:
        async with streamable_http_client(endpoint,http_client=http) as (read,write,_):
            async with ClientSession(read,write) as session:
                init=await session.initialize()
                assert {t.name for t in (await session.list_tools()).tools}=={'overview','search','read','run_query'}
                resources=(await session.list_resources()).resources
                assert resources
                assert (await session.read_resource(resources[0].uri)).contents
                async def call(name,args=None): return await session.call_tool(name,args or {})
                overview=(await call('overview')).structuredContent
                assert overview['warehouse']['configured']
                search=await call('search',{'query':'recognized'})
                assert not search.isError
                context=await call('read',{'path':'knowledge/team-demo.md'})
                assert not context.isError
                assert (await call('read',{'path':'.env'})).isError
                identity=await call('run_query',{'sql':"SELECT current_user,current_setting('transaction_read_only')"})
                assert identity.structuredContent['rows']==[['example_reader','on']]
                answers={}
                for q in json.loads((HERE/'questions.json').read_text()):
                    result=await call('run_query',{'sql':q['sql']})
                    assert not result.isError,q['id']
                    rows=result.structuredContent['rows']
                    assert normalize(rows)==normalize(q['expected']),q['id']
                    answers[q['id']]=rows
                capped=await call('run_query',{'sql':'SELECT * FROM history_orders','limit':1000})
                assert capped.structuredContent['row_count']==1000 and capped.structuredContent['truncated']
                assert (await call('run_query',{'sql':'SELECT * FROM history_orders','limit':1001})).isError
                assert (await call('run_query',{'sql':'DELETE FROM orders'})).isError
                start=time.monotonic()
                assert (await call('run_query',{'sql':f'SELECT pg_sleep({deadline+5})'})).isError
                elapsed=time.monotonic()-start
                assert deadline*.8 <= elapsed < deadline+5
                assert (await call('run_query',{'sql':'SELECT 1'})).structuredContent['rows']==[[1]]
                active=await call('run_query',{'sql':"SELECT count(*) FROM pg_stat_activity WHERE usename=current_user AND pid<>pg_backend_pid() AND state='active' AND query LIKE '%pg_sleep%'"})
                assert active.structuredContent['rows']==[[0]]
                return {'protocol':init.protocolVersion,'questions':answers,'deadline_seconds':round(elapsed,2),'auth_401':True,'tools_resources_context':True,'cap':1000,'native_cancel_recovery':True,'lingering_queries':0}

if __name__=='__main__':
    try:
        result=asyncio.run(check(os.environ['UNSHACKLE_MCP_URL'],os.environ['UNSHACKLE_MCP_TOKEN']))
    except Exception:
        print('Hosted check failed; inspect endpoint, credentials and deployment privately.',file=sys.stderr)
        sys.exit(1)
    print(json.dumps(result,indent=2))
