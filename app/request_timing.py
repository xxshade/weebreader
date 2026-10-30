"""Optional request-local wall-time phases; never SQL, identifiers or history."""
from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter

_PHASES = ('db_acquire', 'auth', 'home_data', 'render', 'revalidate')
_HOME_PHASES = ('home_queries', 'home_estimator', 'home_series', 'home_novels',
                'home_tracking', 'home_cards', 'home_reading')
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
        if name not in _PHASES + _HOME_PHASES:
            raise ValueError('Unknown timing phase')
        metrics[name] = metrics.get(name, 0.0) + (perf_counter() - started) * 1000


def headers(total):
    metrics = _metrics.get()
    if metrics is None:
        return []
    # Home children overlap their parent. Subtract only top-level phases
    # from app, and only children from home_data. Never double-count them.
    return [f'{name};dur={metrics.get(name, 0.0):.3f}' for name in _PHASES] + [
        f'other;dur={max(0.0, total - sum(metrics.get(name, 0.0) for name in _PHASES)):.3f}'] + [
        f'{name};dur={metrics.get(name, 0.0):.3f}' for name in _HOME_PHASES] + [
        f'home_other;dur={max(0.0, metrics.get("home_data", 0.0) - sum(metrics.get(name, 0.0) for name in _HOME_PHASES)):.3f}']
