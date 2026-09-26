"""Regression coverage for paid work racing a managed switch's warm-up.

Only fixture clocks, temporary files and mocked endpoints are used. No provider,
launchctl, administrator command or network request is invoked by these tests.
"""

import copy
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import test_prewarm as fixtures
from model_combinations import selection_key
from optimizer import ExternalChange, launch_signature
from prewarm import WarmupDeferred, WarmupError, prewarm


class ManagedWarmupRaceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.WarmupControllerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.o = self.fixture.o
        self.raw = self.fixture.raw
        self.started = self.fixture.now
        self.previous_session = self.raw['started_at'] - 100
        self.o.command = Mock()
        self.o.recover_file_cache = Mock()

    def verify(self, **kwargs):
        return self.o.verify_started('a', self.previous_session, **kwargs)

    def assert_no_recovery_commands(self):
        self.o.command.assert_not_called()
        self.o.recover_file_cache.assert_not_called()
        self.o.runner.assert_not_called()

    def test_paid_work_during_idle_recheck_completes_without_competing_request(self):
        # Keep resource checks stable while exercising the real final activity
        # check inside perform_prewarm, after verify_started's 12-second wait.
        self.o.prewarm_reason = Mock(
            side_effect=lambda raw, *args, **kwargs: (
                'Waiting for idle capacity before pre-warming.' if raw['inference_active'] else None
            )
        )
        perform = self.o.perform_prewarm

        def arrival(target, observed, options, **kwargs):
            self.raw['inference_active'] = True
            try:
                return perform(target, observed, options, **kwargs)
            finally:
                # Paid work completes on the next source observation. It did
                # not exist when the idle admission check was performed.
                self.raw['stats'] = {'requests_served': 1, 'tokens_generated': 10}

        self.o.perform_prewarm = Mock(side_effect=arrival)
        with patch('optimizer.prewarm') as synthetic:
            self.assertTrue(self.verify(timeout=40))
        synthetic.assert_not_called()
        self.assertEqual(self.o.perform_prewarm.call_count, 1)
        self.assertEqual(self.o.warmup['status'], 'ready')
        self.assertGreater(self.fixture.now - self.started, 12)
        self.assertLess(self.fixture.now - self.started, 40)
        self.assert_no_recovery_commands()

    def test_capacity_deferrals_preserve_deadline_and_cap_synthetic_attempts(self):
        self.o.prewarm_reason = Mock(return_value=None)
        self.o.perform_prewarm = Mock(
            side_effect=WarmupDeferred(
                'No idle capacity for local warm-up.', code='warmup-capacity'
            )
        )
        self.assertFalse(self.verify(timeout=80))
        self.assertEqual(self.o.perform_prewarm.call_count, 3)
        self.assertGreaterEqual(self.fixture.now - self.started, 80)
        self.assertLessEqual(self.fixture.now - self.started, 82)
        self.assert_no_recovery_commands()

    def test_paid_output_can_still_complete_after_synthetic_attempt_limit(self):
        self.o.prewarm_reason = Mock(return_value=None)
        self.o.perform_prewarm = Mock(
            side_effect=WarmupDeferred(
                'No idle capacity for local warm-up.', code='warmup-capacity'
            )
        )

        def observed():
            if self.fixture.now - self.started >= 60:
                self.raw['stats'] = {'requests_served': 1, 'tokens_generated': 10}
                self.raw['inference_active'] = True
            return {**self.raw, 'written_at': self.fixture.now}

        self.o.read_state.side_effect = observed
        self.assertTrue(self.verify(timeout=80))
        self.assertEqual(self.o.perform_prewarm.call_count, 3)
        self.assertEqual(self.fixture.now - self.started, 60)
        self.assertEqual(self.o.warmup['status'], 'ready')
        self.assert_no_recovery_commands()

    def test_busy_after_deferral_observes_until_deadline_without_resending(self):
        def transient(*args, **kwargs):
            self.raw['inference_active'] = True
            raise WarmupDeferred('Paid work arrived.', code='work-arrived')

        self.o.prewarm_reason = Mock(
            side_effect=lambda *args, **kwargs: (
                'Waiting for idle capacity.' if self.raw['inference_active'] else None
            )
        )
        self.o.perform_prewarm = Mock(side_effect=transient)
        self.assertFalse(self.verify(timeout=40))
        self.assertEqual(self.o.perform_prewarm.call_count, 1)
        self.assertEqual(self.fixture.now - self.started, 40)
        self.assert_no_recovery_commands()

    def test_capacity_rejection_then_paid_decode_completes_without_another_request(self):
        self.o.prewarm_reason = Mock(return_value=None)

        def saturated(*args, **kwargs):
            self.raw['inference_active'] = True
            self.raw['stats'] = {'requests_served': 1, 'tokens_generated': 10}
            raise WarmupDeferred('Local capacity is busy.', code='warmup-capacity')

        with patch('optimizer.prewarm', side_effect=saturated) as synthetic:
            self.assertTrue(self.verify(timeout=40))
        synthetic.assert_called_once()
        self.assertEqual(self.o.warmup['status'], 'ready')
        self.assert_no_recovery_commands()

    def test_authentication_or_malformed_response_errors_are_terminal(self):
        self.o.prewarm_reason = Mock(return_value=None)
        self.o.perform_prewarm = Mock(
            side_effect=WarmupError('The local model warm-up was rejected.')
        )
        self.assertFalse(self.verify(timeout=80))
        self.o.perform_prewarm.assert_called_once()
        self.assertLess(self.fixture.now - self.started, 80)
        self.assert_no_recovery_commands()

    def test_explicit_stop_while_observing_deferral_is_preserved(self):
        def stop_provider(*args, **kwargs):
            self.o.service_disabled.return_value = True
            raise WarmupDeferred('Paid work arrived.', code='work-arrived')

        self.o.prewarm_reason = Mock(return_value=None)
        self.o.perform_prewarm = Mock(side_effect=stop_provider)
        with self.assertRaises(ExternalChange):
            self.verify(timeout=80)
        self.o.perform_prewarm.assert_called_once()
        self.assert_no_recovery_commands()

    def test_launch_change_after_deferral_cannot_be_accepted_as_success(self):
        def change_launch(*args, **kwargs):
            self.o.read_options.return_value = ('a', ['--local-endpoint', '--port', '9000'], {})
            self.raw['stats'] = {'requests_served': 1, 'tokens_generated': 10}
            raise WarmupDeferred('Paid work arrived.', code='work-arrived')

        self.o.prewarm_reason = Mock(return_value=None)
        self.o.perform_prewarm = Mock(side_effect=change_launch)
        with self.assertRaises(ExternalChange):
            self.verify(timeout=80, expected_launch=launch_signature(['--local-endpoint'], {}))
        self.o.perform_prewarm.assert_called_once()
        self.assertNotEqual(self.o.warmup.get('status'), 'ready')
        self.assert_no_recovery_commands()

    def test_another_target_process_after_deferral_is_an_external_change(self):
        def external_restart(*args, **kwargs):
            self.raw.update(
                pid=124,
                started_at=self.raw['started_at'] + 1,
                stats={'requests_served': 1, 'tokens_generated': 10},
            )
            # Even a fresh collector and identity match for the replacement
            # process cannot make it the process this switch was verifying.
            self.o.raw = copy.deepcopy(self.raw)
            self.o.identity_session = (self.raw['started_at'], self.raw['pid'])
            self.o.identity_at = self.fixture.now
            raise WarmupDeferred('Paid work arrived.', code='work-arrived')

        self.o.prewarm_reason = Mock(return_value=None)
        self.o.perform_prewarm = Mock(side_effect=external_restart)
        with self.assertRaises(ExternalChange):
            self.verify(timeout=80)
        self.o.perform_prewarm.assert_called_once()
        self.assertNotEqual(self.o.warmup.get('status'), 'ready')
        self.assert_no_recovery_commands()

    def test_pair_aggregate_output_never_proves_both_models_ready(self):
        target = selection_key(['a', 'b'])
        self.raw.update(
            advertised_models=['a', 'b'],
            warm_models=['a', 'b'],
            current_model='a',
            inference_active=True,
            stats={'requests_served': 12, 'tokens_generated': 100},
        )
        self.o.raw = copy.deepcopy(self.raw)
        self.o.read_options.return_value = (target, ['--local-endpoint'], {})
        self.o.prewarm_reason = Mock(return_value='Waiting for idle capacity.')
        self.o.perform_prewarm = Mock()
        self.assertFalse(self.o.verify_started(target, self.previous_session, timeout=20))
        self.o.perform_prewarm.assert_not_called()
        self.assertNotEqual(self.o.warmup.get('status'), 'ready')
        self.assert_no_recovery_commands()

    def test_final_activity_race_preserves_deferred_type_and_waiting_status(self):
        observed = copy.deepcopy(self.raw)
        self.raw['inference_active'] = True
        self.o.prewarm_reason = Mock(return_value='Waiting for idle capacity before pre-warming.')
        with patch('optimizer.prewarm') as synthetic:
            with self.assertRaises(WarmupDeferred) as caught:
                self.o.perform_prewarm('a', observed, ['--local-endpoint'])
        self.assertEqual(caught.exception.code, 'work-arrived')
        self.assertEqual(self.o.warmup['status'], 'waiting')
        synthetic.assert_not_called()
        self.assert_no_recovery_commands()

    def test_counter_advance_without_active_flag_also_defers_synthetic_request(self):
        observed = copy.deepcopy(self.raw)
        # Incomplete output is activity, but not enough to prove readiness.
        self.raw['stats'] = {'requests_served': 0, 'tokens_generated': 1}
        self.o.prewarm_reason = Mock(return_value=None)
        with patch('optimizer.prewarm') as synthetic:
            with self.assertRaises(WarmupDeferred) as caught:
                self.o.perform_prewarm('a', observed, ['--local-endpoint'])
        self.assertEqual(caught.exception.code, 'work-arrived')
        self.assertEqual(self.o.warmup['status'], 'waiting')
        synthetic.assert_not_called()
        self.assert_no_recovery_commands()


class EndpointDeferredErrorTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.EndpointTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def test_capacity_responses_are_deferred_without_exposing_upstream_text(self):
        for code in (429, 503):
            with self.subTest(code=code):
                client = Mock()
                client.open.side_effect = HTTPError(
                    'http://private', code, 'secret detail', {}, None
                )
                with self.assertRaises(WarmupDeferred) as caught:
                    prewarm(
                        self.fixture.home,
                        self.fixture.raw,
                        self.fixture.options,
                        'a',
                        opener=client,
                    )
                self.assertEqual(caught.exception.code, 'warmup-capacity')
                self.assertNotIn('secret', str(caught.exception))
                self.assertNotIn('private', str(caught.exception))

    def test_authentication_responses_remain_hard_failures(self):
        for code in (401, 403):
            with self.subTest(code=code):
                client = Mock()
                client.open.side_effect = HTTPError(
                    'http://private', code, 'secret detail', {}, None
                )
                with self.assertRaises(WarmupError) as caught:
                    prewarm(
                        self.fixture.home,
                        self.fixture.raw,
                        self.fixture.options,
                        'a',
                        opener=client,
                    )
                self.assertNotIsInstance(caught.exception, WarmupDeferred)
                client.open.assert_called_once()


if __name__ == '__main__':
    unittest.main()
