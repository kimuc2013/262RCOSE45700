"""Read-only MCP health check against existing data; never seeds a database."""
import argparse
import asyncio
import json

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def check(url, *, search=False):
    async with asyncio.timeout(15):
        async with streamable_http_client(url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = {t.name for t in (await session.list_tools()).tools}
                if not {'get_academic_calendar', 'search_academic_info'} <= tools:
                    raise RuntimeError('Required MCP tools missing')
                result = await session.call_tool('get_academic_calendar', {'semester': '2026-2'})
                if result.isError or not result.structuredContent:
                    raise RuntimeError('Calendar MCP call failed')
                data = result.structuredContent
                if data.get('revision') in (None, 'empty'):
                    raise RuntimeError('No data loaded')
                if search:
                    result = await session.call_tool('search_academic_info', {'query': '이중전공 신청자격은?'})
                    if result.isError or not result.structuredContent:
                        raise RuntimeError('Search MCP call failed')
                    searched = result.structuredContent
                    if searched.get('revision') != data['revision'] or not searched.get('evidence'):
                        raise RuntimeError('Search data/index health check failed')
                print(json.dumps({'ok': True, 'revision': data['revision'], 'status': data['status']}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:8000/mcp')
    parser.add_argument("--search", action="store_true")
    args = parser.parse_args()
    asyncio.run(check(args.url, search=args.search))


if __name__ == '__main__':
    main()
