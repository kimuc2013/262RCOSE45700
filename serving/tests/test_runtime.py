import asyncio
import threading
import time

import pytest

from academic_mcp.runtime import (DeadlineExceeded, Limits, Overloaded,
                                 QueueTimeout, RequestController)


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.001)


def test_identical_requests_share_work_and_have_independent_results():
    async def run():
        controller = RequestController(Limits(max_running=1, max_queued=0))
        started, release = threading.Event(), threading.Event()
        calls = []

        def work():
            calls.append(1)
            started.set()
            assert release.wait(2)
            return {'items': [1]}

        leader = asyncio.create_task(controller.execute('same', work))
        await until(started.is_set)
        followers = [asyncio.create_task(controller.execute('same', work)) for _ in range(9)]
        await until(lambda: controller._waiters == 10)
        release.set()
        results = await asyncio.gather(leader, *followers)
        assert len(calls) == 1
        assert sum(coalesced for _, coalesced, _ in results) == 9
        results[0][0]['items'].append(2)
        assert results[1][0]['items'] == [1]
        await until(lambda: controller._admitted == 0)

    asyncio.run(run())


def test_bounded_queue_and_max_waiters():
    async def run():
        controller = RequestController(Limits(max_running=1, max_queued=1, max_waiters=3))
        started, release = threading.Event(), threading.Event()
        def work():
            started.set()
            assert release.wait(2)
            return {}
        one = asyncio.create_task(controller.execute('one', work))
        await until(started.is_set)
        two = asyncio.create_task(controller.execute('two', lambda: {}))
        await until(lambda: controller.queued == 1)
        with pytest.raises(Overloaded, match='capacity'):
            await controller.execute('three', lambda: {})
        joined = asyncio.create_task(controller.execute('one', work))
        await until(lambda: controller._waiters == 3)
        with pytest.raises(Overloaded, match='callers'):
            await controller.execute('one', work)
        release.set()
        await asyncio.gather(one, two, joined)

    asyncio.run(run())


def test_timeout_holds_slot_until_worker_actually_finishes():
    async def run():
        controller = RequestController(Limits(max_running=1, max_queued=0, request_timeout=0.04))
        started, release = threading.Event(), threading.Event()
        def work():
            started.set()
            assert release.wait(2)
            return {}
        task = asyncio.create_task(controller.execute('slow', work))
        await until(started.is_set)
        try:
            with pytest.raises(DeadlineExceeded):
                await asyncio.wait_for(task, 0.5)
            assert controller.running == 1
            with pytest.raises(Overloaded):
                await controller.execute('different', lambda: {})
        finally:
            release.set()
            await until(lambda: controller._admitted == 0)
        assert (await controller.execute('after-drain', lambda: {'ok': True}))[0]['ok']

    asyncio.run(run())


def test_queue_timeout_never_starts_queued_work():
    async def run():
        controller = RequestController(Limits(max_running=1, max_queued=1, queue_timeout=0.025))
        started, release = threading.Event(), threading.Event()
        calls = []
        def work():
            started.set()
            assert release.wait(2)
            return {}
        leader = asyncio.create_task(controller.execute('one', work))
        await until(started.is_set)
        try:
            with pytest.raises(QueueTimeout):
                await controller.execute('two', lambda: calls.append(1))
            assert not calls
        finally:
            release.set()
            await leader

    asyncio.run(run())


def test_one_cancelled_caller_does_not_cancel_others():
    async def run():
        controller = RequestController(Limits(max_running=1))
        started, release = threading.Event(), threading.Event()
        def work():
            started.set()
            assert release.wait(2)
            return {'ok': True}
        leader = asyncio.create_task(controller.execute('same', work))
        await until(started.is_set)
        follower = asyncio.create_task(controller.execute('same', work))
        await until(lambda: controller._waiters == 2)
        leader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await leader
        release.set()
        assert (await follower)[0]['ok']

    asyncio.run(run())


def test_cancelled_queue_and_failed_flight_are_cleaned_up():
    async def run():
        controller = RequestController(Limits(max_running=1, max_queued=1))
        started, release = threading.Event(), threading.Event()
        def work():
            started.set()
            assert release.wait(2)
            return {}
        leader = asyncio.create_task(controller.execute('one', work))
        await until(started.is_set)
        calls = []
        queued = asyncio.create_task(controller.execute('queued', lambda: calls.append(1)))
        await until(lambda: controller.queued == 1)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        await until(lambda: controller.queued == 0)
        release.set()
        await leader
        assert not calls
        def fail():
            raise RuntimeError('backend unavailable')
        with pytest.raises(RuntimeError, match='backend unavailable'):
            await controller.execute('failed', fail)
        await until(lambda: controller._admitted == 0)
        assert (await controller.execute('failed', lambda: 'recovered'))[0] == 'recovered'

    asyncio.run(run())


def test_shorter_follower_deadline_does_not_abort_shared_work():
    async def run():
        controller = RequestController(Limits(max_running=1))
        started, release = threading.Event(), threading.Event()
        def work():
            started.set()
            assert release.wait(2)
            return {'ok': True}
        leader = asyncio.create_task(controller.execute('same', work))
        await until(started.is_set)
        try:
            with pytest.raises(DeadlineExceeded):
                await controller.execute('same', work, deadline=time.monotonic() + 0.025)
            assert controller.running == 1
        finally:
            release.set()
        assert (await leader)[0]['ok']
    asyncio.run(run())


def test_total_deadline_applies_while_waiting_for_slot():
    async def run():
        controller = RequestController(Limits(max_running=1, max_queued=1,
                                              request_timeout=0.05, queue_timeout=1))
        started, release = threading.Event(), threading.Event()
        queued_calls = []
        def work():
            started.set()
            assert release.wait(2)
            return {}
        leader = asyncio.create_task(controller.execute('one', work))
        await until(started.is_set)
        queued = asyncio.create_task(controller.execute('two', lambda: queued_calls.append(1)))
        try:
            results = await asyncio.wait_for(asyncio.gather(leader, queued, return_exceptions=True), 0.5)
            assert all(isinstance(error, DeadlineExceeded) for error in results)
            assert not queued_calls
        finally:
            release.set()
            await until(lambda: controller._admitted == 0)
    asyncio.run(run())


@pytest.mark.parametrize('kwargs', [{'max_running': 0}, {'max_queued': -1}, {'max_waiters': 0},
                                   {'request_timeout': float('nan')}, {'queue_timeout': 0}])
def test_invalid_limits(kwargs):
    with pytest.raises(ValueError):
        Limits(**kwargs)
