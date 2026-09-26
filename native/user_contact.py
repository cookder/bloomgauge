"""Optional contact details the user chooses to give Bloomkeeper's developer.

Nothing is sent unless the user types an email or Slack handle and ticks consent.
Saving waits for the website to confirm; nothing is queued or retried in the
background. The per-install id and secret let this Mac update or delete its own
entry later. Removing it deletes the website's copy before forgetting it here.
"""

import json
import re
import secrets
import threading
import time
import urllib.error
import urllib.request

import diagnostics

ENDPOINT = 'https://bloomformac.com/api/contact/v1'
CACHE_KEY = 'user-contact-v1'
TIMEOUT = 15
MAX_CONTACT = 254
EMAIL = re.compile(r'[^\s@]{1,64}@[^\s@]{1,189}\.[^\s@.]{2,63}')
SLACK = re.compile(r'@[^\s@][^@]{0,79}')
VERSION = re.compile(r'\d{1,3}\.\d{1,3}\.\d{1,4}')


class ContactError(Exception):
    """Only fixed public messages reach the API, never transport errors."""

    MESSAGES = {
        'phone': (403, 'Change your contact details in Bloomkeeper on your Mac.'),
        'unavailable': (503, 'Contact details can’t be sent from this copy of Bloomkeeper.'),
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


def kind(contact):
    """'email', 'slack' or None. Slack handles start with @."""
    if type(contact) is not str or not 0 < len(contact) <= MAX_CONTACT:
        return None
    if any(ord(c) < 32 or ord(c) == 127 for c in contact):
        return None
    if EMAIL.fullmatch(contact):
        return 'email'
    if SLACK.fullmatch(contact) and contact == contact.strip():
        return 'slack'
    return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _transport(method, url, body, headers, timeout):
    # Fixed destination, no redirects (the secret can't follow one), no proxy,
    # and only the status code is read.
    if url != ENDPOINT or method not in ('POST', 'DELETE'):
        raise ValueError('Unsupported contact request.')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    request = urllib.request.Request(
        url, data=body, headers={**headers, 'User-Agent': 'BloomContact/1.0'}, method=method
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as error:
        code = error.code
        error.close()
        return code


class UserContact:
    def __init__(self, history, network_enabled=True, transport=None, now=None, app_version=None):
        self.history = history
        self.network_enabled = network_enabled
        self.transport = transport or _transport
        self.now = now or time.time
        self.app_version = app_version or diagnostics.app_version
        self.lock = threading.Lock()

    def _saved(self):
        saved = self.history.cache(CACHE_KEY)
        if (
            type(saved) is dict
            and kind(saved.get('contact'))
            and type(saved.get('id')) is str
            and re.fullmatch(r'[0-9a-f]{32}', saved['id'])
            and type(saved.get('secret')) is str
            and re.fullmatch(r'[0-9a-f]{64}', saved['secret'])
        ):
            return saved
        return None

    def status(self, remote=False):
        saved = self._saved()
        return {
            'schema': 1,
            'contact': saved['contact'] if saved else None,
            'kind': kind(saved['contact']) if saved else None,
            'savedAt': saved.get('savedAt') if saved else None,
            'canChange': not remote and self.network_enabled,
        }

    def _send(self, method, payload, secret):
        body = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        try:
            code = self.transport(
                method,
                ENDPOINT,
                body,
                {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + secret},
                TIMEOUT,
            )
        except Exception:
            code = None
        return code

    def action(self, data, remote=False):
        if remote:
            raise ContactError('phone')
        if type(data) is not dict or data.get('action') not in ('save', 'remove'):
            raise ValueError('Choose a contact action.')
        if not self.network_enabled:
            raise ContactError('unavailable')
        if not self.lock.acquire(blocking=False):
            raise ContactError('busy')
        try:
            saved = self._saved()
            if data['action'] == 'remove':
                if set(data) != {'action'}:
                    raise ValueError('Choose a contact action.')
                if saved:
                    # 204 or 404 both mean the website no longer holds it.
                    if self._send(
                        'DELETE', {'schema': 1, 'id': saved['id']}, saved['secret']
                    ) not in (204, 404):
                        raise ContactError('unconfirmed')
                    self.history.cache(CACHE_KEY, {})
                return self.status()
            if set(data) != {'action', 'contact', 'consent'} or data['consent'] is not True:
                raise ValueError('Tick the box to agree before saving.')
            contact = data['contact'].strip() if type(data['contact']) is str else None
            contact_kind = kind(contact)
            if not contact_kind:
                raise ValueError('Enter an email address or a Slack handle starting with @.')
            ident = saved or {'id': secrets.token_hex(16), 'secret': secrets.token_hex(32)}
            version = self.app_version()
            payload = {
                'schema': 1,
                'id': ident['id'],
                'contact': contact,
                'kind': contact_kind,
                'appVersion': version
                if type(version) is str and VERSION.fullmatch(version)
                else 'unknown',
            }
            if self._send('POST', payload, ident['secret']) != 200:
                raise ContactError('unconfirmed')
            self.history.cache(
                CACHE_KEY,
                {
                    'contact': contact,
                    'id': ident['id'],
                    'secret': ident['secret'],
                    'savedAt': self.now(),
                },
            )
            return self.status()
        finally:
            self.lock.release()
