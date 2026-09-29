"""Bounded cache recovery for one explicit On intent starting a stopped model.

Only the provider worker calls this, with the command lock held. Cached pages
authorize one cleanup attempt; only a new hardware reading can authorize Start.
"""

import copy
import time
from cache_recovery import clear_file_cache, CacheRecoveryError
from model_combinations import (
    combination_config_error,
    configured_reserve_gb,
    members,
    pair_budget,
)
from model_readiness import session_key
from optimizer_store import device_id


class OnStart:
    def __init__(self, optimizer, request_id, expected):
        self.o = optimizer
        self.control = optimizer.automatic_control
        self.request_id = request_id
        self.expected = expected
        with optimizer.lock:
            self.op = copy.deepcopy(self.control.operation)
        self.cleared_at = None

    def bound(self):
        o = self.o
        op = self.op
        pending = o.state.get('pending') or {}
        return bool(
            o.automatic_control is self.control
            and op
            and op['id'] == self.request_id
            and self.control.intent_current(self.request_id)
            and self.control.matches_plan(op)
            and o.control_version() == self.expected['admittedControlVersion']
            and pending.get('requestId') == self.request_id
            and pending.get('kind') == 'provider'
            and pending.get('model') == self.expected['model']
            and (o.state.get('providerResult') or {}).get('id') == self.request_id
            and (o.state.get('providerResult') or {}).get('status') == 'working'
        )

    def current(self):
        o = self.o
        expected = self.expected
        current = o.provider_control.inspect()
        with o.lock:
            if not self.bound():
                raise ValueError(
                    'On was cancelled or its saved plan changed. No further start will be sent.'
                )
            o.update_guard.require_available()
            live = copy.deepcopy(o.live) or {}
            raw = copy.deepcopy(o.raw)
            if (
                current['status'] != 'stopped'
                or current['disabled'] not in (True, False)
                or any(
                    current[k] != expected[k]
                    for k in ('model', 'options', 'environment', 'disabled')
                )
                or current['controlVersion'] != expected['admittedControlVersion']
                or session_key(current['raw']) != self.op['session']
                or session_key(raw) != self.op['session']
                or device_id(current['raw']) != self.op['device']
                or device_id(raw) != self.op['device']
            ):
                raise ValueError(
                    'The provider, account or launch settings changed. No further start will be sent.'
                )
        return current, live

    def checked(self, ignore_own_cooldown=False):
        from optimizer import finite, memory_budget

        o = self.o
        current, live = self.current()
        with o.lock:
            raw = copy.deepcopy(o.raw)
            state = copy.deepcopy(o.state)
        reason = o.endpoint_notice(current['options']) or o.manual_selection.common_reason(
            current, self.op['source'], live, raw, time.time()
        )
        if reason:
            raise ValueError(reason)
        models = members(current['model'])
        rows = {r['id']: r for r in o.candidates({}, {}, live, state)}
        if len(models) == 1:
            row = rows.get(models[0])
            if not row or not row['available']:
                raise ValueError(
                    (row or {}).get('reason')
                    or 'The saved model is no longer available for automatic selection.'
                )
        # A pair (the manager holds one without gemma) is checked model by model, and its
        # memory as a pair, as the provider controls' Start does. It was looked up as one
        # model and refused as "no longer available".
        for model in models:
            row = rows.get(model)
            if not row or not row['available']:
                raise ValueError(
                    '%s is not available for automatic selection: %s'
                    % (model, (row or {}).get('reason') or 'it is not in the network catalog.')
                )
        reserve = configured_reserve_gb(o.home, current['options'])
        if len(models) == 2:
            error = combination_config_error(
                o.home, current['options'], current['environment'], voluntary=False
            )
            if error:
                raise ValueError(error)
            budget = pair_budget(
                live.get('hardware', {}),
                {'memoryGB': 0},
                models,
                [rows[m]['memoryGB'] for m in models],
                config_reserve=reserve,
            )
        elif models:
            budget = memory_budget(
                live.get('hardware', {}),
                {'memoryGB': 0},
                models[0],
                rows[models[0]]['memoryGB'],
                config_reserve=reserve,
            )
        else:
            raise ValueError('The saved model is no longer available for automatic selection.')
        if not budget:
            raise ValueError(
                'Waiting for verified memory estimates for the saved model. Refresh before turning On.'
            )
        if self.cleared_at is not None:
            hardware_at = (live.get('hardware') or {}).get('at')
            if not finite(hardware_at) or hardware_at <= self.cleared_at:
                raise ValueError(
                    'Waiting for newly measured memory after cache cleanup. No start was sent.'
                )
        if (
            ignore_own_cooldown
            and (state.get('cacheRecovery') or {}).get('requestId') == self.request_id
        ):
            state['cacheRecovery'] = {}
        # Power checks above can be slow. Revalidate the binding afterwards.
        self.current()
        return current, live, state, budget

    def progress(self, detail):
        with self.o.lock:
            if not self.bound():
                raise ValueError('On was cancelled. No further start will be sent.')
            self.o.state['providerResult']['detail'] = detail
            self.o.save()
            self.control.update_operation(self.op, status='starting', detail=detail)
        self.control.wake.set()

    @staticmethod
    def shortfall(budget):
        return (
            'Not enough newly measured memory after cache cleanup: '
            f'{budget["afterUnloadGB"]:.1f} GB available; {budget["requiredGB"]:.1f} GB required '
            f'({max(0, budget["requiredGB"] - budget["afterUnloadGB"]):.1f} GB more needed). '
            'Close other workloads, then turn On again. The provider remains stopped.'
        )

    def prepare(self):
        from optimizer import finite

        o = self.o
        current, _ = self.current()
        o.verify_local_target(current['model'], current['options'])
        current, live, state, budget = self.checked()
        if budget['afterUnloadGB'] >= budget['requiredGB']:
            return
        authorization = o.manual_selection.permission_status(force=True)
        current, live, state, budget = self.checked()
        if budget['afterUnloadGB'] >= budget['requiredGB']:
            return
        recovery = o.manual_selection.cache_plan(
            current, live, state, budget, time.time(), True, authorization
        )
        if not recovery['canAttempt']:
            detail = (
                'Cache cleanup is cooling down. Wait ten minutes after the previous cleanup, then turn On again.'
                if recovery['retryAt'] is not None
                else recovery['detail']
            )
            raise ValueError(detail + ' The provider remains stopped.')
        with o.lock:
            if not self.bound():
                raise ValueError('On was cancelled before cache cleanup. No cleanup was sent.')
            receipt = next(r for r in o.state['providerRequests'] if r['id'] == self.request_id)
            last = o.state.get('cacheRecovery') or {}
            now = time.time()
            if receipt.get('cacheRecoveryAttempted') or last.get('requestId') == self.request_id:
                raise ValueError(
                    'Cache cleanup was already attempted for this On request. Refresh its result.'
                )
            if finite(last.get('at')) and now - last['at'] < 600:
                raise ValueError(
                    'Cache cleanup is cooling down. Wait ten minutes after the previous cleanup, then turn On again.'
                )
            receipt['cacheRecoveryAttempted'] = True
            o.state['cacheRecovery'] = {
                'requestId': self.request_id,
                'kind': 'automatic-stopped-start',
                'at': now,
                'status': 'running',
                'model': current['model'],
                'detail': 'Clearing file cache once before the explicit On start.',
            }
            o.save()  # A failed save can never dispatch cleanup.
        self.progress('Clearing file cache once before starting the saved model.')
        current, live, state, budget = self.checked(ignore_own_cooldown=True)
        if budget['afterUnloadGB'] >= budget['requiredGB']:
            with o.lock:
                o.state['cacheRecovery'].update(
                    status='not-needed', detail='Memory became available; no cleanup was run.'
                )
                o.save()
            return
        recovery = o.manual_selection.cache_plan(
            current, live, state, budget, time.time(), True, authorization
        )
        if not recovery['canAttempt']:
            raise ValueError(recovery['detail'])
        self.current()
        try:
            clear_file_cache(o.runner)
        except CacheRecoveryError as error:
            with o.lock:
                o.state['cacheRecovery'].update(status='failed', detail=str(error))
                o.save()
            with o.manual_selection.permission_lock:
                o.manual_selection.permission = None
            detail = (
                'Cache cleanup needs authorization on this Mac. Enable cache cleanup in the manual model controls in Optimizer → Overview, then turn On again.'
                if error.code == 'cache-permission'
                else str(error)
            )
            raise ValueError(detail + ' The provider remains stopped.') from None
        self.cleared_at = time.time()
        with o.lock:
            o.state['cacheRecovery'].update(
                status='cleared',
                clearedAt=self.cleared_at,
                detail='Cache cleanup completed. Waiting for newly measured memory.',
            )
            o.save()
        self.progress(
            'Cache cleanup completed. Checking newly measured memory before starting the saved model.'
        )
        for _ in range(10):
            if o.stop.wait(2):
                raise ValueError(
                    'BloomGauge closed while checking memory. This On request will not be replayed.'
                )
            _, live = self.current()
            hardware_at = (live.get('hardware') or {}).get('at')
            if not finite(hardware_at) or hardware_at <= self.cleared_at:
                continue
            _, _, _, budget = self.checked()
            if budget['afterUnloadGB'] < budget['requiredGB']:
                raise ValueError(self.shortfall(budget))
            return
        raise ValueError(
            'Cache cleanup completed, but fresh hardware memory readings did not arrive. Refresh before turning On again. The provider remains stopped.'
        )

    def before_start(self):
        self.progress(
            'Memory checked. Starting the saved model; On will wait for verified readiness.'
        )
        current, _, _, budget = self.checked()
        if budget['afterUnloadGB'] < budget['requiredGB']:
            raise ValueError(
                self.shortfall(budget)
                if self.cleared_at
                else 'Available memory changed before Start. Refresh before turning On again.'
            )
        self.current()
        return current
