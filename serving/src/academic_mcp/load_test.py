"""Closed-loop load generator using independent real MCP HTTP sessions.

Without --url, launch an isolated sample server with a temporary SQLite DB.
No production cache or data is cleared. This is not an open-loop capacity test.
"""
import argparse
import asyncio
from collections import Counter
from contextlib import AsyncExitStack
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys
import tempfile
import time
import uuid

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .ingest import ingest

ERROR_CODES = ('SERVER_BUSY', 'QUEUE_TIMEOUT', 'REQUEST_DEADLINE_EXCEEDED',
               'DATA_CHANGED', 'REQUEST_CANCELLED')


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(len(ordered) * fraction) - 1)], 3)


async def measure(url, concurrency, count, scenario, timeout):
    tag = uuid.uuid4().hex
    query = f'이중전공 신청자격은? 실험 {tag}'
    async with AsyncExitStack() as stack:
        sessions = []
        # Connection establishment/initialize are deliberately outside timing.
        for _ in range(concurrency):
            read, write, _ = await stack.enter_async_context(streamable_http_client(url))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            sessions.append(session)
        if scenario == 'warm_shared':
            warm = await sessions[0].call_tool('search_academic_info', {'query': query})
            if warm.isError:
                raise RuntimeError('Warmup failed; cannot measure warm cache')
        pending = iter(range(count))
        records = []
        gate = asyncio.Event()

        async def worker(session):
            await gate.wait()
            for i in pending:
                started = time.perf_counter()
                row = {}
                try:
                    async with asyncio.timeout(timeout):
                        if scenario == 'calendar':
                            result = await session.call_tool('get_academic_calendar',
                                {'semester': '2026-2', 'keyword': '이중전공'})
                        else:
                            # Unique cold keys, or one shared key for the entire run.
                            text = query + f' 요청 {i}' if scenario == 'cold_unique' else query
                            result = await session.call_tool('search_academic_info', {'query': text})
                    if result.isError:
                        message = ' '.join(getattr(c, 'text', '') for c in result.content)
                        row['error'] = next((c for c in ERROR_CODES if c in message), 'TOOL_ERROR')
                    else:
                        data = result.structuredContent
                        row.update(status=data['status'], cache_hit=data.get('cache_hit', False),
                                   **{k: data.get('metrics', {}).get(k, 0) for k in
                                      ('coalesced', 'cache_fast_path', 'queue_ms', 'model_calls')})
                except Exception as exc:
                    row['error'] = type(exc).__name__
                row['latency_ms'] = (time.perf_counter() - started) * 1000
                records.append(row)

        tasks = [asyncio.create_task(worker(s)) for s in sessions]
        start = time.perf_counter()
        gate.set()
        await asyncio.gather(*tasks)
        seconds = time.perf_counter() - start
    successful = [r for r in records if 'error' not in r]
    return {'scenario': scenario, 'concurrency': concurrency, 'requests': count,
            'successes': len(successful), 'errors': dict(Counter(r['error'] for r in records if 'error' in r)),
            'statuses': dict(Counter(r['status'] for r in successful)),
            'elapsed_s': round(seconds, 3), 'completed_rps': round(count / seconds, 2),
            'successful_rps': round(len(successful) / seconds, 2),
            'p50_ms': percentile([r['latency_ms'] for r in records], .5),
            'p95_ms': percentile([r['latency_ms'] for r in records], .95),
            'p99_ms': percentile([r['latency_ms'] for r in records], .99),
            'success_p95_ms': percentile([r['latency_ms'] for r in successful], .95),
            'queue_p95_ms': percentile([r['queue_ms'] for r in successful], .95),
            **{k: sum(r[k] for r in successful) for k in
               ('cache_hit', 'coalesced', 'cache_fast_path', 'model_calls')}}


async def run(args, url):
    cases = []
    for concurrency in args.concurrency:
        for scenario in ('warm_shared', 'cold_shared', 'cold_unique', 'calendar'):
            result = await measure(url, concurrency, args.requests, scenario, args.timeout)
            cases.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
    return cases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', help='Existing authorized MCP server; otherwise start an isolated local sample server')
    parser.add_argument('--samples', type=Path, default=Path('../keynote/sample'))
    parser.add_argument('--concurrency', type=int, nargs='+', default=[1, 8, 32, 96])
    parser.add_argument('--requests', type=int, default=192, help='Requests per scenario and concurrency')
    parser.add_argument('--timeout', type=float, default=20)
    parser.add_argument('--output', type=Path, default=Path('data/load-test.json'))
    args = parser.parse_args()
    if args.requests < 1 or any(c < 1 or c > 256 for c in args.concurrency) or not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error('Positive request count/timeout and concurrency 1..256 required')
    report = {'timestamp_utc': datetime.now(timezone.utc).isoformat(), 'python': platform.python_version(),
              'platform': platform.platform(), 'scope': 'external endpoint' if args.url else 'isolated local sample, no model API',
              'method': 'closed-loop independent MCP sessions; setup/warmup excluded; latency includes client/HTTP; all-response percentiles include errors',
              'server_defaults': None if args.url else {'running': 4, 'queued': 8, 'callers': 64, 'request_timeout_s': 10},
              'cases': []}
    with tempfile.TemporaryDirectory(prefix='ku-mcp-load-') as tmp:
        server = None
        with open(Path(tmp) / 'server.log', 'w') as log:
            try:
                if args.url:
                    url = args.url
                else:
                    db = str(Path(tmp) / 'academic.sqlite3')
                    report['sample'] = ingest(db, args.samples)
                    with socket.socket() as sock:
                        sock.bind(('127.0.0.1', 0))
                        port = sock.getsockname()[1]
                    env = {k: v for k, v in os.environ.items() if not k.startswith(('MODEL_', 'SERVING_'))}
                    env.update(ACADEMIC_DB=db, MODEL_MODE='local')
                    server = subprocess.Popen([sys.executable, '-m', 'academic_mcp.server',
                        '--transport', 'streamable-http', '--port', str(port)], env=env, stdout=log, stderr=log)
                    deadline = time.monotonic() + 15
                    while True:
                        if server.poll() is not None:
                            raise RuntimeError('Sample server exited before startup')
                        try:
                            with socket.create_connection(('127.0.0.1', port), timeout=.2):
                                break
                        except OSError:
                            if time.monotonic() >= deadline:
                                raise TimeoutError('Sample server startup timed out')
                            time.sleep(.05)
                    url = f'http://127.0.0.1:{port}/mcp'
                report['cases'] = asyncio.run(run(args, url))
            finally:
                if server is not None:
                    server.terminate()
                    try:
                        server.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait(timeout=5)
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(f'Report: {args.output}', flush=True)


if __name__ == '__main__':
    main()
