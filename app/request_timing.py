"""Optional request-local wall-time phases; never SQL, identifiers or history."""
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from time import perf_counter

_PHASES = ('db_acquire', 'auth', 'home_data', 'render', 'revalidate')
_HOME_PHASES = ('home_queries', 'home_estimator', 'home_series', 'home_novels',
                'home_tracking', 'home_cards', 'home_reading')
_SERIES_PHASES = ('series_query', 'series_prepare', 'series_aggregate')
_ITEM_PHASES = ('items_query', 'items_positions', 'items_rules', 'items_stat',
                'items_index', 'items_snapshots')
_items_enabled: ContextVar[bool] = ContextVar('item_timing_enabled', default=False)
_item_span: ContextVar[list | None] = ContextVar('item_timing_span', default=None)
_metrics: ContextVar[dict | None] = ContextVar('request_timing', default=None)


@contextmanager
def collect(enabled):
    token = _metrics.set({} if enabled else None)
    try:
        yield
    finally:
        _metrics.reset(token)


@contextmanager
def item_preparation():
    """Only the home series preparation contributes item-level timings."""
    enabled = _items_enabled.set(_metrics.get() is not None)
    span = _item_span.set(None)
    try:
        yield
    finally:
        _item_span.reset(span)
        _items_enabled.reset(enabled)


def item_phase(name):
    # Most requests never collect these timings. Avoid constructing a
    # generator for every item when its measurement scope is inactive.
    if not _items_enabled.get() or _metrics.get() is None:
        return nullcontext()
    return _measure_item_phase(name)


@contextmanager
def _measure_item_phase(name):
    metrics = _metrics.get()
    if not _items_enabled.get() or metrics is None:
        yield
        return
    if name not in _ITEM_PHASES:
        raise ValueError('Unknown item timing phase')
    # Exclusive time: loading the index inside a snapshot lookup belongs
    # to items_index, not also to items_snapshots. Accumulate repeated calls.
    parent = _item_span.get()
    children = [0.0]
    started = perf_counter()
    token = _item_span.set(children)
    try:
        yield
    finally:
        elapsed = perf_counter() - started
        _item_span.reset(token)
        if parent is not None:
            parent[0] += elapsed
        metrics[name] = metrics.get(name, 0.0) + max(0.0, elapsed - children[0]) * 1000


def start():
    return perf_counter() if _metrics.get() is not None else None


def finish(name, started):
    metrics = _metrics.get()
    if metrics is not None and started is not None:
        if name not in _PHASES + _HOME_PHASES + _SERIES_PHASES + _ITEM_PHASES:
            raise ValueError('Unknown timing phase')
        metrics[name] = metrics.get(name, 0.0) + (perf_counter() - started) * 1000


def headers(total):
    metrics = _metrics.get()
    if metrics is None:
        return []
    # Children overlap their parents. Subtract only direct children at
    # each level so nested timings are never counted twice.
    return [f'{name};dur={metrics.get(name, 0.0):.3f}' for name in _PHASES] + [
        f'other;dur={max(0.0, total - sum(metrics.get(name, 0.0) for name in _PHASES)):.3f}'] + [
        f'{name};dur={metrics.get(name, 0.0):.3f}' for name in _HOME_PHASES] + [
        f'home_other;dur={max(0.0, metrics.get("home_data", 0.0) - sum(metrics.get(name, 0.0) for name in _HOME_PHASES)):.3f}'] + [
        f'{name};dur={metrics.get(name, 0.0):.3f}' for name in _SERIES_PHASES] + [
        f'series_other;dur={max(0.0, metrics.get("home_series", 0.0) - sum(metrics.get(name, 0.0) for name in _SERIES_PHASES)):.3f}'] + [
        f'{name};dur={metrics.get(name, 0.0):.3f}' for name in _ITEM_PHASES] + [
        f'items_other;dur={max(0.0, metrics.get("series_prepare", 0.0) - sum(metrics.get(name, 0.0) for name in _ITEM_PHASES)):.3f}']
