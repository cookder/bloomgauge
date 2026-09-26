"""Qualified economic confirmation, independent of transient memory admission.

Only background control ticks mutate proposals. Missing intervals never earn time.
"""

import copy
import math
import json
import hashlib
from model_readiness import session_key

REVISION = 'economic-confirmation-v2'
MAX_GAP_SECONDS = 150
CONTINUITY_SECONDS = 90


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def scope(state, raw, intent=None):
    values = {
        'session': session_key(raw),
        'intent': intent,
        **{
            k: state.get(k)
            for k in (
                'account',
                'device',
                'mode',
                'models',
                'demandPolicy',
                'startedAt',
                'expectedModel',
            )
        },
    }
    return hashlib.sha256(json.dumps(values, sort_keys=True, allow_nan=False).encode()).hexdigest()


def view(previous, session, now):
    if not previous or previous.get('revision') != REVISION:
        return copy.deepcopy(previous) if previous else None
    if (
        previous.get('session') != session
        or not finite(previous.get('expiresAt'))
        or now > previous['expiresAt']
        or not finite(previous.get('checkedAt'))
        or not 0 <= now - previous['checkedAt'] <= MAX_GAP_SECONDS
    ):
        return None
    if not finite(previous.get('sourceAt')) or previous['sourceAt'] > now:
        return None
    if now - previous['sourceAt'] >= 90:
        return pause(
            previous, session, now, 'Waiting for fresh evidence; retained confirmation is paused.'
        )
    return copy.deepcopy(previous)


def pause(previous, session, now, reason):
    if (
        not previous
        or previous.get('revision') != REVISION
        or previous.get('session') != session
        or not finite(previous.get('checkedAt'))
        or not 0 <= now - previous['checkedAt'] <= MAX_GAP_SECONDS
    ):
        return None
    result = copy.deepcopy(previous)
    result.update(
        status='paused',
        reason=reason,
        expiresAt=previous['checkedAt'] + MAX_GAP_SECONDS,
        pausedAt=previous.get('pausedAt', now),
    )
    return result


def advance(previous, model, source, session, now, required, blocker=None):
    if not finite(source) or not 0 <= now - source < 90:
        return None
    previous = previous or {}
    if (
        previous.get('session') == session
        and finite(previous.get('sourceAt'))
        and source < previous['sourceAt']
    ):
        return None
    compatible = (
        previous.get('revision') == REVISION
        and previous.get('model') == model
        and previous.get('session') == session
        and previous.get('requiredSeconds') == required
        and finite(previous.get('checkedAt'))
        and 0 <= now - previous['checkedAt'] <= MAX_GAP_SECONDS
        and finite(previous.get('sourceAt'))
        and source >= previous['sourceAt']
        and finite(previous.get('since'))
        and now >= previous['since']
    )
    if not compatible:
        return {
            'revision': REVISION,
            'model': model,
            'kind': 'earnings',
            'session': session,
            'since': now,
            'firstSourceAt': source,
            'sourceAt': source,
            'checkedAt': now,
            'samples': 1,
            'seconds': 0,
            'requiredSeconds': required,
            'status': 'confirming',
            'reason': blocker or 'Confirming the paid advantage using distinct fresh observations.',
            'expiresAt': now + MAX_GAP_SECONDS,
        }
    result = copy.deepcopy(previous)
    if source > previous['sourceAt']:
        wall = now - previous['checkedAt']
        elapsed = source - previous['sourceAt']
        # A delayed tick may miss the moment the old observation expires.
        # Separate short deltas do not prove continuous fresh coverage.
        continuous = (
            previous.get('pausedAt') is None
            and max(wall, elapsed, now - previous['sourceAt']) <= CONTINUITY_SECONDS
        )
        credit = min(wall, elapsed) if continuous else 0
        result.update(
            sourceAt=source,
            checkedAt=now,
            samples=previous['samples'] + 1,
            seconds=min(required, previous['seconds'] + max(0, credit)),
        )
        result.pop('pausedAt', None)
    # A duplicate cannot resume continuity after a missing-data pause.
    elif previous.get('pausedAt') is not None:
        return pause(
            previous,
            session,
            now,
            'Waiting for a new qualified demand observation; retained progress is paused.',
        )
    ready = result['seconds'] >= required and result['samples'] >= required / 60 + 1
    result.update(
        status='ready' if ready else 'confirming',
        reason=blocker
        or (
            'Earnings confirmation is complete; final admission checks still apply.'
            if ready
            else 'Confirming the paid advantage using distinct fresh observations.'
        ),
        expiresAt=result['checkedAt'] + MAX_GAP_SECONDS,
    )
    return result
