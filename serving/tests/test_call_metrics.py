import asyncio
import json
import threading

import httpx

from academic_mcp.answer import FinalAnswers
from academic_mcp.service import AcademicService
from academic_mcp.call_metrics import response_metrics
from test_retrieval import setup as retrieval_setup
from test_final_answers import generator
from test_service import service
from test_runtime import until


def contract(metrics, counts):
    assert [metrics[k+'_calls'] for k in ('embedding','rerank','generation','rewrite')] == counts
    assert metrics['model_calls'] == sum(counts)
    assert metrics['latency_ms'] == metrics['server_elapsed_ms']
    assert metrics['metrics_schema_version'] == 2
    assert metrics['model_calls_complete']


def test_search_and_answer_count_real_http_stages_and_never_replay_cache_counts(retrieval_setup):
    store, fake, retriever = retrieval_setup
    retriever.build_index(); fake.requests.clear()
    svc = AcademicService(store, retriever=retriever)
    def generate(req):
        return httpx.Response(200,json={'model':'m','choices':[{'finish_reason':'stop','message':{
            'content':json.dumps({'answer':'공식 안내입니다.','can_answer':True,'sources':[1]})}}]})
    answer = FinalAnswers(svc,base_url='https://model.test/v1',model='m',transport=httpx.MockTransport(generate))
    async def run():
        a = await answer.answer('이중전공 제도를 설명해줘')
        contract(a['metrics'], [1,1,1,0])
        assert len(fake.requests) == 2
        b = await answer.answer('이중전공 제도를 설명해줘')
        contract(b['metrics'], [0,0,0,0])
        assert b['metrics']['cache_hit']
        # A previously warmed search is reused without attributing historical calls.
        c = await svc.asearch('이중전공 제도를 설명해줘')
        contract(c['metrics'], [0,0,0,0])
        d = await svc.asearch('휴학 절차 설명')
        contract(d['metrics'], [1,1,0,0])
    try: asyncio.run(run())
    finally: answer.close();svc.close()


def test_connection_retry_counts_each_http_attempt(retrieval_setup):
    store, fake, retriever = retrieval_setup
    retriever.build_index()
    inner= retriever._client.post
    failed=[False]
    def once(*a,**kw):
        if not failed[0]:
            failed[0]=True
            raise httpx.ConnectError('broken')
        return inner(*a,**kw)
    retriever._client.post=once
    svc=AcademicService(store,retriever=retriever)
    try:
        result=asyncio.run(svc.asearch('휴학 절차 설명'))
        contract(result['metrics'], [2,1,0,0])
    finally:svc.close()


def test_shared_generation_has_zero_own_calls_but_reports_shared_work(service):
    started,release=threading.Event(),threading.Event()
    def handler(req):
        started.set();release.wait(1)
        return httpx.Response(200,json={'model':'same-model','choices':[{'finish_reason':'stop','message':{
            'content':json.dumps({'answer':'답변','can_answer':True,'sources':[1]})}}]})
    answer=generator(service,handler)
    async def run():
        tasks=[asyncio.create_task(answer.answer('이중전공 제도를 설명해줘')) for _ in range(3)]
        await until(started.is_set);await until(lambda:service.requests._waiters==3)
        release.set();results=await asyncio.gather(*tasks)
        assert sum(r['metrics']['model_calls'] for r in results)==1
        for r in results:
            m=r['metrics'];assert m['shared_model_calls']==1
            assert m['shared_model_calls_by_stage']['generation_calls']==1
            contract(m,[0,0,0 if m['coalesced'] else 1,0])
    try:asyncio.run(run())
    finally:release.set();answer.close()


def test_unknown_timeout_counts_are_not_zero():
    m=response_metrics({},10,complete=False)
    assert m['model_calls'] is None and m['generation_calls'] is None
    assert not m['model_calls_complete']


def test_semantic_hit_counts_embedding_only(retrieval_setup):
    store,fake,retriever=retrieval_setup
    retriever.build_index();fake.requests.clear()
    svc=AcademicService(store,retriever=retriever,semantic_cache=True)
    async def run():
        contract((await svc.asearch('복학 신청은 어디서 해?'))['metrics'],[1,1,0,0])
        hit=await svc.asearch('복학 신청 경로를 알려줘')
        assert hit['metrics']['semantic_cache_hit']
        contract(hit['metrics'],[1,0,0,0])
    try:asyncio.run(run())
    finally:svc.close()


def test_direct_answer_still_reports_retrieval_calls(retrieval_setup,monkeypatch):
    store,fake,retriever=retrieval_setup
    retriever.build_index()
    svc=AcademicService(store,retriever=retriever)
    monkeypatch.setattr('academic_mcp.answer.direct_answer',lambda *a:{'answer':'원문 답변','can_answer':True,'citations':['https://example.test']})
    answer=FinalAnswers(svc,base_url='https://model.test/v1',model='m',transport=httpx.MockTransport(lambda r:(_ for _ in ()).throw(AssertionError('No generation'))))
    try:
        result=asyncio.run(answer.answer('복학 신청은 어디서 해?'))
        contract(result['metrics'],[1,1,0,0])
    finally:answer.close();svc.close()
