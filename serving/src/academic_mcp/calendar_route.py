"""Conservative calendar-only requests may avoid the model entirely."""
import re
from .search import normalize


def calendar_request(query, semester):
    # Never infer a year from an old event or a relative season.
    if not re.search(r'언제|일정|날짜|기간|마감|신청일',query):return None
    if re.search(r'자격|조건|방법|어떻게|서류|학점|비용|평점',query):return None
    explicit=re.search(r'(20\d{2})\s*(?:학년도|년)?\s*[- ]?\s*([12])\s*학기',query)
    hyphen=re.search(r'\b(20\d{2})-([12])\b',query)
    match=explicit or hyphen
    inferred=f'{match[1]}-{match[2]}' if match else None
    if semester and inferred and semester!=inferred:return None
    return semester or inferred


def matching_events(query, semester, events):
    q=normalize(query)
    matches=[]
    for event in events:
        if event.get('target_semester',event['semester'])!=semester:continue
        # A whole event topic must occur in the question; no loose bigram guess.
        topic=re.sub(r'신청|기간|등록|일정|\([^)]*\)','',event['name']).strip()
        if len(normalize(topic))>=2 and normalize(topic) in q:matches.append(event)
    return matches
