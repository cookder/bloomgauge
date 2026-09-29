"""Explicit, bounded user commands for the installed provider, never a watchdog.

Uses the optimizer's command lock, pending marker and update reservation. Request
IDs survive lost responses; version tokens cover both service settings and the
observed process. Endpoint configuration remains Mac-only.

Stop pauses automatic control. Under the manager, Start and endpoint setup do not:
like a manual pick, automatic control continues once the command is done (the
pending marker keeps the manager out while it runs).
"""

import copy
import hashlib
import json
import pathlib
import plistlib
import subprocess
import threading
import time
import uuid
import manager
from model_combinations import (
    members,
    selection_key,
    pair_budget,
    combination_config_error,
    configured_reserve_gb,
)
from model_readiness import session_key
from provider_sessions import matching_process, process_identity
from provider_reporting import state_fresh
from prewarm import local_request, WarmupError

# `darkbloom stop` returns in seconds on older providers. From 0.9.9 it drains first
# (600 s default deadline; a timeout leaves the service draining), like `start`.
STOP_SECONDS = 60
ENDPOINT_SETUP = 'Open Optimizer → Overview on the Mac. In the manual model controls, select a model and use Prepare, Start or Switch to set up pre-warming.'
# After this many setups in a row failed for the same reason, the notice gives that reason and
# the Terminal command instead of asking for the same setup again.
ENDPOINT_FAILURES = 2
ENDPOINT_FIX = '`darkbloom start --local-endpoint --model '
# A drain disables launchd recovery until the start that follows it; with none, Darkbloom runs
# drained (refusing work) with its launch agent disabled.
DRAINED = (
    'Darkbloom finished draining but was not started again, so it serves nothing. Pick a model '
    'in the model controls or turn the manager on to start it again, or run `darkbloom start` '
    'in Terminal.'
)


def fenced_drain(status, disabled, raw):
    """Running, drained and serving nothing, with launchd recovery still disabled (DRAINED)."""
    from optimizer import drained_idle

    return status == 'running' and disabled is True and drained_idle(raw)


def endpoint_issue(options):
    if '--no-auth' in options:
        return 'Local endpoint authentication is disabled. Enable authentication in Darkbloom on the Mac before switching.'
    host = options[options.index('--bind') + 1] if '--bind' in options else '127.0.0.1'
    if host not in ('127.0.0.1', 'localhost', '::1'):
        return 'Pre-warming needs a loopback endpoint. Review Darkbloom’s bind setting on the Mac.'
    if '--local-endpoint' not in options:
        return ENDPOINT_SETUP
    return None


def endpoint_fix(failure):
    """The manual fix once ENDPOINT_FAILURES setups in a row failed for the same reason, else
    None. `failure`: the optimizer's endpointSetupFailure record."""
    failure = failure if isinstance(failure, dict) else {}
    count, model = failure.get('count'), failure.get('model')
    if (
        not isinstance(count, int)
        or count < ENDPOINT_FAILURES
        or not isinstance(model, str)
        or not isinstance(failure.get('cause'), str)
    ):
        return None
    return (
        'Setting up pre-warming failed %s this way. To set it up yourself, run %s%s` in '
        'Terminal, then refresh BloomGauge.'
        % ('twice' if count == 2 else '%d times' % count, ENDPOINT_FIX, model)
    )


def endpoint_setup_notice(detail):
    """A status that asks for the endpoint setup, or gives its manual fix."""
    return isinstance(detail, str) and (detail == ENDPOINT_SETUP or ENDPOINT_FIX in detail)


def multi_model_notice(home, options):
    """provider.toml's [backend] enabled_models lists three or more models. Darkbloom 0.9.10's
    launchd child serves that list, not the launch agent's --model
    (StartCommand.usesPinnedModelSelection), and a failed `start` puts it back
    (ProviderModelSelection.withReplacement). BloomGauge runs one model or a pair, so it says
    so plainly rather than waiting or retrying. A `darkbloom start --model` from the model
    controls replaces the list."""
    count = len(set(manager.toml_models(home, options) or []))
    if count < 3:
        return None
    return (
        'Darkbloom is set to serve %d models. Run `darkbloom start` in Terminal and pick one '
        'model, or use BloomGauge’s model controls.' % count
    )


DROPPED_NOTE_SECONDS = 7 * 86400


def dropped_environment_note(dropped, now):
    """A sentence for the model controls after a start dropped launch-agent variables."""
    if not isinstance(dropped, dict) or not isinstance(dropped.get('keys'), list):
        return ''
    at = dropped.get('at')
    keys = [k for k in dropped['keys'] if isinstance(k, str) and k][:8]
    if not keys or not isinstance(at, (int, float)) or not 0 <= now - at < DROPPED_NOTE_SECONDS:
        return ''
    return (
        ' When Darkbloom last started, it kept only its own launch settings and dropped '
        + ', '.join(keys)
        + '. A start from Terminal does the same; set them again only if Darkbloom supports them.'
    )


class ProviderControl:
    def __init__(self, optimizer):
        self.o = optimizer

    def setup_reason(self, raw):
        o = self.o
        now = time.time()
        if (
            not o.identity_ok
            or not 0 <= now - o.identity_at < 180
            or o.identity_session != (raw.get('started_at'), raw.get('pid'))
            or session_key(raw) != session_key(o.raw)
        ):
            return 'Wait for this Mac’s fresh, verified provider identity.'
        return o.environment_reason(o.live, now, manual=True)

    def inspect(self):
        from optimizer import launch_options

        o = self.o
        plist = o.read_agent()
        model, options = launch_options(plist, allow_auto=True, allow_many=True)
        if pathlib.Path(plist['ProgramArguments'][0]).resolve() != o.binary.resolve():
            raise ValueError(
                'The installed provider executable changed. Review Darkbloom on the Mac.'
            )
        environment = plist.get('EnvironmentVariables', {})
        if not isinstance(environment, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in environment.items()
        ):
            raise ValueError('The provider environment could not be read.')
        try:
            raw = o.read_state()
        except (OSError, ValueError):
            raw = {}
        process = matching_process(process_identity(raw))
        disabled = o.service_disabled()
        # 0.9.10 preloads its startup models before it registers and writes its state only
        # every 30 s meanwhile (state_fresh allows 45 s then). A fixed 15 s read the loading
        # provider as 'unknown', so On from a stopped provider failed right after its Start.
        fresh = state_fresh(raw, time.time())
        status = (
            'running' if process is True and fresh else 'stopped' if process is False else 'unknown'
        )
        control_version = o.control_version()
        with o.lock:
            source_identity = [(o.live or {}).get('account'), (o.live or {}).get('device')]
        version = hashlib.sha256(
            json.dumps(
                [control_version, plist, session_key(raw), process, disabled, source_identity],
                sort_keys=True,
            ).encode()
        ).hexdigest()
        return {
            'version': version,
            'status': status,
            # Three or more --model picks are the launch agent's selection, never the daemon's.
            'model': model
            or (
                None
                if plist['ProgramArguments'].count('--model') >= 3
                else selection_key(raw.get('advertised_models'))
            ),
            'options': options,
            'environment': environment,
            'raw': raw,
            'disabled': disabled,
            'controlVersion': control_version,
        }

    def snapshot(self):
        o = self.o
        try:
            v = self.inspect()
        except (OSError, ValueError, KeyError, TypeError, plistlib.InvalidFileException):
            return {
                'status': 'unavailable',
                'detail': 'Set up and start Darkbloom once on this Mac, then refresh the model controls in Optimizer → Overview. Your existing configuration is kept.',
                'canStart': False,
                'canStop': False,
                'canEnableEndpoint': False,
            }
        with o.lock:
            blocked = bool(
                o.state.get('pending')
                or o.state.get('requestedModel')
                or o.worker
                and o.worker.is_alive()
                or o.warmup_worker
                and o.warmup_worker.is_alive()
                or o.update_guard.active()
            )
        issue = o.endpoint_notice(v['options'])
        endpoint = 'ready'
        if issue:
            endpoint = 'setup'
        else:
            try:
                local_request(o.home, v['raw'], v['options'])
            except WarmupError as error:
                endpoint = 'waiting'
                issue = str(error)
        can_enable = (
            '--local-endpoint' not in v['options']
            and '--no-auth' not in v['options']
            and (
                '--bind' not in v['options']
                or v['options'][v['options'].index('--bind') + 1]
                in ('127.0.0.1', 'localhost', '::1')
            )
        )
        return {
            'status': v['status'],
            'version': v['version'],
            'model': v['model'],
            'operationPending': blocked,
            'configurationIssue': combination_config_error(
                o.home, v['options'], v['environment'], require_pair=False
            ),
            'canStart': not blocked and v['status'] == 'stopped' and bool(v['model']),
            'canStop': not blocked and v['status'] == 'running',
            'canEnableEndpoint': not blocked
            and v['status'] in ('running', 'stopped')
            and bool(v['model'])
            and can_enable,
            'endpoint': endpoint,
            'endpointDetail': issue,
            'detail': 'Controls are temporarily unavailable while a switch, warm-up or update finishes.'
            if blocked
            else DRAINED
            if fenced_drain(v['status'], v['disabled'], v['raw'])
            else 'Running in the background. Closing this window does not stop Darkbloom.'
            + dropped_environment_note(o.state.get('environmentDropped'), time.time())
            if v['status'] == 'running'
            else 'Stopped. Start it in Optimizer → Overview; automatic control stays on.'
            if v['status'] == 'stopped' and manager.active(o.state)
            else 'Stopped. Choose a model and start that selection in Optimizer → Overview; automatic switching stays paused.'
            if v['status'] == 'stopped'
            else 'Waiting for a fresh provider status. Refresh before sending a command.',
            'lastResult': copy.deepcopy(o.state.get('providerResult')),
        }

    def action(self, data, source, automatic_id=None):
        self.admit(data, source, automatic_id)
        return self.o.manual_snapshot(remote=source == 'phone')

    def admit(self, data, source, automatic_id=None):
        o = self.o
        if set(data) != {'action', 'requestId', 'expectedProvider'}:
            raise ValueError('Refresh the model controls before sending this command.')
        kind = data.get('action')
        if kind not in ('provider-start', 'provider-stop', 'provider-endpoint'):
            raise ValueError('Unsupported provider command.')
        try:
            request_id = str(uuid.UUID(data['requestId']))
        except (ValueError, TypeError, AttributeError):
            raise ValueError('A valid request ID is required.') from None
        if kind == 'provider-endpoint' and source != 'mac':
            raise ValueError(
                'Set up pre-warming in Optimizer → Overview on the Mac using the manual model controls. Phone access cannot change endpoint configuration.'
            )
        # Service inspection can call launchctl. Keep it outside the shared
        # optimizer lock; the worker independently rechecks before dispatch.
        v = self.inspect()
        endpoint_ready = (
            self.snapshot()['canEnableEndpoint'] if kind == 'provider-endpoint' else False
        )
        endpoint_reason = (
            self.setup_reason(v['raw'])
            if kind == 'provider-endpoint' and v['status'] == 'running'
            else None
        )
        with o.lock:
            if automatic_id is not None and not o.automatic_control.intent_current(automatic_id):
                raise ValueError(
                    'On was cancelled before the provider command. No command was sent.'
                )
            for old in o.state.get('providerRequests', []):
                if old['id'] == request_id:
                    if old['action'] != kind or old['version'] != data['expectedProvider']:
                        raise ValueError(
                            'This request ID was already used for a different command.'
                        )
                    return None
            if any(old['id'] == request_id for old in o.state.get('selectionRequests', [])):
                raise ValueError('This request ID was already used for a different command.')
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
            ):
                raise ValueError(
                    'The provider changed. Refresh the model controls and review the command again.'
                )
            required = (
                ('stopped',)
                if kind == 'provider-start'
                else ('running', 'stopped')
                if kind == 'provider-endpoint'
                else ('running',)
            )
            if v['status'] not in required:
                raise ValueError('The provider state changed. Refresh the model controls.')
            if kind != 'provider-stop' and not members(v['model']):
                raise ValueError('Choose a model in Darkbloom on the Mac first.')
            if kind == 'provider-endpoint':
                if not endpoint_ready:
                    raise ValueError(
                        'Review endpoint authentication and bind settings in Darkbloom on the Mac.'
                    )
                if v['status'] == 'running' and (
                    endpoint_reason
                    or v['raw'].get('inference_active') is not False
                    or o.idle_since is None
                    or time.time() - o.idle_since < 12
                ):
                    raise ValueError(
                        'Pre-warming setup needs 12 seconds idle. No busy restart was sent. Open Optimizer → Overview on the Mac to review the manual model controls.'
                    )
            # Persist admission before dispatch; no command follows a failed save.
            before = copy.deepcopy(o.state)
            if kind == 'provider-stop' or automatic_id is not None or not manager.active(o.state):
                o.pause_internal(
                    'Manual provider control paused automatic switching.',
                    keep_automatic_intent=automatic_id,
                )
            o.state['providerRequests'] = (
                o.state.get('providerRequests', [])
                + [{'id': request_id, 'action': kind, 'version': v['version']}]
            )[-32:]
            o.state['pending'] = {
                'kind': 'provider',
                'model': v['model'],
                'requestId': request_id,
                'at': time.time(),
            }
            o.state['providerResult'] = {
                'id': request_id,
                'status': 'working',
                'detail': 'Sending the requested provider command.',
            }
            try:
                o.save()
            except Exception:
                o.state = before
                raise
            v['admittedControlVersion'] = o.control_version()
            o.worker = threading.Thread(
                target=self.run, args=(kind, request_id, v, automatic_id), daemon=True
            )
            o.worker.start()
        return None

    def run(self, kind, request_id, expected, automatic_id=None):
        o = self.o
        outcome = 'failed'
        detail = 'Could not verify this command. Check Darkbloom on the Mac before retrying.'
        try:
            with o.command_lock:
                # Compare launch settings and process again after worker admission.
                current = self.inspect()
                if automatic_id is not None and not o.automatic_control.intent_current(
                    automatic_id
                ):
                    raise ValueError(
                        'On was cancelled before the provider command. No command was sent.'
                    )
                if (
                    o.stop.is_set()
                    or current['status'] != expected['status']
                    or current['options'] != expected['options']
                    or current['environment'] != expected['environment']
                    or current['model'] != expected['model']
                    or session_key(current['raw']) != session_key(expected['raw'])
                    or current['disabled'] != expected['disabled']
                ):
                    raise ValueError(
                        'The provider changed before the command. Refresh the model controls; no command was sent.'
                    )
                if kind == 'provider-stop':
                    from optimizer import DRAIN_SECONDS, graceful_drain

                    with o.lock:
                        o.warmup = {}
                    o.runner(
                        [str(o.binary), 'stop'],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=DRAIN_SECONDS + STOP_SECONDS
                        if graceful_drain(current['raw'])
                        else STOP_SECONDS,
                        check=True,
                    )
                else:
                    on_start = None
                    if automatic_id is not None:
                        from optimizer_start import OnStart

                        on_start = OnStart(o, request_id, expected)
                        on_start.prepare()
                        current = on_start.before_start()
                    elif current['status'] == 'stopped':
                        # The explicit Start action is the only stopped-provider exception.
                        # Check saved local models and resources; readiness is verified later
                        # by the normal observer, never inferred from a successful command.
                        o.verify_local_target(current['model'], current['options'])
                        live = copy.deepcopy(o.live) or {}
                        live['provider'] = {
                            **live.get('provider', {}),
                            'online': True,
                            'memoryGB': 0,
                        }
                        reason = o.environment_reason(live, time.time(), manual=True)
                        if reason:
                            raise ValueError(reason)
                        from optimizer import memory_budget, finite

                        models = members(current['model'])
                        weights = [
                            next(
                                (r.get('estimated_memory_gb') for r in o.local if r.get('id') == m),
                                None,
                            )
                            for m in models
                        ]
                        if any(not finite(w) or w <= 0 for w in weights):
                            raise ValueError(
                                'Waiting for memory estimates for every saved model. Refresh models before starting.'
                            )
                        # The user's own Start: provider.toml's memory_reserve_gb is the reserve,
                        # and knobs that hold only BloomGauge's voluntary moves don't refuse it.
                        reserve = configured_reserve_gb(o.home, current['options'])
                        if len(models) == 2:
                            reason = combination_config_error(
                                o.home, current['options'], current['environment'], voluntary=False
                            )
                            if reason:
                                raise ValueError(reason)
                            budget = pair_budget(
                                live.get('hardware', {}),
                                {'memoryGB': 0},
                                models,
                                weights,
                                config_reserve=reserve,
                            )
                        else:
                            budget = memory_budget(
                                live.get('hardware', {}),
                                {'memoryGB': 0},
                                models[0],
                                weights[0],
                                config_reserve=reserve,
                            )
                        if not budget or budget['afterUnloadGB'] < budget['requiredGB']:
                            raise ValueError(
                                'Not enough verified free memory to start the saved selection. Close other workloads and refresh.'
                            )
                    else:
                        if (
                            self.setup_reason(current['raw'])
                            or current['raw'].get('inference_active') is not False
                            or o.idle_since is None
                            or time.time() - o.idle_since < 12
                        ):
                            raise ValueError(
                                'Work resumed before endpoint setup. No restart was sent; retry when idle.'
                            )
                    options = list(current['options'])
                    if kind == 'provider-endpoint':
                        options.append('--local-endpoint')
                    if automatic_id is not None and not o.automatic_control.intent_current(
                        automatic_id
                    ):
                        raise ValueError(
                            'On was cancelled before the provider command. No command was sent.'
                        )
                    with o.lock:
                        o.warmup = {}
                    o.command(current['model'], options, current['environment'])
                    if on_start is not None:
                        o.automatic_control.update_operation(
                            on_start.op,
                            status='waiting',
                            detail='The start command finished. Waiting for verified model readiness before turning On.',
                        )
                        o.automatic_control.wake.set()
                # Successful CLI exit is not proof the requested state was reached.
                for _ in range(30):
                    if o.stop.wait(1):
                        break
                    observed = self.inspect()
                    if kind == 'provider-stop' and observed['status'] == 'stopped':
                        outcome = 'completed'
                        detail = 'Darkbloom stopped. Automatic switching is paused. Choose a model and use Start in Optimizer → Overview to resume.'
                        break
                    if (
                        kind != 'provider-stop'
                        and observed['status'] == 'running'
                        and session_key(observed['raw']) != session_key(expected['raw'])
                    ):
                        if observed['model'] != expected['model']:
                            break
                        if kind == 'provider-endpoint':
                            try:
                                local_request(o.home, observed['raw'], observed['options'])
                            except WarmupError:
                                continue
                        outcome = 'completed'
                        detail = (
                            'Darkbloom started. Waiting for model readiness; automatic control continues.'
                            if manager.active(o.state)
                            else 'Darkbloom started. Waiting for model readiness; automatic switching remains paused.'
                        )
                        break
        except ValueError as error:
            detail = str(error)
        except Exception:
            pass  # Never expose CLI output, credentials or local paths.
        finally:
            with o.lock:
                o.state['providerResult'] = {
                    'id': request_id,
                    'status': outcome,
                    'detail': detail,
                    'at': time.time(),
                }
                if (o.state.get('pending') or {}).get('requestId') == request_id:
                    o.state.pop('pending', None)
                o.status = 'optimizing' if manager.active(o.state) else 'observing'
                o.detail = detail
                o.next_identity = 0
                o.next_discovery = 0
                o.previous = None
                o.idle_since = None
                o.save()
