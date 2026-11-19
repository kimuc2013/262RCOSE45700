import json
import time

import httpx
import pytest

from academic_mcp.ingest import ingest
from academic_mcp.retrieval import Retriever, document_text
from academic_mcp.runtime import Budget, DeadlineExceeded, current_budget
from academic_mcp.store import Store
from test_service import SAMPLES

EMBED_URL = 'http://embed.test/v1/embeddings'
RERANK_URL = 'http://rerank.test/v1/rerank'
WORDS = ['신청자격', '접수방법', '평가기준', '정의']


def vector(text):
    """Deterministic test vector: one axis per keyword, plus a constant axis."""
    return [1.0] + [3.0 if word in text else 0.0 for word in WORDS]


class FakeModels:
    def __init__(self):
        self.requests = []
        self.embedding_model = 'embed-v1'
        self.rerank_model = 'rerank-v1'
        self.rerank_scores = None
        self.status = 200

    def __call__(self, request):
        body = json.loads(request.content)
        self.requests.append((str(request.url), body))
        if self.status != 200:
            return httpx.Response(self.status, json={'error': 'unavailable'})
        if str(request.url) == EMBED_URL:
            return httpx.Response(200, json={'model': self.embedding_model, 'data': [
                {'index': i, 'embedding': vector(text)} for i, text in enumerate(body['input'])]})
        scores = self.rerank_scores or [1.0 if '신청자격' in d else 0.1 for d in body['documents']]
        return httpx.Response(200, json={'model': self.rerank_model, 'results': [
            {'index': i, 'relevance_score': s} for i, s in enumerate(scores)]})


@pytest.fixture
def setup(tmp_path):
    store = Store(str(tmp_path / 'db.sqlite3'))
    ingest(store.path, SAMPLES)
    fake = FakeModels()
    retriever = Retriever(store, embedding_url=EMBED_URL, rerank_url=RERANK_URL, embedding_model='embed-v1',
                          rerank_model='rerank-v1', transport=httpx.MockTransport(fake))
    yield store, fake, retriever
    retriever.close()


def test_index_then_search_returns_ranked_chunks_with_sources(setup):
    store, fake, retriever = setup
    report = retriever.build_index()
    revision, chunks = store.table_snapshot('chunks')
    assert report == {'revision': revision, 'chunks': len(chunks), 'dimension': 5, 'embedding_model': 'embed-v1'}
    # Documents are embedded with title and section, not body text alone.
    assert fake.requests[0][1]['input'][0] == document_text(chunks[0])

    result = retriever.search('이중전공 신청자격은?', top_k=3)
    assert result['status'] == 'evidence_available' and result['score_type'] == 'reranker'
    assert result['min_rerank_score'] is None  # nothing is judged without a calibrated threshold
    results = result['evidence']
    assert len(results) == 3
    assert results[0]['section'] == '신청자격' and results[0]['score_type'] == 'reranker'
    assert results[0]['url'].startswith('https://registrar.korea.ac.kr/') and results[0]['collected_at']
    assert [r['score'] for r in results] == sorted((r['score'] for r in results), reverse=True)
    url, body = fake.requests[-1]
    assert url == RERANK_URL and body['documents'][0].startswith(chunks[0]['doc_title'])


def test_search_without_reranker_uses_dense_order(setup):
    store, fake, retriever = setup
    retriever.build_index()
    dense = Retriever(store, embedding_url=EMBED_URL, embedding_model='embed-v1', use_reranker=False,
                      transport=httpx.MockTransport(fake))
    results = dense.search('평가기준이 뭐야', top_k=2)['evidence']
    assert results[0]['section'] == '평가기준' and results[0]['score_type'] == 'dense_cosine'
    assert all(url == EMBED_URL for url, _ in fake.requests)
    # An index built for another endpoint or model is not reused.
    other = Retriever(store, embedding_url=EMBED_URL, embedding_model='embed-v2', use_reranker=False,
                      transport=httpx.MockTransport(fake))
    with pytest.raises(RuntimeError):
        other.search('평가기준', top_k=1)


def test_vectors_are_decoded_once_and_follow_data_revision(setup, monkeypatch):
    store, fake, retriever = setup
    retriever.build_index()
    calls = []
    original = store.load_vectors
    monkeypatch.setattr(store, 'load_vectors', lambda *a: calls.append(a) or original(*a))
    retriever.search('정의', top_k=1)
    retriever.search('접수방법', top_k=1)
    assert len(calls) == 1
    revision, chunks = store.table_snapshot('chunks')
    store.replace(chunks[:-1], store.table_snapshot('events')[1])  # new revision drops vectors
    with pytest.raises(RuntimeError):
        retriever.search('정의', top_k=1)
    # A caller holding the old snapshot cannot get results for a mismatched index.
    with pytest.raises(RuntimeError):
        retriever.search('정의', top_k=1, chunks=chunks, revision='stale-revision')


def test_model_failures_raise_instead_of_returning_no_evidence(setup):
    store, fake, retriever = setup
    retriever.build_index()
    fake.embedding_model = 'another-model'  # endpoint ignores the requested name
    with pytest.raises(ValueError, match='unexpected model'):
        retriever.search('신청자격', top_k=1)
    fake.embedding_model = 'embed-v1'
    fake.rerank_scores = [0.5]  # fewer scores than candidates
    with pytest.raises(ValueError, match='reranking'):
        retriever.search('신청자격', top_k=1)
    fake.rerank_scores, fake.status = None, 503
    with pytest.raises(httpx.HTTPStatusError):
        retriever.search('신청자격', top_k=1)


def test_input_validation_empty_corpus_and_deadline(setup, tmp_path):
    store, fake, retriever = setup
    with pytest.raises(RuntimeError):  # no index yet
        retriever.search('신청자격')
    for query, top_k in (('  ', 5), ('질문', 0), ('질문', True)):
        with pytest.raises(ValueError):
            retriever.search(query, top_k)
    with pytest.raises(ValueError):
        retriever.search('질문', chunks=[])
    with pytest.raises(ValueError):
        Retriever(store, embedding_url=EMBED_URL)  # reranking enabled without a reranker URL

    empty = Retriever(Store(str(tmp_path / 'empty.sqlite3')), embedding_url=EMBED_URL, use_reranker=False,
                      transport=httpx.MockTransport(fake))
    before = len(fake.requests)
    result = empty.search('질문')
    assert result['status'] == 'insufficient_evidence' and result['evidence'] == []
    assert len(fake.requests) == before

    retriever.build_index()
    token = current_budget.set(Budget(time.monotonic() - 1))
    try:
        with pytest.raises(DeadlineExceeded):
            retriever.search('신청자격')
    finally:
        current_budget.reset(token)


def test_from_env_reads_separate_endpoints(monkeypatch, tmp_path):
    monkeypatch.setenv('RETRIEVAL_EMBEDDING_URL', EMBED_URL)
    monkeypatch.setenv('RETRIEVAL_RERANK_URL', RERANK_URL)
    monkeypatch.setenv('RETRIEVAL_CANDIDATES', '10')
    retriever = Retriever.from_env(Store(str(tmp_path / 'db.sqlite3')))
    profile = retriever.profile()
    assert profile['embedding_url'] == EMBED_URL and profile['rerank_url'] == RERANK_URL
    assert profile['embedding_model'] == 'furiosa-ai/bge-m3' and profile['candidates'] == 10
    assert profile['min_rerank_score'] is None
    monkeypatch.setenv('RETRIEVAL_MIN_RERANK_SCORE', '0.2')
    judged = Retriever.from_env(Store(str(tmp_path / 'db.sqlite3')))
    assert judged.profile()['min_rerank_score'] == 0.2
    judged.close()
    retriever.close()


def test_threshold_decides_found_or_insufficient(setup):
    store, fake, retriever = setup
    retriever.build_index()
    judged = Retriever(store, embedding_url=EMBED_URL, rerank_url=RERANK_URL, embedding_model='embed-v1',
                       rerank_model='rerank-v1', min_rerank_score=0.5, transport=httpx.MockTransport(fake))
    found = judged.search('이중전공 신청자격은?', top_k=2)
    assert found['status'] == 'found' and found['evidence'][0]['score'] == 1.0
    assert found['min_rerank_score'] == 0.5
    count = len(store.table_snapshot('chunks')[1])
    fake.rerank_scores = [0.49] * count  # every candidate just below the threshold
    low = judged.search('이중전공 합격 커트라인', top_k=2)
    # Candidates are still returned, but marked as not sufficient.
    assert low['status'] == 'insufficient_evidence' and len(low['evidence']) == 2
    fake.rerank_scores = [0.5] * count  # the threshold itself counts as found
    assert judged.search('경계값', top_k=1)['status'] == 'found'
    judged.close()
    for bad in (dict(min_rerank_score=0.5, use_reranker=False), dict(min_rerank_score=float('nan')),
                dict(min_rerank_score=True)):
        with pytest.raises(ValueError):
            Retriever(store, embedding_url=EMBED_URL, rerank_url=RERANK_URL, **bad)
