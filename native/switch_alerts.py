"""Confirmed model changes, scoped to this Mac. Never initiates a switch."""

import hashlib
import json
from model_combinations import selection_key, selection_label
from model_readiness import session_key

REASONS = {
    'paid_upgrade': 'Comparable paid work supports better earnings.',
    'paid_trial': 'Recent earnings fell; testing sustained demand. Higher earnings are not yet proven.',
    'idle_trial': 'The previous model was idle; testing a model with sustained demand.',
    'spike_trial': 'Testing an unusual, sustained demand spike to measure its earnings.',
    'trial_return': 'The trial did not justify staying; returning to the previous model.',
    'preferred_return': 'Returning to Gemma based on repeated paid history and current demand.',
    'fallback': 'Other trial options did not qualify; trying the supported traffic fallback.',
    'failed_trial': 'The previous trial did not produce enough paid work; trying another model.',
    'manual': 'Your manual selection is now warm and ready.',
    'scheduled': 'Scheduled model comparison to gather earnings evidence.',
    'recovery': 'Restoring the previous model after a loading problem.',
    'automatic': 'Automatic selection based on the saved optimizer decision.',
    'external': 'The new selection is warm and ready. No BloomGauge switch reason was recorded.',
    'home_return': 'Returning to the home model, the best-paying model on this Mac.',
    'excursion': 'Network evidence favours this model for now; BloomGauge returns home afterwards.',
}


def switch_reason(request_kind, mode=None, decision=None):
    decision = decision or {}
    if request_kind == 'manual-recovery' or request_kind == 'restore':
        return 'recovery'
    if request_kind.startswith('manual'):
        return 'manual'
    if request_kind == 'demand':
        if decision.get('kind') in ('home', 'excursion'):
            return 'home_return' if decision['kind'] == 'home' else 'excursion'
        if decision.get('explorationTrigger') == 'spike_return':
            return 'trial_return'
        if decision.get('kind') == 'earnings':
            return 'paid_upgrade'
        if (decision.get('preferredReturn') or {}).get('selection') == 'preferred_return':
            return 'preferred_return'
        if (decision.get('fallback') or {}).get('selection') == 'fallback':
            return 'fallback'
        reason = {
            'earnings_target': 'paid_trial',
            'idle': 'idle_trial',
            'demand_spike': 'spike_trial',
            'spike_return': 'trial_return',
            'failed_trial': 'failed_trial',
        }.get(decision.get('explorationTrigger'))
        return reason or 'automatic'
    if mode in ('week', 'combo'):
        return 'scheduled'
    return 'paid_upgrade' if mode == 'optimize' else 'automatic'


class SwitchAlerts:
    def __init__(self, history):
        self.h = history
        with self.h.lock:
            self.h.db.execute("""CREATE TABLE IF NOT EXISTS model_switch_alerts(
                id TEXT PRIMARY KEY,account TEXT,device TEXT,at REAL,
                previous TEXT,model TEXT,reason TEXT,queued INTEGER DEFAULT 0)""")
            self.h.db.commit()

    def record(self, account, device, session, previous, model, reason, now):
        if (
            not account
            or not device
            or not session
            or not previous
            or not model
            or previous == model
        ):
            return False
        if reason not in REASONS:
            reason = 'automatic'
        key = hashlib.sha256(
            json.dumps([account, device, session, model], sort_keys=True).encode()
        ).hexdigest()
        with self.h.lock:
            self.h.db.execute(
                'INSERT OR IGNORE INTO model_switch_alerts VALUES(?,?,?,?,?,?,?,0)',
                (key, account, device, now, previous, model, reason),
            )
            self.h.db.commit()
        return True

    def observe(self, account, device, raw, tracking, busy, now):
        # The caller verifies account/device identity. A queued, loading or
        # failed selection never advances the last confirmed model.
        if not account or not device or busy or not tracking.get('counting'):
            return
        model = selection_key(raw.get('advertised_models'))
        session = session_key(raw)
        if not model or not session:
            return
        key = (
            'switch-alert-observed:' + hashlib.sha256((account + ':' + device).encode()).hexdigest()
        )
        with self.h.lock:
            old = self.h.cache(key)
            if old and old.get('model') != model:
                # A controller success has already recorded the same session
                # with its precise reason; INSERT OR IGNORE preserves it.
                self.record(account, device, session, old['model'], model, 'external', now)
            current = {'session': session, 'model': model}
            if current != old:
                self.h.cache(key, current)

    def send_pending(self, account, device, push, now):
        with self.h.lock:
            rows = [
                dict(r)
                for r in self.h.db.execute(
                    """SELECT * FROM model_switch_alerts
                WHERE account=? AND device=? AND queued=0 AND at>=? ORDER BY at LIMIT 32""",
                    (account, device, now - 900),
                )
            ]
        for row in rows:
            accepted = push.enqueue_switch(
                account,
                row['id'],
                selection_label(row['previous']),
                selection_label(row['model']),
                row['reason'],
            )
            if accepted or push.has_event(account, row['id']):
                with self.h.lock:
                    self.h.db.execute(
                        'UPDATE model_switch_alerts SET queued=1 WHERE id=? AND account=? AND device=?',
                        (row['id'], account, device),
                    )
                    self.h.db.commit()
