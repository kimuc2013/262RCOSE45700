"""Cache validated ranked evidence, separate from caller-specific responses."""
import copy
import hashlib
import json
import threading

from .cache import ranked_query
from .runtime import checkpoint, remaining_timeout, Overloaded, DataChanged


class RankedCache:
    def __init__(self, service, ttl=1800):
        self.service, self.ttl = service, ttl
        self._guard = threading.Lock()
        self._flights = {}

    def search(self, query, top_k, *, chunks, revision, metrics, semester=None, target=None):
        service, retriever = self.service, self.service.retriever
        # Only the model module's known prefix invariant is eligible. Test or
        # alternate adapters without candidates retain their original contract.
        if not chunks or not hasattr(retriever, 'candidates'):
            metrics['retrieval_cache_hit'] = False
            return retriever.search(query, top_k, chunks=chunks, revision=revision)
        width = max(top_k, min(10, retriever.candidates)) if retriever.use_reranker else 10
        identity = ['ranked-v2-polite', ranked_query(query), width, revision, service.profile(), service.today().isoformat()]
        key = 'ranked:' + hashlib.sha256(json.dumps(identity, sort_keys=True,
            ensure_ascii=False, allow_nan=False).encode()).hexdigest()

        def read():
            checkpoint()
            with service.store.connect(timeout=1) as db:
                row = db.execute('SELECT payload FROM cache WHERE key=? AND revision=? AND expires>?',
                                 (key, revision, service.clock())).fetchone()
            return json.loads(row[0]) if row else None

        def output(result, hit, shared=False, semantic=False):
            metrics.update(retrieval_cache_hit=hit, retrieval_coalesced=shared, semantic_cache_hit=semantic)
            if hit and not semantic:
                metrics['semantic_cache_reason']='ranked_cache_hit'
            if semantic:
                metrics['semantic_similarity'] = result['semantic_cache']['similarity']
            result = copy.deepcopy(result)
            result['evidence'] = result['evidence'][:top_k]
            return result

        hit = read()
        if hit is not None:
            return output(hit, True)
        with self._guard:
            lock, refs = self._flights.get(key, (threading.Lock(), 0))
            self._flights[key] = (lock, refs + 1)
        shared = lock.locked()
        acquired = False
        try:
            acquired = lock.acquire(timeout=remaining_timeout(service.requests.limits.request_timeout))
            if not acquired:
                raise Overloaded('Ranked cache lookup busy; retry later')
            checkpoint()
            hit = read()
            if hit is not None:
                return output(hit, True, shared)
            prepared = service.semantic_cache.prepare(query, width, revision, semester, target, metrics)
            result = service.semantic_cache.lookup(prepared, metrics)
            semantic = result is not None
            if not semantic:
                result = service.semantic_cache.search(query, width, chunks=chunks, revision=revision, prepared=prepared, metrics=metrics)
            checkpoint()
            # Retriever has already validated responses and applied its threshold.
            with service.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                current = db.execute("SELECT value FROM meta WHERE key='revision'").fetchone()
                if not current or current[0] != revision:
                    raise DataChanged('Source changed before ranked cache publication')
                db.execute('DELETE FROM cache WHERE expires<=?', (service.clock(),))
                if db.execute('SELECT count(*) FROM cache').fetchone()[0] >= 1000:
                    db.execute('DELETE FROM cache WHERE key IN (SELECT key FROM cache ORDER BY expires LIMIT 100)')
                checkpoint()
                db.execute('INSERT OR REPLACE INTO cache VALUES(?,?,?,?)',
                    (key, revision, min(service.clock()+self.ttl, result.get('semantic_cache',{}).get('expires_at',float('inf'))), json.dumps(result, ensure_ascii=False)))
            if not semantic:
                service.semantic_cache.publish(prepared, key, result, revision)
            return output(result, semantic, semantic=semantic)
        finally:
            if acquired:
                lock.release()
            with self._guard:
                existing, refs = self._flights[key]
                if refs == 1:
                    del self._flights[key]
                else:
                    self._flights[key] = existing, refs - 1
