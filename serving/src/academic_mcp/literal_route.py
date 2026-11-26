"""Exact public document/field lookup; ambiguous or contextual questions search normally."""
import re
from .direct_answer import _named_fields
from .search import normalize

FIELDS={'정의','개념','신청자격','신청방법','신청절차','증빙서류','휴학기간','수수료'}
POLICY='exact-public-fields-v2'


def lookup(query,chunks,semester=None,target=None):
    if semester is not None or target is not None:return None
    # The combined-field parser full-matches the whole question and requires
    # unique fields in one document. It never infers an individual decision.
    combined=_named_fields(query,chunks)
    if combined is not None:return combined
    q=normalize(query)
    reason=_reason_field(q,chunks)
    if reason is not None:return reason
    matches=[]
    for row in chunks:
        topic=normalize(row.get('doc_title','').split('>')[-1])
        section=normalize(row.get('section',''))
        if len(topic)<2 or section not in FIELDS or not q.startswith(topic):continue
        pattern=(re.escape(topic)+r'(?:의)?'+re.escape(section)+
                 r'(?:은|는|을|를)?(?:알려줘|알려주세요|설명해줘|설명해주세요|무엇인가요|뭐야)?')
        if re.fullmatch(pattern,q):matches.append(row)
    if len(matches)!=1:return None
    row=matches[0];text=row.get('text') or ''
    if not 0<len(text)<=1200:return None
    return {'answer':f"공식 안내 ({row['section']})\n{text}",'citations':[row['url']],
            'chunk_ids':[row['id']],'answer_mode':'source_item','can_answer':True}


def _reason_field(q,chunks):
    """Exact leave-reason/document or duration intents, with complete list blocks.

    No age, first-term, academic-year, eligibility or personal-decision inputs
    match this grammar. Conditions inside the official block remain intact.
    """
    match=re.fullmatch(r'([가-힣]{2,8}?)(?:하려고|때문에)?휴학(?:하려면|하면)?'
                      r'(어떤서류가필요해|필요한서류알려줘|증빙서류알려줘|최대얼마나쉴수있어|기간알려줘)',q)
    if not match:return None
    reason,field=match.groups()
    section='증빙서류' if '서류' in field else '종류'
    matches=[]
    for row in chunks:
        if normalize(row.get('section',''))!=section:continue
        if not normalize(row.get('doc_title','')).endswith('휴학특별휴학'):continue
        text=row.get('text') or ''
        if not text.startswith('- '):continue
        blocks=re.split(r'(?m)(?=^- )',text)
        for block in blocks:
            heading=normalize(block.splitlines()[0]) if block else ''
            if reason not in heading or '휴학' not in heading:continue
            if section=='종류' and not re.search(r'[0-9]+(?:년|학기)',heading):continue
            if 0<len(block.strip())<=1200:matches.append((row,block.strip()))
    if len(matches)!=1:return None
    row,block=matches[0]
    return {'answer':f"공식 안내 ({row['section']})\n{block}",'citations':[row['url']],
            'chunk_ids':[row['id']],'answer_mode':'source_item','can_answer':True}
