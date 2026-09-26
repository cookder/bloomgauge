"""Five-minute manual-switch fallback tests; every provider command is mocked."""

import copy
import unittest
from unittest.mock import Mock, patch

import test_optimizer as fixtures
from optimizer import manual_pause_at, manual_pause_due, launch_signature, session_key


class ManualTimeoutTests(unittest.TestCase):
    setUp = fixtures.ControllerTests.setUp
    tearDown = fixtures.ControllerTests.tearDown
    manual_payload = fixtures.ControllerTests.manual_payload

    def queue(self, age=300):
        payload = self.manual_payload()
        self.o.manual_action(payload, 'phone')
        self.o.state['requestedAt'] = self.now - age
        self.o.raw['inference_active'] = True
        self.o.read_state.return_value = copy.deepcopy(self.o.raw)
        self.o.idle_since = None
        return payload

    def arm(self, after=True):
        _, options, environment = self.o.read_options()
        self.o.state['pending'] = {
            'model': 'b',
            'previous': 'a',
            'kind': self.o.state.get('requestedKind'),
            'requestId': self.o.state.get('requestId'),
            'requestedAt': self.o.state.get('requestedAt'),
            'afterIdleTimeout': after,
            'session': session_key(self.o.raw),
            'launchSignature': launch_signature(options, environment),
        }
        self.o.command = Mock()
        self.o.verify_started = Mock(return_value=True)

    def run_switch(self):
        with patch('optimizer.time.time', return_value=self.now):
            self.o.switch('a', 'b', 'acct', self.live['device'])

    def test_always_busy_waits_at_299_seconds_and_dispatches_at_300(self):
        self.queue(299)
        self.o.switch = Mock()
        self.o.tick(self.now)
        self.o.switch.assert_not_called()
        self.assertNotIn('pending', self.o.state)
        self.o.state['requestedAt'] = self.now - 300
        self.o.tick(self.now)
        self.o.worker.join(timeout=2)
        self.o.switch.assert_called_once_with('a', 'b', 'acct', self.live['device'])
        self.assertTrue(self.o.state['pending']['afterIdleTimeout'])
        self.assertIsNone(self.o.idle_since)
        self.assertEqual(
            [
                c.args[0]
                for c in self.o.runner.call_args_list
                if c.args[0] != ['/usr/bin/sudo', '-n', '-ll', '/usr/sbin/purge']
            ],
            [],
        )

    def test_busy_final_recheck_dispatches_after_deadline_and_verifies_prewarm(self):
        payload = self.queue(300)
        self.arm()
        self.o.read_state.return_value['stats'] = {'requests_served': 11, 'tokens_generated': 30}
        self.run_switch()
        self.o.command.assert_called_once_with('b', ['--local-endpoint', '--port', '8000'], {})
        from optimizer import launch_signature

        self.o.verify_started.assert_called_once_with(
            'b',
            self.raw['started_at'],
            360,
            launch_signature(['--local-endpoint', '--port', '8000'], {}),
        )
        self.assertEqual(self.o.state['manualResult']['id'], payload['requestId'])
        self.assertEqual(self.o.state['manualResult']['status'], 'completed')
        event = self.h.db.execute(
            "SELECT detail FROM opt_events WHERE kind='switching'"
        ).fetchone()[0]
        self.assertIn('five-minute manual idle timeout', event)
        self.assertIn('requests may be interrupted', event)
        self.assertEqual(
            [
                c.args[0]
                for c in self.o.runner.call_args_list
                if c.args[0] != ['/usr/bin/sudo', '-n', '-ll', '/usr/sbin/purge']
            ],
            [],
        )

    def test_deadline_flag_does_not_bypass_actual_age(self):
        payload = self.queue(299)
        self.arm(after=True)
        self.run_switch()
        self.o.command.assert_not_called()
        self.o.verify_started.assert_not_called()
        self.assertEqual(self.o.state['requestedModel'], 'b')
        self.assertEqual(self.o.state['requestId'], payload['requestId'])
        self.assertEqual(self.o.state['manualResult']['status'], 'queued')

    def test_busy_recheck_without_deadline_flag_keeps_request_queued(self):
        self.queue(301)
        self.arm(after=False)
        self.run_switch()
        self.o.command.assert_not_called()
        self.assertEqual(self.o.state['manualResult']['status'], 'queued')

    def test_deadline_applies_only_to_deliberate_manual_queue(self):
        self.queue(3600)
        original = copy.deepcopy(self.o.state)
        self.o.switch = Mock()
        for mode, kind in [
            ('week', None),
            ('optimize', None),
            ('observe', 'restore'),
            ('observe', 'manual-recovery'),
            ('week', 'manual'),
            ('optimize', 'manual'),
        ]:
            with self.subTest(mode=mode, kind=kind):
                self.o.state = copy.deepcopy(original)
                self.o.state.update(mode=mode, requestedKind=kind)
                self.assertIsNone(manual_pause_at(self.o.state))
                self.o.tick(self.now)
                self.o.switch.assert_not_called()
                self.assertNotIn('pending', self.o.state)
        self.assertEqual(
            [
                c.args[0]
                for c in self.o.runner.call_args_list
                if c.args[0] != ['/usr/bin/sudo', '-n', '-ll', '/usr/sbin/purge']
            ],
            [],
        )

    def test_invalid_missing_or_future_queue_time_never_forces(self):
        self.queue()
        original = copy.deepcopy(self.o.state)
        self.o.switch = Mock()
        for value in [None, False, 0, -1, float('nan'), float('inf'), 'old', self.now + 1]:
            with self.subTest(value=value):
                self.o.state = copy.deepcopy(original)
                self.o.state['requestedAt'] = value
                self.assertFalse(manual_pause_due(self.o.state, self.now))
                self.o.tick(self.now)
                self.o.switch.assert_not_called()
                self.assertNotIn('pending', self.o.state)
        self.o.state = copy.deepcopy(original)
        self.o.state.pop('requestedAt')
        self.assertFalse(manual_pause_due(self.o.state, self.now))

    def test_cancel_and_new_selection_start_a_new_five_minute_clock(self):
        old = self.queue(350)
        self.o.manual_action({'action': 'cancel', 'requestId': old['requestId']})
        self.assertFalse(manual_pause_due(self.o.state, self.now))
        with patch('optimizer.time.time', return_value=self.now):
            new = self.manual_payload()
            self.o.manual_action(new, 'phone')
        self.assertNotEqual(new['requestId'], old['requestId'])
        self.assertEqual(manual_pause_at(self.o.state), self.now + 300)
        self.o.switch = Mock()
        self.o.tick(self.now)
        self.o.switch.assert_not_called()
        self.assertNotIn('pending', self.o.state)

    def test_expired_manual_queue_does_not_bypass_tick_safety_gates(self):
        self.queue()
        settings = copy.deepcopy(self.o.state)
        self.o.switch = Mock()
        for guard in [
            'offline',
            'stale',
            'identity',
            'hardware',
            'battery',
            'heat',
            'memory',
            'catalog',
        ]:
            with self.subTest(guard=guard):
                self.o.state = copy.deepcopy(settings)
                self.o.live = copy.deepcopy(self.live)
                self.o.identity_ok = True
                self.o.on_ac_power.return_value = True
                self.o.discovery_error = None
                if guard == 'offline':
                    self.o.live['provider']['online'] = False
                elif guard == 'stale':
                    self.o.live['at'] = self.now - 30
                elif guard == 'identity':
                    self.o.identity_ok = False
                elif guard == 'hardware':
                    self.o.live['hardware'].pop('cpuTemp')
                elif guard == 'battery':
                    self.o.on_ac_power.return_value = False
                elif guard == 'heat':
                    self.o.live['hardware']['thermal'] = 'Critical'
                elif guard == 'memory':
                    self.o.live['hardware']['memoryAvailableGB'] = 0
                else:
                    self.o.discovery_error = 'Unavailable'
                self.o.tick(self.now)
                self.o.switch.assert_not_called()
                self.assertNotIn('pending', self.o.state)
                self.assertEqual(self.o.state['requestedModel'], 'b')
        self.assertEqual(
            [
                c.args[0]
                for c in self.o.runner.call_args_list
                if c.args[0] != ['/usr/bin/sudo', '-n', '-ll', '/usr/sbin/purge']
            ],
            [],
        )

    def test_final_changed_account_session_launch_or_reset_still_blocks(self):
        self.queue()
        settings = copy.deepcopy(self.o.state)
        for guard in [
            'account',
            'session',
            'options',
            'environment',
            'counter-reset',
            'invalid-counter',
            'invalid-activity',
            'stale-daemon',
            'stopped',
        ]:
            with self.subTest(guard=guard):
                self.o.state = copy.deepcopy(settings)
                self.o.live = copy.deepcopy(self.live)
                self.o.read_state.return_value = copy.deepcopy(self.o.raw)
                self.o.read_options.return_value = ('a', ['--local-endpoint', '--port', '8000'], {})
                self.o.service_disabled.return_value = False
                self.arm()
                if guard == 'account':
                    self.o.live['account'] = 'different-account'
                elif guard == 'session':
                    self.o.read_state.return_value['pid'] = 2
                elif guard == 'options':
                    self.o.read_options.return_value = ('a', ['--port', '8001'], {})
                elif guard == 'environment':
                    self.o.read_options.return_value = (
                        'a',
                        ['--local-endpoint', '--port', '8000'],
                        {'EXAMPLE': 'different'},
                    )
                elif guard == 'counter-reset':
                    self.o.read_state.return_value['stats'] = {
                        'requests_served': 0,
                        'tokens_generated': 0,
                    }
                elif guard == 'invalid-counter':
                    self.o.read_state.return_value['stats']['tokens_generated'] = float('nan')
                elif guard == 'invalid-activity':
                    self.o.read_state.return_value.pop('inference_active')
                elif guard == 'stale-daemon':
                    self.o.read_state.return_value['written_at'] = self.now - 20
                else:
                    self.o.service_disabled.return_value = True
                self.run_switch()
                self.o.command.assert_not_called()
                self.o.verify_started.assert_not_called()
                self.assertEqual(self.o.state['manualResult']['status'], 'failed')
        self.assertEqual(
            [
                c.args[0]
                for c in self.o.runner.call_args_list
                if c.args[0] != ['/usr/bin/sudo', '-n', '-ll', '/usr/sbin/purge']
            ],
            [],
        )

    def test_final_hardware_power_memory_and_online_guards_still_block(self):
        self.queue()
        settings = copy.deepcopy(self.o.state)
        for guard in ['hardware', 'battery', 'heat', 'memory', 'offline', 'stale-live', 'identity']:
            with self.subTest(guard=guard):
                self.o.state = copy.deepcopy(settings)
                self.o.live = copy.deepcopy(self.live)
                self.o.on_ac_power.return_value = True
                self.o.identity_ok = True
                self.arm()
                if guard == 'hardware':
                    self.o.live['hardware']['gpuTemp'] = None
                elif guard == 'battery':
                    self.o.on_ac_power.return_value = False
                elif guard == 'heat':
                    self.o.live['hardware']['gpuTemp'] = 99
                elif guard == 'memory':
                    self.o.live['hardware']['memoryAvailableGB'] = 0
                elif guard == 'offline':
                    self.o.live['provider']['online'] = False
                elif guard == 'stale-live':
                    self.o.live['at'] = self.now - 30
                else:
                    self.o.identity_ok = False
                self.run_switch()
                self.o.command.assert_not_called()
                self.o.verify_started.assert_not_called()
                self.assertEqual(self.o.state['manualResult']['status'], 'failed')
        self.assertEqual(
            [
                c.args[0]
                for c in self.o.runner.call_args_list
                if c.args[0] != ['/usr/bin/sudo', '-n', '-ll', '/usr/sbin/purge']
            ],
            [],
        )

    def test_stale_pending_request_id_or_clock_cannot_force(self):
        self.queue()
        settings = copy.deepcopy(self.o.state)
        for change in ['id', 'clock', 'kind', 'model']:
            with self.subTest(change=change):
                self.o.state = copy.deepcopy(settings)
                self.arm()
                if change == 'id':
                    self.o.state['pending']['requestId'] = 'another-request'
                elif change == 'clock':
                    self.o.state['pending']['requestedAt'] = self.now - 900
                elif change == 'kind':
                    self.o.state['pending']['kind'] = 'restore'
                else:
                    self.o.state['pending']['model'] = 'different-model'
                self.run_switch()
                self.o.command.assert_not_called()
                self.assertEqual(self.o.state['manualResult']['status'], 'queued')

    def test_timed_pause_does_not_force_a_busy_failed_switch_recovery(self):
        self.queue()
        self.arm()
        self.o.verify_started.return_value = False
        self.o.recovery_ready.return_value = None
        self.run_switch()
        self.o.command.assert_called_once_with('b', ['--local-endpoint', '--port', '8000'], {})
        self.o.recovery_ready.assert_called_once()
        self.assertEqual(self.o.state['manualResult']['status'], 'failed')
        self.assertEqual(self.o.state['mode'], 'observe')
        self.assertIsNone(self.o.state['requestedModel'])
        self.assertEqual(
            [
                c.args[0]
                for c in self.o.runner.call_args_list
                if c.args[0] != ['/usr/bin/sudo', '-n', '-ll', '/usr/sbin/purge']
            ],
            [],
        )


if __name__ == '__main__':
    unittest.main()
