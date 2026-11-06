"""Per-process admission and singleflight for online requests.

Caller timeouts never pretend that a synchronous worker has stopped. A slot is
held until the worker exits; cooperative checkpoints prevent later stages and
cache writes after abandonment. A model call already sent cannot be retracted.
"""
import asyncio
import copy
import math
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass, field


class ServingError(RuntimeError):
    code = 'SERVING_ERROR'


class Overloaded(ServingError):
    code = 'SERVER_BUSY'


class DeadlineExceeded(ServingError):
    code = 'REQUEST_DEADLINE_EXCEEDED'


class QueueTimeout(ServingError):
    code = 'QUEUE_TIMEOUT'


class DataChanged(ServingError):
    code = 'DATA_CHANGED'


class RequestAbandoned(ServingError):
    code = 'REQUEST_CANCELLED'


@dataclass(frozen=True)
class Limits:
    max_running: int = 4
    max_queued: int = 8
    max_waiters: int = 64
    request_timeout: float = 10.0
    queue_timeout: float = 1.0

    def __post_init__(self):
        for key in ('max_running', 'max_queued', 'max_waiters'):
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, int) or value < (0 if key == 'max_queued' else 1):
                raise ValueError(f'Invalid {key}')
        for key in ('request_timeout', 'queue_timeout'):
            value = getattr(self, key)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f'{key} must be positive and finite')


@dataclass
class Budget:
    deadline: float
    abandoned: threading.Event = field(default_factory=threading.Event)

    def remaining(self):
        self.check()
        return self.deadline - time.monotonic()

    def check(self):
        if self.abandoned.is_set():
            raise RequestAbandoned('All callers stopped waiting')
        if time.monotonic() >= self.deadline:
            raise DeadlineExceeded('Request time budget exhausted')


current_budget: ContextVar[Budget | None] = ContextVar('request_budget', default=None)


def checkpoint():
    budget = current_budget.get()
    if budget:
        budget.check()


def remaining_timeout(configured: float):
    budget = current_budget.get()
    return min(configured, budget.remaining()) if budget else configured


@dataclass
class Flight:
    budget: Budget
    task: asyncio.Task | None = None
    waiters: int = 0
    started: bool = False
    queue_ms: float = 0


class RequestController:
    """One controller per event loop; bounded unique jobs AND total waiting callers."""

    def __init__(self, limits: Limits):
        self.limits = limits
        self._flights: dict[str, Flight] = {}
        self._admitted = self._waiters = self.running = 0
        self._loop = self._semaphore = None

    @property
    def queued(self):
        return self._admitted - self.running

    async def execute(self, key, function, *, deadline=None):
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop, self._semaphore = loop, asyncio.Semaphore(self.limits.max_running)
        if self._loop is not loop:
            raise RuntimeError('RequestController must stay on one event loop')
        if deadline is not None and time.monotonic() >= deadline:
            raise DeadlineExceeded('Request time budget exhausted before admission')
        if self._waiters >= self.limits.max_waiters:
            raise Overloaded('Too many callers waiting; retry later')
        flight = self._flights.get(key)
        coalesced = flight is not None
        if flight and flight.budget.abandoned.is_set():
            raise Overloaded('Previous identical work is still draining; retry later')
        if flight is None:
            if self._admitted >= self.limits.max_running + self.limits.max_queued:
                raise Overloaded('Execution and queue capacity exhausted; retry later')
            flight = Flight(Budget(min(deadline, time.monotonic() + self.limits.request_timeout)
                                   if deadline is not None else time.monotonic() + self.limits.request_timeout))
            self._flights[key] = flight
            self._admitted += 1
            flight.task = asyncio.create_task(self._run(flight, function))
            flight.task.add_done_callback(lambda task: self._cleanup(key, flight, task))
        flight.waiters += 1
        self._waiters += 1
        try:
            # The first caller owns the work deadline. Joining never extends it.
            wait = flight.budget.remaining()
            if deadline is not None:
                wait = min(wait, max(0, deadline - time.monotonic()))
            async with asyncio.timeout(wait):
                result = await asyncio.shield(flight.task)
            return copy.deepcopy(result), coalesced, flight.queue_ms
        except TimeoutError as exc:
            raise DeadlineExceeded('Request time budget exhausted') from exc
        finally:
            flight.waiters -= 1
            self._waiters -= 1
            if not flight.waiters and not flight.task.done():
                flight.budget.abandoned.set()
                if not flight.started:
                    flight.task.cancel()  # Queued work has no worker to drain.

    async def _run(self, flight, function):
        acquired = False
        started = time.monotonic()
        try:
            wait = min(self.limits.queue_timeout, flight.budget.remaining())
            try:
                async with asyncio.timeout(wait):
                    await self._semaphore.acquire()
            except TimeoutError as exc:
                flight.budget.check()
                raise QueueTimeout('Queue wait budget exhausted; retry later') from exc
            acquired = True
            flight.budget.check()
            flight.started = True
            self.running += 1
            flight.queue_ms = round((time.monotonic() - started) * 1000, 3)

            def worker():
                token = current_budget.set(flight.budget)
                try:
                    checkpoint()
                    result = function()
                    checkpoint()
                    return result
                finally:
                    current_budget.reset(token)

            # Caller cancellation is shielded in execute(), so this slot remains
            # occupied until to_thread actually returns, even after a deadline.
            return await asyncio.to_thread(worker)
        finally:
            if acquired:
                self._semaphore.release()
            if flight.started:
                self.running -= 1

    def _cleanup(self, key, flight, task):
        if self._flights.get(key) is flight:
            del self._flights[key]
        self._admitted -= 1
        if not task.cancelled():
            task.exception()  # Drain errors even if every caller has disconnected.
