import asyncio
import json
import threading
import time

import httpx
import pytest

from academic_mcp.answer import FinalAnswers
from academic_mcp.runtime import DataChanged
from academic_mcp.service import AcademicService
from test_service import service  # noqa: F401
from test_runtime import until

MODEL='same-model'


def generator(service,handler=None,**options):
    def good(request):
        payload=json.loads(request.content)
        assert payload['model']==MODEL
        assert 'url' not in payload['messages'][1]['content']  # citations are server-rendered
        return httpx.Response(200,json={'model':MODEL,'choices':[{'finish_reason':'stop','message':{
            'content':json.dumps({'answer':'등록 3학기 이상입니다.','can_answer':True,'sources':[1]})}}]})
    return FinalAnswers(service,base_url='https://model.test/v1',model=MODEL,
                        transport=httpx.MockTransport(handler or good),**options)


def test_complete_answer_citations_and_cache_without_regeneration(service):
    answer=generator(service)
    async def run():
        first=await answer.answer('이중전공 제도를 설명해줘')
        assert first['status']=='answered' and first['display_verbatim']
        assert first['citations'][0].startswith('https://registrar.korea.ac.kr/')
        assert first['metrics']['generation_calls']==1
        first['answer']='caller mutation';first['citations'].clear()
        second=await answer.answer('이중전공 제도를 설명해줘')
        assert second['answer']=='등록 3학기 이상입니다.' and second['citations']
        assert second['metrics']['cache_hit'] and second['metrics']['generation_calls']==0
        assert first['request_id']!=second['request_id']
    asyncio.run(run());answer.close()


@pytest.mark.parametrize('parsed,finish',[
    ({'answer':'허위 링크','can_answer':True,'sources':[999]},'stop'),
    ({'answer':'불완전','can_answer':True,'sources':[1]},'length'),
    ({'answer':'근거 없음','can_answer':True,'sources':[]},'stop'),
    ({'answer':'참조 오류','can_answer':True,'sources':[True]},'stop'),
])
def test_invalid_or_truncated_answers_never_enter_cache(service,parsed,finish):
    def handler(req):return httpx.Response(200,json={'model':MODEL,'choices':[{'finish_reason':finish,
        'message':{'content':json.dumps(parsed)}}]})
    answer=generator(service,handler)
    async def run():
        with pytest.raises(ValueError):await answer.answer('이중전공 제도를 설명해줘')
        with service.store.connect() as db:assert db.execute("SELECT count(*) FROM cache WHERE key LIKE 'answer:%'").fetchone()[0]==0
    asyncio.run(run());answer.close()


def test_deadline_is_failed_completion_and_late_worker_cannot_cache(service):
    started,release=threading.Event(),threading.Event()
    def slow(req):
        started.set();release.wait(1)
        return httpx.Response(200,json={'model':MODEL,'choices':[{'finish_reason':'stop','message':{
            'content':json.dumps({'answer':'늦은 답','can_answer':True,'sources':[1]})}}]})
    answer=generator(service,slow,budget=.05)
    async def run():
        task=asyncio.create_task(answer.answer('이중전공 제도를 설명해줘'))
        await until(started.is_set)
        result=await task
        assert result['status']=='answer_timeout' and result['answer'] is None
        assert result['metrics']['server_elapsed_ms']<300
        release.set();await until(lambda:service.requests._admitted==0)
        with service.store.connect() as db:assert db.execute("SELECT count(*) FROM cache WHERE key LIKE 'answer:%'").fetchone()[0]==0
    try:asyncio.run(run())
    finally:release.set();answer.close()


def test_revision_change_during_generation_rejects_answer(service):
    started,release=threading.Event(),threading.Event()
    def handler(req):
        started.set();release.wait(1)
        return httpx.Response(200,json={'model':MODEL,'choices':[{'finish_reason':'stop','message':{
            'content':json.dumps({'answer':'구 규정','can_answer':True,'sources':[1]})}}]})
    answer=generator(service,handler)
    async def run():
        task=asyncio.create_task(answer.answer('이중전공 제도를 설명해줘'))
        await until(started.is_set)
        _,chunks,events=service.store.snapshot();chunks[0]['text']+='변경'
        service.store.replace(chunks,events);release.set()
        with pytest.raises(DataChanged):await task
        with service.store.connect() as db:assert db.execute("SELECT count(*) FROM cache WHERE key LIKE 'answer:%'").fetchone()[0]==0
    try:asyncio.run(run())
    finally:release.set();answer.close()


def test_final_generation_coalesces_and_one_cancellation_keeps_other_callers(service):
    calls=[];started,release=threading.Event(),threading.Event()
    def handler(req):
        calls.append(req);started.set();release.wait(1)
        return httpx.Response(200,json={'model':MODEL,'choices':[{'finish_reason':'stop','message':{
            'content':json.dumps({'answer':'공유한 완성 답','can_answer':True,'sources':[1]})}}]})
    answer=generator(service,handler)
    async def run():
        tasks=[asyncio.create_task(answer.answer('이중전공 제도를 설명해줘')) for _ in range(8)]
        await until(started.is_set);await until(lambda:service.requests._waiters==8)
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):await tasks[0]
        release.set();results=await asyncio.gather(*tasks[1:])
        assert len(calls)==1 and all(r['status']=='answered' for r in results)
        assert len({r['request_id'] for r in results})==7
        assert sum(r['metrics']['generation_calls'] for r in results)<=1
    try:asyncio.run(run())
    finally:release.set();answer.close()


def test_private_lookup_never_calls_answer_api(service):
    answer=generator(service,lambda req:pytest.fail('Private data must not call model'))
    result=asyncio.run(answer.answer('내 성적 알려줘'))
    assert result['status']=='login_required' and not result['can_answer']
    assert result['metrics']['generation_calls']==0
    answer.close()


def test_short_completion_protocol_avoids_generated_url_and_json(service):
    def handler(req):
        fmt=json.loads(req.content)['response_format']
        assert fmt['type']=='json_schema' and fmt['json_schema']['strict']
        assert fmt['json_schema']['schema']['required']==['answer','can_answer','sources']
        return httpx.Response(200,json={'model':MODEL,'choices':[{'finish_reason':'stop','message':{
            'content':'등록 3학기 이상입니다.\n근거:1'}}]})
    answer=generator(service,handler)
    result=asyncio.run(answer.answer('이중전공 제도를 설명해줘'))
    assert result['status']=='answered'
    assert result['answer']=='등록 3학기 이상입니다.'
    assert result['citations']
    answer.close()


def test_abstention_with_spacing_is_not_a_positive_answer(service):
    def handler(req):return httpx.Response(200,json={'model':MODEL,'choices':[{'finish_reason':'stop','message':{
        'content':'확인 불가: 제공된 자료에 없습니다.\n근거: 없음'}}]})
    answer=generator(service,handler)
    result=asyncio.run(answer.answer('이중전공 합격컷'))
    assert result['status']=='insufficient_evidence' and not result['can_answer']
    assert not result['citations']
    answer.close()


def test_completed_calendar_answer_never_calls_generation_api(service):
    answer=generator(service,lambda req:pytest.fail('Calendar final text must not call model'))
    result=asyncio.run(answer.answer('2026-2 이중전공 신청 언제?'))
    assert result['status']=='answered' and result['answer_mode']=='structured_calendar'
    assert '2026-11-11' in result['answer'] and '2026-11-13' in result['answer']
    assert result['citations'] and result['metrics']['generation_calls']==0
    answer.close()


@pytest.mark.parametrize('text,status',[
    ('등록 3학기 이상입니다. 근거:1','answered'),
    ('제공된 원문에 없음\n확인불가: 근거:없음','insufficient_evidence'),
])
def test_inline_source_protocol_from_fast_model(service,text,status):
    def handler(req):return httpx.Response(200,json={'model':MODEL,'choices':[{'finish_reason':'stop','message':{'content':text}}]})
    answer=generator(service,handler);result=asyncio.run(answer.answer('이중전공 제도를 설명해줘'))
    assert result['status']==status
    answer.close()


def test_real_mcp_cli_displays_completed_text_without_second_model(service,monkeypatch,capsys):
    import socket
    import uvicorn
    from academic_mcp.answer_cli import run
    from academic_mcp.server import create_server
    calls=[]
    def handler(req):
        calls.append(req)
        return httpx.Response(200,json={'model':MODEL,'choices':[{'finish_reason':'stop','message':{
            'content':json.dumps({'answer':'등록 3학기 이상입니다.','can_answer':True,'sources':[1]})}}]})
    original=FinalAnswers
    monkeypatch.setenv('ANSWER_BASE_URL','https://model.test/v1')
    monkeypatch.setenv('ANSWER_MODEL',MODEL)
    monkeypatch.setattr('academic_mcp.server.build_service',lambda:service)
    monkeypatch.setattr('academic_mcp.answer.FinalAnswers',lambda svc,**kw:original(svc,transport=httpx.MockTransport(handler),**kw))
    async def check():
        sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(create_server(port=port).streamable_http_app(),log_level='error'))
        task=asyncio.create_task(server.serve(sockets=[sock]))
        try:
            await until(lambda:server.started)
            assert await run(f'http://127.0.0.1:{port}/mcp','이중전공 제도를 설명해줘')==0
            assert len(calls)==1
        finally:
            server.should_exit=True;await task;sock.close()
    asyncio.run(check())
    output=capsys.readouterr().out
    assert '등록 3학기 이상입니다.' in output
    assert 'https://registrar.korea.ac.kr/' in output


def test_reasoning_option_is_in_request_and_cache_identity(service):
    def handler(req):
        assert json.loads(req.content)['reasoning_effort']=='low'
        return httpx.Response(200,json={'model':MODEL,'choices':[{'finish_reason':'stop','message':{
            'content':json.dumps({'answer':'확인 불가','can_answer':False,'sources':[]})}}]})
    low=generator(service,handler,reasoning_effort='low');normal=generator(service)
    assert low.key('질문',[],5,None,None)!=normal.key('질문',[],5,None,None)
    result=asyncio.run(low.answer('이중전공 제도를 설명해줘'))
    assert not result['can_answer']
    low.close();normal.close()


@pytest.mark.parametrize('history',[False,0,''])
def test_invalid_history_is_not_silently_ignored(service,history):
    answer=generator(service)
    async def run():
        with pytest.raises(ValueError):await answer.answer('이중전공 제도를 설명해줘',history=history)
    asyncio.run(run());answer.close()


def test_one_format_repair_uses_same_sources_and_counts_both_calls(service):
    requests=[]
    def handler(req):
        payload=json.loads(req.content);requests.append(payload)
        content={'analysis':'not a final answer'} if len(requests)==1 else {'answer':'등록 3학기 이상입니다.','can_answer':True,'sources':[1]}
        return httpx.Response(200,json={'model':MODEL,'choices':[{'finish_reason':'stop','message':{'content':json.dumps(content)}}]})
    answer=generator(service,handler)
    result=asyncio.run(answer.answer('이중전공 제도를 설명해줘'))
    assert result['status']=='answered' and result['metrics']['generation_calls']==2
    assert len(requests)==2
    assert requests[0]['messages'][1]==requests[1]['messages'][1]
    assert 'analysis' not in result['answer']
    answer.close()


def test_literal_field_preserves_whole_item_and_never_infers_narrow_scope():
    from academic_mcp.direct_answer import direct_answer
    row={'id':'credits','doc_title':'교육정보 > 졸업 > 졸업요구조건','section':'졸업요구 학점',
         'text':'- 졸업요구학점은 130학점\n- 단, 의학과는 172학점', 'url':'https://example.edu/rules'}
    result=direct_answer('졸업하려면 총 몇 학점?', [row])
    assert row['text'] in result['answer'] and result['citations']==[row['url']]
    assert result['answer_mode']=='source_item'
    assert direct_answer('2027년 졸업 총 몇 학점?', [row]) is None
    assert direct_answer('졸업 신청 방법과 몇 학점?', [row]) is None
    wrong={**row,'doc_title':'교육정보 > 수강신청 > 현장실습지원센터','section':'학점','text':'- 최대 6학점'}
    assert direct_answer('계절학기는 최대 몇 학점?', [wrong]) is None


def test_explicit_empty_abstention_is_not_retried_or_counted_as_answer(service):
    calls=[]
    def handler(req):
        calls.append(req)
        return httpx.Response(200,json={'model':MODEL,'choices':[{'finish_reason':'stop','message':{
            'content':json.dumps({'answer':'','can_answer':False,'sources':[]})}}]})
    answer=generator(service,handler)
    result=asyncio.run(answer.answer('이중전공 합격컷'))
    assert result['status']=='insufficient_evidence' and not result['can_answer']
    assert result['answer']=='검색된 자료만으로는 질문의 핵심을 확인하지 못했습니다.'
    assert len(calls)==1 and result['metrics']['generation_calls']==1
    answer.close()


def test_literal_lookup_selects_named_field_not_first_transition_or_narrow_scope():
    from academic_mcp.direct_answer import direct_answer
    transition={'id':'transition','doc_title':'교육정보 > 성적 > 재수강','section':'경과조치',
        'text':'이 규정의 최대 B+는 2014년부터 적용','url':'https://example.edu/retake'}
    cap={**transition,'id':'cap','section':'재수강 성적처리','text':'- 재수강 최고 A(4.0)\n- 다시 재수강 최고 B+(3.5)'}
    assert direct_answer('재수강 최고 성적은?', [transition,cap])['chunk_ids']==['cap']
    narrow={'id':'narrow','doc_title':'교육 > 계절수업 > 현장실습','section':'학점',
        'text':'- 최대 6학점','url':'https://example.edu/narrow'}
    broad={'id':'broad','doc_title':'교육 > 계절수업등록','section':'신청가능 학점',
        'text':'- 최대 9학점','url':'https://example.edu/broad'}
    assert direct_answer('계절학기는 최대 몇 학점?', [narrow,broad])['chunk_ids']==['broad']


def test_field_word_alone_is_not_topic_match_and_plain_atomic_item_is_allowed():
    from academic_mcp.direct_answer import direct_answer
    foreign={'id':'foreign','doc_title':'교육프로그램 > 국내대학 학점교류(IN)','section':'학점교류 기준',
        'text':'- 학기당 최대 9학점','url':'https://example.edu/incoming'}
    season={'id':'season','doc_title':'등록금 > 계절수업등록','section':'신청가능 학점',
        'text':'학기당 최대 9학점 (휴학생도 신청 가능)','url':'https://example.edu/season'}
    assert direct_answer('계절학기는 최대 몇 학점?', [foreign]) is None
    assert direct_answer('계절학기는 최대 몇 학점?', [foreign,season])['chunk_ids']==['season']


def test_named_procedure_and_eligibility_fields_keep_complete_conditions():
    from academic_mcp.direct_answer import direct_answer
    identity={'id':'id','doc_title':'학사정보 > 학생증 / 신분증','section':'금융 학생증 재발급',
        'text':'- 재학증명서와 신분증 제출\n- 최대 7영업일, 수수료 5000원','url':'https://example.edu/id'}
    assert direct_answer('학생증 잃어버리면 어떻게 다시 받아?', [identity])['answer'].endswith(identity['text'])
    assert direct_answer('학생증 처음 어떻게 받아?', [identity]) is None
    major={'id':'major','doc_title':'교육정보 > 이중전공','section':'신청자격',
        'text':'- 재학생 3학기 이상\n- 편입생 2학기 이상','url':'https://example.edu/major'}
    assert direct_answer('이중전공 몇 학기 뒤 신청해?', [major])['chunk_ids']==['major']
    assert direct_answer('이중전공 자격과 신청 방법?', [major]) is None


def test_long_named_field_match_never_chooses_unrelated_withdrawal():
    from academic_mcp.direct_answer import direct_answer
    drop={'id':'drop','doc_title':'교육정보 > 수강신청 절차','section':'수강신청 포기',
        'text':'- 수강포기 학점은 이월 불가','url':'https://example.edu/course'}
    carry={**drop,'id':'carry','section':'유연 수강신청 학점제','text':'- 학년도 내 최대 3학점 선사용 또는 이월'}
    q='안 쓴 수강신청 학점을 다음 학기로 넘길 수 있어?'
    assert direct_answer(q,[drop,carry])['chunk_ids']==['carry']
