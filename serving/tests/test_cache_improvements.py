import asyncio
import threading
import json

import httpx
import pytest

from academic_mcp.cache import ranked_query
from academic_mcp.semantic_cache import signature
from academic_mcp.service import AcademicService
from academic_mcp.direct_answer import direct_answer
from test_retrieval import setup as retrieval_setup
from test_final_answers import generator
from test_service import service
from test_runtime import until

@pytest.mark.parametrize('a,b',[
 ('복학 신청 방법 알려줘','복학 신청 방법 알려주세요'),
 ('휴학 가능해?','휴학 가능한가요?'),
 ('등록금 얼마야?','등록금 얼마인가요?'),
])
def test_explicit_polite_variants_match(a,b):assert ranked_query(a)==ranked_query(b)

@pytest.mark.parametrize('a,b',[
 ('휴학 가능해?','휴학 불가능해?'),('재수강 A+ 가능해?','재수강 A 가능해?'),
 ('2026-1 복학 알려줘','2026-2 복학 알려주세요'),('6학점 등록금 얼마야','3학점 등록금 얼마인가요'),
 ('신입생 휴학 알려줘','편입생 휴학 알려주세요'),('복학 방법 알려줘','복학 자격 알려주세요'),
])
def test_politeness_never_erases_conditions(a,b):assert ranked_query(a)!=ranked_query(b)


def test_polite_reuse_and_shared_embedding_across_filter_context(retrieval_setup):
    store,fake,r=retrieval_setup;r.build_index();fake.requests.clear()
    svc=AcademicService(store,retriever=r)
    async def run():
        first=await svc.asearch('복학 신청 방법 알려줘',semester='2026-1')
        assert first['metrics']['model_calls']==2
        second=await svc.asearch('복학 신청 방법 알려주세요',semester='2026-1')
        assert second['metrics']['model_calls']==0 and second['metrics']['retrieval_cache_hit']
        assert second['metrics']['semantic_cache_reason']=='ranked_cache_hit'
        third=await svc.asearch('복학 신청 방법 알려줘',semester='2026-2')
        assert third['metrics']['embedding_cache_hit']
        assert not third['metrics']['retrieval_cache_hit']
        assert third['metrics']['embedding_calls']==0 and third['metrics']['rerank_calls']==1
        assert len(fake.requests)==3
    try:asyncio.run(run())
    finally:svc.close()


def test_query_vector_failure_does_not_publish(retrieval_setup):
    store,fake,r=retrieval_setup;r.build_index();fake.status=503
    svc=AcademicService(store,retriever=r)
    with pytest.raises(httpx.HTTPStatusError):svc.search('복학 신청 경로')
    with store.connect() as db:assert db.execute("select count(*) from cache where key like 'query-vector:%'").fetchone()[0]==0


def test_requirement_and_refund_fields_stay_distinct():
    assert signature('마이크로디그리는 따로 신청해야 해?')==signature('마이크로디그리 이수에는 별도의 신청이 필요한가요?')
    assert signature('마이크로디그리 신청 방법은?')!=signature('마이크로디그리는 따로 신청해야 해?')
    assert signature('학기 중간에 휴학하면 등록금 얼마나 돌려받아?')==signature('학기가 진행되는 도중 휴학할 때 등록금 반환 금액은 어떻게 되나요?')
    assert signature('등록금 환불 금액은?')!=signature('등록금 환불 방법은?')
    assert signature('학사경고 평점 몇 점 미만?')!=signature('학사경고 평점 하한 몇 점?')


def test_timeout_shows_attempts_started_without_claiming_final_total(service):
    started,release=threading.Event(),threading.Event()
    def slow(req):started.set();release.wait(1);raise httpx.ReadTimeout('late')
    answer=generator(service,slow,budget=.05)
    async def run():
        task=asyncio.create_task(answer.answer('이중전공 제도를 설명해줘'))
        await until(started.is_set);result=await task
        assert result['status']=='answer_timeout'
        assert result['timeout_stage']=='generation' and result['retrieval_complete']
        assert result['metrics']['model_calls']==1 and result['metrics']['generation_calls']==1
        assert not result['metrics']['model_calls_complete'] and result['metrics']['model_calls_observed']
        release.set();await until(lambda:service.requests._admitted==0)
    try:asyncio.run(run())
    finally:release.set();answer.close()


def test_location_variant_and_first_term_restriction_use_official_text():
    method={'id':'m','doc_title':'학사정보 > 복학','section':'신청방법','url':'https://example.test/m','text':'- 포털에서 신청'}
    assert direct_answer('복학하려면 어느 곳에서 신청하면 되나요?',[method])['chunk_ids']==['m']
    generic={'id':'p','doc_title':'학사정보 > 일반휴학','section':'신청방법','url':'https://example.test/p','text':'- 포털에서 신청'}
    restriction={'id':'r','doc_title':'학사정보 > 일반휴학','section':'유의사항','url':'https://example.test/r','text':'- 신입학, 편입학 및 재입학생은 질병 또는 군입대 이외의 사유로는 입학 후 첫학기에 휴학할 수 없음'}
    result=direct_answer('신입생 첫 학기 휴학 어떻게 해?',[generic,restriction])
    assert result['chunk_ids']==['r'] and restriction['text'] in result['answer']


def test_identical_query_vector_shared_across_different_filter_flights(retrieval_setup):
    store,fake,r=retrieval_setup;r.build_index();fake.requests.clear()
    svc=AcademicService(store,retriever=r)
    async def run():
        results=await asyncio.gather(*(svc.asearch('복학 신청 방법 알려줘',semester=f'2026-{i%2+1}',target=str(i)) for i in range(4)))
        assert sum(x['metrics']['embedding_calls'] for x in results)==1
        assert sum(x['metrics']['rerank_calls'] for x in results)==4
        assert len(fake.requests)==5
    try:asyncio.run(run())
    finally:svc.close()


def test_shared_timeout_never_duplicates_physical_counts(service):
    started,release=threading.Event(),threading.Event()
    def slow(req):started.set();release.wait(1);raise httpx.ReadTimeout('late')
    answer=generator(service,slow,budget=.06)
    async def run():
        tasks=[asyncio.create_task(answer.answer('이중전공 제도를 설명해줘')) for _ in range(2)]
        await until(started.is_set);await until(lambda:service.requests._waiters==2)
        results=await asyncio.gather(*tasks)
        assert all(r['status']=='answer_timeout' for r in results)
        assert sum(r['metrics']['model_calls'] for r in results)==1
        shared=[r for r in results if r['metrics']['coalesced']]
        assert len(shared)==1 and shared[0]['timeout_stage']=='shared_work'
        assert shared[0]['metrics']['shared_model_calls'] is None
        release.set();await until(lambda:service.requests._admitted==0)
    try:asyncio.run(run())
    finally:release.set();answer.close()
