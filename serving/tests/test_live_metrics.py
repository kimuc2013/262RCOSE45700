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
            assert r.json()['groups']['all']['series']['bucket_seconds']==300
            assert 12 <= len(r.json()['groups']['all']['series']['points']) <= 13
            assert client.get('/ops/api/metrics?bucket_minutes=7',headers=h).status_code==400
            assert client.get('/ops/api/metrics?bucket_minutes=foo',headers=h).json()['error']=='invalid_bucket'
            fine=client.get('/ops/api/metrics?hours=1&bucket_minutes=1',headers=h).json()
            assert fine['groups']['all']['series']['bucket_seconds']==60
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
        assert request.url.path=='/ops/api/metrics' and request.url.params['hours']=='1'
        assert request.url.params['bucket_minutes'] in ('1','5')
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
                assert calls[0].url.params['bucket_minutes']=='5'
                assert client.get('/api/metrics?hours=1&bucket_minutes=1').status_code==200
                assert calls[-1].url.params['bucket_minutes']=='1'
                assert client.get('/api/metrics', headers={'Host': 'evil.test'}).status_code == 403
                assert client.get('/api/metrics', headers={'Origin': 'https://evil.test'}).status_code == 403
                assert client.get('/api/metrics?hours=999').status_code == 400
                assert client.get('/api/metrics?bucket_minutes=7').status_code==400
                assert len(calls) == 2
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


def test_five_minute_reaggregation_uses_raw_samples_not_average_of_rates_or_percentiles():
    rows=[{'ts':61,'kind':'request','tool':'search_academic_info','status':'evidence_available','elapsed_ms':1010,'cache_hit':True}]
    rows += [{'ts':121+i,'kind':'request','tool':'search_academic_info','status':'error' if i==0 else 'evidence_available','elapsed_ms':10,'cache_hit':False} for i in range(9)]
    points=time_series(rows,0,600,bucket_seconds=300)['points']
    first,empty=points
    assert first['cache_samples']==10 and first['cache_hits']==1 and first['cache_rate']==10
    assert first['latency_samples']==10 and first['p50_ms']==10 and first['p95_ms']==560
    assert first['requests']==10 and first['nonfailures']==9 and first['failures']==1
    assert empty['cache_samples']==0 and empty['cache_rate'] is None
    assert empty['latency_samples']==0 and empty['p50_ms'] is None and empty['p95_ms'] is None


def test_series_scope_counts_boundaries_and_partial_intervals():
    rows=[{'ts':90,'kind':'request','tool':'search_academic_info','status':'found','elapsed_ms':99},
          {'ts':120,'kind':'request','tool':'search_academic_info','status':'found','elapsed_ms':20,'cache_hit':True},
          {'ts':300,'kind':'request','tool':'get_academic_calendar','status':'found','elapsed_ms':30},
          {'ts':301,'kind':'api','stage':'embedding'},
          {'ts':660,'kind':'request','tool':'search_academic_info','status':'found','elapsed_ms':99}]
    points=time_series(rows,100,650,bucket_seconds=300)['points']
    assert [p['ts'] for p in points]==[0,300,600]
    assert points[0]['partial'] and points[0]['interval_start']==100 and points[0]['interval_end']==300
    assert points[0]['cache_samples']==1 and points[0]['latency_samples']==1
    assert points[1]['requests']==1 and points[1]['cache_samples']==0 and points[1]['cache_rate'] is None
    assert points[1]['latency_samples']==1 and points[1]['p50_ms']==points[1]['p95_ms']==30
    assert points[1]['api_calls']=={'embedding':1}
    assert points[2]['partial'] and points[2]['interval_end']==650 and points[2]['requests']==0
    assert sum(p['requests'] for p in points)==2


def test_reader_cache_is_partitioned_by_aggregation_interval(monkeypatch):
    calls=[]
    def read(path,hours,**kwargs):calls.append(kwargs['bucket_minutes']);return {'bucket':kwargs['bucket_minutes']}
    monkeypatch.setattr('academic_mcp.live_metrics.read_report',read)
    async def run():
        reader=MetricsReader('unused')
        assert (await reader.get(1,5))['bucket']==5
        assert (await reader.get(1,1))['bucket']==1
        assert (await reader.get(1,5))['bucket']==5
        assert calls==[5,1]
    asyncio.run(run())
