"""Optional request-local wall-time phases; never SQL, identifiers or history."""
from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter

_PHASES = ('db_acquire', 'auth', 'home_data', 'render', 'revalidate')
_metrics: ContextVar[dict | None] = ContextVar('request_timing', default=None)


@contextmanager
def collect(enabled):
    token = _metrics.set({} if enabled else None)
    try:
        yield
    finally:
        _metrics.reset(token)


def start():
    return perf_counter() if _metrics.get() is not None else None


def finish(name, started):
    metrics = _metrics.get()
    if metrics is not None and started is not None:
        if name not in _PHASES:
            raise ValueError('Unknown timing phase')
        metrics[name] = metrics.get(name, 0.0) + (perf_counter() - started) * 1000


def headers(total):
    metrics = _metrics.get()
    if metrics is None:
        return []
    # These are sequential phases, not nested query timings. Keep the
    # unmeasured framework and scheduling work visible as a residual.
    return [f'{name};dur={metrics.get(name, 0.0):.3f}' for name in _PHASES] + [
        f'other;dur={max(0.0, total - sum(metrics.values())):.3f}']
