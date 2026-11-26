import json
import pytest
from academic_mcp.service import AcademicService
from academic_mcp.semantic_cache import signature, normalized
from test_retrieval import setup as retrieval_setup

@pytest.mark.parametrize('a,b',[
 ('이중전공 신청 자격은?','복수전공 신청 자격은?'),
 ('현장실습 6학점에 필요한 근무일은?','현장실습 3학점에 필요한 근무일은?'),
 ('2026년 2학기 이중전공 신청 기간은?','2027년 1학기 이중전공 신청 기간은?'),
 ('신입생 첫 학기 휴학 가능해?','편입생 첫 학기 휴학 가능해?'),
 ('재수강하면 A+ 받을 수 있어?','재수강하면 A 받을 수 있어?'),
 ('학기 중 휴학하면 등록금 환불돼?','학기 중 휴학하지 않으면 등록금 환불돼?'),
 ('서울캠퍼스 재학증명서 발급 방법은?','세종캠퍼스 재학증명서 발급 방법은?'),
 ('졸업 조건은?','조기졸업 조건은?'),
 ('컴퓨터학과 졸업 요건은?','수학과 졸업 요건은?'),
 ('휴학 신청 방법은?','휴학 신청 자격은?'),
 ('고려대 복학 신청 방법은?','연세대 복학 신청 방법은?'),
 ('휴학 신청 방법은?','질병 휴학 신청 방법은?'),
 ('휴학 가능해?','휴학 불가능하지 않아?'),
])
def test_changed_conditions_never_share_signature(a,b):
    assert signature(a)!=signature(b) or signature(a) is None

def ready(setup,**kw):
    store,fake,r=setup;r.build_index();fake.requests.clear()
    return AcademicService(store,retriever=r,semantic_cache=True,**kw),fake

def test_semantic_hit_uses_one_embedding_and_labels_cached_scores(retrieval_setup):
    s,fake=ready(retrieval_setup)
    a=s.search('복학 신청은 어디서 해?');assert len(fake.requests)==2
    b=s.search('복학 신청 경로를 알려줘')
    assert b['metrics']['semantic_cache_hit'] and b['metrics']['model_calls']==1
    assert len(fake.requests)==3 and b['score_type']=='cached_reranker'
    assert b['semantic_cache']['score_origin']=='cached_query'
    assert [e['id'] for e in a['evidence']]==[e['id'] for e in b['evidence']]
    assert s.search('복학 신청 경로를 알려줘')['metrics']['model_calls']==0
    with s.store.connect() as db:
        assert db.execute("select count(*) from cache where key like 'semantic:%'").fetchone()[0]==1

def test_semantic_miss_does_not_double_embed_and_preserves_original_result(retrieval_setup):
    s,fake=ready(retrieval_setup)
    expected=s.retriever.search('복학 신청 방법은?',5)
    fake.requests.clear();actual=s.search('복학 신청 방법은?')
    assert len(fake.requests)==2
    assert actual['status']==expected['status']
    assert [{k:v for k,v in e.items() if k!='content_kind'} for e in actual['evidence']]==expected['evidence']
    assert s.retriever._vectors is not None

def test_scope_ttl_and_no_transitive_aliases(retrieval_setup):
    now=[1000.];s,fake=ready(retrieval_setup,clock=lambda:now[0],ranked_cache_ttl=10)
    s.search('복학 신청 방법은?',semester='2026-2')
    x=s.search('복학 신청 경로는?',semester='2026-1')
    assert not x['metrics']['semantic_cache_hit'] and x['metrics']['model_calls']==2
    now[0]+=5
    x=s.search('복학 신청 절차를 알려줘',semester='2026-2')
    assert x['metrics']['semantic_cache_hit']
    assert x['semantic_cache']['expires_at']==1010
    now[0]=1011
    x=s.search('복학 신청 절차를 알려줘',semester='2026-2')
    assert not x['metrics']['semantic_cache_hit'] and x['metrics']['model_calls']==1
    assert x['metrics']['embedding_cache_hit'] and x['metrics']['rerank_calls']==1

def test_semantic_disabled_with_model_threshold_and_invalid_vectors(retrieval_setup):
    s,fake=ready(retrieval_setup);s.retriever.min_rerank_score=.1
    s.search('복학 신청 방법은?');x=s.search('복학 신청 경로는?')
    assert not x['metrics']['semantic_cache_hit'] and len(fake.requests)==4
    for vector in [[],[0,0],[True,1],[float('nan')],[float('inf')]]:
        with pytest.raises(ValueError):normalized(vector)

def test_ambiguity_margin_rejects_different_top_sources(retrieval_setup):
    s,_=ready(retrieval_setup);s.search('복학 신청 방법은?')
    with s.store.connect() as db:
        key,rev,expiry,payload=db.execute("select * from cache where key like 'semantic:%'").fetchone()
        value=json.loads(payload);value['result']['evidence'][0]['id']='different-source'
        db.execute('insert into cache values(?,?,?,?)',(key+'other',rev,expiry,json.dumps(value)))
    x=s.search('복학 신청 경로는?')
    assert not x['metrics']['semantic_cache_hit'] and x['metrics']['model_calls']==2

def test_unknown_topics_and_disabled_semantics_keep_legacy_contract(retrieval_setup):
    s,fake=ready(retrieval_setup)
    assert signature('알려줘') is None
    s.semantic_cache_enabled=False
    s.search('복학 신청 방법은?');x=s.search('복학 신청 경로는?')
    assert not x['metrics']['semantic_cache_hit'] and len(fake.requests)==4

def test_semantic_failure_does_not_publish_and_never_hides_upstream_error(retrieval_setup):
    s,fake=ready(retrieval_setup);fake.status=503
    import httpx
    with pytest.raises(httpx.HTTPStatusError):s.search('복학 신청 방법은?')
    with s.store.connect() as db:assert db.execute('select count(*) from cache').fetchone()[0]==0

def test_profile_and_reference_day_invalidate_semantic_entries(retrieval_setup):
    from datetime import date
    today=[date(2026,11,21)];s,_=ready(retrieval_setup,today=lambda:today[0])
    s.search('복학 신청 방법은?')
    today[0]=date(2026,11,22)
    assert not s.search('복학 신청 경로는?')['metrics']['semantic_cache_hit']
    s.model_revision='new-provider-model'
    assert not s.search('복학 신청 절차를 알려줘')['metrics']['semantic_cache_hit']

def test_revision_change_during_embedding_cannot_publish_semantic_or_response(retrieval_setup):
    from academic_mcp.runtime import DataChanged
    s,_=ready(retrieval_setup);original=s.retriever.embed
    def embed(texts):
        result=original(texts)
        with s.store.connect() as db:db.execute("update meta set value='changed' where key='revision'")
        return result
    s.retriever.embed=embed
    with pytest.raises(DataChanged):s.search('복학 신청 방법은?')
    with s.store.connect() as db:assert db.execute('select count(*) from cache').fetchone()[0]==0

@pytest.mark.parametrize('a,b',[
 ('이중전공 3학기 신청 조건은?','이중전공 3학점 신청 조건은?'),
 ('휴학 최소 기간은 얼마야?','휴학 최대 기간은 얼마야?'),
 ('전역 후 복학 신청 방법은?','복학 신청 방법은?'),
 ('재수강 평점 3.0 이상 조건은?','재수강 평점 3.0 이하 조건은?'),
])
def test_units_comparators_and_military_are_part_of_condition_guard(a,b):
    assert signature(a)!=signature(b) or signature(a) is None
