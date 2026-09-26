"""Brief, native-only update reservations; never persist or alter a model plan.

The optimizer lock serializes reservations with every worker admission. Existing
work must finish before an update; a reservation prevents a new worker appearing
between the updater's readiness check and collector termination. Lost/cancelled
requests expire so a failed update cannot leave automation inhibited.
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

    def active(self):
        # All callers use the same reentrant optimizer lock, including admission.
        with self.optimizer.lock:
            if self.lease and self.clock() >= self.deadline:
                self.lease = self.request_id = None
                self.deadline = 0
                self.committed = False
            return self.lease is not None

    def require_available(self):
        if self.active():
            raise ValueError(
                'Bloomkeeper is preparing an update. Try this model change again after it finishes or is cancelled.'
            )

    def _busy(self):
        o = self.optimizer
        if o.stop.is_set():
            raise UpdateBlocked(
                'collector-stopping',
                'Bloomkeeper is already closing. Wait for it to reopen before updating.',
            )
        if o.state.get('pending') or o.worker and o.worker.is_alive():
            raise UpdateBlocked(
                'model-switch',
                'A model switch is in progress. Let it finish, then try the update again.',
            )
        if o.warmup_worker and o.warmup_worker.is_alive():
            raise UpdateBlocked(
                'model-warmup',
                'Bloomkeeper is checking model readiness. Let it finish, then try the update again.',
            )
        if o.state.get('requestedModel'):
            raise UpdateBlocked(
                'queued-switch',
                'A model switch is queued. Let it finish or cancel it before updating.',
            )
        if o.command_lock.locked():
            raise UpdateBlocked(
                'provider-command',
                'Bloomkeeper is finishing a provider operation. Try the update again shortly.',
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
        if action not in ('prepare', 'commit', 'release') or set(data) != {'action', field}:
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
                return {'released': released}
            if action == 'prepare':
                if held and identifier != self.request_id:
                    raise UpdateBlocked(
                        'update-in-progress',
                        'Another update check is already in progress. Try again shortly.',
                    )
                self._busy()
                if not held:
                    self.lease, self.request_id = str(uuid.uuid4()), identifier
                    self.deadline = self.clock() + self.PREPARE_SECONDS
                    self.committed = False
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
            if not self.committed:
                self.deadline = self.clock() + self.COMMIT_SECONDS
                self.committed = True
            return {
                'ready': True,
                'lease': self.lease,
                'expiresInSeconds': max(0, self.deadline - self.clock()),
            }
