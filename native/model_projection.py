"""Cumulative projections from settled, device-attributed model runtime.

Single-model history reuses optimizer evidence. Concurrent models require
observations of that exact combination; solo earning rates are never added.
"""

import math
from datetime import datetime
from optimizer_store import context, device_id
from model_combinations import selection_key
from demand_baselines import read_view


def model_set(raw):
    values = raw.get('advertised_models', [])
    if (
        not isinstance(values, list)
        or not values
        or not all(isinstance(v, str) and v for v in values)
    ):
        return []
    return sorted(set(values))


def projection(rows, models, now, as_of):
    seconds = sum(r['seconds'] for r in rows)
    result = {
        'status': 'learning',
        'models': models,
        'asOf': as_of,
        'hours': seconds / 3600,
        'days': len({datetime.fromtimestamp(r['at']).date() for r in rows}),
        'ratePerHour': None,
        'points': [],
        'detail': 'Learning this model combination. At least 30 minutes of settled, verified warm runtime is needed.',
    }
    if seconds < 1800:
        return result

    def weighted_rate(items):
        weighted = [(r, 0.5 ** (max(0, now - r['at']) / (3 * 86400))) for r in items]
        return (
            sum(r['usd'] * w for r, w in weighted)
            * 3600
            / sum(r['seconds'] * w for r, w in weighted)
        )

    baseline = weighted_rate(rows)
    recent = [r for r in rows if r['at'] >= now - 3600]
    recent_seconds = sum(r['seconds'] for r in recent)
    recent_weight = 0.35 * min(1, recent_seconds / 3600) if recent_seconds >= 900 else 0
    recent_rate = weighted_rate(recent) if recent else baseline
    groups = {}
    for r in rows:
        groups.setdefault(context(r['at']), []).append(r)
    slots = {
        key: (sum(r['seconds'] for r in items), weighted_rate(items))
        for key, items in groups.items()
    }

    def rate(at):
        comparable_seconds, contextual = slots.get(context(at), (0, baseline))
        weight = min(0.5, comparable_seconds / 14400) if comparable_seconds >= 3600 else 0
        historical = (1 - weight) * baseline + weight * contextual
        # Recent pace matters most near now; do not carry one busy spell all day.
        momentum = recent_weight * math.exp(-max(0, at - now) / 7200)
        return max(0, (1 - momentum) * historical + momentum * recent_rate)

    points = [{'at': as_of, 'additional': 0}]
    at = as_of
    total = 0
    end = now + 86400
    while at < end:
        stop = min(end, (int(at // 900) + 1) * 900)
        total += rate((at + stop) / 2) * (stop - at) / 3600
        points.append({'at': stop, 'additional': total})
        at = stop
    result.update(
        status='ready',
        ratePerHour=rate(now),
        points=points,
        detail='Up to 30 days of this Mac’s settled inference earnings and verified warm runtime, including ready idle minutes. Recent days, recent pace, and matching weekday/weekend time slots receive more weight. Future base rewards are excluded.',
    )
    return result


class ModelProjection:
    def __init__(self, store):
        self.store = store
        self.h = store.h
        self.cached = None
        self.cache_key = None
        self.cache_at = 0
        self.evidence_key = None
        self.evidence_data = {}

    def evidence(self, account, device, models, now, shared=False):
        # Solo and pair forecasts share the same verified-ready provenance.
        # Legacy mix/optimizer minutes remain saved but cannot prove pre-warm.
        key = selection_key(models)
        return (
            self.recent_evidence(account, device, now, shared).get(key, {}).get('minutes', [])
            if key
            else []
        )

    def recent_evidence(self, account, device, now, shared=False):
        """30-day evidence from a read-only snapshot so collect() never waits on it.
        shared: the projection and the Pulse baseline both refresh once a minute and
        reuse one read within the same minute."""
        key = (account, device, int(now // 60))
        if not shared or key != self.evidence_key:
            with read_view(self.store) as view:
                self.evidence_data = view.evidence(account, device, now - 30 * 86400, now, now)
            self.evidence_key = key
        return self.evidence_data

    def estimate(self, account, raw, provider, monitor, earnings, now):
        models = model_set(raw)
        as_of = min(now, monitor.get('observedAt') or monitor.get('updatedAt') or 0)
        empty = {
            'status': 'unavailable',
            'models': models,
            'asOf': as_of,
            'hours': 0,
            'days': 0,
            'ratePerHour': None,
            'points': [],
        }
        if not provider.get('online') or not models:
            return {
                **empty,
                'detail': 'Projection paused while the provider is offline or its running models are unknown.',
            }
        if not provider.get('tracking', {}).get('counting'):
            return {
                **empty,
                'detail': 'Projection paused until every selected model is loaded and pre-warmed.',
            }
        if (
            monitor.get('status') != 'ok'
            or now - as_of > 180
            or earnings.get('status') != 'ok'
            or now - (earnings.get('updatedAt') or 0) > 180
        ):
            return {**empty, 'detail': 'Projection paused until fresh earnings readings return.'}
        key = (account, device_id(raw), tuple(models))
        if key != self.cache_key or now - self.cache_at >= 60:
            self.cached = (
                self.evidence(account, key[1], models, now, shared=True)
                if account and key[1]
                else []
            )
            self.cache_key, self.cache_at = key, now
        return projection(self.cached or [], models, now, as_of)
