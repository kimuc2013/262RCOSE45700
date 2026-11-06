import asyncio
import threading

import pytest

from mcp.server.fastmcp.exceptions import ToolError
from academic_mcp.runtime import Limits
from academic_mcp.service import AcademicService
from academic_mcp import server as server_module
from test_service import service  # fixture
from test_runtime import until


def test_mcp_returns_explicit_overload_and_deadline_errors(service, monkeypatch):
    started, release = threading.Event(), threading.Event()
    online = AcademicService(service.store, limits=Limits(max_running=1, max_queued=0, request_timeout=0.05))
    original = online.search
    def slow(*args, **kwargs):
        started.set()
        assert release.wait(2)
        return original(*args, **kwargs)
    monkeypatch.setattr(online, 'search', slow)
    monkeypatch.setattr(server_module, 'build_service', lambda: online)
    mcp = server_module.create_server()

    async def run():
        one = asyncio.create_task(mcp.call_tool('search_academic_info', {'query': '이중전공 자격'}))
        await until(started.is_set)
        try:
            with pytest.raises(ToolError, match='SERVER_BUSY'):
                await mcp.call_tool('search_academic_info', {'query': '이중전공 정의'})
            with pytest.raises(ToolError, match='REQUEST_DEADLINE_EXCEEDED'):
                await one
        finally:
            release.set()
            await until(lambda: online.requests._admitted == 0)
    asyncio.run(run())
