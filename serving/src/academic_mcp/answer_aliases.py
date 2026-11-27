"""Bounded exact-intent aliases. No fuzzy hash or embedding can authorize an answer hit."""
import hashlib
import json
import re
from .cache import ranked_query

POLICY='verified-answer-intents-v5'


def polite_query(query):
    # A sentence-final period after Korean prose is punctuation, never a decimal.
    text=query.strip()
    if re.search(r'[가-힣]\.$',text):text=text[:-1]
    return ranked_query(text)


# Whole-question grammars, with every unmatched topic/condition retained in the
# topic slot. A partial keyword match is never an equivalence proof.
GRAMMARS=(
 ('application_location_and_procedure',r'(?P<topic>.+?)신청(?:은|는)?(?:어디서|어디에서)(?:어떻게해|어떻게하나요|어떻게하면되나요)'),
 ('application_procedure',r'(?P<topic>.+?)신청(?:은|는)?(?:어디서해|어디에서해)'),
 ('application_procedure',r'(?P<topic>.+?)어디서신청하나요'),
 ('location',r'(?P<topic>.+?)(?:은|는)?(?:어디서해|어디에서해)'),
 ('application_procedure',r'(?P<topic>.+?)하려면(?:어느곳에서|어디서)신청하면되나요'),
 ('application_procedure',r'(?P<topic>.+?)신청을접수하는경로가궁금해요'),
 ('process',r'(?:학교를)?(?P<topic>.+?)(?:하려면어떻게해야돼|할때거쳐야하는절차가궁금해요)'),
 ('application_procedure',r'(?P<topic>.+?)신청(?:방법|절차)(?:을|를)?(?:알려줘|설명해줘)?'),
 ('application_eligibility',r'(?P<topic>.+?)신청(?:자격|자격조건|조건|요건)(?:을|를|은|는|이|가)?(?:알려줘|설명해줘|무엇)?'),
 ('eligibility',r'(?P<topic>.+?)(?:자격|자격조건|조건|요건)(?:을|를|은|는|이|가)?(?:알려줘|설명해줘|무엇)?'),
 ('application_eligibility',r'(?P<topic>.+?)에지원할수있는자격조건을알려줘'),
 ('application_eligibility',r'어떤요건을갖춰야(?P<topic>.+?)신청이가능해'),
 ('eligibility',r'(?P<topic>.+?)을하려면어떤요건을충족해야하나요'),
 ('application_eligibility',r'(?P<topic>.+?)(?:을|를)신청하려면어떤자격(?:이)?(?:필요해|필요한가요)'),
 ('application_eligibility',r'(?P<topic>.+?)신청에필요한자격(?:을)?(?:알려줘|설명해줘)'),
 ('scoped_graduation_credits',r'(?P<topic>.+?)(?:의)?졸업요건(?:의)?(?:이수학점|학점)(?:을|이|은)?(?:알려줘|무엇)?'),
 ('scoped_graduation_credits',r'(?P<topic>.+?)졸업하려면(?:총)?몇학점(?:을)?(?:이수해야해|채워야해|들어야해)'),
 ('scoped_graduation_credits',r'(?P<topic>.+?)(?:의)?졸업에필요한이수학점(?:은)?(?:몇학점인가요|알려줘)'),
 ('application_term',r'(?P<topic>.+?)은몇학기다닌뒤에신청할수있어'),
 ('application_term',r'(?P<topic>.+?)신청자격을얻으려면몇학기를등록해야하나요'),
 ('application_term',r'몇학기를다녀야(?P<topic>.+?)을신청할수있나요'),
 ('duration',r'(?P<topic>.+?)(?:은)?(?:최대)?몇년까지할수있어'),
 ('duration',r'(?P<topic>.+?)으로쉴수있는최장기간은몇년인가요'),
 ('duration',r'(?P<topic>.+?)가능한기간의상한을연수로알려줘'),
 ('credit_limit',r'(?P<topic>.+?)는몇학점까지가능해'),
 ('credit_limit',r'(?P<topic>.+?)로없앨수있는학점은최대몇학점인가요'),
 ('grade_cap',r'(?P<topic>.+?)하면받을수있는최고성적이무엇'),
 ('grade_cap',r'(?P<topic>.+?)과목에서취득가능한성적의상한은무엇'),
 ('grade_minimum',r'(?P<topic>.+?)은성적이몇이하여야할수있어'),
 ('grade_minimum',r'어느성적이하인과목부터(?P<topic>.+?)할수있나요'),
 ('grade_minimum',r'(?P<topic>.+?)을신청할수있는기존성적기준은무엇'),
 ('graduation_credits',r'(?P<topic>졸업)(?:하려면총몇학점들어야해|에필요한총이수학점은몇학점인가요)'),
)


def intent(query):
    q=polite_query(query)
    # Normalize only the known institution spelling and its grammatical case,
    # retaining an explicit scope in the proof. Other schools/qualifiers survive.
    school=re.match(r'^(고려대학교|고려대)(?:에서는|에서|에는|에선|에|는|은|의)?',q)
    scope='고려대학교:' if school else ''
    if school:q=q[school.end():]
    matches=set()
    for field,pattern in GRAMMARS:
        match=re.fullmatch(pattern,q)
        if match:
            topic=match['topic']
            if re.search(r'할수있는|신청자격을|신청을|하려면|신청$',topic):continue
            if field=='eligibility' and topic in ('이중전공','복수전공','전과'):
                field='application_eligibility'
            if field in ('process','location') and topic in ('복학','휴학','자퇴'):
                field='application_procedure'
            if field in ('location','process'):continue
            if 1<len(topic)<=128:matches.add((scope+topic,field))
    # Different parse meanings cannot be resolved by rule order.
    return next(iter(matches)) if len(matches)==1 else None


def proof(query):
    value=intent(query)
    return hashlib.sha256(json.dumps([POLICY,value],ensure_ascii=False).encode()).hexdigest() if value else None


def retrieval_proof(query):
    """Evidence-only field equivalence; never authorizes a generated answer hit."""
    # An explicit request to use MCP selects transport, not academic meaning.
    query=re.sub(r'\s*mcp\s*로\s*(?:조회|검색)해(?:줘|봐|주세요)[.!?？]*\s*$','',query,flags=re.I).strip()
    value=intent(query)
    if value:
        topic,field=value
        # Both select application instructions as evidence. Generated answer
        # aliases keep the compound request separate from a place-only request.
        if field=='application_location_and_procedure':field='application_procedure'
        return hashlib.sha256(json.dumps([POLICY,(topic,field)],ensure_ascii=False).encode()).hexdigest()
    from .policy_fields import intent_key
    field=intent_key(query)
    return hashlib.sha256(json.dumps(['public-fields-v1',field],ensure_ascii=False).encode()).hexdigest() if field else None


# Public document names, not evaluation question IDs. Missing/renamed documents
# simply fall back to retrieval; no facts or policy values are hardcoded here.
DOCUMENTS={
 '휴학':('leave_normal',),'일반휴학':('leave_normal',),'복학':('return',),
 '자퇴':('drop',),'이중전공':('major_double',),'복수전공':('major_dual',),
 '전과':('major_transfer',),'재수강':('grade_retake',),'학점포기':('grade_giveup',),
 '졸업':('graduation_require',),'조기졸업':('graduation_early',),
}
SECTIONS={
 'application_procedure':('신청방법','신청절차'),
 'application_location_and_procedure':('신청방법','신청절차'),
 'application_eligibility':('신청자격','신청대상'),
 'eligibility':('일반조기졸업',),
 'application_term':('신청자격',), 'duration':('휴학기간',),
 'credit_limit':('포기가능학점',), 'grade_cap':('재수강성적처리',),
 'grade_minimum':('신청가능대상',), 'graduation_credits':('졸업요구학점',),
}


def source_item(query,chunks,semester=None,target=None):
    if semester is not None or target is not None:return None
    value=intent(query)
    if not value:return None
    topic,field=value
    if topic.startswith('고려대학교:'):topic=topic.removeprefix('고려대학교:')
    if field not in SECTIONS:return None
    docs=DOCUMENTS.get(topic,())
    rows=[r for r in chunks if r.get('doc_id') in docs and
          re.sub(r'\s+','',r.get('section','')) in SECTIONS[field]]
    if len(rows)!=1:return None
    row=rows[0];text=row.get('text') or ''
    # Complete source items preserve all caveats; unreadable/link-only, huge or
    # ambiguous items cannot be promoted to an answered response.
    if not 0<len(text)<=1200 or row.get('extraction_method')=='none':return None
    return {'answer':f"공식 안내 ({row['doc_title']} > {row['section']})\n{text}",
            'citations':[row['url']],'chunk_ids':[row['id']],
            'answer_mode':'source_item','can_answer':True}
