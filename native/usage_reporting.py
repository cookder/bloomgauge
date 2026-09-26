"""Optional, coarse beta usage reporting; never part of provider decisions.

The caller must obtain affirmative local consent. Construction, observation and
ticks without consent neither create analytics credentials nor use the network.
All transport runs on one serialized daemon worker. Call tick periodically for
heartbeats/retries; a stopped app does not backfill older activity.

Injected transport(method, url, body_bytes, headers, timeout_seconds) returns an
HTTP status integer. Injected now() returns Unix seconds and random_hex(n) returns
n random bytes encoded as lowercase hex. Tests must inject transport or pass
network_enabled=False. state_dir=None keeps state only in memory.
"""

import datetime
import fcntl
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import threading
import time
import urllib.error
import urllib.request


ENDPOINT = 'https://bloomformac.com/api/usage/v1'
PRIVACY_URL = 'https://bloomformac.com/privacy'
STATE_FILENAME = 'usage-reporting.json'
LOCK_FILENAME = '.usage-reporting.lock'
REPORT_INTERVAL = 6 * 60 * 60
TIMEOUT = 8
MAX_BODY = 4096
MAX_STATE = 32768
MAX_DAILY_ATTEMPTS = 16
RETRY_DELAYS = (60, 300, 1800)
DELETE_DELAYS = (60, 300, 1800, 21600, 86400)
# trialRequested, trialStarted and proActivated date from the paid era and are now
# always false. The site's usage schema still requires them, so they stay.
FLAGS = frozenset(
    (
        'setupCompleted',
        'dashboardOpened',
        'trialRequested',
        'trialStarted',
        'proActivated',
        'phoneUsed',
        'optimizerUsed',
    )
)
IMMEDIATE_FLAGS = frozenset(('setupCompleted', 'trialRequested', 'trialStarted', 'proActivated'))
ERRORS = frozenset(('none', 'connection', 'provider', 'setup', 'unknown'))
CHIPS = frozenset(
    {'Other'}
    | {
        'M%d%s' % (generation, suffix)
        for generation in range(1, 6)
        for suffix in ('', ' Pro', ' Max', ' Ultra')
    }
)
MEMORY_BANDS = frozenset(('up-to-16', '17-32', '33-64', '65-128', 'over-128', 'unknown'))
PAYLOAD_KEYS = (
    frozenset(
        (
            'schema',
            'analyticsId',
            'day',
            'appVersion',
            'osMajor',
            'chipFamily',
            'memoryBand',
            'setupError',
        )
    )
    | FLAGS
)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def _transport(method, url, body, headers, timeout):
    # No redirect, proxy, response-body read, or raw error logging. In particular,
    # the per-install secret cannot follow a redirect to another origin.
    if url != ENDPOINT or method not in ('POST', 'DELETE'):
        raise ValueError('Unsupported usage request.')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    request = urllib.request.Request(
        url, data=body, headers={**headers, 'User-Agent': 'BloomUsage/1.0'}, method=method
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as error:
        code = error.code
        error.close()
        return code


def _metadata(value):
    value = value if isinstance(value, dict) else {}
    os_major = value.get('osMajor')
    chip = value.get('chipFamily')
    memory = value.get('memoryBand')
    return {
        'osMajor': os_major
        if type(os_major) is int and (os_major == 0 or 14 <= os_major <= 99)
        else 0,
        'chipFamily': chip if isinstance(chip, str) and chip in CHIPS else 'Other',
        'memoryBand': memory if isinstance(memory, str) and memory in MEMORY_BANDS else 'unknown',
    }


def _day(timestamp):
    return datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc).date().isoformat()


def _iso(timestamp):
    if timestamp is None:
        return None
    return (
        datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc)
        .isoformat()
        .replace('+00:00', 'Z')
    )


def _number(value, default=None):
    return (
        value
        if type(value) in (int, float) and math.isfinite(value) and 0 <= value < 1e11
        else default
    )


def _count(value, maximum):
    return value if type(value) is int and 0 <= value <= maximum else 0


def _credentials(value):
    return (
        isinstance(value.get('analyticsId'), str)
        and re.fullmatch(r'[0-9a-f]{32}', value['analyticsId']) is not None
        and isinstance(value.get('secret'), str)
        and re.fullmatch(r'[0-9a-f]{64}', value['secret']) is not None
    )


def _empty_day():
    return {
        'flags': dict.fromkeys(sorted(FLAGS), False),
        'setupError': 'none',
        'revision': 0,
        'sentRevision': -1,
        'immediatePending': False,
        'immediateUsed': [],
        'attempts': 0,
        'cycleAttempts': 0,
        'retryAt': None,
        'lastAttemptAt': None,
    }


class UsageReporter:
    """One reporter per collector; state and transport never include license IDs.

    status() contains only safe UI fields. set_consent, record, observe, tick,
    retry_delete and update_metadata return that status. set_consent(True) while
    deletion is pending is rejected with error='deletion_pending'. close() is
    nonblocking; an already dispatched request finishes, but no next request is
    dispatched. A file lock prevents a replacement reporter from racing it.
    error='opt_out_not_saved' means sharing has stopped in this process but the
    disabled state could not be made durable; callers must show that limitation
    and keep retrying the save before treating restart behavior as confirmed.
    """

    def __init__(
        self,
        state_dir,
        app_version,
        metadata=None,
        transport=None,
        now=None,
        random_hex=None,
        network_enabled=True,
    ):
        self._directory = Path(state_dir) if state_dir is not None else None
        self._path = self._directory / STATE_FILENAME if self._directory is not None else None
        self._app_version = (
            app_version
            if isinstance(app_version, str)
            and re.fullmatch(r'[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}', app_version)
            else '0.0.0'
        )
        self._metadata = _metadata(metadata)
        self._transport = transport or _transport
        self._now = now or time.time
        self._random_hex = random_hex or secrets.token_hex
        self._network_enabled = network_enabled is True
        self._lock = threading.RLock()
        self._worker = None
        self._lock_fd = None
        self._closed = False
        self._deletion_confirmed = False
        self._error = None
        self._storage_ok = True
        self._ownership_blocked = False
        self._state = {'schema': 1, 'enabled': False}
        # Do not create a directory, file, credentials or worker for a new/off
        # installation. Existing consent is resumed by the caller's next tick.
        if self._path is not None and os.path.lexists(self._path):
            if self._own_storage_locked():
                self._load_locked()

    def _own_storage_locked(self):
        if self._directory is None or self._lock_fd is not None:
            return True
        fd = None
        try:
            self._directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(
                self._directory / LOCK_FILENAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
            )
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError('Invalid state lock.')
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_fd = fd
            return True
        except OSError:
            if fd is not None:
                os.close(fd)
            self._ownership_blocked = True
            self._storage_ok = False
            self._error = 'state_unavailable'
            return False

    def _release_storage_locked(self):
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None

    def _load_locked(self):
        try:
            fd = os.open(self._path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, 'r', encoding='utf-8') as stream:
                details = os.fstat(stream.fileno())
                if not stat.S_ISREG(details.st_mode) or details.st_size > MAX_STATE:
                    raise ValueError('Invalid state.')
                os.fchmod(stream.fileno(), 0o600)
                saved = json.load(stream)
            if not isinstance(saved, dict) or saved.get('schema') != 1:
                raise ValueError('Invalid state.')
            if saved.get('enabled') is not True and not isinstance(saved.get('deletion'), dict):
                return
            if not _credentials(saved):
                raise ValueError('Invalid credentials.')
            now = self._now()
            if saved.get('enabled') is not True:
                deletion = saved['deletion']
                self._state = {
                    'schema': 1,
                    'enabled': False,
                    'analyticsId': saved['analyticsId'],
                    'secret': saved['secret'],
                    'deletion': {
                        'attempts': _count(deletion.get('attempts'), len(DELETE_DELAYS) + 1),
                        'nextAttemptAt': _number(deletion.get('nextAttemptAt'), now),
                        'lastAttemptAt': _number(deletion.get('lastAttemptAt')),
                    },
                }
                self._metadata = _metadata(None)
                return
            current = saved.get('current') if isinstance(saved.get('current'), dict) else {}
            self._state = {
                'schema': 1,
                'enabled': True,
                'analyticsId': saved['analyticsId'],
                'secret': saved['secret'],
                'days': {},
                'current': {
                    'setupCompleted': current.get('setupCompleted') is True,
                    'proActivated': current.get('proActivated') is True,
                    'setupError': current.get('setupError')
                    if current.get('setupError') in ERRORS
                    else 'unknown',
                },
                'nextRegularAt': min(
                    _number(saved.get('nextRegularAt'), now), now + REPORT_INTERVAL
                ),
                'lastSentAt': _number(saved.get('lastSentAt')),
            }
            days = saved.get('days') if isinstance(saved.get('days'), dict) else {}
            for day in (_day(now), _day(now - 86400)):
                old = days.get(day)
                if not isinstance(old, dict):
                    continue
                entry = _empty_day()
                flags = old.get('flags') if isinstance(old.get('flags'), dict) else {}
                entry['flags'] = {key: flags.get(key) is True for key in sorted(FLAGS)}
                entry['setupError'] = (
                    old.get('setupError') if old.get('setupError') in ERRORS else 'unknown'
                )
                entry['revision'] = _count(old.get('revision'), 1000000)
                entry['sentRevision'] = _count(old.get('sentRevision'), 1000000)
                entry['immediatePending'] = old.get('immediatePending') is True and day == _day(now)
                used = (
                    old.get('immediateUsed') if isinstance(old.get('immediateUsed'), list) else []
                )
                entry['immediateUsed'] = sorted(
                    set(
                        x
                        for x in used
                        if isinstance(x, str) and x in IMMEDIATE_FLAGS | {'setupError', 'consent'}
                    )
                )
                entry['attempts'] = _count(old.get('attempts'), MAX_DAILY_ATTEMPTS)
                entry['cycleAttempts'] = _count(old.get('cycleAttempts'), len(RETRY_DELAYS) + 1)
                entry['retryAt'] = _number(old.get('retryAt'))
                if entry['retryAt'] is not None:
                    entry['retryAt'] = min(entry['retryAt'], now + REPORT_INTERVAL)
                entry['lastAttemptAt'] = _number(old.get('lastAttemptAt'))
                self._state['days'][day] = entry
            self._roll_day_locked(now)
        except (OSError, ValueError, TypeError, OverflowError):
            self._state = {'schema': 1, 'enabled': False}
            self._error = 'state_unavailable'

    def _save_locked(self):
        if self._directory is None:
            self._storage_ok = True
            return True
        if self._ownership_blocked or not self._own_storage_locked():
            return False
        temporary = None
        try:
            encoded = json.dumps(
                self._state, sort_keys=True, separators=(',', ':'), allow_nan=False
            ).encode('utf-8')
            if len(encoded) > MAX_STATE:
                raise ValueError('Invalid state size.')
            fd, temporary = tempfile.mkstemp(
                prefix='.usage-reporting-', suffix='.tmp', dir=self._directory
            )
            with os.fdopen(fd, 'wb') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
            temporary = None
            directory_fd = os.open(self._directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            self._storage_ok = True
            if self._error in ('storage_unavailable', 'opt_out_not_saved'):
                self._error = None
            return True
        except (OSError, ValueError, TypeError):
            self._storage_ok = False
            self._error = (
                'opt_out_not_saved' if 'deletion' in self._state else 'storage_unavailable'
            )
            return False
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def _roll_day_locked(self, now):
        if self._state.get('enabled') is not True:
            return False
        today, yesterday = _day(now), _day(now - 86400)
        days = self._state['days']
        changed = False
        for day in list(days):
            if day not in (today, yesterday):
                del days[day]
                changed = True
        if today not in days:
            days[today] = _empty_day()
            changed = True
        # Previous-day data is retained only for an already scheduled bounded
        # retry, never promoted into an immediate or historical backfill queue.
        if yesterday in days and days[yesterday]['immediatePending']:
            days[yesterday]['immediatePending'] = False
            changed = True
        return changed

    def _status_locked(self):
        state = self._state
        pending = 'deletion' in state
        error = self._error
        if (
            not self._network_enabled
            and (state.get('enabled') or pending)
            and error not in ('storage_unavailable', 'opt_out_not_saved', 'state_unavailable')
        ):
            error = 'network_disabled'
        due = None
        if pending:
            deletion = state['deletion']
            if deletion['attempts'] <= len(DELETE_DELAYS):
                due = deletion['nextAttemptAt']
            elif error is None:
                error = 'retry_paused'
        elif state.get('enabled'):
            now = self._now()
            candidates = []
            today = state['days'].get(_day(now))
            if today and today['attempts'] < MAX_DAILY_ATTEMPTS:
                candidates.append(state['nextRegularAt'])
                if today['immediatePending'] and today['retryAt'] is None:
                    candidates.append(now)
            candidates.extend(
                entry['retryAt']
                for entry in state['days'].values()
                if entry['retryAt'] is not None and entry['attempts'] < MAX_DAILY_ATTEMPTS
            )
            due = min(candidates) if candidates else None
        return {
            'enabled': state.get('enabled') is True,
            'deletionPending': pending,
            'busy': self._worker is not None,
            'lastSentAt': state.get('lastSentAt'),
            'nextAttemptAt': _iso(due),
            'error': error,
            'privacyUrl': PRIVACY_URL,
        }

    def status(self):
        with self._lock:
            return self._status_locked()

    def set_consent(self, enabled):
        with self._lock:
            if self._closed or type(enabled) is not bool:
                return self._status_locked()
            if enabled and 'deletion' in self._state:
                if self._error != 'opt_out_not_saved':
                    self._error = 'deletion_pending'
                return self._status_locked()
            if self._ownership_blocked:
                return self._status_locked()
            if enabled and not self._state.get('enabled'):
                if not self._own_storage_locked():
                    return self._status_locked()
                try:
                    credentials = {
                        'analyticsId': self._random_hex(16),
                        'secret': self._random_hex(32),
                    }
                    if (
                        not _credentials(credentials)
                        or credentials['analyticsId'] == credentials['secret'][:32]
                    ):
                        raise ValueError('Invalid credentials.')
                except Exception:
                    self._error = 'state_unavailable'
                    return self._status_locked()
                now = self._now()
                entry = _empty_day()
                entry.update(immediatePending=True, immediateUsed=['consent'])
                self._state = {
                    'schema': 1,
                    'enabled': True,
                    **credentials,
                    'days': {_day(now): entry},
                    'current': {
                        'setupCompleted': False,
                        'proActivated': False,
                        'setupError': 'none',
                    },
                    'nextRegularAt': now,
                    'lastSentAt': None,
                }
                self._error = None
                if not self._save_locked():
                    self._state = {'schema': 1, 'enabled': False}
                    return self._status_locked()
            elif not enabled and self._state.get('enabled'):
                # Replace rather than mutate: no flags, current state, metadata,
                # app version or report timestamps survive pending deletion.
                self._state = {
                    'schema': 1,
                    'enabled': False,
                    'analyticsId': self._state['analyticsId'],
                    'secret': self._state['secret'],
                    'deletion': {
                        'attempts': 0,
                        'nextAttemptAt': self._now(),
                        'lastAttemptAt': None,
                    },
                }
                self._metadata = _metadata(None)
                self._error = None
                self._save_locked()
            self._start_worker_locked()
            return self._status_locked()

    def update_metadata(self, metadata):
        with self._lock:
            self._metadata = _metadata(None if 'deletion' in self._state else metadata)
            return self._status_locked()

    def _immediate_locked(self, entry, flag):
        if flag not in entry['immediateUsed']:
            entry['immediateUsed'].append(flag)
            entry['immediatePending'] = True

    def record(self, flag, immediate=False):
        with self._lock:
            if (
                self._closed
                or not self._state.get('enabled')
                or not isinstance(flag, str)
                or flag not in FLAGS
            ):
                return self._status_locked()
            changed = self._roll_day_locked(self._now())
            entry = self._state['days'][_day(self._now())]
            if not entry['flags'][flag]:
                entry['flags'][flag] = True
                entry['revision'] += 1
                changed = True
                if immediate is True and flag in IMMEDIATE_FLAGS:
                    previous = self._state['current'].get(flag, False)
                    if flag == 'trialRequested' or not previous:
                        self._immediate_locked(entry, flag)
            if flag in ('setupCompleted', 'proActivated'):
                if self._state['current'][flag] is not True:
                    self._state['current'][flag] = True
                    changed = True
            if changed:
                self._save_locked()
            self._start_worker_locked()
            return self._status_locked()

    def observe(
        self,
        *,
        setup_completed=False,
        setup_error='none',
        pro_activated=False,
        optimizer_used=False,
    ):
        with self._lock:
            if self._closed or not self._state.get('enabled'):
                return self._status_locked()
            changed = self._roll_day_locked(self._now())
            entry = self._state['days'][_day(self._now())]
            current = self._state['current']
            for flag, value in (
                ('setupCompleted', setup_completed),
                ('proActivated', pro_activated),
                ('optimizerUsed', optimizer_used),
            ):
                value = value is True
                if value and not entry['flags'][flag]:
                    entry['flags'][flag] = True
                    entry['revision'] += 1
                    changed = True
                if flag in current and current[flag] != value:
                    if value:
                        self._immediate_locked(entry, flag)
                    current[flag] = value
                    changed = True
            error = (
                setup_error if isinstance(setup_error, str) and setup_error in ERRORS else 'unknown'
            )
            if error != 'none' and entry['setupError'] != error:
                entry['setupError'] = error
                entry['revision'] += 1
                changed = True
            if current['setupError'] != error:
                if error != 'none':
                    self._immediate_locked(entry, 'setupError')
                current['setupError'] = error
                changed = True
            if changed:
                self._save_locked()
            self._start_worker_locked()
            return self._status_locked()

    def tick(self):
        with self._lock:
            if self._closed:
                return self._status_locked()
            if self._deletion_confirmed:
                self._finish_delete_locked()
                return self._status_locked()
            changed = self._roll_day_locked(self._now())
            if changed or not self._storage_ok and not self._ownership_blocked:
                self._save_locked()
            self._start_worker_locked()
            return self._status_locked()

    def retry_delete(self):
        with self._lock:
            if not self._closed and 'deletion' in self._state:
                if self._deletion_confirmed:
                    self._finish_delete_locked()
                    return self._status_locked()
                deletion = self._state['deletion']
                # An explicit retry starts a new finite retry sequence. Repeated
                # button presses cannot enqueue requests beside the active one.
                if self._worker is None:
                    deletion.update(attempts=0, nextAttemptAt=self._now())
                    self._error = None
                    self._save_locked()
                self._start_worker_locked()
            return self._status_locked()

    def _choose_locked(self, now):
        if (
            self._closed
            or not self._network_enabled
            or not self._storage_ok
            or self._deletion_confirmed
        ):
            return None
        state = self._state
        if 'deletion' in state:
            deletion = state['deletion']
            if deletion['attempts'] <= len(DELETE_DELAYS) and deletion['nextAttemptAt'] <= now:
                return ('DELETE', None, None)
            return None
        if not state.get('enabled'):
            return None
        today = state['days'].get(_day(now))
        if today and today['attempts'] < MAX_DAILY_ATTEMPTS:
            if today['immediatePending'] and today['retryAt'] is None:
                return ('POST', _day(now), 'immediate')
        for day in sorted(state['days'], reverse=True):
            entry = state['days'][day]
            if (
                day in (_day(now), _day(now - 86400))
                and entry['retryAt'] is not None
                and entry['retryAt'] <= now
                and entry['attempts'] < MAX_DAILY_ATTEMPTS
                and entry['cycleAttempts'] <= len(RETRY_DELAYS)
            ):
                return ('POST', day, 'retry')
        if today and today['attempts'] < MAX_DAILY_ATTEMPTS and state['nextRegularAt'] <= now:
            return ('POST', _day(now), 'regular')
        return None

    def _start_worker_locked(self):
        if self._worker is None and self._choose_locked(self._now()) is not None:
            self._worker = threading.Thread(
                target=self._run, name='bloom-optional-usage', daemon=True
            )
            self._worker.start()

    def _prepare_locked(self, operation, now):
        method, day, kind = operation
        state = self._state
        analytics_id, secret = state['analyticsId'], state['secret']
        payload = {'schema': 1, 'analyticsId': analytics_id}
        revision = None
        if method == 'DELETE':
            deletion = state['deletion']
            deletion['attempts'] += 1
            deletion['lastAttemptAt'] = now
            offset = min(deletion['attempts'] - 1, len(DELETE_DELAYS) - 1)
            deletion['nextAttemptAt'] = now + DELETE_DELAYS[offset]
        else:
            entry = state['days'][day]
            payload.update(
                day=day,
                appVersion=self._app_version,
                **self._metadata,
                setupError=entry['setupError'],
                **entry['flags'],
            )
            revision = entry['revision']
            entry['attempts'] += 1
            entry['cycleAttempts'] = entry['cycleAttempts'] + 1 if kind == 'retry' else 1
            entry['lastAttemptAt'] = now
            entry['immediatePending'] = False
            count = entry['cycleAttempts']
            entry['retryAt'] = now + RETRY_DELAYS[count - 1] if count <= len(RETRY_DELAYS) else None
            if kind != 'retry':
                state['nextRegularAt'] = now + REPORT_INTERVAL
        body = json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False).encode(
            'utf-8'
        )
        if len(body) > MAX_BODY or not self._save_locked():
            return None
        return (
            analytics_id,
            revision,
            body,
            {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + secret},
        )

    def _finish_delete_locked(self):
        pending = self._state
        self._state = {'schema': 1, 'enabled': False}
        if self._save_locked():
            self._deletion_confirmed = False
            self._error = None
        else:
            # Remote deletion succeeded, but clearing local credentials did not.
            # Keep the retry control and safe state until local cleanup succeeds;
            # a restart can safely repeat the server's idempotent DELETE.
            self._state = pending
            self._error = 'storage_unavailable'

    def _run(self):
        try:
            while True:
                with self._lock:
                    now = self._now()
                    if self._roll_day_locked(now) and not self._save_locked():
                        return
                    operation = self._choose_locked(now)
                    if operation is None:
                        return
                    prepared = self._prepare_locked(operation, now)
                    if prepared is None:
                        return
                analytics_id, revision, body, headers = prepared
                method, day, unused_kind = operation
                try:
                    code = self._transport(method, ENDPOINT, body, headers, TIMEOUT)
                except Exception:
                    code = None
                # Drop the in-flight measurement before allowing deletion. No
                # queued payloads or response/error bodies are retained.
                del body, headers, prepared
                with self._lock:
                    state = self._state
                    if state.get('analyticsId') != analytics_id:
                        continue
                    if method == 'DELETE' and 'deletion' in state:
                        if type(code) is int and code == 204:
                            self._deletion_confirmed = True
                            self._finish_delete_locked()
                        else:
                            self._error = (
                                'retry_pending'
                                if state['deletion']['attempts'] <= len(DELETE_DELAYS)
                                else 'retry_paused'
                            )
                            self._save_locked()
                    elif method == 'POST' and state.get('enabled') and day in state['days']:
                        entry = state['days'][day]
                        if type(code) is int and code == 200:
                            entry['retryAt'] = None
                            entry['cycleAttempts'] = 0
                            entry['sentRevision'] = max(entry['sentRevision'], revision)
                            state['lastSentAt'] = self._now()
                            self._error = None
                        else:
                            self._error = (
                                'retry_pending' if entry['retryAt'] is not None else 'retry_paused'
                            )
                        self._save_locked()
        finally:
            with self._lock:
                self._worker = None
                if self._closed:
                    self._release_storage_locked()
                # A caller can add work while the previous loop is returning.
                # Recheck under the same lock so that work cannot get stranded.
                else:
                    self._start_worker_locked()

    def close(self):
        with self._lock:
            self._closed = True
            if self._worker is None:
                self._release_storage_locked()
