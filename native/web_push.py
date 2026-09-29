"""Private, account-scoped Web Push subscriptions and bounded alert delivery.

Encryption and VAPID are supplied by pywebpush/py-vapid/cryptography. The custom
transport only restricts destinations, redirects, proxies, response size and
timeouts; it uses Python's certificate-verified HTTPS, not custom cryptography.
"""

import base64
import copy
import hashlib
import ipaddress
import json
import math
import os
import pathlib
import queue
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import uuid
import warnings


MAX_SUBSCRIPTIONS = 8
SCREENS = ('overview', 'test', 'demand')  # screens a notification may open (push-sw.js)
MAX_TOTAL_SUBSCRIPTIONS = 32
PUSH_REASONS = frozenset(
    (
        'BadTtl',
        'BadUrgency',
        'BadWebPushRequest',
        'BadWebPushTopic',
        'VapidPkHashMismatch',
        'IdleTimeout',
        'BadAuthorizationHeader',
        'BadJwtToken',
        'BadVapidPublicKey',
        'BadPath',
        'MethodNotAllowed',
        'PayloadTooLarge',
        'TooManyRequests',
        'InternalServerError',
        'ServiceUnavailable',
        'Shutdown',
    )
)


def safe_reason(value):
    return value if isinstance(value, str) and value in PUSH_REASONS else None


def validate_contact(value):
    """VAPID needs a real operator contact, never a localhost placeholder."""
    if (
        not isinstance(value, str)
        or not 5 <= len(value) <= 512
        or any(ord(c) < 33 or ord(c) > 126 for c in value)
    ):
        raise ValueError('Use your email address or a public HTTPS contact page.')
    value = value if ':' in value else 'mailto:' + value
    try:
        parsed = urlsplit(value)
        if parsed.scheme == 'mailto':
            if (
                parsed.netloc
                or parsed.query
                or parsed.fragment
                or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+", parsed.path)
            ):
                raise ValueError()
            host = parsed.path.rsplit('@', 1)[1].lower()
        elif parsed.scheme == 'https':
            if (
                parsed.username
                or parsed.password
                or parsed.port not in (None, 443)
                or parsed.fragment
            ):
                raise ValueError()
            host = parsed.hostname or ''
        else:
            raise ValueError()
        if (
            '.' not in host
            or host.endswith(('.localhost', '.local', '.internal', '.invalid', '.test', '.example'))
            or host in ('localhost', 'localhost.invalid')
            or any(
                not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                for label in host.split('.')
            )
        ):
            raise ValueError()
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return value
    except (ValueError, TypeError):
        pass
    raise ValueError(
        'Use your email address or a public HTTPS contact page; local placeholders are rejected by Apple.'
    )


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def libraries():
    import sys

    vendor = str(pathlib.Path(__file__).resolve().parent / 'push_vendor')
    if vendor not in sys.path:
        sys.path.insert(0, vendor)
    # Apple's Python3.9 has LibreSSL. We do not use urllib3's transport: the
    # bounded adapter below uses certificate-verified stdlib HTTPS directly.
    with warnings.catch_warnings():
        warnings.filterwarnings('ignore', message='urllib3 v2 only supports OpenSSL.*')
        from pywebpush import webpush
        from py_vapid import Vapid
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import serialization
        import certifi
    return webpush, Vapid, ec, serialization, certifi


def endpoint(value):
    if (
        not isinstance(value, str)
        or not 20 <= len(value) <= 2048
        or any(ord(c) < 33 or ord(c) > 126 for c in value)
    ):
        raise ValueError('Invalid push endpoint.')
    try:
        url = urlsplit(value)
        host = url.hostname or ''
        apple = bool(re.fullmatch(r'[a-z0-9-]+\.push\.apple\.com', host))
        allowed = apple or host in ('updates.push.services.mozilla.com', 'fcm.googleapis.com')
        if (
            url.scheme != 'https'
            or not allowed
            or url.port not in (None, 443)
            or url.username
            or url.password
            or url.fragment
            or not url.path.startswith('/')
            or url.path == '/'
            or '\\' in value
        ):
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('Use a subscription from Safari, Chrome or Firefox.') from None
    return value


def validate_subscription(value):
    if not isinstance(value, dict) or set(value) - {'endpoint', 'keys', 'expirationTime'}:
        raise ValueError('Invalid push subscription.')
    target = endpoint(value.get('endpoint'))
    keys = value.get('keys')
    if not isinstance(keys, dict) or set(keys) != {'auth', 'p256dh'}:
        raise ValueError('Invalid push subscription keys.')
    decoded = {}
    for key, length in (('auth', 16), ('p256dh', 65)):
        text = keys.get(key)
        if not isinstance(text, str) or not re.fullmatch(r'[A-Za-z0-9_-]{20,90}={0,2}', text):
            raise ValueError('Invalid push subscription keys.')
        try:
            raw = base64.b64decode(
                text.rstrip('=') + '=' * (-len(text.rstrip('=')) % 4), altchars=b'-_', validate=True
            )
        except (ValueError, TypeError):
            raise ValueError('Invalid push subscription keys.') from None
        if len(raw) != length:
            raise ValueError('Invalid push subscription keys.')
        decoded[key] = raw
    _, _, ec, _, _ = libraries()
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), decoded['p256dh'])
    except ValueError:
        raise ValueError('Invalid browser public key.') from None
    return {'endpoint': target, 'keys': dict(keys)}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class PushResponse:
    def __init__(self, code, headers, reason=None):
        self.status_code, self.headers = code, headers
        self.reason, self.text = 'Push service response', ''
        self.service_reason = safe_reason(reason)


class PushTransport:
    def post(self, url, data=None, headers=None, timeout=8, **kwargs):
        endpoint(url)
        host = urlsplit(url).hostname
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(r[4][0]).is_global for r in addresses):
            raise ValueError('Push service address is unavailable.')
        certifi = libraries()[4]
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where())),
        )
        request = urllib.request.Request(url, data=data, headers=headers or {}, method='POST')
        try:
            with opener.open(request, timeout=min(float(timeout or 8), 8)) as response:
                return PushResponse(response.status, dict(response.headers))
        except urllib.error.HTTPError as error:
            reason = None
            try:
                body = json.loads(error.read(1024))
                if isinstance(body, dict):
                    reason = safe_reason(body.get('reason'))
            except (OSError, ValueError, TypeError):
                pass
            error.close()
            return PushResponse(error.code, dict(error.headers), reason)


def send_webpush(subscription, payload, private_key, contact):
    contact = validate_contact(contact)
    webpush, Vapid, _, _, _ = libraries()
    # A new claims dict per recipient prevents reusing a different service's aud.
    return webpush(
        subscription_info=subscription,
        data=json.dumps(payload, separators=(',', ':')),
        vapid_private_key=Vapid.from_pem(private_key.encode()),
        vapid_claims={'sub': contact},
        content_encoding='aes128gcm',
        ttl=600,
        headers={'Urgency': 'normal'},
        timeout=8,
        requests_session=PushTransport(),
    )


class DeliveryError(Exception):
    def __init__(self, code=None, retry_after=None, reason=None):
        self.response = PushResponse(
            code, {'Retry-After': retry_after} if retry_after is not None else {}, reason
        )


def bounded_sender(subscription, payload, private_key, contact):
    # A subprocess deadline also bounds system DNS resolution and TLS setup.
    # Secrets travel only through stdin, never command-line arguments or logs.
    # Isolated Python ignores PYTHONDONTWRITEBYTECODE; protect the signed bundle.
    try:
        result = subprocess.run(
            [sys.executable, '-I', '-B', str(pathlib.Path(__file__).resolve()), '--deliver'],
            input=json.dumps(
                {
                    'subscription': subscription,
                    'payload': payload,
                    'privateKey': private_key,
                    'contact': validate_contact(contact),
                }
            ),
            capture_output=True,
            text=True,
            timeout=15,
        )
        reply = (
            json.loads(result.stdout)
            if result.returncode == 0 and len(result.stdout) < 1024
            else {}
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise DeliveryError() from None
    reply = reply if isinstance(reply, dict) else {}
    code = reply.get('statusCode')
    code = code if type(code) is int and 100 <= code <= 599 else None
    if type(code) is not int or not 200 <= code < 300:
        raise DeliveryError(code, reply.get('retryAfter'), reply.get('reason'))
    return PushResponse(code, {})


class WebPush:
    def __init__(self, data_dir, stop=None, sender=None, clock=time.time):
        self.path = pathlib.Path(data_dir) / 'web-push.json' if data_dir is not None else None
        self.stop = stop if stop is not None else threading.Event()
        self.sender, self.clock = sender, clock
        self.lock = threading.RLock()
        self.queue = queue.Queue(maxsize=32)
        self.state, self.problem, self.thread = None, None, None
        self.inflight = set()

    def load(self):
        if self.state is not None:
            return True
        if self.path is None:
            self.problem = 'Notifications are available in the installed app.'
            return False
        try:
            _, Vapid, _, serialization, _ = libraries()
            if self.path.exists():
                if self.path.is_symlink() or self.path.stat().st_size > 256 * 1024:
                    raise ValueError()
                state = json.loads(self.path.read_text())
                if (
                    state.get('version') != 1
                    or not isinstance(state.get('subscriptions'), dict)
                    or not isinstance(state.get('events'), dict)
                ):
                    raise ValueError()
                if (
                    len(state['subscriptions']) > MAX_TOTAL_SUBSCRIPTIONS
                    or len(state['events']) > 512
                ):
                    raise ValueError()
                for key, record in state['subscriptions'].items():
                    if (
                        not isinstance(record, dict)
                        or record.get('id') != key
                        or not re.fullmatch(r'[a-f0-9]{64}', record.get('account', ''))
                    ):
                        raise ValueError()
                    if digest(validate_subscription(record.get('subscription'))['endpoint']) != key:
                        raise ValueError()
                    if record.get('status') not in ('subscribed', 'delayed', 'expired'):
                        raise ValueError()
                    for field in (
                        'createdAt',
                        'lastAttemptAt',
                        'lastSentAt',
                        'retryAfter',
                        'lastTestAt',
                        'lastTestResultAt',
                    ):
                        value = record.get(field)
                        if value is not None and (
                            type(value) not in (int, float) or not math.isfinite(value) or value < 0
                        ):
                            raise ValueError()
                    if record.get('lastError') is not None and not isinstance(
                        record['lastError'], str
                    ):
                        raise ValueError()
                    if record.get('lastTestError') is not None and not isinstance(
                        record['lastTestError'], str
                    ):
                        raise ValueError()
                    code = record.get('lastStatusCode')
                    if code is not None and (type(code) is not int or not 100 <= code <= 599):
                        raise ValueError()
                    record['lastReason'] = safe_reason(record.get('lastReason'))
                if any(
                    not re.fullmatch(r'[a-f0-9]{64}', key)
                    or type(value) not in (int, float)
                    or not math.isfinite(value)
                    for key, value in state['events'].items()
                ):
                    raise ValueError()
                if self.path.stat().st_mode & 0o077:
                    raise ValueError()
                vapid = Vapid.from_pem(state['privateKey'].encode())
                public = (
                    base64.urlsafe_b64encode(
                        vapid.public_key.public_bytes(
                            serialization.Encoding.X962,
                            serialization.PublicFormat.UncompressedPoint,
                        )
                    )
                    .decode()
                    .rstrip('=')
                )
                if public != state['publicKey']:
                    raise ValueError()
                if state.get('contact') is not None:
                    state['contact'] = validate_contact(state['contact'])
                self.state = state
                pending = state.setdefault('pendingSwitches', {})
                if not isinstance(pending, dict) or len(pending) > 32:
                    raise ValueError()
                for key, item in pending.items():
                    if (
                        not re.fullmatch(r'[a-f0-9]{64}', key)
                        or not isinstance(item, dict)
                        or not re.fullmatch(r'[a-f0-9]{64}', item.get('scope', ''))
                        or type(item.get('createdAt')) not in (int, float)
                        or not math.isfinite(item['createdAt'])
                        or not isinstance(item.get('remaining'), list)
                        or len(item['remaining']) > MAX_SUBSCRIPTIONS
                        or any(
                            not isinstance(k, str) or not re.fullmatch(r'[a-f0-9]{64}', k)
                            for k in item['remaining']
                        )
                        or not isinstance(item.get('payload'), dict)
                        or item['payload'].get('tag') != 'bloom-switch-' + key[:24]
                        or any(
                            not isinstance(item['payload'].get(k), str)
                            or len(item['payload'][k]) > limit
                            for k, limit in [('title', 100), ('body', 240)]
                        )
                        or set(item['payload']) - {'title', 'body', 'tag', 'screen'}
                        or item['payload'].get('screen', 'test') not in SCREENS
                    ):
                        raise ValueError()
            else:
                vapid = Vapid()
                vapid.generate_keys()
                public = (
                    base64.urlsafe_b64encode(
                        vapid.public_key.public_bytes(
                            serialization.Encoding.X962,
                            serialization.PublicFormat.UncompressedPoint,
                        )
                    )
                    .decode()
                    .rstrip('=')
                )
                self.state = {
                    'version': 1,
                    'privateKey': vapid.private_pem().decode(),
                    'publicKey': public,
                    'subscriptions': {},
                    'events': {},
                    'pendingSwitches': {},
                }
                self.save()
            self.problem = None
            return True
        except (ImportError, OSError, ValueError, TypeError, KeyError, AttributeError):
            self.state = None
            self.problem = 'Notification keys or dependencies could not be loaded. Reopen BloomGauge on the Mac.'
            return False

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_name('.web-push-' + uuid.uuid4().hex + '.tmp')
        try:
            fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as output:
                json.dump(self.state, output, separators=(',', ':'), allow_nan=False)
                output.flush()
                os.fsync(output.fileno())
            if self.path.is_symlink():
                raise OSError('Notification storage must not be a symbolic link.')
            os.replace(str(temporary), str(self.path))
        finally:
            if temporary.exists():
                temporary.unlink()

    def status(self, account):
        with self.lock:
            supported = self.load()
            records = (
                [
                    r
                    for r in (self.state or {}).get('subscriptions', {}).values()
                    if r.get('account') == digest(account)
                ]
                if account
                else []
            )
            subscriptions = [
                {
                    k: r.get(k)
                    for k in (
                        'id',
                        'status',
                        'lastAttemptAt',
                        'lastSentAt',
                        'lastError',
                        'lastStatusCode',
                        'lastReason',
                        'lastTestAt',
                        'lastTestResultAt',
                        'lastTestError',
                        'retryAfter',
                    )
                }
                for r in records
            ]
            latest = max(records, key=lambda r: r.get('lastAttemptAt') or 0) if records else {}
            configured = bool(supported and self.state.get('contact'))
            return {
                'supported': supported,
                'publicKey': self.state['publicKey'] if supported else None,
                'contactConfigured': configured,
                'subscriptionCount': sum(r.get('status') != 'expired' for r in records),
                'subscriptions': subscriptions,
                'lastAttemptAt': latest.get('lastAttemptAt'),
                'lastSentAt': max((r.get('lastSentAt') or 0 for r in records), default=0) or None,
                'lastError': latest.get('lastError'),
                'categories': {'demandSpikes': False, 'modelSwitches': True},
                'detail': self.problem
                or (
                    'Alerts you turn on under Notifications for the phone: model switches, problems, earnings and more.'
                    if configured
                    else 'Push delivery needs a valid sender contact configured on the Mac. Phone permission and subscriptions are preserved.'
                ),
            }

    def configure_contact(self, contact):
        contact = validate_contact(contact)
        with self.lock:
            if not self.load():
                raise ValueError(self.problem)
            self.state['contact'] = contact
            self.save()

    def test_notification(self, account, subscription_id):
        """An explicit test targets only this owner's selected device."""
        with self.lock:
            if not account or not self.load() or not self.state.get('contact'):
                raise ValueError('Configure a valid push sender contact on the Mac first.')
            record = (
                self.state['subscriptions'].get(subscription_id)
                if isinstance(subscription_id, str)
                else None
            )
            if (
                not record
                or record.get('account') != digest(account)
                or record.get('status') == 'expired'
            ):
                raise ValueError('Enable notifications on this device before sending a test.')
            now = self.clock()
            if now - (record.get('lastTestAt') or 0) < 60 or record.get('retryAfter', 0) > now:
                raise ValueError(
                    'Wait for the notification retry delay before sending another test.'
                )
            if self.stop.is_set() or self.queue.full():
                raise ValueError('Notification delivery is busy. Try again shortly.')
            record.update(lastTestAt=now, lastTestResultAt=None, lastTestError=None)
            self.save()
            payload = {
                'title': 'BloomGauge · notification test',
                'body': 'Test notification from BloomGauge. Tap to open the dashboard.',
                'tag': 'bloom-switch-' + uuid.uuid4().hex[:24],
            }
            self.queue.put_nowait((digest(account), payload, subscription_id))
            return {**self.status(account), 'testQueued': True}

    def subscribe(self, account, subscription):
        if not isinstance(account, str) or not account:
            raise ValueError('A connected account is required.')
        subscription = validate_subscription(subscription)
        with self.lock:
            if not self.load():
                raise ValueError(self.problem)
            key, scope = digest(subscription['endpoint']), digest(account)
            records = self.state['subscriptions']
            previous = records.get(key) or {}
            if previous.get('account') == scope and previous.get('subscription') == subscription:
                # Opening the page renews registration idempotently. It must not
                # erase delivery history, expiry or a push service's retry delay.
                return {**self.status(account), 'subscriptionId': key}
            if key not in records:
                for expired in [k for k, r in records.items() if r.get('status') == 'expired']:
                    del records[expired]
                if (
                    len(records) >= MAX_TOTAL_SUBSCRIPTIONS
                    or sum(r.get('account') == scope for r in records.values()) >= MAX_SUBSCRIPTIONS
                ):
                    raise ValueError(
                        'Remove an old notification subscription before adding another device.'
                    )
            records[key] = {
                'id': key,
                'account': scope,
                'subscription': subscription,
                'status': 'subscribed',
                'createdAt': previous.get('createdAt', self.clock()),
                'lastAttemptAt': None,
                'lastSentAt': None,
                'lastError': None,
                'retryAfter': 0,
            }
            self.save()
            return {**self.status(account), 'subscriptionId': key}

    def unsubscribe(self, account, target):
        target = endpoint(target)
        with self.lock:
            if not self.load():
                raise ValueError(self.problem)
            key = digest(target)
            if self.state['subscriptions'].get(key, {}).get('account') == digest(account):
                del self.state['subscriptions'][key]
                self.save()

            return self.status(account)

    def start(self):
        with self.lock:
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self.run, daemon=True, name='bloom-web-push')
                self.thread.start()

    def ready(self, account):
        """A phone of this account can receive alerts. Never creates push keys."""
        with self.lock:
            if self.state is None and (self.path is None or not self.path.exists()):
                return False
            if not account or not self.load() or not self.state.get('contact'):
                return False
            scope = digest(account)
            return any(
                r.get('account') == scope and r.get('status') != 'expired'
                for r in self.state['subscriptions'].values()
            )

    def has_event(self, account, event_id):
        with self.lock:
            return bool(
                self.state and digest(account + ':' + str(event_id)) in self.state['events']
            )

    def enqueue_switch(self, account, event_id, previous, model, reason='automatic'):
        if (
            not account
            or not isinstance(event_id, (str, int))
            or len(str(event_id)) > 256
            or not isinstance(model, str)
        ):
            return False
        from switch_alerts import REASONS

        def name(value):
            return (
                re.sub(r'[^A-Za-z0-9 ._+/-]', '', value).strip()[:50]
                if isinstance(value, str)
                else ''
            )

        old, new = name(previous), name(model)
        if not old or not new or previous == model:
            return False
        key = digest(account + ':' + str(event_id))
        payload = {
            'title': 'BloomGauge · model switched',
            'body': (old + ' → ' + new + '. ' + REASONS.get(reason, REASONS['automatic']))[:240],
            'tag': 'bloom-switch-' + key[:24],
        }
        return self.enqueue_payload(account, key, payload)

    def enqueue_notice(self, account, event_id, title, body, screen=None):
        """A one-off notice (e.g. work stopped arriving), same delivery as switches.
        screen: the dashboard screen a tap opens (push-sw.js allow-list; default test)."""
        if not account or not isinstance(event_id, (str, int)) or len(str(event_id)) > 256:
            return False
        if (
            not isinstance(title, str)
            or not isinstance(body, str)
            or not title.strip()
            or not body.strip()
        ):
            return False
        key = digest(account + ':' + str(event_id))
        # Saved pending items and the service worker accept only this tag form.
        payload = {
            'title': title.strip()[:60],
            'body': body.strip()[:240],
            'tag': 'bloom-switch-' + key[:24],
        }
        if screen in SCREENS:
            payload['screen'] = screen
        return self.enqueue_payload(account, key, payload)

    def enqueue_payload(self, account, key, payload):
        with self.lock:
            # No one has opted in yet: scanning must not initialize push state.
            if self.state is None and (self.path is None or not self.path.exists()):
                return False
            if not self.load() or not self.state.get('contact') or self.stop.is_set():
                return False
            scope = digest(account)
            if key in self.state['events'] or not any(
                r.get('account') == scope and r.get('status') != 'expired'
                for r in self.state['subscriptions'].values()
            ):
                return False
            if self.queue.full() or len(self.state.setdefault('pendingSwitches', {})) >= 32:
                return False
            self.state['events'][key] = self.clock()
            self.state['events'] = dict(
                sorted(self.state['events'].items(), key=lambda item: item[1])[-512:]
            )
            self.state['pendingSwitches'][key] = {
                'scope': scope,
                'payload': payload,
                'createdAt': self.clock(),
                'remaining': [
                    k
                    for k, r in self.state['subscriptions'].items()
                    if r.get('account') == scope and r.get('status') != 'expired'
                ],
            }
            self.save()
            self.inflight.add(payload['tag'])
            self.queue.put_nowait((scope, payload, None))
            return True

    def resume_pending(self):
        with self.lock:
            if self.state is None and (self.path is None or not self.path.exists()):
                return
            if not self.load() or not self.state.get('contact') or self.stop.is_set():
                return
            now = self.clock()
            changed = False
            for key, item in list(self.state.setdefault('pendingSwitches', {}).items()):
                records = self.state['subscriptions']
                remaining = [
                    k
                    for k in item['remaining']
                    if k in records
                    and records[k].get('account') == item['scope']
                    and records[k].get('status') != 'expired'
                ]
                if not remaining or not 0 <= now - item['createdAt'] <= 900:
                    del self.state['pendingSwitches'][key]
                    changed = True
                    continue
                if remaining != item['remaining']:
                    item['remaining'] = remaining
                    changed = True
                if (
                    item['payload']['tag'] not in self.inflight
                    and not self.queue.full()
                    and any(records[k].get('retryAfter', 0) <= now for k in remaining)
                ):
                    self.inflight.add(item['payload']['tag'])
                    self.queue.put_nowait((item['scope'], item['payload'], None))
            if changed:
                self.save()

    def run(self):
        while not self.stop.is_set():
            try:
                self.resume_pending()
            except Exception:
                with self.lock:
                    self.problem = 'Saved notifications could not resume. BloomGauge will retry.'
            try:
                scope, payload, target = self.queue.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self.deliver(scope, payload, target)
            except Exception:
                # Never log subscription URLs, keys or upstream response text.
                with self.lock:
                    self.problem = (
                        'Notification delivery could not finish. Check the next alert status.'
                    )
            finally:
                with self.lock:
                    self.inflight.discard(payload.get('tag'))
                self.queue.task_done()

    def deliver(self, scope, payload, target=None):
        with self.lock:
            now = self.clock()
            pending_key = next(
                (
                    k
                    for k, v in self.state.get('pendingSwitches', {}).items()
                    if v['scope'] == scope and v['payload']['tag'] == payload.get('tag')
                ),
                None,
            )
            pending = self.state.get('pendingSwitches', {}).get(pending_key)
            if target is None and payload.get('tag', '').startswith('bloom-switch-'):
                # A stale queue item must not become a fresh broadcast after
                # its durable entry expires or the last recipient unsubscribes.
                if pending is None:
                    return
                if not 0 <= now - pending['createdAt'] <= 900:
                    del self.state['pendingSwitches'][pending_key]
                    self.save()
                    return
            targets = [
                (key, copy.deepcopy(r))
                for key, r in self.state['subscriptions'].items()
                if r.get('account') == scope
                and (target is None or key == target)
                and (pending is None or key in pending['remaining'])
                and r.get('status') != 'expired'
                and r.get('retryAfter', 0) <= now
            ]
            private = self.state['privateKey']
            contact = self.state.get('contact')
            if not contact:
                return
        for key, record in targets:
            if self.stop.is_set():
                return
            now, code, retry, reason = self.clock(), None, 0, None
            try:
                subscription = validate_subscription(record['subscription'])
                response = (
                    self.sender(subscription, payload, private)
                    if self.sender
                    else bounded_sender(subscription, payload, private, contact)
                )
                code = response.status_code
                reason = getattr(response, 'service_reason', None)
            except Exception as error:
                response = getattr(error, 'response', None)
                code = getattr(response, 'status_code', None)
                reason = getattr(response, 'service_reason', None)
                try:
                    retry = min(
                        86400,
                        max(
                            60,
                            int((getattr(response, 'headers', {}) or {}).get('Retry-After', 300)),
                        ),
                    )
                except (ValueError, TypeError):
                    retry = 300
            code = code if type(code) is int and 100 <= code <= 599 else None
            success = code is not None and 200 <= code < 300
            expired = code in (404, 410)
            reason = safe_reason(reason)
            message = (
                None
                if success
                else 'Browser subscription expired. Enable notifications again on this device.'
                if expired
                else (
                    'Push authentication was rejected'
                    + (' (' + reason + ')' if reason else '')
                    + '. Check the sender contact and push keys on the Mac.'
                    if code in (401, 403)
                    else 'Push service delayed delivery'
                    + (' (HTTP ' + str(code) + ')' if type(code) is int else '')
                    + '. Recent switch alerts retry for up to 15 minutes.'
                )
            )
            with self.lock:
                current = self.state['subscriptions'].get(key)
                # Unsubscribing or changing accounts while sending wins.
                if (
                    not current
                    or current.get('account') != scope
                    or current.get('subscription') != record['subscription']
                ):
                    continue
                current.update(
                    lastAttemptAt=now,
                    lastError=message,
                    lastStatusCode=code if type(code) is int else None,
                    lastReason=reason,
                    status='subscribed' if success else 'expired' if expired else 'delayed',
                    retryAfter=0 if success or expired else now + (retry or 300),
                )
                if success:
                    current['lastSentAt'] = now
                if target:
                    current.update(lastTestResultAt=now, lastTestError=message)
                if pending_key and (success or expired):
                    item = self.state.get('pendingSwitches', {}).get(pending_key)
                    if item and key in item['remaining']:
                        item['remaining'].remove(key)
                        if not item['remaining']:
                            del self.state['pendingSwitches'][pending_key]
                self.save()
                self.problem = None


if __name__ == '__main__' and sys.argv[1:] == ['--deliver']:
    reply = {}
    try:
        data = json.loads(sys.stdin.read(8193))
        response = send_webpush(
            validate_subscription(data['subscription']),
            data['payload'],
            data['privateKey'],
            data['contact'],
        )
        reply = {'statusCode': response.status_code}
    except Exception as error:
        response = getattr(error, 'response', None)
        code = getattr(response, 'status_code', None)
        if type(code) is int:
            reply['statusCode'] = code
        reason = getattr(response, 'service_reason', None)
        if safe_reason(reason):
            reply['reason'] = safe_reason(reason)
        retry_after = (getattr(response, 'headers', {}) or {}).get('Retry-After')
        if isinstance(retry_after, str) and retry_after.isdigit() and len(retry_after) < 8:
            reply['retryAfter'] = retry_after
    sys.stdout.write(json.dumps(reply))
