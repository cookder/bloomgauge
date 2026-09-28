"""On-demand, allowlisted support reports. Never export raw logs or identifiers."""

import copy
import bloom_log
from datetime import datetime, timezone
import math
from pathlib import Path
import platform
import plistlib
import re
import time

from decision_journal import reason_code
from model_readiness import session_key

SCHEMA = 'bloom-diagnostics-v1'
MAX_BYTES = 256 * 1024
CODES = {
    'memory',
    'temperature',
    'power',
    'identity',
    'readiness',
    'network',
    'daily_limit',
    'retry',
    'confirmation',
    'minimum_run',
    'trial',
    'earnings_evidence',
    'freshness',
    'selection',
    'gain',
    'observation',
    'off',
    'switching',
    'completed',
    'failed',
}
PHASES = {'off', 'waiting', 'confirming', 'watching', 'switching', 'completed', 'failed'}
MODES = {'observe', 'week', 'combo', 'best', 'demand'}
# Internal names the report schema (and the support site's fixed lists) spell differently.
MODE_NAMES = {'optimize': 'best'}
# 'partial': fresh credits reconciled with a coverage gap (live_earnings). The source is
# current, and the site's fixed list has no partial state.
STATUS_NAMES = {'partial': 'ok', 'connecting': 'loading'}
FAILURE_REPORT_SECONDS = 86400
STATUSES = {
    'ok',
    'stale',
    'error',
    'unavailable',
    'missing',
    'loading',
    'idle',
    'waiting',
    'ready',
    'warming',
    'failed',
    'cold',
    'paused',
    'observing',
    'learning',
    'optimizing',
    'switching',
}
EVENTS = {
    'switching',
    'switch-deferred',
    'switched',
    'recovered',
    'failed',
    'paused',
    'started',
    'stopped',
    'restored',
    'manual',
    'warning',
}
SWITCH_FAILURE_CODES = {
    'startup-command',
    'startup-timeout',
    'readiness-timeout',
    'model-load-error',
    'endpoint-configuration',
    'endpoint-discovery',
    'endpoint-authentication',
    'endpoint-unsafe',
    'warmup-capacity',
    'warmup-response',
    'warmup-rejected',
    'warmup-transport',
    'loaded-model-unconfirmed',
    'cache-permission',
    'cache-command',
    'cache-readings',
    'cache-recovery-limited',
    'readiness-changed',
    'warmup-failed',
    'unknown',
}
RECOVERY_CODES = {
    'none',
    'idle-not-verified',
    'resource-or-identity',
    'provider-changed',
    'restore-not-ready',
    'restored',
    'unknown',
}


def number(value, minimum=0, maximum=1e15):
    return (
        round(value, 4)
        if type(value) in (int, float) and math.isfinite(value) and minimum <= value <= maximum
        else None
    )


def enum(value, allowed):
    return value if isinstance(value, str) and value in allowed else 'unknown'


def mode(value):
    return enum(MODE_NAMES.get(value, value) if isinstance(value, str) else value, MODES)


def status(value):
    return enum(STATUS_NAMES.get(value, value) if isinstance(value, str) else value, STATUSES)


def age(value, now):
    at = number(value)
    return number(now - at, maximum=315576000) if at is not None else None


def model_count(value):
    return (
        len(set(value))
        if isinstance(value, list)
        and len(value) <= 1000
        and all(isinstance(model, str) and 0 < len(model) <= 1024 for model in value)
        else None
    )


def event_reason(kind, detail):
    # This legacy outcome describes actions that were NOT taken. Its mention of
    # cache is not evidence of the original failure. Keep the correction local
    # to support exports; economic decision classification is unchanged.
    legacy = (
        'warm-up or switching failed. a safe, idle recovery could not be verified; '
        'no busy restart or cache purge was forced.'
    )
    if kind == 'failed' and isinstance(detail, str) and detail.lower().startswith(legacy):
        return 'failed'
    return reason_code(detail if isinstance(detail, str) else '')


def switch_failure(value, models, now):
    if not isinstance(value, dict) or not value:
        return None
    return {
        'ageSeconds': age(value.get('at'), now),
        'model': models.describe(value.get('model')),
        'stage': enum(value.get('stage'), {'preflight', 'start', 'verify'}),
        'code': enum(value.get('code'), SWITCH_FAILURE_CODES),
        'recovery': enum(value.get('recovery'), {'not-attempted', 'restored', 'blocked', 'failed'}),
        'recoveryCode': enum(value.get('recoveryCode'), RECOVERY_CODES),
        'elapsedSeconds': number(value.get('elapsedSeconds'), maximum=86400),
    }


def recent_failure(value, now):
    at = value.get('at') if isinstance(value, dict) else None
    return number(at) is not None and 0 <= now - at < FAILURE_REPORT_SECONDS


def cache_recovery(value, raw, now):
    if not isinstance(value, dict) or not value:
        return None
    # A saved attempt is not a current permission check. A missing session stays
    # unknown, rather than looking like a verified attempt in another session.
    current = None
    if (
        isinstance(value.get('session'), str)
        and value['session']
        and isinstance(raw.get('attestation_public_key'), str)
        and raw['attestation_public_key']
        and type(raw.get('pid')) is int
        and raw['pid'] > 0
        and number(raw.get('started_at')) is not None
        and model_count(raw.get('advertised_models')) not in (None, 0)
    ):
        current = value['session'] == session_key(raw)
    return {
        'ageSeconds': age(value.get('at'), now),
        'status': enum(value.get('status'), {'running', 'cleared', 'failed'}),
        'currentSession': current,
    }


def error_category(value):
    # Classify source errors without returning their possibly sensitive messages.
    if not isinstance(value, str) or not value:
        return None
    text = value[:2000].lower()
    for category, terms in (
        ('certificate', ('certificate', 'ssl', 'tls')),
        ('authentication', ('401', '403', 'unauthorized', 'sign in', 'login', 'authentication')),
        ('rate_limit', ('429', 'rate limit')),
        ('timeout', ('timed out', 'timeout')),
        ('permission', ('permission', 'denied')),
        ('connection', ('connect', 'unreachable', 'resolve', 'network')),
        ('memory', ('memory', 'cache')),
    ):
        if any(term in text for term in terms):
            return category
    return 'other'


def app_version():
    try:
        info = plistlib.loads((Path(__file__).resolve().parent.parent / 'Info.plist').read_bytes())
        version = info.get('CFBundleShortVersionString', '')
        return (
            version
            if isinstance(version, str) and re.fullmatch(r'\d{1,3}\.\d{1,3}\.\d{1,4}', version)
            else 'unknown'
        )
    except (OSError, ValueError):
        return 'development'


class Models:
    """Per-report aliases, with coarse public family/size, never raw model IDs."""

    def __init__(self):
        self.aliases = {}

    def describe(self, value):
        if not isinstance(value, str) or not value or len(value) > 1024:
            return None
        if value not in self.aliases:
            self.aliases[value] = 'model-' + str(len(self.aliases) + 1)
        lowered = value.lower()
        family = next(
            (
                name
                for prefix, name in (
                    ('gemma', 'Gemma'),
                    ('gpt-oss', 'GPT-OSS'),
                    ('qwen', 'Qwen'),
                    ('nvidia-nemotron', 'Nemotron'),
                    ('nemotron', 'Nemotron'),
                )
                if lowered.startswith(prefix)
            ),
            'Other / combination',
        )
        size = re.search(r'(?:^|[-_ ])(\d{1,3}(?:\.\d{1,2})?)b(?:[-_ ]|$)', lowered)
        return {
            'alias': self.aliases[value],
            'family': family,
            'parameterBillions': number(float(size.group(1)), maximum=999) if size else None,
        }


def source(value, now):
    value = value if isinstance(value, dict) else {}
    return {
        'status': status(value.get('status')),
        'ageSeconds': age(value.get('updatedAt'), now),
        'errorCategory': error_category(value.get('error')),
    }


def build_report(collector, include_earnings=False, remote=False, now=None):
    if type(include_earnings) is not bool:
        raise ValueError('Choose whether to include earnings.')
    now = time.time() if now is None else now
    with collector.lock:
        account = collector.account
        snapshot = copy.deepcopy(collector.snapshot or {})
        earnings = copy.deepcopy(collector.earnings)
        hardware = copy.deepcopy(collector.hardware)
        hardware_at = collector.hardware_at
    with collector.optimizer.lock:
        live = collector.optimizer.live or {}
        matched = bool(account and account == live.get('account') and live.get('device'))
        device = live.get('device') if matched else ''
        state = copy.deepcopy(collector.optimizer.state)
        optimizer_status = collector.optimizer.status
        warmup = copy.deepcopy(collector.optimizer.warmup)
        raw = (
            copy.deepcopy(collector.optimizer.raw)
            if isinstance(collector.optimizer.raw, dict)
            else {}
        )
        identity_ok = bool(
            matched
            and collector.optimizer.identity_ok
            and 0 <= now - collector.optimizer.identity_at < 180
        )
    network = collector.network.snapshot()
    models = Models()
    provider = snapshot.get('provider') or {}
    tracking = provider.get('tracking') or {}
    capacity = raw.get('capacity') if isinstance(raw.get('capacity'), dict) else {}
    chip = hardware.get('chip')
    chip = (
        chip
        if isinstance(chip, str) and re.fullmatch(r'Apple M\d{1,2}(?: Pro| Max| Ultra)?', chip)
        else 'unknown'
    )
    macos = platform.mac_ver()[0]
    macos = macos if re.fullmatch(r'\d{1,2}(?:\.\d{1,3}){0,2}', macos) else 'unknown'
    report = {
        'schema': SCHEMA,
        'generatedAt': datetime.fromtimestamp(now, timezone.utc).isoformat(),
        'app': {
            'version': app_version(),
            'surface': 'phone' if remote else 'mac',
            'python': platform.python_version(),
            'architecture': enum(platform.machine(), {'arm64', 'x86_64'}),
            'macOS': macos,
        },
        'privacy': {
            'earningsIncluded': include_earnings,
            'modelNames': 'per-report aliases with family/size only',
            'excluded': [
                'credentials',
                'account and device identifiers',
                'private URLs',
                'user paths',
                'process names',
                'raw logs',
                'free-text error messages',
                'credit IDs',
                'push subscriptions',
            ],
            'sharing': 'Nothing uploaded. Only share this file with someone you choose.',
        },
        'hardware': {
            'chip': chip,
            'memoryTotalGB': number(hardware.get('memoryTotalGB'), maximum=4096),
            'memoryAvailableGB': number(hardware.get('memoryAvailableGB'), maximum=4096),
            'cachedFilesGB': number(hardware.get('cachedFilesGB'), maximum=4096),
            'thermalState': enum(
                hardware.get('thermal'),
                {
                    'Nominal',
                    'Fair',
                    'Serious',
                    'Critical',
                    'nominal',
                    'fair',
                    'serious',
                    'critical',
                },
            ),
            'powerSource': enum(
                hardware.get('powerSource'), {'AC Power', 'Battery Power', 'UPS Power'}
            ),
            'sampleAgeSeconds': age(hardware_at, now),
        },
        'sources': {
            'collector': {'sampleAgeSeconds': age(snapshot.get('at'), now)},
            'earnings': source(earnings, now),
            'monitor': source(snapshot.get('monitor'), now),
            'network': source(network.get('capacity'), now),
        },
        'provider': {
            'online': provider.get('online') is True,
            'serving': provider.get('active') is True,
            'countingWarmStatistics': tracking.get('counting') is True,
            'sampleAgeSeconds': age(raw.get('written_at'), now),
            'selectedModelCount': model_count(raw.get('advertised_models')),
            'warmModelCount': model_count(raw.get('warm_models')),
            'inferenceActive': raw.get('inference_active')
            if type(raw.get('inference_active')) is bool
            else None,
            'gpuActiveGB': number(capacity.get('gpu_memory_active_gb'), maximum=4096),
            'gpuCacheGB': number(capacity.get('gpu_memory_cache_gb'), maximum=4096),
            'version': provider.get('version')
            if isinstance(provider.get('version'), str)
            and re.fullmatch(r'\d{1,3}\.\d{1,3}\.\d{1,4}', provider['version'])
            else 'unknown',
            'model': models.describe(provider.get('model')),
        },
        'optimizer': {
            'mode': mode(state.get('mode')),
            'status': enum(optimizer_status, STATUSES),
            'identityVerified': identity_ok,
            'scopeMatched': matched,
            'pendingCommand': bool(state.get('pending')),
            'requestedModel': models.describe(state.get('requestedModel')),
            'warmupStatus': enum(warmup.get('status'), STATUSES),
            # A recent failure is reported even when this account/provider scope doesn't
            # match now: a new device key or a sign-out must not hide why control stopped.
            'lastSwitchFailure': switch_failure(state.get('lastSwitchFailure'), models, now)
            if matched or recent_failure(state.get('lastSwitchFailure'), now)
            else None,
            'cacheRecovery': cache_recovery(state.get('cacheRecovery'), raw, now)
            if matched
            else None,
            'recentDecisions': [],
            'recentEvents': [],
            'windowHours': 24,
            'limitPerList': 40,
        },
        'issues': [],
    }
    if not identity_ok:
        # Solo/pair control identity is intentionally unavailable for 3+ models.
        # Do not mislabel that expected control hold as a failed reporting match.
        detail = (
            'Automatic model control is not verified for this model set. Multi-model reporting uses a separate match.'
            if (report['provider']['selectedModelCount'] or 0) > 2
            else 'Fresh account/provider matching is not verified.'
        )
        report['issues'].append({'code': 'identity_not_verified', 'description': detail})
    for name, entry in report['sources'].items():
        if name != 'collector' and entry['status'] != 'ok':
            report['issues'].append(
                {
                    'code': name + '_not_current',
                    'description': 'This source is unavailable, stale or has not reported yet.',
                }
            )
    collector_age = report['sources']['collector']['sampleAgeSeconds']
    if collector_age is None or collector_age > 15:
        report['issues'].append(
            {
                'code': 'collector_stale',
                'description': 'Current collector telemetry is missing or stale.',
            }
        )
    if provider.get('online') is not True:
        report['issues'].append(
            {
                'code': 'provider_offline',
                'description': 'The provider was offline in the captured snapshot.',
            }
        )
    if provider.get('online') is True and not tracking.get('counting'):
        report['issues'].append(
            {
                'code': 'model_not_counting',
                'description': 'The model is not currently verified for warm-statistics accounting.',
            }
        )
    if matched:
        # Select only necessary columns and the current owner/device; no raw payloads.
        with collector.history.lock:
            db = collector.history.db
            decisions = db.execute(
                """SELECT at,updated,mode,model,target,phase,code,observed_seconds
                FROM optimizer_decisions WHERE account=? AND device=? AND updated>=? AND at<=?
                ORDER BY id DESC LIMIT 41""",
                (account, device, now - 86400, now),
            ).fetchall()
            events = db.execute(
                """SELECT at,kind,model,detail,downtime FROM opt_events
                WHERE account=? AND device=? AND at>=? AND at<=? ORDER BY at DESC LIMIT 41""",
                (account, device, now - 86400, now),
            ).fetchall()
            if include_earnings:
                paid = db.execute(
                    """SELECT COUNT(*) AS entries,SUM(micro_usd) AS micro_usd FROM opt_credits
                    WHERE account=? AND provider IN (SELECT provider FROM opt_identity WHERE device=?)
                    AND model!='base_reward' AND at>=? AND at<?""",
                    (account, device, now - 86400, now - 120),
                ).fetchone()
                report['earnings'] = {
                    'recordedInferenceUsd': (paid['micro_usd'] or 0) / 1e6,
                    'recordedCreditCount': paid['entries'],
                    'windowHours': 24,
                    'settlementTailSeconds': 120,
                    'coverage': 'Recorded credits only. Missing periods stay unknown; this is not an ROI estimate.',
                    'baseRewardsIncluded': False,
                }
        report['optimizer']['decisionsTruncated'] = len(decisions) > 40
        report['optimizer']['eventsTruncated'] = len(events) > 40
        report['optimizer']['recentDecisions'] = [
            {
                'ageSeconds': age(row['updated'], now),
                'observedSeconds': number(row['observed_seconds']),
                'mode': mode(row['mode']),
                'model': models.describe(row['model']),
                'target': models.describe(row['target']),
                'phase': enum(row['phase'], PHASES),
                'reasonCategory': enum(row['code'], CODES),
            }
            for row in decisions[:40]
        ]
        report['optimizer']['recentEvents'] = [
            {
                'ageSeconds': age(row['at'], now),
                'kind': enum(row['kind'], EVENTS),
                'model': models.describe(row['model']),
                'reasonCategory': event_reason(row['kind'], row['detail']),
                'downtimeSeconds': number(row['downtime']),
            }
            for row in events[:40]
        ]
    elif include_earnings:
        report['earnings'] = {
            'available': False,
            'reason': 'Current account and provider scope are not matched.',
        }
    # Recent backend warnings: fixed messages and exception types only, no payloads.
    report['recentWarnings'] = [
        {
            'ageSeconds': age(w['at'], now),
            'level': w['level'],
            'source': w['source'][:60],
            'message': w['message'][:200],
            'error': w['error'],
        }
        for w in bloom_log.recent_warnings()[-10:]
    ]
    # A sign-out/identity change during capture invalidates this snapshot.
    with collector.lock:
        if collector.account != account:
            raise ValueError('Account changed. Generate a new report.')
    with collector.optimizer.lock:
        current = collector.optimizer.live or {}
        if matched and (current.get('account') != account or current.get('device') != device):
            raise ValueError('Provider changed. Generate a new report.')
    return report
