import asyncio
import threading

import httpx

from academic_mcp import release
from academic_mcp.call_metrics import response_metrics
from test_service import service
from test_final_answers import generator
from test_runtime import until

SHA='abcdef0123456789abcdef0123456789abcdef01'


def test_baked_image_identity_wins_over_runtime_and_has_no_secret_values(tmp_path,monkeypatch):
    p=tmp_path/'release';p.write_text(SHA+'\n');monkeypatch.setattr(release,'BUILD_FILE',p)
    monkeypatch.setenv('SERVING_RELEASE_SHA','1'*40)
    a=response_metrics({},1)
    assert a['server_version']==SHA[:12] and a['release_sha']==SHA
    assert a['metrics_schema_version']==2 and len(a['server_instance'])==12
    p.write_text('not-a-build')
    b=release.release_metrics();assert b['release_sha'] is None and b['server_version']=='unversioned'


def test_current_version_stamped_on_cached_search_and_answer(service,tmp_path,monkeypatch):
    p=tmp_path/'release';p.write_text(SHA);monkeypatch.setattr(release,'BUILD_FILE',p)
    answer=generator(service)
    async def run():
        for _ in range(2):
            for result in [await service.asearch('이중전공 제도를 설명해줘'),await answer.answer('이중전공 제도를 설명해줘')]:
                assert result['metrics']['release_sha']==p.read_text()
                assert result['metrics']['request_budget_ms']>0
            p.write_text('2'*40)
    try:asyncio.run(run())
    finally:answer.close()


def test_timeout_carries_build_budget_and_stage(service,tmp_path,monkeypatch):
    p=tmp_path/'release';p.write_text(SHA);monkeypatch.setattr(release,'BUILD_FILE',p)
    started,release_worker=threading.Event(),threading.Event()
    def slow(req):started.set();release_worker.wait(1);raise httpx.ReadTimeout('slow')
    answer=generator(service,slow,budget=.05)
    async def run():
        task=asyncio.create_task(answer.answer('이중전공 제도를 설명해줘'))
        await until(started.is_set);result=await task
        assert result['status']=='answer_timeout'
        m=result['metrics'];assert m['release_sha']==SHA and m['request_budget_ms']==50
        assert m['timeout_stage']=='generation' and m['generation_calls']==1
        assert not m['model_calls_complete']
        release_worker.set();await until(lambda:service.requests._admitted==0)
    try:asyncio.run(run())
    finally:release_worker.set();answer.close()
