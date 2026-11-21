"""Recovery is bounded; ranked reuse must preserve original Retriever results."""
import asyncio
import json
import threading
import time
from datetime import date

import httpx
import pytest

from academic_mcp.model_transport import ReliableModelClient
from academic_mcp.runtime import Budget, current_budget, DeadlineExceeded, Limits
from academic_mcp.service import AcademicService
from academic_mcp.telemetry import ObservedClient, Telemetry
from academic_mcp.metrics import read_report
from test_retrieval import setup as retrieval_setup
from test_runtime import until


@pytest.mark.parametrize('failure', [httpx.ReadError, httpx.RemoteProtocolError, httpx.ConnectError, httpx.ConnectTimeout])
def test_transient_failure_retried_once_and_every_attempt_observed(tmp_path, failure):
    telemetry = Telemetry(tmp_path/'metrics.db')
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise failure('socket closed', request=request)
        return httpx.Response(200, json={'model':'embed'})
    client = ReliableModelClient(ObservedClient(httpx.Client(transport=httpx.MockTransport(handler)),telemetry))
    try:
        assert client.post('http://model.test/embed',json={'input':['private question'],'model':'embed'},timeout=1).status_code == 200
        assert len(calls) == 2
    finally:
        client.close(); telemetry.close()
    stage = read_report(telemetry.path)['groups']['live']['api_stages']['embedding']
    assert stage['attempts'] == 2 and stage['transport_errors'] == 1
    assert stage['error_types'] == {failure.__name__:1}


def test_retry_exhaustion_and_read_timeout_do_not_loop():
    calls=[]
    def handler(request):
        calls.append(1)
        raise httpx.ReadError('closed',request=request)
    client=ReliableModelClient(httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(httpx.ReadError):client.post('http://model.test',timeout=1)
    assert len(calls)==2
    client.close()
    def timeout(request):
        calls.append(1)
        raise httpx.ReadTimeout('late',request=request)
    client=ReliableModelClient(httpx.Client(transport=httpx.MockTransport(timeout)))
    with pytest.raises(httpx.ReadTimeout):client.post('http://model.test',timeout=1)
    assert len(calls)==3
    client.close()


def test_retry_never_resets_deadline():
    calls=[]
    def handler(request):
        calls.append(1)
        time.sleep(.02)
        raise httpx.ReadError('closed',request=request)
    client=ReliableModelClient(httpx.Client(transport=httpx.MockTransport(handler)))
    token=current_budget.set(Budget(time.monotonic()+.01))
    try:
        with pytest.raises(DeadlineExceeded):client.post('http://model.test',timeout=1)
        assert len(calls)==1
    finally:current_budget.reset(token);client.close()


def test_http_error_is_not_retried():
    calls=[]
    def handler(request):
        calls.append(1)
        return httpx.Response(503,json={'error':'not-ready'})
    client=ReliableModelClient(httpx.Client(transport=httpx.MockTransport(handler)))
    assert client.post('http://model.test',timeout=1).status_code==503
    assert len(calls)==1
    client.close()


def test_ranked_cache_reuses_across_topk_scope_but_response_stays_isolated(retrieval_setup):
    store,fake,retriever=retrieval_setup
    retriever.build_index();fake.requests.clear()
    service=AcademicService(store,retriever=retriever)
    first=service.search('이중전공 자격',top_k=1,semester='2026-1',target='학부')
    second=service.search('이중전공 자격',top_k=3,semester='2026-2',target='편입생')
    assert len(fake.requests)==2
    assert not first['metrics']['retrieval_cache_hit']
    assert second['metrics']['retrieval_cache_hit'] and second['metrics']['model_calls']==0
    assert not second['cache_hit']  # whole-response cache and evidence cache are distinct
    assert second['semester']=='2026-2' and second['target']=='편입생'
    assert first['evidence']==second['evidence'][:1]
    assert first['freshness_warning'] and not second['freshness_warning']
    assert service.search('이중전공 자격',top_k=3,semester='2026-2',target='편입생')['cache_hit']


def test_cache_matches_uncached_model_for_candidate_windows(retrieval_setup):
    store,fake,retriever=retrieval_setup
    retriever.build_index()
    service=AcademicService(store,retriever=retriever)
    for reranker in [True,False]:
        retriever.use_reranker=reranker
        for candidates in [2,20]:
            retriever.candidates=candidates
            for k in [1,2,5,10]:
                expected=retriever.search('이중전공 자격',k)
                actual=service.search('이중전공 자격',top_k=k)
                assert actual['evidence']==[{**r,'content_kind':'untrusted_source_excerpt'} for r in expected['evidence']]
                assert actual['status']==expected['status']


def test_failure_never_populates_cache_and_retry_recovers_in_service(retrieval_setup):
    store,fake,retriever=retrieval_setup
    retriever.build_index();fake.requests.clear()
    original=retriever._client
    calls=[]
    def handler(request):
        calls.append(1)
        if len(calls)<=2:raise httpx.RemoteProtocolError('reset',request=request)
        return fake(request)
    retriever._client=httpx.Client(transport=httpx.MockTransport(handler));original.close()
    service=AcademicService(store,retriever=retriever)
    with pytest.raises(httpx.RemoteProtocolError):service.search('이중전공 자격')
    with store.connect() as db:assert db.execute('select count(*) from cache').fetchone()[0]==0
    assert service.search('이중전공 자격')['status']=='evidence_available'
    assert len(calls)==4


def test_ranked_ttl_reference_day_profile_and_negation(retrieval_setup):
    store,fake,retriever=retrieval_setup
    retriever.build_index();fake.requests.clear()
    now=[100.0]
    service=AcademicService(store,retriever=retriever,clock=lambda:now[0],cache_ttl=1,ranked_cache_ttl=10)
    service.search('이중전공 신청 가능')
    now[0]=102
    assert service.search('이중전공 신청 가능')['metrics']['retrieval_cache_hit']
    assert not service.search('이중전공 신청 불가능')['metrics']['retrieval_cache_hit']
    now[0]=111
    assert not service.search('이중전공 신청 가능')['metrics']['retrieval_cache_hit']
    service.today=lambda:date(2026,11,22)
    assert not service.search('이중전공 신청 가능')['metrics']['retrieval_cache_hit']
    service.model_revision='new'
    assert not service.search('이중전공 신청 가능')['metrics']['retrieval_cache_hit']


def test_cross_topk_concurrent_requests_share_one_retrieval(retrieval_setup,monkeypatch):
    store,fake,retriever=retrieval_setup
    retriever.build_index();fake.requests.clear()
    started,release=threading.Event(),threading.Event();original=retriever.search
    def blocked(*args,**kwargs):
        started.set();assert release.wait(2)
        return original(*args,**kwargs)
    monkeypatch.setattr(retriever,'search',blocked)
    service=AcademicService(store,retriever=retriever,limits=Limits(max_running=4))
    async def run():
        tasks=[asyncio.create_task(service.asearch('이중전공 자격',top_k=k)) for k in [1,3,5,10]]
        await until(started.is_set)
        await until(lambda:service.requests.running==4)
        release.set()
        results=await asyncio.gather(*tasks)
        assert len(fake.requests)==2
        assert sum(r['metrics']['model_calls'] for r in results)==2
        assert sum(r['metrics']['retrieval_cache_hit'] for r in results)==3
        assert all(r['evidence'][:1]==results[0]['evidence'][:1] for r in results)
        assert not service.ranked_cache._flights
    try:asyncio.run(run())
    finally:release.set()


def test_revision_change_during_model_work_never_publishes_ranked_cache(retrieval_setup,monkeypatch):
    from academic_mcp.runtime import DataChanged
    store,fake,retriever=retrieval_setup
    retriever.build_index();original=retriever.search
    def changed(*args,**kwargs):
        result=original(*args,**kwargs)
        _,chunks,events=store.snapshot();chunks[0]['text']+=' changed'
        store.replace(chunks,events)
        return result
    monkeypatch.setattr(retriever,'search',changed)
    service=AcademicService(store,retriever=retriever)
    with pytest.raises(DataChanged):service.search('이중전공 자격')
    with store.connect() as db:assert db.execute('select count(*) from cache').fetchone()[0]==0


def test_malformed_embedding_is_never_cached(retrieval_setup):
    store,fake,retriever=retrieval_setup
    retriever.build_index();retriever._client.close()
    def malformed(request):
        return httpx.Response(200,json={'model':'wrong-model','data':[]})
    retriever._client=httpx.Client(transport=httpx.MockTransport(malformed))
    service=AcademicService(store,retriever=retriever)
    with pytest.raises(ValueError,match='unexpected model'):service.search('이중전공 자격')
    with store.connect() as db:assert db.execute('select count(*) from cache').fetchone()[0]==0


def test_effective_cache_rate_does_not_double_count():
    from academic_mcp.metrics import aggregate
    events=[{'kind':'request','payload':{'tool':'search_academic_info','status':'evidence_available','cache_hit':whole,'retrieval_cache_hit':ranked}} for whole,ranked in [(True,True),(False,True),(False,False)]]
    # aggregate accepts decoded events with fields at the top level.
    events=[dict(kind=e['kind'],**e['payload']) for e in events]
    tool=aggregate(events)['tools']['search_academic_info']
    assert tool['cache_hit_rate']['count']==1
    assert tool['effective_cache_hit_rate']=={'count':2,'denominator':3,'percent':66.67}
