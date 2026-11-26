"""Conservative cache identity; this is not semantic normalization."""
import hashlib
import json
import re
import unicodedata

POLICY_VERSION = 'exact-v2'


def canonical_query(text):
    text = unicodedata.normalize('NFKC', text).lower().strip()
    text = re.sub(r'\s+', ' ', text)
    # Korean spacing variants may share a result. Numeric/Latin word boundaries,
    # decimal points, date delimiters, quotes and negation are retained.
    text = re.sub(r'(?<=[가-힣]) (?=[가-힣])', '', text)
    return text.rstrip('?!？！').rstrip()


def identity(query, history, top_k, semester, target, profile, reference_date):
    payload = [POLICY_VERSION, canonical_query(query), history, top_k,
               semester, target, profile, reference_date]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                     allow_nan=False).encode()).hexdigest()


def ranked_query(text):
    """Normalize only explicit request politeness, retaining the requested action.

    This is for evidence reuse, never a topic-only or SimHash answer key.
    Numeric punctuation, grades, names, negatives and time expressions survive.
    """
    text=unicodedata.normalize('NFKC',text).lower().strip().rstrip('?!？！').rstrip()
    for pattern,replacement in (
        (r'알려(?:줘|주세요|주십시오|줄래|주시겠어요|주시겠습니까)$','알려줘'),
        (r'설명해(?:줘|주세요|주십시오|줄래)$','설명해줘'),
        (r'가능(?:해|한가요|합니까|할까요)$','가능해'),
        (r'얼마(?:야|인가요|입니까)$','얼마야'),
        (r'(?:뭐야|무엇인가요|뭔가요|무엇입니까)$','무엇'),
    ):
        text=re.sub(pattern,replacement,text)
    return canonical_query(text)
