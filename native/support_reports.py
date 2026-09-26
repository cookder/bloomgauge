"""Explicit problem reports. No telemetry or persistent queue.

Automatic sending happens only after the user opts in (auto_send), and then uses
the same allowlisted preview and single bounded send as a manual report. Automatic
reports are limited here, not in each browser window, so every window, the phone
view and app restarts share one record of what was already sent.

Preview freezes a tiny allowlisted JSON document in memory. Only a confirmed Send
with that preview's secret starts one bounded transport attempt. Retrying uses the
same bytes and per-report credential; nothing here runs on a collection tick.
"""

import copy
from datetime import datetime, timezone
import hashlib
import hmac
import json
import math
import re
import secrets
import threading
import time
import urllib.error
import urllib.request

import diagnostics

ENDPOINT = 'https://bloomformac.com/api/support/v1'
MAX_BYTES = 12 * 1024
MAX_JSON_DEPTH = 8
MAX_ACK_BYTES = 1024
MAX_PREVIEWS = 12
PREVIEW_SECONDS = 10 * 60
MAX_NEW_PER_HOUR = 5
# Automatic reports: the same problem at most once a day, the same kind of problem
# at most every six hours, and at most three a day in total.
AUTO_SAME_PROBLEM_SECONDS = 86400
AUTO_SAME_CATEGORY_SECONDS = 6 * 3600
AUTO_MAX_PER_DAY = 3
TIMEOUT = 15
CATEGORIES = {'manual', 'ui', 'connection', 'setup', 'model', 'action'}
CONTEXTS = {
    'overview',
    'setup',
    'models',
    'phone',
    'earnings',
    'hardware',
    'network',
    'help',
    'unknown',
}
ERRORS = {
    'certificate',
    'authentication',
    'rate_limit',
    'timeout',
    'permission',
    'connection',
    'memory',
    'other',
    'none',
}
MEMORY_BANDS = {'up-to-16', '17-32', '33-64', '65-128', 'over-128', 'unknown'}
VERSION = re.compile(r'\d{1,3}\.\d{1,3}\.\d{1,4}')
CHIP = re.compile(r'M\d{1,2}(?: Pro| Max| Ultra)?')


class SupportError(Exception):
    """Only fixed public messages reach the API, never transport/source errors."""

    MESSAGES = {
        'expired': (410, 'This review expired. Create and review a new report before sending.'),
        'busy': (409, 'A report is still being sent. Wait a moment before retrying.'),
        'rate_limited': (
            429,
            'Five new reports have been submitted in the last hour. Try again later.',
        ),
        'preview_limit': (
            429,
            'There are too many open report previews. Wait a few minutes and try again.',
        ),
        'unavailable': (503, 'Sending is unavailable here. No report was uploaded.'),
        'unconfirmed': (
            503,
            'Delivery could not be confirmed. Retry this same reviewed report to avoid duplicates.',
        ),
    }

    def __init__(self, status, report_id=None):
        self.status = status
        self.http_status, message = self.MESSAGES[status]
        self.response = {'status': status, 'error': message}
        if report_id:
            self.response['reportId'] = report_id
        super().__init__(message)


STALE_FAILURE_SECONDS = 86400


def loads(body):
    """Bound JSON before parsing; interpreter recursion limits are not a budget."""
    if not isinstance(body, bytes) or not 0 < len(body) <= MAX_BYTES:
        raise ValueError('Invalid support request.')

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate key.')
            result[key] = value
        return result

    def constant(_):
        raise ValueError('Invalid number.')

    try:
        text = body.decode('utf-8', errors='strict')
        depth, quoted, escaped = 0, False, False
        for char in text:
            if quoted:
                if escaped:
                    escaped = False
                elif char == '\\':
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char in '{[':
                depth += 1
                if depth > MAX_JSON_DEPTH:
                    raise ValueError('Support JSON is too deeply nested.')
            elif char in '}]':
                depth -= 1
                if depth < 0:
                    raise ValueError('Invalid support JSON.')
        return json.loads(text, object_pairs_hook=unique, parse_constant=constant)
    except (UnicodeError, RecursionError) as error:
        raise ValueError('Invalid support request.') from error


def _dict(value):
    return value if type(value) is dict else {}


def _enum(value, allowed, fallback='unknown'):
    return value if type(value) is str and value in allowed else fallback


def _version(value):
    return value if type(value) is str and VERSION.fullmatch(value) else 'unknown'


def _text(value, maximum, multiline=False):
    if type(value) is not str or len(value) > maximum:
        raise ValueError('Invalid support text.')
    if any((ord(c) < 32 and not (multiline and c in '\n\r\t')) or ord(c) == 127 for c in value):
        raise ValueError('Invalid support text.')
    value.encode('utf-8', errors='strict')
    return value


def summary(value, available=True):
    """Re-project every nested field; never forward the full diagnostic export."""
    data = _dict(value)
    app, hardware = _dict(data.get('app')), _dict(data.get('hardware'))
    provider, optimizer = _dict(data.get('provider')), _dict(data.get('optimizer'))
    failure, sources = _dict(optimizer.get('lastSwitchFailure')), _dict(data.get('sources'))
    age = failure.get('ageSeconds')
    if type(age) in (int, float) and age > STALE_FAILURE_SECONDS:
        # A day-old switch failure isn't what this report is about; old ones
        # otherwise ride along on every report, update after update.
        failure = {}
    os_version = app.get('macOS')
    os_major = (
        int(os_version.split('.')[0])
        if type(os_version) is str and re.fullmatch(r'\d{1,2}(?:\.\d{1,3}){0,2}', os_version)
        else 0
    )
    chip = hardware.get('chip')
    chip = chip.removeprefix('Apple ') if type(chip) is str else ''
    memory = hardware.get('memoryTotalGB')
    band = 'unknown'
    if type(memory) in (int, float) and math.isfinite(memory) and 0 < memory <= 4096:
        band = next(
            (
                label
                for ceiling, label in (
                    (16, 'up-to-16'),
                    (32, '17-32'),
                    (64, '33-64'),
                    (128, '65-128'),
                )
                if memory <= ceiling
            ),
            'over-128',
        )
    return {
        'available': bool(available),
        'osMajor': os_major,
        'chipFamily': chip if CHIP.fullmatch(chip) else 'Other',
        'memoryBand': band,
        'providerOnline': provider.get('online') if type(provider.get('online')) is bool else None,
        'providerVersion': _version(provider.get('version')),
        'optimizerMode': _enum(optimizer.get('mode'), diagnostics.MODES),
        'optimizerStatus': _enum(optimizer.get('status'), diagnostics.STATUSES),
        'failureCode': _enum(failure.get('code'), diagnostics.SWITCH_FAILURE_CODES)
        if failure
        else ('none' if available else 'unknown'),
        'recoveryCode': _enum(failure.get('recoveryCode'), diagnostics.RECOVERY_CODES)
        if failure
        else ('none' if available else 'unknown'),
        'sources': [
            {
                'name': name,
                'status': _enum(_dict(sources.get(name)).get('status'), diagnostics.STATUSES),
                'error': _enum(_dict(sources.get(name)).get('errorCategory'), ERRORS, 'none'),
            }
            for name in ('earnings', 'monitor', 'network')
        ],
    }


class _MemoryStore:
    """history.cache's interface, for a reporter without a collector (tests)."""

    def __init__(self):
        self.data = {}

    def __call__(self, key, data=None):
        if data is None:
            return self.data.get(key)
        self.data[key] = data
        return data


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _transport(url, body, headers, timeout):
    # A fixed destination, normal system TLS validation, no proxy credentials and
    # no redirects. Do not log response bodies, headers or raw exceptions.
    if url != ENDPOINT or len(body) > MAX_BYTES:
        raise ValueError('Invalid report destination.')
    request = urllib.request.Request(
        ENDPOINT, data=body, headers={**headers, 'User-Agent': 'BloomSupport/1.0'}, method='POST'
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.status != 200:
                return response.status, None
            payload = response.read(MAX_ACK_BYTES + 1)
            if len(payload) > MAX_ACK_BYTES:
                return 200, None
            return 200, loads(payload)
    except urllib.error.HTTPError as error:
        code = error.code
        error.close()
        return code, None


class SupportReports:
    def __init__(
        self,
        collector,
        network_enabled=True,
        transport=None,
        now=None,
        monotonic=None,
        capture=None,
        store=None,
    ):
        self.collector = collector
        # Small persistent records: the opt-in and what was sent automatically.
        self.store = store or (collector.history.cache if collector is not None else _MemoryStore())
        self.network_enabled = network_enabled
        self.transport = transport or _transport
        self.now = now or time.time
        self.clock = monotonic or time.monotonic
        self.capture = capture or (
            lambda remote: diagnostics.build_report(
                collector, include_earnings=False, remote=remote
            )
        )
        self.lock = threading.RLock()
        self.previews = {}
        self.submissions = []
        self.inflight = None
        self.closed = False

    AUTO_KEY = 'support-auto-send-v1'
    AUTO_SENT_KEY = 'support-auto-sent-v1'

    def auto_status(self):
        saved = self.store(self.AUTO_KEY)
        enabled = isinstance(saved, dict) and saved.get('enabled') is True
        return {'autoSend': enabled and self.network_enabled}

    def set_auto(self, data):
        """Opt in or out of sending future problem reports without a tap."""
        if (
            type(data) is not dict
            or set(data) != {'autoSend'}
            or type(data['autoSend']) is not bool
        ):
            raise ValueError('Choose whether to send reports automatically.')
        self.store(self.AUTO_KEY, {'enabled': data['autoSend'], 'changedAt': self.now()})
        return self.auto_status()

    @staticmethod
    def _problem(category, safe, version):
        # The page the user happened to be on and moment-to-moment status are left
        # out, so the same underlying problem counts once.
        key = [
            category,
            version,
            safe['providerOnline'],
            safe['failureCode'],
            safe['recoveryCode'],
            [(s['name'], s['error']) for s in safe['sources']],
        ]
        return hashlib.sha256(json.dumps(key).encode()).hexdigest()[:16]

    def _auto_sent(self):
        saved = self.store(self.AUTO_SENT_KEY)
        now = self.now()
        return [
            item
            for item in (saved if isinstance(saved, list) else [])[-50:]
            if isinstance(item, dict)
            and type(item.get('at')) in (int, float)
            and 0 <= now - item['at'] < AUTO_SAME_PROBLEM_SECONDS
            and type(item.get('category')) is str
            and type(item.get('problem')) is str
        ]

    def _auto_allowed(self, category, problem):
        sent, now = self._auto_sent(), self.now()
        return self.auto_status()['autoSend'] and not (
            len(sent) >= AUTO_MAX_PER_DAY
            or any(item['problem'] == problem for item in sent)
            or any(
                item['category'] == category and now - item['at'] < AUTO_SAME_CATEGORY_SECONDS
                for item in sent
            )
        )

    def _prune(self):
        now = self.clock()
        self.previews = {
            key: value
            for key, value in self.previews.items()
            if now < value['expires'] or key == self.inflight
        }
        self.submissions = [at for at in self.submissions if now - at < 3600]

    def preview(self, data, remote=False):
        fields = {'category', 'context', 'description', 'contact'}
        if type(data) is not dict or set(data) not in (fields, fields | {'automatic'}):
            raise ValueError('Invalid support preview.')
        automatic = data.get('automatic', False)
        if type(automatic) is not bool or (automatic and (data['description'] or data['contact'])):
            raise ValueError('Invalid support preview.')
        category, context = data['category'], data['context']
        if (
            _enum(category, CATEGORIES, '') != category
            or _enum(context, CONTEXTS, '') != context
            or not category
            or not context
        ):
            raise ValueError('Invalid support context.')
        description = _text(data['description'], 2000, multiline=True)
        contact = _text(data['contact'], 254)
        with self.lock:
            self._prune()
            if self.closed:
                raise SupportError('unavailable')
            if len(self.previews) >= MAX_PREVIEWS:
                raise SupportError('preview_limit')
            try:
                captured = self.capture(bool(remote))
                safe = summary(captured)
                version = _version(_dict(_dict(captured).get('app')).get('version'))
            except Exception:
                safe, version = summary({}, available=False), _version(diagnostics.app_version())
            problem = self._problem(category, safe, version) if automatic else None
            if automatic and not self._auto_allowed(category, problem):
                raise SupportError('rate_limited')
            report_id, review_token = secrets.token_hex(16), secrets.token_hex(32)
            report = {
                'schema': 1,
                'id': report_id,
                'generatedAt': datetime.fromtimestamp(self.now(), timezone.utc)
                .isoformat(timespec='seconds')
                .replace('+00:00', 'Z'),
                'appVersion': version,
                'surface': 'phone' if remote else 'mac',
                'category': category,
                'context': context,
                'description': description,
                'contact': contact,
                'diagnostics': safe,
            }
            body = json.dumps(
                report, ensure_ascii=False, allow_nan=False, separators=(',', ':')
            ).encode('utf-8')
            if len(body) > MAX_BYTES:
                raise ValueError('Support report is too large.')
            self.previews[report_id] = {
                'token': review_token,
                'secret': secrets.token_hex(32),
                'body': body,
                'remote': bool(remote),
                'expires': self.clock() + PREVIEW_SECONDS,
                'attempted': False,
                'sent': False,
                'automatic': automatic,
                'category': category,
                'problem': problem,
            }
            return {'report': copy.deepcopy(report), 'reviewToken': review_token}

    def send(self, data, remote=False, preview_only=False):
        if (
            type(data) is not dict
            or set(data) != {'reportId', 'reviewToken', 'confirmed'}
            or data['confirmed'] is not True
        ):
            raise ValueError('Review and confirm the support report first.')
        report_id, token = data['reportId'], data['reviewToken']
        if (
            type(report_id) is not str
            or not re.fullmatch(r'[0-9a-f]{32}', report_id)
            or type(token) is not str
            or not re.fullmatch(r'[0-9a-f]{64}', token)
        ):
            raise ValueError('Invalid support review.')
        with self.lock:
            self._prune()
            if preview_only or not self.network_enabled or self.closed:
                raise SupportError('unavailable', report_id)
            entry = self.previews.get(report_id)
            if (
                not entry
                or self.clock() >= entry['expires']
                or entry['remote'] != bool(remote)
                or not hmac.compare_digest(entry['token'], token)
            ):
                raise SupportError('expired', report_id)
            if entry['sent']:
                return {'status': 'sent', 'reportId': report_id}
            if self.inflight is not None:
                raise SupportError('busy', report_id)
            if not entry['attempted'] and len(self.submissions) >= MAX_NEW_PER_HOUR:
                raise SupportError('rate_limited', report_id)
            if not entry['attempted'] and entry['automatic']:
                # Checked again here: two windows can preview the same problem at once.
                if not self._auto_allowed(entry['category'], entry['problem']):
                    raise SupportError('rate_limited', report_id)
                sent = {'at': self.now(), 'category': entry['category'], 'problem': entry['problem']}
                self.store(self.AUTO_SENT_KEY, self._auto_sent() + [sent])
            if not entry['attempted']:
                self.submissions.append(self.clock())
                entry['attempted'] = True
            self.inflight = report_id
            finished = threading.Event()

            def attempt():
                accepted = False
                try:
                    status, ack = self.transport(
                        ENDPOINT,
                        entry['body'],
                        {
                            'Authorization': 'Bearer ' + entry['secret'],
                            'Content-Type': 'application/json',
                        },
                        TIMEOUT,
                    )
                    accepted = (
                        type(status) is int
                        and status == 200
                        and type(ack) is dict
                        and set(ack) == {'status', 'reportId'}
                        and ack['status'] == 'sent'
                        and ack['reportId'] == report_id
                    )
                except Exception:
                    pass
                finally:
                    with self.lock:
                        entry['sent'] = accepted
                        self.inflight = None
                    finished.set()

            # At most one worker exists, only after explicit confirmation. The
            # wait cap includes DNS/connect/body stalls. An unresolved attempt is
            # not replaced or queued; a late matching acknowledgement is retained.
            worker = threading.Thread(target=attempt, name='Bloomkeeper support send', daemon=True)
            try:
                worker.start()
            except Exception:
                self.inflight = None
                raise SupportError('unavailable', report_id) from None
        if not finished.wait(TIMEOUT):
            raise SupportError('unconfirmed', report_id)
        with self.lock:
            if entry['sent']:
                return {'status': 'sent', 'reportId': report_id}
        raise SupportError('unconfirmed', report_id)

    def close(self):
        with self.lock:
            self.closed = True
            self.previews.clear()
