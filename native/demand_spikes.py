"""Bounded discovery during exceptional demand, separate from paid upgrades.

These are explicit experiment limits, not network cooldowns or dollar forecasts.
All decisions still pass the controller's normal identity/resource preflight.
"""

import math
from statistics import median

MAX_TRIALS_PER_DAY = 3
MAX_CLOCK_MINUTES = 40


def number(v):
    return type(v) in (int, float) and math.isfinite(v)


def demand_context(samples, now):
    """Equal-weight 30s observations, not peaks or repeated reads of one sample."""
    cutoff = int(now // 30) * 30
    buckets = {}
    for s in samples:
        if not all(
            number(s.get(k)) and 0 <= s[k] <= 1e12 for k in ('at', 'active', 'queued', 'warm')
        ):
            continue
        if not cutoff - 2400 <= s['at'] < cutoff:
            continue
        key = int(s['at'] // 30)
        if key not in buckets or s['at'] > buckets[key]['at']:
            buckets[key] = s

    def window(start, seconds, coverage=0.8):
        rows = [s for s in buckets.values() if start <= s['at'] < start + seconds]
        pressures = [(s['active'] + s['queued']) / s['warm'] for s in rows if s['warm'] >= 1]
        return {
            'start': start,
            'end': start + seconds,
            'coverage': len(rows) / (seconds / 30),
            'qualified': len(rows) >= math.ceil(seconds / 30 * coverage)
            and len(pressures) == len(rows),
            'load': median(s['active'] + s['queued'] for s in rows) if rows else None,
            'pressure': median(pressures) if pressures else None,
        }

    source = max((s['at'] for s in buckets.values()), default=None)
    return {
        'sourceAt': source,
        'fresh': source is not None and 0 <= now - source < 90,
        'recent': window(cutoff - 2400, 1800),
        'windows': [window(cutoff - 600, 300), window(cutoff - 300, 300)],
        'persistent': [window(start, 300) for start in range(cutoff - 1800, cutoff, 300)],
        'tails': [window(cutoff - 240, 120, 0.75), window(cutoff - 120, 120, 0.75)],
    }


def retention(decision, context, warm_start, now):
    """A lost spike requires two covered post-warm windows, never a stale zero."""
    original = (decision.get('candidate') or {}).get('signal') or {}
    tails = (context or {}).get('tails') or []
    result = {
        'status': 'unknown',
        'reason': 'Waiting for fresh, covered demand after warm-up.',
        'sourceAt': (context or {}).get('sourceAt'),
        'load': None,
        'pressure': None,
        'referenceLoad': original.get('load'),
        'referencePressure': original.get('pressure'),
    }
    if (
        not (context or {}).get('fresh')
        or not number(result['sourceAt'])
        or not 0 <= now - result['sourceAt'] < 90
        or len(tails) != 2
        or not number(warm_start)
        or not all(number(original.get(k)) and original[k] > 0 for k in ('load', 'pressure'))
        or not all(
            w.get('qualified')
            and w['start'] >= warm_start
            and all(number(w.get(k)) for k in ('load', 'pressure'))
            for w in tails
        )
    ):
        return result
    result.update(load=tails[-1]['load'], pressure=tails[-1]['pressure'])
    faded = all(any(w[k] < 0.75 * original[k] for k in ('load', 'pressure')) for w in tails)
    result.update(
        status='faded' if faded else 'retained',
        reason=(
            'Demand fell more than 25% below the trial entry level in both recent two-minute windows.'
            if faded
            else 'Recent demand is holding or recovering; continuing to measure local paid work.'
        ),
    )
    return result


def spike(signal, runs, model, now):
    sustained = signal.get('sustained') or {}
    windows = sustained.get('windows') or []
    result = {
        'qualified': False,
        'reason': 'A spike trial needs ten minutes of exceptional demand.',
        'loadRatio': None,
        'pressureRatio': None,
        'trialsUsed': 0,
        'trialLimit': MAX_TRIALS_PER_DAY,
        'maxClockMinutes': MAX_CLOCK_MINUTES,
    }
    recent = [
        r
        for r in runs
        if now - 86400 < r['at'] <= now
        and (r.get('decision') or {}).get('explorationTrigger') == 'demand_spike'
    ]
    result['trialsUsed'] = len(recent)
    if (
        signal.get('status') != 'spike'
        or not sustained.get('qualified')
        or len(windows) != 2
        or not number(signal.get('baselineHours'))
        or signal['baselineHours'] < 2
        or not number(signal.get('baselineAsOf'))
        or not 0 <= now - signal['baselineAsOf'] < 600
        or not all(
            number(signal.get(k)) and signal[k] > 0 for k in ('baselineLoad', 'baselinePressure')
        )
        or not number(sustained.get('sourceAt'))
        or not 0 <= now - sustained['sourceAt'] < 90
    ):
        return result
    for w in windows:
        if not (
            w.get('qualified')
            and number(w.get('coverage'))
            and w['coverage'] >= 0.8
            and number(w.get('load'))
            and w['load'] >= max(10, 3 * signal['baselineLoad'])
            and number(w.get('pressure'))
            and w['pressure'] >= max(1, 3 * signal['baselinePressure'])
        ):
            return result
    result.update(
        loadRatio=min(w['load'] for w in windows) / signal['baselineLoad'],
        pressureRatio=min(w['pressure'] for w in windows) / signal['baselinePressure'],
    )
    if any(windows[1][k] < 0.75 * windows[0][k] for k in ('load', 'pressure')):
        result['reason'] = (
            'The spike is fading by more than 25%; keep observing before spending a trial.'
        )
        return result
    context = signal.get('spikeContext') or {}
    recent_demand = context.get('recent') or {}
    robust = context.get('windows') or []
    tails = context.get('tails') or []
    persistent = context.get('persistent') or []
    prolonged = bool(
        len(persistent) == 6
        and all(
            w.get('qualified')
            and all(
                number(w.get(k)) and w[k] >= max(floor, 3 * signal[baseline])
                for k, baseline, floor in [
                    ('load', 'baselineLoad', 10),
                    ('pressure', 'baselinePressure', 1),
                ]
            )
            for w in persistent
        )
    )
    if (
        not context.get('fresh')
        or not number(context.get('sourceAt'))
        or not 0 <= now - context['sourceAt'] < 90
        or not (recent_demand.get('qualified') or prolonged)
        or len(robust) != 2
        or len(tails) != 2
        or not all(w.get('qualified') for w in robust)
        or not tails[-1].get('qualified')
    ):
        result['reason'] = (
            'A spike trial needs covered recent demand: the preceding 30 minutes and a fresh two-minute tail.'
        )
        return result
    for k, baseline, floor in [('load', 'baselineLoad', 10), ('pressure', 'baselinePressure', 1)]:
        recent_floor = (
            0 if prolonged else 1.5 * recent_demand[k] if number(recent_demand.get(k)) else math.inf
        )
        if any(
            not number(w.get(k)) or w[k] < max(floor, 3 * signal[baseline], recent_floor)
            for w in robust
        ):
            result['reason'] = (
                'Typical demand must exceed 3× historical levels in both five-minute windows, plus 1.5× recent demand or 30 minutes of persistent exceptional demand. Brief peaks do not qualify.'
            )
            return result
        if not number(tails[-1].get(k)) or tails[-1][k] < max(
            floor, 3 * signal[baseline], 0.75 * robust[-1][k]
        ):
            result['reason'] = (
                'The latest two-minute demand has faded; keep observing before leaving productive work.'
            )
            return result
    if len(recent) >= MAX_TRIALS_PER_DAY:
        result['reason'] = (
            'The three exceptional-demand trials for this rolling day have been used; passive learning continues.'
        )
    else:
        # Retry only a materially different demand regime, not the same plateau
        # every time we return to the incumbent. Sparse old zeros stay neutral.
        repeated = []
        for run in recent:
            old = (run.get('decision') or {}).get('candidate', {}).get('signal') or {}
            if run.get('model') == model and all(
                number(old.get(k)) and old[k] > 0 and min(w[k] for w in windows) < 2 * old[k]
                for k in ('load', 'pressure')
            ):
                repeated.append(run)
        if repeated:
            result['reason'] = (
                'This demand regime was already tried today; another spike trial needs twice its load or load per warm provider.'
            )
        else:
            result.update(
                qualified=True,
                reason=(
                    'Typical demand remains exceptional through all six five-minute windows, with a sustained fresh tail.'
                    if prolonged
                    else 'Both five-minute windows show typical demand above 3× historical and 1.5× recent levels, with a sustained fresh tail.'
                ),
            )
    return result


def review(runs, trial, current, live_paid, activity, now, settled_paid=None, context=None):
    """Compare one exceptional trial with its own saved incumbent, never Gemma by fiat."""
    if not trial or not trial.get('current') or trial.get('model') != current:
        return None
    run = next(
        (r for r in runs if r.get('id') == trial.get('runId') and r.get('model') == current), None
    )
    decision = (run or {}).get('decision') or {}
    anchor = decision.get('spikeTrial') or decision.get('learningTrial')
    learning = bool(decision.get('learningTrial'))
    if not anchor or decision.get('spikeResolution') or decision.get('trialResolution'):
        return None
    reference = anchor.get('referenceRate')
    if (
        not number(reference)
        or reference < 0
        or reference == 0
        and not learning
        or not anchor.get('incumbent')
    ):
        return None
    clock_trial = bool(decision.get('economicTrial'))
    trial_rate = (
        (trial.get('clock') or {}).get('usdPerHour') if clock_trial else trial.get('usdPerHour')
    )
    result = {
        'runId': run['id'],
        'incumbent': anchor['incumbent'],
        'referenceRate': reference,
        'trialRate': trial_rate,
        'liveRate': live_paid.get('rate') if live_paid.get('fresh') else None,
        'rateBasis': 'settled_inference_per_elapsed_selection_hour' if clock_trial else 'warm_hour',
        'learning': learning,
        'status': 'measuring',
        'reason': (
            'Collecting complete payment samples before comparing this baseline trial with the previous model.'
            if learning
            else 'Measuring this exceptional-demand trial before comparing it with the previous model.'
        ),
        'at': now,
        'deadline': run['at'] + MAX_CLOCK_MINUTES * 60,
    }
    if not learning:
        result['demand'] = retention(decision, context, trial.get('warmStartedAt'), now)
        # Only this session's verified warm/paid observations can end a faded
        # spike early. A network dip alone cannot dislodge useful paid work.
        paid = settled_paid or {}
        fresh = bool(activity.get('fresh') and live_paid.get('fresh'))
        if fresh and (paid.get('highEarnings') or {}).get('active'):
            result.update(
                status='keep',
                reason='Paid earnings are consistently at least $0.20/hour. Keep this model despite network fluctuations.',
            )
            return result
        low_paid = bool(
            fresh
            and number(live_paid.get('rate'))
            and live_paid['rate'] < 0.75 * reference
            and number(paid.get('fastRate'))
            and paid['fastRate'] < 0.75 * reference
            and number(paid.get('asOf'))
            and 0 <= now - paid['asOf'] <= 240
            and paid.get('warmMinutes', 0) >= 4
        )
        if (
            not trial.get('complete')
            and now < result['deadline']
            and result['demand']['status'] == 'faded'
            and trial.get('warmSeconds', 0) >= 300
            and low_paid
        ):
            result.update(
                status='return',
                reason='The spike faded across two covered two-minute windows and fresh plus settled paid pace remain below 75% of the previous model. End this spike trial early, retain its partial observations and recheck the previous model.',
            )
            return result
    if not trial.get('complete') and now < result['deadline']:
        if trial.get('status') == 'settling':
            result.update(
                status='settling',
                reason='The warm trial is complete; waiting up to five minutes for its credits to settle.',
            )
        return result
    if not activity.get('fresh') or not live_paid.get('fresh'):
        result.update(
            status='waiting',
            reason='Trial comparison is waiting for fresh verified paid readings; missing data is not zero earnings.',
        )
        return result
    if (settled_paid or {}).get('highEarnings', {}).get('active'):
        result.update(
            status='keep',
            reason='Paid earnings are consistently at least $0.20/hour. Keep this model; no further trial or speculative return is needed.',
        )
        return result
    # Once the bounded comparison finishes, its frozen result must not undo
    # later verified productive work using an old incumbent benchmark. Normal
    # paid-upgrade comparisons resume after this experiment resolves.
    productive_floor = (settled_paid or {}).get('productiveFloor')
    if (
        (trial.get('complete') or now >= result['deadline'])
        and number(productive_floor)
        and productive_floor > 0
        and number(live_paid.get('rate'))
        and live_paid['rate'] >= productive_floor
    ):
        result.update(
            status='keep',
            reason='Fresh confirmed five-minute earnings have recovered into the productive band. Close the trial comparison and resume ordinary paid-opportunity checks; the old incumbent rate is not a current forecast.',
        )
        return result
    # A new paid burst gets one short extension instead of an immediate return
    # based on a lagging average. The clock deadline prevents endless extension.
    rate = trial_rate
    comparable = bool(
        trial.get('settled')
        and number(rate)
        and (not clock_trial or (trial.get('clock') or {}).get('qualified'))
        and trial.get('paidWarmSeconds', 0)
        >= (
            max(600, trial.get('warmSeconds', 0)) - 120
            if learning
            else (trial['trialMinutes'] - 2) * 60
        )
    )
    recovered = bool(
        settled_paid
        and number(settled_paid.get('fastRate'))
        and number(settled_paid.get('asOf'))
        and 0 <= now - settled_paid['asOf'] <= 240
        and settled_paid['fastRate'] >= reference
        and live_paid['rate'] >= reference
    )
    if (
        recovered
        or comparable
        and rate >= reference * 0.95
        and live_paid['rate'] >= reference * 0.95
    ):
        result.update(
            status='keep',
            reason=(
                'Settled and fresh five-minute paid windows confirm the recovery. Keep serving and learning.'
                if recovered
                else 'The settled trial and fresh paid pace are competitive with the previous model. Keep serving and learning; an earnings upgrade is not assumed.'
            ),
        )
    elif (
        live_paid['rate'] >= reference
        and now < (trial.get('windowEnd') or now) + 600
        and now < result['deadline']
    ):
        result.update(
            status='recovering',
            reason='Fresh paid work has recovered; observing up to ten minutes after the trial window before deciding whether to return.',
        )
    else:
        result.update(
            status='return',
            reason=(
                'The trial did not match the previous model’s paid pace. Return to that model if its demand and resource checks still pass.'
                if comparable
                else 'The bounded trial ended without enough paid coverage to justify staying. Keep the partial data and recheck a return to the previous model.'
            ),
        )
    return result
