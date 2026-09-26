"""Prospective trial comparisons and elapsed sampling time, never counterfactual pay."""

import hashlib
import json
import math

from model_combinations import members, selection_key

REVISION = 'paid-trials-v1'
MAX_CLOCK_MINUTES = 40
# Reuse the existing exceptional-trial envelope (3 x 40 minutes) as one shared
# prospective discovery budget. Returns and independently paid upgrades are exempt.
MAX_DAILY_MINUTES = 3 * MAX_CLOCK_MINUTES
RETURN_TRIGGERS = ('spike_return', 'baseline_return', 'ordinary_return', 'preferred_return')


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def independent_paid(goal, baseline, now):
    live = goal.get('livePaid') or {}
    return bool(
        baseline
        and live.get('fresh')
        and number(live.get('rate'))
        and number(goal.get('fastRate'))
        and goal.get('warmMinutes', 0) >= 4
        and number(goal.get('asOf'))
        and 0 <= now - goal['asOf'] <= 240
        and (
            baseline.get('recent')
            or number(baseline.get('recentGuardRate'))
            or baseline.get('forecastUsable')
        )
    )


def comparison(current, goal, baseline, now, regime, purpose, minutes, expected=None):
    live = goal.get('livePaid') or {}
    qualified = independent_paid(goal, baseline, now) and goal.get('warmMinutes', 0) >= 20
    rates = [goal.get('rate'), goal.get('fastRate'), live.get('rate')]
    qualified = bool(qualified and all(number(r) and r >= 0 for r in rates))
    # Judge a trial against what the incumbent normally earns at demand like
    # now, not only its pace at the moment it was interrupted (often a dip).
    expected = expected if number(expected) and expected >= 0 else None
    low = max(min(rates), expected or 0) if qualified else None
    high = max(max(rates), expected or 0) if qualified else None
    return {
        'revision': REVISION,
        'purpose': purpose,
        'incumbent': current,
        'referenceRate': high,
        'comparison': {
            'qualified': qualified,
            'lower': low * 0.95 if qualified else None,
            'upper': high * 1.05 if qualified else None,
            'expectedRate': expected,
            'observedAt': goal.get('asOf'),
            'capturedAt': now,
            'warmMinutes': goal.get('warmMinutes'),
            'liveRate': live.get('rate'),
        },
        'regime': regime,
        'measurementMinutes': minutes,
        'maxClockMinutes': MAX_CLOCK_MINUTES,
        'deadline': now + MAX_CLOCK_MINUTES * 60,
    }


def session_span(history, device, run, now):
    """Match the actual provider lifecycle; never extend it to the next auto run."""
    key = (run.get('decision') or {}).get('providerSession')
    if not key:
        return None
    with history.lock:
        exists = history.db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='provider_sessions'"
        ).fetchone()
        if not exists:
            return None
        rows = history.db.execute(
            """SELECT data FROM provider_sessions
            WHERE json_extract(data,'$.providerStartedAt')>=? AND json_extract(data,'$.startedAt')<=?""",
            (run['at'] - 600, now),
        ).fetchall()
    matches = []
    for row in rows:
        s = json.loads(row[0])
        process = s.get('_process') or {}
        signature = hashlib.sha256(
            json.dumps(
                [
                    device,
                    process.get('pid'),
                    s.get('providerStartedAt'),
                    members(selection_key(s.get('models'))),
                ],
                sort_keys=True,
            ).encode()
        ).hexdigest()
        if signature == key and selection_key(s.get('models')) == run['model']:
            matches.append(s)
    if not matches:
        return None
    start = min(s['startedAt'] for s in matches)
    end = max(min(now, s.get('endedAt') or s.get('lastSeenAt') or start) for s in matches)
    return {'start': start, 'end': end, 'ended': all(s.get('endedAt') is not None for s in matches)}


def clock_evidence(store, account, device, run, end, now):
    result = {
        'start': run['at'],
        'end': end,
        'seconds': None,
        'usd': None,
        'usdPerHour': None,
        'coveragePercent': 0,
        'qualified': False,
    }
    if not number(end) or end <= run['at'] or end > now - 120:
        return result
    span = session_span(store.h, device, run, now)
    if not span or span['end'] < end or span['start'] > run.get('completedAt', end):
        return result
    start = run['at']
    seconds = end - start
    with store.h.lock:
        covered = store.h.db.execute(
            'SELECT start,end FROM opt_coverage WHERE account=? AND end>? AND start<? ORDER BY start',
            (account, start, end),
        ).fetchall()
        cursor = start
        known = 0
        for low, high in covered:
            low, high = max(start, low, cursor), min(end, high)
            if high > low:
                known += high - low
                cursor = high
        revenue = store.h.db.execute(
            """SELECT c.model,SUM(c.micro_usd)/1000000.0,
            SUM(CASE WHEN c.micro_usd>0 THEN 1 ELSE 0 END) FROM opt_credits c
            WHERE c.account=? AND c.at>=? AND c.at<? AND c.model!='base_reward'
            AND EXISTS(SELECT 1 FROM opt_identity i WHERE i.device=? AND i.provider=c.provider)
            GROUP BY c.model""",
            (account, max(start, span['start']), end, device),
        ).fetchall()
    full = known >= seconds - 0.000001
    money = (
        sum(value for model, value, _ in revenue if model in members(run['model']))
        if full
        else None
    )
    result.update(
        seconds=seconds,
        coveragePercent=min(100, known * 100 / seconds),
        usd=money,
        usdPerHour=money * 3600 / seconds if full else None,
        qualified=full,
        paymentSeen=any(count > 0 for model, _, count in revenue if model in members(run['model'])),
    )
    return result


def competitive(anchor, clock, settled):
    saved = (anchor or {}).get('comparison') or {}
    rate = clock.get('usdPerHour')
    outcome = 'uncertain'
    if (
        settled
        and saved.get('qualified')
        and clock.get('qualified')
        and number(rate)
        and number(saved.get('lower'))
        and number(saved.get('upper'))
    ):
        # Reuse the paid-upgrade path's half-cent/hour minimum difference.
        # Microscopic positive credits against zero do not establish a win.
        if rate >= saved['upper'] + 0.005:
            outcome = 'win'
        elif rate <= saved['lower'] - 0.005:
            outcome = 'loss'
    return {
        'outcome': outcome,
        'rateBasis': 'settled_inference_per_elapsed_selection_hour',
        'comparison': saved,
        'regime': (anchor or {}).get('regime'),
        'revision': (anchor or {}).get('revision'),
    }


def sampling_budget(runs, now, reserve=False, limit_minutes=None):
    limit = MAX_DAILY_MINUTES if limit_minutes is None else limit_minutes
    seconds = 0
    uncertain = 0
    starts = sorted(r['at'] for r in runs if number(r.get('at')))
    for run in runs:
        d = run.get('decision') or {}
        anchor = d.get('economicTrial')
        if not anchor or not anchor.get('sampling') or run['at'] > now:
            continue
        resolution = d.get('trialResolution') or d.get('spikeResolution') or {}
        occupancy = (d.get('outcome') or {}).get('occupancy') or {}
        end = resolution.get('at') or occupancy.get('end')
        if run.get('result') in ('failed', 'recovered', 'interrupted') and number(
            run.get('completedAt')
        ):
            end = run['completedAt']
            occupancy = {'ended': True}
        if not number(end):
            # Unknown lifecycle is charged at its reservation, not as zero time.
            end = min(now, run['at'] + MAX_CLOCK_MINUTES * 60)
            uncertain += 1
        elif not resolution and not occupancy.get('ended'):
            end = now
        # A later switch attempt ends this trial, even if the model then sat
        # unserved (Sep 26: a drained provider kept a trial open for 9 hours).
        later = [t for t in starts if t > run['at']]
        if later:
            end = min(end, later[0])
        seconds += max(0, min(now, end) - max(run['at'], now - 86400))
    required = MAX_CLOCK_MINUTES if reserve else 0
    return {
        'minutesUsed': seconds / 60,
        'minutesLimit': limit,
        'reservationMinutes': required,
        'unknownLifecycles': uncertain,
        'qualified': seconds / 60 + required <= limit + 1e-9,
        'cohort': REVISION,
    }


def sampling_available_at(runs, now, limit_minutes=None, step=300):
    """When the next trial fits the rolling budget, on a fixed 5-minute grid so the
    answer doesn't drift as time passes. None if learning time is off or not within a day."""
    limit = MAX_DAILY_MINUTES if limit_minutes is None else limit_minutes
    if not limit or MAX_CLOCK_MINUTES > limit:
        return None
    start = math.ceil(now / step) * step
    for at in range(int(start), int(now) + 86400 + step, step):
        if sampling_budget(runs, at, reserve=True, limit_minutes=limit)['qualified']:
            return at
    return None


# This is an admission cooldown, not an assertion of unchosen earnings.
# The six-hour bound permits a new observed period without a permanent model ban.
RETRY_SECONDS = 6 * 3600
DISCOVERY_REVISION = 'bounded-ordinary-discovery-v1'


def discovery_retry(runs, model, signal, now):
    result = {
        'held': False,
        'until': None,
        'priorRunId': None,
        'changedContext': False,
        'revision': DISCOVERY_REVISION,
        'reason': None,
    }
    for run in sorted(runs, key=lambda r: r['at'], reverse=True):
        decision = run.get('decision') or {}
        anchor = decision.get('economicTrial') or {}
        outcome = decision.get('outcome') or {}
        resolution = decision.get('trialResolution') or decision.get('spikeResolution') or {}
        if (
            run.get('model') != model
            or run['at'] > now
            or not anchor.get('sampling')
            or anchor.get('revision') != REVISION
            or run.get('result') != 'switched'
        ):
            continue
        ended = (
            resolution.get('at') or outcome.get('windowEnd') or run.get('completedAt') or run['at']
        )
        if not number(ended) or not 0 <= now - ended < RETRY_SECONDS:
            continue
        economic = outcome.get('competitive') or {}
        prior_signal = (decision.get('candidate') or {}).get('signal') or {}
        current_regime = signal.get('regime') or {}
        comparable_success = (
            outcome.get('complete')
            and outcome.get('settled')
            and economic.get('outcome') == 'win'
            and economic.get('revision') == REVISION
            and (economic.get('comparison') or {}).get('qualified')
            and (outcome.get('clock') or {}).get('qualified')
            and current_regime.get('providerVersion')
            and economic.get('regime') == current_regime
            and number(signal.get('observedAt'))
            and 0 <= now - signal['observedAt'] < 90
            and signal.get('coverage', 0) >= 0.8
            and signal.get('status') in ('normal', 'spike')
            and (signal.get('sustained') or {}).get('qualified')
            and all(
                number(signal.get(k))
                and signal[k] > 0
                and number(prior_signal.get(k))
                and 0.5 * signal[k] <= prior_signal[k] <= 2 * signal[k]
                for k in ('load', 'pressure')
            )
        )
        if comparable_success:
            result.update(
                priorRunId=run.get('id'),
                changedContext=True,
                reason='A newer comparable settled competitive win supersedes earlier inconclusive or weak samples.',
            )
            return result
        weak = (outcome.get('competitive') or {}).get('outcome') == 'loss'
        inconclusive = resolution.get('status') == 'inconclusive' or (
            resolution.get('status') != 'keep'
            and (outcome.get('complete') or outcome.get('status') == 'interrupted')
            and (outcome.get('competitive') or {}).get('outcome') not in ('win', 'loss')
        )
        if not (weak or inconclusive):
            continue
        prior_regime = anchor.get('regime') or {}
        regime = signal.get('regime') or {}
        fresh = (
            signal.get('status') in ('normal', 'spike')
            and number(signal.get('observedAt'))
            and 0 <= now - signal['observedAt'] < 90
            and signal.get('coverage', 0) >= 0.8
            and (signal.get('sustained') or {}).get('qualified')
            and number(signal.get('load'))
            and signal['load'] > 0
            and number(signal.get('pressure'))
            and signal['pressure'] > 0
        )
        provider_changed = (
            prior_regime.get('providerVersion')
            and regime.get('providerVersion')
            and prior_regime['providerVersion'] != regime['providerVersion']
        )
        stronger_demand = all(
            number(prior_signal.get(k))
            and prior_signal[k] > 0
            and signal.get(k, 0) >= 2 * prior_signal[k]
            for k in ('load', 'pressure')
        )
        changed = bool(fresh and (provider_changed or stronger_demand))
        result.update(
            held=not changed,
            until=ended + RETRY_SECONDS,
            priorRunId=run.get('id'),
            changedContext=changed,
            reason=(
                'Fresh sustained demand or provider context materially changed since the prior sample.'
                if changed
                else 'A recent inconclusive or weak trial already sampled this model. Wait up to six hours or for fresh sustained demand to double in both load and pressure, or a qualified provider-version change.'
            ),
        )
        # Every recent sample must be stale or have a materially changed context.
        if not changed:
            return result
    return result


def ordinary_review(run, trial, goal, activity, now):
    decision = run.get('decision') or {}
    anchor = decision.get('ordinaryTrial')
    if not anchor or decision.get('trialResolution'):
        return None
    live = goal.get('livePaid') or {}
    reference = anchor.get('referenceRate')
    steady = trial.get('steadyUsdPerHour') if number(trial.get('steadyUsdPerHour')) else None
    clock_rate = (
        (trial.get('clock') or {}).get('usdPerHour')
        if (trial.get('clock') or {}).get('qualified')
        else None
    )
    trial_rate = steady if steady is not None else clock_rate
    result = {
        'runId': run['id'],
        'incumbent': anchor['incumbent'],
        'referenceRate': reference,
        'trialRate': trial_rate,
        'rampSeconds': trial.get('rampSeconds'),
        'rateBasis': 'settled_inference_per_warm_hour_after_first_work'
        if steady is not None
        else 'settled_inference_per_elapsed_selection_hour',
        'liveRate': live.get('rate') if live.get('fresh') else None,
        'ordinary': True,
        'at': now,
        'deadline': anchor['deadline'],
        'status': 'measuring',
        'reason': 'Measuring the ordinary trial before its bounded keep/return review.',
    }
    if not trial.get('complete') and now < anchor['deadline']:
        if trial.get('status') == 'settling':
            result.update(
                status='settling',
                reason='The warm trial is complete; waiting for covered settled credits.',
            )
        return result
    recovered = (
        activity.get('fresh')
        and live.get('fresh')
        and number(live.get('rate'))
        and (
            (goal.get('highEarnings') or {}).get('active')
            or (
                number(goal.get('asOf'))
                and 0 <= now - goal['asOf'] <= 240
                and number(goal.get('productiveFloor'))
                and live['rate'] >= goal['productiveFloor']
                and number(goal.get('fastRate'))
                and goal['fastRate'] >= goal['productiveFloor']
            )
        )
    )
    if recovered:
        result.update(
            status='keep',
            reason='Fresh and settled paid earnings are productive. Keep the recovered model and close the trial comparison.',
        )
        return result
    unresolved = (
        not (anchor.get('comparison') or {}).get('qualified')
        or not number(reference)
        or not number((anchor.get('comparison') or {}).get('lower'))
        or not activity.get('fresh')
        or not live.get('fresh')
        or not number(live.get('rate'))
        or not trial.get('settled')
        or not number(trial_rate)
    )
    if now >= anchor['deadline'] and unresolved:
        result.update(
            status='inconclusive',
            allowPaidAlternative=True,
            reason='The ordinary trial ended without a qualified settled comparison. Its result is inconclusive; continue guarded paid-opportunity checks without repeating this sample immediately.',
        )
        return result
    if not activity.get('fresh') or not live.get('fresh') or not number(live.get('rate')):
        result.update(
            status='waiting',
            reason='Trial review is waiting for fresh paid and local readings; missing income is unknown.',
        )
    elif (
        not (anchor.get('comparison') or {}).get('qualified')
        or not number((anchor.get('comparison') or {}).get('lower'))
        or not number(reference)
    ):
        result.update(
            status='waiting',
            allowPaidAlternative=True,
            reason='The saved incumbent has no sufficient paid comparison. Hold until the bounded review ends while checking independently supported paid alternatives.',
        )
    elif (
        trial.get('settled')
        and number(trial_rate)
        and (
            trial_rate >= anchor['comparison']['lower']
            and live['rate'] >= anchor['comparison']['lower']
        )
    ):
        result.update(
            status='keep',
            reason="Settled pay after this model's first work, and its fresh pay, are competitive with what the previous model normally earns at this demand. Keep this model.",
        )
    elif (
        number(reference)
        and live['rate'] >= reference
        and now < min(anchor['deadline'], (trial.get('windowEnd') or now) + 600)
    ):
        result.update(
            status='recovering',
            reason='Fresh pay is recovering. Allow a bounded observation extension before reviewing a return.',
        )
    else:
        result.update(
            status='return',
            reason='The bounded ordinary trial has ended. Recheck the saved incumbent and independently supported alternatives; partial earnings remain unknown.',
        )
    return result
