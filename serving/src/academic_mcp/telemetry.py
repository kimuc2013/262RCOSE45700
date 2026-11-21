"""Bounded, content-free serving telemetry. Physical API work is counted once.

This database is independent of the academic snapshot/cache. Only the writer
thread touches it on the serving path. Missing provider usage is unknown, not 0.
"""
import asyncio
from contextvars import ContextVar
import json
import queue
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path

trace = ContextVar('telemetry_trace', default=('', 'live'))
REQUEST_FIELDS = {'tool', 'status', 'error_type', 'elapsed_ms', 'cache_hit',
    'coalesced', 'retrieval_cache_hit', 'retrieval_coalesced', 'answer_mode', 'can_answer', 'response_bytes'}
API_FIELDS = {'stage', 'model', 'elapsed_ms', 'status_code', 'error_type',
    'prompt_tokens', 'completion_tokens', 'total_tokens', 'cached_prompt_tokens'}


def count(value):
    return value if type(value) is int and 0 <= value <= 10**10 else None


def usage(data):
    raw = data.get('usage') if isinstance(data, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    detail = raw.get('prompt_tokens_details')
    detail = detail if isinstance(detail, dict) else {}
    return {k: count(v) for k, v in {
        'prompt_tokens': raw.get('prompt_tokens', raw.get('input_tokens')),
        'completion_tokens': raw.get('completion_tokens', raw.get('output_tokens')),
        'total_tokens': raw.get('total_tokens'),
        'cached_prompt_tokens': detail.get('cached_tokens')}.items()}


class Telemetry:
    def __init__(self, path, *, capacity=4096, retention_days=7, max_rows=100000):
        self.path = str(path)
        self.retention_days, self.max_rows = retention_days, max_rows
        self.run_id = uuid.uuid4().hex
        self.dropped = self.write_errors = 0
        self._queue = queue.Queue(maxsize=capacity)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._writer, name='serving-metrics', daemon=True)
        self._thread.start()

    def emit(self, kind, values):
        allowed = REQUEST_FIELDS if kind == 'request' else API_FIELDS
        # No raw question, history, answer, body, endpoint or credential fields.
        payload = {k: v for k, v in values.items() if k in allowed}
        ident, traffic = trace.get()
        row = (time.time(), kind, ident, traffic, json.dumps(payload, separators=(',', ':')))
        try:
            if self._stop.is_set():
                raise queue.Full
            self._queue.put_nowait(row)
        except queue.Full:
            with self._lock:
                self.dropped += 1

    async def observe(self, tool, call, *, experiment=False):
        started = time.monotonic()
        token = trace.set((uuid.uuid4().hex, 'experiment' if experiment else 'live'))
        values = {'tool': tool, 'status': 'error'}
        try:
            result = await call()
            metrics = result.setdefault('metrics', {})
            metrics['observation_id'] = trace.get()[0]
            values.update(status=result.get('status', 'unknown'),
                cache_hit=metrics.get('cache_hit', result.get('cache_hit')),
                coalesced=metrics.get('coalesced', False),
                retrieval_cache_hit=metrics.get('retrieval_cache_hit'),
                retrieval_coalesced=metrics.get('retrieval_coalesced', False),
                answer_mode=result.get('answer_mode'), can_answer=result.get('can_answer'))
            values['response_bytes'] = len(json.dumps(result, ensure_ascii=False).encode())
            return result
        except BaseException as exc:
            values['status'] = 'cancelled' if isinstance(exc, asyncio.CancelledError) else 'error'
            values['error_type'] = type(exc).__name__
            raise
        finally:
            values['elapsed_ms'] = round((time.monotonic() - started) * 1000, 3)
            self.emit('request', values)
            trace.reset(token)

    def close(self):
        self._stop.set()
        self._thread.join(timeout=3)

    def _writer(self):
        db = None
        while not self._stop.is_set() or not self._queue.empty():
            batch = []
            try:
                batch.append(self._queue.get(timeout=.1))
            except queue.Empty:
                continue
            for _ in range(99):
                try:
                    batch.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            try:
                if db is None:
                    Path(self.path).parent.mkdir(parents=True, exist_ok=True)
                    db = sqlite3.connect(self.path, timeout=.1)
                    db.execute('PRAGMA journal_mode=WAL')
                    db.executescript('''
                        CREATE TABLE IF NOT EXISTS telemetry_events (
                            id INTEGER PRIMARY KEY, ts REAL, kind TEXT,
                            trace_id TEXT, traffic TEXT, payload TEXT);
                        CREATE INDEX IF NOT EXISTS telemetry_time ON telemetry_events(ts);
                        CREATE TABLE IF NOT EXISTS telemetry_health (
                            run_id TEXT PRIMARY KEY, updated REAL, dropped INTEGER, write_errors INTEGER);
                    ''')
                with db:
                    db.executemany('INSERT INTO telemetry_events(ts,kind,trace_id,traffic,payload) VALUES(?,?,?,?,?)', batch)
                    db.execute('DELETE FROM telemetry_events WHERE ts<?', (time.time()-86400*self.retention_days,))
                    db.execute('DELETE FROM telemetry_events WHERE id <= '
                        '(SELECT max(id) FROM telemetry_events)-?', (self.max_rows,))
                    db.execute('INSERT OR REPLACE INTO telemetry_health VALUES(?,?,?,?)',
                        (self.run_id, time.time(), self.dropped, self.write_errors))
                    db.execute('DELETE FROM telemetry_health WHERE updated<?', (time.time()-86400*self.retention_days,))
            except Exception:
                # A broken/locked metrics DB must never fail an academic request.
                with self._lock:
                    self.write_errors += len(batch)
                if db is not None:
                    db.close()
                    db = None
            finally:
                for _ in batch:
                    self._queue.task_done()
        if db is not None:
            with db:
                db.execute('INSERT OR REPLACE INTO telemetry_health VALUES(?,?,?,?)',
                    (self.run_id, time.time(), self.dropped, self.write_errors))
            db.close()


class ObservedClient:
    """Transparent boundary around a pooled client's post, not model logic.

    Includes HTTP failures/transport timeouts and work completing after the
    initiating caller timed out. Coalesced callers do not repeat this code.
    """
    def __init__(self, client, telemetry, stage=None):
        self.client, self.telemetry, self.stage = client, telemetry, stage

    def __getattr__(self, key):
        return getattr(self.client, key)

    def post(self, *args, **kwargs):
        body = kwargs.get('json') or {}
        stage = self.stage or ('embedding' if 'input' in body else 'rerank')
        model = body.get('model', '')
        model = model if isinstance(model, str) and re.fullmatch(r'[\w./:-]{1,120}', model) and '://' not in model else 'unknown'
        started = time.monotonic()
        values = {'stage': stage, 'model': model}
        try:
            response = self.client.post(*args, **kwargs)
            values['status_code'] = response.status_code
            try:
                values.update(usage(response.json()))
            except (ValueError, TypeError):
                pass
            return response
        except BaseException as exc:
            values['error_type'] = type(exc).__name__
            raise
        finally:
            values['elapsed_ms'] = round((time.monotonic()-started)*1000, 3)
            self.telemetry.emit('api', values)
