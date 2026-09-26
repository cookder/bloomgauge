"""Time-boxed learning boost: more learning time early in a Mac's history.

Only learning allowances change. Memory, temperature, power, idle, failed-load,
demand, confirmation and high-earnings protections are unchanged, and the
mode never starts demand following on its own.
"""

import math

import baseline_learning

DURATIONS = (86400, 3 * 86400, 7 * 86400)
# Data gathering boosts learning time to at least this many minutes a day.
SAMPLING_MINUTES = 180
# Both values are within demand_optimizer.RANGES, so a relaxed policy still validates.
MIN_SWITCHES_PER_DAY = 24
MIN_DOWNTIME_MINUTES = 60


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def status(saved, now):
    """Active only inside a valid saved window; anything malformed is off."""
    if not isinstance(saved, dict):
        return {'active': False}
    started, ends, seconds = saved.get('startedAt'), saved.get('endsAt'), saved.get('seconds')
    if not (
        finite(started) and finite(ends) and seconds in DURATIONS and ends == started + seconds
    ):
        return {'active': False}
    active = finite(now) and started <= now < ends
    return {
        'active': active,
        'startedAt': started,
        'endsAt': ends,
        'seconds': seconds,
        'remainingSeconds': ends - now if active else 0,
    }


def start(seconds, now):
    if type(seconds) is not int or seconds not in DURATIONS:
        raise ValueError('Choose 24 hours, 3 days or 7 days of learning boost.')
    return {'startedAt': now, 'endsAt': now + seconds, 'seconds': seconds}


def relaxed(rules):
    return {
        **rules,
        'maxSwitchesPerDay': max(rules['maxSwitchesPerDay'], MIN_SWITCHES_PER_DAY),
        'maxDowntimeMinutes': max(rules['maxDowntimeMinutes'], MIN_DOWNTIME_MINUTES),
        'baselineLearningEnabled': 1,
    }


def learning_limits(gathering, rules=None):
    """Learning time is the limit; the boost raises it to at least 3 hours a day."""
    minutes = (rules or {}).get('learningMinutesPerDay', 60)
    if gathering and gathering.get('active'):
        minutes = max(minutes, SAMPLING_MINUTES)
    return {'repeatSeconds': baseline_learning.REPEAT_SECONDS, 'samplingMinutes': minutes}
