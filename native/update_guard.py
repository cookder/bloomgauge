"""Brief, native-only update reservations; never persist or alter a model plan.

The optimizer lock serializes reservations with every worker admission. Existing
work must finish before an update; a reservation prevents a new worker appearing
between the updater's readiness check and collector termination. Lost/cancelled
requests expire so a failed update cannot leave automation inhibited.

An automatic (silent) install also waits out work that survives a relaunch but
should not be cut short by one: a manager excursion (big-model trial), a manager
recovery or pending manual pick, and a legacy demand trial or learning run.
Updates.swift drops these soft holds after an update has waited a day, so a
stuck state can never keep a Mac on an old version forever.
"""

import time
import uuid


class UpdateBlocked(ValueError):
    def __init__(self, reason, detail):
        super().__init__(detail)
        self.reason = reason


class UpdateGuard:
    PREPARE_SECONDS = 60
    COMMIT_SECONDS = 30

    def __init__(self, optimizer, clock=time.monotonic):
        self.optimizer, self.clock = optimizer, clock
        self.lease = self.request_id = None
        self.deadline = 0
        self.committed = False
        self.automatic = False

    def active(self):
        # All callers use the same reentrant optimizer lock, including admission.
        with self.optimizer.lock:
            if self.lease and self.clock() >= self.deadline:
                self.lease = self.request_id = None
                self.deadline = 0
                self.committed = False
                self.automatic = False
            return self.lease is not None

    def require_available(self):
        if self.active():
            raise ValueError(
                'BloomGauge is preparing an update. Try this model change again after it finishes or is cancelled.'
            )

    def _busy(self):
        o = self.optimizer
        if o.stop.is_set():
            raise UpdateBlocked(
                'collector-stopping',
                'BloomGauge is already closing. Wait for it to reopen before updating.',
            )
        if o.state.get('pending') or o.worker and o.worker.is_alive():
            raise UpdateBlocked(
                'model-switch',
                'A model switch is in progress. Let it finish, then try the update again.',
            )
        if o.warmup_worker and o.warmup_worker.is_alive():
            raise UpdateBlocked(
                'model-warmup',
                'BloomGauge is checking model readiness. Let it finish, then try the update again.',
            )
        if o.state.get('requestedModel'):
            raise UpdateBlocked(
                'queued-switch',
                'A model switch is queued. Let it finish or cancel it before updating.',
            )
        if o.command_lock.locked():
            raise UpdateBlocked(
                'provider-command',
                'BloomGauge is finishing a provider operation. Try the update again shortly.',
            )

    def _soft_holds(self):
        """Work an automatic install waits for; a person choosing Install does not."""
        o = self.optimizer
        m = o.state.get('manager') or {}
        if not isinstance(m, dict):
            m = {}
        if (m.get('excursion') or {}).get('target'):
            raise UpdateBlocked(
                'excursion',
                'BloomGauge is trying a bigger model. The update installs after the trial ends.',
            )
        if m.get('recovery') or m.get('resume'):
            raise UpdateBlocked(
                'manager-recovery',
                'BloomGauge is settling a model change. The update installs after it finishes.',
            )
        trial = (getattr(o, 'last_demand_decision', None) or {}).get('trial') or {}
        # Same test as demand_optimizer's trial_running: a settling trial is still running.
        if isinstance(trial, dict) and trial.get('current') and trial.get('status') in ('running', 'settling'):
            raise UpdateBlocked(
                'trial',
                'BloomGauge is testing a model. The update installs after the test ends.',
            )

    @staticmethod
    def _id(value):
        if not isinstance(value, str) or len(value) != 36:
            raise ValueError('Invalid update request.')
        try:
            if str(uuid.UUID(value)) != value:
                raise ValueError()
        except (ValueError, AttributeError):
            raise ValueError('Invalid update request.') from None
        return value

    def action(self, data):
        if type(data) is not dict:
            raise ValueError('Invalid update request.')
        action = data.get('action')
        field = 'requestId' if action == 'prepare' else 'lease'
        keys = {'action', field}
        # Only prepare may carry automatic, and only as an explicit true.
        automatic = action == 'prepare' and 'automatic' in data
        if automatic:
            keys.add('automatic')
            if data['automatic'] is not True:
                raise ValueError('Invalid update request.')
        if action not in ('prepare', 'commit', 'release') or set(data) != keys:
            raise ValueError('Invalid update request.')
        identifier = self._id(data[field])
        with self.optimizer.lock:
            held = self.active()
            if action == 'release':
                released = bool(held and identifier == self.lease)
                if released:
                    self.lease = self.request_id = None
                    self.deadline = 0
                    self.committed = False
                    self.automatic = False
                return {'released': released}
            if action == 'prepare':
                if held and identifier != self.request_id:
                    raise UpdateBlocked(
                        'update-in-progress',
                        'Another update check is already in progress. Try again shortly.',
                    )
                if held and automatic != self.automatic:
                    raise ValueError('Invalid update request.')
                self._busy()
                if automatic:
                    self._soft_holds()
                if not held:
                    self.lease, self.request_id = str(uuid.uuid4()), identifier
                    self.deadline = self.clock() + self.PREPARE_SECONDS
                    self.committed = False
                    self.automatic = automatic
                # Retried replies do not prolong a reservation indefinitely.
                return {
                    'ready': True,
                    'lease': self.lease,
                    'expiresInSeconds': max(0, self.deadline - self.clock()),
                }
            if not held or identifier != self.lease:
                raise UpdateBlocked(
                    'reservation-expired',
                    'The update readiness check expired. Try the update again.',
                )
            self._busy()
            if self.automatic:
                self._soft_holds()
            if not self.committed:
                self.deadline = self.clock() + self.COMMIT_SECONDS
                self.committed = True
            return {
                'ready': True,
                'lease': self.lease,
                'expiresInSeconds': max(0, self.deadline - self.clock()),
            }
