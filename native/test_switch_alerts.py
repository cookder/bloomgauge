import copy
import unittest
from unittest.mock import Mock
from history import History
from model_readiness import session_key
from switch_alerts import SwitchAlerts, switch_reason
import test_demand_controller


class SwitchAlertTests(unittest.TestCase):
    def setUp(self):
        self.h = History(':memory:')
        self.alerts = SwitchAlerts(self.h)
        self.raw = {
            'pid': 1,
            'started_at': 100,
            'advertised_models': ['a'],
            'attestation_public_key': 'test',
        }

    def tearDown(self):
        self.h.db.close()

    def rows(self):
        return [dict(r) for r in self.h.db.execute('SELECT * FROM model_switch_alerts')]

    def observe(self, raw=None, counting=True, busy=False, account='owner', device='mac'):
        self.alerts.observe(account, device, raw or self.raw, {'counting': counting}, busy, 200)

    def test_first_observation_restart_and_same_model_are_quiet(self):
        self.observe()
        self.alerts = SwitchAlerts(self.h)
        self.observe()
        self.observe({**self.raw, 'pid': 2, 'started_at': 180})
        self.assertEqual(self.rows(), [])

    def test_cold_and_queued_change_wait_for_ready_then_dedupe(self):
        self.observe()
        new = {**self.raw, 'pid': 2, 'advertised_models': ['b']}
        self.observe(new, counting=False)
        self.observe(new, busy=True)
        self.assertEqual(self.rows(), [])
        self.observe(new)
        self.observe(new)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]['reason'], 'external')
        self.assertEqual(self.rows()[0]['previous'], 'a')

    def test_controller_reason_wins_over_observer_and_survives_restart(self):
        self.observe()
        new = {**self.raw, 'pid': 2, 'advertised_models': ['b']}
        self.alerts.record('owner', 'mac', session_key(new), 'a', 'b', 'paid_trial', 190)
        self.alerts = SwitchAlerts(self.h)
        self.observe(new)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]['reason'], 'paid_trial')

    def test_scope_and_combination_changes(self):
        self.observe()
        self.observe({**self.raw, 'advertised_models': ['b']}, account='other')
        self.observe({**self.raw, 'advertised_models': ['b']}, device='other')
        self.assertEqual(self.rows(), [])
        self.observe({**self.raw, 'pid': 2, 'advertised_models': ['a', 'b']})
        self.assertEqual(len(self.rows()), 1)

    def test_pending_only_queues_recent_owned_events_and_retries_pressure(self):
        self.alerts.record('owner', 'mac', 'one', 'a', 'b', 'manual', 100)
        self.alerts.record('other', 'mac', 'two', 'a', 'b', 'manual', 100)
        push = Mock()
        push.enqueue_switch.return_value = False
        push.has_event.return_value = False
        self.alerts.send_pending('owner', 'mac', push, 200)
        self.assertEqual(push.enqueue_switch.call_count, 1)
        self.assertEqual(self.rows()[0]['queued'], 0)
        push.enqueue_switch.return_value = True
        self.alerts.send_pending('owner', 'mac', push, 200)
        self.assertEqual(self.rows()[0]['queued'], 1)
        self.alerts.send_pending('owner', 'mac', push, 200)
        self.assertEqual(push.enqueue_switch.call_count, 2)
        self.alerts.record('owner', 'mac', 'three', 'b', 'c', 'manual', 100)
        self.alerts.send_pending('owner', 'mac', push, 1100)
        self.assertEqual(push.enqueue_switch.call_count, 2)

    def test_already_accepted_event_is_not_queued_twice(self):
        self.alerts.record('owner', 'mac', 'one', 'a', 'b', 'manual', 100)
        push = Mock()
        push.enqueue_switch.return_value = False
        push.has_event.return_value = True
        self.alerts.send_pending('owner', 'mac', push, 200)
        self.assertEqual(self.rows()[0]['queued'], 1)

    def test_justifications_do_not_promote_trials_to_proven_upgrades(self):
        for trigger, expected in [
            ('earnings_target', 'paid_trial'),
            ('idle', 'idle_trial'),
            ('demand_spike', 'spike_trial'),
            ('failed_trial', 'failed_trial'),
            ('spike_return', 'trial_return'),
        ]:
            self.assertEqual(
                switch_reason(
                    'demand', decision={'kind': 'explore', 'explorationTrigger': trigger}
                ),
                expected,
            )
        self.assertEqual(
            switch_reason(
                'demand', decision={'kind': 'earnings', 'explorationTrigger': 'earnings_target'}
            ),
            'paid_upgrade',
        )
        self.assertEqual(
            switch_reason(
                'demand',
                decision={'kind': 'explore', 'preferredReturn': {'selection': 'preferred_return'}},
            ),
            'preferred_return',
        )
        self.assertEqual(
            switch_reason(
                'demand', decision={'kind': 'explore', 'fallback': {'selection': 'fallback'}}
            ),
            'fallback',
        )
        self.assertEqual(switch_reason('manual'), 'manual')
        self.assertEqual(switch_reason('manual-recovery'), 'recovery')
        for mode in ('week', 'combo'):
            self.assertEqual(switch_reason('automatic', mode), 'scheduled')


class ControllerNotificationTests(unittest.TestCase):
    setUp = test_demand_controller.DemandControllerTests.setUp
    tearDown = test_demand_controller.DemandControllerTests.tearDown
    setup_demand = test_demand_controller.DemandControllerTests.setup_demand

    def test_notification_only_after_actual_successful_warm_verification(self):
        self.setup_demand()
        self.o.state['pending'] = {'model': 'b', 'kind': 'demand'}
        self.o.command = Mock()

        def verified(*args):
            self.assertEqual(
                self.h.db.execute('SELECT COUNT(*) FROM model_switch_alerts').fetchone()[0], 0
            )
            self.o.warmup = {'session': 'new-provider-session', 'model': 'b', 'status': 'ready'}
            return True

        self.o.verify_started = Mock(side_effect=verified)
        self.o.switch('a', 'b', 'acct', self.live['device'])
        rows = list(self.h.db.execute('SELECT * FROM model_switch_alerts'))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['model'], 'b')

    def test_failed_target_and_successful_return_do_not_claim_target_switched(self):
        self.setup_demand()
        self.o.state['pending'] = {'model': 'b', 'kind': 'demand'}
        self.o.command = Mock()
        self.o.verify_started = Mock(side_effect=[False, True])
        self.o.switch('a', 'b', 'acct', self.live['device'])
        self.assertEqual(
            self.h.db.execute('SELECT COUNT(*) FROM model_switch_alerts').fetchone()[0], 0
        )

    def test_notification_failure_cannot_change_successful_switch(self):
        self.setup_demand()
        self.o.state['pending'] = {'model': 'b', 'kind': 'demand'}
        self.o.command = Mock()
        self.o.verify_started = Mock(return_value=True)
        self.o.switch_alerts.record = Mock(side_effect=OSError('disk'))
        self.o.switch('a', 'b', 'acct', self.live['device'])
        self.assertEqual(self.o.state['expectedModel'], 'b')
        self.assertEqual(self.o.state['mode'], 'demand')


if __name__ == '__main__':
    unittest.main()
