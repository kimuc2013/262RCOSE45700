"""Serving-only recovery for read-only embedding/reranking HTTP requests."""
import time
from contextvars import ContextVar

import httpx

from .runtime import checkpoint, remaining_timeout
from .call_metrics import record_attempt, timed_stage
from .http_timing import post as traced_post

physical_attempts = ContextVar('retrieval_http_attempts', default=0)


class ReliableModelClient:
    """One retry for broken connections, sharing the original call deadline.

    The inner client is observed per attempt. HTTP statuses, read timeouts and
    malformed model responses are not retried. Never used for generation/writes.
    """
    def __init__(self, client):
        self.client = client

    def __getattr__(self, name):
        return getattr(self.client, name)

    def post(self, *args, **kwargs):
        timeout = kwargs.get('timeout', 15)
        deadline = time.monotonic() + remaining_timeout(timeout)
        for attempt in range(2):
            checkpoint()
            left = deadline - time.monotonic()
            if left <= 0:
                raise httpx.ReadTimeout('Model HTTP time budget exhausted')
            kwargs['timeout'] = remaining_timeout(left)
            try:
                physical_attempts.set(physical_attempts.get()+1)
                body = kwargs.get('json') or {}
                stage='embedding' if 'input' in body else 'rerank'
                record_attempt(stage)
                with timed_stage(stage):
                    return traced_post(self.client,stage,*args,**kwargs)
            except (httpx.ReadError, httpx.RemoteProtocolError,
                    httpx.ConnectError, httpx.ConnectTimeout):
                checkpoint()
                if attempt or deadline - time.monotonic() < .1:
                    raise
                # httpx discards broken connections; the next attempt reconnects.
