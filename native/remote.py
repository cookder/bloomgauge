"""Private phone access through Tailscale Serve; no public listener or Funnel.

The separate loopback listener trusts Serve's identity headers only after an
explicit local enable, and only for the account that owns this Mac's node.

Any local program can connect to 127.0.0.1 and set those headers itself, so Serve
proxies to a random path prefix (the secret) that the listener requires and strips.
The secret lives only in remote-access.json (0600) and Tailscale's Serve config,
which other non-admin accounts on the Mac can't read. A Unix socket target would be
stronger, but the sandboxed Tailscale network extension can't connect to one in the
user's folders ("operation not permitted", Sep 26 2026).
"""

import copy
import email.header
import json
import os
import pathlib
import re
import secrets
import subprocess
import threading
import time
import uuid
from urllib.parse import urlsplit

HTTPS_PORT = 8443
SECRET = re.compile(r'[A-Za-z0-9_-]{32}')
# A failed re-point (Tailscale busy or disconnected) is retried at most this often.
REPOINT_SECONDS = 60


def tailscale_binary():
    for value in (
        '/Applications/Tailscale.app/Contents/MacOS/Tailscale',
        '/Applications/Tailscale.app/Contents/MacOS/tailscale',
        '/usr/local/bin/tailscale',
        '/opt/homebrew/bin/tailscale',
    ):
        if os.path.isfile(value) and os.access(value, os.X_OK):
            return value
    return None


def identity(status):
    node = status.get('Self') or {}
    host = str(node.get('DNSName', '')).rstrip('.').lower()
    user = (status.get('User') or {}).get(str(node.get('UserID')), {})
    login = user.get('LoginName', '')
    if (
        status.get('BackendState') != 'Running'
        or not login
        or not re.fullmatch(r'[a-z0-9-]+\.[a-z0-9.-]+\.ts\.net', host)
    ):
        return None
    return {'host': host, 'owner': login}


def new_secret():
    return secrets.token_urlsafe(24)


def serve_slot(config, host, target, listener=None):
    """Never overwrite another service or accept a public Funnel configuration.

    'stale' is Bloomkeeper's own slot pointing at this listener with an older or missing
    secret (phone access set up before 1.36.54); Bloomkeeper may re-point it."""
    authority = f'{host}:{HTTPS_PORT}'
    tcp = (config.get('TCP') or {}).get(str(HTTPS_PORT))
    web = {k: v for k, v in (config.get('Web') or {}).items() if k.endswith(f':{HTTPS_PORT}')}
    public = any(
        v for k, v in (config.get('AllowFunnel') or {}).items() if k.endswith(f':{HTTPS_PORT}')
    )
    if public:
        return 'conflict'
    if not tcp and not web:
        # Foreground Serve configurations can also own the same port.
        for child in (config.get('Foreground') or {}).values():
            if serve_slot(child, host, target) != 'empty':
                return 'conflict'
        return 'empty'
    if tcp == {'HTTPS': True} and web == {authority: {'Handlers': {'/': {'Proxy': target}}}}:
        return 'ours'
    if listener and tcp == {'HTTPS': True} and list(web) == [authority]:
        handlers = web[authority].get('Handlers') if isinstance(web[authority], dict) else None
        root = handlers.get('/') if isinstance(handlers, dict) else None
        older = root.get('Proxy') if isinstance(root, dict) else None
        if (
            isinstance(older, str)
            and web[authority] == {'Handlers': {'/': {'Proxy': older}}}
            and (
                older == listener
                or older.startswith(listener + '/')
                and SECRET.fullmatch(older[len(listener) + 1 :])
            )
        ):
            return 'stale'
    return 'conflict'


def consent_url(output):
    for value in re.findall(r'https://[^\s<>\"\']+', output):
        parsed = urlsplit(value)
        if (
            parsed.hostname == 'login.tailscale.com'
            and not parsed.username
            and parsed.port in (None, 443)
        ):
            return value
    return None


class Remote:
    def __init__(self, path, port=8766, runner=None, binary=tailscale_binary):
        self.path = pathlib.Path(path)
        self.listener = f'http://127.0.0.1:{port}'
        self.binary = binary
        self.runner = runner or self.run
        self.lock = threading.RLock()
        self.operations = threading.Lock()
        self.stop = threading.Event()
        self.config = {}
        self.trusted = None
        self.state = {
            'status': 'checking',
            'detail': 'Checking private phone access.',
            'url': None,
            'canEnable': False,
        }
        self.checked = 0
        self.status_revision = 0
        self.status_instance = uuid.uuid4().hex
        self.operation_revision = 0
        self.pending_operations = 0
        self.auth_url = None
        self.error = None
        self.repointed = None
        try:
            data = json.loads(self.path.read_text())
            if isinstance(data, dict):
                self.config = data
        except (OSError, ValueError):
            pass
        saved = self.config.get('secret')
        self.secret = saved if isinstance(saved, str) and SECRET.fullmatch(saved) else new_secret()
        if self.config.get('enabled') and saved != self.secret:
            # Phone access set up before the secret existed: the refresh loop moves Serve to it.
            try:
                self.persist(dict(self.config, secret=self.secret))
            except OSError:
                pass

    @property
    def target(self):
        return f'{self.listener}/{self.secret}'

    def phone_path(self, path):
        """The request path without the secret prefix, or None when it's missing."""
        prefix = '/' + self.secret + '/'
        if not isinstance(path, str) or not secrets.compare_digest(
            path[: len(prefix)].encode(), prefix.encode()
        ):
            return None
        return path[len(prefix) - 1 :]

    def run(self, args):
        env = dict(os.environ, TAILSCALE_BE_CLI='1')
        try:
            p = subprocess.run(
                [self.binary(), *args], capture_output=True, text=True, timeout=12, env=env
            )
            return p.returncode, p.stdout, p.stderr
        except subprocess.TimeoutExpired as e:

            def decoded(v):
                return v.decode(errors='replace') if isinstance(v, bytes) else v or ''

            return 124, decoded(e.stdout), decoded(e.stderr)

    def read(self, args):
        code, output, error = self.runner(args)
        if code:
            raise ValueError('Open Tailscale on this Mac and connect, then try again.')
        value = json.loads(output)
        if not isinstance(value, dict):
            raise ValueError('Tailscale returned an unreadable status. Try again.')
        return value

    def persist(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f)
        os.replace(temporary, self.path)
        self.config = data

    def refresh(self):
        with self.operations:
            return self._refresh()

    def _refresh(self):
        state = {
            'status': 'missing',
            'detail': 'Install Tailscale on your Mac and phone to connect privately.',
            'url': None,
            'canEnable': False,
            'enabled': bool(self.config.get('enabled')),
            'authURL': self.auth_url,
            'error': self.error,
        }
        trusted = None
        try:
            if self.binary():
                status = self.read(['status', '--json'])
                who = identity(status)
                state.update(
                    status='disconnected',
                    detail='Open Tailscale on your Mac and sign in or reconnect.',
                )
                if who:
                    state['owner'] = who['owner']
                    slot = self.slot(who)
                    if (
                        slot == 'stale'
                        and self.config.get('enabled')
                        and all(self.config.get(k) == who[k] for k in ('host', 'owner'))
                        and (
                            self.repointed is None
                            or time.monotonic() - self.repointed >= REPOINT_SECONDS
                        )
                    ):
                        self.repointed = time.monotonic()
                        self.runner(
                            ['serve', '--bg', f'--https={HTTPS_PORT}', '--yes', self.target]
                        )
                        slot = self.slot(who)
                    state.update(
                        status='off',
                        detail='Ready to connect your phone.',
                        canEnable=slot != 'conflict',
                    )
                    if slot == 'conflict':
                        state.update(
                            status='conflict',
                            detail='Tailscale port 8443 is already configured for another service. Its settings have been left intact.',
                        )
                    elif self.config.get('enabled') and any(
                        self.config.get(k) != who[k] for k in ('host', 'owner')
                    ):
                        state.update(
                            status='changed',
                            detail='The Tailscale account or address changed. Enable again on this Mac to use the new account.',
                        )
                    elif self.config.get('enabled') and slot == 'ours':
                        trusted = who
                        state.update(
                            status='enabled',
                            detail='Private access is on for devices signed in to your Tailscale account.',
                            url=f'https://{who["host"]}:{HTTPS_PORT}',
                            canEnable=False,
                        )
                    elif self.auth_url:
                        state.update(
                            status='needs_https',
                            detail='Finish enabling HTTPS in Tailscale, then select Enable phone access again.',
                        )
        except (OSError, ValueError, TypeError, KeyError):
            state.update(
                status='disconnected',
                detail='Open Tailscale on this Mac and connect, then try again.',
            )
        with self.lock:
            self.state, self.trusted, self.checked = state, trusted, time.monotonic()
            self.status_revision += 1
        return self.snapshot()

    def slot(self, who):
        return serve_slot(
            self.read(['serve', 'status', '--json']), who['host'], self.target, self.listener
        )

    def snapshot(self, remote=False):
        with self.lock:
            result = copy.deepcopy(self.state)
            result.update(
                operationPending=self.pending_operations > 0,
                operationRevision=self.operation_revision,
                statusRevision=self.status_revision,
                statusInstance=self.status_instance,
            )
        result['canManage'] = not remote
        if remote:
            result.pop('authURL', None)
            result.pop('error', None)
        return result

    def authorize(self, headers):
        with self.lock:
            trusted = copy.deepcopy(self.trusted)
            fresh = time.monotonic() - self.checked < 35
        if not trusted or not fresh:
            return False
        authority = f'{trusted["host"]}:{HTTPS_PORT}'
        values = headers.get_all('Tailscale-User-Login', [])
        try:
            login = (
                str(email.header.make_header(email.header.decode_header(values[0])))
                if len(values) == 1
                else ''
            )
        except (ValueError, LookupError):
            return False
        return (
            login == trusted['owner']
            and headers.get_all('Host', []) == [authority]
            and headers.get('X-Forwarded-Proto') == 'https'
        )

    def action(self, action):
        # Expose queued as well as running actions. A timed-out browser must not
        # mistake a cached status response for a completed Tailscale operation.
        with self.lock:
            self.pending_operations += 1
        try:
            self._action(action)
        finally:
            with self.lock:
                self.pending_operations -= 1
                self.operation_revision += 1
        return self.snapshot()

    def _action(self, action):
        with self.operations:
            self.error = self.auth_url = None
            if action == 'disable':
                # Revoke locally first, even if Tailscale is disconnected or the CLI fails.
                previous = dict(self.config)
                self.persist({'enabled': False})
                with self.lock:
                    self.trusted = None
                if self.binary() and previous.get('host'):
                    try:
                        if self.slot(previous) in ('ours', 'stale'):
                            self.runner(['serve', '--bg', f'--https={HTTPS_PORT}', 'off'])
                    except (OSError, ValueError):
                        pass
                # A later enable gets a new secret.
                self.secret = new_secret()
                return self._refresh()
            if action != 'enable':
                raise ValueError('Unknown phone-access action.')
            self._refresh()
            if not self.state['canEnable']:
                raise ValueError(self.state['detail'])
            who = identity(self.read(['status', '--json']))
            if not who:
                raise ValueError('Connect Tailscale on this Mac first.')
            if self.slot(who) == 'conflict':
                raise ValueError('Tailscale port 8443 is in use by another service.')
            code, output, error = self.runner(
                ['serve', '--bg', f'--https={HTTPS_PORT}', '--yes', self.target]
            )
            if code:
                self.auth_url = consent_url(output + '\n' + error)
                self.error = (
                    None
                    if self.auth_url
                    else 'Tailscale could not enable the connection. Check that it is connected and try again.'
                )
                return self._refresh()
            if self.slot(who) != 'ours':
                raise ValueError('Tailscale did not confirm a private dashboard connection.')
            self.persist(dict(who, enabled=True, secret=self.secret))
            return self._refresh()

    def start(self):
        def loop():
            while not self.stop.is_set():
                self.refresh()
                self.stop.wait(5)

        threading.Thread(target=loop, daemon=True).start()
