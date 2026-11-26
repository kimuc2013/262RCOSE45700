"""Conservative field lookup: quote a complete official item, never infer facts."""
import re
from .search import normalize, tokenize

POLICY='literal-field-v5'


def direct_answer(query,evidence):
    # Explicit first-term populations must see the restriction before a generic
    # application procedure. Quote only a retrieved official restriction, with
    # its exceptions intact; never synthesize an eligibility decision.
    if (re.search(r'휴학',query) and re.search(r'신입생|신입학|편입생|편입학|재입학생',query)
            and re.search(r'첫\s*학기',query) and not re.search(r'왜|이유|서류|몇|얼마|20\d{2}',query)):
        for row in evidence:
            text=row.get('text') or ''
            if (0<len(text)<=1200 and '휴학' in row.get('doc_title','')
                    and re.search(r'첫\s*학기',text) and '휴학할 수 없음' in text
                    and all(not re.search(qp,query) or re.search(tp,text) for qp,tp in
                            [(r'신입생|신입학',r'신입생|신입학'),(r'편입생|편입학',r'편입'),(r'재입학생',r'재입학')])):
                return {'answer':'첫 학기 휴학에 관한 공식 유의사항입니다.\n'+text,
                        'citations':[row['url']],'chunk_ids':[row['id']],
                        'answer_mode':'source_item','can_answer':True}
        return None
    # Inspect the requested named field, without changing retrieval rankings.
    for row in evidence:
        result=_item(query,row)
        if result is not None:return result
    return None


def _item(query,row):
    if re.search(r'왜|이유',query):return None
    text=row.get('text') or '';section=normalize(row.get('section',''));q=normalize(query)
    if not 0<len(text)<=1200:return None
    quantity=bool(re.search(r'몇|얼마|최대|최고|상한',query))
    method=bool(re.search(r'어떻게|어디|어느 곳|방법|절차|서류',query))
    eligibility='자격' in q or ('신청' in q and '몇학기' in q)
    refund=bool(re.search(r'환불|반환',query))
    # One requested field only. Combined procedure/eligibility questions stay generative.
    if method and (quantity or eligibility):return None
    named_phrase='학점' in q and '학점' in section and any(section[i:i+6] in q for i in range(max(0,len(section)-5)))
    if not (quantity or method or eligibility or refund or named_phrase):return None
    if not (text.lstrip().startswith(('-', '|', '[표]')) or '\n|' in text or '\n' not in text):return None
    leaf=row.get('doc_title','').split('>')[-1]
    qt=set(tokenize(query));title=set(tokenize(leaf))
    title_overlap=(qt & title)-set(tokenize('교육 정보 학사 등록 학점 평점 수수료 금액 등록금'))
    year_field='학년' in q and '학점' in q and '학년구분' in section
    if not title_overlap and not year_field:return None
    exact_topic=any(len(t)>=2 and normalize(t) in q for t in re.findall(r'[가-힣]+',leaf) if t not in ('개요','목적','안내','정보'))
    field=quantity and any(word in q and word in section for word in ('학점','수수료','평점'))
    amount=quantity and ('등록금' in q or '납부' in q) and '납부금액' in section
    cap=bool(re.search(r'최대|최고|상한',query)) and '성적' in q and '성적처리' in section
    method_field=method and exact_topic and bool(re.search(r'신청방법|신청절차|제출서류|재발급',section))
    if '재발급' in section and not re.search(r'잃어|분실|재발급|다시',query):method_field=False
    eligibility_field=eligibility and exact_topic and bool(re.search(r'신청자격|신청대상',section))
    refund_field=refund and '반환기준' in section
    if not (field or amount or cap or year_field or method_field or eligibility_field or refund_field or named_phrase):return None
    # A specifically dated rule cannot be silently answered with an undated item.
    if any(year not in text for year in re.findall(r'20\d{2}',query)):return None
    return {'answer':f"공식 안내 ({row['section']})\n{text}",
            'citations':[row['url']],'chunk_ids':[row['id']],
            'answer_mode':'source_item','can_answer':True}
