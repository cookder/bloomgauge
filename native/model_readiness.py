"""One conservative readiness decision for model performance measurements."""

import hashlib, json, math
from optimizer_store import device_id
from model_combinations import members, selection_key
from provider_reporting import observed_models, preloading, state_fresh


STARTING = 'Statistics paused · Darkbloom is starting and loading its models.'
UNAUTHORIZED = (
    'Statistics paused · the Darkbloom network hasn’t cleared this Mac to serve yet '
    '(it checks each new session). Counting starts when it does.'
)


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def session_key(raw):
    selected = members(selection_key(raw.get('advertised_models')))
    return hashlib.sha256(
        json.dumps(
            [device_id(raw), raw.get('pid'), raw.get('started_at'), selected], sort_keys=True
        ).encode()
    ).hexdigest()


def readiness(raw, proof, now, identity_verified=False, pending=False, authorized=None):
    """`authorized`: whether the network lets this session serve (serving_trust); False
    pauses counting, since warm time earns nothing before that. None skips the check."""
    selected = members(selection_key(raw.get('advertised_models')))
    result = {
        'counting': False,
        'status': 'paused',
        'models': selected,
        'detail': 'Waiting for verified model readiness.',
    }
    written = raw.get('written_at')
    offered = observed_models(raw.get('advertised_models'))
    warm = raw.get('warm_models') if isinstance(raw.get('warm_models'), list) else []
    if not selected and len(offered) > 2:
        # Darkbloom manages 3+ models; their work is reported together (provider_reporting).
        result['detail'] = (
            'Per-model statistics need one model or a pair. Darkbloom manages the '
            f'{len(offered)} models this Mac offers; pick one model in Model controls '
            'to record them.'
        )
    elif not selected or not finite(written) or not -5 < now - written < 15:
        # Counting keeps the 15 s window; 0.9.10's startup preload writes every 30 s.
        result['detail'] = (
            STARTING
            if selected and preloading(raw) and state_fresh(raw, now)
            else 'Statistics paused · waiting for fresh model readings.'
        )
    elif (
        not identity_verified
        or not device_id(raw)
        or type(raw.get('pid')) is not int
        or raw['pid'] <= 0
        or not finite(raw.get('started_at'))
    ):
        result['detail'] = 'Statistics paused · verifying this Mac and provider session.'
    elif pending or proof.get('status') == 'warming':
        result['detail'] = 'Statistics paused · model switching, loading or pre-warming.'
    elif raw.get('trust', {}).get('status') != 'online':
        result['detail'] = 'Statistics paused · provider is not ready to serve.'
    elif authorized is False:
        result['detail'] = UNAUTHORIZED
    elif (
        proof.get('session') != session_key(raw)
        or members(proof.get('model')) != selected
        or proof.get('status') != 'ready'
        or not finite(proof.get('verifiedAt'))
    ):
        result['detail'] = 'Statistics paused · successful pre-warm has not been verified.'
    elif not isinstance(raw.get('warm_models'), list) or not all(
        m in raw['warm_models'] for m in selected
    ):
        # Darkbloom 0.9.10 unloads idle models and loads them again when work arrives.
        cold = ', '.join(m for m in selected if m not in warm)
        result['detail'] = (
            f"Statistics paused · {cold} isn't loaded right now. Counting resumes when "
            'Darkbloom loads it again.'
            if len(selected) == 1
            else 'Statistics paused · a pair counts only while both models are loaded. '
            f'Not loaded now: {cold}.'
        )
    elif written < proof['verifiedAt']:
        result['detail'] = 'Statistics paused · waiting for the first sample after pre-warm.'
    else:
        result.update(
            counting=True,
            status='counting',
            verifiedAt=proof['verifiedAt'],
            detail='Counting warm model time, including idle time.'
            if len(selected) == 1
            else 'Counting time while both models are loaded and warm, including idle time.',
        )
    return result
