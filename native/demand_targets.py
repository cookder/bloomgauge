"""Sustained earnings shortfalls and the user's preferred return model.

Inputs are device-matched, covered, settled warm minutes from OptimizerStore.
The target is an aspiration, never an earnings forecast or a busy-switch waiver.
"""

import math
from model_combinations import selection_key

GEMMA = 'gemma-4-26b-qat-4bit'
# The earnings target is a report-only goal. Decisions use the protect level:
# above it BloomGauge never interrupts a model to learn; below it, learning time may.
DEFAULT_TARGET = 0.12
HIGH_EARNINGS_USD = 0.20


def chosen_goal(rules):
    """The earnings goal the user picked, or None.

    DEFAULT_TARGET only fills the policy slot; it is not a goal anyone chose, and
    Macs differ too much for one number to judge them all. The policy has no
    "no goal" value yet, so a saved $0.12 also reads as no goal.
    """
    target = (rules or {}).get('targetUsdPerHour')
    return (
        target
        if finite(target) and target > 0 and abs(target - DEFAULT_TARGET) > 1e-9
        else None
    )


def finite(value):
    return type(value) in (float, int) and math.isfinite(value)


def number(value):
    return finite(value) and value >= 0


def fresh_paid(pulse, session, raw, current, now):
    """Read the same confirmed 5m credits as Pulse, with current-session proof.

    Never treat a short warm burst, a stale source, or another session as a
    five-minute earnings recovery. This value is not a candidate forecast.
    """
    pulse, session, raw = pulse or {}, session or {}, raw or {}
    window = (pulse.get('windows') or {}).get('300') or {}
    result = {'fresh': False, 'rate': None, 'seconds': 0, 'asOf': None}
    if (
        pulse.get('status') != 'live'
        or session.get('status') != 'active'
        or (session.get('performance') or {}).get('status') != 'counting'
        or type(session.get('id')) is not int
        or pulse.get('sessionId') != session['id']
        or not current
        or selection_key(pulse.get('models')) != current
        or selection_key(session.get('models')) != current
        or selection_key(raw.get('advertised_models')) != current
        or not number(raw.get('started_at'))
        or session.get('providerStartedAt') != raw['started_at']
    ):
        return result
    for value, age in (
        (pulse.get('at'), 15),
        (pulse.get('updatedAt'), 45),
        (session.get('lastSeenAt'), 15),
        (raw.get('written_at'), 15),
        (window.get('end'), 60),
    ):
        if not number(value) or not 0 <= now - value <= age:
            return result
    if (
        not finite(window.get('ratePerHour'))
        or not all(number(window.get(k)) for k in ('seconds', 'start', 'end'))
        or not number(session.get('startedAt'))
        or window['start'] < session['startedAt']
        or not 240 <= window['seconds'] <= window['end'] - window['start'] + 0.001
        or window['end'] - window['start'] > 300.001
    ):
        return result
    return {
        'fresh': True,
        'rate': window['ratePerHour'],
        'seconds': window['seconds'],
        'asOf': window['end'],
    }


def high_earnings(evidence, since, now, live_paid=None, threshold=HIGH_EARNINGS_USD):
    """Three covered 5m windows in this session, with a current paid check.

    Full settled warm minutes only; missing time and base rewards never become
    earnings. A missing live tail cannot revoke an otherwise supported hold.
    """
    result = {
        'active': False,
        'threshold': threshold,
        'windowMinutes': 15,
        'coveredMinutes': 0,
        'rates': [],
        'reason': 'Learning whether paid earnings are consistently at least $%.2f/hour.'
        % threshold,
    }
    if not number(since) or since > now:
        return result
    end = int((now - 120) // 60) * 60
    minutes = {
        m['at']: m
        for m in evidence.get('minutes', [])
        if all(number(m.get(k)) for k in ('at', 'seconds'))
        and finite(m.get('usd'))
        and 59.999999 <= m['seconds'] <= 60.000001
        and m['at'] % 60 == 0
        and max(since, end - 900) <= m['at']
        and m['at'] + 60 <= end
    }
    for start in (end - 900, end - 600, end - 300):
        rows = [m for at, m in minutes.items() if start <= at < start + 300]
        seconds = sum(m['seconds'] for m in rows)
        result['coveredMinutes'] += seconds / 60
        result['rates'].append(
            sum(m['usd'] for m in rows) * 3600 / seconds if seconds >= 240 else None
        )
    sustained = all(rate is not None and rate >= threshold - 1e-9 for rate in result['rates'])
    paid = live_paid or {}
    current = paid.get('fresh') and finite(paid.get('rate'))
    result['active'] = bool(sustained and (not current or paid['rate'] >= threshold - 1e-9))
    if result['active']:
        result['reason'] = (
            'Earning at least your $%.2f/hour protect level. Not interrupting it to learn; confident paid upgrades remain available.'
            % threshold
            if current
            else 'Recent covered earnings are above your $%.2f/hour protect level. Waiting for a fresh paid reading before allowing a learning run.'
            % threshold
        )
    return result


def target_status(evidence, since, now, rules, last_switch=0, live_paid=None):
    target = rules.get('targetUsdPerHour', DEFAULT_TARGET)
    protect = rules.get('protectUsdPerHour', HIGH_EARNINGS_USD)
    result = {
        'usdPerHour': target,
        'dailyUsd': target * 24,
        'protectUsdPerHour': protect,
        'rate': None,
        'fastRate': None,
        'warmMinutes': 0,
        'asOf': None,
        'ready': False,
        'belowTarget': False,
        'status': 'learning',
        'nextCheckAt': None,
        'productiveFloor': protect,
        'livePaid': live_paid,
        'highEarnings': high_earnings(evidence, since, now, live_paid, protect),
        'reason': 'Learning this session: need 30 covered, settled warm minutes before a learning run.',
    }
    if not number(since) or since > now:
        return result
    minutes = {}
    for m in evidence.get('minutes', []):
        # Signed corrections belong to the covered minute, in both the paid
        # total and its denominator. They are not gaps or malformed readings.
        if (
            all(number(m.get(k)) for k in ('at', 'seconds'))
            and finite(m.get('usd'))
            and 59.999999 <= m['seconds'] <= 60.000001
            and max(since, now - 2520) <= m['at']
            and m['at'] + 60 <= now - 120
        ):
            minutes[m['at']] = m  # repeated reads cannot create extra time
    groups = {}
    for m in minutes.values():
        key = int(m['at'] // 300) * 300
        b = groups.setdefault(key, {'at': key, 'end': 0, 'seconds': 0, 'usd': 0})
        b['seconds'] += m['seconds']
        b['usd'] += m['usd']
        b['end'] = max(b['end'], m['at'] + 60)
    blocks = [b for _, b in sorted(groups.items()) if 0 < b['seconds'] <= 300.000001][-9:]
    seconds = sum(m['seconds'] for m in minutes.values())
    span = min(2400, max(0, now - 120 - since))
    result.update(
        warmMinutes=seconds / 60,
        rate=sum(m['usd'] for m in minutes.values()) * 3600 / seconds if seconds else None,
        coveragePercent=min(100, seconds * 100 / span) if span else 0,
        asOf=max((m['at'] + 60 for m in minutes.values()), default=None),
    )
    fast_end = int((now - 120) // 60) * 60
    fast = [m for m in minutes.values() if fast_end - 300 <= m['at'] and m['at'] + 60 <= fast_end]
    fast_seconds = sum(m['seconds'] for m in fast)
    if 240 <= fast_seconds <= 300.000001:
        result['fastRate'] = sum(m['usd'] for m in fast) * 3600 / fast_seconds
    if seconds < 1800 or result['coveragePercent'] < 75:
        return result
    if now - result['asOf'] > 240 or result['fastRate'] is None:
        result.update(
            status='stale',
            reason='Waiting for fresh covered earnings; missing readings do not mean zero income.',
        )
        return result
    # Covered time, including warm zeros, supplies the denominator. A gap does
    # not erase earlier observations or become zero income. Require a sustained
    # low aggregate plus a majority of observed blocks and a fresh 5m check.
    substantial = [b for b in blocks if b['seconds'] >= 180]
    below = bool(
        result['rate'] < protect
        and len(substantial) >= 6
        and sum(b['usd'] * 3600 / b['seconds'] < protect for b in substantial)
        >= 0.75 * len(substantial)
    )
    result['belowTarget'] = below
    current_rate = max(
        result['rate'],
        result['fastRate'],
        live_paid['rate'] if live_paid and live_paid.get('fresh') else 0,
    )
    if current_rate >= protect:
        result.update(
            status='productive',
            reason='Recent paid pace is at or above your $%.2f/hour protect level; not interrupting it to learn. Confident paid upgrades remain available.'
            % protect,
        )
    elif not below:
        result.update(
            status='watching',
            reason='Paid pace varies around your protect level. Waiting until it stays below before a learning run.',
        )
    elif live_paid is not None and not live_paid.get('fresh'):
        result.update(
            status='stale',
            reason='Waiting for a fresh five-minute confirmed earnings check before a shortfall trial; missing readings are not zero pay.',
        )
    else:
        due = max(since, last_switch) + rules['minRunMinutes'] * 60
        result.update(
            ready=now >= due,
            status='below_target',
            nextCheckAt=due,
            reason='Paid pace is below your protect level. A learning run can measure another model; higher earnings are not assumed.'
            if now >= due
            else 'Paid pace is below your protect level; finishing the minimum run before a learning run.',
        )
    return result


def return_evidence(observed, now):
    observed = observed or {}
    qualified = bool(
        observed.get('established')
        and number(observed.get('asOf'))
        and 0 <= now - observed['asOf'] <= 7 * 86400
        and number(observed.get('rate'))
        and observed['rate'] > 0
    )
    return {
        'model': GEMMA,
        'qualified': qualified,
        'rate': observed.get('rate'),
        'hours': observed.get('evidenceHours', 0),
        'scope': observed.get('scope', 'all_observed_hours'),
        'asOf': observed.get('asOf'),
        'eligible': False,
        'selection': None,
        'reason': 'User-preferred return with repeated paid history; current demand and switch checks must still qualify.'
        if qualified
        else 'Gemma is the preferred return, but recent repeated paid evidence is still required.',
    }
