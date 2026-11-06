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
