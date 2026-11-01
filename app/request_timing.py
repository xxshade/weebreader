"""Optional request-local wall-time phases; never SQL, identifiers or history."""
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from time import perf_counter

from starlette.responses import JSONResponse

_PHASES = ('db_acquire', 'auth', 'home_data', 'render', 'revalidate')
_HOME_PHASES = ('home_queries', 'home_estimator', 'home_series', 'home_novels',
                'home_tracking', 'home_cards', 'home_reading')
_SERIES_PHASES = ('series_query', 'series_prepare', 'series_aggregate')
_ITEM_PHASES = ('items_query', 'items_positions', 'items_rules', 'items_stat',
                'items_index', 'items_snapshots')
_API_PHASES = ('api_auth', 'api_slot', 'api_db', 'api_input', 'api_estimator',
               'api_cache', 'api_fallback', 'api_encode', 'api_flush')
_API_CACHE_PHASES = ('api_validate', 'api_targets')
_API_VALIDATION_PHASES = ('api_files', 'api_index', 'api_revisions')
_api_enabled: ContextVar[bool] = ContextVar('api_timing_enabled', default=False)
_api_validating: ContextVar[bool] = ContextVar('api_timing_validation', default=False)
_items_enabled: ContextVar[bool] = ContextVar('item_timing_enabled', default=False)
_item_span: ContextVar[list | None] = ContextVar('item_timing_span', default=None)
_metrics: ContextVar[dict | None] = ContextVar('request_timing', default=None)


@contextmanager
def collect(enabled, *, api=False):
    token = _metrics.set({} if enabled else None)
    api_token = _api_enabled.set(enabled and api)
    validation_token = _api_validating.set(False)
    try:
        yield
    finally:
        _api_validating.reset(validation_token)
        _api_enabled.reset(api_token)
        _metrics.reset(token)


def api_enabled():
    return _api_enabled.get() and _metrics.get() is not None


def api_start():
    return perf_counter() if api_enabled() else None


def api_phase(name):
    if not api_enabled() or (name in _API_VALIDATION_PHASES and not _api_validating.get()):
        return nullcontext()
    return _measure_api_phase(name)


@contextmanager
def _measure_api_phase(name):
    token = _api_validating.set(True) if name == 'api_validate' else None
    started = perf_counter()
    try:
        yield
    finally:
        finish(name, started)
        if token is not None:
            _api_validating.reset(token)


class EstimateJSONResponse(JSONResponse):
    def render(self, content):
        # FastAPI's preceding response conversion remains in api_other.
        with api_phase('api_encode'):
            return super().render(content)


@contextmanager
def item_preparation():
    """Only the home series preparation contributes item-level timings."""
    enabled = _items_enabled.set(_metrics.get() is not None and not api_enabled())
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
        if name not in (_PHASES + _HOME_PHASES + _SERIES_PHASES + _ITEM_PHASES
                        + _API_PHASES + _API_CACHE_PHASES + _API_VALIDATION_PHASES):
            raise ValueError('Unknown timing phase')
        metrics[name] = metrics.get(name, 0.0) + (perf_counter() - started) * 1000


def headers(total):
    metrics = _metrics.get()
    if metrics is None:
        return []
    if api_enabled():
        def residual(parent, children):
            return max(0.0, parent - sum(metrics.get(name, 0.0) for name in children))
        return [f'{name};dur={metrics.get(name, 0.0):.3f}'
                for name in _API_PHASES] + [
            f'api_other;dur={residual(total, _API_PHASES):.3f}'] + [
            f'{name};dur={metrics.get(name, 0.0):.3f}'
            for name in _API_CACHE_PHASES] + [
            f'api_cache_other;dur={residual(metrics.get("api_cache", 0.0), _API_CACHE_PHASES):.3f}'] + [
            f'{name};dur={metrics.get(name, 0.0):.3f}'
            for name in _API_VALIDATION_PHASES] + [
            f'api_validate_other;dur={residual(metrics.get("api_validate", 0.0), _API_VALIDATION_PHASES):.3f}']
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
