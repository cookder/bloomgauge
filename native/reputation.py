"""This Mac's official reputation, delivered by the signed-in console view.

The browser session stays in WebKit. Only an allowlist of reputation fields
crosses the native bridge; no console credentials are stored in this database.
"""

import copy, hashlib, math, pathlib, threading, time
import json, re
from datetime import datetime


def concurrency(provider, now):
    """Allowlisted owner-API gauges; absence is not idle or a zero limit."""
    if provider.get('online') is not True:
        return None
    try:
        raw = re.sub(r'\.(\d+)', lambda m: '.' + m[1][:6].ljust(6, '0'), provider['last_heartbeat'])
        stamp = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            return None
        heartbeat = stamp.timestamp()
    except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
        return None
    if not -5 <= now - heartbeat <= 60:
        return None

    def count(value):
        return (
            value
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 1000000
            else None
        )

    slots = (
        (provider.get('backend_capacity') or {}).get('slots')
        if isinstance(provider.get('backend_capacity'), dict)
        else None
    )
    result = {
        'at': heartbeat,
        'pending': count(provider.get('pending_requests')),
        'limit': count(provider.get('max_concurrency')),
        'slots': [],
    }
    if isinstance(slots, list) and len(slots) <= 128:
        for slot in slots:
            if (
                not isinstance(slot, dict)
                or not isinstance(slot.get('model'), str)
                or not 0 < len(slot['model']) <= 512
            ):
                return None
            result['slots'].append(
                {
                    'model': slot['model'],
                    'state': slot.get('state')
                    if slot.get('state')
                    in ('running', 'idle', 'idle_shutdown', 'crashed', 'reloading')
                    else None,
                    'running': count(slot.get('num_running')),
                    'waiting': count(slot.get('num_waiting')),
                    'limit': count(slot.get('max_concurrency')),
                }
            )
    return result if result['pending'] is not None or result['slots'] else None


def number(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def normalize(provider):
    """The allowlisted reputation fields. A missing or malformed field is unknown (None);
    it never rejects the reading. Darkbloom 0.9.10 drops the 0-1 score ("Remove the
    composite score calculation and owner API field", coordinator myReputation)."""
    rep = provider.get('reputation')
    rep = rep if isinstance(rep, dict) else {}
    score = rep.get('score')
    result = {'score': score if number(score) and score <= 1 else None}
    for source, target in [
        ('total_jobs', 'totalJobs'),
        ('successful_jobs', 'successfulJobs'),
        ('failed_jobs', 'failedJobs'),
        ('total_uptime_seconds', 'uptimeSeconds'),
        ('avg_response_time_ms', 'responseTimeMs'),
        ('challenges_passed', 'challengesPassed'),
        ('challenges_failed', 'challengesFailed'),
    ]:
        value = rep.get(source)
        result[target] = value if number(value) and value == int(value) else None
    total, passed, failed = (result[k] for k in ('totalJobs', 'successfulJobs', 'failedJobs'))
    if total is not None and passed is not None and failed is not None and passed + failed > total:
        # The coordinator adds stored counters to live ones (me_handlers.go,
        # machine_history.go); a total below its parts is unknown, the rest still counts.
        result['totalJobs'] = None
    result['trustLevel'] = (
        provider.get('trust_level')
        # self_signed: Darkbloom's tier for Macs verified through App Attest without MDM.
        if provider.get('trust_level') in ('hardware', 'self_signed', 'software', 'untrusted', 'unknown')
        else None
    )
    result['providerStatus'] = (
        provider.get('status')
        if provider.get('status') in ('online', 'serving', 'offline', 'never_seen', 'untrusted')
        else None
    )
    return result


READING_FIELDS = (
    'score',
    'totalJobs',
    'successfulJobs',
    'failedJobs',
    'uptimeSeconds',
    'responseTimeMs',
    'challengesPassed',
    'challengesFailed',
)


def blank(reading):
    """A provider row without any reputation value (e.g. no reputation object): the
    reading is unavailable, not a fresh all-unknown one to save over the last good."""
    return all(reading.get(k) is None for k in READING_FIELDS)


class Reputation:
    def __init__(self, history, home, native_token='', sessions=None, identity_context=None):
        self.history, self.home, self.native_token = history, pathlib.Path(home), native_token
        self.lock = threading.RLock()
        self.status = 'disconnected'
        self.key = None
        self.data = None
        self.sequence = 0
        self.sessions, self.identity_context = sessions, identity_context

    def identity(self):
        try:
            path = self.home / '.darkbloom/daemon-state.json'
            if path.stat().st_size > 8 * 1024 * 1024:
                return None
            key = json.loads(path.read_text()).get('attestation_public_key')
            return key if isinstance(key, str) and 0 < len(key) <= 4096 else None
        except (OSError, ValueError, AttributeError):
            return None

    def sync_identity(self):
        key = self.identity()
        if key != self.key:
            self.key = key
            self.data = self.history.cache(self.cache_key()) if key else None
            self.status = 'stale' if self.data else 'disconnected'

    def cache_key(self):
        return 'reputation:' + hashlib.sha256(self.key.encode()).hexdigest()

    def ingest(self, payload, now=None):
        now = time.time() if now is None else now
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get('sequence'), int)
            or isinstance(payload['sequence'], bool)
        ):
            raise ValueError('Invalid native message')
        with self.lock:
            self.sync_identity()
            if payload['sequence'] <= self.sequence:
                return self.snapshot(now)
            state = payload.get('status')
            if state == 'ok':
                providers = payload.get('providers')
                if not isinstance(providers, list) or len(providers) > 1000:
                    raise ValueError('Invalid provider list')
                matches = [
                    p
                    for p in providers
                    if isinstance(p, dict) and self.key and p.get('se_public_key') == self.key
                ]
                if len(matches) != 1:
                    self.data = None
                    if self.key:
                        self.history.cache(self.cache_key(), {})
                    self.status = 'unmatched'
                else:
                    requested = payload.get('requestedAt')
                    previous = (self.data or {}).get('requestedAt')
                    if number(requested) and (
                        requested > now + 5 or (number(previous) and requested <= previous)
                    ):
                        self.sequence = payload['sequence']
                        return self.snapshot(now)
                    reading = normalize(matches[0])
                    if blank(reading):
                        # Keep the last good reading (shown as saved) until a real one arrives.
                        self.status = 'unavailable'
                        self.sequence = payload['sequence']
                        return self.snapshot(now)
                    data = {**reading, 'updatedAt': now, 'scope': 'network_record'}
                    if number(requested):
                        data['requestedAt'] = requested
                    if self.sessions and self.identity_context:
                        row = matches[0]
                        selected = row.get('models')
                        selected = (
                            sorted(set(selected))
                            if isinstance(selected, list)
                            and all(isinstance(m, str) and m for m in selected)
                            else None
                        )
                        record_id = (
                            row.get('id')
                            if isinstance(row.get('id'), str) and 0 < len(row['id']) <= 512
                            else None
                        )
                        accepted = self.sessions.reputation_observation(
                            data,
                            payload.get('requestedAt'),
                            record_id,
                            selected,
                            self.identity_context(now),
                            now,
                        )
                        data['observedSessionId'] = accepted or None
                        data['concurrency'] = concurrency(row, now) if accepted else None
                    self.history.cache(self.cache_key(), data)
                    self.data, self.status = data, 'ok'
            elif state in ('connecting', 'auth_required', 'unavailable', 'disconnected'):
                self.status = state
                if state == 'disconnected':
                    self.data = None
                    if self.key:
                        self.history.cache(self.cache_key(), {})
            else:
                raise ValueError('Invalid connection status')
            self.sequence = payload['sequence']
            return self.snapshot(now)

    def snapshot(self, now=None):
        now = time.time() if now is None else now
        with self.lock:
            self.sync_identity()
            status = self.status
            if status == 'ok' and self.data and now - self.data['updatedAt'] > 150:
                status = 'stale'
            session = self.sessions.snapshot(now)['current'] if self.sessions else None
            data = copy.deepcopy(self.data) or None
            if data and data.get('concurrency'):
                c = data['concurrency']
                if (
                    status != 'ok'
                    or now - c['at'] > 60
                    or now - data.get('requestedAt', 0) > 45
                    or not session
                    or session.get('status') != 'active'
                    or (session.get('performance') or {}).get('status') != 'counting'
                    or data.get('requestedAt', 0)
                    < (session.get('performance') or {}).get('segmentStartedAt', 0)
                    or data.get('observedSessionId') != session['id']
                ):
                    data['concurrency'] = None
            return {
                'status': status,
                'data': data,
                'session': session,
                'nativeAvailable': bool(self.native_token),
                'identityAvailable': bool(self.key),
            }
