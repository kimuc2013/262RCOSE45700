"""Serving cache for validated online query vectors, with bounded worker sharing."""
import copy
import hashlib
import json
import threading

from .cache import canonical_query
from .cache_policy import expires
from .runtime import checkpoint, remaining_timeout, Overloaded, DataChanged

POLICY = 'query-vectors-v1'


class QueryEmbeddings:
    def __init__(self, service):
        self.service=service
        self._guard=threading.Lock()
        self._flights={}

    def embed(self, query, revision, metrics):
        s=self.service
        identity=[POLICY,canonical_query(query),[s.retriever.embedding_url,s.retriever.embedding_model],s.model_revision,revision]
        key='query-vector:'+hashlib.sha256(json.dumps(identity,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        def read():
            checkpoint()
            with s.store.connect(timeout=1) as db:
                row=db.execute('SELECT payload FROM cache WHERE key=? AND revision=? AND expires>?',
                               (key,revision,s.clock())).fetchone()
            return json.loads(row[0]) if row else None
        value=read()
        if value is not None:
            metrics['embedding_cache_hit']=True
            return copy.deepcopy(value)
        with self._guard:
            lock,refs=self._flights.get(key,(threading.Lock(),0))
            self._flights[key]=(lock,refs+1)
        acquired=lock.acquire(blocking=False)
        metrics['embedding_coalesced']=not acquired
        try:
            if not acquired:acquired=lock.acquire(timeout=remaining_timeout(s.requests.limits.request_timeout))
            if not acquired:raise Overloaded('Query embedding wait exhausted')
            value=read()
            if value is not None:
                metrics['embedding_cache_hit']=True
                return copy.deepcopy(value)
            metrics['embedding_cache_hit']=False
            value=s.retriever.embed([query])
            # The model adapter validates its response; reject corrupt vectors at
            # the serving publication boundary as well, without changing values.
            from .semantic_cache import normalized
            if len(value)!=1:raise ValueError('Unexpected query vector count')
            normalized(value[0]);checkpoint()
            with s.store.connect(timeout=1) as db:
                db.execute('BEGIN IMMEDIATE')
                row=db.execute("SELECT value FROM meta WHERE key='revision'").fetchone()
                if not row or row[0]!=revision:raise DataChanged('Source changed before query vector publication')
                db.execute('DELETE FROM cache WHERE expires<=?',(s.clock(),))
                if db.execute('SELECT count(*) FROM cache').fetchone()[0]>=1000:
                    db.execute('DELETE FROM cache WHERE key IN (SELECT key FROM cache ORDER BY expires LIMIT 100)')
                checkpoint()
                db.execute('INSERT OR REPLACE INTO cache VALUES(?,?,?,?)',
                           (key,revision,expires(s,None,s.ranked_cache.ttl),json.dumps(value)))
            return copy.deepcopy(value)
        finally:
            if acquired:lock.release()
            with self._guard:
                lock,refs=self._flights[key]
                if refs==1:del self._flights[key]
                else:self._flights[key]=(lock,refs-1)
