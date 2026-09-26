"""A conditional GPT-OSS safety net, not a traffic-based earnings forecast."""

import math

MODEL = 'gpt-oss-20b'


def number(v):
    return type(v) in (int, float) and math.isfinite(v) and v >= 0


def reliability(evidence, now):
    # The latest hour of substantial, paid-covered five-minute blocks from the
    # last day. No run/test reset, assumed continuity or lifetime reputation.
    groups = {}
    for m in evidence.get('minutes', []):
        if not all(
            number(m.get(k)) for k in ('at', 'seconds', 'usd', 'paidJobs', 'requests', 'tokens')
        ):
            continue
        if (
            not now - 86400 <= m['at']
            or m['at'] + 60 > now - 120
            or not 59.999999 <= m['seconds'] <= 60.000001
        ):
            continue
        at = int(m['at'] // 300) * 300
        g = groups.setdefault(
            at,
            {'at': at, 'end': 0, 'seconds': 0, 'usd': 0, 'paidJobs': 0, 'requests': 0, 'tokens': 0},
        )
        g['end'] = max(g['end'], m['at'] + 60)
        for k in ('seconds', 'usd', 'paidJobs', 'requests', 'tokens'):
            g[k] += m[k]
    blocks = [g for _, g in sorted(groups.items()) if 240 <= g['seconds'] <= 300.000001][-12:]
    # Older runs cannot fill in an unobserved gap in the latest traffic check.
    for i in range(len(blocks) - 1, 0, -1):
        if blocks[i]['at'] - blocks[i - 1]['at'] > 300:
            blocks = blocks[i:]
            break
    seconds = sum(b['seconds'] for b in blocks)
    usd = sum(b['usd'] for b in blocks)
    jobs = sum(b['paidJobs'] for b in blocks)
    productive = lambda b: b['requests'] > 0 and b['tokens'] > 0
    work = sum(productive(b) for b in blocks)
    recent = blocks[-2:]
    as_of = blocks[-1]['end'] if blocks else None
    enough = len(blocks) >= 6 and seconds >= 1800
    steady = bool(
        enough
        and work >= 0.8 * len(blocks)
        and len(recent) == 2
        and all(productive(b) for b in recent)
    )
    paid = bool(steady and usd > 0 and jobs >= 3 and sum(b['usd'] for b in recent) > 0)
    return {
        'qualified': paid,
        'warmMinutes': seconds / 60,
        'windows': len(blocks),
        'trafficWindows': work,
        'trafficPercent': work * 100 / len(blocks) if blocks else None,
        'usdPerHour': usd * 3600 / seconds if seconds else None,
        'paidJobs': jobs,
        'asOf': as_of,
        'reason': (
            'Recent observed traffic also produced paid work.'
            if paid
            else 'Waiting for 30 covered warm minutes across at least six recent five-minute windows.'
            if not enough
            else 'Recent traffic is no longer steady enough for fallback preference.'
            if not steady
            else 'Recent traffic has not produced enough confirmed paid work for fallback preference.'
        ),
    }


def assess(rows, current, activity, enabled, now):
    row = next((r for r in rows if r['id'] == MODEL), {})
    local = row.get('fallbackEvidence') or reliability({}, now)
    signal = row.get('signal') or {}
    sustained = signal.get('sustained') or {}
    reason = None
    if not enabled:
        reason = 'GPT-OSS fallback preference is off.'
    elif not row.get('selected'):
        reason = 'Select GPT-OSS for automatic switching to use it as a fallback.'
    elif not row.get('available'):
        reason = 'GPT-OSS is not locally available for switching.'
    elif (
        not number(signal.get('observedAt'))
        or not 0 <= now - signal['observedAt'] < 90
        or signal.get('status') not in ('spike', 'normal', 'learning', 'watching')
        or signal.get('coverage', 0) < 0.8
        or not sustained.get('qualified')
    ):
        reason = 'Waiting for fresh, sustained GPT-OSS network demand.'
    elif current == MODEL and (not activity.get('fresh') or activity.get('idleSeconds', 0) >= 300):
        reason = 'Current GPT-OSS traffic is quiet or unverified; fallback preference is suspended.'
    elif not local['qualified']:
        reason = local['reason']
    return {
        **local,
        'model': MODEL,
        'enabled': bool(enabled),
        'qualified': reason is None,
        'current': current == MODEL,
        'reason': reason or local['reason'],
        'eligible': False,
        'status': 'checking' if reason else 'serving' if current == MODEL else 'ready',
        'selection': None,
        'holdReason': None,
        'expiresAt': local['asOf'] + 86400 if local['asOf'] is not None else None,
    }
