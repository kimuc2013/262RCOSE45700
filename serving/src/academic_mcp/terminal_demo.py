"""Real MCP client: initialize, list tools, and call example tools from a terminal."""
import argparse
import asyncio
import json
import os
import shutil
import sys
import unicodedata

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

EXAMPLES = [
    ('이번 학기 이중전공 신청 언제야?', 'get_academic_calendar',
     {'semester': '2026-2', 'keyword': '이중전공'}),
    ('이중전공 신청자격은?', 'search_academic_info',
     {'query': '이중전공 신청자격은?', 'semester': '2026-2'}),
    ('같은 질문 다시: 이중전공 신청자격은?', 'search_academic_info',
     {'query': '이중전공 신청자격은?', 'semester': '2026-2'}),
    ('이중전공 합격컷 알려줘', 'search_academic_info',
     {'query': '이중전공 합격컷 알려줘', 'semester': '2026-2'}),
    ('내 성적 알려줘', 'search_academic_info', {'query': '내 성적 알려줘'}),
    ('그럼 자격은? (앞 대화: 이중전공 신청)', 'search_academic_info',
     {'query': '그럼 자격은?', 'history': [{'role': 'user', 'content': '이중전공 신청 언제야?'}]}),
]


class Console:
    COLORS = {'title': '1;36', 'question': '1', 'protocol': '35',
              'good': '1;32', 'warning': '1;33', 'info': '1;34',
              'error': '1;31', 'dim': '90', 'source': '36'}

    def __init__(self, color='auto', raw=False):
        self.enabled = not raw and color != 'never' and (color == 'always' or (
            sys.stdout.isatty() and 'NO_COLOR' not in os.environ and os.getenv('TERM') != 'dumb'))
        self.width = max(40, min(shutil.get_terminal_size((88, 24)).columns, 100))

    def paint(self, text, kind):
        return f'\033[{self.COLORS[kind]}m{text}\033[0m' if self.enabled else str(text)

    def line(self, text='', kind=None):
        print(self.paint(text, kind) if kind else text)

    def field(self, label, text, kind=None):
        print('  ' + self.paint(label + '  ', 'dim') + (self.paint(text, kind) if kind else str(text)))

    def paragraph(self, text):
        # Count terminal cells so Korean excerpts fit narrow terminal windows.
        current, width = '', 0
        for char in text:
            size = 0 if unicodedata.combining(char) else (2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1)
            if char == '\n' or width + size > self.width - 4:
                print('  ' + current.rstrip())
                current, width = '', 0
                if char == '\n':
                    continue
            if not current and char.isspace():
                continue
            current += char
            width += size
        if current:
            print('  ' + current.rstrip())

    def heading(self, number, question):
        self.line()
        self.line('─' * self.width, 'dim')
        self.line(f'  {number:02d}  {question}', 'question')
        self.line('─' * self.width, 'dim')

    def result(self, data):
        status = data['status']
        kind = {'found': 'good', 'insufficient_evidence': 'warning',
                'login_required': 'info', 'out_of_scope': 'warning'}.get(status, 'error')
        metrics = data.get('metrics', {})
        badges = [self.paint(status.upper(), kind),
                  self.paint('CACHE HIT' if data['cache_hit'] else 'CACHE MISS', 'good' if data['cache_hit'] else 'dim')]
        if metrics.get('latency_ms') is not None:
            badges.append(self.paint(f"{metrics['latency_ms']:.2f} ms", 'title'))
        print('\n  ' + '   |   '.join(badges))
        if data['freshness_warning']:
            self.line('  ! 최신성 경고: 수집일과 공식 원문의 최신 공지를 확인하세요.', 'warning')
        if data['message']:
            self.line()
            self.paragraph(data['message'])
        for event in data.get('events', []):
            self.line()
            self.field('일정', event['name'], 'question')
            self.field('기간', f"{event['start']} ~ {event['end']}", 'good')
            self.field('출처', event['source'], 'source')
            self.field('수집', event['collected_at'], 'dim')
        if data['evidence']:
            first = data['evidence'][0]
            self.line()
            self.field('근거' if status == 'found' else '검색 후보', first['section'], kind)
            self.paragraph(first['text'])
            self.field('출처', first['url'], 'source')
            self.field('수집', first['collected_at'], 'dim')
        calls, shared = metrics.get('model_calls', 0), metrics.get('coalesced', False)
        self.line()
        self.field('실행', f"모델 호출 {calls}회 / 진행 중 작업 공유 {'예' if shared else '아니오'}", 'dim')


async def run(args):
    console = Console(args.color, args.raw)
    console.line('\n  KU ACADEMIC MCP', 'title')
    console.line('  실제 MCP 호출 / 로컬 개발판', 'dim')
    console.field('서버', args.url, 'source')
    console.line('  예시에 도구와 입력을 명시합니다. LLM 자동 선택과 답변 생성은 사용하지 않습니다.', 'dim')
    async with streamable_http_client(args.url) as (read, write, _):
        async with ClientSession(read, write) as session:
            initialized = await session.initialize()
            console.field('initialize', f'{initialized.serverInfo.name} {initialized.serverInfo.version}', 'protocol')
            tools = await session.list_tools()
            console.field('tools/list', ', '.join(tool.name for tool in tools.tools), 'protocol')
            examples = [(args.query, args.tool, json.loads(args.arguments))] if args.tool else EXAMPLES
            for number, (question, tool, arguments) in enumerate(examples, 1):
                console.heading(number, question)
                console.field('tools/call', tool, 'protocol')
                console.field('입력', json.dumps(arguments, ensure_ascii=False), 'dim')
                result = await session.call_tool(tool, arguments)
                if result.isError:
                    console.line('  MCP TOOL ERROR', 'error')
                    console.paragraph('\n'.join(c.text for c in result.content if c.type == 'text'))
                    continue
                data = result.structuredContent
                if args.raw:
                    print(json.dumps(data, ensure_ascii=False, indent=2))
                    continue
                console.result(data)
            console.line()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8000/mcp')
    parser.add_argument('--raw', action='store_true', help='Print complete structured MCP responses')
    parser.add_argument('--color', choices=['auto', 'always', 'never'], default='auto',
                        help='ANSI color mode (default: auto, respects NO_COLOR)')
    parser.add_argument('--tool', choices=['get_academic_calendar', 'search_academic_info'])
    parser.add_argument('--query', default='직접 지정한 도구 호출', help='Display label for a custom call')
    parser.add_argument('--arguments', default='{}', help='Actual tool arguments as JSON')
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == '__main__':
    main()
