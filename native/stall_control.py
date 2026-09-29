"""Runs the stall-recovery ladder (stall_recovery.py) inside automatic switching.

Steps are recorded as opt_events ('stall-probe', 'stall-restart', 'stall-escape',
'stall-hold'), so the ladder survives an app restart and never repeats a step
within one episode. Only demand mode acts; Observe, manual choices, trials in
progress and pending switches are left alone.

The nudge is a tiny request routed back to this Mac through Darkbloom's
coordinator ("X-Darkbloom-Route: self"), which providers report can restart
routing. It needs an API key from the owner's Darkbloom account, stored by the
owner in the login Keychain:
    security add-generic-password -U -a bloom -s bloom-darkbloom-api-key -w
Without a key, the nudge is a one-token request to the local engine instead.
"""

import copy
import json
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from model_combinations import members
from prewarm import prewarm, WarmupError
from stall_recovery import assess, EPISODE_LIMIT_SECONDS, BASELINE_SECONDS, PEER_NOTE
from network_health import stall_wait
import manager

KEYCHAIN_ACCOUNT = 'bloom'
KEYCHAIN_SERVICE = 'bloom-darkbloom-api-key'
API_URL = 'https://api.darkbloom.dev/v1/chat/completions'
STEPS = ('probe', 'restart', 'escape', 'hold')
FRESH_SECONDS = 60
# Error words in a self-route reply, as providers and staff describe them.
# model_not_loaded: the coordinator's record of this Mac is wrong.
REPLY_CODES = (
    'model_not_loaded',
    'machine_busy',
    'no_available_provider',
    'no available provider',
    'unauthorized',
    'invalid_api_key',
    'insufficient',
)


def api_key(runner=subprocess.run):
    try:
        result = runner(
            [
                '/usr/bin/security',
                'find-generic-password',
                '-a',
                KEYCHAIN_ACCOUNT,
                '-s',
                KEYCHAIN_SERVICE,
                '-w',
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    key = result.stdout.strip() if result.returncode == 0 else ''
    return key if key and len(key) <= 4096 and not any(c in key for c in '\r\n ') else None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect becomes an HTTP error (reported as http-3xx), never a resend."""

    def redirect_request(self, *args, **kwargs):
        return None


def self_route(model, key, timeout=90, opener=None):
    """One five-token request routed to this Mac. Returns codes only, never content or the key."""
    body = json.dumps(
        {
            'model': model,
            'messages': [{'role': 'user', 'content': 'hi'}],
            'max_tokens': 5,
            'stream': False,
        }
    ).encode()
    request = urllib.request.Request(
        API_URL,
        data=body,
        method='POST',
        headers={
            'Content-Type': 'application/json',
            'Authorization': 'Bearer ' + key,
            'X-Darkbloom-Route': 'self',
            'User-Agent': 'BloomDashboard/1.1',
        },
    )
    try:
        # The key must never follow a redirect or pass through a system proxy.
        opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        )
        with opener.open(request, timeout=timeout) as response:
            value = json.loads(response.read(65537))
        served = isinstance(value, dict) and bool(value.get('choices'))
        return {
            'kind': 'self-route',
            'ok': served,
            'httpStatus': 200,
            'code': 'served' if served else 'unexpected-response',
        }
    except urllib.error.HTTPError as error:
        try:
            text = error.read(4097).decode(errors='replace').lower()
        except Exception:
            text = ''
        code = next((c.replace(' ', '_') for c in REPLY_CODES if c in text), 'http-%d' % error.code)
        return {'kind': 'self-route', 'ok': False, 'httpStatus': error.code, 'code': code}
    except (socket.timeout, TimeoutError):
        return {'kind': 'self-route', 'ok': False, 'httpStatus': None, 'code': 'timeout'}
    except urllib.error.URLError as error:
        timed_out = isinstance(error.reason, (socket.timeout, TimeoutError))
        return {
            'kind': 'self-route',
            'ok': False,
            'httpStatus': None,
            'code': 'timeout' if timed_out else 'unreachable',
        }
    except (OSError, ValueError):
        return {'kind': 'self-route', 'ok': False, 'httpStatus': None, 'code': 'unreachable'}


NOTES = {
    'model_not_loaded': "Darkbloom's record of this Mac is out of date.",
    'machine_busy': 'Darkbloom reported this Mac as busy.',
    'no_available_provider': 'Darkbloom found no available provider for it.',
    'unauthorized': 'The stored API key was rejected.',
    'invalid_api_key': 'The stored API key was rejected.',
    'insufficient': 'The account has too little balance for the request.',
    'unreachable': 'Darkbloom could not be reached.',
    'timeout': 'No reply within 90 seconds; Darkbloom did not route it to this Mac in time.',
}


def describe(outcome):
    """Activity-log sentence for a nudge result."""
    via = (
        'through Darkbloom'
        if outcome.get('kind') == 'self-route'
        else 'to the local engine (no API key stored)'
    )
    if outcome.get('ok'):
        return 'Test request ' + via + ' was served.'
    return (
        'Test request '
        + via
        + ' failed ('
        + str(outcome.get('code'))
        + '). '
        + NOTES.get(outcome.get('code'), '')
    ).strip()


class StallControl:
    def __init__(self, optimizer, keychain=api_key, route=self_route, local=prewarm):
        self.o = optimizer
        self.keychain, self.route, self.local = keychain, route, local
        self.lock = threading.RLock()
        self.status, self.status_at = {}, 0
        self.nudging = False
        self.notified = set()

    def observations(self, account, device, now):
        start = now - EPISODE_LIMIT_SECONDS - BASELINE_SECONDS - 600
        db, lock = self.o.store.h.db, self.o.store.h.lock
        with lock:
            minutes = [
                dict(r)
                for r in db.execute(
                    """SELECT at,model,seconds,jobs FROM opt_ready_minutes
                WHERE account=? AND device=? AND at>=? ORDER BY at""",
                    (account, device, start),
                )
            ]
            models = sorted({m['model'] for m in minutes})
            network = {}
            if models:
                for r in db.execute(
                    'SELECT at,model,active,queued,warm FROM opt_network WHERE at>=? AND model IN ('
                    + ','.join('?' for _ in models)
                    + ')',
                    (start, *models),
                ):
                    network.setdefault(r['model'], []).append(dict(r))
            events = [
                dict(r)
                for r in db.execute(
                    """SELECT at,kind,model FROM opt_events
                WHERE account=? AND device=? AND at>=? AND (kind LIKE 'stall-%' OR kind='switching') ORDER BY at""",
                    (account, device, now - 86400),
                )
            ]
        attempts = [
            {'at': e['at'], 'step': e['kind'][6:], 'model': e['model']}
            for e in events
            if e['kind'].startswith('stall-') and e['kind'][6:] in STEPS
        ]
        switches = [e['at'] for e in events if e['kind'] == 'switching']
        return minutes, network, attempts, switches

    def peer_windows(self, now):
        """This Mac's recent public-counter windows with its peers' rates (network_evidence),
        or None: no evidence yet, or a network-wide outage (network_health), when Macs like
        this one are no guide and the peer trigger must stay quiet."""
        health = getattr(self.o, 'network_health', None)
        if health is not None:
            try:
                if health.outage(now):
                    return None
            except Exception:
                pass  # no outage information
        evidence = getattr(self.o, 'network_evidence', None)
        if evidence is None:
            return None
        try:
            windows = evidence.own_windows(now)
        except Exception:
            return None
        return windows if isinstance(windows, list) else None

    def evaluate(self, account, device, raw, now):
        minutes, network, attempts, switches = self.observations(account, device, now)
        return assess(
            minutes,
            network,
            attempts,
            switches,
            raw.get('started_at'),
            now,
            peers=self.peer_windows(now),
        )

    def tick(self, now, settings, live, raw, current, options, environment):
        """Take the next step if one is due. Returns True when a restart was dispatched."""
        account, device = live.get('account', ''), live.get('device', '')
        trial = (self.o.last_demand_decision or {}).get('trial') or {}
        if (
            settings.get('mode') != 'demand'
            or settings.get('pending')
            or settings.get('requestedModel')
            or not account
            or not device
            or not self.o.tracking(raw, now, cleared=False)['counting']
            or (trial.get('current') and not trial.get('complete'))
        ):
            with self.lock:
                self.status, self.status_at = {'status': 'inactive'}, now
            return False
        result = self.evaluate(account, device, raw, now)
        # A Darkbloom-wide outage: no restart or escape, wait (network_health.py).
        result = stall_wait(result, getattr(self.o, 'network_health', None), now)
        if result['step'] == 'escape' and manager.active(settings):
            # The manager never escapes to another model. After a nudge or restart the
            # ladder stops (hold); when demand fell as well there is nothing to recover.
            taken = result.get('taken') or []
            note = PEER_NOTE + ' ' if result.get('trigger') == 'peers' else ''
            result = (
                {
                    **result,
                    'step': 'hold',
                    'reason': note
                    + 'No work after '
                    + ' and '.join(taken)
                    + '. Holding the home model; BloomGauge has stopped trying. Check Darkbloom (darkbloom doctor, Slack) for a routing problem.',
                }
                if taken
                else {
                    **result,
                    'step': None,
                    'reason': 'Work stopped as this model’s network demand fell. Holding the home model.',
                }
            )
        with self.lock:
            self.status, self.status_at = result, now
        step = result['step']
        if step == 'probe':
            self.record(account, device, now, step, result)
            self.start_nudge(account, device, result['model'], raw, options)
        elif step == 'restart':
            # The restart event is recorded by confirm_restart, inside the
            # guarded switch preflight, once every check has passed.
            return self.o.dispatch_stall_restart(
                now, live, raw, current, options, environment, result
            )
        elif step in ('escape', 'hold'):
            self.record(account, device, now, step, result)
        if step:
            with self.o.lock:
                self.o.detail = result['reason']
        return False

    def record(self, account, device, now, step, result):
        self.o.store.event(
            account, device, now, 'stall-' + step, result.get('model'), result['reason']
        )
        with self.lock:
            self.status = {**self.status, 'taken': [*self.status.get('taken', []), step]}

    def start_nudge(self, account, device, model, raw, options):
        with self.lock:
            if self.nudging:
                return
            self.nudging = True
        target = members(model)[0] if model else None
        worker = threading.Thread(
            target=self.nudge,
            args=(account, device, target, copy.deepcopy(raw), list(options)),
            daemon=True,
        )
        worker.start()

    def nudge(self, account, device, model, raw, options):
        try:
            key = self.keychain()
            if key:
                outcome = self.route(model, key)
            else:
                try:
                    self.local(self.o.home, raw, options, model, timeout=60)
                    outcome = {'kind': 'local', 'ok': True, 'code': 'served'}
                except WarmupError as error:
                    outcome = {'kind': 'local', 'ok': False, 'code': error.code}
            self.o.store.event(
                account, device, time.time(), 'stall-nudge', model, describe(outcome)
            )
            with self.lock:
                self.status = {**self.status, 'nudge': outcome}
        except Exception:
            pass
        finally:
            with self.lock:
                self.nudging = False

    def confirm_restart(self, account, device, raw, target, rules, now):
        """Inside the switch preflight: the stall must still call for this restart."""
        result = self.evaluate(account, device, raw, now)
        result = stall_wait(result, getattr(self.o, 'network_health', None), now)
        if result['step'] != 'restart' or result['model'] not in members(target):
            return None
        self.record(account, device, now, 'restart', result)
        return {
            'at': now,
            'kind': 'recovery',
            'currentModel': target,
            'target': target,
            'policy': dict(rules),
            'reason': result['reason'],
            'stall': {
                k: result.get(k)
                for k in (
                    'model',
                    'silenceSeconds',
                    'baselineJobsPerMinute',
                    'demandHeld',
                    'episodeStart',
                    'taken',
                    'trigger',
                    'peers',
                )
            },
        }

    def escape_active(self, now):
        with self.lock:
            status, at = self.status, self.status_at
        taken = status.get('taken') or []
        return bool(
            0 <= now - at <= FRESH_SECONDS
            and status.get('status') == 'stalled'
            and (status.get('step') == 'escape' or 'escape' in taken)
            and 'hold' not in taken
        )

    def snapshot(self, now):
        with self.lock:
            status, at = copy.deepcopy(self.status), self.status_at
        status['at'] = at
        status['fresh'] = 0 <= now - at <= FRESH_SECONDS
        return status

    def send_pending(self, account, device, push, now):
        """Push a notice when BloomGauge stops trying (the 'hold' step)."""
        with self.o.store.h.lock:
            rows = [
                dict(r)
                for r in self.o.store.h.db.execute(
                    """SELECT id,at,kind,model,detail FROM opt_events
                WHERE account=? AND device=? AND kind IN ('stall-hold','manager-notice') AND at>=? ORDER BY at""",
                    (account, device, now - 900),
                )
            ]
        for row in rows:
            hold = row['kind'] == 'stall-hold'
            key = ('stall-hold-' if hold else 'manager-notice-') + str(row['id'])
            if key in self.notified:
                continue
            if push.enqueue_notice(
                account,
                key,
                'BloomGauge · no work arriving' if hold else 'BloomGauge · model recovery',
                row['detail']
                or 'This Mac stopped getting jobs. Check Darkbloom for a routing or verification problem.',
            ) or push.has_event(account, key):
                self.notified.add(key)
