"""Retrieval module: question in, ranked evidence chunks with sources out.

Serving treats embedding, vector search and reranking as a black box and calls
only ``Retriever.search``. This module reads the same Store (chunks and the
vectors table) as serving, and never falls back to fake vectors or scores:
a model failure raises, so it is not mistaken for insufficient evidence.

Pipeline: query embedding -> cosine over prebuilt document vectors ->
rerank the top candidates -> top_k -> evidence judgement. Document vectors are built offline with
``python -m academic_mcp.retrieval index``, not during a user request.
"""
import argparse
import json
import math
import os
import threading

import httpx

from .runtime import checkpoint, remaining_timeout
from .store import Store

# Part of the index key: changing how documents are rendered requires new vectors.
DOCUMENT_TEMPLATE = 'title-section-text-v1'
DEFAULT_EMBEDDING_MODEL = 'furiosa-ai/bge-m3'
DEFAULT_RERANK_MODEL = 'furiosa-ai/bge-reranker-v2-m3'


def document_text(chunk: dict) -> str:
    """Input for both document embedding and reranking.

    Body text alone loses the topic: many chunks share section names such as
    신청자격, so the title and section are always included.
    """
    return chunk['doc_title'] + '\n' + chunk['section'] + '\n' + chunk['text']


def _normalize(vector):
    norm = math.sqrt(sum(x * x for x in vector))
    if not norm:
        raise ValueError('Zero embedding vector')
    return [x / norm for x in vector]


class Retriever:
    def __init__(self, store: Store, *, embedding_url: str, rerank_url: str | None = None,
                 embedding_model: str = DEFAULT_EMBEDDING_MODEL, rerank_model: str = DEFAULT_RERANK_MODEL,
                 api_key: str | None = None, timeout: float = 15, candidates: int = 20,
                 use_reranker: bool = True, min_rerank_score: float | None = None, transport=None):
        if not embedding_url or not embedding_model:
            raise ValueError('Embedding URL and model must not be empty')
        if use_reranker and (not rerank_url or not rerank_model):
            raise ValueError('Reranker URL and model must not be empty when reranking is enabled')
        if isinstance(candidates, bool) or not isinstance(candidates, int) or not 1 <= candidates <= 100:
            raise ValueError('candidates must be between 1 and 100')
        if min_rerank_score is not None:
            if isinstance(min_rerank_score, bool) or not isinstance(min_rerank_score, (int, float)) \
                    or not math.isfinite(min_rerank_score):
                raise ValueError('min_rerank_score must be a finite number')
            if not use_reranker:
                # Cosine similarity is not calibrated for this judgement.
                raise ValueError('min_rerank_score requires the reranker')
        self.store = store
        self.embedding_url, self.rerank_url = embedding_url, rerank_url
        self.embedding_model, self.rerank_model = embedding_model, rerank_model
        self.timeout, self.candidates, self.use_reranker = timeout, candidates, use_reranker
        # Leave unset until calibrated on a reviewed evaluation set.
        self.min_rerank_score = min_rerank_score
        # One pooled client: a new connection per call cost about 400 ms in measurement.
        self._client = httpx.Client(headers={'Authorization': f'Bearer {api_key}'} if api_key else {},
                                    transport=transport)
        self._lock = threading.Lock()
        self._vectors = None  # (revision, normalized vectors) of the latest revision only

    @classmethod
    def from_env(cls, store: Store, **overrides):
        """Embedding and reranker are separate services, so each has its own full URL."""
        settings = dict(
            embedding_url=os.environ['RETRIEVAL_EMBEDDING_URL'],
            rerank_url=os.getenv('RETRIEVAL_RERANK_URL') or None,
            embedding_model=os.getenv('RETRIEVAL_EMBEDDING_MODEL') or DEFAULT_EMBEDDING_MODEL,
            rerank_model=os.getenv('RETRIEVAL_RERANK_MODEL') or DEFAULT_RERANK_MODEL,
            api_key=os.getenv('RETRIEVAL_API_KEY') or None,
            timeout=float(os.getenv('RETRIEVAL_TIMEOUT_SECONDS', '15')),
            candidates=int(os.getenv('RETRIEVAL_CANDIDATES', '20')),
            use_reranker=os.getenv('RETRIEVAL_USE_RERANKER', 'true').lower() != 'false',
            min_rerank_score=float(threshold) if (threshold := os.getenv('RETRIEVAL_MIN_RERANK_SCORE')) else None)
        return cls(store, **{**settings, **overrides})

    def close(self):
        self._client.close()

    @property
    def index_key(self):
        """Vectors are valid only for this endpoint, model and document template."""
        return f'{self.embedding_url}|{self.embedding_model}|{DOCUMENT_TEMPLATE}'

    def profile(self):
        """Settings that change results. Serving can put this in its cache key."""
        return {'embedding_url': self.embedding_url, 'embedding_model': self.embedding_model,
                'rerank_url': self.rerank_url if self.use_reranker else None,
                'rerank_model': self.rerank_model if self.use_reranker else None,
                'document_template': DOCUMENT_TEMPLATE, 'candidates': self.candidates,
                'min_rerank_score': self.min_rerank_score}

    def _post(self, url, body, model):
        # No retries: an unavailable model must not silently multiply latency.
        checkpoint()
        response = self._client.post(url, json=body, timeout=remaining_timeout(self.timeout))
        response.raise_for_status()
        checkpoint()
        data = response.json()
        # The real endpoint ignores the requested model name and answers with
        # whatever it serves, so only the response can reveal a model change.
        if not isinstance(data, dict) or data.get('model') != model:
            raise ValueError(f'Model service answered with an unexpected model; expected {model}')
        return data

    def embed(self, texts: list[str]) -> list[list[float]]:
        data = self._post(self.embedding_url, {'model': self.embedding_model, 'input': texts}, self.embedding_model)
        rows = sorted(data['data'], key=lambda row: row['index'])
        if [row['index'] for row in rows] != list(range(len(texts))):
            raise ValueError('Missing or duplicate embeddings')
        vectors = [row['embedding'] for row in rows]
        if any(not v or len(v) != len(vectors[0]) or any(
                isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in v)
               for v in vectors):
            raise ValueError('Invalid embedding response')
        return vectors

    def rerank(self, query: str, documents: list[str]) -> list[tuple[int, float]]:
        data = self._post(self.rerank_url, {'model': self.rerank_model, 'query': query, 'documents': documents},
                          self.rerank_model)
        rows = [(row['index'], float(row['relevance_score'])) for row in data['results']]
        if sorted(i for i, _ in rows) != list(range(len(documents))) or any(not math.isfinite(s) for _, s in rows):
            raise ValueError('Invalid reranking response')
        return sorted(rows, key=lambda row: (-row[1], row[0]))

    def build_index(self, batch_size: int = 32) -> dict:
        """Embed every stored chunk and save the vectors for the current revision."""
        revision, chunks = self.store.table_snapshot('chunks')
        if not chunks:
            raise RuntimeError('Ingest documents before building vectors')
        vectors = []
        for offset in range(0, len(chunks), batch_size):
            vectors.extend(self.embed([document_text(c) for c in chunks[offset:offset + batch_size]]))
        if len({len(v) for v in vectors}) != 1:
            raise ValueError('Embedding dimensions changed between batches')
        self.store.save_vectors(revision, self.index_key, vectors)  # fails if data changed meanwhile
        return {'revision': revision, 'chunks': len(vectors), 'dimension': len(vectors[0]),
                'embedding_model': self.embedding_model}

    def _load_vectors(self, revision, count):
        with self._lock:
            if self._vectors is None or self._vectors[0] != revision:
                # Decoding the JSON payload on every request cost about 130 ms for 308 chunks.
                loaded = self.store.load_vectors(revision, self.index_key)
                self._vectors = (revision, [_normalize(v) for v in loaded])
            vectors = self._vectors[1]
        if len(vectors) != count:
            raise ValueError('Vector count differs from chunk count; rebuild the index')
        return vectors

    def search(self, query: str, top_k: int = 5, *, chunks: list[dict] | None = None,
               revision: str | None = None) -> dict:
        """Return ``{"status", "evidence", "score_type", "min_rerank_score"}``.

        ``evidence`` holds up to top_k chunks, best first. Each keeps every stored
        field (url, collected_at, section, ...) plus ``score`` and ``score_type``.
        Scores rank candidates and are not answer probabilities.

        ``status`` is the evidence judgement, in serving's vocabulary:
        - ``found``: the best reranker score reached ``min_rerank_score``.
        - ``insufficient_evidence``: it did not, or there is nothing to search.
          The chunks are still returned as candidates, not as confirmed evidence.
        - ``evidence_available``: no threshold is configured, so nothing was judged.

        Pass the ``chunks`` and ``revision`` serving already holds to search exactly
        that snapshot; otherwise the current stored snapshot is read. Model and
        index failures raise instead of returning ``insufficient_evidence``.
        """
        if not isinstance(query, str) or not query.strip():
            raise ValueError('query must not be empty')
        if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 1:
            raise ValueError('top_k must be a positive integer')
        if (chunks is None) != (revision is None):
            raise ValueError('Pass chunks and revision together')
        if chunks is None:
            revision, chunks = self.store.table_snapshot('chunks')
        if not chunks:
            return self._result('insufficient_evidence', [], None)
        vectors = self._load_vectors(revision, len(chunks))
        embedded = self.embed([query])[0]
        if len(embedded) != len(vectors[0]):
            raise ValueError('Query embedding dimension differs from the index; rebuild the index')
        embedded = _normalize(embedded)
        checkpoint()
        dense = sorted(((i, sum(a * b for a, b in zip(embedded, v))) for i, v in enumerate(vectors)),
                       key=lambda row: (-row[1], row[0]))
        checkpoint()
        score_type = 'dense_cosine'
        ranked = dense[:max(top_k, self.candidates) if self.use_reranker else top_k]
        if self.use_reranker:
            reranked = self.rerank(query, [document_text(chunks[i]) for i, _ in ranked])
            ranked = [(ranked[i][0], score) for i, score in reranked]
            score_type = 'reranker'
        evidence = [{**chunks[i], 'score': round(score, 6), 'score_type': score_type} for i, score in ranked[:top_k]]
        if self.min_rerank_score is None:
            status = 'evidence_available'
        else:
            status = 'found' if ranked[0][1] >= self.min_rerank_score else 'insufficient_evidence'
        return self._result(status, evidence, score_type)

    def _result(self, status, evidence, score_type):
        return {'status': status, 'evidence': evidence, 'score_type': score_type,
                'min_rerank_score': self.min_rerank_score}


def main():
    parser = argparse.ArgumentParser(description='Retrieval index and search checks')
    parser.add_argument('--db', default=os.getenv('ACADEMIC_DB', 'data/academic.sqlite3'))
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('index', help='Embed stored chunks for the current data revision')
    search = commands.add_parser('search', help='Run one query through the module')
    search.add_argument('query')
    search.add_argument('--top-k', type=int, default=5)
    args = parser.parse_args()
    retriever = Retriever.from_env(Store(args.db))
    try:
        if args.command == 'index':
            print(json.dumps(retriever.build_index(), ensure_ascii=False))
        else:
            result = retriever.search(args.query, args.top_k)
            print(f"status: {result['status']} (min_rerank_score={result['min_rerank_score']})")
            for rank, row in enumerate(result['evidence'], 1):
                print(f"{rank}. [{row['score_type']} {row['score']}] {row['id']}\n   {row['url']}")
    finally:
        retriever.close()


if __name__ == '__main__':
    main()
