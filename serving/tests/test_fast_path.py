"""Exercise public async paths with independent callers and real SQLite."""
import asyncio
import threading

import pytest

from academic_mcp.runtime import DataChanged, Limits, Overloaded
from academic_mcp.service import AcademicService
from academic_mcp.store import Store
from test_service import service  # noqa: F401
from test_runtime import until


def test_cached_requests_bypass_saturated_search_and_never_load_corpus(service, monkeypatch):
    service.search('이중전공 자격')
    online = AcademicService(service.store, today=service.today, limits=Limits(max_running=1, max_queued=0))
    started, release = threading.Event(), threading.Event()
    original = online.search
    def slow(*args):
        started.set()
        assert release.wait(2)
        return original(*args)
    monkeypatch.setattr(online, 'search', slow)
    async def run():
        blocked = asyncio.create_task(online.asearch('이중전공 정의'))
        await until(started.is_set)
        try:
            results = await asyncio.gather(*[online.asearch('이중전공 자격') for _ in range(20)])
            assert all(r['cache_hit'] and r['metrics']['cache_fast_path'] for r in results)
            assert all(r['metrics']['model_calls'] == 0 for r in results)
            assert len({r['request_id'] for r in results}) == 20
            results[0]['evidence'][0]['text'] = 'caller changed'
            assert results[1]['evidence'][0]['text'] != 'caller changed'
            assert online.requests.running == 1
        finally:
            release.set()
            await blocked
    asyncio.run(run())


def test_hit_does_not_decode_snapshots(service, monkeypatch):
    service.search('이중전공 자격')
    def forbidden(*args):
        pytest.fail('cache hit loaded the corpus')
    monkeypatch.setattr(service.store, 'snapshot', forbidden)
    monkeypatch.setattr(service.store, 'table_snapshot', forbidden)
    assert service.search('이중전공 자격')['cache_hit']
    assert asyncio.run(service.asearch('이중전공 자격'))['metrics']['cache_fast_path']


def test_cache_rechecked_after_queue_wait(service, monkeypatch):
    online = AcademicService(service.store, today=service.today, limits=Limits(max_running=1, max_queued=1))
    started, release = threading.Event(), threading.Event()
    original = online.search
    def slow(query, *args):
        if query == '이중전공 정의':
            started.set()
            assert release.wait(2)
        return original(query, *args)
    monkeypatch.setattr(online, 'search', slow)
    async def run():
        first = asyncio.create_task(online.asearch('이중전공 정의'))
        await until(started.is_set)
        second = asyncio.create_task(online.asearch('이중전공 자격'))
        await until(lambda: online.requests.queued == 1)
        # Another producer publishes while the request is waiting for a slot.
        service.search('이중전공 자격')
        release.set()
        _, result = await asyncio.gather(first, second)
        assert result['cache_hit']
        assert not result['metrics']['cache_fast_path']
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["search", "calendar"])
def test_global_caller_limit_applies_before_db_reads(service, monkeypatch, kind):
    online = AcademicService(service.store, today=service.today, limits=Limits(max_waiters=1),
                             calendar_limits=Limits(max_waiters=1))
    def call():
        return online.asearch("이중전공 자격") if kind == "search" else online.acalendar("2026-2", "이중전공")
    entered, release = asyncio.Event(), asyncio.Event()
    original = online.read_revision
    async def blocked(deadline):
        entered.set()
        await release.wait()
        return await original(deadline)
    monkeypatch.setattr(online, 'read_revision', blocked)
    async def run():
        first = asyncio.create_task(call())
        await entered.wait()
        with pytest.raises(Overloaded):
            await call()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert online._search_callers == online._calendar_callers == 0
        release.set()
        assert (await call())['status'] == 'found'
    asyncio.run(run())


def test_decoded_snapshot_reuses_json_but_tracks_external_updates(service, monkeypatch):
    import academic_mcp.store as module
    original = module.json.loads
    calls = []
    def count(value):
        calls.append(1)
        return original(value)
    monkeypatch.setattr(module.json, 'loads', count)
    revision, chunks = service.store.table_snapshot('chunks')
    assert len(calls) == len(chunks)
    chunks[0]['text'] = 'caller mutation'
    _, again = service.store.table_snapshot('chunks')
    assert len(calls) == len(chunks)
    assert again[0]['text'] != 'caller mutation'
    _, updated, events = service.store.snapshot()
    updated[0]['text'] += ' externally updated'
    Store(service.store.path).replace(updated, events)
    new_revision, current = service.store.table_snapshot('chunks')
    assert new_revision != revision
    assert current[0]['text'].endswith('externally updated')


def test_fast_hit_still_rejects_data_changed_before_return(service, monkeypatch):
    service.search('이중전공 자격')
    original = service.cached_result
    def changed(*args):
        result = original(*args)
        _, chunks, events = service.store.snapshot()
        chunks[0]['text'] += ' updated'
        service.store.replace(chunks, events)
        return result
    monkeypatch.setattr(service, 'cached_result', changed)
    with pytest.raises(DataChanged):
        asyncio.run(service.asearch('이중전공 자격'))


def test_cancelling_cache_probe_leader_preserves_other_callers(service, monkeypatch):
    service.search('이중전공 자격')
    started, release = threading.Event(), threading.Event()
    original = service.cached_result
    calls = []
    def slow(*args):
        calls.append(1)
        started.set()
        assert release.wait(2)
        return original(*args)
    monkeypatch.setattr(service, 'cached_result', slow)
    async def run():
        leader = asyncio.create_task(service.asearch('이중전공 자격'))
        await until(started.is_set)
        follower = asyncio.create_task(service.asearch('이중전공 자격'))
        await until(lambda: service.cache_reads._waiters == 2)
        leader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await leader
        release.set()
        result = await follower
        assert result['metrics']['cache_fast_path']
        assert len(calls) == 1
        assert service._search_callers == 0
    asyncio.run(run())
