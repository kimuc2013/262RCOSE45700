"""Response metrics count server-side HTTP attempts, never client LLM calls."""
from contextvars import ContextVar
import threading
import time
from contextlib import contextmanager
from .release import release_metrics

STAGES = ('embedding', 'rerank', 'generation', 'rewrite')
class CallProgress:
    """A request-local snapshot shared with its synchronous worker thread."""
    def __init__(self):
        self.lock=threading.Lock()
        self.counts={stage+'_calls':0 for stage in STAGES}
        self.stage='cache_lookup'
        self.retrieval_complete=False
        self.elapsed={}
        self.active={}
        self.next_span=0

    def record(self,stage):
        with self.lock:
            self.counts[stage+'_calls']+=1
            self.stage=stage

    def phase(self,stage):
        with self.lock:
            self.stage=stage
            if stage=='answer_routing':self.retrieval_complete=True

    def snapshot(self):
        with self.lock:return dict(self.counts),self.stage,self.retrieval_complete

    def begin(self,stage):
        with self.lock:
            self.next_span+=1
            token=self.next_span
            self.active[token]=(stage,time.monotonic())
            return token

    def end(self,token):
        with self.lock:
            stage,start=self.active.pop(token)
            self.elapsed[stage]=self.elapsed.get(stage,0)+time.monotonic()-start

    def timings(self):
        with self.lock:
            values=dict(self.elapsed)
            now=time.monotonic()
            for stage,start in self.active.values():
                values[stage]=values.get(stage,0)+now-start
            return {stage:round(values.get(stage,0)*1000,3) for stage in
                    ('revision','cache_lookup','work_wait','retrieval',*STAGES,'answer_routing','cache_write')}


@contextmanager
def timed_stage(stage):
    tracker=progress.get()
    token=tracker.begin(stage) if tracker is not None else None
    try:yield
    finally:
        if tracker is not None:tracker.end(token)


def measured(stage,function,*args,**kwargs):
    with timed_stage(stage):return function(*args,**kwargs)


progress = ContextVar('model_call_progress', default=None)
attempts = ContextVar('model_stage_attempts', default=(0, 0, 0, 0))


def record_attempt(stage):
    current=progress.get()
    if current is not None:current.record(stage)
    values = list(attempts.get())
    values[STAGES.index(stage)] += 1
    attempts.set(tuple(values))


def since(before):
    return {stage + '_calls': now - old
            for stage, now, old in zip(STAGES, attempts.get(), before)}


def response_metrics(counts, elapsed_ms, *, shared=False, complete=True, observed=False):
    """Shared calls belong to the initiating request; expose work separately.

    A timed-out synchronous worker may still be running. Timeout snapshots expose attempts already started when observed=True; the
    eventual total remains incomplete. Telemetry includes late attempts.
    """
    values = {stage + '_calls': counts.get(stage + '_calls', 0) if complete or observed else None
              for stage in STAGES}
    total = sum(values.values()) if complete or observed else None
    own = {key: 0 if shared else value for key, value in values.items()}
    return {**own, 'model_calls': 0 if shared else total,
            'shared_model_calls': total, 'shared_model_calls_by_stage': values,
            'model_calls_complete': complete, 'model_calls_observed': complete or observed, 'model_calls_scope': 'server_http_attempts',
            'metrics_schema_version': 2, **release_metrics(),
            'stage_elapsed_ms': progress.get().timings() if progress.get() is not None else {},
            'stage_timings_complete':complete,
            'stage_timing_scope':'inclusive_nested_request_spans',
            'latency_ms': elapsed_ms, 'server_elapsed_ms': elapsed_ms}
