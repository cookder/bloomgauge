"""Read-only per-model earnings outlook; never consumed by switch decisions."""

from demand_baselines import (
    finite,
    read_view,
    network_minutes,
    joined_minutes,
    summary,
    periods,
    conditional,
    repeatable,
)
from model_combinations import members
import copy
import math
import sqlite3
import threading
import weakref
from functools import lru_cache
from types import FunctionType, SimpleNamespace
import demand_baselines as baseline_calculations
from earnings_forecast import (
    build as build_forecast,
    presentation,
    LOOKBACK_SECONDS,
    REFRESH_SECONDS,
    METHOD_VERSION,
    read_paid_evidence,
)

BANDS = ((0, 0.25), (0.25, 0.5), (0.5, 1), (1, 2), (2, 4), (4, 8), (8, None))


def band_evidence(evidence, joined, low, high, now, calculations=None):
    summarize = calculations.summary if calculations else summary
    make_periods = calculations.periods if calculations else periods
    is_repeatable = calculations.repeatable if calculations else repeatable
    rows = [m for m in joined if m['pressure'] >= low and (high is None or m['pressure'] < high)]
    all_observed = summarize(rows)
    # Count time-separated, substantial periods, not hundreds of adjacent
    # seconds as independent evidence. Sparse network periods stay observations.
    exposure = {}
    overlap = {}
    for m in evidence.get('minutes', []):
        key = int(m['at'] // 1800) * 1800
        exposure[key] = exposure.get(key, 0) + m['seconds']
    for m in joined:
        key = int(m['at'] // 1800) * 1800
        overlap[key] = overlap.get(key, 0) + m['seconds']
    good = {k for k, seconds in exposure.items() if seconds and overlap.get(k, 0) / seconds >= 0.8}
    supported = [m for m in rows if int(m['at'] // 1800) * 1800 in good]
    blocks = [p for p in make_periods(supported) if p['hours'] >= 0.25]
    substantial = {p['at'] for p in blocks}
    supported = [m for m in supported if int(m['at'] // 1800) * 1800 in substantial]
    repeated = is_repeatable(supported, 4, 3, 8)
    # If repeated evidence exists, isolated extreme minutes don't inflate it.
    observed = summarize(supported) if repeated else all_observed
    recent = observed['asOf'] is not None and 0 <= now - observed['asOf'] <= 7 * 86400
    quality = (
        'none'
        if not rows
        else 'older'
        if not recent
        else 'repeated'
        if repeated and observed['paidJobs'] >= 200
        else 'limited'
    )
    return {
        'low': low,
        'high': high,
        **observed,
        'blocks': len(blocks),
        'quality': quality,
        'excludedThinHours': max(0, all_observed['hours'] - observed['hours']),
    }


def _historical_report(store, account, device, start, end, now, signals=None):
    if (
        not all(finite(v) for v in (start, end, now))
        or start < 0
        or end <= start
        or min(end, now) <= start
    ):
        raise ValueError('Choose a valid observed earnings range.')
    signals = signals or {}
    with read_view(store) as view:
        evidence = read_paid_evidence(view, account, device, start, min(end, now), now)
        evidence = {m: e for m, e in evidence.items() if len(members(m)) == 1}
        models = sorted(set(evidence) | set(signals))
        network = network_minutes(view, models, start, min(end, now - 120))
    rows = []
    calculations = _report_calculations()
    for model in models:
        earned = evidence.get(model, {})
        joined = joined_minutes(earned, network.get(model, {}), now)
        signal = signals.get(model, {})
        baseline = calculations.conditional(earned, network.get(model, {}), signal, now)
        rows.append(
            {
                'model': model,
                'serving': bool(signal.get('current')),
                'eligible': model in signals,
                'signalAt': signal.get('observedAt'),
                'totalWarmHours': earned.get('hours', 0),
                'pairedWarmHours': len(joined) / 60,
                'current': baseline,
                'bands': [band_evidence(earned, joined, a, b, now, calculations) for a, b in BANDS],
            }
        )
    calculations.clear()
    return {
        'at': now,
        'from': start,
        'to': min(end, now),
        'models': rows,
        'scope': 'This Mac · inference USD per verified warm hour · solo runs',
        'method': 'Current-demand estimates reuse the optimizer’s comparable-history calculation: pressure, active requests, total load and warm providers, with nearby time/day preferred when repeated evidence supports it. Demand bands are broader historical observations, not switch recommendations. Cold/loading time, base rewards, pairs, missing coverage and other devices are excluded; verified warm idle time remains included.',
    }


_CACHE_LOCK = threading.RLock()
_CACHES = weakref.WeakKeyDictionary()
HISTORY_CACHE_SECONDS = 120


def _state(store):
    with _CACHE_LOCK:
        if store not in _CACHES:
            _CACHES[store] = {'lock': threading.RLock(), 'history': {}, 'forecasts': {}}
        return _CACHES[store]


def _history_key(account, device, start, end, now):
    # Rolling presets have moving bounds; preserve the cached response's actual
    # from/to rather than claiming its historical query used later timestamps.
    bounds = ('rolling', round(end - start)) if abs(end - now) < 2 else ('fixed', start, end)
    return (account, device, bounds)


def _forecasts(state, store, account, device, now, signals):
    key = (account, device)
    old = state['forecasts'].get(key)
    journal = getattr(store, 'earnings_forecast_journal', None)
    if journal is not None and journal.enabled:
        latest = journal.latest(account, device, now)
        if latest is not None:
            return latest, now - latest['at'] >= REFRESH_SECONDS
    if old and 0 <= now - old['at'] < REFRESH_SECONDS:
        return old, False
    try:
        with read_view(store) as view:
            evidence = read_paid_evidence(
                view, account, device, max(0, now - LOOKBACK_SECONDS), now, now
            )
        evidence = {m: e for m, e in evidence.items() if len(members(m)) == 1}
        models = sorted(set(evidence) | set(signals))
        origin = int(math.ceil(now / 60)) * 60
        packet = {
            'at': now,
            'models': {
                m: build_forecast(evidence.get(m, {}), signals.get(m, {}), origin, now)
                for m in models
            },
        }
        state['forecasts'][key] = packet
        # Scope changes cannot reuse another account/device's packet.
        while len(state['forecasts']) > 2:
            del state['forecasts'][next(iter(state['forecasts']))]
        return packet, False
    except (sqlite3.Error, OSError):
        if old and now >= old['at']:
            return old, True
        raise


def report(store, account, device, start, end, now, signals=None):
    if (
        not all(finite(v) for v in (start, end, now))
        or start < 0
        or end <= start
        or min(end, now) <= start
    ):
        raise ValueError('Choose a valid observed earnings range.')
    signals = signals or {}
    state = _state(store)
    with state['lock']:
        key = _history_key(account, device, start, end, now)
        old = state['history'].get(key)
        held = False
        if old and 0 <= now - old['at'] < HISTORY_CACHE_SECONDS:
            history = copy.deepcopy(old)
            cached = True
        else:
            try:
                history = _historical_report(store, account, device, start, end, now, signals)
                state['history'][key] = copy.deepcopy(history)
                while len(state['history']) > 4:
                    del state['history'][next(iter(state['history']))]
                cached = False
            except (sqlite3.Error, OSError):
                if not old or now < old['at']:
                    raise
                history = copy.deepcopy(old)
                cached = held = True
        packet, forecast_held = _forecasts(state, store, account, device, now, signals)
        rows = {row['model']: row for row in history['models']}
        for model, forecast in packet['models'].items():
            if model not in rows:
                signal = signals.get(model, {})
                row = {
                    'model': model,
                    'serving': bool(signal.get('current')),
                    'eligible': model in signals,
                    'signalAt': signal.get('observedAt'),
                    'totalWarmHours': 0,
                    'pairedWarmHours': 0,
                    'current': conditional({}, {}, signal, now),
                    'bands': [band_evidence({}, [], a, b, now) for a, b in BANDS],
                }
                history['models'].append(row)
                rows[model] = row
            rows[model]['forecast'] = presentation(forecast, now, held or forecast_held)
        for row in history['models']:
            signal = signals.get(row['model'], {})
            row['serving'] = bool(signal.get('current'))
            row['eligible'] = row['model'] in signals
            if 'forecast' not in row:
                row['forecast'] = presentation(
                    build_forecast({}, signal, packet['at']), now, held or forecast_held
                )
        history['forecastMethodVersion'] = METHOD_VERSION
        history['forecastLookbackSeconds'] = LOOKBACK_SECONDS
        history['forecastAt'] = packet['at']
        journal = getattr(store, 'earnings_forecast_journal', None)
        history['forecastPersistence'] = (
            'versioned_local_journal'
            if journal is not None and journal.enabled and 'checkpoint' in packet
            else 'awaiting_collector'
            if journal is not None and journal.enabled
            else 'memory_only'
        )
        if journal is not None and journal.enabled:
            try:
                history['forecastEvaluation'] = journal.evaluation(account, device, now)
            except (sqlite3.Error, OSError):
                history['forecastEvaluation'] = {
                    'status': 'unavailable',
                    'methodVersion': METHOD_VERSION,
                    'models': [],
                }
        else:
            history['forecastEvaluation'] = {
                'status': 'disabled',
                'methodVersion': METHOD_VERSION,
                'models': [],
            }
        history['reportStatus'] = (
            'held' if held or forecast_held else 'cached' if cached else 'fresh'
        )
        history['historyCacheSeconds'] = HISTORY_CACHE_SECONDS
        return history


def _report_calculations():
    """Run unchanged baseline arithmetic with a private per-report clock cache.

    Python's local-time conversion is expensive on this Mac. These four pure
    functions repeatedly convert the same observed minute. Give their exact
    existing code/defaults an isolated globals mapping, changing only that
    deterministic conversion to a bounded memoized call. Never patch the shared
    optimizer module or its functions; timezone/DST semantics stay identical.
    """
    clock = lru_cache(maxsize=65536)(baseline_calculations.datetime.fromtimestamp)
    namespace = dict(vars(baseline_calculations))
    namespace['datetime'] = SimpleNamespace(fromtimestamp=clock)
    for name in ('summary', 'periods', 'repeatable', 'conditional'):
        original = getattr(baseline_calculations, name)
        namespace[name] = FunctionType(
            original.__code__,
            namespace,
            original.__name__,
            original.__defaults__,
            original.__closure__,
        )
    return SimpleNamespace(
        **{name: namespace[name] for name in ('summary', 'periods', 'repeatable', 'conditional')},
        clear=clock.cache_clear,
    )
