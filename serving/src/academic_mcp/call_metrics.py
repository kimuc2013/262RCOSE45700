"""Response metrics count server-side HTTP attempts, never client LLM calls."""
from contextvars import ContextVar

STAGES = ('embedding', 'rerank', 'generation', 'rewrite')
attempts = ContextVar('model_stage_attempts', default=(0, 0, 0, 0))


def record_attempt(stage):
    values = list(attempts.get())
    values[STAGES.index(stage)] += 1
    attempts.set(tuple(values))


def since(before):
    return {stage + '_calls': now - old
            for stage, now, old in zip(STAGES, attempts.get(), before)}


def response_metrics(counts, elapsed_ms, *, shared=False, complete=True):
    """Shared calls belong to the initiating request; expose work separately.

    A timed-out synchronous worker may still be running. Unknown eventual counts
    are null, not zero; telemetry remains authoritative for late attempts.
    """
    values = {stage + '_calls': counts.get(stage + '_calls', 0) if complete else None
              for stage in STAGES}
    total = sum(values.values()) if complete else None
    own = {key: 0 if shared else value for key, value in values.items()}
    return {**own, 'model_calls': 0 if shared else total,
            'shared_model_calls': total, 'shared_model_calls_by_stage': values,
            'model_calls_complete': complete, 'model_calls_scope': 'server_http_attempts',
            'metrics_schema_version': 2,
            'latency_ms': elapsed_ms, 'server_elapsed_ms': elapsed_ms}
