"""Live policy tuning must retain experiments and reject obsolete control writes."""

import copy
import threading
import unittest
from unittest.mock import Mock

from demand_optimizer import policy
from optimizer import Optimizer
import test_optimizer


class PolicyUpdateTests(unittest.TestCase):
    tearDown = test_optimizer.ControllerTests.tearDown

    def setUp(self):
        test_optimizer.ControllerTests.setUp(self)
        self.o.state.update(
            mode='demand',
            demandPolicy=policy({'minimumNetUsd': 0.05}),
            originalModel='original',
            comboPlan={'status': 'queued'},
            demandProposal={'target': 'b', 'seconds': 180},
        )
        self.o.save()

    def request(self, changes=None, **extra):
        return {
            'action': 'update-policy',
            'expectedControl': self.o.control_version(),
            'demandPolicy': changes if changes is not None else {'maxSwitchesPerDay': 24},
            **extra,
        }

    def test_mac_and_phone_retain_plan_history_and_current_provider(self):
        for source in ('mac', 'phone'):
            with self.subTest(source=source):
                changes = {'maxSwitchesPerDay': 24 if source == 'mac' else 12}
                before = copy.deepcopy(self.o.state)
                raw = copy.deepcopy(self.o.raw)
                body = self.request(changes)
                self.o.control_action(body, source)
                for key, value in before.items():
                    if key not in ('demandPolicy', 'demandProposal'):
                        self.assertEqual(self.o.state.get(key), value, key)
                self.assertEqual(self.o.raw, raw)
                self.assertEqual(
                    self.o.state['demandPolicy'], {**before['demandPolicy'], **changes}
                )
                self.assertNotIn('demandProposal', self.o.state)
                self.assertEqual(self.o.state['demandPolicy']['minimumNetUsd'], 0.05)
                self.o.runner.assert_not_called()
                with self.assertRaises(ValueError):
                    self.o.control_action(body, source)
        restored = Optimizer(self.h, self.net, self.tmp.name, threading.Event(), Mock())
        self.assertEqual(restored.state['startedAt'], self.o.state['startedAt'])
        self.assertEqual(restored.state['originalModel'], 'original')
        self.assertEqual(restored.state['demandPolicy'], self.o.state['demandPolicy'])
        events = self.h.db.execute(
            "SELECT detail FROM opt_events WHERE kind='demand-policy-updated'"
        ).fetchall()
        self.assertEqual(len(events), 2)
        self.assertIn('phone.', events[-1][0])

    def test_paused_queued_or_switching_states_are_not_overwritten(self):
        for change in (
            {'mode': 'observe'},
            {'mode': 'week'},
            {'requestedModel': 'b'},
            {'pending': {'model': 'b', 'kind': 'demand'}},
        ):
            with self.subTest(change=change):
                original = copy.deepcopy(self.o.state)
                self.o.state.update(change)
                before = copy.deepcopy(self.o.state)
                with self.assertRaises(ValueError):
                    self.o.control_action(self.request(), 'phone')
                self.assertEqual(self.o.state, before)
                self.o.runner.assert_not_called()
                self.o.state = original

    def test_rejects_stale_control_and_changed_provider(self):
        old = self.request()
        self.o.state['models'] = ['a', 'c']
        with self.assertRaises(ValueError):
            self.o.control_action(old)
        fresh = self.request()
        self.o.read_state.return_value = {**self.raw, 'pid': 2}
        with self.assertRaises(ValueError):
            self.o.control_action(fresh)
        self.assertEqual(self.o.state['demandPolicy']['maxSwitchesPerDay'], 12)

    def test_rejects_foreign_scope_offline_and_stale_readings(self):
        for change in ({'account': 'other'}, {'device': 'other'}, {'provider': {'online': False}}):
            original = copy.deepcopy(self.o.live)
            self.o.live.update(change)
            with self.assertRaises(ValueError):
                self.o.control_action(self.request())
            self.o.live = original
        self.o.raw['written_at'] = self.now - 60
        with self.assertRaises(ValueError):
            self.o.control_action(self.request())
        self.assertEqual(self.o.state['demandPolicy']['maxSwitchesPerDay'], 12)

    def test_invalid_changes_do_not_partially_apply(self):
        before = copy.deepcopy(self.o.state)
        for changes in (
            {},
            {'maxSwitchesPerDay': 49},
            {'idleEscapeMinutes': 37},
            {'maxSwitchesPerDay': True},
            {'memoryHeadroomGB': 0},
            {'newField': 1},
            {'minRunMinutes': 30, 'confirmationMinutes': 60},
            {'maxSwitchesPerDay': float('nan')},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    self.o.control_action(self.request(changes))
                self.assertEqual(self.o.state, before)
        with self.assertRaises(ValueError):
            self.o.control_action(self.request(models=['a', 'b']))

    def test_noop_does_not_erase_confirmation_or_add_event(self):
        before = copy.deepcopy(self.o.state)
        self.o.control_action(self.request({'maxSwitchesPerDay': 12}))
        self.assertEqual(self.o.state, before)
        self.assertEqual(self.h.db.execute('SELECT COUNT(*) FROM opt_events').fetchone()[0], 0)

    def test_failed_persistence_leaves_in_memory_policy_unchanged(self):
        before = copy.deepcopy(self.o.state)
        self.o.save = Mock(side_effect=OSError('fixture storage failure'))
        with self.assertRaises(OSError):
            self.o.control_action(self.request())
        self.assertEqual(self.o.state, before)
        self.o.runner.assert_not_called()

    def test_active_inference_does_not_require_a_new_idle_wait_to_tune(self):
        self.o.raw['inference_active'] = True
        self.o.read_state.return_value = copy.deepcopy(self.o.raw)
        self.o.control_action(self.request())
        self.assertEqual(self.o.state['demandPolicy']['maxSwitchesPerDay'], 24)
        self.o.runner.assert_not_called()


if __name__ == '__main__':
    unittest.main()
