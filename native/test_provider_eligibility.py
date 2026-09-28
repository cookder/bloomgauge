"""Run61 regressions: identity, eligibility, safe restoration and explicit resume."""

import copy, json, time, unittest
from unittest.mock import Mock, patch
from optimizer import Optimizer, roster_identity, session_key, DemandDeferred
import test_optimizer


class EligibilityTests(unittest.TestCase):
    setUp = test_optimizer.ControllerTests.setUp
    tearDown = test_optimizer.ControllerTests.tearDown

    def roster(self, models=None, **changes):
        return {
            'se_public_key': 'public-test-key',
            'provider_id': 'p',
            'models': models,
            'status': 'online',
            'trust_level': 'hardware',
            **changes,
        }

    def refresh(self, row):
        self.net.fetch.side_effect = lambda path: {'providers': [row]}
        self.o.next_discovery = self.now + 1000
        self.o.next_identity = 0
        # This fixture advances its logical clock explicitly; discovery-lag
        # coverage supplies elapsed monotonic time in the reporting suite.
        with patch('optimizer.time.monotonic', return_value=0):
            self.o.refresh(self.now)

    def test_null_empty_or_malformed_roster_never_becomes_serving_eligibility(self):
        for models in (None, [], {}, 'a', [None]):
            with self.subTest(models=models):
                self.refresh(self.roster(models))
                self.assertTrue(self.o.device_identity_ok)
                self.assertFalse(self.o.identity_ok)
                self.assertIn(
                    'catalog or runtime eligibility', self.o.wait_reason(self.live, self.now)
                )
                self.assertFalse(self.o.tracking(self.raw, self.now)['counting'])
                self.assertEqual(
                    self.h.db.execute('SELECT COUNT(*) FROM opt_identity').fetchone()[0], 1
                )

    def test_unique_hardware_identity_is_required_even_if_models_match(self):
        for rows in (
            [],
            [self.roster(['a'])] * 2,
            [self.roster(['a'], se_public_key='different')],
            [self.roster(['a'], provider_id=None)],
        ):
            with self.subTest(rows=rows):
                with self.assertRaises(ValueError):
                    roster_identity(self.raw, rows)

    def test_identity_is_not_selection_equality(self):
        self.refresh(self.roster(['b']))
        self.assertTrue(self.o.device_identity_ok)
        self.assertFalse(self.o.identity_ok)
        self.refresh(self.roster(['a']))
        self.assertTrue(self.o.identity_ok)

    def gate(self):
        self.o.catalog[1]['required_provider_capabilities'] = ['apple_m5', 'mlx_nax']
        # Even with the runtime reporting both, a model never served here is not automatic.
        self.o.raw['runtime_capabilities'] = ['apple_m5', 'mlx_nax']
        self.o.device_identity_ok = True
        self.o.identity_hardware = True
        self.o.identity_device = self.live['device']
        self.o.eligible_models = ['a']

    def row(self):
        return next(r for r in self.o.candidates({}, {}, self.live, self.o.state) if r['id'] == 'b')

    def available(self):
        return self.row()['available']

    def test_m5_chip_and_disk_presence_do_not_prove_runtime_capabilities(self):
        self.gate()
        self.assertFalse(self.available())
        self.assertIn('Needs one run on this Mac first', self.row()['reason'])
        self.o.eligible_models = ['a', 'b']
        self.assertTrue(self.available())
        for field, value in [
            ('identity_at', self.now - 181),
            ('identity_at', self.now + 300),
            ('identity_hardware', False),
            ('device_identity_ok', False),
            ('identity_session', (0, 0)),
        ]:
            old = getattr(self.o, field)
            setattr(self.o, field, value)
            self.assertFalse(self.available(), field)
            setattr(self.o, field, old)

    def test_gated_target_cannot_dispatch_from_a_saved_automatic_proposal(self):
        self.gate()
        self.o.state['pending'] = {'kind': 'automatic', 'automaticMode': 'week', 'model': 'b'}
        self.o.command = Mock()
        self.o.switch('a', 'b', 'acct', self.live['device'])
        self.o.command.assert_not_called()

    def test_malformed_capability_requirements_fail_closed(self):
        for required in ({}, False, 0, '', [''], [' apple_m5'], [None]):
            with self.subTest(required=required):
                self.o.catalog[1]['required_provider_capabilities'] = required
                self.assertFalse(self.available())

    def fail_target(self):
        failed = {
            **copy.deepcopy(self.raw),
            'advertised_models': ['b'],
            'current_model': None,
            'warm_models': [],
            'started_at': self.now - 10,
            'pid': 2,
        }

        def verify(target, *args, **kwargs):
            if target == 'a':
                return True
            self.o.raw = copy.deepcopy(failed)
            self.o.read_state.return_value = copy.deepcopy(failed)
            self.o.read_options.return_value = ('b', ['--local-endpoint', '--port', '8000'], {})
            self.o.identity_ok = False
            self.o.identity_session = (failed['started_at'], failed['pid'])
            self.o.recovery_ready.return_value = copy.deepcopy(failed)
            return False

        self.net.fetch.side_effect = lambda path: (
            {'models': self.o.catalog}
            if path == '/v1/models/catalog'
            else {'providers': [self.roster(None)]}
        )
        self.o.command = Mock()
        self.o.verify_started = Mock(side_effect=verify)
        return failed

    def test_failed_ineligible_target_restores_verified_previous_without_eligibility_bypass(self):
        self.fail_target()
        start = self.o.state['startedAt']
        self.o.switch('a', 'b', 'acct', self.live['device'])
        self.assertEqual([c.args[0] for c in self.o.command.call_args_list], ['b', 'a'])
        self.assertEqual(self.o.state['mode'], 'observe')
        self.assertEqual(self.o.state['startedAt'], start)
        self.assertFalse(self.o.identity_ok)
        self.assertIn('Restored', self.o.detail)
        self.assertFalse(
            any(
                c.args[0] == ['/usr/bin/sudo', '-n', '/usr/sbin/purge']
                for c in self.o.runner.call_args_list
            )
        )

    def test_recovery_requires_previous_warm_decode_proof(self):
        self.fail_target()
        self.o.warmup = {}
        self.o.switch('a', 'b', 'acct', self.live['device'])
        self.assertEqual([c.args[0] for c in self.o.command.call_args_list], ['b'])

    def test_recovery_does_not_ignore_hardware_trust_process_account_or_source_guards(self):
        cases = [
            'wrong_key',
            'duplicate',
            'offline',
            'software_trust',
            'local_trust',
            'process',
            'account',
            'device',
            'session',
            'stale',
            'hot',
            'battery',
        ]
        for case in cases:
            with self.subTest(case=case):
                raw = copy.deepcopy(self.raw)
                self.o.raw = copy.deepcopy(raw)
                self.o.live = copy.deepcopy(self.live)
                rows = [self.roster(None)]
                self.o.on_ac_power.return_value = True
                if case == 'wrong_key':
                    rows[0]['se_public_key'] = 'other'
                elif case == 'duplicate':
                    rows *= 2
                elif case == 'offline':
                    rows[0]['status'] = 'offline'
                elif case == 'software_trust':
                    rows[0]['trust_level'] = 'software'
                elif case == 'local_trust':
                    raw['trust']['trust_level'] = 'software'
                elif case == 'account':
                    self.o.live['account'] = 'other'
                elif case == 'device':
                    raw['attestation_public_key'] = 'other'
                elif case == 'session':
                    raw['pid'] = 2
                elif case == 'stale':
                    raw['written_at'] = self.now - 20
                elif case == 'hot':
                    self.o.live['hardware']['thermal'] = 'Serious'
                elif case == 'battery':
                    self.o.on_ac_power.return_value = False
                self.net.fetch.side_effect = lambda path: {'providers': rows}
                with patch('optimizer.matching_process', return_value=case != 'process'):
                    self.assertFalse(
                        self.o.recovery_identity(raw, 'acct', self.live['device'], self.now)
                    )

    def test_activity_change_after_idle_verification_prevents_restore(self):
        failed = self.fail_target()
        base = self.o.verify_started.side_effect

        def verify(target, *args):
            result = base(target, *args)
            if target == 'b':
                self.o.read_state.return_value = {**failed, 'inference_active': True}
            return result

        self.o.verify_started.side_effect = verify
        self.o.switch('a', 'b', 'acct', self.live['device'])
        self.assertEqual([c.args[0] for c in self.o.command.call_args_list], ['b'])

    def test_work_arriving_during_roster_network_read_prevents_restore(self):
        failed = self.fail_target()
        reads = []

        def fetch(path):
            if path == '/v1/models/catalog':
                return {'models': self.o.catalog}
            reads.append(path)
            if len(reads) == 2:
                self.o.read_state.return_value = {
                    **failed,
                    'stats': {'requests_served': 11, 'tokens_generated': 25},
                }
            return {'providers': [self.roster(None)]}

        self.net.fetch.side_effect = fetch
        self.o.switch('a', 'b', 'acct', self.live['device'])
        self.assertEqual(len(reads), 2)
        self.assertEqual([c.args[0] for c in self.o.command.call_args_list], ['b'])

    def test_changed_launch_or_disabled_service_during_failure_is_preserved(self):
        for changed in ('launch', 'disabled'):
            with self.subTest(changed=changed):
                self.o.raw = copy.deepcopy(self.raw)
                self.o.read_state.return_value = copy.deepcopy(self.raw)
                self.o.read_options.return_value = ('a', ['--local-endpoint', '--port', '8000'], {})
                self.o.identity_ok = True
                self.o.identity_session = (self.raw['started_at'], self.raw['pid'])
                self.o.service_disabled.return_value = False
                self.fail_target()
                base = self.o.verify_started.side_effect

                def verify(target, *args):
                    result = base(target, *args)
                    if changed == 'launch':
                        self.o.read_options.return_value = (
                            'b',
                            ['--local-endpoint', '--port', '8001'],
                            {},
                        )
                    else:
                        self.o.service_disabled.return_value = True
                    return result

                self.o.verify_started.side_effect = verify
                self.o.switch('a', 'b', 'acct', self.live['device'])
                self.assertEqual([c.args[0] for c in self.o.command.call_args_list], ['b'])

    def test_recovery_still_checks_disk_and_catalog(self):
        self.fail_target()
        self.o.local[0]['template_render_ok'] = False
        self.o.runner.return_value.stdout = json.dumps({'models': self.o.local})
        self.o.switch('a', 'b', 'acct', self.live['device'])
        self.assertEqual([c.args[0] for c in self.o.command.call_args_list], ['b'])

    def resume(self):
        self.o.state.update(mode='observe', endsAt=None)
        return self.o.control_action(
            {'action': 'resume-demand', 'expectedControl': self.o.control_version()}
        )

    def test_explicit_resume_keeps_saved_plan_policy_history_and_provider(self):
        self.o.state.update(mode='observe', endsAt=None)
        before = copy.deepcopy(self.o.state)
        self.o.command = Mock()
        self.resume()
        for key in [
            'startedAt',
            'endsAt',
            'lastSwitchAt',
            'originalModel',
            'models',
            'demandPolicy',
            'blockHours',
        ]:
            self.assertEqual(self.o.state.get(key), before.get(key), key)
        self.assertEqual(self.o.state['mode'], 'demand')
        self.o.command.assert_not_called()

    def test_resume_requires_verified_readiness_and_current_controls(self):
        for fault in ('cold', 'disabled', 'stale', 'identity', 'changed_model', 'queued'):
            with self.subTest(fault=fault):
                self.o.state.update(
                    mode='observe', endsAt=None, expectedModel='a', requestedModel=None
                )
                self.o.service_disabled.return_value = False
                self.o.identity_ok = True
                self.o.live['at'] = self.now
                self.o.warmup = {
                    'session': session_key(self.raw),
                    'model': 'a',
                    'status': 'ready',
                    'verifiedAt': self.now - 1,
                }
                if fault == 'cold':
                    self.o.warmup = {}
                    # Legacy only: the manager turns on with a cold model (test_manager.py).
                    self.o.state['demandPolicy'] = {
                        **self.o.state['demandPolicy'],
                        'managerStrategy': 0,
                    }
                elif fault == 'disabled':
                    self.o.service_disabled.return_value = True
                elif fault == 'stale':
                    self.o.live['at'] = self.now - 30
                elif fault == 'identity':
                    self.o.identity_ok = False
                elif fault == 'changed_model':
                    self.o.state['expectedModel'] = 'b'
                elif fault == 'queued':
                    self.o.state['requestedModel'] = 'b'
                with self.assertRaises(ValueError):
                    self.resume()
                self.assertEqual(self.o.state['mode'], 'observe')

    def test_reviewed_external_model_can_resume_without_restarting_or_resetting_history(self):
        self.o.state.update(
            mode='observe',
            endsAt=None,
            expectedModel='b',
            rollbackModel='b',
            demandProposal={'target': 'b'},
        )
        self.o.command = Mock()
        before = copy.deepcopy(self.o.state)
        status = self.o.demand_resume_status()
        self.assertTrue(status['available'])
        self.assertEqual(status['currentModel'], 'a')
        self.assertEqual(self.o.state, before)  # Reading availability never resumes.
        self.o.control_action(
            {
                'action': 'resume-demand',
                'expectedControl': self.o.control_version(),
                'currentModel': 'a',
            },
            'phone',
        )
        self.assertEqual(self.o.state['mode'], 'demand')
        self.assertEqual(self.o.state['expectedModel'], 'a')
        for key in ['startedAt', 'endsAt', 'originalModel', 'models', 'demandPolicy', 'blockHours']:
            self.assertEqual(self.o.state.get(key), before.get(key), key)
        self.assertNotIn('rollbackModel', self.o.state)
        self.assertNotIn('demandProposal', self.o.state)
        self.o.command.assert_not_called()
        self.o.runner.assert_not_called()

    def test_explicit_model_review_cannot_bypass_readiness_identity_stop_or_freshness(self):
        cases = [
            'stopped',
            'cold',
            'account',
            'identity',
            'stale',
            'catalog',
            'wrong_model',
            'stale_control',
            'queued',
            'settings',
        ]
        for fault in cases:
            with self.subTest(fault=fault):
                self.tearDown()
                self.setUp()
                self.o.state.update(mode='observe', endsAt=None, expectedModel='b')
                payload = {
                    'action': 'resume-demand',
                    'expectedControl': self.o.control_version(),
                    'currentModel': 'a',
                }
                if fault == 'stopped':
                    self.o.service_disabled.return_value = True
                elif fault == 'cold':
                    self.o.warmup = {}
                    self.o.state['demandPolicy'] = {
                        **self.o.state['demandPolicy'],
                        'managerStrategy': 0,
                    }
                elif fault == 'account':
                    self.o.live['account'] = 'changed'
                elif fault == 'identity':
                    self.o.identity_ok = False
                elif fault == 'stale':
                    self.o.live['at'] = self.now - 40
                elif fault == 'catalog':
                    self.o.discovery_at = self.now - 1000
                elif fault == 'wrong_model':
                    payload['currentModel'] = 'b'
                elif fault == 'stale_control':
                    payload['expectedControl'] = 'old'
                elif fault == 'queued':
                    self.o.state['requestedModel'] = 'a'
                elif fault == 'settings':
                    payload['models'] = ['a', 'b']
                before = copy.deepcopy(self.o.state)
                with self.assertRaises(ValueError):
                    self.o.control_action(payload)
                self.assertEqual(self.o.state, before)
                self.o.runner.assert_not_called()

    def test_resume_persistence_failure_rolls_back_reviewed_model_and_mode(self):
        self.o.state.update(mode='observe', endsAt=None, expectedModel='b')
        before = copy.deepcopy(self.o.state)
        self.o.save = Mock(side_effect=OSError('fixture'))
        with self.assertRaises(OSError):
            self.o.control_action(
                {
                    'action': 'resume-demand',
                    'expectedControl': self.o.control_version(),
                    'currentModel': 'a',
                }
            )
        self.assertEqual(self.o.state, before)
        self.o.runner.assert_not_called()

    def test_stopped_provider_resume_status_points_to_controller_without_action(self):
        self.o.state.update(mode='observe', endsAt=None)
        self.o.service_disabled.return_value = True
        result = self.o.demand_resume_status()
        self.assertTrue(result['hasSavedPlan'])
        self.assertFalse(result['available'])
        self.assertIn('Optimizer → Overview', result['reason'])
        self.assertEqual(self.o.state['mode'], 'observe')
        self.o.runner.assert_not_called()


if __name__ == '__main__':
    unittest.main()
