"""Switch outcomes using isolated provider snapshots; never invoke a real CLI."""

import copy
import json
import subprocess
import unittest
from unittest.mock import Mock, patch

from optimizer import Optimizer, session_key
from prewarm import WarmupError
import test_optimizer as fixtures


class SwitchFailureDetailsTests(unittest.TestCase):
    tearDown = fixtures.ControllerTests.tearDown

    def setUp(self):
        fixtures.ControllerTests.setUp(self)
        self.options = ['--local-endpoint', '--port', '8000']
        self.o.state['pending'] = {'kind': 'automatic', 'automaticMode': 'week', 'model': 'b'}
        self.o.recovery_ready.return_value = None
        self.o.command = Mock(side_effect=self.start_target)
        self.o.read_state.side_effect = lambda: {
            **copy.deepcopy(self.o.raw),
            'written_at': self.now,
        }
        clock = patch('optimizer.time.time', side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        purge = patch('optimizer.clear_file_cache')
        self.purge = purge.start()
        self.addCleanup(purge.stop)

    def start_target(self, target, options, environment):
        self.o.raw = {
            **copy.deepcopy(self.raw),
            'pid': 2,
            'started_at': self.now - 1,
            'advertised_models': [target],
            'current_model': target,
            'warm_models': [],
            'inference_active': False,
            'stats': {'requests_served': 0, 'tokens_generated': 0},
        }
        self.o.read_options.return_value = (target, list(options), dict(environment))
        self.o.identity_session = (self.o.raw['started_at'], self.o.raw['pid'])
        self.o.identity_at = self.now

    def serving(self):
        self.o.raw.update(
            warm_models=['b'],
            inference_active=True,
            stats={'requests_served': 2, 'tokens_generated': 32},
        )

    def verification_failure(self, code, message='The selected model did not become ready.'):
        def fail(*args):
            self.o.verification_session = session_key(self.o.raw)
            self.o.verification_failure = WarmupError(message, code=code)
            return False

        self.o.verify_started = Mock(side_effect=fail)

    def switch(self):
        self.o.switch('a', 'b', 'acct', self.live['device'])

    def events(self):
        return [dict(row) for row in self.h.db.execute('SELECT * FROM opt_events ORDER BY id')]

    def assert_no_recovery_restart(self):
        self.assertEqual([call.args[0] for call in self.o.command.call_args_list], ['b'])
        self.purge.assert_not_called()
        # The read-only permission listing is allowed; running purge is not.
        self.assertFalse(
            any(
                c.args[0] == ['/usr/bin/sudo', '-n', '/usr/sbin/purge']
                for c in self.o.runner.call_args_list
            )
        )

    def test_specific_warmup_failure_survives_blocked_recovery_and_is_persisted(self):
        self.verification_failure('endpoint-authentication', 'The local warm-up was rejected.')
        self.switch()
        failure = self.o.state['lastSwitchFailure']
        self.assertEqual(failure['code'], 'endpoint-authentication')
        self.assertEqual(failure['stage'], 'verify')
        self.assertEqual(failure['recovery'], 'blocked')
        self.assertEqual(failure['recoveryCode'], 'idle-not-verified')
        self.assertIn('The local warm-up was rejected.', self.o.detail)
        self.assertEqual(self.o.state['mode'], 'observe')
        self.assertEqual(self.h.cache('optimizer-settings')['lastSwitchFailure'], failure)
        self.assertEqual(self.events()[-1]['kind'], 'failed')
        self.assert_no_recovery_restart()

    def test_startup_process_error_is_redacted_and_not_called_warmup_failure(self):
        private = '/Users/private/DO-NOT-EXPORT?token=secret'
        self.o.command.side_effect = subprocess.CalledProcessError(
            2, [private], output=private, stderr=private
        )
        self.o.verify_started = Mock()
        self.switch()
        self.assertEqual(self.o.state['lastSwitchFailure']['code'], 'startup-command')
        self.assertEqual(self.o.state['lastSwitchFailure']['stage'], 'start')
        self.assertIn('start command failed', self.o.detail)
        self.assertNotIn(private, json.dumps([self.events(), self.o.state, self.o.detail]))
        self.o.verify_started.assert_not_called()
        self.assert_no_recovery_restart()

    def test_startup_timeout_is_separate_and_never_uses_late_warmup_success(self):
        self.o.command.side_effect = subprocess.TimeoutExpired('/Users/private/DO-NOT-EXPORT', 60)
        self.o.target_serving_after_wait = Mock(return_value=True)
        self.switch()
        self.assertEqual(self.o.state['lastSwitchFailure']['code'], 'startup-timeout')
        self.assertIn('start command timed out', self.o.detail)
        self.o.target_serving_after_wait.assert_not_called()
        self.assertEqual(self.o.state['mode'], 'observe')
        self.assertNotIn('DO-NOT-EXPORT', json.dumps([self.events(), self.o.state, self.o.detail]))
        self.assert_no_recovery_restart()

    def test_late_serving_output_completes_switch_without_rollback_or_pausing(self):
        def verify(*args):
            self.o.verification_session = session_key(self.o.raw)
            self.o.verification_failure = WarmupError('Deadline elapsed.', code='readiness-timeout')
            self.serving()
            return False

        self.o.verify_started = Mock(side_effect=verify)
        self.switch()
        self.assertEqual(self.o.state['mode'], 'week')
        self.assertEqual(self.o.state['expectedModel'], 'b')
        self.assertEqual(self.o.state['lastSwitchResult']['outcome'], 'switched')
        self.assertNotIn('lastSwitchFailure', self.o.state)
        self.assertEqual(self.o.warmup['status'], 'ready')
        self.assertEqual(self.events()[-1]['kind'], 'switched')
        self.o.recovery_ready.assert_not_called()
        self.assert_no_recovery_restart()

    def test_serving_output_arriving_during_recovery_wait_avoids_rollback(self):
        self.verification_failure('warmup-capacity')

        def wait_for_idle(*args):
            self.serving()
            return None

        self.o.recovery_ready.side_effect = wait_for_idle
        self.switch()
        self.o.recovery_ready.assert_called_once()
        self.assertEqual(self.o.state['mode'], 'week')
        self.assertEqual(self.o.state['lastSwitchResult']['outcome'], 'switched')
        self.assertNotIn('lastSwitchFailure', self.o.state)
        self.assertIn('no recovery restart was needed', self.o.detail)
        self.assert_no_recovery_restart()

    def test_cold_busy_target_exhausts_real_idle_recovery_without_purge(self):
        self.verification_failure('readiness-timeout')
        start_target = self.o.command.side_effect

        def start_busy(*args):
            start_target(*args)
            self.o.raw['inference_active'] = True

        self.o.command.side_effect = start_busy
        self.o.recovery_ready = Mock(wraps=Optimizer.recovery_ready.__get__(self.o, Optimizer))

        def wait(seconds):
            self.now += seconds
            return False

        with patch.object(self.o.stop, 'wait', side_effect=wait):
            self.switch()
        self.assertEqual(self.o.state['mode'], 'observe')
        self.assertEqual(self.o.state['lastSwitchFailure']['code'], 'readiness-timeout')
        self.assertEqual(self.o.state['lastSwitchFailure']['recoveryCode'], 'idle-not-verified')
        self.assertGreaterEqual(self.o.state['lastSwitchFailure']['elapsedSeconds'], 60)
        self.assertEqual(self.events()[-1]['kind'], 'failed')
        self.assert_no_recovery_restart()

    def test_changed_launch_options_cannot_be_accepted_as_late_success(self):
        def verify(*args):
            self.o.verification_session = session_key(self.o.raw)
            self.o.verification_failure = WarmupError('Deadline elapsed.', code='readiness-timeout')
            self.serving()
            self.o.read_options.return_value = ('b', ['--local-endpoint', '--port', '9000'], {})
            return False

        self.o.verify_started = Mock(side_effect=verify)
        self.switch()
        self.assertEqual(self.o.state['mode'], 'observe')
        self.assertEqual(self.o.state['lastSwitchResult']['outcome'], 'failed')
        self.assert_no_recovery_restart()

    def test_second_provider_session_cannot_be_accepted_as_late_success(self):
        def verify(*args):
            self.o.verification_session = session_key(self.o.raw)
            self.o.verification_failure = WarmupError('Deadline elapsed.', code='readiness-timeout')
            self.serving()
            self.o.raw.update(pid=3, started_at=self.now)
            self.o.identity_session = (self.o.raw['started_at'], self.o.raw['pid'])
            return False

        self.o.verify_started = Mock(side_effect=verify)
        self.switch()
        self.assertEqual(self.o.state['mode'], 'observe')
        self.assertEqual(self.o.state['lastSwitchResult']['outcome'], 'failed')
        self.assert_no_recovery_restart()


if __name__ == '__main__':
    unittest.main()
