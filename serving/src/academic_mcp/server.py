import argparse
import logging
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .service import INSTRUCTIONS, build_service
from .runtime import ServingError
from mcp.server.fastmcp.exceptions import ToolError


def create_server(host='127.0.0.1', port=8000):
    service = build_service()
    mcp = FastMCP("KU Academic Evidence", host=host, port=port,
                  stateless_http=True, json_response=True)
    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

    @mcp.tool(annotations=read_only, structured_output=True)
    async def get_academic_calendar(semester: str, keyword: str = "") -> dict[str, Any]:
        """학기(YYYY-1 또는 YYYY-2)와 행사명으로 수집된 공식 일정을 조회합니다. 날짜를 추정하지 않습니다."""
        try:
            return await service.acalendar(semester, keyword)
        except ServingError as exc:
            raise ToolError(f'{exc.code}: {exc}') from exc

    @mcp.tool(annotations=read_only, structured_output=True)
    async def search_academic_info(query: str, history: list[dict[str, str]] | None = None,
                             top_k: int = 5, semester: str | None = None, target: str | None = None) -> dict[str, Any]:
        """공개 학부 학사자료의 근거 후보와 URL을 반환합니다. status와 최신성 경고를 확인하세요.
        history는 이전 user/assistant 메시지입니다. 로컬 모드는 BM25 기준선이며 후속 질의 재작성을 지원하지 않습니다.
        """
        try:
            return await service.asearch(query, history, top_k, semester, target)
        except ServingError as exc:
            raise ToolError(f'{exc.code}: {exc}') from exc

    @mcp.prompt()
    def grounded_answer() -> str:
        """공식 근거에 한정하여 답변하도록 클라이언트에 제공하는 지침입니다."""
        return INSTRUCTIONS

    return mcp


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--transport', choices=['stdio', 'streamable-http'], default='stdio')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)  # stderr, never contaminate the stdio protocol
    create_server(args.host, args.port).run(transport=args.transport)


if __name__ == '__main__':
    main()
