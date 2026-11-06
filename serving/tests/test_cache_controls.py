import asyncio
import threading
from datetime import date

import pytest

from academic_mcp.cache import canonical_query
from academic_mcp.runtime import DataChanged, DeadlineExceeded, Limits
from academic_mcp.service import AcademicService
from test_service import service  # noqa: F401, fixture
from test_runtime import until


@pytest.mark.parametrize('left,right', [
    ('수강 신청 언제?', '수강신청 언제'),
    (' 이중전공\t신청자격? ', '이중전공 신청자격'),
])
def test_safe_spacing_variants(left, right):
    assert canonical_query(left) == canonical_query(right)


@pytest.mark.parametrize('left,right', [
    ('평점 3.5 이상', '평점 35 이상'), ('2026-1 신청', '2026-2 신청'),
    ('수강신청', '수강정정'), ('신청 가능', '신청 불가능'), ('신청해도 돼', '신청하면 안 돼'),
    ('신청 A B', '신청 AB'), ('3 5', '35'), ('2026/1', '20261'),
])
def test_semantically_distinct_queries_never_collapse(left, right):
    assert canonical_query(left) != canonical_query(right)


def test_cache_isolates_history_model_settings_and_reference_day(service):
    query = '이중전공 신청자격'
    assert not service.search(query)['cache_hit']
    assert service.search(query)['cache_hit']
    service.model_revision = 'new-weights'
    assert not service.search(query)['cache_hit']
    assert service.search(query)['cache_hit']
    service.today = lambda: date(2026, 10, 29)
    assert not service.search(query)['cache_hit']
    assert service.cache_key('그럼 자격은?', [{'role': 'user', 'content': '이중전공'}], 5, None, None) != service.cache_key(
        '그럼 자격은?', [{'role': 'user', 'content': '복수전공'}], 5, None, None)


def test_stale_revision_is_never_published_or_returned(service, monkeypatch):
    from academic_mcp import service as module
    original = module.bm25
    def changed(query, chunks):
        _, current_chunks, events = service.store.snapshot()
        current_chunks[0]['text'] += ' 갱신'
        service.store.replace(current_chunks, events)
        return original(query, chunks)
    monkeypatch.setattr(module, 'bm25', changed)
    with pytest.raises(DataChanged):
        service.search('이중전공 자격')
    with service.store.connect() as db:
        assert db.execute('SELECT count(*) FROM cache').fetchone()[0] == 0


def test_expired_work_stops_before_cache_write_and_calendar_is_isolated(service, monkeypatch):
    from academic_mcp import service as module
    original = module.bm25
    started, release = threading.Event(), threading.Event()
    def slow(query, chunks):
        started.set()
        assert release.wait(2)
        return original(query, chunks)
    monkeypatch.setattr(module, 'bm25', slow)
    online = AcademicService(service.store, limits=Limits(max_running=1, max_queued=0, request_timeout=0.04))
    async def run():
        task = asyncio.create_task(online.asearch('이중전공 자격'))
        await until(started.is_set)
        try:
            # Calendar requests keep their own execution lane, despite a blocked search.
            assert (await online.acalendar('2026-2', '이중전공'))['status'] == 'found'
            with pytest.raises(DeadlineExceeded):
                await asyncio.wait_for(task, 0.5)
        finally:
            release.set()
            await until(lambda: online.requests._admitted == 0)
        with service.store.connect() as db:
            assert db.execute('SELECT count(*) FROM cache').fetchone()[0] == 0
    asyncio.run(run())


def test_merged_online_requests_get_unique_ids_and_real_worker_count(service, monkeypatch):
    from academic_mcp import service as module
    original = module.bm25
    started, release = threading.Event(), threading.Event()
    calls = []
    def slow(query, chunks):
        calls.append(1)
        started.set()
        assert release.wait(2)
        return original(query, chunks)
    monkeypatch.setattr(module, 'bm25', slow)
    async def run():
        leader = asyncio.create_task(service.asearch('이중전공 자격'))
        await until(started.is_set)
        joined = [asyncio.create_task(service.asearch('이중전공 자격')) for _ in range(4)]
        await until(lambda: service.requests._waiters == 5)
        release.set()
        results = await asyncio.gather(leader, *joined)
        assert len(calls) == 1
        assert len({r['request_id'] for r in results}) == 5
        assert sum(r['metrics']['coalesced'] for r in results) == 4
        assert all('latency_ms' in r['metrics'] for r in results)
    asyncio.run(run())
