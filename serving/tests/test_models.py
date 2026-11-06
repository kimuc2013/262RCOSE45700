import json
from datetime import date

import httpx
import pytest
import time
import threading
import asyncio

from academic_mcp.models import RemoteModels
from academic_mcp.service import AcademicService
from academic_mcp.store import Store
from test_service import SAMPLES
from academic_mcp.ingest import ingest
from academic_mcp.runtime import Budget, current_budget, DeadlineExceeded, Limits
from test_runtime import until


@pytest.fixture
def models(monkeypatch):
    for key, value in {'MODEL_BASE_URL': 'http://model.test', 'EMBEDDING_MODEL': 'embed-v1',
                       'REWRITE_MODEL': 'rewrite-v1', 'RERANK_MODEL': 'rerank-v1'}.items():
        monkeypatch.setenv(key, value)
    return RemoteModels()


def test_embedding_response_validation(models, monkeypatch):
    monkeypatch.setattr(models, 'post', lambda *args: {'data': [{'index': 1, 'embedding': [1, 0]}, {'index': 0, 'embedding': [0, 1]}]})
    assert models.embed(['a', 'b']) == [[0, 1], [1, 0]]
    monkeypatch.setattr(models, 'post', lambda *args: {'data': [{'index': 0, 'embedding': [float('nan')]}]})
    with pytest.raises(ValueError):
        models.embed(['a'])


def test_remote_pipeline_contract_cache_and_failure(tmp_path, models, monkeypatch):
    store = Store(str(tmp_path / 'db.sqlite3'))
    ingest(store.path, SAMPLES)
    revision, chunks, _ = store.snapshot()
    store.save_vectors(revision, models.base + '|' + models.embedding, [[1, 0]] * len(chunks))
    requests = []

    def post(path, body):
        requests.append((path, body))
        if path == '/v1/chat/completions':
            return {'choices': [{'message': {'content': json.dumps({'query': '이중전공 자격', 'semester': '2026-2', 'target': '학부생'})}}]}
        if path == '/v1/embeddings':
            return {'data': [{'index': 0, 'embedding': [1, 0]}]}
        return {'results': [{'index': i, 'relevance_score': 0.9 - i * 0.01} for i in range(len(body['documents']))]}

    monkeypatch.setattr(models, 'post', post)
    service = AcademicService(store, models, today=lambda: date(2026, 10, 28), rerank_min_score=0.8)
    result = service.search('그럼 자격은?', [{'role': 'user', 'content': '이중전공'}])
    assert result['status'] == 'found'
    assert result['metrics']['model_calls'] == 3
    assert [path for path, _ in requests] == ['/v1/chat/completions', '/v1/embeddings', '/v1/rerank']
    assert service.search('그럼 자격은?', [{'role': 'user', 'content': '이중전공'}])['cache_hit']
    # Standalone questions never reuse context-dependent cache entries.
    assert not service.search('이중전공 자격', semester='2026-2', target='학부생')['cache_hit']
    other_history = [{'role': 'user', 'content': '복수전공'}]
    assert not service.search('그럼 자격은?', other_history)['cache_hit']
    assert service.search('그럼 자격은?', other_history)['cache_hit']
    # Service outages are technical failures, not evidence that the answer does not exist.
    monkeypatch.setattr(models, 'post', lambda *args: (_ for _ in ()).throw(httpx.ConnectError('offline')))
    with pytest.raises(httpx.ConnectError):
        service.search('이중전공 정의')


def test_no_vectors_and_no_threshold_are_not_silently_accepted(tmp_path, models, monkeypatch):
    store = Store(str(tmp_path / 'db.sqlite3'))
    ingest(store.path, SAMPLES)
    service = AcademicService(store, models)
    with pytest.raises(RuntimeError, match='vector index'):
        service.search('이중전공 자격')
    revision, chunks, _ = store.snapshot()
    store.save_vectors(revision, models.base + '|' + models.embedding, [[1, 0]] * len(chunks))
    monkeypatch.setattr(models, 'embed', lambda texts: [[1, 0]])
    monkeypatch.setattr(models, 'rerank', lambda query, docs: [(i, 0.99) for i in range(len(docs))])
    assert service.search('이중전공 자격')['status'] == 'insufficient_evidence'


def test_model_transport_clamps_timeout_to_remaining_budget(models, monkeypatch):
    observed = []
    class Client:
        def __init__(self, **kwargs):
            observed.append(kwargs['timeout'])
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def post(self, *args, **kwargs):
            return httpx.Response(200, json={'ok': True}, request=httpx.Request('POST', 'http://model.test'))
    monkeypatch.setattr(httpx, 'Client', Client)
    token = current_budget.set(Budget(time.monotonic() + 0.5))
    try:
        models.post('/test', {})
        assert 0 < observed[0] <= 0.5
    finally:
        current_budget.reset(token)


def test_deadline_stops_before_rerank_and_does_not_cache(tmp_path, models, monkeypatch):
    store = Store(str(tmp_path / 'db.sqlite3'))
    ingest(store.path, SAMPLES)
    revision, chunks, _ = store.snapshot()
    store.save_vectors(revision, models.base + '|' + models.embedding, [[1, 0]] * len(chunks))
    started, release = threading.Event(), threading.Event()
    rerank_calls = []
    def embed(texts):
        started.set()
        assert release.wait(2)
        return [[1, 0]]
    monkeypatch.setattr(models, 'embed', embed)
    monkeypatch.setattr(models, 'rerank', lambda *args: rerank_calls.append(1))
    online = AcademicService(store, models, limits=Limits(max_running=1, request_timeout=0.04))
    async def run():
        task = asyncio.create_task(online.asearch('이중전공 신청자격'))
        await until(started.is_set)
        try:
            with pytest.raises(DeadlineExceeded):
                await asyncio.wait_for(task, 0.5)
        finally:
            release.set()
            await until(lambda: online.requests._admitted == 0)
        assert not rerank_calls
        with store.connect() as db:
            assert db.execute('SELECT count(*) FROM cache').fetchone()[0] == 0
    asyncio.run(run())
