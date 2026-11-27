"""Snapshot-lived facts; relative time stays on a daily, short-lived partition."""
import re

POLICY='revision-static-v1'
# Finite SQLite/JSON-compatible sentinel, not a promise of unlimited storage.
UNTIL_REVISION=253402300799.0


def date_sensitive(query):
    # A complete public-field grammar proves a static rule even if its wording
    # asks 'when' or 'how many days'. Arbitrary date questions remain daily.
    if query:
        from .policy_fields import intent_key
        if intent_key(query) is not None:return False
    return bool(re.search(r'오늘|내일|어제|모레|올해|금년|작년|내년|이번|현재|지금|최근|최신|다음|지난|언제|일정|마감|기한|신청일|신청기간|신청\s*날짜|시기|몇\s*월|며칠',re.sub(r'\s+','',query or '')))


def partition(service,query):
    return 'snapshot' if service.revision_cache and not date_sensitive(query) else service.today().isoformat()


def expires(service,query,ttl):
    return UNTIL_REVISION if service.revision_cache and not date_sensitive(query) else service.clock()+ttl
