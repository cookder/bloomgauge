"""Demand-following decisions using this Mac's paid work, never traffic x price.

This module can evaluate and explain opportunities without changing a provider.
The existing Optimizer owns every command and rechecks its normal safety gates.
"""

import json
import math
import threading
from datetime import datetime

from demand_alerts import DemandAlerts
from demand_trials import DemandTrials
from trial_economics import (
    REVISION as ECONOMIC_REVISION,
    RETURN_TRIGGERS,
    independent_paid,
    comparison,
    ordinary_review,
    sampling_budget,
    sampling_available_at,
    discovery_retry,
)
from demand_baselines import network_minutes, conditional, read_view
from demand_fallback import (
    MODEL as FALLBACK_MODEL,
    reliability as fallback_reliability,
    assess as assess_fallback,
)
from demand_targets import GEMMA, DEFAULT_TARGET, target_status, return_evidence, fresh_paid
from model_combinations import members
from demand_spikes import (
    MAX_TRIALS_PER_DAY,
    demand_context,
    spike as spike_opportunity,
    review as spike_review,
)
from baseline_learning import baselines, opportunity as learning_opportunity
from data_gathering import learning_limits

LOOKBACK = 30 * 86400
POLICY_REVISION = 2
FAILURE_RETRY_SECONDS = 15 * 60
LEARNING_PRESSURE = 0.3
LEARNING_MIN_LOAD = 10
RECOVERY_RESERVED_SECONDS = 120
PREVIOUS_DEFAULTS = {
    'minRunMinutes': 60,
    'confirmationMinutes': 10,
    'maxSwitchesPerDay': 4,
    'maxDowntimeMinutes': 20,
    'memoryHeadroomGB': 2,
    'trialCooldownMinutes': 120,
}
POLICY = {
    'minRunMinutes': 30,
    'confirmationMinutes': 5,
    'improvementPercent': 20,
    'planningMinutes': 60,
    'minimumNetUsd': 0.02,
    'maxSwitchesPerDay': 12,
    'maxDowntimeMinutes': 30,
    'memoryHeadroomGB': 1,
    'idleEscapeMinutes': 20,
    'trialMinutes': 20,
    'trialCooldownMinutes': 30,
    'fallbackEnabled': 1,
    'targetUsdPerHour': DEFAULT_TARGET,
    'baselineLearningEnabled': 1,
    'protectUsdPerHour': 0.20,
    'learningMinutesPerDay': 60,
}
# Timing and threshold controls accept any value on a step within a range, so the
# optimizer can be tuned from passive to aggressive. Memory headroom never drops
# below 1 GB. Switches and preferences stay fixed choices.
RANGES = {
    'minRunMinutes': (15, 480, 15),
    'confirmationMinutes': (1, 60, 1),
    'improvementPercent': (5, 100, 5),
    'planningMinutes': (30, 240, 15),
    'minimumNetUsd': (0.005, 0.2, 0.005),
    'maxSwitchesPerDay': (1, 48, 1),
    'maxDowntimeMinutes': (5, 120, 5),
    'memoryHeadroomGB': (1, 8, 0.5),
    'idleEscapeMinutes': (10, 120, 5),
    'trialMinutes': (10, 60, 5),
    'trialCooldownMinutes': (10, 480, 5),
    'protectUsdPerHour': (0.05, 1, 0.01),
    'learningMinutesPerDay': (0, 480, 15),
}
CHOICES = {
    'fallbackEnabled': (0, 1),
    'targetUsdPerHour': (0.08, 0.10, 0.12, 0.15, 0.20, 0.25),
    'baselineLearningEnabled': (0, 1),
}


def allowed(key, value):
    if key in CHOICES:
        return value in CHOICES[key]
    low, high, step = RANGES[key]
    return (
        low - 1e-9 <= value <= high + 1e-9
        and abs((value - low) / step - round((value - low) / step)) < 1e-6
    )


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def policy(value=None):
    if value is None:
        return dict(POLICY)
    if not isinstance(value, dict) or set(value) - set(POLICY):
        raise ValueError('Choose supported demand-switching controls.')
    result = {**POLICY, **value}
    if any(not number(v) or not allowed(k, v) for k, v in result.items()):
        raise ValueError('A demand-switching control is outside its supported range.')
    if result['confirmationMinutes'] > result['minRunMinutes']:
        raise ValueError('Confirmation must fit within the minimum run time.')
    return result


def upgraded_policy(value=None, revision=0):
    """One-time update of old defaults; explicit nondefault controls survive."""
    result = policy(value)
    if revision != POLICY_REVISION and isinstance(value, dict):
        for key, old in PREVIOUS_DEFAULTS.items():
            if value.get(key) == old:
                result[key] = POLICY[key]
        # A user-selected longer confirmation must still fit its run window.
        result['minRunMinutes'] = max(result['minRunMinutes'], result['confirmationMinutes'])
    return policy(result)


def quantile(values, fraction):
    values = sorted(values)
    if not values:
        return None
    index = (len(values) - 1) * fraction
    low = int(index)
    return values[low] + (values[min(low + 1, len(values) - 1)] - values[low]) * (index - low)


def rate_summary(blocks):
    seconds = sum(b['seconds'] for b in blocks)
    if not seconds:
        return None
    rate = sum(b['usd'] for b in blocks) * 3600 / seconds
    # Between-block variability deliberately does not vanish after hundreds of
    # correlated minutes. These are conservative planning bounds, not a CI.
    rates = [b['usd'] * 3600 / b['seconds'] for b in blocks]
    low = min(rate * 0.85, quantile(rates, 0.25))
    high = max(rate * 1.15, quantile(rates, 0.75))
    return {
        'rate': rate,
        'lower': max(0, low),
        'upper': max(0, high),
        'hours': seconds / 3600,
        'blocks': len(blocks),
        'days': len({datetime.fromtimestamp(b['at']).date() for b in blocks}),
    }


def blocks_for(minutes, network=None, step=3600, minimum_seconds=1800):
    groups = {}
    for m in minutes:
        if not all(number(m.get(k)) for k in ('at', 'usd', 'seconds')) or m['seconds'] <= 0:
            continue
        key = int(m['at'] // step) * step
        b = groups.setdefault(
            key,
            {
                'at': key,
                'lastAt': 0,
                'seconds': 0,
                'usd': 0,
                'networkSeconds': 0,
                'pressureSum': 0,
                'loadSum': 0,
            },
        )
        b['seconds'] += m['seconds']
        b['usd'] += m['usd']
        b['lastAt'] = max(b['lastAt'], m['at'] + m['seconds'])
        n = (network or {}).get(int(m['at'] // 60) * 60)
        if n and number(n.get('pressure')) and number(n.get('load')) and n['warm'] >= 1:
            b['networkSeconds'] += m['seconds']
            b['pressureSum'] += n['pressure'] * m['seconds']
            b['loadSum'] += n['load'] * m['seconds']
    result = []
    for b in groups.values():
        if b['seconds'] < minimum_seconds or b['seconds'] > step + 0.000001:
            continue
        b['pressure'] = b['pressureSum'] / b['networkSeconds'] if b['networkSeconds'] else None
        b['load'] = b['loadSum'] / b['networkSeconds'] if b['networkSeconds'] else None
        result.append(b)
    return sorted(result, key=lambda b: b['at'])


def enough_context(blocks):
    s = rate_summary(blocks)
    return bool(s and s['hours'] >= 2 and s['blocks'] >= 4 and s['days'] >= 2)


def estimate(evidence, network, signal, now, current_since=None, condition=None):
    minutes = [
        m
        for m in evidence.get('minutes', [])
        if now - LOOKBACK <= m['at'] and m['at'] + 60 <= now - 120
    ]
    all_blocks = blocks_for(minutes, network)
    if not all_blocks:
        all_blocks = blocks_for(minutes, step=300, minimum_seconds=240)
        if not all_blocks:
            return None
    clock = datetime.fromtimestamp(now)
    chosen = all_blocks
    scope = 'all_observed_hours'
    # Exact day first when it has independent dates; otherwise day type, then
    # all recorded hours. A sparse slot cannot replace the broader evidence.
    for exact in (True, False):
        matching = []
        for b in all_blocks:
            d = datetime.fromtimestamp(b['at'])
            day_match = (
                d.weekday() == clock.weekday()
                if exact
                else (d.weekday() >= 5) == (clock.weekday() >= 5)
            )
            if day_match and min(abs(d.hour - clock.hour), 24 - abs(d.hour - clock.hour)) <= 2:
                matching.append(b)
        if enough_context(matching):
            chosen = matching
            scope = 'weekday_time' if exact else 'daytype_time'
            break
    calibrated = False
    pressure = signal.get('pressure')
    load = signal.get('load')
    if number(pressure) and pressure > 0 and number(load) and load > 0:
        matching = [
            b
            for b in chosen
            if b['networkSeconds'] >= 0.8 * b['seconds']
            and number(b['pressure'])
            and 0.5 * pressure <= b['pressure'] <= 2 * pressure
            and number(b['load'])
            and 0.5 * load <= b['load'] <= 2 * load
        ]
        if enough_context(matching):
            chosen = matching
            calibrated = True
    result = rate_summary(chosen)
    result.update(
        scope=scope,
        demandMatched=calibrated,
        evidenceHours=evidence.get('hours', 0),
        evidenceJobs=evidence.get('jobs', 0),
        recent=False,
        asOf=max(b['lastAt'] for b in chosen),
    )
    # Hours from one run and a handful of jobs are observations, not a stable
    # forecast. Never promote a broad old rate when today's pressure differs.
    established = (
        evidence.get('hours', 0) >= 24
        and evidence.get('days', 0) >= 3
        and evidence.get('jobs', 0) >= 200
        and len(all_blocks) >= 12
    )
    usable = bool(
        established
        and calibrated
        and result['hours'] >= 4
        and result['days'] >= 3
        and now - result['asOf'] <= 7 * 86400
    )
    result.update(
        established=established,
        confidence='matched' if usable else 'limited',
        forecastUsable=usable,
        historyReason='Repeated paid observations at similar demand and time.'
        if usable
        else 'Limited or unmatched history; observed earnings are not a forecast and do not exclude a demand trial.',
    )
    if condition is not None:
        # Production decisions use the same timestamp-paired observations shown
        # in Demand & earnings, including active concurrency and warm capacity.
        # Sufficient comparable paid periods stand on their own. Requiring 24
        # unrelated warm hours rewards time spent serving in the wrong demand.
        usable = bool(
            condition.get(
                'forecastUsable', condition['usable'] and condition.get('paidJobs', 0) >= 200
            )
        )
        if condition['usdPerHour'] is not None:
            result.update(
                rate=condition['usdPerHour'],
                lower=condition['lower'] if condition['lower'] is not None else 0,
                upper=condition['upper']
                if condition['upper'] is not None
                else max(result['upper'], condition['usdPerHour'] * 1.15),
                hours=condition['hours'],
                days=condition['days'],
                blocks=condition['blocks'],
                scope=condition['scope'],
                asOf=condition['asOf'],
            )
        result.update(
            demandMatched=bool(condition['hours']),
            forecastUsable=usable,
            confidence='matched' if usable else 'limited',
            historyReason=condition['reason'],
        )
    # The current model may be earning far more (or less) than its usual rate.
    # Only settled, fully warm/covered minutes from THIS provider session count.
    if number(current_since):
        recent = [m for m in minutes if m['at'] >= max(current_since, now - 1920)]
        recent_blocks = blocks_for(recent, step=300, minimum_seconds=240)
        sample = rate_summary(recent_blocks)
        if (
            sample
            and sample['hours'] >= 24 / 60
            and len(recent_blocks) >= 5
            and recent
            and now - (recent[-1]['at'] + 60) <= 240
        ):
            result = {
                **result,
                **sample,
                'scope': 'current_session_30m',
                'recent': True,
                'demandMatched': False,
                'asOf': max(m['at'] + 60 for m in recent),
            }
        fast_end = int((now - 120) // 60) * 60
        fast = [
            m
            for m in minutes
            if m['at'] >= max(current_since, fast_end - 300) and m['at'] + 60 <= fast_end
        ]
        fast_seconds = sum(m['seconds'] for m in fast)
        if fast_seconds >= 240 and now - max(m['at'] + 60 for m in fast) <= 240:
            fast_rate = sum(m['usd'] for m in fast) * 3600 / fast_seconds
            result['upper'] = max(result['upper'], fast_rate * 1.15)
            result['recentGuardRate'] = max(0, fast_rate)
            result['recentGuardAt'] = max(m['at'] + 60 for m in fast)
    return result


def sustained_demand(samples, now, pressure_floor=0.5, min_load=1, observed_floor=0.25):
    """Two independent 5m windows; a one-sample spike cannot win a trial."""
    cutoff = int(now // 30) * 30
    buckets = {}
    for sample in samples:
        if not all(
            number(sample.get(k)) and 0 <= sample[k] <= 1e12
            for k in ('at', 'active', 'queued', 'warm')
        ):
            continue
        if not cutoff - 600 <= sample['at'] < cutoff:
            continue
        key = int(sample['at'] // 30)
        if key not in buckets or buckets[key]['at'] < sample['at']:
            buckets[key] = sample
    windows = []
    for start in (cutoff - 600, cutoff - 300):
        rows = [s for s in buckets.values() if start <= s['at'] < start + 300]
        pressures = [(s['active'] + s['queued']) / s['warm'] for s in rows if s['warm'] >= 1]
        good = bool(len(rows) >= 8 and len(pressures) == len(rows))
        pressure = sum(pressures) / len(pressures) if pressures else None
        load = sum(s['active'] + s['queued'] for s in rows) / len(rows) if rows else None
        good = bool(
            good
            and pressure >= pressure_floor
            and load >= min_load
            and sum(p >= observed_floor for p in pressures) >= 0.8 * len(rows)
        )
        windows.append(
            {'pressure': pressure, 'load': load, 'coverage': len(rows) / 10, 'qualified': good}
        )
    source = max((s['at'] for s in buckets.values()), default=None)
    return {
        'qualified': all(w['qualified'] for w in windows)
        and source is not None
        and 0 <= now - source < 90,
        'seconds': 600,
        'samples': len(buckets),
        'sourceAt': source,
        'pressure': min(w['pressure'] for w in windows)
        if all(w['pressure'] is not None for w in windows)
        else None,
        'windows': windows,
    }


def context_weight(runs, model, signal, now):
    # Only repeated trials on separate dates can nudge the pressure ranking.
    # An isolated zero (or jackpot) never changes another model's trial odds.
    clock = datetime.fromtimestamp(now)
    pressure = signal.get('pressure')
    matching = []
    for run in runs:
        outcome = (run.get('decision') or {}).get('outcome') or {}
        p = outcome.get('pressure')
        d = datetime.fromtimestamp(run['at'])
        economic = outcome.get('competitive') or {}
        regime = signal.get('regime') or {}
        if (
            run.get('model') == model
            and now - 7 * 86400 <= run['at'] <= now
            and outcome.get('complete')
            and outcome.get('settled')
            and economic.get('outcome') in ('win', 'loss')
            and economic.get('revision') == ECONOMIC_REVISION
            and regime.get('providerVersion')
            and economic.get('regime') == regime
            and number(signal.get('load'))
            and signal['load'] > 0
            and number(outcome.get('load'))
            and 0.5 * signal['load'] <= outcome['load'] <= 2 * signal['load']
            and number(pressure)
            and pressure > 0
            and number(p)
            and 0.5 * pressure <= p <= 2 * pressure
            and (d.weekday() >= 5) == (clock.weekday() >= 5)
            and min(abs(d.hour - clock.hour), 24 - abs(d.hour - clock.hour)) <= 2
        ):
            matching.append(run)
    days = len({datetime.fromtimestamp(r['at']).date() for r in matching})
    if len(matching) < 3 or days < 2:
        return 1, len(matching)
    success = sum(r['decision']['outcome']['competitive']['outcome'] == 'win' for r in matching)
    return 0.8 + 0.4 * (success + 1) / (len(matching) + 2), len(matching)


def switch_timing(events, target, now):
    successful = sorted(
        (
            e
            for e in events
            if now - LOOKBACK <= e.get('at', 0) <= now
            and e.get('kind') == 'switched'
            and number(e.get('downtime'))
            and e['downtime'] > 0
        ),
        key=lambda e: e['at'],
        reverse=True,
    )
    same = [e['downtime'] for e in successful if e.get('model') == target][:5]
    if same:
        # Recent actual verification times, without a multi-minute minimum or
        # another uncertainty multiplier on top of the earnings bounds.
        return {
            'seconds': quantile(same, 0.75) if len(same) >= 3 else same[0],
            'scope': 'measured' if len(same) >= 3 else 'limited_history',
            'samples': len(same),
        }
    local = [
        e['downtime'] for e in successful if len(members(e.get('model'))) == len(members(target))
    ][:10]
    if len(local) >= 3:
        return {'seconds': quantile(local, 0.75), 'scope': 'mac_history', 'samples': len(local)}
    return {'seconds': 120, 'scope': 'unmeasured', 'samples': 0}


def switch_seconds(events, target, now):
    timing = switch_timing(events, target, now)
    return timing['seconds'], timing['scope']


def last_failure(events, target, now):
    relevant = [
        e
        for e in events
        if e.get('model') == target
        and now - FAILURE_RETRY_SECONDS < e.get('at', 0) <= now
        and e.get('kind') in ('switched', 'failed', 'recovered')
        and e.get('downtime', 0) > 0
    ]
    latest = max(relevant, key=lambda e: e['at'], default={})
    return latest.get('at') if latest.get('kind') in ('failed', 'recovered') else None


def earnings_value(baseline, predicted, outbound, rules):
    """The same conservative economics for ordinary and completed-trial exits."""
    old = max(0, baseline['upper'])
    new = max(0, predicted['lower'])
    delta = new - old
    cost = new * outbound / 3600
    net = delta * rules['planningMinutes'] / 60 - cost
    return {
        'cost': cost,
        'net': net,
        'improvement': (new / old - 1) * 100 if old > 0 else None,
        'payback': cost / delta * 60 if delta > 0 else None,
        'qualified': delta >= 0.005
        and new >= old * (1 + rules['improvementPercent'] / 100)
        and net >= rules['minimumNetUsd'],
    }


def reclaimable(budget):
    """File cache the purge before a switch may free (0 without purge permission)."""
    value = budget.get('reclaimableGB')
    return value if number(value) and value > 0 else 0


def learning_time_reason(budget):
    if not budget['minutesLimit']:
        return 'Learning time is off. Confident paid upgrades and returns remain available.'
    at = budget.get('availableAt')
    when = datetime.fromtimestamp(at).strftime('%I:%M %p').lstrip('0') if number(at) else None
    return (
        "Today's learning time is used up (%d of %d minutes in the last 24 hours; each run needs %d minutes free).%s "
        'Confident paid upgrades and returns remain available.'
        % (
            round(budget['minutesUsed']),
            budget['minutesLimit'],
            budget['reservationMinutes'],
            ' The next learning run can start around %s.' % when if when else '',
        )
    )


def decide(
    rows,
    current,
    estimates,
    events,
    runs,
    rules,
    now,
    last_switch=0,
    activity=None,
    trial=None,
    gathering=None,
    stall_escape=False,
):
    """Pure decision. Trial permission and a profitable forecast are distinct."""
    gathering = gathering or {'active': False}
    limits_learning = learning_limits(gathering, rules)
    baseline = estimates.get(current)
    activity = activity or {'fresh': False, 'idleSeconds': 0, 'idleSince': None}
    idle_seconds = activity.get('idleSeconds', 0) if activity.get('fresh') else 0
    # Steady work stopped and the recovery steps did not bring it back
    # (stall_control.py): open the idle escape now and treat current pay as zero.
    stalled = bool(stall_escape and activity.get('fresh'))
    if stalled:
        idle_seconds = max(idle_seconds, rules['idleEscapeMinutes'] * 60)
    current_row = next((r for r in rows if r['id'] == current), {})
    matched_current = current_row.get('conditional') or {}
    expected_current = (
        matched_current.get('usdPerHour')
        if matched_current.get('usable') and number(matched_current.get('usdPerHour'))
        else None
    )
    goal = dict(current_row.get('earningsTarget') or target_status({}, None, now, rules))
    live_paid = goal.get('livePaid') or {}
    if baseline and live_paid.get('fresh') and number(live_paid.get('rate')):
        # Recheck the recovering paid tail, not only the lagged settled average.
        baseline = {
            **baseline,
            'upper': max(baseline['upper'], live_paid['rate']),
            'liveGuardRate': live_paid['rate'],
            'liveGuardAt': live_paid.get('asOf'),
        }
    high_hold = (goal.get('highEarnings') or {}).get('active', False)
    if high_hold:
        goal.update(ready=False, status='high_earnings', reason=goal['highEarnings']['reason'])
    shortfall = bool(goal['ready'] and activity.get('fresh'))
    if goal['ready'] and not activity.get('fresh'):
        goal.update(
            ready=False,
            status='stale',
            reason='Waiting for fresh verified local activity before acting on the earnings target.',
        )
    # Compare returns with what the current model earns now, not a lagged average.
    current_pace = (
        live_paid['rate']
        if live_paid.get('fresh') and number(live_paid.get('rate'))
        else (goal.get('rate') or 0)
    )
    # Lagged averages still include pay from before a stall.
    pace_reference = (
        0
        if stalled
        else max(goal.get('rate') or 0, goal.get('fastRate') or 0, live_paid.get('rate') or 0)
    )
    preferred = dict(
        next((r.get('returnEvidence') for r in rows if r['id'] == GEMMA), None)
        or return_evidence(None, now)
    )
    # An old unpaid trial cannot replace current pay after a collection gap.
    # Only a fresh known zero can qualify this escape; missing is not zero.
    failed_trial = bool(
        trial
        and trial.get('current')
        and trial.get('complete')
        and trial.get('settled')
        and trial.get('status') in ('no_traffic', 'no_paid_work')
        and activity.get('fresh')
        and not (baseline or {}).get('recentGuardRate')
        and live_paid.get('fresh')
        and number(live_paid.get('rate'))
        and live_paid['rate'] == 0
    )
    # Data gathering explores on fresh activity alone; the high-earnings hold still wins.
    collecting = bool(gathering.get('active') and activity.get('fresh'))
    exploring = not high_hold and (idle_seconds >= 600 or failed_trial or shortfall or collecting)
    can_escape = (
        idle_seconds >= rules['idleEscapeMinutes'] * 60 or failed_trial or shortfall or collecting
    )
    spike_result = spike_review(
        runs,
        trial,
        current,
        live_paid,
        activity,
        now,
        goal,
        (current_row.get('signal') or {}).get('spikeContext'),
    )
    ordinary_resolved = False
    if trial and trial.get('current') and trial.get('model') == current:
        active_run = next((r for r in runs if r.get('id') == trial.get('runId')), None)
        if active_run:
            saved = active_run.get('decision') or {}
            ordinary_resolved = bool(
                saved.get('ordinaryTrial')
                and (saved.get('trialResolution') or {}).get('status') in ('keep', 'inconclusive')
            )
        if active_run and not spike_result:
            spike_result = ordinary_review(active_run, trial, goal, activity, now)
    trial_running = bool(
        trial
        and trial.get('current')
        and trial.get('status') in ('running', 'settling')
        and not ordinary_resolved
    )
    if spike_result:
        trial_running = spike_result['status'] not in ('return', 'keep', 'inconclusive')
    returning_spike = bool(spike_result and spike_result['status'] == 'return')
    fallback = assess_fallback(rows, current, activity, rules.get('fallbackEnabled', 1) == 1, now)
    recent_runs = [r for r in runs if now - 86400 < r['at'] <= now]
    used_seconds = sum(
        r['downtime'] if r.get('downtime') is not None else r.get('reservedSeconds', 0)
        for r in recent_runs
    )
    limits = {
        'switchesUsed': len(recent_runs),
        'switchLimit': rules['maxSwitchesPerDay'],
        'downtimeMinutesUsed': used_seconds / 60,
        'downtimeMinutesLimit': rules['maxDowntimeMinutes'],
        'nextRunAt': last_switch + rules['minRunMinutes'] * 60 if last_switch else now,
    }
    sample_budget = sampling_budget(
        runs, now, reserve=True, limit_minutes=limits_learning['samplingMinutes']
    )
    if not sample_budget['qualified']:
        sample_budget['availableAt'] = sampling_available_at(
            runs, now, limits_learning['samplingMinutes']
        )
    limits['sampling'] = sample_budget
    opportunities = []
    guard_holds = {}
    for row in rows:
        model = row['id']
        signal = row.get('signal') or {}
        predicted = estimates.get(model)
        budget = row.get('loadBudget')
        reason = None
        timing = switch_timing(events, model, now)
        outbound, cost_scope = timing['seconds'], timing['scope']
        returning, _ = switch_seconds(events, current, now)
        failure = last_failure(events, model, now)
        latest_trial = max(
            (
                r
                for r in runs
                if r.get('model') == model
                and (r.get('decision') or {}).get('outcome', {}).get('complete')
            ),
            key=lambda r: r['at'],
            default={},
        )
        cooldown = latest_trial.get('decision', {}).get('outcome', {}).get('cooldownUntil')
        ended = latest_trial.get('decision', {}).get('outcome', {}).get('windowEnd')
        if number(cooldown) and number(ended):
            cooldown = min(cooldown, ended + rules['trialCooldownMinutes'] * 60)
        cooldown = cooldown if number(cooldown) and cooldown > now else None
        net = cost = payback = improvement = None
        sustained = signal.get('sustained') or {}
        weight, context_trials = context_weight(runs, model, signal, now)
        matched = row.get('conditional')
        # Repeated passive observations and trial outcomes share ONE ±20% cap.
        # Thin histories retain a neutral weight, including observed zero dollars.
        weight = max(
            0.8, min(1.2, weight * (matched['weight'] if matched and matched['usable'] else 1))
        )
        ratio = signal.get('pressureRatio')
        pressure_score = (
            (sustained.get('pressure') or 0)
            * (max(0.5, min(2, ratio)) ** 0.25 if number(ratio) else 1)
            * weight
        )
        exceptional = spike_opportunity(signal, runs, model, now)
        spike_ready = bool(
            exceptional['qualified']
            and not high_hold
            and not exploring
            and not spike_result
            and activity.get('fresh')
            and live_paid.get('fresh')
            and live_paid.get('rate', 0) > 0
            and goal.get('warmMinutes', 0) >= 30
            and now - last_switch >= rules['minRunMinutes'] * 60
            and not (predicted or {}).get('forecastUsable')
        )
        is_return = returning_spike and model == spike_result['incumbent']
        learning = learning_opportunity(
            row.get('paymentBaseline'),
            signal,
            runs,
            model,
            now,
            rules.get('baselineLearningEnabled', 1),
            limits_learning,
        )
        paid_upgrade = bool(
            (predicted or {}).get('forecastUsable')
            and predicted['lower']
            > max(
                0.005,
                max(goal.get('rate') or 0, goal.get('fastRate') or 0, live_paid.get('rate') or 0)
                * 1.2,
            )
        )
        learning_ready = bool(
            (shortfall or collecting)
            and not high_hold
            and not spike_result
            and learning['qualified']
            and not paid_upgrade
        )
        supported_return = bool(
            model == GEMMA
            and preferred.get('qualified')
            and preferred['rate'] > max(0.005, current_pace * 1.2 if shortfall else 0)
        )
        prospective = comparison(
            current,
            goal,
            baseline,
            now,
            signal.get('regime'),
            'ordinary',
            rules['trialMinutes'],
            expected_current,
        )
        retry = discovery_retry(runs, model, signal, now)
        kind = 'explore' if exploring or spike_ready or is_return else 'earnings'
        value = (
            earnings_value(baseline, predicted, outbound, rules)
            if baseline and (predicted or {}).get('forecastUsable')
            else None
        )
        # Independent current paid evidence does not inherit discovery's
        # pressure floor. Incomplete trial dollars are never the benchmark.
        # An owned experiment still blocks until its review permits an exit.
        legacy_exit = bool(
            trial and trial.get('current') and trial.get('complete') and trial.get('settled')
        )
        paid_review = bool(
            spike_result
            and spike_result.get('ordinary')
            and (returning_spike or spike_result.get('allowPaidAlternative'))
        )
        independent = independent_paid(goal, baseline, now)
        if (
            (exploring or paid_review)
            and (not spike_result or paid_review)
            and (not trial_running or paid_review)
            and (
                activity.get('fresh')
                and live_paid.get('fresh')
                and value
                and value['qualified']
                and signal.get('status') in ('normal', 'spike')
                and (
                    independent
                    or legacy_exit
                    and (
                        baseline.get('forecastUsable')
                        or baseline.get('recent')
                        or number(baseline.get('recentGuardRate'))
                    )
                )
                and number(predicted.get('asOf'))
                and 0 <= now - predicted['asOf'] <= 7 * 86400
            )
        ):
            kind = 'earnings'
        paid_supported = bool(
            model != current
            and row.get('selected')
            and independent
            and value
            and value['qualified']
            and signal.get('status') in ('normal', 'spike')
            and number(signal.get('observedAt'))
            and 0 <= now - signal['observedAt'] < 90
            and signal.get('coverage', 0) >= 0.8
            and number(predicted.get('asOf'))
            and 0 <= now - predicted['asOf'] <= 7 * 86400
        )
        if model == current:
            reason = 'Currently serving.'
        elif not row.get('selected'):
            reason = 'Not selected for automatic switching.'
        elif not row.get('available'):
            reason = row.get('reason') or 'Model is not locally available and supported.'
        elif (
            signal.get('status')
            not in (
                ('spike', 'normal', 'learning', 'watching')
                if kind == 'explore'
                else ('spike', 'normal')
            )
            or not number(signal.get('observedAt'))
            or not 0 <= now - signal['observedAt'] < 90
            or signal.get('coverage', 0) < 0.8
        ):
            reason = 'Waiting for fresh, sustained network observations.'
        elif (
            not number(signal.get('load'))
            or signal['load'] < 1
            or not number(signal.get('pressure'))
            or signal['pressure'] <= 0
        ):
            reason = 'No measured demand with warm provider capacity.'
        elif failure:
            reason = 'Bloomkeeper is waiting 15 minutes after this model failed to load or switch. This is a local retry guard.'
        elif cooldown:
            reason = 'Bloomkeeper is briefly delaying a repeat of an unpaid trial; this is a local retry setting, not a network cooldown.'
        elif not budget or not all(number(budget.get(k)) for k in ('afterUnloadGB', 'requiredGB')):
            reason = 'Memory admission is not known.'
        elif (
            budget['afterUnloadGB'] + reclaimable(budget)
            < budget['requiredGB'] + rules['memoryHeadroomGB']
        ):
            memory_shortfall = (
                budget['requiredGB']
                + rules['memoryHeadroomGB']
                - budget['afterUnloadGB']
                - reclaimable(budget)
            )
            reason = (
                'Needs '
                + (
                    str(max(1, math.ceil(memory_shortfall * 1000))) + ' MB'
                    if memory_shortfall < 0.1
                    else '%.1f GB' % memory_shortfall
                )
                + ' more memory, including the automatic-switch safety margin.'
            )
            could_free = budget.get('cleanupCouldFreeGB')
            if number(could_free) and could_free >= memory_shortfall:
                reason += (
                    ' Turn on cache cleanup (Manual model controls → Enable cache cleanup) to let'
                    ' Bloomkeeper clear %.1f GB of file cache before switching.' % could_free
                )
        elif trial_running and not (paid_review and kind == 'earnings'):
            reason = 'Measuring the current trial before trying another model.'
        if (
            not reason
            and spike_result
            and not returning_spike
            and not (paid_review and kind == 'earnings')
        ):
            reason = spike_result['reason']
        if (
            not reason
            and spike_result
            and spike_result.get('ordinary')
            and not is_return
            and kind != 'earnings'
        ):
            reason = 'This trial review needs its supported incumbent or an independently qualified paid alternative.'
        if (
            not reason
            and high_hold
            and (kind == 'explore' or not (predicted or {}).get('forecastUsable'))
        ):
            reason = goal['highEarnings']['reason']
        guard_holds[model] = reason
        # No dollar estimate is manufactured for a trial, even if an old rate
        # exists. Ranking uses pressure; sparse old zeros cannot exclude it.
        if kind == 'explore':
            # A saved incumbent already has paid-work evidence. Returning needs
            # covered ongoing demand, not the pressure floor for a new trial.
            admission = signal.get('returnDemand', sustained) if is_return else sustained
            if not reason and not admission.get('qualified') and not learning_ready:
                reason = (
                    'The previous model needs fresh covered positive demand and warm capacity in both five-minute windows.'
                    if is_return
                    else learning['reason']
                    if (shortfall or collecting)
                    and rules.get('baselineLearningEnabled', 1)
                    and (row.get('paymentBaseline') or {}).get('needsSamples')
                    else 'A trial needs load/warm ≥0.5 in both of the last two five-minute windows, with sustained activity.'
                )
            if (
                not reason
                and (shortfall or exploring)
                and not is_return
                and not supported_return
                and not learning_ready
                and model == FALLBACK_MODEL
                and (row.get('fallbackEvidence') or {}).get('qualified')
                and row['fallbackEvidence']['usdPerHour'] <= pace_reference * 1.2
            ):
                reason = 'Recent GPT-OSS paid pace does not improve the current earnings shortfall.'
            if (
                not reason
                and (shortfall or exploring)
                and not is_return
                and not supported_return
                and not learning_ready
                and predicted
                and predicted.get('forecastUsable')
                and predicted['lower'] <= pace_reference * (1 + rules['improvementPercent'] / 100)
            ):
                reason = 'Matched paid history does not support an earnings improvement; network demand alone cannot justify this trial.'
        else:
            if not reason and (not predicted or not predicted.get('forecastUsable')):
                reason = (
                    (
                        'Exceptional network demand qualifies; waiting for fresh incumbent paid/activity readings and its minimum observation window.'
                        if exceptional['qualified']
                        else exceptional['reason']
                    )
                    if exceptional['loadRatio'] is not None
                    else 'Limited or unmatched paid history. Trials need an exceptional sustained spike, substantial paid shortfall or quiet period; no earnings forecast yet.'
                )
            if not reason and (
                not baseline
                or not (
                    baseline.get('forecastUsable')
                    or baseline.get('recent')
                    or number(baseline.get('recentGuardRate'))
                )
            ):
                reason = 'Waiting for recent measured earnings from the current model; a sparse old rate cannot establish its current value.'
            if not reason and number(predicted.get('asOf')) and now - predicted['asOf'] > 7 * 86400:
                reason = 'This model needs a recent paid-work observation; its last verified run is over seven days old.'
            if predicted and baseline and predicted.get('forecastUsable') and model != current:
                # Compare stay=old*H with switch=new*(H-loadTime). Returning is
                # optional, so reserve its recovery time but don't charge it as
                # a certain earnings loss. These are opportunity costs, no fees.
                cost, net, improvement, payback = (
                    value[k] for k in ('cost', 'net', 'improvement', 'payback')
                )
                if not reason and not value['qualified']:
                    reason = 'The conservative gain does not repay switching and the minimum improvement.'
        if not reason and spike_ready and len(recent_runs) + 2 > rules['maxSwitchesPerDay']:
            reason = 'A spike trial needs room for both its start and a possible return in the daily attempt limit.'
        if not reason and learning_ready and len(recent_runs) + 2 > rules['maxSwitchesPerDay']:
            reason = 'Baseline learning needs room for its start and a possible return in the daily attempt limit.'
        ordinary_start = (
            kind == 'explore' and not is_return and not spike_ready and not learning_ready
        )
        # A supported return is not a new speculative sample. Final selection
        # determines its durable purpose again in begin().
        sampling = kind == 'explore' and not is_return and not supported_return
        if (
            not reason
            and ordinary_start
            and not supported_return
            and not prospective['comparison']['qualified']
        ):
            reason = 'An ordinary comparative trial needs a qualified frozen incumbent paid benchmark; missing earnings are not zero.'
        if not reason and ordinary_start and not supported_return and retry['held']:
            reason = retry['reason']
        if not reason and sampling and not sample_budget['qualified']:
            reason = learning_time_reason(sample_budget)
        if (
            not reason
            and ordinary_start
            and not supported_return
            and len(recent_runs) + 2 > rules['maxSwitchesPerDay']
        ):
            reason = 'An ordinary trial needs room for its start and a possible return in the daily attempt limit.'
        if not reason and len(recent_runs) >= rules['maxSwitchesPerDay']:
            reason = 'The rolling 24-hour automatic-switch limit has been reached.'
        # A bounded trial needs room for entry, planned exit and a possible
        # failed-exit restoration. Return admission itself reserves two legs.
        reserved_seconds = (
            outbound
            + returning
            + (
                outbound
                if spike_ready or learning_ready or ordinary_start and not supported_return
                else 0
            )
        )
        if not reason and used_seconds + reserved_seconds > rules['maxDowntimeMinutes'] * 60:
            reason = 'This switch would exceed the rolling 24-hour downtime budget.'
        memory_hold = bool(
            reason
            and (
                reason.startswith('Needs ')
                and 'more memory' in reason
                or reason == 'Memory admission is not known.'
            )
        )
        economic_eligible = bool(
            kind == 'earnings'
            and paid_supported
            and activity.get('fresh')
            and row.get('available')
            and (not reason or memory_hold)
            and not failure
            and not cooldown
            and (not spike_result or returning_spike or paid_review)
            and (not trial_running or paid_review)
            and len(recent_runs) < rules['maxSwitchesPerDay']
            and used_seconds + reserved_seconds <= rules['maxDowntimeMinutes'] * 60
        )
        evidence_missing = bool(
            row.get('selected')
            and row.get('available')
            and model != current
            and (
                signal.get('status') == 'stale'
                or not number(signal.get('observedAt'))
                or not live_paid.get('fresh')
                or not number(goal.get('asOf'))
                or now - goal.get('asOf', 0) > 240
            )
            and (not value or value['qualified'])
        )
        opportunities.append(
            {
                'model': model,
                'selected': bool(row.get('selected')),
                'current': model == current,
                'learning': learning,
                'learningTrial': {
                    'incumbent': current,
                    'referenceRate': max(
                        goal.get('rate') or 0,
                        goal.get('fastRate') or 0,
                        live_paid.get('rate') or 0,
                        expected_current or 0,
                    ),
                    'sampleGoal': 50,
                    'jobMinutesGoal': 10,
                    'maxClockMinutes': 40,
                }
                if learning_ready
                else None,
                'signal': {
                    k: signal.get(k)
                    for k in (
                        'status',
                        'load',
                        'pressure',
                        'loadRatio',
                        'pressureRatio',
                        'observedAt',
                        'coverage',
                        'baselineScope',
                        'baselineHours',
                        'baselineLoad',
                        'baselinePressure',
                    )
                },
                'sustained': sustained,
                'returnDemand': signal.get('returnDemand') if is_return else None,
                'spikeContext': signal.get('spikeContext'),
                'kind': kind,
                'spike': exceptional,
                'regime': signal.get('regime'),
                'sampling': sampling,
                'ordinaryStart': ordinary_start and not supported_return,
                'supportedReturn': supported_return,
                'discoveryRetry': retry,
                'paidSupported': paid_supported,
                'confirmationEligible': economic_eligible,
                'confirmationMissing': evidence_missing,
                'spikeTrial': {
                    'incumbent': current,
                    'referenceRate': max(
                        goal.get('rate') or 0,
                        goal.get('fastRate') or 0,
                        live_paid.get('rate') or 0,
                        expected_current or 0,
                    ),
                }
                if spike_ready
                else None,
                'pressureScore': pressure_score,
                'contextTrials': context_trials,
                'conditional': matched,
                'historyWeight': weight,
                'estimate': predicted,
                'loadBudget': budget,
                'extraMemoryGB': rules['memoryHeadroomGB'],
                'outboundSeconds': outbound,
                'returnSeconds': returning,
                'costScope': cost_scope,
                'timingSamples': timing['samples'],
                'reservedSeconds': reserved_seconds,
                'switchCostUsd': cost,
                'netGainUsd': net,
                'paybackMinutes': payback,
                'improvementPercent': improvement,
                'eligible': reason is None,
                'reason': reason,
                'trialCooldownUntil': cooldown,
                'failureCooldownUntil': failure + FAILURE_RETRY_SECONDS if failure else None,
            }
        )
    qualified = [r for r in opportunities if r['eligible']]
    # Paid upgrades get priority over speculative experiments.
    best = max(
        qualified,
        key=lambda r: (
            r['kind'] == 'earnings',
            r['netGainUsd'] or 0,
            r['pressureScore'],
            r['model'],
        ),
        default=None,
    )
    fallback_row = next((r for r in opportunities if r['model'] == FALLBACK_MODEL), None)
    if fallback_row:
        fallback['eligible'] = bool(fallback['qualified'] and fallback_row['eligible'])
        fallback['holdReason'] = (
            (fallback_row['reason'] if exploring else guard_holds[FALLBACK_MODEL])
            if not fallback_row['current']
            else None
        )
        if fallback['qualified'] and fallback['holdReason']:
            fallback['status'] = 'held'
        fallback_row['fallbackPreferred'] = fallback['qualified']
    selection_reason = None
    preferred_row = next((r for r in opportunities if r['model'] == GEMMA), None)
    if preferred_row:
        preferred['eligible'] = bool(preferred['qualified'] and preferred_row['eligible'])
        if not preferred_row['eligible']:
            preferred['reason'] = (
                preferred_row['reason'] if exploring else guard_holds[GEMMA] or preferred['reason']
            )
    # Spread learning: among qualified models, measure the least recently
    # measured first (days since its last run or paid observation, capped).
    measured = {}
    for r in runs:
        if number(r.get('at')) and r['at'] <= now:
            measured[r.get('model')] = max(measured.get(r.get('model'), 0), r['at'])

    def staleness(row):
        last = max(measured.get(row['model'], 0), (row.get('estimate') or {}).get('asOf') or 0)
        return 8 if not last else min(7, int(max(0, now - last) // 86400))

    for row in opportunities:
        row['daysSinceMeasured'] = None if staleness(row) == 8 else staleness(row)
    if exploring and not returning_spike:
        alternatives = [r for r in qualified if r['model'] != FALLBACK_MODEL]
        reference = max(
            current_pace if shortfall else 0, fallback['usdPerHour'] if fallback['eligible'] else 0
        )
        return_ready = bool(
            preferred['eligible'] and preferred['rate'] > max(0.005, reference * 1.2)
        )
        # Gemma's repeated observations support a return trial, not a forecast.
        # A qualified higher earnings forecast can still beat that preference.
        stronger = [
            r
            for r in qualified
            if (r.get('estimate') or {}).get('forecastUsable')
            and (
                not number(r['estimate'].get('asOf'))
                or 0 <= now - r['estimate']['asOf'] <= 7 * 86400
            )
            and r['estimate']['lower']
            > max(0.005, reference * 1.2, preferred['rate'] * 1.2 if return_ready else 0)
        ]
        paid_exits = [r for r in qualified if r['kind'] == 'earnings']
        if paid_exits:
            best = max(
                paid_exits, key=lambda r: (r['netGainUsd'], r['estimate']['lower'], r['model'])
            )
        elif stronger:
            best = max(
                stronger, key=lambda r: (r['estimate']['lower'], r['pressureScore'], r['model'])
            )
        elif return_ready and (
            failed_trial
            or shortfall
            or len(recent_runs) >= rules['maxSwitchesPerDay'] - 1
            or len(alternatives) == 1
        ):
            best = preferred_row
            selection_reason = 'preferred_return'
            preferred['selection'] = selection_reason
        elif any(r.get('learningTrial') for r in qualified):
            best = max(
                (r for r in qualified if r.get('learningTrial')),
                key=lambda r: (
                    r['learning']['evidence']['priority'],
                    staleness(r),
                    r['pressureScore'],
                    r['model'],
                ),
            )
            selection_reason = 'baseline_learning'
        elif rules.get('fallbackEnabled', 1) and alternatives:
            best = max(alternatives, key=lambda r: (staleness(r), r['pressureScore'], r['model']))
        elif fallback['eligible']:
            best = fallback_row
            selection_reason = 'fallback'
            fallback['selection'] = selection_reason
    # Clear preference is explanatory only: an unqualified fallback remains an
    # ordinary trial candidate, subject to all the same eligibility checks.
    for row in opportunities:
        row['selectionReason'] = (
            selection_reason if selection_reason and row['model'] == best['model'] else None
        )
    opportunities.sort(
        key=lambda r: (
            r['current'],
            not r['eligible'],
            not r['selected'],
            -(r['pressureScore'] if exploring else (r['netGainUsd'] or 0)),
            r['model'],
        )
    )
    if returning_spike:
        incumbent = next((r for r in qualified if r['model'] == spike_result['incumbent']), None)
        # Prefer the benchmark return, but a blocked incumbent must not trap
        # the provider or hide another otherwise-qualified paid opportunity.
        best = incumbent or best
        if not best and (
            trial.get('complete')
            or number(spike_result.get('deadline'))
            and now >= spike_result['deadline']
        ):
            held = next((r for r in opportunities if r['model'] == spike_result['incumbent']), {})
            spike_result = {
                **spike_result,
                'status': 'keep',
                'reason': 'Trial ended. The previous model cannot currently return: '
                + (held.get('reason') or 'not available')
                + ' Resume ordinary opportunity checks; no earnings improvement is assumed.',
            }
            returning_spike = False
    spike_selected = bool(best and best.get('spikeTrial'))
    learning_selected = bool(
        best and selection_reason == 'baseline_learning' and best.get('learningTrial')
    )
    incumbent_selected = bool(
        returning_spike and best and best['model'] == spike_result['incumbent']
    )
    completed_trial_exit = bool(
        best
        and best['kind'] == 'earnings'
        and trial
        and trial.get('current')
        and trial.get('complete')
        and trial.get('settled')
        and activity.get('fresh')
        and live_paid.get('fresh')
    )
    if spike_result:
        reason = spike_result['reason']
        if returning_spike and not best:
            held_return = next(
                (r for r in opportunities if r['model'] == spike_result['incumbent']), {}
            )
            reason += ' Return held: ' + (
                held_return.get('reason') or 'Previous model is no longer selected or available.'
            )
        elif returning_spike and not incumbent_selected:
            reason += (
                ' The previous model cannot currently return; another qualified opportunity remains available: '
                + best['model']
                + '.'
            )
    elif spike_selected:
        reason = (
            'Exceptional demand justifies a bounded learning trial, even while the current model earns well. Compare %s warm minutes with the saved incumbent, then keep or return based on paid work.'
            % rules['trialMinutes']
        )
    elif learning_selected and collecting and not shortfall:
        reason = (
            'Learning boost: collect 50 complete paid token samples across 10 paid minutes, up to %s warm minutes, then compare with the previous model.'
            % rules['trialMinutes']
        )
    elif learning_selected:
        reason = (
            'Low earnings make room for a baseline-learning trial: collect 50 complete paid token samples across 10 paid minutes, up to %s warm minutes, then compare with the previous model.'
            % rules['trialMinutes']
        )
    elif best and stalled:
        reason = 'Work stopped abruptly and recovery steps did not bring it back. Trying another model now instead of waiting for the usual idle time.'
    elif selection_reason == 'preferred_return':
        reason = 'Gemma is the preferred return based on repeated paid history. Rechecking current demand and switch limits; earnings are not guaranteed.'
    elif selection_reason == 'fallback':
        reason = 'No other trial candidate qualifies. GPT-OSS is the last resort while recent local paid traffic and network demand remain supported.'
    elif best and best['kind'] == 'earnings':
        reason = 'A measured alternative clears the net-gain checks; confirming persistence before any switch.'
    elif best and shortfall:
        reason = (
            'Paid pace is below your $%.2f/hour protect level. Using learning time to measure another model; higher earnings are not assumed.'
            % goal.get('protectUsdPerHour', rules.get('protectUsdPerHour', 0.20))
        )
    elif best and exploring:
        reason = (
            'The current trial produced no paid work. Rechecking a sustained-demand alternative before switching.'
            if failed_trial
            else 'A sustained-demand trial is ready after %s verified warm idle minutes; historical earnings are uncertain.'
            % rules['idleEscapeMinutes']
            if can_escape
            else 'Scanning alternatives after 10 idle minutes. A trial can start at %s verified warm idle minutes.'
            % rules['idleEscapeMinutes']
        )
    elif best:
        reason = 'A measured alternative clears the net-gain checks; confirming persistence before any switch.'
    elif high_hold:
        reason = goal['highEarnings']['reason']
    elif trial_running:
        reason = (
            'Measuring the current trial for %s warm minutes. Cold time and collection gaps do not count.'
            % trial['trialMinutes']
        )
    elif shortfall or exploring:
        held = max(
            (r for r in opportunities if r['selected'] and not r['current']),
            key=lambda r: r['signal'].get('pressure') or 0,
            default=None,
        )
        reason = (
            'Paid pace is below your protect level. '
            if shortfall
            else 'Learning boost is on. '
            if collecting
            else 'The current model is quiet. '
        )
        # Out of learning time, no new trial can start whatever the models show.
        reason += (
            learning_time_reason(sample_budget)
            if not sample_budget['qualified']
            else 'Highest-demand selected alternative, %s: %s' % (held['model'], held['reason'])
            if held
            else 'No alternative model is selected.'
        )
    else:
        reason = (
            goal['reason']
            if goal.get('status') == 'productive'
            else 'Keeping the current model. Learning runs start when paid pace stays below your protect level or the model goes quiet.'
        )
    paid_row = max(
        (r for r in opportunities if r['paidSupported']),
        key=lambda r: (r['eligible'], r['netGainUsd'] or 0, r['model']),
        default=None,
    )
    paid_alternative = (
        {
            'model': paid_row['model'],
            'eligible': paid_row['eligible'],
            'at': now,
            'reason': paid_row['reason']
            or 'Qualified paid evidence; awaiting confirmation and final safety checks.',
        }
        if paid_row
        else None
    )
    return {
        'at': now,
        'paidAlternative': paid_alternative,
        'currentModel': current,
        'baseline': baseline,
        'expectedCurrentRate': expected_current,
        'target': best['model'] if best else None,
        'kind': best['kind'] if best else None,
        'escapeReady': bool(
            best
            and best['kind'] == 'explore'
            and (exploring and can_escape or spike_selected or incumbent_selected)
        ),
        'completedTrialExit': completed_trial_exit,
        'stallEscape': stalled,
        'reason': reason,
        'activity': activity,
        'trial': trial,
        'fallback': fallback,
        'economicRevision': ECONOMIC_REVISION,
        'earningsTarget': goal,
        'preferredReturn': preferred,
        'spikeReview': spike_result,
        'dataGathering': gathering,
        'baselineLearning': {
            'enabled': bool(rules.get('baselineLearningEnabled', 1)),
            'trialsUsed': sum(
                now - 86400 < r['at'] <= now
                and (r.get('decision') or {}).get('explorationTrigger') == 'baseline_learning'
                for r in runs
            ),
        },
        'explorationTrigger': None
        if best and best['kind'] == 'earnings'
        else (
            'ordinary_return'
            if spike_result.get('ordinary')
            else 'baseline_return'
            if spike_result.get('learning')
            else 'spike_return'
        )
        if incumbent_selected
        else 'trial_alternative'
        if returning_spike and best
        else 'baseline_learning'
        if learning_selected
        else 'demand_spike'
        if spike_selected
        else 'earnings_target'
        if shortfall
        else 'failed_trial'
        if failed_trial
        else 'idle'
        if exploring
        else None,
        'opportunities': opportunities,
        'limits': limits,
        'policy': dict(rules),
        'planningMinutes': rules['planningMinutes'],
        'lookbackDays': 30,
        'assumption': 'Demand trials discover local paid work; pressure is not an earnings forecast.',
    }


class DemandOptimizer:
    def __init__(self, history, store):
        self.h, self.store = history, store
        self.alerts = DemandAlerts(history, store)
        self.trials = DemandTrials(history, store)
        self.lock = threading.RLock()
        self.cached = None
        self.cache_key = None
        self.cache_at = 0
        self.learning_cache = {}
        self.learning_key = None
        with self.h.lock:
            self.h.db.executescript("""CREATE TABLE IF NOT EXISTS demand_switch_runs(
                id INTEGER PRIMARY KEY,account TEXT,device TEXT,at REAL,previous TEXT,model TEXT,
                payload TEXT,reserved_seconds REAL,completed_at REAL,result TEXT,downtime REAL);
                CREATE INDEX IF NOT EXISTS demand_switch_scope ON demand_switch_runs(account,device,at);""")
            self.h.db.commit()

    def evidence(self, account, device, models, now):
        key = (account, device, tuple(sorted(models)), int(now // 60))
        with self.lock:
            if self.cached is not None and key == self.cache_key and 0 <= now - self.cache_at < 60:
                return self.cached
            with read_view(self.store) as view:
                earned = view.evidence(account, device, now - LOOKBACK, now, now)
                network = network_minutes(view, models, now - LOOKBACK, now - 120)
            self.cached = earned, network
            self.cache_key = key
            self.cache_at = now
            return self.cached

    def runs(self, account, device, now, limit=20):
        with self.h.lock:
            rows = self.h.db.execute(
                """SELECT id,at,previous,model,payload,reserved_seconds,completed_at,result,downtime
                FROM demand_switch_runs WHERE account=? AND device=? AND at<=? ORDER BY at DESC LIMIT ?""",
                (account, device, now, limit),
            ).fetchall()
        return [
            {
                'id': r['id'],
                'at': r['at'],
                'previousModel': r['previous'],
                'model': r['model'],
                'decision': json.loads(r['payload']),
                'reservedSeconds': r['reserved_seconds'],
                'completedAt': r['completed_at'],
                'result': r['result'],
                'downtime': r['downtime'],
            }
            for r in rows
        ]

    def evaluate(
        self,
        account,
        device,
        rows,
        current,
        raw,
        rules,
        now,
        last_switch=0,
        live=None,
        admission_activity=None,
        gathering=None,
        stall_escape=False,
    ):
        models = sorted({r['id'] for r in rows} | ({current} if current else set()))
        activity = admission_activity or self.trials.activity(account, device, raw, now)
        earned, network = self.evidence(account, device, models, now)
        learning_key = (account, device, tuple(models), int(now // 60))
        with self.lock:
            if self.learning_key != learning_key:
                with read_view(self.store) as view:
                    self.learning_cache = baselines(view, account, device, models, now)
                self.learning_key = learning_key
            payment_baselines = self.learning_cache
        scan = self.alerts.current(account, device, now)
        signals = {r['model']: r for r in scan['models']}
        conditions = {
            m: conditional(earned.get(m, {}), network.get(m, {}), signals.get(m, {}), now)
            for m in models
        }
        estimates = {
            m: estimate(
                earned.get(m, {}),
                network.get(m, {}),
                signals.get(m, {}),
                now,
                raw.get('started_at') if m == current else None,
                conditions[m],
            )
            for m in models
        }
        with self.h.lock:
            events = [
                dict(r)
                for r in self.h.db.execute(
                    """SELECT at,kind,model,downtime FROM opt_events
                WHERE account=? AND device=? AND at>=? AND at<=? ORDER BY at""",
                    (account, device, now - LOOKBACK, now),
                )
            ]
            samples = [
                dict(r)
                for r in self.h.db.execute(
                    'SELECT model,at,active,queued,warm FROM opt_network WHERE at>=? AND at<=?',
                    (now - 2430, now),
                )
            ]
        for model in models:
            signals.setdefault(model, {})['regime'] = {
                'providerVersion': raw.get('version'),
                'model': model,
            }
            signals[model]['sustained'] = sustained_demand(
                [r for r in samples if r['model'] == model], now
            )
            # A learning run only teaches something when this Mac would get real
            # work: 10+ requests on the network and ~0.3 per warm provider.
            signals[model]['learningDemand'] = sustained_demand(
                [r for r in samples if r['model'] == model],
                now,
                LEARNING_PRESSURE,
                LEARNING_MIN_LOAD,
                LEARNING_PRESSURE / 2,
            )
            signals[model]['returnDemand'] = sustained_demand(
                [r for r in samples if r['model'] == model], now, 0, 1, 1e-12
            )
            signals[model]['spikeContext'] = demand_context(
                [r for r in samples if r['model'] == model], now
            )
        runs = self.runs(account, device, now, 100)
        latest = next(
            (
                r
                for r in runs
                if r['decision'].get('kind') == 'explore' and r.get('result') == 'switched'
            ),
            None,
        )
        trial = self.trials.outcome(account, device, latest, raw, now) if latest else None
        gemma_return = return_evidence(estimate(earned.get(GEMMA, {}), {}, {}, now), now)
        live = live or {}
        paid = fresh_paid(
            live.get('pulse'), (live.get('provider') or {}).get('session'), raw, current, now
        )
        enriched = [
            {
                **r,
                'evidence': earned.get(r['id'], {}),
                'signal': signals.get(r['id'], {}),
                'paymentBaseline': payment_baselines.get(r['id']),
                'conditional': conditions.get(r['id']),
                'earningsTarget': target_status(
                    earned.get(r['id'], {}), raw.get('started_at'), now, rules, last_switch, paid
                )
                if r['id'] == current
                else None,
                'returnEvidence': gemma_return if r['id'] == GEMMA else None,
                'fallbackEvidence': fallback_reliability(earned.get(r['id'], {}), now)
                if r['id'] == FALLBACK_MODEL
                else None,
            }
            for r in rows
        ]
        result = decide(
            enriched,
            current,
            estimates,
            events,
            runs,
            rules,
            now,
            last_switch,
            activity,
            trial,
            gathering,
            stall_escape,
        )
        target = result['target']
        result['sourceAt'] = (
            (
                signals[target]['sustained']['sourceAt']
                if result.get('kind') == 'explore'
                else signals[target].get('observedAt')
            )
            if target
            else None
        )
        result['scanAt'] = scan['at']
        result['scanStatus'] = scan['status']
        return result

    def begin(self, account, device, decision, now):
        target = decision.get('target')
        chosen = next(
            (r for r in decision['opportunities'] if r['model'] == target and r['eligible']), None
        )
        if not chosen or not 0 <= now - decision['at'] <= 30:
            raise ValueError('The demand opportunity expired before switching.')
        if decision.get('kind') == 'explore' and not decision.get('escapeReady'):
            raise ValueError('The verified idle or trial evaluation window is not complete.')
        payload = {
            'baseline': decision['baseline'],
            'candidate': chosen,
            'planningMinutes': decision['planningMinutes'],
            'reason': decision.get('reason'),
            'explorationTrigger': decision.get('explorationTrigger'),
            'earningsTarget': decision.get('earningsTarget'),
            'activity': decision.get('activity'),
            'spikeTrial': chosen.get('spikeTrial'),
            'spikeReview': decision.get('spikeReview'),
            'learningTrial': chosen.get('learningTrial')
            if decision.get('explorationTrigger') == 'baseline_learning'
            else None,
            'kind': decision.get('kind', 'earnings'),
            'trialMinutes': decision['policy']['trialMinutes'],
            'activeAtDispatch': decision.get('activeAtDispatch'),
            'workAdvancedDuringPreflight': decision.get('workAdvancedDuringPreflight'),
            'completedTrialExit': decision.get('completedTrialExit'),
            'trialCooldownMinutes': decision['policy']['trialCooldownMinutes'],
        }
        if (
            payload['kind'] == 'explore'
            and decision.get('explorationTrigger') not in RETURN_TRIGGERS
            and not chosen.get('supportedReturn')
        ):
            anchor = comparison(
                decision['currentModel'],
                decision.get('earningsTarget') or {},
                decision.get('baseline'),
                now,
                chosen.get('regime'),
                decision.get('explorationTrigger'),
                payload['trialMinutes'],
                decision.get('expectedCurrentRate'),
            )
            anchor['sampling'] = bool(chosen.get('sampling'))
            payload['economicTrial'] = anchor
            if not payload.get('spikeTrial') and not payload.get('learningTrial'):
                if not anchor['comparison']['qualified']:
                    raise ValueError('The ordinary trial has no qualified frozen paid comparator.')
                payload['ordinaryTrial'] = anchor
        saved_gathering = decision.get('dataGathering') or {}
        gathering = {
            'active': bool(
                saved_gathering.get('active')
                and number(saved_gathering.get('endsAt'))
                and now < saved_gathering['endsAt']
            )
        }
        if saved_gathering.get('active') and not gathering['active']:
            raise ValueError(
                'Learning boost ended before the switch; normal trial limits apply again.'
            )
        with self.h.lock:
            rules = policy(decision.get('policy'))
            allowance = learning_limits(gathering, rules)
            recent = self.h.db.execute(
                """SELECT COUNT(*) AS n,SUM(COALESCE(downtime,reserved_seconds,0)) AS seconds,
                SUM(CASE WHEN result='starting' THEN 1 ELSE 0 END) AS pending
                FROM demand_switch_runs WHERE account=? AND device=? AND at>? AND at<=?""",
                (account, device, now - 86400, now),
            ).fetchone()
            # Recompute from the verified decision legs, not an independently
            # supplied total. Include the planned exit and its failure recovery.
            bounded_trial = bool(payload.get('economicTrial')) or decision.get(
                'explorationTrigger'
            ) in ('demand_spike', 'baseline_learning')
            reserved = (
                chosen['outboundSeconds']
                + chosen['returnSeconds']
                + (chosen['outboundSeconds'] if bounded_trial else 0)
            )
            if (
                recent['pending']
                or recent['n'] >= rules['maxSwitchesPerDay']
                or (recent['seconds'] or 0) + reserved > rules['maxDowntimeMinutes'] * 60
            ):
                raise ValueError('The automatic switch or downtime budget changed before restart.')
            if (payload.get('economicTrial') or {}).get('sampling'):
                attempts = self.runs(account, device, now, 100)
                if (
                    payload.get('ordinaryTrial')
                    and discovery_retry(
                        attempts,
                        target,
                        {
                            **(chosen.get('signal') or {}),
                            'sustained': chosen.get('sustained'),
                            'regime': chosen.get('regime'),
                        },
                        now,
                    )['held']
                ):
                    raise ValueError(
                        'Recent trial evidence requires waiting before repeating this ordinary sample.'
                    )
                if (
                    not sampling_budget(
                        attempts, now, reserve=True, limit_minutes=allowance['samplingMinutes']
                    )['qualified']
                    or recent['n'] + 2 > rules['maxSwitchesPerDay']
                ):
                    raise ValueError(
                        'The elapsed sampling allowance or reserved return attempt changed before restart.'
                    )
            if decision.get('explorationTrigger') == 'demand_spike':
                attempts = self.runs(account, device, now, 100)
                spike_count = sum(
                    now - 86400 < r['at'] <= now
                    and r['decision'].get('explorationTrigger') == 'demand_spike'
                    for r in attempts
                )
                if (
                    spike_count >= MAX_TRIALS_PER_DAY
                    or recent['n'] + 2 > rules['maxSwitchesPerDay']
                    or not chosen.get('spikeTrial')
                ):
                    raise ValueError(
                        'The exceptional-trial allowance or reserved return attempt changed before restart.'
                    )
            if decision.get('explorationTrigger') == 'baseline_learning':
                attempts = self.runs(account, device, now, 100)
                learning = [
                    r
                    for r in attempts
                    if now - 86400 < r['at'] <= now
                    and r['decision'].get('explorationTrigger') == 'baseline_learning'
                ]
                if (
                    any(
                        r['model'] == target and now - allowance['repeatSeconds'] < r['at']
                        for r in learning
                    )
                    or recent['n'] + 2 > rules['maxSwitchesPerDay']
                    or not chosen.get('learningTrial')
                ):
                    raise ValueError(
                        'The baseline-learning allowance or reserved return attempt changed before restart.'
                    )
            result = self.h.db.execute(
                """INSERT INTO demand_switch_runs
                (account,device,at,previous,model,payload,reserved_seconds,result)
                VALUES(?,?,?,?,?,?,?,?)""",
                (
                    account,
                    device,
                    now,
                    decision['currentModel'],
                    target,
                    json.dumps(payload, allow_nan=False),
                    reserved,
                    'starting',
                ),
            )
            self.h.db.commit()
            return result.lastrowid

    def begin_recovery(self, account, device, decision, now):
        """A same-model restart after work stopped. Counts toward the daily switch and downtime budgets."""
        target = decision.get('target')
        if not target or decision.get('kind') != 'recovery' or not 0 <= now - decision['at'] <= 30:
            raise ValueError('The stall restart expired before starting.')
        rules = policy(decision.get('policy'))
        payload = {
            'kind': 'recovery',
            'reason': decision.get('reason'),
            'stall': decision.get('stall'),
            'activeAtDispatch': decision.get('activeAtDispatch'),
            'workAdvancedDuringPreflight': decision.get('workAdvancedDuringPreflight'),
        }
        with self.h.lock:
            recent = self.h.db.execute(
                """SELECT COUNT(*) AS n,SUM(COALESCE(downtime,reserved_seconds,0)) AS seconds,
                SUM(CASE WHEN result='starting' THEN 1 ELSE 0 END) AS pending
                FROM demand_switch_runs WHERE account=? AND device=? AND at>? AND at<=?""",
                (account, device, now - 86400, now),
            ).fetchone()
            if (
                recent['pending']
                or recent['n'] >= rules['maxSwitchesPerDay']
                or (recent['seconds'] or 0) + RECOVERY_RESERVED_SECONDS
                > rules['maxDowntimeMinutes'] * 60
            ):
                raise ValueError('The daily switch or downtime budget does not allow a restart.')
            result = self.h.db.execute(
                """INSERT INTO demand_switch_runs
                (account,device,at,previous,model,payload,reserved_seconds,result)
                VALUES(?,?,?,?,?,?,?,?)""",
                (
                    account,
                    device,
                    now,
                    target,
                    target,
                    json.dumps(payload, allow_nan=False),
                    RECOVERY_RESERVED_SECONDS,
                    'starting',
                ),
            )
            self.h.db.commit()
            return result.lastrowid

    def record_spike_review(self, account, device, result):
        # Background decisions only. GET previews never commit an experiment.
        if not result or (result.get('status') != 'keep' and not result.get('ordinary')):
            return
        with self.h.lock:
            row = self.h.db.execute(
                "SELECT payload FROM demand_switch_runs WHERE id=? AND account=? AND device=? AND result='switched'",
                (result['runId'], account, device),
            ).fetchone()
            if not row:
                return
            payload = json.loads(row['payload'])
            if not (
                payload.get('spikeTrial')
                or payload.get('learningTrial')
                or payload.get('ordinaryTrial')
            ):
                return
            if payload.get('trialResolution') or payload.get('spikeResolution'):
                return
            if result.get('ordinary'):
                previous = payload.get('trialReview') or {}
                if (
                    previous.get('status') == result.get('status')
                    and previous.get('reason') == result.get('reason')
                    and 0 <= result['at'] - previous.get('at', 0) < 60
                ):
                    return
                payload['trialReview'] = result
            if result.get('status') in ('keep', 'inconclusive'):
                payload['trialResolution' if result.get('ordinary') else 'spikeResolution'] = result
            self.h.db.execute(
                'UPDATE demand_switch_runs SET payload=? WHERE id=? AND account=? AND device=?',
                (json.dumps(payload, allow_nan=False), result['runId'], account, device),
            )
            self.h.db.commit()

    def finish(self, run_id, account, device, now, result, downtime, session=None):
        if run_id is None:
            return
        with self.h.lock:
            row = self.h.db.execute(
                'SELECT payload FROM demand_switch_runs WHERE id=? AND account=? AND device=? AND completed_at IS NULL',
                (run_id, account, device),
            ).fetchone()
            if row and session and result == 'switched':
                payload = {**json.loads(row['payload']), 'providerSession': session}
                prior = payload.get('spikeReview') or {}
                if prior.get('runId') and prior.get('status') in ('return', 'waiting'):
                    previous = self.h.db.execute(
                        'SELECT payload FROM demand_switch_runs WHERE id=? AND account=? AND device=?',
                        (prior['runId'], account, device),
                    ).fetchone()
                    if previous:
                        old = json.loads(previous['payload'])
                        if not old.get('trialResolution') and not old.get('spikeResolution'):
                            old['trialResolution'] = {
                                **prior,
                                'status': 'returned'
                                if prior.get('incumbent') == payload['candidate']['model']
                                else 'paid_alternative',
                                'at': now,
                                'nextRunId': run_id,
                            }
                            self.h.db.execute(
                                'UPDATE demand_switch_runs SET payload=? WHERE id=? AND account=? AND device=?',
                                (json.dumps(old, allow_nan=False), prior['runId'], account, device),
                            )
                self.h.db.execute(
                    'UPDATE demand_switch_runs SET payload=? WHERE id=? AND account=? AND device=?',
                    (json.dumps(payload, allow_nan=False), run_id, account, device),
                )
            self.h.db.execute(
                """UPDATE demand_switch_runs SET completed_at=?,result=?,downtime=?
                WHERE id=? AND account=? AND device=? AND completed_at IS NULL""",
                (now, result, max(0, downtime), run_id, account, device),
            )
            self.h.db.commit()

    def interrupt_pending(self, account, device, now):
        # The old process cannot verify the outcome after reopening. Retain its
        # reserved downtime in the daily budget; never invent a successful run.
        with self.h.lock:
            result = self.h.db.execute(
                """UPDATE demand_switch_runs SET completed_at=?,result='interrupted'
                WHERE account=? AND device=? AND completed_at IS NULL""",
                (now, account, device),
            )
            self.h.db.commit()
            return result.rowcount
