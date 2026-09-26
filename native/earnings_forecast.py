"""Report-only one-hour paid baselines. No commands or optimizer policy callers.

Each forecast assumes this exact solo model is already warm and stays selected
and ready for the next hour. Missing exposure is never a zero-valued hour.
"""

import copy
import math
from datetime import datetime

METHOD_VERSION = 'paid-hour-baselines-v1'
LOOKBACK_SECONDS = 30 * 86400
HORIZON_SECONDS = 3600
REFRESH_SECONDS = 300
SETTLEMENT_SECONDS = 120


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def complete_hours(evidence, cutoff):
    groups = {}
    for minute in evidence.get('minutes', []):
        at = minute.get('at')
        if not finite(at) or at % 60 or at + 60 > cutoff:
            continue
        if (
            not finite(minute.get('seconds'))
            or abs(minute['seconds'] - 60) > 1e-6
            or not finite(minute.get('usd'))
        ):
            continue
        groups.setdefault(int(at // 3600) * 3600, {}).setdefault(at, []).append(minute)
    result = []
    for origin, minutes in sorted(groups.items()):
        expected = range(origin, origin + 3600, 60)
        if origin + 3600 <= cutoff and all(len(minutes.get(at, [])) == 1 for at in expected):
            result.append(
                {
                    'origin': origin,
                    'end': origin + 3600,
                    'usd': sum(minutes[at][0]['usd'] for at in expected),
                }
            )
    return result


def quantile(values, fraction):
    if not values:
        return None
    values = sorted(values)
    i = (len(values) - 1) * fraction
    low = int(i)
    return values[low] + (values[min(low + 1, len(values) - 1)] - values[low]) * (i - low)


def build(evidence, signal, origin, issued_at=None):
    issued_at = origin if issued_at is None else issued_at
    cutoff = int((min(origin, issued_at) - SETTLEMENT_SECONDS) // 60) * 60
    # Defensive cutoff even if the caller passes a wider history object.
    past = [
        m
        for m in evidence.get('minutes', [])
        if finite(m.get('at')) and origin - LOOKBACK_SECONDS <= m['at'] and m['at'] + 60 <= cutoff
    ]
    hours = complete_hours({'minutes': past}, cutoff)
    dates = len({datetime.fromtimestamp(h['origin']).date() for h in hours})
    latest = max((h['end'] for h in hours), default=None)
    values = [h['usd'] for h in hours]
    historical_rate = sum(values) / len(values) if values else None
    recent = {}
    for m in past:
        if cutoff - 900 <= m['at'] < cutoff:
            recent.setdefault(m['at'], []).append(m)
    expected = list(range(cutoff - 900, cutoff, 60))
    recent_complete = all(
        len(recent.get(at, [])) == 1
        and finite(recent[at][0].get('seconds'))
        and abs(recent[at][0]['seconds'] - 60) <= 1e-6
        and finite(recent[at][0].get('usd'))
        for at in expected
    )
    recent_minutes = sum(
        len(recent.get(at, [])) == 1
        and finite(recent[at][0].get('seconds'))
        and abs(recent[at][0]['seconds'] - 60) <= 1e-6
        for at in expected
    )
    signal_fresh = (
        finite(signal.get('observedAt'))
        and 0 <= issued_at - signal['observedAt'] < 90
        and signal.get('status') in ('normal', 'spike', 'learning', 'watching')
        and finite(signal.get('coverage'))
        and signal['coverage'] >= 0.8
    )
    rate = None
    basis = 'unavailable'
    reasons = []
    if signal.get('current') and signal_fresh and recent_complete:
        rate = sum(recent[at][0]['usd'] for at in expected) * 4
        basis = 'recent_paid_persistence'
        reason = 'The latest 15 complete settled warm minutes, extended over one hour. Demand may change; this baseline does not predict those changes.'
    elif len(hours) >= 4 and dates >= 3 and latest is not None and origin - latest <= 7 * 86400:
        rate = historical_rate
        basis = 'completed_hour_history'
        reasons.append('no_fresh_complete_paid_window')
        reason = 'Baseline from complete same-model warm hours. No fresh complete paid window is available for this model; this estimate is not adjusted to current demand.'
    else:
        if not hours:
            reasons.append('no_complete_warm_hours')
        elif len(hours) < 4:
            reasons.append('insufficient_complete_hours')
        if dates < 3:
            reasons.append('insufficient_dates')
        if latest is not None and origin - latest > 7 * 86400:
            reasons.append('old_paid_history')
        if signal.get('current') and not recent_complete:
            reasons.append('incomplete_recent_paid_window')
        if not signal_fresh:
            reasons.append('unavailable_or_stale_demand')
        reason = 'Not enough recent complete same-model paid history for a next-hour baseline. Historical observations remain available below.'
    return {
        'methodVersion': METHOD_VERSION,
        'state': 'estimate' if rate is not None else 'unavailable',
        'issuedAt': issued_at,
        'origin': origin,
        'targetEnd': origin + HORIZON_SECONDS,
        'validUntil': origin + HORIZON_SECONDS,
        'refreshAfter': issued_at + REFRESH_SECONDS,
        'horizonSeconds': HORIZON_SECONDS,
        'target': 'inference_usd_next_hour_if_already_warm_and_stays_selected_ready',
        'condition': 'already_warm_and_same_model_selected_ready_for_entire_hour',
        'assumption': 'Assumes this model is already warm and stays selected and ready for the full hour. Loading, switching, base rewards, and unknown exposure are excluded.',
        'usd': rate,
        'usdPerHour': rate,
        'basis': basis,
        'reason': reason,
        'reasonCodes': reasons,
        'demandAsOf': signal.get('observedAt'),
        'paidEvidenceThrough': cutoff,
        'inputStatus': 'fresh' if signal_fresh else 'demand_unavailable',
        'lookbackSeconds': LOOKBACK_SECONDS,
        'support': {
            'completedHours': len(hours),
            'distinctDates': dates,
            'recentCompleteMinutes': recent_minutes,
            'recentRequiredMinutes': 15,
            'lastOutcomeAt': latest,
            'lastPaidMinuteEnd': max((m['at'] + 60 for m in past), default=None),
            'demandAdjusted': False,
        },
        'uncertainty': {
            'kind': 'descriptive_historical_spread' if len(values) >= 4 else 'unavailable',
            'lower': quantile(values, 0.25) if len(values) >= 4 else None,
            'upper': quantile(values, 0.75) if len(values) >= 4 else None,
            'nominalCoverage': None,
            'validationWindows': 0,
            'empiricalCoverage': None,
            'label': 'Middle half of completed historical warm-hour outcomes; not a prediction interval.',
        },
        'historicalFallback': {
            'basis': 'completed_warm_hours',
            'usdPerWarmHour': historical_rate,
            'observedHours': len(hours),
            'asOf': latest,
            'isForecast': False,
        },
        'validation': {
            'status': 'experimental_baseline',
            'prospectiveValidated': False,
            'retrospectiveEvidence': 'Chronological diagnostic comparisons were too sparse to establish general forecasting skill.',
            'creditAvailability': 'A settlement lag is applied; historical first-seen and correction times are unavailable.',
        },
    }


def presentation(packet, now, held=False):
    """A retained packet keeps its original target; it never becomes a new hour."""
    result = copy.deepcopy(packet)
    if now >= result['targetEnd']:
        result['state'] = 'expired'
        result['usd'] = result['usdPerHour'] = None
        result['reasonCodes'] = [*result['reasonCodes'], 'forecast_window_expired']
        result['reason'] = 'This forecast window has ended. Waiting for a new complete report.'
    elif held or now >= result['refreshAfter']:
        if result['usd'] is not None:
            result['state'] = 'held'
        result['inputStatus'] = 'held'
        result['reasonCodes'] = [*result['reasonCodes'], 'last_complete_report']
        result['reason'] = (
            'Showing the last complete forecast for its original time window. ' + result['reason']
        )
    return result


def read_paid_evidence(view, account, device, start, end, now):
    """Report-only fast path with the existing exact paid/coverage predicates.

    Reuse the independently tested manual-statistics reader's EXISTS identity
    query. The optimizer store and its decision callers remain unchanged.
    """
    from model_insights import warm_evidence

    models = [
        r[0]
        for r in view.h.db.execute(
            'SELECT DISTINCT model FROM opt_ready_minutes WHERE account=? AND device=? AND at>=? AND at+60<=?',
            (account, device, start, min(end, now - SETTLEMENT_SECONDS)),
        )
    ]
    return warm_evidence(view, account, device, models, start, end, now, None)
