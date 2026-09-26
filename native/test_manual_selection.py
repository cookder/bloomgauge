"""Target-aware manual operations use only synthetic providers and in-memory history."""

import copy
import json
import time
import unittest
import uuid
from unittest.mock import Mock, patch
from optimizer import Optimizer, launch_signature, session_key
import test_provider_control


class SelectionTests(unittest.TestCase):
    tearDown = test_provider_control.ProviderTests.tearDown
    write_plist = test_provider_control.ProviderTests.write_plist
    provider_calls = test_provider_control.ProviderTests.provider_calls

    def setUp(self):
        test_provider_control.ProviderTests.setUp(self)
        self.o.local.append(
            {'id': 'b', 'estimated_memory_gb': 12, 'size_bytes': 1000, 'template_render_ok': True}
        )
        self.o.catalog.append({'id': 'b', 'active': True, 'min_ram_gb': 16})
        self.o.live['hardware']['chip'] = 'Apple M5 Pro'
        self.o.device_identity_ok = self.o.identity_hardware = True
        self.o.eligible_models = ['a']
        self.o.tick_prewarm = Mock(return_value=False)
        self.o.record_decision = Mock()
        self.o.verify_started = Mock(side_effect=self.verified)
        self.raw['warm_models'] = ['a']
        self.raw['current_model'] = 'a'
        self.o.raw = copy.deepcopy(self.raw)
        self.o.warmup = {
            'status': 'ready',
            'model': 'a',
            'session': session_key(self.raw),
            'verifiedAt': self.now - 30,
        }
        self.calls = []

    def run_cli(self, args, **kw):
        result = test_provider_control.ProviderTests.run_cli(self, args, **kw)
        if args[1] == 'start':
            target = args[args.index('--model') + 1]
            self.raw.update(advertised_models=[target], current_model=target, warm_models=[])
        return result

    def verified(self, target, *args):
        self.raw.update(warm_models=[target], current_model=target, written_at=time.time())
        self.o.raw = copy.deepcopy(self.raw)
        self.o.live['provider'].update(online=True, model=target)
        self.o.verification_session = session_key(self.raw)
        self.o.warmup = {
            'status': 'ready',
            'model': target,
            'session': session_key(self.raw),
            'verifiedAt': time.time(),
        }
        return True

    def stopped(self, endpoint=True):
        self.process.return_value = False
        self.o.service_disabled.return_value = True
        self.o.live['provider']['online'] = False
        self.o.identity_ok = False
        self.o.idle_since = None
        self.raw['written_at'] = self.now - 600
        self.o.raw = copy.deepcopy(self.raw)
        if not endpoint:
            self.args.remove('--local-endpoint')
            self.write_plist()

    def payload(self, model='b', verify=False):
        v = self.p.inspect()
        return {
            'action': 'select',
            'model': model,
            'requestId': str(uuid.uuid4()),
            'expectedProvider': v['version'],
            'expectedSession': session_key(v['raw']),
            'verifyRuntime': verify,
        }

    def admit(self, data=None, source='mac'):
        data = data or self.payload()
        captured = self.calls

        class Deferred:
            def __init__(self, **kw):
                captured.append(kw)

            def start(self):
                pass

            def is_alive(self):
                return False

            def join(self, *args):
                pass

        with patch('manual_selection.threading.Thread', Deferred):
            result = self.o.manual_action(data, source)
        return result

    def run_worker(self):
        self.calls[-1]['target'](*self.calls[-1]['args'])
        return self.o.manual_snapshot()

    def row(self, model='b', remote=False):
        return next(x for x in self.o.manual_snapshot(remote)['models'] if x['id'] == model)

    def arm_switch(self):
        self.o.state['pending'] = {
            'kind': 'manual',
            'model': 'b',
            'previous': 'a',
            'requestId': self.o.state['requestId'],
            'requestedAt': self.o.state['requestedAt'],
            'session': session_key(self.raw),
            'selectionAction': True,
            'verifyRuntime': self.o.state['requestedVerifyRuntime'],
            'launchSignature': launch_signature(self.p.inspect()['options'], {}),
        }
        # The provider fixture deliberately retains an environment variable.
        self.o.state['pending']['launchSignature'] = launch_signature(
            self.p.inspect()['options'], self.p.inspect()['environment']
        )

    def test_stopped_stale_provider_can_start_selected_b_directly(self):
        self.stopped()
        self.o.state['mode'] = 'demand'
        self.assertTrue(self.row()['canStart'])
        self.assertFalse(self.row()['canSwitch'])
        self.admit()
        self.assertEqual(self.o.state['mode'], 'observe')
        result = self.run_worker()
        calls = self.provider_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][calls[0].index('--model') + 1], 'b')
        self.assertEqual(result['selectionResult']['status'], 'completed')
        self.assertEqual(result['selectionResult']['model'], 'b')
        self.assertEqual(result['selectionResult']['kind'], 'start')
        self.assertNotIn('rollbackModel', self.o.state)

    def test_missing_endpoint_is_prepared_in_the_same_selected_start(self):
        self.stopped(endpoint=False)
        self.assertTrue(self.row()['canStart'])
        self.assertTrue(self.o.manual_snapshot()['providerControl']['endpointSetupRequired'])
        self.admit()
        self.run_worker()
        calls = self.provider_calls()
        self.assertEqual(len(calls), 1)
        self.assertIn('--local-endpoint', calls[0])
        self.assertEqual(calls[0][-1], 'b')
        self.assertIn('--idle-timeout', calls[0])
        self.assertNotIn('--no-auth', calls[0])

    def test_phone_can_start_configured_selection_but_cannot_change_endpoint(self):
        self.stopped(endpoint=False)
        self.assertFalse(self.row(remote=True)['canStart'])
        with self.assertRaisesRegex(ValueError, 'on the Mac'):
            self.admit(source='phone')
        self.assertEqual(self.provider_calls(), [])
        self.args.append('--local-endpoint')
        self.write_plist()
        self.admit(source='phone')
        self.run_worker()
        self.assertEqual(len(self.provider_calls()), 1)

    def test_no_commands_or_false_ready_from_stopped_snapshot(self):
        self.stopped()
        self.o.state['manualResult'] = {'status': 'completed', 'detail': 'a is warm and ready'}
        value = self.o.manual_snapshot()
        self.assertIsNone(value['currentModel'])
        self.assertIsNone(value['lastResult'])
        self.assertIsNone(value['selectionResult'])
        self.assertEqual(value['warmup']['status'], 'waiting')
        self.assertIsNone(value['warmup']['model'])
        self.assertEqual(self.provider_calls(), [])

    def test_unknown_and_stale_alive_are_never_stopped_start_permissions(self):
        for process in (True, None):
            self.raw['written_at'] = self.now - 300
            self.o.raw = copy.deepcopy(self.raw)
            self.process.return_value = process
            self.assertFalse(self.row()['canStart'])
            with self.assertRaises(ValueError):
                self.admit()
        self.assertEqual(self.provider_calls(), [])

    def test_stopped_qwen_requires_explicit_known_runtime_verification(self):
        self.stopped()
        self.o.catalog[1]['required_provider_capabilities'] = ['apple_m5', 'mlx_nax']
        row = self.row()
        self.assertTrue(row['canStart'])
        self.assertTrue(row['requiresRuntimeVerification'])
        self.assertFalse(
            next(x for x in self.o.candidates({}, {}, self.o.live, self.o.state) if x['id'] == 'b')[
                'available'
            ]
        )
        with self.assertRaisesRegex(ValueError, 'Confirm runtime'):
            self.admit()
        self.admit(self.payload(verify=True))
        self.run_worker()
        self.assertEqual(len(self.provider_calls()), 1)
        self.o.eligible_models = ['a']
        self.assertFalse(
            next(x for x in self.o.candidates({}, {}, self.o.live, self.o.state) if x['id'] == 'b')[
                'available'
            ]
        )

    def test_unsupported_unknown_missing_or_stale_targets_stay_blocked(self):
        for fault in (
            'chip',
            'capability',
            'template',
            'disk',
            'inactive',
            'ram',
            'catalog',
            'hardware',
            'future',
            'memory',
            'battery',
            'hot',
            'account',
            'device',
        ):
            with self.subTest(fault=fault):
                self.tearDown()
                self.setUp()
                self.stopped()
                self.o.catalog[1]['required_provider_capabilities'] = ['apple_m5', 'mlx_nax']
                if fault == 'chip':
                    self.o.live['hardware']['chip'] = 'Apple M4 Max'
                elif fault == 'capability':
                    self.o.catalog[1]['required_provider_capabilities'] = ['unknown']
                elif fault == 'template':
                    self.o.local[1]['template_render_ok'] = False
                elif fault == 'disk':
                    self.o.local[1]['size_bytes'] = 0
                elif fault == 'inactive':
                    self.o.catalog[1]['active'] = False
                elif fault == 'ram':
                    self.o.catalog[1]['min_ram_gb'] = 129
                elif fault == 'catalog':
                    self.o.discovery_at = self.now - 601
                elif fault == 'hardware':
                    self.o.live['hardware']['at'] = self.now - 20
                elif fault == 'future':
                    self.o.live['at'] = self.now + 60
                elif fault == 'memory':
                    self.o.live['hardware']['memoryAvailableGB'] = 1
                elif fault == 'battery':
                    self.o.on_ac_power.return_value = False
                elif fault == 'hot':
                    self.o.live['hardware']['cpuTemp'] = 96
                elif fault == 'account':
                    self.o.live['account'] = ''
                elif fault == 'device':
                    self.o.live['device'] = 'changed'
                self.assertFalse(self.row()['canStart'])
                with self.assertRaises(ValueError):
                    self.admit(self.payload(verify=True))
                self.assertEqual(self.provider_calls(), [])

    def test_endpoint_auth_and_bind_are_not_silently_rewritten(self):
        self.stopped()
        for extra in (['--no-auth'], ['--bind', '0.0.0.0']):
            self.args = ['--model', 'a', *extra]
            self.write_plist()
            self.assertFalse(self.row()['canStart'])
            with self.assertRaises(ValueError):
                self.admit()
        self.assertEqual(self.provider_calls(), [])

    def test_uuid_same_body_replays_no_command_after_completion(self):
        self.stopped()
        data = self.payload()
        self.admit(data)
        self.run_worker()
        count = len(self.calls)
        self.admit(data)
        self.assertEqual(len(self.calls), count)
        self.assertEqual(len(self.provider_calls()), 1)
        for edit in (
            {'model': 'a'},
            {'verifyRuntime': True},
            {'expectedProvider': 'different'},
            {'expectedSession': 'different'},
        ):
            with self.assertRaisesRegex(ValueError, 'different'):
                self.admit({**data, **edit})
        with self.assertRaisesRegex(ValueError, 'different'):
            self.admit(data, source='phone')

    def test_malformed_uuid_or_payload_never_observes_or_mutates(self):
        self.stopped()
        data = self.payload()
        before = copy.deepcopy(self.o.state)
        with patch.object(self.p, 'inspect', side_effect=AssertionError('must not inspect')):
            for edit in (
                {'requestId': 'bad'},
                {'command': 'start'},
                {'verifyRuntime': 'yes'},
                {'model': ['b']},
            ):
                with self.assertRaises(ValueError):
                    self.o.manual_selection.admit({**data, **edit}, 'mac')
        self.assertEqual(self.o.state, before)
        self.assertEqual(self.provider_calls(), [])

    def test_changed_provider_and_session_rejected_before_persisting(self):
        self.stopped()
        data = self.payload()
        before = copy.deepcopy(self.o.state)
        for edit in ({'expectedProvider': 'other'}, {'expectedSession': 'other'}):
            with self.assertRaisesRegex(ValueError, 'changed'):
                self.admit({**data, **edit})
        self.assertEqual(self.o.state, before)

    def test_save_failure_never_admits_or_dispatches(self):
        self.stopped()
        before = copy.deepcopy(self.o.state)
        self.o.save = Mock(side_effect=OSError('synthetic'))
        with self.assertRaises(OSError):
            self.admit()
        self.assertEqual(self.o.state, before)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.provider_calls(), [])

    def test_state_changes_during_slow_preflight_prevent_start(self):
        for fault in (
            'process',
            'account',
            'target',
            'stop',
            'memory',
            'files',
            'catalog',
            'hardware',
        ):
            with self.subTest(fault=fault):
                self.tearDown()
                self.setUp()
                self.stopped()
                self.admit()
                original = self.o.verify_local_target

                def change(*args):
                    original(*args)
                    if fault == 'process':
                        self.raw['pid'] = 999
                    elif fault == 'account':
                        self.o.live['account'] = 'another'
                    elif fault == 'target':
                        self.o.state['selectionRequest']['model'] = 'a'
                    elif fault == 'stop':
                        self.o.stop.is_set.return_value = True
                    elif fault == 'memory':
                        self.o.live['hardware']['memoryAvailableGB'] = 1
                    elif fault == 'files':
                        self.o.local[1]['template_render_ok'] = False
                    elif fault == 'catalog':
                        self.o.catalog[1]['active'] = False
                    elif fault == 'hardware':
                        self.o.live['hardware']['at'] = self.now - 100

                self.o.verify_local_target = change
                self.run_worker()
                self.assertEqual(self.provider_calls(), [])
                self.assertEqual(self.o.state['manualResult']['status'], 'failed')

    def test_cli_success_without_readiness_does_not_complete_or_start_saved_a(self):
        self.stopped()
        self.admit()
        self.o.verify_started.return_value = False
        self.o.verify_started.side_effect = None
        self.run_worker()
        self.assertEqual(self.o.state['manualResult']['status'], 'failed')
        calls = self.provider_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][-1], 'b')

    def test_reopen_never_replays_admitted_start(self):
        self.stopped()
        self.admit()
        reopened = Optimizer(self.h, self.net, self.tmp.name, self.stop, Mock())
        self.assertEqual(reopened.state['mode'], 'observe')
        self.assertNotIn('pending', reopened.state)
        self.assertEqual(reopened.state['manualResult']['status'], 'failed')
        reopened.runner.assert_not_called()

    def test_running_selection_uses_existing_idle_queue(self):
        self.o.idle_since = None
        self.admit()
        self.assertEqual(self.o.state['requestedModel'], 'b')
        self.o.switch = Mock()
        self.o._tick(time.time())
        self.o.switch.assert_not_called()
        self.o.idle_since = time.time() - 13
        self.o._tick(time.time())
        self.o.worker.join(2)
        self.o.switch.assert_called_once_with('a', 'b', 'acct', self.o.live['device'])
        self.assertTrue(self.o.state['pending']['selectionAction'])

    def test_running_busy_queue_retains_five_minute_deadline(self):
        self.o.idle_since = None
        self.raw['inference_active'] = True
        self.o.raw = copy.deepcopy(self.raw)
        self.admit()
        self.o.state['requestedAt'] = time.time() - 300
        self.o.switch = Mock()
        self.o._tick(time.time())
        self.o.worker.join(2)
        self.o.switch.assert_called_once()
        self.assertTrue(self.o.state['pending']['afterIdleTimeout'])

    def test_running_endpoint_setup_is_part_of_selected_switch(self):
        self.args.remove('--local-endpoint')
        self.write_plist()
        self.assertTrue(self.row()['canSwitch'])
        self.admit()
        self.arm_switch()
        self.o.switch('a', 'b', 'acct', self.o.live['device'])
        calls = self.provider_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][-1], 'b')
        self.assertIn('--local-endpoint', calls[0])
        self.assertEqual(self.o.state['manualResult']['status'], 'completed')

    def test_same_running_target_pauses_automation_without_restart(self):
        self.o.state['mode'] = 'demand'
        self.admit(self.payload(model='a'))
        self.assertEqual(self.o.state['mode'], 'observe')
        self.assertEqual(self.o.state['manualResult']['status'], 'unchanged')
        self.assertEqual(self.provider_calls(), [])

    def test_running_selection_cancels_through_existing_request_id(self):
        data = self.payload()
        self.admit(data)
        value = self.o.manual_action({'action': 'cancel', 'requestId': data['requestId']})
        self.assertEqual(value['selectionResult']['status'], 'cancelled')
        self.assertEqual(self.provider_calls(), [])

    def test_completed_receipt_is_hidden_after_stop_or_new_session(self):
        self.stopped()
        self.admit()
        self.run_worker()
        self.assertIsNotNone(self.o.manual_snapshot()['selectionResult'])
        self.stopped()
        self.assertIsNone(self.o.manual_snapshot()['selectionResult'])
        self.process.return_value = True
        self.o.service_disabled.return_value = False
        self.raw.update(pid=999, written_at=time.time())
        self.o.raw = copy.deepcopy(self.raw)
        self.assertIsNone(self.o.manual_snapshot()['selectionResult'])

    def test_request_id_cannot_be_reused_by_legacy_provider_or_switch_routes(self):
        self.stopped()
        data = self.payload()
        self.admit(data)
        self.run_worker()
        with self.assertRaisesRegex(ValueError, 'different'):
            self.o.manual_action(
                {
                    'action': 'provider-stop',
                    'requestId': data['requestId'],
                    'expectedProvider': self.p.inspect()['version'],
                }
            )
        with self.assertRaisesRegex(ValueError, 'different'):
            self.o.manual_action(
                {
                    'action': 'switch',
                    'model': 'a',
                    'requestId': data['requestId'],
                    'expectedSession': session_key(self.raw),
                }
            )
        self.assertEqual(len(self.provider_calls()), 1)

    def test_existing_legacy_id_cannot_admit_selected_start(self):
        self.stopped()
        data = self.payload()
        for group in ('providerRequests', 'manualRequests'):
            self.o.state[group] = [{'id': data['requestId']}]
            with self.assertRaisesRegex(ValueError, 'different'):
                self.admit(data)
            self.o.state.pop(group)
        self.assertEqual(self.provider_calls(), [])

    def test_refresh_is_not_permission_to_start_stopped_provider(self):
        self.stopped()
        self.o.manual_action({'action': 'refresh'})
        self.assertEqual(self.provider_calls(), [])
        self.assertFalse(self.o.state.get('pending'))

    def test_update_guard_blocks_selected_start_and_preserves_stop(self):
        self.stopped()
        data = self.payload()
        self.o.update_guard.action({'action': 'prepare', 'requestId': str(uuid.uuid4())})
        with self.assertRaisesRegex(ValueError, 'update'):
            self.admit(data)
        self.assertEqual(self.provider_calls(), [])

    def test_pending_target_tampering_during_preflight_sends_nothing(self):
        self.stopped()
        self.admit()
        original = self.o.verify_local_target

        def change(*args):
            original(*args)
            self.o.state['pending']['model'] = 'a'

        self.o.verify_local_target = change
        self.run_worker()
        self.assertEqual(self.provider_calls(), [])

    def test_local_preflight_failure_has_actionable_sanitized_reason(self):
        from optimizer import DemandDeferred

        self.stopped()
        self.admit()
        self.o.verify_local_target = Mock(
            side_effect=DemandDeferred('private path should not leak')
        )
        self.run_worker()
        detail = self.o.state['manualResult']['detail']
        self.assertIn('Refresh the model list', detail)
        self.assertNotIn('private path', detail)
        self.assertEqual(self.provider_calls(), [])

    def test_refresh_preserves_queued_and_completed_receipts(self):
        self.admit()
        before = copy.deepcopy(self.o.state)
        value = self.o.manual_action({'action': 'refresh'})
        self.assertEqual(self.o.state, before)
        self.assertEqual(value['selectionResult']['status'], 'queued')
        self.assertEqual(self.o.next_discovery, 0)
        self.assertEqual(self.o.next_identity, 0)
        self.tearDown()
        self.setUp()
        self.stopped()
        self.admit()
        self.run_worker()
        before = copy.deepcopy(self.o.state)
        value = self.o.manual_action({'action': 'refresh'})
        self.assertEqual(self.o.state, before)
        self.assertEqual(value['selectionResult']['status'], 'completed')
        self.assertEqual(len(self.provider_calls()), 1)

    def test_same_selected_model_missing_endpoint_still_requires_guarded_restart(self):
        self.args.remove('--local-endpoint')
        self.write_plist()
        self.admit(self.payload(model='a'))
        self.assertEqual(self.o.state['manualResult']['status'], 'queued')
        self.o.switch = Mock()
        self.o.idle_since = time.time() - 13
        self.o._tick(time.time())
        self.o.worker.join(2)
        self.o.switch.assert_called_once_with('a', 'a', 'acct', self.o.live['device'])

    def test_lost_reply_completed_then_stopped_reconciles_without_restart(self):
        self.stopped()
        data = self.payload()
        self.admit(data)
        self.run_worker()
        self.stopped()
        self.assertIsNone(self.o.manual_snapshot()['selectionResult'])
        response = self.admit(data)
        self.assertEqual(response['selectionResult']['id'], data['requestId'])
        self.assertEqual(response['selectionResult']['model'], 'b')
        self.assertEqual(response['selectionResult']['status'], 'cancelled')
        self.assertIn('No command was repeated', response['selectionResult']['detail'])
        self.assertEqual(len(self.provider_calls()), 1)
        self.assertEqual(response['providerControl']['status'], 'stopped')

    def test_retry_old_request_after_new_admission_returns_its_own_terminal_receipt(self):
        self.stopped()
        first = self.payload()
        self.admit(first)
        self.run_worker()
        self.stopped()
        second = self.payload(model='a')
        self.admit(second)
        before = copy.deepcopy(self.o.state)
        response = self.admit(first)
        self.assertEqual(response['selectionResult']['id'], first['requestId'])
        self.assertEqual(response['selectionResult']['status'], 'cancelled')
        self.assertEqual(self.o.state, before)
        self.assertEqual(self.o.state['selectionRequest']['id'], second['requestId'])
        self.assertEqual(len(self.provider_calls()), 1)
        self.assertEqual(len(self.calls), 2)

    def test_provider_token_binds_account_even_before_admission_begins(self):
        self.stopped()
        data = self.payload()
        self.o.live['account'] = 'new-account'
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.admit(data)
        self.assertEqual(self.provider_calls(), [])
        self.assertFalse(self.o.state.get('pending'))


if __name__ == '__main__':
    unittest.main()
