import asyncio
import socket

import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from academic_mcp.ingest import ingest
from academic_mcp.server import create_server
from test_service import SAMPLES


def test_streamable_http_roundtrip(tmp_path, monkeypatch):
    db = str(tmp_path / 'http.sqlite3')
    ingest(db, SAMPLES)
    monkeypatch.setenv('ACADEMIC_DB', db)
    monkeypatch.setenv('MODEL_MODE', 'local')

    async def run():
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(create_server(port=port).streamable_http_app(), log_level='error'))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            for _ in range(200):
                if server.started:
                    break
                if task.done():
                    await task
                await asyncio.sleep(0.01)
            assert server.started
            async with streamable_http_client(f'http://127.0.0.1:{port}/mcp') as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool('get_academic_calendar', {'semester': '2026-2', 'keyword': '이중전공'})
                    assert not result.isError
                    assert result.structuredContent['events'][0]['end'] == '2026-11-13'
                    # Independent protocol requests share public evidence without
                    # sharing request IDs. Exercise cold and warmed bursts.
                    for warmed in (False, True):
                        responses = await asyncio.gather(*[
                            session.call_tool('search_academic_info', {'query': '이중전공 신청자격은?'})
                            for _ in range(20)])
                        assert all(not r.isError for r in responses)
                        payloads = [r.structuredContent for r in responses]
                        assert len({r['request_id'] for r in payloads}) == 20
                        assert all(r['status'] == 'found' for r in payloads)
                        if warmed:
                            assert all(r['metrics']['cache_fast_path'] for r in payloads)
                            assert all(r['metrics']['model_calls'] == 0 for r in payloads)
        finally:
            server.should_exit = True
            await task
            sock.close()

    asyncio.run(asyncio.wait_for(run(), timeout=20))
