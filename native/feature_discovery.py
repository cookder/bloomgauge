"""Local, optional feature discovery. No analytics or provider commands."""

import copy
import math
import threading
import time

KEY = 'feature-discovery-v1'
READY_SECONDS = 2 * 60 * 60
SNOOZE_SECONDS = 24 * 60 * 60
FEATURES = ('phone', 'optimizer')


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def blank():
    return {
        'schema': 1,
        'healthySeconds': 0,
        'features': {
            name: {'dismissed': False, 'used': False, 'snoozedUntil': 0} for name in FEATURES
        },
    }


def valid(state):
    return (
        type(state) is dict
        and state.get('schema') == 1
        and number(state.get('healthySeconds'))
        and 0 <= state['healthySeconds'] <= READY_SECONDS
        and type(state.get('features')) is dict
        and set(state['features']) == set(FEATURES)
        and all(
            type(item) is dict
            and type(item.get('dismissed')) is bool
            and type(item.get('used')) is bool
            and number(item.get('snoozedUntil'))
            and item['snoozedUntil'] >= 0
            for item in state['features'].values()
        )
    )


class FeatureDiscovery:
    def __init__(self, collector, enabled=True, now=time.time, monotonic=time.monotonic):
        self.collector, self.enabled = collector, enabled
        self.now, self.monotonic = now, monotonic
        self.lock = threading.RLock()
        self.state = blank()
        self.available = True
        self.last_tick = None  # Never restore elapsed-time anchors across launches.
        self.last_save = monotonic()
        self.dirty = False
        self.invalid_state = False
        if enabled:
            try:
                saved = collector.history.cache(KEY)
                if saved is not None:
                    if not valid(saved):
                        raise ValueError('Invalid saved discovery preferences.')
                    self.state = saved
            except Exception:
                # Do not reset a previously dismissed suggestion after a bad read.
                self.invalid_state = True
                self.available = False

    def context(self):
        now = self.now()
        try:
            complete = (self.collector.history.cache('setup-v1') or {}).get('completed') is True
            with self.collector.lock:
                snapshot = self.collector.snapshot or {}
                at = snapshot.get('at')
                earnings = dict(snapshot.get('earnings') or {})
                provider = dict(snapshot.get('provider') or {})
            with self.collector.optimizer.lock:
                optimizer = dict(self.collector.optimizer.state)
            updated = earnings.get('updatedAt')
            healthy = (
                complete
                and number(at)
                and 0 <= now - at <= 10
                and earnings.get('status') == 'ok'
                and not earnings.get('error')
                and number(updated)
                and 0 <= now - updated <= 60
                and provider.get('online') is True
            )
            used = optimizer.get('mode') in ('week', 'optimize', 'combo', 'demand') or (
                number(optimizer.get('startedAt')) and optimizer['startedAt'] > 0
            )
            return {'at': at, 'healthy': bool(healthy), 'complete': complete, 'optimizerUsed': used}
        except Exception:
            return {'at': None, 'healthy': False, 'complete': False, 'optimizerUsed': False}

    def save(self, next_state):
        self.last_save = self.monotonic()
        try:
            self.collector.history.cache(KEY, next_state)
        except Exception:
            self.available = False
            raise
        self.state = next_state
        self.available = True
        self.dirty = False

    def observe(self):
        """Called only after a real collector sample, never by browser polling."""
        if not self.enabled or self.invalid_state:
            return
        context = self.context()
        now, mono = self.now(), self.monotonic()
        with self.lock:
            before = copy.deepcopy(self.state)
            if self.last_tick and self.last_tick['at'] == context['at']:
                return
            previous = self.last_tick
            self.last_tick = {**context, 'wall': now, 'mono': mono}
            if previous and previous['healthy'] and context['healthy']:
                elapsed, wall = mono - previous['mono'], now - previous['wall']
                sample = context['at'] - previous['at']
                # Reject sleep, stopped collection, clock jumps and duplicate samples.
                if (
                    0 < elapsed <= 15
                    and 0 < wall <= 15
                    and 0 < sample <= 15
                    and abs(elapsed - wall) <= 2
                ):
                    self.state['healthySeconds'] = min(
                        READY_SECONDS, self.state['healthySeconds'] + min(elapsed, wall, sample)
                    )
            if context['optimizerUsed']:
                self.state['features']['optimizer']['used'] = True
            changed_feature = before['features'] != self.state['features']
            self.dirty = self.dirty or before != self.state
            if self.dirty and (
                changed_feature
                or mono - self.last_save >= 60
                or before['healthySeconds'] < READY_SECONDS <= self.state['healthySeconds']
            ):
                try:
                    self.save(copy.deepcopy(self.state))
                except Exception:
                    pass  # Discovery must never interrupt monitoring.

    def status(self, remote=False, phone_configured=False, preview=False):
        quiet = {'schema': 1, 'available': False, 'localOnly': bool(remote), 'features': []}
        if preview or not self.enabled or self.invalid_state:
            return quiet
        context = self.context()
        with self.lock:
            next_state = copy.deepcopy(self.state)
            if phone_configured or (remote and context['complete']):
                next_state['features']['phone']['used'] = True
            if context['optimizerUsed']:
                next_state['features']['optimizer']['used'] = True
            if next_state != self.state:
                try:
                    self.save(next_state)
                except Exception:
                    return quiet
            ready = (
                self.available
                and context['healthy']
                and self.state['healthySeconds'] >= READY_SECONDS
            )
            features = []
            if ready:
                for name in FEATURES:
                    state = self.state['features'][name]
                    if state['used'] or state['dismissed'] or self.now() < state['snoozedUntil']:
                        continue
                    if name == 'phone' and not remote:
                        features.append({'id': name, 'destination': 'phone'})
                    elif name == 'optimizer':
                        features.append({'id': name, 'destination': 'optimizer'})
            return {**quiet, 'available': self.available, 'features': features}

    def action(self, data, remote=False, phone_configured=False, preview=False):
        if preview or not self.enabled:
            raise PermissionError('Feature discovery is disabled in setup preview.')
        if (
            type(data) is not dict
            or set(data) != {'action', 'feature'}
            or data.get('action') not in ('dismiss', 'snooze', 'open')
            or data.get('feature') not in FEATURES
        ):
            raise ValueError('Choose a valid feature suggestion action.')
        if remote and data['feature'] == 'phone' and data['action'] == 'open':
            raise PermissionError('Configure phone access on the Mac.')
        if self.invalid_state:
            raise RuntimeError('Discovery preferences are unavailable.')
        with self.lock:
            next_state = copy.deepcopy(self.state)
            state = next_state['features'][data['feature']]
            if data['action'] == 'dismiss':
                state['dismissed'] = True
            else:
                # Opening setup is an invitation, not evidence of configuration.
                state['snoozedUntil'] = self.now() + SNOOZE_SECONDS
            self.save(next_state)
        return self.status(remote, phone_configured, preview)

    def close(self):
        if self.enabled and not self.invalid_state:
            with self.lock:
                if self.dirty:
                    try:
                        self.save(copy.deepcopy(self.state))
                    except Exception:
                        pass
