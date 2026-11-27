import asyncio
from datetime import date,timedelta
import json

from academic_mcp.service import AcademicService
from academic_mcp.cache_policy import UNTIL_REVISION, date_sensitive
from academic_mcp.answer_aliases import proof
from test_retrieval import setup as retrieval_setup
from test_service import service
from test_final_answers import generator


def ready(setup):
    store,fake,r=setup;r.build_index();fake.requests.clear()
    now=[1000.];day=[date(2026,11,27)]
    s=AcademicService(store,retriever=r,revision_cache=True,semantic_cache=True,clock=lambda:now[0],today=lambda:day[0])
    return s,fake,now,day


def test_static_entries_survive_ttl_day_and_service_recreation(retrieval_setup):
    s,fake,now,day=ready(retrieval_setup)
    q='이중전공 신청자격 알려줘';a=s.search(q)
    now[0]+=86400*200;day[0]+=timedelta(days=200)
    other=AcademicService(s.store,retriever=s.retriever,revision_cache=True,semantic_cache=True,clock=s.clock,today=s.today)
    b=other.search(q,history=[{'role':'user','content':'다른 세션'}])
    assert b['cache_hit'] and b['metrics']['model_calls']==0 and b['freshness_warning']
    assert len(fake.requests)==2 and [e['id'] for e in a['evidence']]==[e['id'] for e in b['evidence']]
    with s.store.connect() as db:assert db.execute('select max(expires) from cache').fetchone()[0]==UNTIL_REVISION


def test_verified_search_alias_has_no_embedding_or_rerank_and_keeps_conditions(retrieval_setup):
    s,fake,now,day=ready(retrieval_setup)
    s.search('이중전공 신청자격 알려줘',semester='2026-2',target='학부')
    now[0]+=3600*24*2;day[0]+=timedelta(days=2)
    b=s.search('이중전공을 신청하려면 어떤 자격이 필요해?',semester='2026-2',target='학부')
    assert b['metrics']['retrieval_cache_hit'] and b['metrics']['retrieval_cache_kind']=='verified_intent'
    assert b['metrics']['model_calls']==0 and len(fake.requests)==2
    assert b['score_type']=='cached_reranker' and all(e['score_type']=='cached_reranker' for e in b['evidence'])
    assert b['semester']=='2026-2' and b['target']=='학부'
    for q in ['복수전공 신청자격 알려줘','편입생 이중전공 신청자격 알려줘','이중전공 신청 기간 알려줘']:
        assert not s.search(q)['metrics']['retrieval_cache_hit']


def test_relative_questions_stay_daily_and_short_lived(retrieval_setup):
    s,fake,now,day=ready(retrieval_setup)
    q='이번 학기 이중전공 신청 시기 알려줘';s.search(q)
    assert s.search(q)['cache_hit']
    now[0]+=301
    assert not s.search(q)['cache_hit']  # response expired, ranked result remains within same day
    day[0]+=timedelta(days=1)
    b=s.search(q)
    assert not b['cache_hit'] and not b['metrics']['retrieval_cache_hit']
    assert b['metrics']['embedding_cache_hit']  # The exact query vector is date independent.
    assert all(date_sensitive(x) for x in ['오늘','이번 학기','다음 학기','2026년 신청일','언제 복학해?','이중전공 신청 기간 알려줘'])


def test_model_profile_and_snapshot_changes_invalidate_persistent_results(retrieval_setup):
    s,fake,now,day=ready(retrieval_setup);q='이중전공 신청자격 알려줘';s.search(q)
    s.model_revision='new-provider-build'
    assert s.search(q)['metrics']['model_calls']==2
    old,chunks,events=s.store.snapshot();chunks[0]['text']+=' 수정된 공식 규정'
    s.store.replace(chunks,events);s.retriever.build_index();fake.requests.clear()
    b=s.search(q)
    assert b['revision']!=old and not b['cache_hit'] and b['metrics']['model_calls']==2


def test_thresholded_retriever_never_bypasses_judgement_for_alias(retrieval_setup):
    s,fake,now,day=ready(retrieval_setup);s.retriever.min_rerank_score=.5
    s.search('이중전공 신청자격 알려줘')
    b=s.search('이중전공을 신청하려면 어떤 자격이 필요해?')
    assert not b['metrics']['retrieval_cache_hit'] and b['metrics']['model_calls']==2


def test_scoped_credit_equivalence_retains_department_and_requested_field():
    a='컴퓨터학과 이중전공 졸업요건 이수학점'
    assert proof(a)==proof('컴퓨터학과 이중전공 졸업하려면 몇 학점을 이수해야 해?') and proof(a)
    for q in ['수학과 이중전공 졸업요건 이수학점','컴퓨터학과 복수전공 졸업요건 이수학점','컴퓨터학과 이중전공 졸업요건','컴퓨터학과 이중전공 졸업요건 24학점','2027학번 컴퓨터학과 이중전공 졸업요건 이수학점']:
        assert proof(a)!=proof(q)


def test_final_static_answer_freshness_is_recomputed_without_expiring(service):
    service.revision_cache=True;now=[1000.];day=[date(2026,10,28)]
    service.clock=lambda:now[0];service.today=lambda:day[0];a=generator(service)
    async def run():
        first=await a.answer('이중전공 신청자격 알려줘')
        now[0]+=86400*200;day[0]+=timedelta(days=200)
        second=await a.answer('이중전공 신청자격 알려줘')
        assert second['metrics']['cache_hit'] and second['metrics']['model_calls']==0
        assert not first['freshness_warning'] and second['freshness_warning']
        assert '_freshness_info' not in second and '_cache_day' not in second
    try:asyncio.run(run())
    finally:a.close()


def test_generated_relative_wording_never_survives_midnight(service,monkeypatch):
    import httpx
    from test_final_answers import MODEL
    service.revision_cache=True;day=[date(2026,10,28)];service.today=lambda:day[0]
    monkeypatch.setattr('academic_mcp.answer.literal_lookup',lambda *a,**k:None)
    monkeypatch.setattr('academic_mcp.answer.direct_answer',lambda *a,**k:None)
    def handler(request):return httpx.Response(200,json={'model':MODEL,'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'answer':'현재 안내된 규정입니다.','can_answer':True,'sources':[1]})}}]})
    a=generator(service,handler)
    async def run():
        await a.answer('이중전공 제도를 설명해줘')
        day[0]+=timedelta(days=1)
        b=await a.answer('이중전공 제도를 설명해줘')
        assert not b['metrics']['cache_hit'] and b['metrics']['generation_calls']==1
    try:asyncio.run(run())
    finally:a.close()


def test_public_field_paraphrases_share_evidence_but_not_generated_answer_proof(retrieval_setup):
    from academic_mcp.answer_aliases import retrieval_proof
    s,fake,now,day=ready(retrieval_setup)
    q='현장실습 6학점 받으려면 며칠 일해야 해?'
    other='현장실습 6학점을 인정받으려면 필요한 실제 근무일수가 며칠인가요?'
    assert retrieval_proof(q)==retrieval_proof(other) and retrieval_proof(q)
    assert proof(q) is None and proof(other) is None
    assert retrieval_proof(q)!=retrieval_proof('현장실습 3학점 받으려면 며칠 일해야 해?')
    s.search(q);now[0]+=86400*2;day[0]+=timedelta(days=2)
    b=s.search(other)
    assert b['metrics']['retrieval_cache_kind']=='verified_intent' and b['metrics']['model_calls']==0
    assert not date_sensitive(q) and not date_sensitive(other)


def test_relative_date_detection_matches_unicode_cache_identity():
    import unicodedata
    for q in ['오늘 이중전공 신청 가능해?','이중전공 신청 기간 알려줘']:
        assert date_sensitive(q) and date_sensitive(unicodedata.normalize('NFD',q))
