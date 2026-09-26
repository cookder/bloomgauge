"""Shadow per-model pay curves: this Mac's paid $/warm-hour against the model's
network pressure (active+queued requests per warm provider).

Each model's curve is level * (pressure + 0.01) ** exponent, fitted to half-hour
steady-state pay and pulled toward a pooled curve when its own history is thin.
A model never run here gets the pooled curve with a wide range.

Passive only. Predictions are logged and scored against later warm runs; they
never rank, trigger or block a switch until they beat the matched estimator.
Published prices and this Mac's throughput are deliberately unused: on this
Mac's own history (Sep 25, 2026) adding either made cross-model predictions
worse than pressure alone.

Shared starting curves (shared_priors.py) replace the pooled curve as the
pull target for a model that has one, so a model new to this Mac starts from
what it paid elsewhere instead of from the typical model here.
"""

import math
from model_combinations import members

METHOD_VERSION = 'pressure-curve-v1'
LOOKBACK_SECONDS = 30 * 86400
PERIOD_SECONDS = 1800
MIN_PERIOD_MINUTES = 10
# Routing ramp: after a model becomes warm the network takes a while to send
# this Mac work. Minutes before the first paid minute (at most 15) are not
# steady-state pay. A gap over 5 minutes starts a new warm stretch.
RAMP_LIMIT_SECONDS = 900
STRETCH_GAP_SECONDS = 300
HALF_LIFE_SECONDS = 7 * 86400
PRESSURE_FLOOR = 0.01
EXPONENTS = [i / 20 for i in range(0, 41)]  # 0 .. 2
PRIOR_HOURS = 0.5  # pull of a model's level toward the pooled curve, in warm hours
PRIOR_SLOPE = 3.0  # pull of its exponent, in warm hours x (log pressure)^2
SHARED_HOURS = 2.0  # pull of a shared starting curve's level, in warm hours
OWN_RANGE_PERIODS = 8  # periods before a model's own residuals set its range
EXTRAPOLATION_LOG = 0.35  # about 1.4x beyond the pressures a model was measured at
RECENT_SECONDS = 1800
RECENT_WEIGHT = 0.7
# Beyond two requests per warm provider, more network demand stopped adding
# pay in proportion; without a cap, thin histories extrapolated spikes 3x high.
SATURATION = 2.0


def finite(v):
    return type(v) in (int, float) and math.isfinite(v)


def steady(minutes):
    output, start, paid, previous = [], None, False, None
    for m in sorted(minutes, key=lambda m: m['at']):
        if previous is None or m['at'] - previous > STRETCH_GAP_SECONDS:
            start, paid = m['at'], False
        previous = m['at']
        paid = paid or m['usd'] > 0
        if paid or m['at'] - start >= RAMP_LIMIT_SECONDS:
            output.append(m)
    return output


def summarize(rows):
    seconds = sum(m['seconds'] for m, _ in rows)
    return {
        'hours': seconds / 3600,
        'usdPerHour': sum(m['usd'] for m, _ in rows) * 3600 / seconds,
        'pressure': sum(p * m['seconds'] for m, p in rows) / seconds,
    }


def periods(minutes, network, now):
    """Half-hour steady-state pay joined to that model's per-minute pressure."""
    groups = {}
    for m in steady(minutes):
        n = network.get(m['at'])
        if n is None or m['at'] + 60 > now - 120 or not finite(n.get('pressure')):
            continue
        groups.setdefault(int(m['at'] // PERIOD_SECONDS) * PERIOD_SECONDS, []).append(
            (m, n['pressure'])
        )
    return [
        {'at': at, **summarize(rows)}
        for at, rows in sorted(groups.items())
        if len(rows) >= MIN_PERIOD_MINUTES
    ]


def recent(minutes, network, now):
    """The last half hour of steady pay, when at least 10 minutes joined to pressure."""
    rows = [
        (m, network[m['at']]['pressure'])
        for m in steady(minutes)
        if now - 120 - RECENT_SECONDS <= m['at']
        and m['at'] + 60 <= now - 120
        and finite((network.get(m['at']) or {}).get('pressure'))
    ]
    return {'minutes': len(rows), **summarize(rows)} if len(rows) >= MIN_PERIOD_MINUTES else None


def shape(pressure, exponent):
    return (min(max(0, pressure), SATURATION) + PRESSURE_FLOOR) ** exponent


def weighted(points, now):
    return [
        (
            p['pressure'],
            p['usdPerHour'],
            p['hours'] * 0.5 ** (max(0, now - p['at']) / HALF_LIFE_SECONDS),
        )
        for p in points
    ]


def level(rows, exponent, prior=None, hours=PRIOR_HOURS):
    """Least-squares level for a fixed exponent, pulled toward the prior's level."""
    sgy = sum(w * shape(p, exponent) * y for p, y, w in rows)
    sgg = sum(w * shape(p, exponent) ** 2 for p, y, w in rows)
    if prior is None:
        return sgy / sgg if sgg > 0 else None
    typical = sgg / sum(w for _, _, w in rows) if rows else 1
    return (sgy + hours * typical * prior) / (sgg + hours * typical)


def error(rows, exponent, k):
    return sum(w * (y - k * shape(p, exponent)) ** 2 for p, y, w in rows)


def quartiles(ratios):
    """Weighted 25th and 75th percentiles of (ratio, weight) pairs."""
    ratios = sorted(ratios)
    total = sum(w for _, w in ratios)
    if not total:
        return None

    def at(f):
        seen = 0.0
        for r, w in ratios:
            seen += w
            if seen >= f * total:
                return r
        return ratios[-1][0]

    return at(0.25), at(0.75)


def pooled_curve(points, now):
    """One exponent shared by every model (each keeps its own level); level of a typical model."""
    groups = {m: weighted(rows, now) for m, rows in points.items() if rows}
    if not groups:
        return None
    best = min(
        EXPONENTS,
        key=lambda e: sum(error(rows, e, level(rows, e) or 0) for rows in groups.values()),
    )
    everything = [r for rows in groups.values() for r in rows]
    k = level(everything, best)
    if not k:
        return None
    ratios = [(y / (k * shape(p, best)), w) for p, y, w in everything]
    hours = {m: sum(p['hours'] for p in points[m]) for m in groups}
    levels = [
        ((level(groups[m], best) or 0) / k, min(hours[m], 10)) for m in groups if hours[m] >= 2
    ]
    return {
        'exponent': best,
        'level': k,
        'range': quartiles(ratios),
        'levels': quartiles(levels) if levels else None,
    }


def model_curve(points, now, pooled, shared=None):
    rows = weighted(points, now)
    target = shared or pooled
    if not rows:
        return {
            'exponent': target['exponent'],
            'level': target['level'],
            'range': None,
            'hours': 0,
            'periods': 0,
            'xmin': None,
            'xmax': None,
            'shared': bool(shared),
        }
    xs = [math.log(p + PRESSURE_FLOOR) for p, _, _ in rows]
    total = sum(w for _, _, w in rows)
    mean = sum(w * x for (_, _, w), x in zip(rows, xs)) / total
    spread = sum(w * (x - mean) ** 2 for (_, _, w), x in zip(rows, xs))
    own = min(EXPONENTS, key=lambda e: error(rows, e, level(rows, e) or 0))
    # Pressure spread decides how much a model's own data can move its exponent.
    exponent = (spread * own + PRIOR_SLOPE * target['exponent']) / (spread + PRIOR_SLOPE)
    k = level(rows, exponent, target['level'], SHARED_HOURS if shared else PRIOR_HOURS)
    ratios = [(y / (k * shape(p, exponent)), w) for p, y, w in rows] if k > 0 else []
    return {
        'exponent': exponent,
        'level': k,
        'hours': sum(p['hours'] for p in points),
        'periods': len(points),
        'range': quartiles(ratios) if len(points) >= OWN_RANGE_PERIODS else None,
        'xmin': min(xs),
        'xmax': max(xs),
        'shared': bool(shared),
    }


def predict(curve, pooled, pressure, latest=None):
    value = curve['level'] * shape(pressure, curve['exponent'])
    # Pay that ran above or below the curve in the last half hour tends to
    # persist: carry part of that gap forward, scaled to today's pressure.
    adjustment = None
    if latest:
        expected = curve['level'] * shape(latest['pressure'], curve['exponent'])
        if expected > 0:
            weight = RECENT_WEIGHT * min(1, latest['minutes'] * 60 / RECENT_SECONDS)
            adjustment = weight * (latest['usdPerHour'] / expected - 1)
            value *= 1 + adjustment
    low, high = curve['range'] or curve.get('sharedRange') or pooled['range'] or (1, 1)
    # Same convention as the matched estimator: the range always spans the estimate.
    low, high = min(low, 0.85), max(high, 1.15)
    if not curve['periods'] and curve.get('shared'):
        # Measured on other Macs or earlier here: routing differs, and more so on other hardware.
        low, high = (
            (low * 0.7, high * 1.3) if curve.get('sameHardware', True) else (low * 0.55, high * 1.6)
        )
    elif not curve['periods'] and pooled['levels']:
        # Never run here: the level itself is uncertain across models.
        low, high = low * min(1, pooled['levels'][0]), high * max(1, pooled['levels'][1])
    x = math.log(max(0, pressure) + PRESSURE_FLOOR)
    outside = max(0, curve['xmin'] - x, x - curve['xmax']) if curve['periods'] else 0
    widen = math.exp(0.5 * outside)
    return {
        'usdPerHour': value,
        'lower': value * low / widen,
        'upper': value * high * widen,
        'extrapolated': outside > EXTRAPOLATION_LOG,
        'recentAdjustment': adjustment,
    }


def current_pressure(signal, serving, now):
    """Pressure this Mac would see now. A model it isn't serving gains one warm provider: this Mac."""
    fresh = (
        finite(signal.get('observedAt'))
        and 0 <= now - signal['observedAt'] < 90
        and signal.get('status') != 'stale'
        and signal.get('coverage', 0) >= 0.8
    )
    load, warm = signal.get('load'), signal.get('warmProviders')
    if not fresh or not finite(load) or load < 0 or not finite(warm) or warm < 0:
        return None
    if serving:
        pressure = signal.get('pressure')
        return pressure if finite(pressure) and pressure >= 0 and warm >= 1 else None
    return load / (warm + 1)


def build(evidence, network, signals, current, now, shared=None):
    """Predicted steady $/warm-hour right now for every solo model with evidence or a signal.

    shared: optional {model: {'level', 'exponent', 'range'}} starting curves."""
    shared = shared or {}
    serving = set(members(current)) if current else set()
    models = sorted(m for m in set(evidence) | set(signals) if len(members(m)) == 1)
    points = {
        m: periods(evidence.get(m, {}).get('minutes', []), network.get(m, {}), now) for m in models
    }
    pooled = pooled_curve(points, now)
    if pooled is None and shared:
        # A Mac with no paid history yet: shared curves alone. Models without
        # one get the middle shared curve with the widest range.
        middle = sorted(shared.values(), key=lambda c: c['level'])[len(shared) // 2]
        pooled = {
            'exponent': middle['exponent'],
            'level': middle['level'],
            'range': (0.5, 1.5),
            'levels': None,
        }
    if pooled is None:
        return {}
    output = {}
    for m in models:
        prior = shared.get(m)
        curve = model_curve(points[m], now, pooled, prior)
        if prior and not curve['periods']:
            curve['sharedRange'] = prior.get('range')
            curve['sameHardware'] = prior.get('sameHardware', True)
        row = {
            'basis': 'measured'
            if curve['hours'] >= 2 and curve['periods'] >= 4
            else 'limited'
            if curve['periods']
            else 'shared'
            if prior
            else 'prior',
            'hours': curve['hours'],
            'periods': curve['periods'],
            'exponent': curve['exponent'],
            'pressure': current_pressure(signals.get(m) or {}, m in serving, now),
            'serving': m in serving,
            'usdPerHour': None,
            'lower': None,
            'upper': None,
            'extrapolated': False,
            'recentAdjustment': None,
        }
        if row['pressure'] is not None:
            latest = recent(evidence.get(m, {}).get('minutes', []), network.get(m, {}), now)
            row.update(predict(curve, pooled, row['pressure'], latest))
        output[m] = row
    return output


def steady_rate(minutes, start, end):
    """Realized steady $/warm-hour of whole warm minutes in [start, end)."""
    rows = [m for m in steady(minutes) if start <= m['at'] and m['at'] + 60 <= end]
    seconds = sum(m['seconds'] for m in rows)
    return (sum(m['usd'] for m in rows) * 3600 / seconds if seconds else None), len(rows)


def summary(evidence, network, now, min_hours=2, min_periods=4):
    """This Mac's own fitted curve per model with enough steady pay: the shape
    shared-priors.json uses. No times, amounts or identities, only the curve."""
    models = sorted(m for m in evidence if len(members(m)) == 1)
    points = {m: periods(evidence[m].get('minutes', []), network.get(m, {}), now) for m in models}
    pooled = pooled_curve(points, now)
    if pooled is None:
        return {}
    out = {}
    for m in models:
        c = model_curve(points[m], now, pooled)
        if c['hours'] >= min_hours and c['periods'] >= min_periods and c['level'] > 0:
            rng = c['range'] or pooled['range']
            out[m] = {
                'level': round(c['level'], 5),
                'exponent': round(c['exponent'], 4),
                'range': [round(rng[0], 3), round(rng[1], 3)] if rng else None,
                'hours': round(c['hours'], 1),
                'periods': c['periods'],
            }
    return out
