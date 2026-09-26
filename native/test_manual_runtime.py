"""Unadvertised protected models: explicit manual verification, never auto admission."""

import copy
import unittest
import uuid
from unittest.mock import Mock, patch
import test_optimizer
from optimizer import launch_signature, session_key


class ManualRuntimeTests(unittest.TestCase):
    tearDown = test_optimizer.ControllerTests.tearDown

    def manual_payload(self):
        return {
            'action': 'switch',
            'model': 'b',
            'expectedSession': session_key(self.raw),
            'requestId': str(uuid.uuid4()),
        }

    def setUp(self):
        test_optimizer.ControllerTests.setUp(self)
        self.o.catalog[1]['required_provider_capabilities'] = ['apple_m5', 'mlx_nax']
        self.o.device_identity_ok = self.o.identity_hardware = True
        self.o.eligible_models = ['a']

    def row(self, manual=False):
        return next(
            m
            for m in self.o.candidates({}, {}, self.o.live, self.o.state, manual=manual)
            if m['id'] == 'b'
        )

    def queue(self):
        p = {**self.manual_payload(), 'verifyRuntime': True}
        self.o.manual_action(p, 'phone')
        return p

    def arm(self):
        self.o.state['pending'] = {
            'model': 'b',
            'previous': 'a',
            'kind': 'manual',
            'requestId': self.o.state['requestId'],
            'verifyRuntime': True,
            'session': session_key(self.raw),
            'launchSignature': launch_signature(['--local-endpoint', '--port', '8000'], {}),
        }
        self.o.command = Mock()
        self.o.verify_started = Mock(return_value=True)

    def test_unadvertised_model_can_be_manually_verified_but_stays_out_of_auto_pool(self):
        self.assertFalse(self.row()['available'])
        self.assertTrue(self.row(True)['available'])
        self.assertTrue(self.row(True)['requiresRuntimeVerification'])
        self.assertFalse(self.row()['available'])
        self.assertEqual(
            [
                c.args[0]
                for c in self.o.runner.call_args_list
                if c.args[0] != ['/usr/bin/sudo', '-n', '-ll', '/usr/sbin/purge']
            ],
            [],
        )

    def test_explicit_verification_required_and_idempotent(self):
        before = copy.deepcopy(self.o.state)
        with self.assertRaisesRegex(ValueError, 'Verify'):
            self.o.manual_action(self.manual_payload(), 'phone')
        self.assertEqual(self.o.state, before)
        p = self.queue()
        self.assertTrue(self.o.state['requestedVerifyRuntime'])
        state = copy.deepcopy(self.o.state)
        self.o.manual_action(p, 'phone')
        self.assertEqual(self.o.state, state)
        with self.assertRaises(ValueError):
            self.o.manual_action({**p, 'verifyRuntime': False}, 'phone')
        self.assertEqual(
            [
                c.args[0]
                for c in self.o.runner.call_args_list
                if c.args[0] != ['/usr/bin/sudo', '-n', '-ll', '/usr/sbin/purge']
            ],
            [],
        )

    def test_manual_guard_does_not_turn_hardware_or_unknown_requirements_into_attestation(self):
        for fault in [
            'M4',
            'M50',
            'unknown',
            'future',
            'stale',
            'session',
            'hardware',
            'identity',
            'cold',
            'template',
            'catalog',
            'ram',
        ]:
            with self.subTest(fault=fault):
                self.tearDown()
                self.setUp()
                if fault in ['M4', 'M50']:
                    self.o.live['hardware']['chip'] = 'Apple ' + fault + ' Max'
                elif fault == 'unknown':
                    self.o.catalog[1]['required_provider_capabilities'] = ['new_runtime']
                elif fault == 'future':
                    self.o.identity_at = self.now + 300
                elif fault == 'stale':
                    self.o.identity_at = self.now - 181
                elif fault == 'session':
                    self.o.identity_session = (0, 0)
                elif fault == 'hardware':
                    self.o.identity_hardware = False
                elif fault == 'identity':
                    self.o.identity_ok = False
                elif fault == 'cold':
                    self.o.warmup = {}
                elif fault == 'template':
                    self.o.local[1]['template_render_ok'] = False
                elif fault == 'catalog':
                    self.o.catalog[1]['active'] = False
                elif fault == 'ram':
                    self.o.catalog[1]['min_ram_gb'] = 129
                self.assertFalse(self.row(True)['available'])

    def test_acknowledged_manual_request_reaches_guarded_dispatch(self):
        self.queue()
        self.o.idle_since = self.now - 13
        self.o.switch = Mock()
        self.o.tick(self.now)
        self.o.worker.join(timeout=2)
        self.o.switch.assert_called_once_with('a', 'b', 'acct', self.live['device'])
        self.assertTrue(self.o.state['pending']['verifyRuntime'])
        self.assertEqual(self.o.state['mode'], 'observe')

    def test_switch_refreshes_files_then_uses_normal_post_start_verification(self):
        self.queue()
        self.arm()
        with patch('optimizer.time.time', return_value=self.now):
            self.o.switch('a', 'b', 'acct', self.live['device'])
        self.o.command.assert_called_once_with('b', ['--local-endpoint', '--port', '8000'], {})
        self.assertEqual(self.o.verify_started.call_count, 1)
        self.assertEqual(self.o.runner.call_args.args[0][1:4], ['models', 'list', '--json'])
        self.assertFalse(self.row()['available'])  # A mocked completion is not capability proof.

    def test_activity_during_slow_preflight_keeps_manual_selection_queued(self):
        self.queue()
        self.arm()
        original = self.o.verify_local_target

        def changed(*args):
            original(*args)
            self.o.read_state.return_value = {**copy.deepcopy(self.raw), 'inference_active': True}

        self.o.verify_local_target = changed
        with patch('optimizer.time.time', return_value=self.now):
            self.o.switch('a', 'b', 'acct', self.live['device'])
        self.o.command.assert_not_called()
        self.assertEqual(self.o.state['requestedModel'], 'b')

    def test_stale_or_forged_manual_ack_cannot_admit_automatic_switch(self):
        self.o.state.update(requestedVerifyRuntime=True)
        self.o.state['pending'] = {
            'kind': 'automatic',
            'automaticMode': 'week',
            'model': 'b',
            'verifyRuntime': True,
        }
        self.o.command = Mock()
        self.o.switch('a', 'b', 'acct', self.live['device'])
        self.o.command.assert_not_called()

    def test_verified_current_model_needs_no_manual_exception(self):
        self.o.eligible_models = ['a', 'b']
        self.assertTrue(self.row()['available'])
        self.assertFalse(self.row(True)['requiresRuntimeVerification'])

    def test_changed_or_removed_target_during_preflight_does_not_restart(self):
        for fault in ['cancelled', 'catalog', 'stopped', 'request', 'identity']:
            with self.subTest(fault=fault):
                self.tearDown()
                self.setUp()
                self.queue()
                self.arm()
                original = self.o.verify_local_target

                def changed(*args):
                    original(*args)
                    if fault == 'cancelled':
                        self.o.state['requestedModel'] = None
                    elif fault == 'catalog':
                        self.o.catalog[1]['active'] = False
                    elif fault == 'stopped':
                        self.o.service_disabled.return_value = True
                    elif fault == 'request':
                        self.o.state['requestId'] = str(uuid.uuid4())
                    elif fault == 'identity':
                        self.o.identity_hardware = False

                self.o.verify_local_target = changed
                self.o.switch('a', 'b', 'acct', self.live['device'])
                self.o.command.assert_not_called()

    def test_failed_verification_uses_guarded_previous_model_recovery(self):
        from test_provider_eligibility import EligibilityTests

        self.queue()
        self.arm()
        self.roster = lambda models: {
            'se_public_key': 'public-test-key',
            'provider_id': 'p',
            'models': models,
            'status': 'online',
            'trust_level': 'hardware',
        }
        EligibilityTests.fail_target(self)
        self.o.switch('a', 'b', 'acct', self.live['device'])
        self.assertEqual([call.args[0] for call in self.o.command.call_args_list], ['b', 'a'])
        self.assertEqual(self.o.state['mode'], 'observe')
        self.assertIn('Restored', self.o.detail)
        self.assertFalse(self.o.identity_ok)
        self.assertFalse(
            any(
                c.args[0] == ['/usr/bin/sudo', '-n', '/usr/sbin/purge']
                for c in self.o.runner.call_args_list
            )
        )

    def test_busy_model_uses_existing_five_minute_manual_deadline(self):
        self.queue()
        self.o.state['requestedAt'] = self.now - 300
        self.o.raw['inference_active'] = True
        self.o.read_state.return_value = copy.deepcopy(self.o.raw)
        self.arm()
        self.o.state['pending'].update(afterIdleTimeout=True, requestedAt=self.now - 300)
        with patch('optimizer.time.time', return_value=self.now):
            self.o.switch('a', 'b', 'acct', self.live['device'])
        self.o.command.assert_called_once()


if __name__ == '__main__':
    unittest.main()
