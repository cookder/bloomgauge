"""Small, scope-bound display of the existing background demand comparison.

Reads never evaluate, query history, inspect the provider or advance a clock.
Freshness describes observations; it is not permission to execute a switch.
"""

import copy
import json
import math
import time

from demand_optimizer import policy
from demand_confirmation import (
    REVISION as CONFIRMATION_REVISION,
    scope as confirmation_scope,
    view as confirmation_view,
)
from model_combinations import members, selection_key, selection_label
from model_readiness import session_key
from optimizer_store import device_id
from optimizer_control import PLAN_FIELDS, digest

COMPARISON_SECONDS = 90
SOURCE_SECONDS = 90
MAX_ROWS = 16


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def nonnegative(value):
    value = number(value)
    return value if value is not None and value >= 0 else None


def text(value, limit=800):
    return value[:limit] if isinstance(value, str) and value else None


def recent(value, now, seconds, future=0):
    return number(value) is not None and -future <= now - value <= seconds


def binding(state, live, raw):
    """Only small in-memory fields from the exact evaluator inputs."""
    account, device = live.get('account'), live.get('device')
    models = state.get('models')
    current = selection_key(raw.get('advertised_models'))
    if (
        state.get('mode') != 'demand'
        or not account
        or not device
        or state.get('account') != account
        or state.get('device') != device
        or device_id(raw) != device
        or type(raw.get('pid')) is not int
        or raw['pid'] <= 0
        or number(raw.get('started_at')) is None
        or not current
        or not isinstance(models, list)
        or not 1 <= len(models) <= 128
        or any(not isinstance(m, str) or not 1 <= len(m) <= 512 for m in models)
        or len(set(models)) != len(models)
    ):
        return None
    try:
        rules = policy(state.get('demandPolicy'))
    except (TypeError, ValueError):
        return None
    return (
        account,
        device,
        session_key(raw),
        current,
        state['mode'],
        tuple(models),
        json.dumps(rules, sort_keys=True, allow_nan=False),
        state.get('startedAt'),
        state.get('lastSwitchAt'),
        state.get('expectedModel'),
    )


def paid_basis(value):
    if not isinstance(value, dict):
        return None
    rate = number(value.get('rate'))
    basis = (
        'recent_paid'
        if value.get('recent')
        else 'matched_history'
        if value.get('forecastUsable')
        else 'limited_history'
        if rate is not None
        else 'unknown'
    )
    return {
        'meanUsdPerWarmHour': rate,
        'lowerUsdPerWarmHour': number(value.get('lower')),
        'upperUsdPerWarmHour': number(value.get('upper')),
        'liveGuardUsdPerHour': nonnegative(value.get('liveGuardRate')),
        'recentGuardUsdPerHour': nonnegative(value.get('recentGuardRate')),
        'asOf': nonnegative(value.get('asOf')),
        'basis': basis,
        'detail': text(value.get('historyReason'))
        or (
            'Recent paid observations from this provider session.'
            if basis == 'recent_paid'
            else 'Observed paid history; the bounds are conservative planning values, not confidence intervals.'
        ),
    }


def candidate(model, value, target):
    value = value or {}
    estimate, signal = value.get('estimate') or {}, value.get('signal') or {}
    net = number(value.get('netGainUsd'))
    eligible = value.get('eligible') is True
    kind = value.get('kind') if value.get('kind') in ('earnings', 'explore') else None
    group = (
        'comparison_target'
        if model == target
        else 'unknown'
        if net is None
        else 'paid_alternative'
        if eligible and kind == 'earnings'
        else 'held'
        if not eligible and value
        else 'unknown'
    )
    return {
        'model': model,
        'name': text(value.get('name'), 512) or selection_label(model),
        'group': group,
        'eligible': eligible,
        'firstBlocker': text(value.get('reason'))
        or (None if value else 'No comparison is available for this model.'),
        'kind': kind,
        'selectionReason': text(value.get('selectionReason'), 120),
        'meanUsdPerWarmHour': number(estimate.get('rate')),
        'lowerUsdPerWarmHour': number(estimate.get('lower')),
        'upperUsdPerWarmHour': number(estimate.get('upper')),
        'netGainUsd': net,
        'load': nonnegative(signal.get('load')),
    }


def measurement(value, current):
    if (
        not isinstance(value, dict)
        or value.get('current') is not True
        or value.get('model') != current
    ):
        return None
    seconds, minutes = nonnegative(value.get('warmSeconds')), nonnegative(value.get('trialMinutes'))
    if value.get('status') not in ('running', 'settling') or seconds is None or not minutes:
        return None
    return {
        'model': current,
        'status': value['status'],
        'warmSeconds': seconds,
        'trialMinutes': minutes,
    }


def pending_on(o, expected_account, now):
    """Accepted, current On intent; inspect only its receipt and saved binding."""
    control = getattr(o, 'automatic_control', None)
    op = control.operation if control else None
    live = o.live or {}
    if (
        not op
        or o.state.get('mode') != 'observe'
        or op.get('status') not in ('pending', 'starting', 'waiting')
        or not any(r.get('id') == op.get('id') for r in control.requests[-32:])
        or number(op.get('at')) is None
        or number(op.get('expiresAt')) is None
        or not op['at'] <= now < op['expiresAt']
        or control.deadline is None
        or time.monotonic() >= control.deadline
        or o.stop.is_set()
        or not expected_account
        or op.get('account') != expected_account
        or live.get('account') != expected_account
        or not op.get('device')
        or live.get('device') != op['device']
        or op.get('plan') != digest({key: o.state.get(key) for key in PLAN_FIELDS})
    ):
        return None
    expected_session = op.get('startedSession') if op.get('startRequested') else op.get('session')
    if expected_session and expected_session != session_key(o.raw):
        return None
    return text(op.get('detail')) or 'Waiting for verified readiness before turning On.'


class OptimizerLive:
    def __init__(self, optimizer):
        self.optimizer = optimizer
        self.cached = None

    def record(self, decision, state, live, raw):
        """Background only. Display failure must not affect the decision path."""
        try:
            self._record(decision, state, live, raw)
        except Exception:
            with self.optimizer.lock:
                self.cached = None

    def _record(self, decision, state, live, raw):
        scope = binding(state, live, raw)
        current = selection_key(raw.get('advertised_models'))
        rules = policy(state.get('demandPolicy'))
        if (
            scope is None
            or decision.get('currentModel') != current
            or decision.get('policy') != rules
            or number(decision.get('at')) is None
        ):
            with self.optimizer.lock:
                self.cached = None
            return
        models = state['models']
        target = decision.get('target')
        if target is not None and (target not in models or target == current):
            raise ValueError('Comparison target is outside the saved alternatives.')
        opportunities = {
            v['model']: v
            for v in decision.get('opportunities', [])
            if isinstance(v, dict) and v.get('model') in models + [current]
        }
        rows = [
            candidate(model, opportunities.get(model), target)
            for model in models
            if model != current
        ]
        for row in rows:
            # A later return branch can override the original row selection.
            row['selectionReason'] = (
                (text(decision.get('explorationTrigger'), 120) or row['selectionReason'])
                if row['model'] == target
                else None
            )
        # The authoritative target may differ from both row order and net rank
        # after a return, learning or fallback branch. Never select a new winner.
        rows.sort(
            key=lambda r: (
                {'comparison_target': 0, 'paid_alternative': 1, 'held': 2, 'unknown': 3}[
                    r['group']
                ],
                -r['netGainUsd'] if r['group'] == 'paid_alternative' else 0,
            )
        )
        rows = rows[:MAX_ROWS]
        sources = {}
        for model, value in opportunities.items():
            stamp = (
                (value.get('sustained') or {}).get('sourceAt')
                if value.get('kind') == 'explore'
                else (value.get('signal') or {}).get('observedAt')
            )
            sources[model] = nonnegative(stamp)
        current_source = nonnegative(
            (opportunities.get(current, {}).get('signal') or {}).get('observedAt')
        )
        source = nonnegative(decision.get('sourceAt')) if target else current_source
        captured = {
            'binding': scope,
            'createdMonotonic': time.monotonic(),
            'at': decision['at'],
            'sourceAt': source,
            'currentSourceAt': current_source,
            'comparisonTarget': target,
            'sources': sources,
            'currentPaidBasis': paid_basis(decision.get('baseline')),
            'planningMinutes': nonnegative(decision.get('planningMinutes')),
            'earliestEligibleAt': nonnegative((decision.get('limits') or {}).get('nextRunAt')),
            'confirmationRequiredSeconds': rules['confirmationMinutes'] * 60,
            'measurement': measurement(decision.get('trial'), current),
            'candidates': rows,
        }
        with self.optimizer.lock:
            o = self.optimizer
            self.cached = captured if scope == binding(o.state, o.live or {}, o.raw) else None

    def snapshot(self, expected_account, now=None):
        now = time.time() if now is None else now
        with self.optimizer.lock:
            return self._snapshot(expected_account, now)

    def _snapshot(self, expected_account, now):
        o = self.optimizer
        state, live, raw = o.state, o.live or {}, o.raw
        mode = text(state.get('mode'), 32) or 'observe'
        current = selection_key(raw.get('advertised_models'))
        reason = text(o.detail) or 'Waiting for optimizer observations.'
        scope = binding(state, live, raw)
        cache = self.cached
        if (
            not expected_account
            or live.get('account') != expected_account
            or not scope
            or cache
            and cache['binding'] != scope
        ):
            self.cached = cache = None
        identity_matches = bool(
            expected_account
            and expected_account == live.get('account') == state.get('account')
            and live.get('device')
            and live.get('device') == state.get('device') == device_id(raw)
        )
        pending = state.get('pending') or {}
        pending_target = text(pending.get('model'), 512) if identity_matches else None
        local_fresh = (
            number(live.get('at')) is not None
            and -5 < now - live['at'] < 10
            and number(raw.get('written_at')) is not None
            and -5 < now - raw['written_at'] < 15
        )
        fresh = bool(
            cache
            and local_fresh
            and recent(cache['at'], now, COMPARISON_SECONDS)
            and 0 <= time.monotonic() - cache['createdMonotonic'] <= COMPARISON_SECONDS
            and recent(cache['sourceAt'], now, SOURCE_SECONDS)
            and recent(cache['currentSourceAt'], now, SOURCE_SECONDS)
            and (
                not cache['comparisonTarget']
                or recent(cache['sources'].get(cache['comparisonTarget']), now, SOURCE_SECONDS)
            )
        )
        result = {
            'schemaVersion': 1,
            'at': now,
            'mode': mode,
            'currentModel': current,
            'phase': 'watching',
            'reason': reason,
            'fresh': fresh,
            'lastComparisonAt': cache['at'] if cache else None,
            'sourceAt': cache['sourceAt'] if cache else None,
            'comparisonTarget': None,
            'proposalTarget': None,
            'pendingTarget': pending_target,
            'planningMinutes': None,
            'earliestEligibleAt': None,
            'currentPaidBasis': None,
            'measurement': None,
            'confirmation': None,
            'candidates': [],
        }
        if fresh:
            for key in (
                'comparisonTarget',
                'planningMinutes',
                'earliestEligibleAt',
                'currentPaidBasis',
                'candidates',
            ):
                result[key] = copy.deepcopy(cache[key])
            for row in result['candidates']:
                if not recent(cache['sources'].get(row['model']), now, SOURCE_SECONDS):
                    row.update(
                        group='unknown',
                        eligible=False,
                        firstBlocker='Waiting for fresh demand readings for this model.',
                        meanUsdPerWarmHour=None,
                        lowerUsdPerWarmHour=None,
                        upperUsdPerWarmHour=None,
                        netGainUsd=None,
                        load=None,
                        selectionReason=None,
                    )
            result['candidates'].sort(
                key=lambda r: {
                    'comparison_target': 0,
                    'paid_alternative': 1,
                    'held': 2,
                    'unknown': 3,
                }[r['group']]
            )
        intent = (getattr(getattr(o, 'automatic_control', None), 'operation', None) or {}).get('id')
        proposal = (
            confirmation_view(
                state.get('demandProposal'), confirmation_scope(state, raw, intent), now
            )
            or {}
        )
        retained = (
            scope is not None
            and mode == 'demand'
            and proposal.get('revision') == CONFIRMATION_REVISION
            and proposal.get('session') == confirmation_scope(state, raw, intent)
            and proposal.get('model') in (state.get('models') or [])
            and proposal.get('model') != current
            and recent(proposal.get('checkedAt'), now, 150)
            and number(proposal.get('expiresAt')) is not None
            and now <= proposal['expiresAt']
        )
        legacy = (
            fresh
            and proposal.get('model') == cache['comparisonTarget']
            and proposal.get('model')
            and recent(proposal.get('checkedAt'), now, 150)
            and recent(proposal.get('sourceAt'), now, SOURCE_SECONDS)
        )
        if retained or legacy:
            seconds = nonnegative(proposal.get('seconds'))
            required = nonnegative(
                proposal.get(
                    'requiredSeconds', cache['confirmationRequiredSeconds'] if cache else None
                )
            )
            if seconds is not None and required is not None:
                result['proposalTarget'] = proposal['model']
                result['confirmation'] = {
                    'model': proposal['model'],
                    'seconds': seconds,
                    'requiredSeconds': required,
                }
                if retained:
                    result['confirmation'].update(
                        status=proposal.get('status') if fresh else 'paused',
                        reason=proposal.get('reason')
                        if fresh
                        else 'Waiting for fresh evidence; retained confirmation is paused.',
                        expiresAt=proposal['expiresAt'],
                    )
        # Execution status is authoritative. In particular, a held historical
        # comparison cannot replace an actual wait for local readings or heat.
        guard = getattr(o, 'update_guard', None)
        updating = bool(guard and guard.lease and time.monotonic() < guard.deadline)
        on_detail = pending_on(o, expected_account, now)
        if pending_target:
            result['phase'] = 'switching'
        elif on_detail:
            result.update(phase='waiting', reason=on_detail)
        elif mode == 'observe' and not state.get('requestedModel'):
            result['phase'] = 'off'
        elif o.status in ('waiting', 'warming') or reason.lower().startswith(
            ('waiting ', 'could not evaluate', 'provider is offline')
        ):
            result['phase'] = 'waiting'
        elif updating:
            result.update(
                phase='waiting',
                reason='An app update is in progress. Automatic comparisons are paused.',
            )
        elif not local_fresh or not identity_matches:
            result.update(
                phase='waiting', reason='Waiting for fresh local account and provider readings.'
            )
        elif (live.get('provider') or {}).get('online') is not True:
            result.update(
                phase='waiting',
                reason='Provider is offline. Waiting for the running provider to be observed.',
            )
        elif result['proposalTarget']:
            result['phase'] = 'waiting' if 'minimum run' in reason.lower() else 'confirming'
        elif fresh and cache['measurement'] and o.status == 'optimizing' and mode == 'demand':
            warm = getattr(o, 'warmup', {})
            if (
                warm.get('status') == 'ready'
                and warm.get('session') == session_key(raw)
                and warm.get('model') == current
                and all(m in (raw.get('warm_models') or []) for m in members(current))
            ):
                result['phase'] = 'measuring'
                result['measurement'] = copy.deepcopy(cache['measurement'])
            else:
                result.update(
                    phase='waiting',
                    reason='Waiting for verified warm readiness before measuring this trial.',
                )
        elif mode == 'demand' and not fresh:
            result.update(
                phase='waiting', reason='Waiting for a fresh background comparison. ' + reason
            )
        if result['phase'] != 'confirming' and not (retained and result['phase'] == 'waiting'):
            result['confirmation'] = None
        return result
