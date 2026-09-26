"""Inline plan edits: live in Follow demand, stored while Manual, never a switch."""

import unittest
from unittest.mock import Mock

import test_optimizer as fixtures


class UpdatePlanTests(unittest.TestCase):
    setUp_base = fixtures.ControllerTests.setUp
    tearDown = fixtures.ControllerTests.tearDown

    def setUp(self):
        self.setUp_base()
        self.o.state['mode'] = 'demand'
        self.o.snapshot = Mock(return_value={})

    def send(self, **fields):
        return self.o.update_plan(
            {'action': 'update-plan', 'expectedControl': self.o.control_version(), **fields}, 'mac'
        )

    def events(self):
        return [r[0] for r in self.h.db.execute('SELECT kind FROM opt_events ORDER BY id')]

    def test_live_update_saves_models_and_rules_without_a_provider_command(self):
        self.o.state['demandProposal'] = {'target': 'b'}
        self.send(models=['a', 'b', 'c'], demandPolicy={'targetUsdPerHour': 0.15})
        self.assertEqual(self.o.state['models'], ['a', 'b', 'c'])
        self.assertEqual(self.o.state['demandPolicy']['targetUsdPerHour'], 0.15)
        self.assertEqual(self.o.state['mode'], 'demand')
        self.assertNotIn('demandProposal', self.o.state)
        self.assertEqual(self.h.cache('optimizer-settings')['models'], ['a', 'b', 'c'])
        self.assertEqual(self.events(), ['plan-updated'])
        self.o.runner.assert_not_called()

    def test_serving_model_must_stay_selected_while_on(self):
        with self.assertRaisesRegex(ValueError, 'serving model'):
            self.send(models=['b', 'c'])
        self.assertEqual(self.o.state['models'], ['a', 'b'])

    def test_manual_mode_stores_plan_for_next_on_even_without_serving_model(self):
        self.o.state['mode'] = 'observe'
        self.send(models=['b', 'c'])
        self.assertEqual(self.o.state['models'], ['b', 'c'])
        self.assertEqual(self.o.state['mode'], 'observe')

    def test_rules_only_update_keeps_models_and_merges_policy(self):
        self.send(demandPolicy={'maxSwitchesPerDay': 6})
        self.assertEqual(self.o.state['models'], ['a', 'b'])
        self.assertEqual(self.o.state['demandPolicy']['maxSwitchesPerDay'], 6)
        self.assertEqual(self.o.state['demandPolicy']['targetUsdPerHour'], 0.12)

    def test_rejects_invalid_input_stale_control_and_busy_states(self):
        for fields in (
            {},
            {'models': ['a']},
            {'models': ['a', 'a']},
            {'models': 'a,b'},
            {'demandPolicy': {'targetUsdPerHour': 9}},
            {'demandPolicy': {'unknown': 1}},
            {'models': ['a', 'b'], 'mode': 'week'},
        ):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.send(**fields)
        with self.assertRaises(ValueError):
            self.o.update_plan(
                {'action': 'update-plan', 'expectedControl': 'old', 'models': ['a', 'b']}, 'mac'
            )
        for change in ({'mode': 'week'}, {'pending': {'model': 'b'}}, {'requestedModel': 'b'}):
            saved = dict(self.o.state)
            self.o.state.update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.send(models=['a', 'b', 'c'])
            self.o.state = saved
        self.o.automatic_control.operation = {'status': 'starting'}
        with self.assertRaisesRegex(ValueError, 'turning on'):
            self.send(models=['a', 'b', 'c'])
        self.assertEqual(self.o.state['models'], ['a', 'b'])
        self.assertEqual(self.events(), [])


if __name__ == '__main__':
    unittest.main()
