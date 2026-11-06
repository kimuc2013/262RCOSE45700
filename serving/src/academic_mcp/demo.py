"""Loopback-only development UI that calls the actual MCP HTTP endpoint."""
import argparse
import asyncio
import json
from pathlib import Path

import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route


def create_demo(mcp_url='http://127.0.0.1:8000/mcp'):
    async def home(request):
        return HTMLResponse(Path(__file__).with_name('demo.html').read_text(encoding='utf-8'))

    async def call(request: Request):
        # A fixed server-side destination, never an arbitrary browser-supplied URL.
        if request.headers.get('origin') and request.headers['origin'] != f"{request.url.scheme}://{request.url.netloc}":
            return JSONResponse({'error': '다른 출처에서 호출할 수 없습니다.'}, status_code=403)
        if len(body := await request.body()) > 10000:
            return JSONResponse({'error': '입력이 너무 큽니다.'}, status_code=413)
        try:
            data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError('잘못된 요청입니다.')
            name, arguments, count = data.get('tool'), data.get('arguments'), data.get('count', 1)
            if name not in ('search_academic_info', 'get_academic_calendar') or not isinstance(arguments, dict):
                raise ValueError('지원하지 않는 도구입니다.')
            if type(count) is not int or count not in (1, 10):
                raise ValueError('요청 수는 1 또는 10이어야 합니다.')
            async with asyncio.timeout(30):
                async with streamable_http_client(mcp_url) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        async def one():
                            result = await session.call_tool(name, arguments)
                            return {'isError': result.isError, 'data': result.structuredContent,
                                    'message': '\n'.join(c.text for c in result.content if c.type == 'text') if result.isError else ''}
                        results = await asyncio.gather(*(one() for _ in range(count)))
            return JSONResponse({'tool': name, 'results': results})
        except (ValueError, TypeError) as exc:
            return JSONResponse({'error': str(exc)}, status_code=400)
        except Exception:
            return JSONResponse({'error': '로컬 MCP 서버에 연결하지 못했습니다. 8000 포트의 서버 상태를 확인하세요.'}, status_code=502)

    return Starlette(routes=[Route('/', home), Route('/api/call', call, methods=['POST'])])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8001)
    parser.add_argument('--mcp-url', default='http://127.0.0.1:8000/mcp')
    args = parser.parse_args()
    uvicorn.run(create_demo(args.mcp_url), host='127.0.0.1', port=args.port)


if __name__ == '__main__':
    main()
