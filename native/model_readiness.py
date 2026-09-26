"""One conservative readiness decision for model performance measurements."""

import hashlib, json, math
from optimizer_store import device_id
from model_combinations import members, selection_key


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def session_key(raw):
    selected = members(selection_key(raw.get('advertised_models')))
    return hashlib.sha256(
        json.dumps(
            [device_id(raw), raw.get('pid'), raw.get('started_at'), selected], sort_keys=True
        ).encode()
    ).hexdigest()


def readiness(raw, proof, now, identity_verified=False, pending=False):
    selected = members(selection_key(raw.get('advertised_models')))
    result = {
        'counting': False,
        'status': 'paused',
        'models': selected,
        'detail': 'Waiting for verified model readiness.',
    }
    written = raw.get('written_at')
    if not selected or not finite(written) or not -5 < now - written < 15:
        result['detail'] = 'Statistics paused · waiting for fresh model readings.'
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
        result['detail'] = 'Statistics paused · every selected model must be loaded and warm.'
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
