"""One explicit, target-bound manual selection; never a background restart rule."""

import copy
import re
import threading
import time
import uuid
from model_combinations import configured_reserve_gb, members, same_selection
from model_readiness import session_key
from optimizer_store import device_id
from provider_control import endpoint_issue
from cache_permission import cache_permission
from cache_recovery import clear_file_cache, CacheRecoveryError


class ManualSelection:
    def __init__(self, optimizer):
        self.o = optimizer
        self.permission = None
        self.permission_lock = threading.Lock()

    def permission_status(self, force=False):
        with self.permission_lock:
            now = time.time()
            if force or not self.permission or not 0 <= now - self.permission['checkedAt'] < 10:
                self.permission = cache_permission(self.o.runner)
            return copy.deepcopy(self.permission)

    def cache_plan(self, v, live, state, budget, now, permitted, authorization):
        from optimizer import finite

        needed = bool(
            v.get('status') == 'stopped'
            and budget
            and budget['afterUnloadGB'] < budget['requiredGB']
        )
        result = {
            'needed': needed,
            'canAttempt': False,
            'detail': '',
            'retryAt': None,
            'authorization': authorization['status'],
        }
        if not needed:
            return result
        result['detail'] = (
            'Not enough verified available memory. Close other workloads and refresh.'
        )
        h = live.get('hardware') or {}
        cached = h.get('cachedFilesGB')
        if (
            not permitted
            or not finite(h.get('at'))
            or not -5 < now - h['at'] < 15
            or not finite(cached)
            or cached < max(2, budget['requiredGB'] - budget['afterUnloadGB'])
        ):
            return result
        last = state.get('cacheRecovery') or {}
        last_at = last.get('at')
        if finite(last_at) and now - last_at < 600:
            result.update(
                retryAt=last_at + 600,
                detail='Cache cleanup is cooling down. Close other workloads, or retry after the displayed time.',
            )
            return result
        if authorization['status'] != 'ready':
            result['detail'] = authorization['detail']
            return result
        result.update(
            canAttempt=True,
            detail='Bloomkeeper will clear reclaimable file cache once, recheck memory, then start only if enough is available.',
        )
        return result

    def endpoint_setup_allowed(self, options):
        o = self.o
        with o.lock:
            request = o.state.get('selectionRequest') or {}
            return bool(
                request.get('kind') == 'switch'
                and request.get('source') == 'mac'
                and request.get('setupEndpoint') is True
                and request.get('id') == o.state.get('requestId')
                and request.get('model') == o.state.get('requestedModel')
                and o.state.get('requestedKind') == 'manual'
                and '--local-endpoint' not in options
                and endpoint_issue(options + ['--local-endpoint']) is None
            )

    def common_reason(self, v, source, live, raw, now):
        from optimizer import finite

        o = self.o
        if v.get('status') not in ('running', 'stopped'):
            return 'Waiting for a verified running or stopped provider state. Refresh before sending a command.'
        if v.get('disabled') not in (True, False):
            return 'The provider launch state is unknown. Refresh before sending a command.'
        if (
            not live.get('account')
            or not live.get('device')
            or live['device'] != device_id(raw)
            or device_id(v['raw']) != live['device']
            or session_key(v['raw']) != session_key(raw)
        ):
            return 'Waiting to verify this Mac and the connected account.'
        if o.discovery_error or not 0 <= now - o.discovery_at < 600:
            return 'Refresh the local model list and network catalog before selecting a model.'
        h = live.get('hardware') or {}
        if not finite(h.get('at')) or not -5 < now - h['at'] < 15:
            return 'Waiting for fresh hardware readings.'
        options = v['options']
        issue = endpoint_issue(options)
        if issue and endpoint_issue(options + ['--local-endpoint']) is not None:
            return issue
        if '--local-endpoint' not in options and source != 'mac':
            return 'Open Optimizer → Overview on the Mac. Use Prepare, Start or Switch in the manual model controls to set up pre-warming before controlling models from your phone.'
        if v['status'] == 'running':
            if (
                v['disabled'] is not False
                or not live.get('provider', {}).get('online')
                or not finite(raw.get('written_at'))
                or not -5 < now - raw['written_at'] < 10
                or not same_selection(raw, v['model'])
            ):
                return 'Waiting for fresh readings for the current provider session.'
            if (
                not o.identity_ok
                or not 0 <= now - o.identity_at < 180
                or o.identity_session != (raw.get('started_at'), raw.get('pid'))
            ):
                return 'Waiting for this Mac’s fresh, verified provider identity.'
        check = copy.deepcopy(live)
        if v['status'] == 'stopped':
            # Explicit Start selected permits an offline provider, never stale
            # host readings or an unknown/possibly-running process.
            check['provider'] = {**check.get('provider', {}), 'online': True, 'memoryGB': 0}
        # Switching a running provider: battery power and the 95 °C line don't hold an explicit
        # pick (thermal state does). Starting a stopped one keeps them: it may clear the file
        # cache first, and On uses this check too.
        move = 'manual' if v['status'] == 'running' else None
        return o.environment_reason(check, now, manual=True, move=move)

    def rows(self, v, source, live, raw, state, now, blocked=False):
        from optimizer import finite, memory_budget

        o = self.o
        candidates = o.candidates({}, {}, live, state, manual=True)
        common = self.common_reason(v, source, live, raw, now)
        if blocked:
            common = 'Wait for the current operation to finish, or cancel the queued switch first.'
        local = {x.get('id'): x for x in o.local}
        catalog = {x.get('id'): x for x in o.catalog}
        result = []
        authorization = (
            self.permission_status()
            if v.get('status') == 'stopped'
            else {'status': 'unknown', 'detail': ''}
        )
        reserve = configured_reserve_gb(o.home, v.get('options') or [])
        for row in candidates:
            row = copy.deepcopy(row)
            model = row['id']
            disk = local.get(model) or {}
            spec = catalog.get(model) or {}
            runtime = bool(row.get('requiresRuntimeVerification'))
            reason = row.get('reason')
            if v.get('status') == 'stopped':
                # A stopped provider has no current roster/warm proof. Known
                # hardware/runtime may be explicitly attempted, then verified
                # through the same post-start identity/eligibility/decode path.
                caps = spec.get('required_provider_capabilities')
                caps_ok = isinstance(caps, list) and all(isinstance(c, str) and c for c in caps)
                known = bool(
                    caps_ok
                    and caps
                    and set(caps) <= {'apple_m5', 'mlx_nax'}
                    and re.fullmatch(
                        r'Apple M5(?: Pro| Max| Ultra)?',
                        (live.get('hardware') or {}).get('chip', ''),
                    )
                )
                if caps:
                    runtime = known
                    if (
                        known
                        and disk
                        and spec.get('active') is True
                        and finite(spec.get('min_ram_gb'))
                        and spec['min_ram_gb'] <= live.get('hardware', {}).get('memoryTotalGB', 0)
                        and finite(disk.get('estimated_memory_gb'))
                        and disk['estimated_memory_gb'] + 6
                        <= live.get('hardware', {}).get('memoryTotalGB', 0)
                    ):
                        reason = None
                    elif not known:
                        reason = 'The selected model’s runtime is not supported by verified local hardware.'
            if (
                not finite(disk.get('size_bytes'))
                or disk['size_bytes'] <= 0
                or disk.get('template_render_ok') is not True
            ):
                reason = 'Refresh models to verify downloaded files and the model template.'
            provider = {'memoryGB': 0} if v.get('status') == 'stopped' else live.get('provider', {})
            cache = (
                0
                if v.get('status') == 'stopped'
                else raw.get('capacity', {}).get('gpu_memory_cache_gb', 0)
            )
            budget = memory_budget(
                live.get('hardware', {}),
                provider,
                model,
                row.get('memoryGB'),
                cache,
                config_reserve=reserve,
            )
            recovery = self.cache_plan(
                v, live, state, budget, now, common is None and reason is None, authorization
            )
            if not budget or budget['afterUnloadGB'] < budget['requiredGB']:
                if not recovery['canAttempt']:
                    reason = (
                        reason
                        or recovery['detail']
                        or 'Not enough verified available memory for the selected model. Close other workloads and refresh.'
                    )
            start_reason = common or reason
            switch_reason = common or reason
            row.update(
                canStart=v.get('status') == 'stopped' and start_reason is None,
                startReason=start_reason
                if v.get('status') == 'stopped'
                else 'Start is available when the provider is stopped.',
                canSwitch=v.get('status') == 'running' and switch_reason is None,
                switchReason=switch_reason
                if v.get('status') == 'running'
                else 'Switch is available when the provider is running.',
                requiresRuntimeVerification=runtime,
                loadBudget=budget,
                cacheRecovery=recovery,
            )
            result.append(row)
        return result

    def decorate(self, value, remote=False):
        o = self.o
        value['selectionResult'] = None
        value['providerControl']['cacheRecoveryAuthorization'] = self.permission_status()
        value['providerControl']['selectionActionSupported'] = True
        value['providerControl']['endpointSetupRequired'] = (
            value['providerControl'].get('endpoint') == 'setup'
        )
        try:
            v = o.provider_control.inspect()
            with o.lock:
                live = copy.deepcopy(o.live) or {}
                raw = copy.deepcopy(o.raw)
                state = copy.deepcopy(o.state)
                blocked = bool(
                    state.get('pending')
                    or state.get('requestedModel')
                    or o.worker
                    and o.worker.is_alive()
                    or o.warmup_worker
                    and o.warmup_worker.is_alive()
                    or o.command_lock.locked()
                    or o.stop.is_set()
                    or o.update_guard.active()
                )
            # Use the same inspected status/token for the response and eligibility.
            value['providerControl'].update(
                status=v['status'],
                version=v['version'],
                model=v['model'],
                endpointSetupRequired='--local-endpoint' not in v['options'],
            )
            value['session'] = session_key(v['raw'])
            rows = self.rows(
                v, 'phone' if remote else 'mac', live, raw, state, time.time(), blocked
            )
            fields = (
                'id',
                'name',
                'available',
                'reason',
                'memoryGB',
                'loadBudget',
                'requiresRuntimeVerification',
                'runtimeProof',
                'canStart',
                'startReason',
                'canSwitch',
                'switchReason',
                'cacheRecovery',
            )
            value['models'] = [{k: row.get(k) for k in fields} for row in rows]
            request = state.get('selectionRequest') or {}
            receipt = state.get('manualResult') or {}
            if request.get('id') and receipt.get('id') == request['id']:
                status = receipt.get('status')
                if status in ('recovered', 'interrupted'):
                    status = 'failed'
                if (
                    status == 'queued'
                    and (state.get('pending') or {}).get('requestId') == request['id']
                ):
                    status = 'working'
                current = (
                    v['status'] == 'running'
                    and same_selection(v['raw'], request['model'])
                    and receipt.get('session') == session_key(v['raw'])
                )
                if (
                    status in ('queued', 'working', 'failed', 'cancelled')
                    or status in ('completed', 'unchanged')
                    and current
                ):
                    value['selectionResult'] = {
                        k: receipt.get(k) for k in ('id', 'model', 'detail', 'at')
                    }
                    value['selectionResult'].update(status=status, kind=request['kind'])
            if v['status'] != 'running':
                value['currentModel'] = None
                value['warmup'] = {
                    'status': 'waiting',
                    'model': None,
                    'detail': 'Provider is stopped.'
                    if v['status'] == 'stopped'
                    else 'Waiting for verified provider status.',
                }
                if (value.get('lastResult') or {}).get('status') in ('completed', 'unchanged'):
                    value['lastResult'] = None
                value['queue'].update(activity='unknown', fresh=False, idleSeconds=0)
        except (OSError, ValueError, KeyError, TypeError):
            for row in value['models']:
                row.update(
                    canStart=False,
                    canSwitch=False,
                    startReason='Provider status could not be verified. Refresh.',
                    switchReason='Provider status could not be verified. Refresh.',
                    requiresRuntimeVerification=bool(row.get('requiresRuntimeVerification')),
                )
        return value

    def replay(self, data, source):
        o = self.o
        for old in o.state.get('selectionRequests', []):
            if old['id'] == data['requestId']:
                if old['body'] != data or old['source'] != source:
                    raise ValueError('This request ID was already used for a different command.')
                return True
        for group in ('manualRequests', 'providerRequests'):
            if any(x['id'] == data['requestId'] for x in o.state.get(group, [])):
                raise ValueError('This request ID was already used for a different command.')
        return False

    def action(self, data, source):
        self.admit(data, source)
        value = self.o.manual_snapshot(remote=source == 'phone')
        request_id = str(uuid.UUID(data['requestId']))
        if (value.get('selectionResult') or {}).get('id') != request_id:
            # A request-specific reply must settle a lost response even when a
            # later choice/stop made its completion unsuitable as current UI.
            # This receipt never asserts current readiness or replays a command.
            with self.o.lock:
                old = next(x for x in self.o.state['selectionRequests'] if x['id'] == request_id)
                latest = copy.deepcopy(self.o.state.get('manualResult')) or {}
                active = (
                    (self.o.state.get('pending') or {}).get('requestId') == request_id
                    or self.o.state.get('requestId') == request_id
                    and self.o.state.get('requestedModel')
                )
            status = 'working' if active else 'cancelled'
            detail = (
                (latest.get('detail') or 'The accepted selection is still being checked.')
                if active
                else 'This earlier request was accepted, but the provider or a newer selection changed. No command was repeated.'
            )
            value['selectionResult'] = {
                'id': request_id,
                'model': old['body']['model'],
                'kind': old['kind'],
                'status': status,
                'detail': detail,
                'at': time.time(),
            }
        return value

    def admit(self, data, source):
        o = self.o
        if (
            set(data)
            != {
                'action',
                'requestId',
                'model',
                'expectedProvider',
                'expectedSession',
                'verifyRuntime',
            }
            or data.get('action') != 'select'
        ):
            raise ValueError(
                'Refresh the model controls and review the selected model before sending this command.'
            )
        if source not in ('mac', 'phone') or not isinstance(data['verifyRuntime'], bool):
            raise ValueError('Explicit runtime verification and a trusted source are required.')
        try:
            request_id = str(uuid.UUID(data['requestId']))
        except (ValueError, TypeError, AttributeError):
            raise ValueError('A valid request ID is required.') from None
        data = {**data, 'requestId': request_id}
        if (
            not isinstance(data['model'], str)
            or len(members(data['model'])) != 1
            or len(data['model']) > 200
        ):
            raise ValueError('Select one supported model from the current list.')
        if not all(
            isinstance(data[k], str) and 0 < len(data[k]) <= 128
            for k in ('expectedProvider', 'expectedSession')
        ):
            raise ValueError('Refresh the provider state before selecting a model.')
        with o.lock:
            repeated = self.replay(data, source)
        if repeated:
            return None
        v = o.provider_control.inspect()
        with o.lock:
            live = copy.deepcopy(o.live) or {}
            raw = copy.deepcopy(o.raw)
            state = copy.deepcopy(o.state)
        rows = self.rows(v, source, live, raw, state, time.time())
        row = next((x for x in rows if x['id'] == data['model']), None)
        with o.lock:
            if self.replay(data, source):
                return None
            o.update_guard.require_available()
            if (
                o.state.get('pending')
                or o.state.get('requestedModel')
                or o.worker
                and o.worker.is_alive()
                or o.warmup_worker
                and o.warmup_worker.is_alive()
                or o.command_lock.locked()
                or o.stop.is_set()
            ):
                raise ValueError(
                    'Wait for the current operation to finish, or cancel the queued switch first.'
                )
            if (
                v['version'] != data['expectedProvider']
                or v['controlVersion'] != o.control_version()
                or data['expectedSession'] != session_key(v['raw'])
                or data['expectedSession'] != session_key(o.raw)
                or live.get('account') != (o.live or {}).get('account')
                or live.get('device') != (o.live or {}).get('device')
            ):
                raise ValueError(
                    'The provider or account changed. Refresh and review the selected model again.'
                )
            kind = 'start' if v['status'] == 'stopped' else 'switch'
            if not row or not row['canStart' if kind == 'start' else 'canSwitch']:
                raise ValueError(
                    (row or {}).get('startReason' if kind == 'start' else 'switchReason')
                    or 'This model cannot be selected from the current provider state.'
                )
            if row['requiresRuntimeVerification'] and not data['verifyRuntime']:
                raise ValueError(
                    'Confirm runtime verification for this selected model before starting or switching.'
                )
            setup = '--local-endpoint' not in v['options']
            before = copy.deepcopy(o.state)
            now = time.time()
            model = data['model']
            request = {
                'id': request_id,
                'model': model,
                'kind': kind,
                'source': source,
                'at': now,
                'setupEndpoint': setup,
            }
            o.state['selectionRequests'] = (
                o.state.get('selectionRequests', [])
                + [{'id': request_id, 'body': data, 'source': source, 'kind': kind}]
            )[-32:]
            o.state['selectionRequest'] = copy.deepcopy(request)
            unchanged = kind == 'switch' and model == v['model'] and not setup
            # Under the manager a manual pick becomes the pin and automatic control resumes after it.
            manager = getattr(o, 'manager', None)
            resume = bool(manager and manager.manual_queued(request_id, model, unchanged))
            o.state.update(
                mode='demand' if resume and unchanged else 'observe',
                account=live['account'],
                device=live['device'],
                requestedModel=None,
                requestedKind=None,
                requestId=None,
                requestedAt=None,
                requestedSession=None,
                requestedLaunchSignature=None,
            )
            o.state.pop('demandProposal', None)
            o.state.pop('rollbackModel', None)
            o.cancel_combo('Manual selection paused automatic switching.')
            o.state['manualResult'] = {
                'id': request_id,
                'model': model,
                'at': now,
                'status': 'unchanged' if unchanged else 'working' if kind == 'start' else 'queued',
                'detail': 'This model is already selected. It is now your pick; automatic control continues.'
                if unchanged and resume
                else 'This model is already selected. Automatic switching is paused.'
                if unchanged
                else 'Preparing the selected model; cache cleanup and memory recheck are required before start.'
                if kind == 'start' and row['cacheRecovery']['canAttempt']
                else 'Starting the selected model and verifying readiness.'
                if kind == 'start'
                else 'Manual switch queued. Automatic switching is paused.',
                'session': session_key(v['raw']) if unchanged else None,
            }
            if kind == 'start':
                o.state['pending'] = {
                    'kind': 'manual-start',
                    'model': model,
                    'requestId': request_id,
                    'at': now,
                }
            elif not unchanged:
                from optimizer import launch_signature

                o.state.update(
                    requestedModel=model,
                    requestedKind='manual',
                    requestId=request_id,
                    requestedAt=now,
                    requestedVerifyRuntime=data['verifyRuntime'],
                    expectedModel=v['model'],
                    requestedSession=session_key(v['raw']),
                    requestedLaunchSignature=launch_signature(v['options'], v['environment']),
                )
            try:
                o.save()
                o.automatic_control.cancel('Manual selection cancelled automatic switching.')
            except Exception:
                o.state = before
                try:
                    o.save()
                except Exception:
                    pass
                raise
            o.proposal = None
            o.next_switch = None
            o.status = 'observing' if unchanged else 'starting' if kind == 'start' else 'waiting'
            o.detail = o.state['manualResult']['detail']
            if kind == 'start':
                o.warmup = {}
                o.worker = threading.Thread(
                    target=self.run_start,
                    args=(
                        copy.deepcopy(request),
                        copy.deepcopy(data),
                        copy.deepcopy(v),
                        live['account'],
                        live['device'],
                    ),
                    daemon=True,
                )
                o.worker.start()
        return None

    def same_stopped_context(self, current, expected, account, device, request):
        o = self.o
        with o.lock:
            return bool(
                not o.stop.is_set()
                and current['status'] == expected['status'] == 'stopped'
                and all(
                    current[k] == expected[k]
                    for k in ('options', 'environment', 'model', 'disabled')
                )
                and session_key(current['raw'])
                == session_key(expected['raw'])
                == session_key(o.raw)
                and device_id(current['raw']) == device == (o.live or {}).get('device')
                and (o.live or {}).get('account') == account
                and o.state.get('selectionRequest') == request
                and (o.state.get('pending') or {}).get('requestId') == request['id']
                and (o.state.get('pending') or {}).get('model') == request['model']
                and (o.state.get('pending') or {}).get('kind') == 'manual-start'
                and o.state['mode'] == 'observe'
            )

    def start_row(self, current, request):
        o = self.o
        with o.lock:
            live = copy.deepcopy(o.live) or {}
            raw = copy.deepcopy(o.raw)
            state = copy.deepcopy(o.state)
        return next(
            (
                x
                for x in self.rows(current, request['source'], live, raw, state, time.time())
                if x['id'] == request['model']
            ),
            None,
        )

    def start_progress(self, request, detail):
        o = self.o
        with o.lock:
            if (
                o.state.get('selectionRequest') != request
                or (o.state.get('pending') or {}).get('requestId') != request['id']
            ):
                raise ValueError('The selected request changed. No further command was sent.')
            o.state['manualResult'] = {
                'id': request['id'],
                'model': request['model'],
                'kind': 'start',
                'status': 'working',
                'detail': detail,
                'at': time.time(),
                'session': None,
            }
            o.detail = detail
            o.save()

    @staticmethod
    def memory_shortfall(row):
        budget = (row or {}).get('loadBudget')
        if not budget:
            return 'Fresh memory readings are unavailable. Refresh before trying again.'
        available = budget['afterUnloadGB']
        required = budget['requiredGB']
        return f'After cache cleanup: {available:.1f} GB available; {required:.1f} GB needed ({max(0, required - available):.1f} GB more needed). Close other workloads and refresh. The provider remains stopped.'

    def recover_stopped_cache(self, request, expected, account, device, verify_runtime):
        o = self.o
        # A permission listing is read-only. Its result is not authority to skip
        # the real command's denial or any fresh stopped-context/resource check.
        self.permission_status(force=True)
        current = o.provider_control.inspect()
        if not self.same_stopped_context(current, expected, account, device, request):
            raise ValueError(
                'The provider, account or selected request changed before cache cleanup. No cleanup was sent.'
            )
        row = self.start_row(current, request)
        if row and row['canStart'] and not row['cacheRecovery']['needed']:
            return
        if row and row['requiresRuntimeVerification'] and not verify_runtime:
            raise ValueError(
                'Runtime requirements changed before cleanup. Review the selected model again.'
            )
        if not row or not row['cacheRecovery']['canAttempt']:
            raise ValueError(
                (row or {}).get('startReason')
                or 'Cache cleanup is no longer available. No cleanup was sent.'
            )
        # Cache cleanup ownership is durable before dispatch. The shared field
        # enforces the existing running/stopped ten-minute cooldown.
        now = time.time()
        with o.lock:
            last = o.state.get('cacheRecovery') or {}
            if last.get('requestId') == request['id']:
                raise ValueError(
                    'Cache cleanup was already attempted for this request. Refresh its result.'
                )
            if isinstance(last.get('at'), (int, float)) and now - last['at'] < 600:
                raise ValueError('Cache cleanup is cooling down. Try again after ten minutes.')
            o.state['cacheRecovery'] = {
                'requestId': request['id'],
                'kind': 'manual-stopped-start',
                'at': now,
                'status': 'running',
                'model': request['model'],
                'detail': 'Clearing file cache once before the selected start.',
            }
            o.save()
        self.start_progress(
            request, 'Clearing reclaimable file cache once before starting the selected model.'
        )
        # Revalidate after persistence as well as after slow permission checking.
        current = o.provider_control.inspect()
        if not self.same_stopped_context(current, expected, account, device, request):
            raise ValueError(
                'The provider or selected request changed before cache cleanup. No cleanup was sent.'
            )
        with o.lock:
            live = copy.deepcopy(o.live) or {}
            raw = copy.deepcopy(o.raw)
            state = copy.deepcopy(o.state)
        # Ignore only this request's just-persisted cooldown marker while
        # rechecking all current target, host and cache evidence at dispatch.
        state['cacheRecovery'] = {}
        final_row = next(
            (
                x
                for x in self.rows(current, request['source'], live, raw, state, time.time())
                if x['id'] == request['model']
            ),
            None,
        )
        if (
            not final_row
            or not final_row['canStart']
            or final_row['requiresRuntimeVerification']
            and not verify_runtime
        ):
            raise ValueError(
                (final_row or {}).get('startReason')
                or 'Resources changed before cache cleanup. No cleanup was sent.'
            )
        if not final_row['cacheRecovery']['needed']:
            with o.lock:
                o.state['cacheRecovery'].update(
                    status='not-needed',
                    detail='Memory became available before cleanup; no cleanup was run.',
                )
                o.save()
            return
        if not final_row['cacheRecovery']['canAttempt']:
            raise ValueError(
                'Current file-cache evidence no longer supports cleanup. No cleanup was sent.'
            )
        try:
            clear_file_cache(o.runner)
        except CacheRecoveryError as error:
            with o.lock:
                o.state['cacheRecovery'].update(status='failed', detail=str(error))
                o.save()
            self.permission = None
            detail = (
                'Cache cleanup needs authorization on this Mac. Enable cache cleanup, then review the selected model again.'
                if error.code == 'cache-permission'
                else str(error)
            )
            raise ValueError(detail + ' The provider remains stopped.') from None
        cleared_at = time.time()
        with o.lock:
            o.state['cacheRecovery'].update(
                status='cleared',
                clearedAt=cleared_at,
                detail='Cache cleanup completed. Waiting for newly measured memory.',
            )
            o.save()
        self.start_progress(
            request,
            'Cache cleanup completed. Waiting for fresh memory readings before starting the selected model.',
        )
        for _ in range(10):
            if o.stop.wait(2):
                raise ValueError(
                    'Bloomkeeper closed while waiting for memory. The selected start will not be replayed.'
                )
            current = o.provider_control.inspect()
            if not self.same_stopped_context(current, expected, account, device, request):
                raise ValueError(
                    'The provider, account or selected request changed during cache cleanup. No start was sent.'
                )
            with o.lock:
                h = copy.deepcopy((o.live or {}).get('hardware') or {})
            from optimizer import finite

            if not finite(h.get('at')) or h['at'] <= cleared_at:
                continue
            row = self.start_row(current, request)
            if not row or not row['canStart']:
                raise ValueError(
                    self.memory_shortfall(row)
                    if row and row['cacheRecovery']['needed']
                    else (row or {}).get('startReason')
                    or 'The selected model is no longer available.'
                )
            if row['cacheRecovery']['needed']:
                raise ValueError(self.memory_shortfall(row))
            return
        raise ValueError(
            'Cache cleanup completed, but fresh hardware memory readings did not arrive. Refresh before trying again. The provider remains stopped.'
        )

    def run_start(self, request, data, expected, account, device):
        from optimizer import launch_signature, ExternalChange, DemandDeferred

        o = self.o
        status = 'failed'
        detail = 'The selected start could not be verified. Refresh its status before trying again.'
        result_session = None
        try:
            with o.command_lock:
                current = o.provider_control.inspect()
                if not self.same_stopped_context(current, expected, account, device, request):
                    raise ValueError(
                        'The provider, account or request changed before start. No command was sent.'
                    )
                o.verify_local_target(request['model'], current['options'])
                # Slow catalog/file verification must not authorize a later
                # changed process, source, request, power or memory state.
                current = o.provider_control.inspect()
                if not self.same_stopped_context(current, expected, account, device, request):
                    raise ValueError(
                        'The provider, account or request changed during preflight. No command was sent.'
                    )
                with o.lock:
                    live = copy.deepcopy(o.live) or {}
                    raw = copy.deepcopy(o.raw)
                    state = copy.deepcopy(o.state)
                row = next(
                    (
                        x
                        for x in self.rows(
                            current, request['source'], live, raw, state, time.time()
                        )
                        if x['id'] == request['model']
                    ),
                    None,
                )
                if not row or not row['canStart']:
                    raise ValueError(
                        (row or {}).get('startReason')
                        or 'The selected model is no longer available. No command was sent.'
                    )
                if row['requiresRuntimeVerification'] and not data['verifyRuntime']:
                    raise ValueError(
                        'The selected runtime requirements changed. Review the selection again.'
                    )
                if row['cacheRecovery']['needed']:
                    self.recover_stopped_cache(
                        request, expected, account, device, data['verifyRuntime']
                    )
                options = list(current['options'])
                if request['setupEndpoint']:
                    options.append('--local-endpoint')
                # Recheck after resource inspection (which may call pmset).
                final = o.provider_control.inspect()
                if not self.same_stopped_context(final, expected, account, device, request):
                    raise ValueError('The provider changed before dispatch. No command was sent.')
                with o.lock:
                    live = copy.deepcopy(o.live) or {}
                    raw = copy.deepcopy(o.raw)
                    state = copy.deepcopy(o.state)
                final_row = next(
                    (
                        x
                        for x in self.rows(final, request['source'], live, raw, state, time.time())
                        if x['id'] == request['model']
                    ),
                    None,
                )
                if (
                    not final_row
                    or not final_row['canStart']
                    or final_row['cacheRecovery']['needed']
                    or final_row['requiresRuntimeVerification']
                    and not data['verifyRuntime']
                ):
                    raise ValueError(
                        (final_row or {}).get('startReason')
                        or 'The selected model or resources changed before dispatch.'
                    )
                self.start_progress(
                    request, 'Memory checked. Starting the selected model and verifying readiness.'
                )
                with o.lock:
                    o.warmup = {}
                    o.next_identity = 0
                    o.previous = None
                    o.idle_since = None
                o.command(request['model'], options, current['environment'])
                if not o.verify_started(
                    request['model'],
                    expected['raw'].get('started_at'),
                    360,
                    launch_signature(options, current['environment']),
                ):
                    raise ValueError(
                        'The selected model started but readiness could not be verified. Automatic switching remains paused; no other model was started.'
                    )
                status = 'completed'
                result_session = getattr(o, 'verification_session', None) or o.warmup.get('session')
                detail = 'The selected model is warm and ready. Automatic switching remains paused.'
        except DemandDeferred:
            detail = 'The selected model’s downloaded files or current catalog could not be verified. Refresh the model list and retry.'
        except (ValueError, ExternalChange) as error:
            detail = str(error)
        except Exception:
            pass  # CLI output, paths and credentials never enter responses.
        finally:
            with o.lock:
                o.state['manualResult'] = {
                    'id': request['id'],
                    'model': request['model'],
                    'kind': 'start',
                    'status': status,
                    'detail': detail,
                    'at': time.time(),
                    'session': result_session,
                }
                if (o.state.get('pending') or {}).get('requestId') == request['id']:
                    o.state.pop('pending', None)
                o.state['mode'] = 'observe'
                if status == 'completed':
                    o.state['expectedModel'] = request['model']
                    o.state['lastSwitchAt'] = time.time()
                manager = getattr(o, 'manager', None)
                if manager:
                    manager.manual_finished(request['id'], request['model'], status)
                o.status = 'optimizing' if o.state['mode'] == 'demand' else 'observing'
                o.detail = detail
                o.next_identity = 0
                o.next_discovery = 0
                o.previous = None
                o.idle_since = None
                o.save()
