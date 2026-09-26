"""Malformed manual requests must not observe or command the provider."""

import unittest
from unittest.mock import Mock
from optimizer import Optimizer


class ManualRequestAdmissionTests(unittest.TestCase):
    def bare_optimizer(self):
        value = Optimizer.__new__(Optimizer)
        for name in (
            'manual_snapshot',
            'read_state',
            'read_options',
            'service_disabled',
            'apply_manual_action',
            'command',
            'runner',
        ):
            setattr(value, name, Mock(side_effect=AssertionError('Unexpected provider access')))
        return value

    def test_invalid_or_missing_ids_never_observe_admit_or_command(self):
        for source in ('mac', 'phone'):
            for action in ('switch', 'cancel'):
                for fields in (
                    {},
                    {'requestId': None},
                    {'requestId': ''},
                    {'requestId': 'invalid'},
                    {'requestId': 1},
                    {'requestId': True},
                    {'requestId': []},
                    {'requestId': {}},
                ):
                    with self.subTest(source=source, action=action, fields=fields):
                        value = self.bare_optimizer()
                        with self.assertRaisesRegex(ValueError, 'valid switch request ID'):
                            value.manual_action(
                                {'action': action, 'model': 'fixture-model', **fields}, source
                            )
                        for name in (
                            'manual_snapshot',
                            'read_state',
                            'read_options',
                            'service_disabled',
                            'apply_manual_action',
                            'command',
                            'runner',
                        ):
                            getattr(value, name).assert_not_called()

    def test_atomic_admission_still_rejects_invalid_id_independently(self):
        for action in ('switch', 'cancel'):
            with self.subTest(action=action):
                value = Optimizer.__new__(Optimizer)
                with self.assertRaisesRegex(ValueError, 'valid switch request ID'):
                    value.apply_manual_action(
                        {'action': action, 'requestId': 'invalid'}, 'mac', None
                    )

    def test_valid_id_keeps_observation_failures_distinct(self):
        value = self.bare_optimizer()
        value.manual_snapshot.side_effect = RuntimeError('Fixture provider unavailable')
        with self.assertRaisesRegex(RuntimeError, 'Fixture provider unavailable'):
            value.manual_action(
                {
                    'action': 'switch',
                    'model': 'fixture-model',
                    'requestId': '79ea2dda-755e-4b23-9632-a768a7ba0e39',
                }
            )
        value.manual_snapshot.assert_called_once()
        value.apply_manual_action.assert_not_called()
        value.command.assert_not_called()
