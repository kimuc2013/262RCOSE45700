"""Integration with the real Retriever and a deterministic HTTP model transport."""
import asyncio
import threading

import httpx
import pytest

from academic_mcp.prepare_retrieval import ensure_index
from academic_mcp.runtime import DeadlineExceeded, Limits
from academic_mcp.service import AcademicService, build_service
from test_retrieval import setup as retrieval_setup  # noqa: F401
from test_runtime import until


def test_module_owns_status_sources_and_cache_profile(retrieval_setup):
    store, fake, retriever = retrieval_setup
    retriever.build_index()
    fake.requests.clear()
    service = AcademicService(store, retriever=retriever, rerank_min_score=2.0)
    result = service.search('이중전공 신청자격은?', top_k=3)
    assert result['status'] == 'evidence_available'  # legacy serving threshold has no effect
    assert result['retrieval_mode'] == 'retrieval'
    assert result['score_type'] == 'reranker'
    assert result['evidence'][0]['section'] == '신청자격'
    assert all(e['content_kind'] == 'untrusted_source_excerpt' for e in result['evidence'])
    assert result['metrics']['model_calls'] == 2
    assert len(fake.requests) == 2
    assert service.search('이중전공 신청자격은?', top_k=3)['cache_hit']
    assert len(fake.requests) == 2
    retriever.min_rerank_score = 0.9
    assert service.search('이중전공 신청자격은?', top_k=3)['status'] == 'found'
    retriever.min_rerank_score = 1.1
    result = service.search('이중전공 신청자격은?', top_k=3)
    assert result['status'] == 'insufficient_evidence' and not result['cache_hit']
    retriever.candidates = 7
    assert not service.search('이중전공 신청자격은?', top_k=3)['cache_hit']


def test_new_revision_requires_offline_index_and_never_falls_back(retrieval_setup):
    store, fake, retriever = retrieval_setup
    service = AcademicService(store, retriever=retriever)
    with pytest.raises(RuntimeError, match='index'):
        service.search('이중전공 자격')
    assert not fake.requests  # never embeds all documents during a search
    assert ensure_index(store, retriever)['changed']
    assert not ensure_index(store, retriever)['changed']
    service.search('이중전공 자격')
    _, chunks, events = store.snapshot()
    chunks[0]['text'] += ' changed'
    store.replace(chunks, events)
    with pytest.raises(RuntimeError, match='index'):
        service.search('이중전공 자격')
    assert ensure_index(store, retriever)['changed']
    result = service.search('이중전공 자격')
    assert result['revision'] == store.revision() and not result['cache_hit']
    fake.status = 503
    with pytest.raises(httpx.HTTPStatusError):
        service.search('이중전공 정의')


def test_retrieval_mode_does_not_require_legacy_model_settings(tmp_path, monkeypatch):
    monkeypatch.setenv('ACADEMIC_DB', str(tmp_path / 'startup.sqlite3'))
    monkeypatch.setenv('MODEL_MODE', 'retrieval')
    monkeypatch.setenv('RETRIEVAL_EMBEDDING_URL', 'http://embed.test/v1/embeddings')
    monkeypatch.setenv('RETRIEVAL_RERANK_URL', 'http://rerank.test/v1/rerank')
    monkeypatch.delenv('MODEL_BASE_URL', raising=False)
    service = build_service()  # construction makes no network calls
    assert service.models is None and service.retriever is not None
    assert service.profile()['retriever'] == service.retriever.profile()
    service.close()
    assert service.retriever._client.is_closed


def test_concurrent_calls_merge_and_hot_cache_skips_model(retrieval_setup, monkeypatch):
    store, fake, retriever = retrieval_setup
    retriever.build_index()
    fake.requests.clear()
    started, release = threading.Event(), threading.Event()
    original = retriever.embed
    def embed(texts):
        started.set()
        assert release.wait(2)
        return original(texts)
    monkeypatch.setattr(retriever, 'embed', embed)
    service = AcademicService(store, retriever=retriever)
    async def run():
        tasks = [asyncio.create_task(service.asearch('이중전공 자격')) for _ in range(8)]
        await until(started.is_set)
        await until(lambda: service.requests._waiters == 8)
        release.set()
        results = await asyncio.gather(*tasks)
        assert all(r['status'] == 'evidence_available' for r in results)
        assert sum(r['metrics']['model_calls'] for r in results) == 2
        assert len(fake.requests) == 2
        hits = await asyncio.gather(*[service.asearch('이중전공 자격') for _ in range(8)])
        assert all(r['metrics']['cache_fast_path'] for r in hits)
        assert len(fake.requests) == 2
    try:
        asyncio.run(run())
    finally:
        release.set()


def test_serving_deadline_stops_retriever_before_rerank(retrieval_setup, monkeypatch):
    store, fake, retriever = retrieval_setup
    retriever.build_index()
    fake.requests.clear()
    started, release = threading.Event(), threading.Event()
    original = retriever.embed
    def slow_embed(texts):
        started.set()
        assert release.wait(2)
        return original(texts)
    monkeypatch.setattr(retriever, 'embed', slow_embed)
    service = AcademicService(store, retriever=retriever, limits=Limits(request_timeout=0.05))
    async def run():
        task = asyncio.create_task(service.asearch('이중전공 자격'))
        await until(started.is_set)
        try:
            with pytest.raises(DeadlineExceeded):
                await task
        finally:
            release.set()
            await until(lambda: service.requests._admitted == 0)
        assert not fake.requests  # checkpoint stops before sending even embedding
        with store.connect() as db:
            assert db.execute('SELECT count(*) FROM cache').fetchone()[0] == 0
    asyncio.run(run())


def test_snapshot_import_option_builds_new_index(retrieval_setup, monkeypatch, capsys):
    import json
    from academic_mcp.ingest import main
    from test_service import SAMPLES
    store, fake, retriever = retrieval_setup
    monkeypatch.setattr('academic_mcp.retrieval.Retriever.from_env', lambda store: retriever)
    monkeypatch.setattr('sys.argv', ['ingest', '--db', store.path, '--samples', str(SAMPLES), '--reindex'])
    main()
    report = json.loads(capsys.readouterr().out)
    assert report['index']['revision'] == store.revision()
    assert report['index']['chunks'] == len(store.table_snapshot('chunks')[1])
    assert store.load_vectors(store.revision(), retriever.index_key)
