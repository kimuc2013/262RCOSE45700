import asyncio
import json
from datetime import date
import pytest
from academic_mcp.answer_aliases import intent,polite_query,proof,source_item
from test_service import service  # noqa: F401
from test_final_answers import generator


@pytest.mark.parametrize('a,b',[
 ('복학 신청은 어디서 해?','복학 신청을 접수하는 경로가 궁금해요.'),
 ('이중전공은 몇 학기 다닌 뒤에 신청할 수 있어?','몇 학기를 다녀야 이중전공을 신청할 수 있나요?'),
 ('전과 신청 자격 알려줘','전과에 지원할 수 있는 자격 조건을 알려주세요.'),
 ('재수강하면 받을 수 있는 최고 성적이 뭐야?','재수강 과목에서 취득 가능한 성적의 상한은 무엇인가요?'),
])
def test_whole_question_proofs(a,b):
    assert proof(a) and proof(a)==proof(b)


@pytest.mark.parametrize('q',[
 '2027년 이중전공 신청 자격 알려줘','편입생 이중전공 신청 자격 알려줘',
 '이중전공 신청 자격 3학기 이상 알려줘','이중전공 신청 자격 3학기 이하 알려줘',
 '이중전공 신청 불가 자격 알려줘','내 이중전공 신청 자격 알려줘',
 '세종 이중전공 신청 자격 알려줘','이중전공 신청 자격 알려줘, 예외도',
 '이중전공 합격 커트라인 알려줘','복수전공 신청 자격 알려줘',
])
def test_conditions_are_never_erased(q):
    assert proof(q)!=proof('이중전공 신청 자격 알려줘')
    assert source_item(q,[]) is None


def test_politeness_preserves_sensitive_punctuation_and_numbers():
    assert polite_query('이중전공 제도를 설명해주세요.')==polite_query('이중전공 제도를 설명해줘')
    for q in ['3.5 이상','3.50 이상','3.5 이하','A+ 성적','A 성적','2026-1 신청','2026-2 신청','신청 안 해도 돼']:
        assert polite_query(q)!=polite_query('이중전공 제도를 설명해줘')
    assert polite_query('A+ 성적 알려줘')!=polite_query('A 성적 알려줘')


def no_direct(monkeypatch):
    monkeypatch.setattr('academic_mcp.answer.literal_lookup',lambda *a,**k:None)
    monkeypatch.setattr('academic_mcp.answer.direct_answer',lambda *a,**k:None)


def test_polite_answer_cache_skips_generation(service,monkeypatch):
    no_direct(monkeypatch);answer=generator(service)
    async def run():
        a=await answer.answer('이중전공 제도를 설명해줘')
        b=await answer.answer('이중전공 제도를 설명해주세요.')
        assert a['metrics']['generation_calls']==1
        assert b['metrics']['cache_hit'] and b['metrics']['model_calls']==0
        assert b['metrics']['model_free_response'] is True
        assert b['answer']==a['answer']
    try:asyncio.run(run())
    finally:answer.close()


def test_verified_alias_ttl_eviction_and_context(service,monkeypatch):
    no_direct(monkeypatch);now=[1000.0];service.clock=lambda:now[0];answer=generator(service)
    q='이중전공은 몇 학기 다닌 뒤에 신청할 수 있어?'
    alias='몇 학기를 다녀야 이중전공을 신청할 수 있나요?'
    async def run():
        original=await answer.answer(q)
        assert original['metrics']['generation_calls']==1
        now[0]+=10
        hit=await answer.answer(alias)
        assert hit['metrics']['answer_cache_kind']=='verified_alias'
        assert hit['metrics']['cache_hit'] and hit['metrics']['model_calls']==0
        assert hit['answer']==original['answer'] and hit['citations']==original['citations']
        with service.store.connect() as db:
            expires=[r[0] for r in db.execute("select expires from cache where key like 'answer:%' or key like 'answer-alias:%'")]
        assert expires==[1300.0,1300.0]
        for kw in [{'top_k':2},{'semester':'2027-1'},{'target':'편입생'},{'history':[{'role':'user','content':'이전 문맥'}]}]:
            isolated=await answer.answer(alias,**kw)
            assert not isolated['metrics']['cache_hit']
        with service.store.connect() as db:
            db.execute('DELETE FROM cache WHERE key=?',(answer.key(q,[],5,None,None),))
        regenerated=await answer.answer(alias)
        assert not regenerated['metrics']['cache_hit'] and regenerated['metrics']['generation_calls']==1
        now[0]+=301
        expired=await answer.answer(q)
        assert not expired['metrics']['cache_hit']
    try:asyncio.run(run())
    finally:answer.close()


def test_alias_revision_and_ambiguous_source_safety(service,monkeypatch):
    no_direct(monkeypatch);answer=generator(service)
    async def run():
        await answer.answer('이중전공은 몇 학기 다닌 뒤에 신청할 수 있어?')
        _,chunks,events=service.store.snapshot();chunks[0]['text']+=' 바뀐 규정'
        service.store.replace(chunks,events)
        result=await answer.answer('몇 학기를 다녀야 이중전공을 신청할 수 있나요?')
        assert not result['metrics']['cache_hit']
    try:asyncio.run(run())
    finally:answer.close()
    row={'id':'x','doc_id':'major_double','section':'신청자격','text':'공식 자격 전체','url':'https://official.test/x','doc_title':'이중전공'}
    assert source_item('이중전공 신청자격 알려줘',[row,dict(row,id='y')]) is None
    assert source_item('이중전공 신청자격 알려줘',[row],semester='2027-1') is None
    assert source_item('이중전공 신청자격 알려줘',[dict(row,extraction_method='none')]) is None


def test_paraphrase_flights_share_work_but_not_zero_model_credit(service,monkeypatch):
    import threading
    import httpx
    from test_runtime import until
    no_direct(monkeypatch);started,release=threading.Event(),threading.Event()
    def handler(req):
        started.set();release.wait(1)
        return httpx.Response(200,json={'model':'same-model','choices':[{'finish_reason':'stop',
            'message':{'content':json.dumps({'answer':'등록 3학기 이상입니다.','can_answer':True,'sources':[1]})}}]})
    answer=generator(service,handler)
    async def run():
        first=asyncio.create_task(answer.answer('이중전공은 몇 학기 다닌 뒤에 신청할 수 있어?'))
        await until(started.is_set)
        second=asyncio.create_task(answer.answer('몇 학기를 다녀야 이중전공을 신청할 수 있나요?'))
        await asyncio.sleep(.02);release.set()
        results=await asyncio.gather(first,second)
        assert sum(r['metrics']['generation_calls'] for r in results)==1
        assert sum(r['metrics']['coalesced'] for r in results)==1
        assert all(r['metrics']['model_free_response'] is False for r in results)
    try:asyncio.run(run())
    finally:release.set();answer.close()


def test_abstentions_never_publish_cross_expression_aliases(service,monkeypatch):
    import httpx
    no_direct(monkeypatch)
    def handler(req):
        return httpx.Response(200,json={'model':'same-model','choices':[{'finish_reason':'stop',
            'message':{'content':json.dumps({'answer':'확인 불가','can_answer':False,'sources':[]})}}]})
    answer=generator(service,handler)
    async def run():
        first=await answer.answer('이중전공은 몇 학기 다닌 뒤에 신청할 수 있어?')
        assert first['can_answer'] is False
        second=await answer.answer('몇 학기를 다녀야 이중전공을 신청할 수 있나요?')
        assert second['metrics']['generation_calls']==1 and not second['metrics']['cache_hit']
        with service.store.connect() as db:
            assert db.execute("select count(*) from cache where key like 'answer-alias:%'").fetchone()[0]==0
    try:asyncio.run(run())
    finally:answer.close()


def test_application_criteria_are_distinct_from_final_graduation_requirements():
    assert proof('조기졸업 조건 알려줘')
    assert proof('조기졸업 신청 자격 알려줘')
    assert proof('조기졸업 조건 알려줘')!=proof('조기졸업 신청 자격 알려줘')
    assert proof('졸업 조건 알려줘')!=proof('졸업 신청 자격 알려줘')
    assert proof('이중전공하려면 어떻게 해야 돼?') is None
    assert proof('이중전공은 어디서 해?') is None
    assert proof('복학하려면 어떻게 해야 돼?')==proof('복학 신청 절차 알려줘')
