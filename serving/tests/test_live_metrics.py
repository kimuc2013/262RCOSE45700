import asyncio
from pathlib import Path
import threading
import time
from http.server import ThreadingHTTPServer

import httpx
from starlette.testclient import TestClient

from academic_mcp.dashboard import make_handler
from academic_mcp.live_metrics import MetricsReader, dashboard_html
from academic_mcp.metrics import time_series
from academic_mcp.server import create_server
from academic_mcp.telemetry import Telemetry
from test_service import service  # noqa: F401


def test_series_keeps_missing_usage_and_failures_distinct():
    rows = [
        {'ts': 1, 'kind': 'request', 'tool': 'answer_academic_question', 'status': 'answered', 'elapsed_ms': 20, 'cache_hit': True},
        {'ts': 1.1, 'kind': 'request', 'tool': 'answer_academic_question', 'status': 'answer_timeout', 'elapsed_ms': 1850},
        {'ts': 1.2, 'kind': 'api', 'stage': 'generation', 'total_tokens': None},
        {'ts': 3, 'kind': 'api', 'stage': 'generation', 'total_tokens': 90}]
    points = time_series(rows, 0, 4, buckets=4)['points']
    assert points[0]['p95_ms'] is None and points[0]['generation_tokens'] == 0
    assert points[1]['requests'] == 2 and points[1]['failures'] == 1
    assert points[1]['cache_rate'] == 50 and points[1]['generation_tokens'] is None
    assert points[3]['generation_tokens'] == 90
    assert sum(p['requests'] for p in points) == 2


def test_owner_gateway_marker_required_and_new_events_appear(service, tmp_path, monkeypatch):
    path = tmp_path/'metrics.sqlite3'
    t = service.telemetry = Telemetry(path)
    t.emit('request', {'tool': 'search_academic_info', 'status': 'found', 'elapsed_ms': 10})
    t._queue.join()
    monkeypatch.setattr('academic_mcp.server.build_service', lambda: service)
    monkeypatch.delenv('ANSWER_MODEL', raising=False)
    mcp = create_server()
    try:
        with TestClient(mcp.streamable_http_app()) as client:
            shell = client.get('/ops')
            assert shell.status_code == 200 and 'const LOCAL=false' in shell.text
            assert 'no-store' in shell.headers['cache-control']
            for headers in [{}, {'Authorization': 'Bearer team-token'}]:
                assert client.get('/ops/api/metrics', headers=headers).status_code == 401
            h = {'X-KU-Metrics-Access': 'owner'}
            assert client.get('/ops/api/metrics?hours=9999', headers=h).status_code == 400
            r = client.get('/ops/api/metrics?hours=1', headers=h)
            assert r.status_code == 200 and r.json()['groups']['live']['requests'] == 1
            assert len(r.json()['groups']['all']['series']['points']) == 60
            assert 'Authorization' not in r.text
            # Another supported window triggers a fresh read after new work.
            t.emit('request', {'tool': 'search_academic_info', 'status': 'found', 'elapsed_ms': 12, 'cache_hit': True})
            t._queue.join()
            r = client.get('/ops/api/metrics?hours=.25', headers=h)
            assert r.json()['groups']['live']['requests'] == 2
    finally:
        service.close()


def test_missing_metrics_returns_not_ready_not_snapshot(service, tmp_path, monkeypatch):
    service.telemetry = Telemetry(tmp_path/'not-yet-written.sqlite3')
    monkeypatch.setattr('academic_mcp.server.build_service', lambda: service)
    monkeypatch.delenv('ANSWER_MODEL', raising=False)
    try:
        with TestClient(create_server().streamable_http_app()) as client:
            r = client.get('/ops/api/metrics', headers={'X-KU-Metrics-Access': 'owner'})
            assert r.status_code == 503 and r.json() == {'error': 'metrics_not_ready'}
    finally:
        service.close()


def test_read_cache_coalesces_viewers_without_mcp_slots(monkeypatch):
    calls = []

    def read(*args, **kwargs):
        calls.append(1)
        time.sleep(.02)
        return {'version': len(calls)}
    monkeypatch.setattr('academic_mcp.live_metrics.read_report', read)

    async def run():
        reader = MetricsReader('unused', ttl=.04)
        values = await asyncio.gather(*(reader.get(1) for _ in range(20)))
        assert len(calls) == 1 and all(v['version'] == 1 for v in values)
        await asyncio.sleep(.05)
        assert (await reader.get(1))['version'] == 2
    asyncio.run(run())


def test_loopback_bridge_hides_owner_key_and_rejects_bad_origin():
    calls = []
    def upstream(request):
        calls.append(request)
        assert request.headers['Authorization'] == 'Bearer private-owner-secret'
        assert str(request.url) == 'https://metrics.test/ops/api/metrics?hours=1'
        return httpx.Response(200, json={'window': {}, 'groups': {}})
    with httpx.Client(transport=httpx.MockTransport(upstream)) as remote:
        server = ThreadingHTTPServer(('127.0.0.1', 0), lambda *a: None)
        port = server.server_port
        server.RequestHandlerClass = make_handler(remote, 'https://metrics.test', 'private-owner-secret', port)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with httpx.Client(base_url=f'http://127.0.0.1:{port}') as client:
                page = client.get('/')
                assert 'const LOCAL=true' in page.text and 'private-owner-secret' not in page.text
                good = client.get('/api/metrics?hours=1')
                assert good.status_code == 200 and 'private-owner-secret' not in good.text
                assert client.get('/api/metrics', headers={'Host': 'evil.test'}).status_code == 403
                assert client.get('/api/metrics', headers={'Origin': 'https://evil.test'}).status_code == 403
                assert client.get('/api/metrics?hours=999').status_code == 400
                assert len(calls) == 1
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


def test_gateway_does_not_authorize_team_key_for_ops():
    config = (Path(__file__).parents[1]/'deploy/public/Caddyfile').read_text()
    ops_section = config.split('@ops_owner {', 1)[1].split('@ops_api path', 1)[0]
    assert 'MCP_OWNER_TOKEN' in ops_section and 'MCP_TEAM_TOKEN' not in ops_section
    assert 'header_up X-KU-Metrics-Access owner' in ops_section
    mcp_section = config.split('handle @authorized {', 1)[1]
    assert 'header_up -X-KU-Metrics-Access' in mcp_section
    assert 'path /ops/api/*' in config


def test_dashboard_credentials_not_persisted_or_embedded():
    page = dashboard_html(local=False)
    assert '__LOCAL_MODE__' not in page
    assert 'localStorage' not in page and 'sessionStorage' not in page
    assert 'setTimeout(poll,5000)' in page
    assert "cache:'no-store'" in page
