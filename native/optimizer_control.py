"""Small cached controls and a bounded, explicit enable intent.

HTTP reads never build earnings summaries or inspect the provider. All provider
inspection, readiness checks and optional Start dispatch run in the worker.
An unfinished intent is cancelled on restart; an old request ID never replays.
"""

import copy
import logging
import bloom_log  # noqa: F401

log = logging.getLogger('bloom.optimizer_control')
import hashlib
import json
import math
import threading
import time
import uuid
from demand_optimizer import policy as demand_policy
from model_combinations import members, selection_key
from model_readiness import session_key

INTENT_SECONDS = 600
CACHE_SECONDS = 15
PLAN_FIELDS = (
    'models',
    'blockHours',
    'startedAt',
    'endsAt',
    'originalModel',
    'expectedModel',
    'lastSwitchAt',
    'account',
    'device',
    'demandPolicy',
)


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
    ).hexdigest()


def saved_plan(state):
    return finite(state.get('startedAt')) and state.get('endsAt') is None


class OptimizerControl:
    def __init__(self, optimizer):
        self.o = optimizer
        self.cache_lock = threading.Lock()
        self.wake = threading.Event()
        self.deadline = None
        stored = optimizer.h.cache('optimizer-control') or {}
        self.requests = stored.get('requests', [])[-32:]
        self.operation = stored.get('operation')
        if self.operation and self.operation.get('status') in ('pending', 'starting', 'waiting'):
            self.operation.update(
                status='blocked',
                detail='Bloomkeeper restarted before On was ready. Review the current status, then turn On again.',
                blocker={'code': 'interrupted', 'action': 'retry'},
            )
            self.save()
        self.view = {
            'at': 0,
            'controlVersion': '',
            'providerVersion': None,
            'currentModel': None,
            'providerRunning': False,
            'canOptimize': False,
            'actualMode': optimizer.state.get('mode', 'observe'),
            'operation': None,
            'lastRequestId': None,
            'hasSavedPlan': saved_plan(optimizer.state),
            'firstPlan': optimizer.state.get('startedAt') is None,
            'selected': [],
            'models': [],
            'demandPolicy': copy.deepcopy(optimizer.state.get('demandPolicy', {})),
            'warmup': {},
            'automatic': {
                'mode': 'manual',
                'phase': 'waiting',
                'detail': 'Reading this Mac’s provider status.',
                'canEnable': False,
            },
        }
        self.context = None

    def save(self):
        self.o.h.cache(
            'optimizer-control', {'requests': self.requests, 'operation': self.operation}
        )

    def snapshot(self):
        with self.cache_lock:
            return copy.deepcopy(self.view)

    def intent_current(self, intent_id):
        with self.o.lock:
            return bool(
                self.operation
                and self.operation.get('id') == intent_id
                and self.operation.get('status') in ('pending', 'starting', 'waiting')
                and time.time() < self.operation['expiresAt']
                and self.deadline is not None
                and time.monotonic() < self.deadline
                and not self.o.stop.is_set()
            )

    def cancel(self, detail, keep=None):
        """Called by every manual/legacy pause or provider-control admission."""
        with self.o.lock:
            if (
                self.operation
                and self.operation.get('id') != keep
                and self.operation.get('status') in ('pending', 'starting', 'waiting')
            ):
                self.operation.update(status='cancelled', detail=detail)
                self.deadline = None
                self.save()
                with self.cache_lock:
                    self.view['automatic'] = {
                        'mode': 'manual',
                        'phase': 'manual',
                        'detail': detail,
                        'canEnable': False,
                    }
                self.wake.set()

    def projection(self, now):
        """Background only: inspect provider and project bounded, history-free data."""
        o = self.o
        try:
            provider = o.provider_control.inspect()
            provider_issue = None
        except (OSError, ValueError, TypeError, KeyError):
            provider = {
                'status': 'unknown',
                'version': None,
                'model': None,
                'options': [],
                'environment': {},
                'raw': {},
            }
            provider_issue = 'Set up Darkbloom on this Mac, then refresh its status.'
        with o.lock:
            state = copy.deepcopy(o.state)
            live = copy.deepcopy(o.live) or {}
            raw = copy.deepcopy(o.raw)
            op = copy.deepcopy(self.operation)
            pending = bool(op and op.get('status') in ('pending', 'starting', 'waiting'))
            rows = o.candidates({}, {}, live, state)
            models = [
                {key: row.get(key) for key in ('id', 'name', 'available', 'reason', 'selected')}
                for row in rows
            ]
            actual = state.get('mode', 'observe')
            automatic = actual != 'observe'
            current = provider.get('model') or selection_key(raw.get('advertised_models'))
            serving = next((row for row in rows if row['id'] == current), None)
            detail = 'Manual mode. The current model keeps serving.'
            phase = 'manual'
            blocker = None
            if pending:
                phase = 'starting' if op['status'] == 'starting' else 'waiting'
                detail = op['detail']
            elif op and op.get('status') == 'blocked' and not automatic:
                phase = 'blocked'
                detail = op['detail']
                blocker = op.get('blocker')
            elif automatic:
                phase = (
                    'active'
                    if provider['status'] == 'running' and o.status not in ('waiting', 'warming')
                    else 'waiting'
                )
                detail = (
                    (o.detail or 'Automatic switching is on.')
                    if provider['status'] == 'running'
                    else 'Automatic switching is waiting for fresh provider status. No automatic Start is sent.'
                )
            elif provider_issue:
                phase = 'blocked'
                detail = provider_issue
                blocker = {'code': 'provider-setup', 'action': 'controller'}
            elif provider['status'] == 'stopped':
                detail = 'Darkbloom is stopped. Turning On starts its saved model, then waits for verified readiness.'
            can_enable = bool(
                not pending
                and not automatic
                and current
                and provider['status'] in ('running', 'stopped')
                and live.get('account')
                and live.get('device')
                and not state.get('pending')
                and not state.get('requestedModel')
                and not (o.worker and o.worker.is_alive())
                and not o.update_guard.active()
            )
            if not pending and not automatic:
                problem = None
                if o.update_guard.active():
                    problem = (
                        'An app update is in progress. Wait for it to finish, then turn On.',
                        'update',
                        'retry',
                    )
                elif (
                    state.get('pending')
                    or state.get('requestedModel')
                    or o.worker
                    and o.worker.is_alive()
                ):
                    problem = (
                        'Wait for the current provider operation to finish before turning On.',
                        'operation',
                        'retry',
                    )
                elif provider_issue:
                    problem = (provider_issue, 'provider-setup', 'controller')
                elif provider['status'] not in ('running', 'stopped'):
                    problem = (
                        'Waiting for a fresh provider status. Refresh before turning On.',
                        'provider-status',
                        'retry',
                    )
                elif not live.get('account') or not live.get('device'):
                    problem = (
                        'Waiting to match this Mac and the signed-in account. Refresh the status.',
                        'identity',
                        'retry',
                    )
                elif len(members(current)) != 1:
                    problem = (
                        'Automatic demand following needs one serving model. Choose one in the manual model controls in Optimizer → Overview; multi-model reporting stays available.',
                        'model-scope',
                        'controller',
                    )
                elif state.get('startedAt') is not None and not saved_plan(state):
                    problem = (
                        'The previous experiment needs a new plan. Review its settings before turning On.',
                        'completed-plan',
                        'configure',
                    )
                elif o.discovery_error or not 0 <= now - o.discovery_at < 600:
                    problem = ('Refresh the model catalog before turning On.', 'catalog', 'retry')
                elif (
                    provider['status'] == 'running'
                    and serving
                    and not serving['available']
                    and o.device_identity_ok
                    and o.identity_session == (raw.get('started_at'), raw.get('pid'))
                    and 0 <= now - o.identity_at < 180
                ):
                    problem = (
                        serving.get('reason')
                        or 'The serving model is not available for automatic selection.',
                        'serving-unavailable',
                        'controller',
                    )
                elif saved_plan(state) and current not in state.get('models', []):
                    problem = (
                        'The serving model is not in the saved pool. Review the saved model selection.',
                        'saved-pool',
                        'configure',
                    )
                if problem:
                    detail, code, action = problem
                    phase = 'waiting' if code == 'operation' else 'blocked'
                    blocker = {'code': code, 'action': action}
                    can_enable = False
            base_version = o.control_version()
            context = {
                'baseVersion': base_version,
                'account': live.get('account'),
                'device': live.get('device'),
                'plan': digest({key: state.get(key) for key in PLAN_FIELDS}),
                'launch': digest(
                    [provider.get('model'), provider['options'], provider['environment']]
                ),
                'session': session_key(provider['raw']),
                'provider': provider,
                'hasSavedPlan': saved_plan(state),
                'firstPlan': state.get('startedAt') is None,
            }
            version = digest(
                [
                    base_version,
                    context['account'],
                    context['device'],
                    context['launch'],
                    provider.get('version'),
                    op.get('id') if op else None,
                    op.get('status') if op else None,
                ]
            )
            status = {
                'mode': 'on' if pending or automatic else 'manual',
                'phase': phase,
                'detail': detail,
                'canEnable': can_enable,
            }
            if op:
                status['intentId'] = op['id']
            if blocker:
                status['blocker'] = blocker
            view = {
                'at': now,
                'controlVersion': version,
                'providerVersion': provider.get('version'),
                'currentModel': current,
                'providerRunning': provider['status'] == 'running',
                'actualMode': actual,
                'hasSavedPlan': context['hasSavedPlan'],
                'firstPlan': context['firstPlan'],
                'selected': state.get('models', []),
                'models': models,
                'demandPolicy': copy.deepcopy(state.get('demandPolicy', {})),
                'warmup': {
                    key: value for key, value in o.warmup.items() if key in ('status', 'detail')
                },
                'automatic': status,
                'operation': {key: op[key] for key in ('id', 'status', 'detail')} if op else None,
                'lastRequestId': self.requests[-1]['id'] if self.requests else None,
            }
            with self.cache_lock:
                self.context = context
                self.view = view
        return view

    def action(self, data, source='mac'):
        """Only validate cached state and persist intent; never inspect or dispatch."""
        allowed = {
            'action',
            'enabled',
            'requestId',
            'expectedControl',
            'expectedProvider',
            'models',
            'demandPolicy',
        }
        if (
            not isinstance(data, dict)
            or set(data) - allowed
            or data.get('action') not in ('set-automatic', 'refresh')
        ):
            raise ValueError('Choose On, Manual or Refresh.')
        try:
            request_id = str(uuid.UUID(data.get('requestId', '')))
        except (ValueError, TypeError, AttributeError):
            raise ValueError('A valid request ID is required.') from None
        refresh = data['action'] == 'refresh'
        if refresh and set(data) != {'action', 'requestId', 'expectedControl'}:
            raise ValueError('Refresh does not change optimizer settings.')
        if not refresh and type(data.get('enabled')) is not bool:
            raise ValueError('Choose On or Manual.')
        signature = digest(data)
        o = self.o
        with o.lock:
            old = next((row for row in self.requests if row['id'] == request_id), None)
            if old:
                if old['signature'] != signature:
                    raise ValueError('This request ID was already used for another action.')
                return self.snapshot()
            view = self.snapshot()
            context = copy.deepcopy(self.context)
            enable = not refresh and data['enabled']
            if refresh:
                if not isinstance(data.get('expectedControl'), str):
                    raise ValueError('A control version is required.')
            else:
                if not context or data.get('expectedControl') != view['controlVersion']:
                    raise ValueError('The control status changed. Refresh it before trying again.')
                if enable and not 0 <= time.time() - view['at'] < CACHE_SECONDS:
                    raise ValueError('The control status changed. Refresh it before trying again.')
                if not enable and context['baseVersion'] != o.control_version():
                    op = self.operation or {}
                    bound = (
                        view['automatic'].get('intentId') == op.get('id')
                        and op.get('status') in ('pending', 'starting', 'waiting', 'active')
                        and op.get('plan') == digest({key: o.state.get(key) for key in PLAN_FIELDS})
                    )
                    if not bound:
                        raise ValueError(
                            'The saved plan changed. Read its current status before choosing Manual.'
                        )
            if enable and context['baseVersion'] != o.control_version():
                raise ValueError(
                    'The provider or saved plan changed. Wait for fresh control status.'
                )
            if (
                not refresh
                and not enable
                and set(data) - {'action', 'enabled', 'requestId', 'expectedControl'}
            ):
                raise ValueError('Manual mode does not replace the saved plan.')
            if enable:
                o.update_guard.require_available()
                if not view['automatic']['canEnable']:
                    raise ValueError(view['automatic']['detail'])
                if not context['firstPlan'] and set(data) & {'models', 'demandPolicy'}:
                    raise ValueError('On resumes the saved plan without replacing its settings.')
                if not context['firstPlan'] and not context['hasSavedPlan']:
                    raise ValueError('Review the completed plan before starting a new one.')
                if (
                    context['provider']['status'] == 'stopped'
                    and data.get('expectedProvider') != view['providerVersion']
                ):
                    raise ValueError('Review the stopped provider before turning On.')
                if len(members(view['currentModel'])) != 1:
                    raise ValueError(
                        'Automatic demand following needs one serving model. Use the manual model controls in Optimizer → Overview to choose it.'
                    )
                if context['firstPlan']:
                    models = data.get('models')
                    if (
                        not isinstance(models, list)
                        or not 2 <= len(models) <= 16
                        or any(not isinstance(m, str) or not m for m in models)
                        or len(set(models)) != len(models)
                        or view['currentModel'] not in models
                    ):
                        raise ValueError(
                            'Review two to sixteen models, including the current model, before turning On.'
                        )
                    policy = demand_policy(data.get('demandPolicy', view['demandPolicy']))
                else:
                    models = None
                    policy = None
            if not refresh and not enable:
                self.cancel(
                    'Manual mode selected. Automatic switching is paused; the current model keeps serving.'
                )
                o.pause_automatic(source)
            before = copy.deepcopy((self.requests, self.operation))
            self.requests = (self.requests + [{'id': request_id, 'signature': signature}])[-32:]
            if enable:
                now = time.time()
                self.operation = {
                    'id': request_id,
                    'status': 'pending',
                    'detail': 'Checking readiness before turning On.',
                    'at': now,
                    'expiresAt': now + INTENT_SECONDS,
                    'source': source,
                    'account': context['account'],
                    'device': context['device'],
                    'plan': context['plan'],
                    'launch': context['launch'],
                    'session': context['session'],
                    'providerVersion': context['provider']['version'],
                    'providerStatus': context['provider']['status'],
                    'currentModel': view['currentModel'],
                    'firstPlan': context['firstPlan'],
                    'models': models,
                    'policy': policy,
                    'startRequested': False,
                }
            try:
                self.save()
            except Exception:
                self.requests, self.operation = before
                raise
            if enable:
                self.deadline = time.monotonic() + INTENT_SECONDS
            if refresh:
                o.next_discovery = 0
                o.next_identity = 0
            with self.cache_lock:
                self.view['lastRequestId'] = request_id
                self.view['operation'] = (
                    {key: self.operation[key] for key in ('id', 'status', 'detail')}
                    if self.operation
                    else None
                )
                # A new action gets a new token immediately, even before a slow
                # worker projection, so an older Manual cannot cancel a new On.
                self.view['controlVersion'] = digest([o.control_version(), request_id])
                if enable:
                    self.view['automatic'] = {
                        'mode': 'on',
                        'phase': 'waiting',
                        'detail': self.operation['detail'],
                        'intentId': request_id,
                        'canEnable': False,
                    }
                elif not refresh:
                    self.view['actualMode'] = 'observe'
                    self.view['automatic'] = {
                        'mode': 'manual',
                        'phase': 'manual',
                        'detail': 'Automatic switching is paused. The current model keeps serving.',
                        'canEnable': False,
                    }
            self.wake.set()
            return self.snapshot()

    def block(self, detail, code='review', action='retry'):
        self.operation.update(
            status='blocked', detail=detail, blocker={'code': code, 'action': action}
        )
        self.save()

    def matches_plan(self, op):
        o = self.o
        live = o.live or {}
        return (
            digest({key: o.state.get(key) for key in PLAN_FIELDS}) == op['plan']
            and o.state.get('mode') == 'observe'
            and live.get('account') == op['account']
            and live.get('device') == op['device']
        )

    def update_operation(self, op, **changes):
        with self.o.lock:
            if self.intent_current(op['id']):
                self.operation.update(changes)
                self.save()

    def fail_operation(self, op, detail, code='review', action='retry'):
        with self.o.lock:
            if (
                self.operation
                and self.operation.get('id') == op['id']
                and self.operation.get('status') in ('pending', 'starting', 'waiting')
            ):
                self.block(detail, code, action)

    def advance(self, now):
        o = self.o
        with o.lock:
            op = copy.deepcopy(self.operation)
            if not op or op.get('status') not in ('pending', 'starting', 'waiting'):
                return
            if now >= op['expiresAt'] or self.deadline is None or time.monotonic() >= self.deadline:
                self.block(
                    'On timed out while waiting for verified readiness. The saved plan is kept; review the status and try again.',
                    'timeout',
                )
                return
            if o.stop.is_set():
                return
            if not self.matches_plan(op):
                self.block(
                    'The account, Mac or saved plan changed. Review it before turning On.',
                    'plan-changed',
                    'configure',
                )
                return
            saved = copy.deepcopy(o.state)
        # Slow inspection/admission runs outside the optimizer lock. Revalidate
        # the intent and all cached bindings immediately before any state commit.
        o.update_guard.require_available()
        provider = o.provider_control.inspect()
        if (
            digest([provider.get('model'), provider['options'], provider['environment']])
            != op['launch']
        ):
            self.fail_operation(
                op,
                'The provider launch settings changed. Review them before turning On.',
                'provider-changed',
                'controller',
            )
            return
        if not op['startRequested']:
            if session_key(provider['raw']) != op['session']:
                self.fail_operation(
                    op,
                    'The provider session changed. Review the current model before turning On.',
                    'session-changed',
                )
                return
            if provider['status'] == 'stopped':
                if op['providerStatus'] != 'stopped':
                    self.fail_operation(
                        op,
                        'Darkbloom stopped after you selected On. Review the stopped provider before trying again.',
                        'provider-stopped',
                        'controller',
                    )
                    return
                with o.lock:
                    if not self.intent_current(op['id']) or not self.matches_plan(op):
                        return
                    self.operation.update(
                        status='starting',
                        detail='Starting the saved provider model; automatic switching is waiting.',
                        startRequested=True,
                    )
                    self.save()
                o.provider_control.action(
                    {
                        'action': 'provider-start',
                        'requestId': op['id'],
                        'expectedProvider': op['providerVersion'],
                    },
                    op['source'],
                    automatic_id=op['id'],
                )
                return
            if provider['status'] != 'running':
                self.update_operation(
                    op, status='waiting', detail='Waiting for fresh provider status.'
                )
                return
        else:
            with o.lock:
                result = copy.deepcopy(o.state.get('providerResult') or {})
                worker_running = bool(o.worker and o.worker.is_alive())
            if result.get('id') != op['id']:
                self.fail_operation(
                    op,
                    'Another provider command replaced this request. Automatic switching stays Manual.',
                    'provider-changed',
                )
                return
            if result.get('status') == 'working' or worker_running:
                return
            if result.get('status') != 'completed':
                self.fail_operation(
                    op,
                    result.get('detail') or 'The provider start could not be verified.',
                    'start-failed',
                    'controller',
                )
                return
            if provider['status'] != 'running':
                self.fail_operation(
                    op,
                    'The provider is not running after Start. Check the model controls in Optimizer → Overview before trying again.',
                    'provider-stopped',
                    'controller',
                )
                return
            if not op.get('startedSession'):
                self.update_operation(op, startedSession=session_key(provider['raw']))
            elif session_key(provider['raw']) != op['startedSession']:
                self.fail_operation(
                    op,
                    'The provider session changed again. Review it before turning On.',
                    'session-changed',
                )
                return
        with o.lock:
            if not self.intent_current(op['id']):
                return
            if (
                o.state.get('pending')
                or o.state.get('requestedModel')
                or o.worker
                and o.worker.is_alive()
            ):
                self.update_operation(
                    op,
                    status='waiting',
                    detail='Waiting for the current provider operation to finish.',
                )
                return
        try:
            if op['firstPlan']:
                saved.update(
                    models=op['models'],
                    startedAt=now,
                    endsAt=None,
                    account=op['account'],
                    device=op['device'],
                    expectedModel=op['currentModel'],
                )
            current, live, raw, allowed = o.demand_resume_context(saved, op['currentModel'])
            if op['firstPlan'] and set(op['models']) != allowed:
                raise ValueError(
                    'Some reviewed models are no longer available. Review the model selection before turning On.'
                )
        except ValueError as error:
            detail = str(error)
            waiting = any(
                text in detail.lower()
                for text in ('wait', 'stale', 'battery', 'hot', 'refresh the model catalog')
            )
            if waiting:
                self.update_operation(op, status='waiting', detail=detail)
            else:
                self.fail_operation(
                    op,
                    detail,
                    'readiness',
                    'configure' if 'selection' in detail or 'model' in detail else 'controller',
                )
            return
        with o.lock:
            if not self.intent_current(op['id']):
                return
            if not self.matches_plan(op) or session_key(o.raw) != session_key(raw):
                self.block(
                    'The provider or saved plan changed during verification. Review it before turning On.',
                    'changed',
                )
                return
            if (
                o.state.get('pending')
                or o.state.get('requestedModel')
                or o.worker
                and o.worker.is_alive()
                or o.warmup_worker
                and o.warmup_worker.is_alive()
                or o.command_lock.locked()
            ):
                self.block(
                    'Another provider operation started during verification. Automatic switching stays Manual.',
                    'operation',
                )
                return
            if not o.tracking(o.raw, time.time())['counting']:
                self.operation.update(
                    status='waiting',
                    detail='Waiting for fresh verified readiness before turning On.',
                )
                self.save()
                return
            o.update_guard.require_available()
            before = copy.deepcopy(o.state)
            if op['firstPlan']:
                o.state.update(
                    mode='demand',
                    models=[current] + [m for m in op['models'] if m != current],
                    blockHours=2,
                    startedAt=now,
                    endsAt=None,
                    originalModel=current,
                    expectedModel=current,
                    lastSwitchAt=raw['started_at'],
                    requestedModel=None,
                    account=op['account'],
                    device=op['device'],
                    demandPolicy=op['policy'],
                )
            else:
                if current != o.state.get('expectedModel'):
                    o.state.update(expectedModel=current, lastSwitchAt=now)
                    o.state.pop('rollbackModel', None)
                o.state['mode'] = 'demand'
            o.state.pop('demandProposal', None)
            try:
                o.save()
            except Exception:
                o.state = before
                raise
            o.proposal = None
            o.status = 'optimizing'
            o.detail = 'Automatic switching is on. The saved plan and history are preserved.'
            self.operation.update(
                status='active',
                detail=o.detail,
                plan=digest({key: o.state.get(key) for key in PLAN_FIELDS}),
            )
            self.save()
        o.store.event(
            op['account'],
            op['device'],
            now,
            'started' if op['firstPlan'] else 'resumed',
            current,
            'Demand following enabled from the explicit On control.'
            if op['firstPlan']
            else 'Saved demand plan resumed after explicit On and fresh readiness verification.',
        )

    def tick(self, now=None):
        now = time.time() if now is None else now
        with self.o.lock:
            intent_id = self.operation.get('id') if self.operation else None
        try:
            self.advance(now)
        except Exception as error:
            with self.o.lock:
                if (
                    self.operation
                    and self.operation.get('id') == intent_id
                    and self.operation.get('status') in ('pending', 'starting', 'waiting')
                ):
                    self.block(
                        str(error)
                        if isinstance(error, ValueError)
                        else 'The request could not be verified. Automatic switching stays Manual; review the status and try again.',
                        'verification',
                    )
        self.projection(now)

    def run(self):
        while not self.o.stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception('Optimizer control tick failed')
            self.wake.wait(3)
            self.wake.clear()
