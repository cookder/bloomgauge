"""Observed demand -> local outcomes. No equal-share or price extrapolation.

Only settled, covered solo warm minutes supplied by OptimizerStore can join.
Network concurrency is sampled, not an arrival rate. Missing overlap is unknown.
"""

import math
import copy
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from model_combinations import members


def finite(v):
    return type(v) in (int, float) and math.isfinite(v)


@contextmanager
def read_view(store):
    """One coherent WAL read snapshot without holding the collector's write lock.

    Large date ranges can take seconds. The live sampler must still record its
    one-second observations while those historical queries run.
    """
    with store.h.lock:
        filename = store.h.db.execute('PRAGMA database_list').fetchone()[2]
    if not filename:  # In-memory development/test history has no independent file.
        with store.h.lock:
            yield store
        return
    db = sqlite3.connect(Path(filename).as_uri() + '?mode=ro', uri=True, timeout=5)
    try:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        view = copy.copy(store)
        view.h = SimpleNamespace(db=db, lock=threading.RLock())
        yield view
    finally:
        db.rollback()
        db.close()


def network_minutes(store, models, start, end):
    if not models:
        return {}
    with store.h.lock:
        # Distinct source timestamps, last observation per 30s slot. A fast poll
        # or duplicate source must not turn one spike into independent evidence.
        rows = store.h.db.execute(
            """WITH latest AS (
            SELECT model,CAST(at/30 AS INTEGER) AS slot,MAX(at) AS at FROM opt_network
            WHERE model IN ("""
            + ','.join('?' for _ in models)
            + """) AND at>=? AND at<? GROUP BY model,slot)
            SELECT n.model,n.at,n.active,n.queued,n.warm FROM latest l
            JOIN opt_network n ON n.model=l.model AND n.at=l.at ORDER BY n.at""",
            (*models, start, end),
        ).fetchall()
    groups = {}
    for row in rows:
        groups.setdefault((row['model'], int(row['at'] // 60) * 60), []).append(row)
    result = {m: {} for m in models}
    for (model, at), rows in groups.items():
        # Both halves of the same warm minute, with meaningful time separation.
        # Do not carry the last known capacity forward over missing polls.
        if len(rows) != 2 or rows[-1]['at'] - rows[0]['at'] < 15:
            continue
        if any(
            not all(finite(r[k]) and r[k] >= 0 for k in ('active', 'queued', 'warm'))
            or r['warm'] < 1
            for r in rows
        ):
            continue
        mean = lambda key: sum(r[key] for r in rows) / len(rows)
        result[model][at] = {
            'active': mean('active'),
            'queued': mean('queued'),
            'warm': mean('warm'),
            'load': mean('active') + mean('queued'),
            'pressure': sum((r['active'] + r['queued']) / r['warm'] for r in rows) / len(rows),
        }
    return result


def joined_minutes(evidence, network, now):
    return [
        {**m, **network[m['at']]}
        for m in evidence.get('minutes', [])
        if m['at'] in network and m['at'] + 60 <= now - 120
    ]


def periods(minutes, step=1800):
    groups = {}
    for m in minutes:
        groups.setdefault(int(m['at'] // step) * step, []).append(m)
    return [
        {'at': at, 'end': max(m['at'] + 60 for m in rows), **summary(rows)}
        for at, rows in sorted(groups.items())
    ]


def summary(minutes):
    seconds = sum(m['seconds'] for m in minutes)
    dates = {datetime.fromtimestamp(m['at']).date() for m in minutes}
    output = {
        'hours': seconds / 3600,
        'days': len(dates),
        'minutes': len(minutes),
        'asOf': max((m['at'] + 60 for m in minutes), default=None),
    }
    for key in ('active', 'queued', 'warm', 'load', 'pressure'):
        output[key] = sum(m[key] * m['seconds'] for m in minutes) / seconds if seconds else None
    for key, source, multiplier in [
        ('usdPerHour', 'usd', 3600),
        ('requestsPerMinute', 'requests', 60),
        ('tokensPerSecond', 'tokens', 1),
        ('busyPercent', 'busy', 100),
    ]:
        output[key] = sum(m[source] for m in minutes) * multiplier / seconds if seconds else None
    output['usd'] = sum(m['usd'] for m in minutes)
    output['paidJobs'] = sum(m['paidJobs'] for m in minutes)
    return output


def near(observed, current, counts=False):
    # Near zero, a factor comparison is undefined; allow at most one request.
    return (
        finite(current)
        and current >= 0
        and (
            abs(observed - current) <= 1
            if counts and current < 1
            else 0.5 * current <= observed <= 2 * current
        )
    )


def repeatable(rows, hours=2, days=2, blocks=4):
    substantial = [p for p in periods(rows) if p['hours'] >= 0.25]
    return (
        sum(p['hours'] for p in substantial) >= hours
        and len(substantial) >= blocks
        and len({datetime.fromtimestamp(p['at']).date() for p in substantial}) >= days
    )


def conditional(evidence, network, signal, now, include_minutes=False):
    joined = joined_minutes(evidence, network, now)
    broad = summary(joined)
    fresh = (
        finite(signal.get('observedAt'))
        and 0 <= now - signal['observedAt'] < 90
        and signal.get('status') in ('normal', 'spike', 'learning', 'watching')
        and signal.get('coverage', 0) >= 0.8
    )
    # Require concurrent active work AND warm capacity, as well as total load
    # per provider. Identical pressure at very different scale isn't equivalent.
    target = {
        'pressure': signal.get('pressure'),
        'active': signal.get('active'),
        'warm': signal.get('warmProviders'),
        'load': signal.get('load'),
    }
    valid = fresh and all(finite(v) and v >= 0 for v in target.values()) and target['warm'] >= 1
    matching = (
        [
            m
            for m in joined
            if all(near(m[k], v, k in ('active', 'load')) for k, v in target.items())
        ]
        if valid
        else []
    )
    demand_hours = sum(m['seconds'] for m in matching) / 3600
    scope = 'similar_demand'
    clock = datetime.fromtimestamp(now)
    for exact in (True, False):
        local = []
        for m in matching:
            d = datetime.fromtimestamp(m['at'])
            day = (
                d.weekday() == clock.weekday()
                if exact
                else (d.weekday() >= 5) == (clock.weekday() >= 5)
            )
            if day and min(abs(d.hour - clock.hour), 24 - abs(d.hour - clock.hour)) <= 2:
                local.append(m)
        # A narrow weekday slot must support the same repeated comparison as
        # the broader demand match; two thin dates must not discard rich data.
        if repeatable(local, 4, 3, 8):
            matching = local
            scope = 'weekday_time' if exact else 'daytype_time'
            break
    # Coverage belongs to the comparison periods, not all of a model's history.
    # Older quiet runs or pre-network tracking cannot dilute well-observed runs
    # at today's demand. Sparse observations within a matching period still fail.
    exposure = {}
    overlap = {}
    for m in evidence.get('minutes', []):
        key = int(m['at'] // 1800) * 1800
        exposure[key] = exposure.get(key, 0) + m['seconds']
    for m in joined:
        key = int(m['at'] // 1800) * 1800
        overlap[key] = overlap.get(key, 0) + m['seconds']
    covered = {
        key for key, seconds in exposure.items() if seconds and overlap.get(key, 0) / seconds >= 0.8
    }
    supported = [m for m in matching if int(m['at'] // 1800) * 1800 in covered]
    usable = bool(
        repeatable(supported, 4, 3, 8)
        and supported
        and now - max(m['at'] + 60 for m in supported) <= 7 * 86400
    )
    # A forecast cannot include a thin, extreme period merely because other
    # periods establish enough evidence. Observations remain available separately.
    if usable:
        substantial_keys = {p['at'] for p in periods(supported) if p['hours'] >= 0.25}
        matching = [m for m in supported if int(m['at'] // 1800) * 1800 in substantial_keys]
    observed = summary(matching)
    substantial = [p for p in periods(matching) if p['hours'] >= 0.25]
    coverage = broad['hours'] / evidence['hours'] if evidence.get('hours', 0) else 0
    compared_keys = {int(m['at'] // 1800) * 1800 for m in matching}
    compared_seconds = sum(exposure.get(key, 0) for key in compared_keys)
    matching_coverage = (
        sum(overlap.get(key, 0) for key in compared_keys) / compared_seconds
        if compared_seconds
        else 0
    )
    forecast_usable = usable and observed['paidJobs'] >= 200
    weight = 1.0
    if usable:
        ratios = [
            observed[k] / broad[k]
            for k in ('usdPerHour', 'requestsPerMinute')
            if broad[k] is not None and broad[k] > 0
        ]
        if ratios:
            weight = max(0.8, min(1.2, sum(ratios) / len(ratios)))
    if not valid:
        reason = 'Waiting for fresh active-request and warm-provider readings.'
    elif not matching:
        reason = (
            'No overlapping warm history at similar demand yet; demand trials remain available.'
        )
    elif forecast_usable:
        reason = 'Repeated paid work at similar demand supports a comparison; quiet periods outside this demand range do not set this rate.'
    elif usable:
        reason = 'Repeated comparable periods are available; at least 200 matched credited jobs are needed for an earnings forecast.'
    else:
        reason = 'Limited comparable history; shown as observations, with no ranking penalty or earnings forecast.'
    rates = sorted(p['usdPerHour'] for p in substantial)

    def quartile(f):
        if not rates:
            return None
        i = (len(rates) - 1) * f
        lo = int(i)
        return rates[lo] + (rates[min(lo + 1, len(rates) - 1)] - rates[lo]) * (i - lo)

    rate = observed['usdPerHour']
    result = {
        **observed,
        'scope': scope,
        'usable': usable,
        'forecastUsable': forecast_usable,
        'weight': weight,
        'reason': reason,
        'blocks': len(substantial),
        'coverage': min(1, coverage),
        'overlapHours': broad['hours'],
        'totalHours': evidence.get('hours', 0),
        'matchingCoverage': min(1, matching_coverage),
        'otherDemandHours': max(0, broad['hours'] - demand_hours) if valid else None,
        'otherContextHours': max(0, demand_hours - observed['hours']) if valid else None,
        'unknownDemandHours': max(0, evidence.get('hours', 0) - broad['hours']),
        'current': target if valid else None,
        'lower': min(rate * 0.85, quartile(0.25)) if rates else None,
        'upper': max(rate * 1.15, quartile(0.75)) if rates else None,
    }
    if include_minutes:
        result['matchedMinutes'] = [m['at'] for m in matching]
    return result


def report(store, account, device, start, end, now, signals=None, selected=None):
    if not all(finite(x) for x in (start, end, now)) or start < 0 or end <= start:
        raise ValueError('Choose a valid comparison range.')
    with read_view(store) as view:
        evidence = view.evidence(account, device, start, min(end, now), now)
        # Pair selections are separate experiments; never credit their total to a solo.
        evidence = {m: e for m, e in evidence.items() if len(members(m)) == 1}
        models = sorted(set(evidence) | set(signals or {}) | ({selected} if selected else set()))
        network = network_minutes(view, models, start, min(end, now - 120))
    choices = [
        {
            'id': m,
            'hours': evidence.get(m, {}).get('hours', 0),
            'overlapHours': len(
                network.get(m, {}).keys()
                & {r['at'] for r in evidence.get(m, {}).get('minutes', [])}
            )
            / 60,
        }
        for m in models
    ]
    model = selected or (
        max(choices, key=lambda c: (c['hours'], c['id']))['id'] if choices else None
    )
    earned = evidence.get(model, {})
    joined = joined_minutes(earned, network.get(model, {}), now)
    low = min((m['at'] for m in joined), default=0)
    high = max((m['at'] for m in joined), default=0)
    step = max(1800, math.ceil((high - low) / 600 / 1800) * 1800)
    bands = []
    for a, b in ((0, 0.25), (0.25, 0.5), (0.5, 1), (1, 2), (2, 4), (4, 8), (8, None)):
        rows = [m for m in joined if m['pressure'] >= a and (b is None or m['pressure'] < b)]
        bands.append({'low': a, 'high': b, **summary(rows)})
    return {
        'at': now,
        'from': start,
        'to': min(end, now),
        'model': model,
        'models': choices,
        'summary': summary(joined),
        'bands': bands,
        'periods': periods(joined, step),
        'periodSeconds': step,
        'baseline': conditional(
            earned, network.get(model, {}), (signals or {}).get(model, {}), now
        ),
        'scope': 'This Mac · settled inference credits · solo models · local time',
        'method': 'Load = active + queued concurrent requests. Load/warm is averaged per observation. Only complete, paid-covered warm minutes with two network observations in separate 30-second slots are paired. Gaps, cold time, base rewards and other devices are excluded.',
    }
