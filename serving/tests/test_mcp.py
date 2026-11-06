import asyncio
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from academic_mcp.ingest import ingest
from test_service import SAMPLES


def test_real_stdio_protocol(tmp_path):
    db = str(tmp_path / 'protocol.sqlite3')
    ingest(db, SAMPLES)

    async def run():
        params = StdioServerParameters(command=sys.executable, args=['-m', 'academic_mcp.server'],
                                       env={**os.environ, 'ACADEMIC_DB': db, 'MODEL_MODE': 'local'})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert {t.name for t in tools.tools} == {'get_academic_calendar', 'search_academic_info'}
                result = await session.call_tool('get_academic_calendar', {'semester': '2026-2', 'keyword': '이중전공'})
                assert not result.isError
                assert result.structuredContent['events'][0]['start'] == '2026-11-11'
                prompts = await session.list_prompts()
                assert prompts.prompts[0].name == 'grounded_answer'
                result = await session.call_tool('search_academic_info', {'query': '이중전공 합격컷'})
                assert result.structuredContent['status'] == 'insufficient_evidence'
                invalid = await session.call_tool('search_academic_info', {'query': '이중전공', 'top_k': 99})
                assert invalid.isError

    asyncio.run(asyncio.wait_for(run(), timeout=30))
