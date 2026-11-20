"""Display the MCP server's completed answer without another generation step."""
import argparse
import asyncio
import json
import os
import time

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def run(url,query,as_json=False):
    token=os.getenv('MCP_AUTH_TOKEN') or os.getenv('MCP_TEAM_TOKEN')
    async with httpx.AsyncClient(headers={'Authorization':'Bearer '+token} if token else {},timeout=5) as client:
        async with streamable_http_client(url,http_client=client) as (read,write,_):
            async with ClientSession(read,write) as session:
                await session.initialize()
                start=time.perf_counter()
                result=await session.call_tool('answer_academic_question',{'query':query})
                elapsed=(time.perf_counter()-start)*1000
                if result.isError:
                    raise RuntimeError('Completed-answer tool failed; check server configuration/logs')
                data=result.structuredContent
                if as_json:print(json.dumps({'response':data,'after_session_ready_elapsed_ms':round(elapsed,3)},ensure_ascii=False))
                elif data.get('answer') is not None:
                    print(data['answer'])
                    for url in data.get('citations',[]):print(url)
                    print(f'완성 답변 수신: {elapsed:.0f}ms (MCP 초기 연결 이후)')
                else:print('시간 안에 답변을 완성하지 못했습니다.')
                return 0 if data.get('display_verbatim') else 2


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('query')
    parser.add_argument('--url',default=os.getenv('MCP_URL','http://127.0.0.1:8000/mcp'))
    parser.add_argument('--json',action='store_true')
    args=parser.parse_args()
    raise SystemExit(asyncio.run(run(args.url,args.query,args.json)))

if __name__=='__main__':main()
