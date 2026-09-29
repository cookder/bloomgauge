"""Optional, off by default: share this Mac's per-model pay curves so BloomGauge's
starting estimates (shared-priors.json) improve for everyone.

A summary holds, per model with 2+ steady warm hours in the last 30 days, the
fitted curve (level, exponent, typical spread), warm hours and period count,
plus chip family, memory band and BloomGauge version. No account or device IDs,
balances, times or individual payments. A random ID and secret, separate from
every other BloomGauge ID, let this Mac replace its summary or delete it.

At most one send a week, and once right after opting in. Turning it off deletes
the website's copy first; nothing is queued in the background.
"""

import json
import re
import secrets
import threading
import time
import urllib.error
import urllib.request

import demand_curves as curves
from demand_baselines import read_view, network_minutes
from earnings_forecast import read_paid_evidence

ENDPOINT = 'https://bloomformac.com/api/pay-summaries/v1'
CACHE_KEY = 'pay-sharing-v1'
TIMEOUT = 15
SEND_EVERY = 7 * 86400
RETRY_AFTER = 6 * 3600
MAX_MODELS = 32
VERSION = re.compile(r'\d{1,3}\.\d{1,3}\.\d{1,4}')


class SharingError(Exception):
    MESSAGES = {
        'phone': (403, 'Change pay summary sharing in BloomGauge on your Mac.'),
        'unavailable': (503, 'Sharing isn’t available in this copy of BloomGauge.'),
        'unconfirmed': (
            503,
            'bloomformac.com didn’t confirm the change. Nothing changed; try again in a moment.',
        ),
        'busy': (409, 'A change is still being sent. Wait a moment and try again.'),
    }

    def __init__(self, status):
        self.status = status
        self.http_status, message = self.MESSAGES[status]
        super().__init__(message)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _transport(method, url, body, headers, timeout):
    if url != ENDPOINT or method not in ('POST', 'DELETE'):
        raise ValueError('Unsupported pay summary request.')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    request = urllib.request.Request(
        url, data=body, headers={**headers, 'User-Agent': 'BloomPriors/1.0'}, method=method
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as error:
        code = error.code
        error.close()
        return code


class PaySharing:
    def __init__(
        self, collector, network_enabled=True, transport=None, now=None, curves_source=None
    ):
        self.collector = collector
        self.network_enabled = network_enabled
        self.transport = transport or _transport
        self.now = now or time.time
        self.curves_source = curves_source or self._own_curves
        self.lock = threading.Lock()

    def _saved(self):
        saved = self.collector.history.cache(CACHE_KEY)
        if (
            isinstance(saved, dict)
            and saved.get('enabled') is True
            and isinstance(saved.get('id'), str)
            and re.fullmatch(r'[0-9a-f]{32}', saved['id'])
            and isinstance(saved.get('secret'), str)
            and re.fullmatch(r'[0-9a-f]{64}', saved['secret'])
        ):
            return saved
        return None

    def status(self, remote=False):
        saved = self._saved()
        return {
            'schema': 1,
            'enabled': bool(saved),
            'lastSentAt': saved.get('lastSentAt') if saved else None,
            'models': saved.get('models', 0) if saved else 0,
            'canChange': not remote and self.network_enabled,
        }

    def _own_curves(self, now):
        account, device = self.collector.demand_identity()
        if not account or not device:
            return {}
        store = self.collector.optimizer.store
        with read_view(store) as view:
            evidence = read_paid_evidence(
                view, account, device, now - curves.LOOKBACK_SECONDS, now, now
            )
            models = sorted(m for m in evidence if len(curves.members(m)) == 1)[:256]
            network = network_minutes(view, models, now - curves.LOOKBACK_SECONDS, now - 120)
        return curves.summary(evidence, network, now)

    def payload(self, ident, now):
        own = self.curves_source(now)
        models = {
            m: c
            for m, c in sorted(own.items(), key=lambda kv: -kv[1]['hours'])[:MAX_MODELS]
            if isinstance(m, str) and 0 < len(m) <= 200
        }
        meta = {}
        try:
            meta = self.collector.usage.metadata()
        except Exception:
            pass
        version = getattr(getattr(self.collector, 'usage', None), 'version', 'unknown')
        return {
            'schema': 1,
            'id': ident['id'],
            'appVersion': version
            if isinstance(version, str) and VERSION.fullmatch(version)
            else 'unknown',
            'chipFamily': meta.get('chipFamily')
            if isinstance(meta.get('chipFamily'), str)
            else 'Other',
            'memoryBand': meta.get('memoryBand')
            if isinstance(meta.get('memoryBand'), str)
            else 'unknown',
            'models': models,
        }

    def _send(self, method, payload, secret):
        body = json.dumps(
            payload, ensure_ascii=False, allow_nan=False, separators=(',', ':')
        ).encode('utf-8')
        try:
            return self.transport(
                method,
                ENDPOINT,
                body,
                {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + secret},
                TIMEOUT,
            )
        except Exception:
            return None

    def action(self, data, remote=False):
        if remote:
            raise SharingError('phone')
        if (
            not isinstance(data, dict)
            or set(data) != {'enabled'}
            or type(data['enabled']) is not bool
        ):
            raise ValueError('Choose whether to share pay summaries.')
        if not self.network_enabled:
            raise SharingError('unavailable')
        if not self.lock.acquire(blocking=False):
            raise SharingError('busy')
        try:
            saved = self._saved()
            if not data['enabled']:
                if saved and self._send(
                    'DELETE', {'schema': 1, 'id': saved['id']}, saved['secret']
                ) not in (204, 404):
                    raise SharingError('unconfirmed')
                self.collector.history.cache(CACHE_KEY, {'enabled': False, 'changedAt': self.now()})
                return self.status()
            ident = saved or {'id': secrets.token_hex(16), 'secret': secrets.token_hex(32)}
            now = self.now()
            payload = self.payload(ident, now)
            # Opting in is confirmed by the website even before this Mac has curves to share.
            if self._send('POST', payload, ident['secret']) != 200:
                raise SharingError('unconfirmed')
            self.collector.history.cache(
                CACHE_KEY,
                {
                    'enabled': True,
                    'id': ident['id'],
                    'secret': ident['secret'],
                    'lastSentAt': now,
                    'attemptAt': now,
                    'models': len(payload['models']),
                },
            )
            return self.status()
        finally:
            self.lock.release()

    def tick(self):
        """Weekly refresh while sharing is on. Failures wait six hours; nothing is queued."""
        if not self.network_enabled or not self.lock.acquire(blocking=False):
            return
        try:
            saved = self._saved()
            now = self.now()
            if (
                not saved
                or now - (saved.get('lastSentAt') or 0) < SEND_EVERY
                or now - (saved.get('attemptAt') or 0) < RETRY_AFTER
            ):
                return
            self.collector.history.cache(CACHE_KEY, {**saved, 'attemptAt': now})
            payload = self.payload(saved, now)
            if self._send('POST', payload, saved['secret']) == 200:
                self.collector.history.cache(
                    CACHE_KEY,
                    {
                        **saved,
                        'attemptAt': now,
                        'lastSentAt': now,
                        'models': len(payload['models']),
                    },
                )
        finally:
            self.lock.release()
