import asyncio
import json
import sqlite3
import threading
import time

import httpx
import pytest

from academic_mcp.metrics import aggregate, html_report, read_report
from academic_mcp.telemetry import ObservedClient, Telemetry, usage
from test_final_answers import generator
from test_runtime import until
from test_service import service  # noqa: F401
from test_retrieval import setup  # noqa: F401


def rows(path):
    with sqlite3.connect(path) as db:
        return [dict(json.loads(p), kind=k, trace_id=t) for k, t, p in
                db.execute('SELECT kind,trace_id,payload FROM telemetry_events')]


def test_physical_usage_once_for_shared_generation_and_cache(service, tmp_path):
    path = tmp_path/'metrics.sqlite3'
    telemetry = service.telemetry = Telemetry(path)
    started, release = threading.Event(), threading.Event()

    def handler(request):
        started.set()
        release.wait(1)
        return httpx.Response(200, json={'model': 'same-model',
            'usage': {'prompt_tokens': 123, 'completion_tokens': 17, 'total_tokens': 140,
                      'prompt_tokens_details': {'cached_tokens': 32}},
            'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({
                'answer': '등록 3학기 이상입니다.', 'can_answer': True, 'sources': [1]})}}]})

    final = generator(service, handler)

    async def run():
        tasks = [asyncio.create_task(telemetry.observe('answer_academic_question',
                 lambda: final.answer('이중전공 제도를 설명해줘'), experiment=True)) for _ in range(8)]
        await until(started.is_set)
        await until(lambda: service.requests._waiters == 8)
        release.set()
        results = await asyncio.gather(*tasks)
        assert sum(r['metrics']['coalesced'] for r in results) == 7
        await telemetry.observe('answer_academic_question',
            lambda: final.answer('이중전공 제도를 설명해줘'), experiment=True)

    try:
        asyncio.run(run())
    finally:
        release.set()
        final.close()
        telemetry.close()
    report = read_report(path)
    assert report['groups']['live']['requests'] == 0
    group = report['groups']['experiment']
    assert group['requests'] == 9 and group['physical_api_attempts'] == 1
    tool = group['tools']['answer_academic_question']
    assert tool['cache_hit_rate']['count'] == 1 and tool['cache_hit_rate']['denominator'] == 9
    assert tool['coalesced'] == 7 and tool['cache_or_shared_rate']['count'] == 8
    tokens = group['api_stages']['generation']['prompt_tokens']
    assert tokens == {'reported_sum': 123, 'reported_calls': 1, 'all_calls': 1}
    raw = rows(path)
    api = next(r for r in raw if r['kind'] == 'api')
    assert api['trace_id'] in {r['trace_id'] for r in raw if r['kind'] == 'request'}
    text = path.read_bytes()
    for private in ['이중전공', '등록 3학기', 'Authorization', 'model.test']:
        assert private.encode() not in text


def test_timeout_still_accounts_late_physical_usage(service, tmp_path):
    path = tmp_path/'metrics.sqlite3'
    telemetry = service.telemetry = Telemetry(path)
    started, release = threading.Event(), threading.Event()

    def handler(req):
        started.set()
        release.wait(1)
        return httpx.Response(200, json={'usage': {'prompt_tokens': 88}, 'model': 'same-model'})

    final = generator(service, handler, budget=.05)

    async def run():
        task = asyncio.create_task(telemetry.observe('answer_academic_question',
            lambda: final.answer('이중전공 제도를 설명해줘')))
        await until(started.is_set)
        assert (await task)['status'] == 'answer_timeout'
        release.set()
        await until(lambda: service.requests._admitted == 0)

    try:
        asyncio.run(run())
    finally:
        release.set()
        final.close()
        telemetry.close()
    group = read_report(path)['groups']['live']
    assert group['tools']['answer_academic_question']['statuses'] == {'answer_timeout': 1}
    assert group['tools']['answer_academic_question']['answered_within_2s']['count'] == 0
    assert group['api_stages']['generation']['prompt_tokens']['reported_sum'] == 88


def test_errors_missing_usage_and_http_failures_are_not_free_tokens(tmp_path):
    path = tmp_path/'metrics.sqlite3'
    telemetry = Telemetry(path)

    def handler(request):
        if request.url.path == '/timeout':
            raise httpx.ReadTimeout('DO NOT LOG PRIVATE ENDPOINT', request=request)
        return httpx.Response(503, json={'error': 'secret-query-content'})

    client = ObservedClient(httpx.Client(transport=httpx.MockTransport(handler)), telemetry)
    assert client.post('https://private.test/embed', json={'input': ['private question'], 'model': 'bge'}).status_code == 503
    with pytest.raises(httpx.ReadTimeout):
        client.post('https://private.test/timeout', json={'documents': ['private doc'], 'model': 'rerank'})
    client.close()
    telemetry.close()
    stage = read_report(path)['groups']['live']['api_stages']
    assert stage['embedding']['http_errors'] == 1
    assert stage['rerank']['transport_errors'] == 1
    assert stage['embedding']['prompt_tokens']['reported_sum'] is None
    assert stage['rerank']['completion_tokens']['reported_calls'] == 0
    assert b'PRIVATE' not in path.read_bytes() and b'private question' not in path.read_bytes()


def test_format_repair_counts_both_api_calls(service, tmp_path):
    path = tmp_path/'metrics.sqlite3'
    telemetry = service.telemetry = Telemetry(path)
    calls = []

    def handler(req):
        calls.append(1)
        content = '{}' if len(calls) == 1 else json.dumps({'answer': '등록 3학기 이상입니다.', 'can_answer': True, 'sources': [1]})
        return httpx.Response(200, json={'model': 'same-model', 'usage': {'prompt_tokens': 40},
            'choices': [{'finish_reason': 'stop', 'message': {'content': content}}]})

    final = generator(service, handler)
    asyncio.run(telemetry.observe('answer_academic_question', lambda: final.answer('이중전공 제도를 설명해줘')))
    final.close()
    telemetry.close()
    g = read_report(path)['groups']['live']['api_stages']['generation']
    assert g['attempts'] == 2 and g['prompt_tokens']['reported_sum'] == 80


def test_telemetry_storage_failure_does_not_fail_answer(tmp_path):
    telemetry = Telemetry(tmp_path)  # A directory cannot be opened as a DB.

    async def run():
        async def good():
            return {'status': 'answered', 'can_answer': True, 'metrics': {}}
        assert (await telemetry.observe('answer_academic_question', good))['status'] == 'answered'
    asyncio.run(run())
    telemetry.close()
    assert telemetry.write_errors == 1


def test_retention_bounds_and_allowlist(tmp_path):
    path = tmp_path/'metrics.sqlite3'
    telemetry = Telemetry(path, max_rows=3)
    for i in range(8):
        telemetry.emit('request', {'tool': 'test', 'status': 'error', 'elapsed_ms': i,
            'query': 'DO NOT STORE', 'Authorization': 'DO NOT STORE', 'history': ['private']})
    telemetry.close()
    assert len(rows(path)) == 3
    assert b'DO NOT STORE' not in path.read_bytes()


def test_failure_denominators_and_provider_cache_distinction():
    events = [
        {'kind': 'request', 'tool': 'answer_academic_question', 'elapsed_ms': 100, 'status': 'answered', 'can_answer': True, 'cache_hit': True},
        {'kind': 'request', 'tool': 'answer_academic_question', 'elapsed_ms': 100, 'status': 'insufficient_evidence', 'can_answer': False},
        {'kind': 'request', 'tool': 'answer_academic_question', 'elapsed_ms': 1850, 'status': 'answer_timeout'},
        {'kind': 'request', 'tool': 'answer_academic_question', 'elapsed_ms': 50, 'status': 'error'}]
    tool = aggregate(events)['tools']['answer_academic_question']
    assert tool['cache_hit_rate']['percent'] == 25
    assert tool['answered_within_2s']['percent'] == 25
    assert tool['answered_latency']['n'] == 1
    assert usage({'usage': {'prompt_tokens': True, 'completion_tokens': -1}})['prompt_tokens'] is None
    assert usage({'usage': {'input_tokens': 0, 'output_tokens': 0}})['prompt_tokens'] == 0


def test_html_embedded_data_escapes_script_injection(tmp_path):
    path = tmp_path/'metrics.sqlite3'
    telemetry = Telemetry(path)
    telemetry.emit('request', {'tool': 'search_academic_info', 'status': '</script><script>alert(1)</script>', 'elapsed_ms': 1})
    telemetry.close()
    html = html_report(read_report(path))
    assert '</script><script>alert' not in html
    assert '\\u003c/script>' in html


def test_real_http_context_marks_experiment_and_records_invalid_calls(service, tmp_path, monkeypatch):
    import socket
    import uvicorn
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from academic_mcp.server import create_server
    path = tmp_path/'metrics.sqlite3'
    service.telemetry = Telemetry(path)
    monkeypatch.setattr('academic_mcp.server.build_service', lambda: service)
    monkeypatch.delenv('ANSWER_MODEL', raising=False)

    async def run():
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(create_server(port=port).streamable_http_app(), log_level='error'))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            await until(lambda: server.started)
            async with httpx.AsyncClient(headers={'X-KU-Experiment': 'cache-benchmark-v1'}) as client:
                async with streamable_http_client(f'http://127.0.0.1:{port}/mcp', http_client=client) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        for tool in (await session.list_tools()).tools:
                            assert 'ctx' not in tool.inputSchema.get('properties', {})
                        good = await session.call_tool('get_academic_calendar', {'semester': '2026-2'})
                        assert not good.isError
                        bad = await session.call_tool('search_academic_info', {'query': ''})
                        assert bad.isError
        finally:
            server.should_exit = True
            await task
            sock.close()
    try:
        asyncio.run(run())
    finally:
        service.close()
    report = read_report(path)
    assert report['groups']['live']['requests'] == 0
    assert report['groups']['experiment']['requests'] == 2
    assert report['groups']['experiment']['tools']['search_academic_info']['statuses'] == {'error': 1}


def test_online_retrieval_boundary_preserves_results_and_excludes_offline_index(setup, tmp_path):
    from academic_mcp.service import AcademicService
    store, fake, retriever = setup
    retriever.build_index()  # Before instrumentation: offline work is separate.
    path = tmp_path/'metrics.sqlite3'
    telemetry = Telemetry(path)
    service = AcademicService(store, retriever=retriever, telemetry=telemetry)

    async def run():
        first = await telemetry.observe('search_academic_info', lambda: service.asearch('이중전공 신청자격은?'))
        second = await telemetry.observe('search_academic_info', lambda: service.asearch('이중전공 신청자격은?'))
        assert first['evidence'] == second['evidence']
        assert second['metrics']['cache_hit']
    try:
        asyncio.run(run())
    finally:
        service.close()
    group = read_report(path)['groups']['live']
    assert group['physical_api_attempts'] == 2
    assert group['api_stages']['embedding']['attempts'] == 1
    assert group['api_stages']['rerank']['attempts'] == 1
    assert group['api_stages']['embedding']['prompt_tokens']['reported_sum'] is None
