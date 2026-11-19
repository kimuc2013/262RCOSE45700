from academic_mcp.context import build_context
from academic_mcp.service import AcademicService
from academic_mcp.store import Store


def chunk(id, title, section, text, *, url='https://example.edu/rules', doc_id='rules'):
    return dict(id=id, doc_id=doc_id, doc_title=title, section=section, text=text,
                url=url, collected_at='2026-11-20')


def test_context_keeps_conditions_whole_and_exposes_omissions():
    rows=[chunk('a','학점','조건','10학점 이내. 단, 편입생은 5학점.'),
          chunk('b','학점','서류','필수 제출 서류는 신청서와 증명서입니다.')]
    result=build_context('학점 몇?',rows,rows,max_chars=40)
    assert result['omitted_chunk_ids']
    text=''.join(s['text'] for s in result['sources'])
    for row in rows:
        assert row['text'] in text or row['id'] in result['omitted_chunk_ids']


def test_complementary_procedure_and_exceptions_keep_sources_and_originals():
    rows=[chunk('rule','전공','정의','제2전공 안내'),
          chunk('steps','전공','신청절차','포털 신청 후 학과 승인을 받아야 합니다.'),
          chunk('exception','전공','신청대상','편입생은 별도 조건을 따릅니다.')]
    result=build_context('전공 신청 자격과 방법은?',rows[:1],rows)
    assert set(result['sources'][0]['chunk_ids'])=={'rule','steps','exception'}
    assert set(result['sources'][0]['supplemental_chunk_ids'])=={'steps','exception'}
    assert result['sources'][0]['url']==rows[0]['url']
    assert rows[0]['text']=='제2전공 안내'


def test_same_doc_id_never_merges_different_scope_or_url():
    rows=[chunk('general','계절수업','학점','최대 9학점'),
          chunk('practice','현장실습','학점','최대 6학점',url='https://example.edu/practice')]
    result=build_context('계절수업 학점?',rows,rows)
    assert len(result['sources'])==2
    assert result['sources'][0]['title']=='계절수업'
    assert '6학점' not in result['sources'][0]['text']


def test_service_preserves_module_decision_and_refreshes_context(tmp_path):
    store=Store(str(tmp_path/'data.sqlite3'))
    rows=[chunk('rule','전공','정의','제2전공 안내'),chunk('steps','전공','신청방법','포털 신청')]
    store.replace(rows,[])
    class Retriever:
        use_reranker=True
        def profile(self):return {'fixed':True}
        def search(self,query,top_k,**kw):
            return {'status':'evidence_available','score_type':'reranker','min_rerank_score':None,
                    'evidence':[{**kw['chunks'][0],'score':0.7}]}
    service=AcademicService(store,retriever=Retriever(),answer_context=True)
    result=service.search('전공 신청 어떻게?')
    assert result['evidence'][0]['text']=='제2전공 안내'
    assert result['evidence'][0]['score']==0.7
    assert result['answer_context']['sources'][0]['supplemental_chunk_ids']==['steps']
    result['answer_context']['sources'][0]['text']='mutated'
    assert 'mutated' not in service.search('전공 신청 어떻게?')['answer_context']['sources'][0]['text']
    rows[1]['text']='학과 제출'
    store.replace(rows,[])
    updated=service.search('전공 신청 어떻게?')
    assert not updated['cache_hit'] and '학과 제출' in updated['answer_context']['sources'][0]['text']


def test_explicit_calendar_route_skips_models_and_preserves_target_semester(tmp_path):
    from academic_mcp.calendar_route import calendar_request
    assert calendar_request('이중전공 신청 언제?',None) is None
    assert calendar_request('2027-1 이중전공 신청 자격과 기간?',None) is None
    assert calendar_request('2027-1 이중전공 신청 언제?','2026-2') is None
    store=Store(str(tmp_path/'calendar.sqlite3'))
    event=dict(name='이중전공 신청',start='2026-11-01',end='2026-11-03',
               semester='2026-2',target_semester='2027-1',url='https://example.edu/calendar',collected_at='2026-11-20')
    store.replace([], [event])
    class Unavailable:
        def profile(self):return {}
        def search(self,*a,**k):raise AssertionError('Calendar route must not call the model')
    result=AcademicService(store,retriever=Unavailable()).search('2027-1 이중전공 신청 언제?')
    assert result['retrieval_mode']=='structured_calendar'
    assert result['events'][0]['semester']=='2026-2'
    assert result['events'][0]['target_semester']=='2027-1'
    assert result['metrics']['model_calls']==0


def test_supplemental_sections_never_evict_retrieved_table():
    table=chunk('table','등록금','납부금액','| 1~3학점 | 수업료 1/6 |')
    second=chunk('second','다른 문서','예외','예외 조건 원문',url='https://example.edu/other')
    extras=[chunk(str(i),'등록금','신청대상','별도 대상 설명 '+str(i)) for i in range(10)]
    result=build_context('등록금 신청 몇 학점?', [table,second], [table,second]+extras,max_chars=100)
    ids={id for s in result['sources'] for id in s['chunk_ids']}
    assert {'table','second'} <= ids


def test_independent_retrieval_shares_across_histories_but_keeps_scope(tmp_path):
    store=Store(str(tmp_path/'shared.sqlite3'))
    rows=[chunk('a','전공','자격','등록 3학기 이상')];store.replace(rows,[])
    class Retriever:
        use_reranker=True
        calls=0
        def profile(self):return {'version':1}
        def search(self,q,top_k,**kw):
            self.calls+=1
            return {'status':'evidence_available','score_type':'reranker','min_rerank_score':None,'evidence':kw['chunks']}
    retriever=Retriever();service=AcademicService(store,retriever=retriever)
    first=service.search('전공 자격',[{'role':'user','content':'서로 다른 대화 A'}])
    second=service.search('전공 자격',[{'role':'user','content':'서로 다른 대화 B'}])
    assert second['cache_hit'] and retriever.calls==1
    assert first['evidence']==second['evidence']
    assert 'answer_context' not in first
    assert not service.search('전공 자격',target='편입생')['cache_hit']
    assert not service.search('전공 자격',semester='2027-1')['cache_hit']
    # Model-free/legacy modes retain their conservative history cache contract.
    legacy=AcademicService(store)
    assert legacy.cache_key('전공 자격',['A'],5,None,None)!=legacy.cache_key('전공 자격',['B'],5,None,None)


def test_broad_query_never_adds_unrequested_special_program():
    general=chunk('general','졸업','일반 조기졸업','일반 졸업 조건 원문')
    special=chunk('special','졸업','특별 조기졸업','별도 과정 제출 서류 원문')
    result=build_context('조기졸업 조건은?', [general], [general,special])
    assert result['sources'][0]['chunk_ids']==['general']


def test_title_token_coverage_surfaces_general_scope_before_unrelated_center():
    narrow=chunk('center','교육 > 단과대학 유의사항 > 현장실습지원센터','학점','계절학기 6학점',url='https://example.edu/center')
    general=chunk('general','교육 > 계절수업','학점','계절수업 9학점')
    result=build_context('계절학기 최대 학점?', [narrow,general],[narrow,general])
    assert result['sources'][0]['chunk_ids']==['general']
    assert 'center' in result['sources'][1]['chunk_ids']
