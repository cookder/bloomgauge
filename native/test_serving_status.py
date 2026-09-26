import copy, unittest
from unittest.mock import Mock
from optimizer import roster_identity
import test_provider_eligibility as fixtures


class ServingStatusTests(unittest.TestCase):
    setUp = fixtures.EligibilityTests.setUp
    tearDown = fixtures.EligibilityTests.tearDown
    refresh = fixtures.EligibilityTests.refresh
    roster = fixtures.EligibilityTests.roster

    def test_serving_hardware_current_model_remains_eligible_and_can_resume(self):
        self.o.catalog[0]['required_provider_capabilities'] = ['apple_m5', 'mlx_nax']
        self.refresh(self.roster(['a'], status='serving'))
        self.assertTrue(self.o.identity_ok)
        self.assertTrue(self.o.identity_hardware)
        self.assertTrue(self.o.capability_verified('a'))
        self.o.state.update(mode='observe', endsAt=None)
        before = copy.deepcopy(self.o.state)
        self.o.command = Mock()
        self.assertTrue(self.o.demand_resume_status()['available'])
        self.o.control_action(
            {
                'action': 'resume-demand',
                'expectedControl': self.o.control_version(),
                'currentModel': 'a',
            }
        )
        self.assertEqual(self.o.state['mode'], 'demand')
        for k in ('models', 'startedAt', 'endsAt', 'originalModel', 'demandPolicy', 'blockHours'):
            self.assertEqual(self.o.state[k], before[k])
        self.o.command.assert_not_called()

    def test_online_serving_online_keeps_same_hardware_capability(self):
        for status in ('online', 'serving', 'online'):
            self.refresh(self.roster(['a'], status=status))
            self.assertTrue(self.o.capability_verified('a'), status)

    def test_nonlive_status_or_nonhardware_trust_never_grants_hardware(self):
        for status in (
            'offline',
            'untrusted',
            'never_seen',
            'draining',
            'unknown',
            'SERVING',
            None,
        ):
            self.assertFalse(
                roster_identity(self.raw, [self.roster(['a'], status=status)])['hardwareVerified'],
                status,
            )
        for trust in ('self_signed', 'software', 'none', 'unknown', None):
            self.assertFalse(
                roster_identity(
                    self.raw, [self.roster(['a'], status='serving', trust_level=trust)]
                )['hardwareVerified'],
                trust,
            )

    def test_serving_does_not_qualify_unadvertised_target_or_mismatched_identity(self):
        self.refresh(self.roster(['a'], status='serving'))
        self.assertFalse(self.o.capability_verified('b'))
        with self.assertRaises(ValueError):
            roster_identity(self.raw, [self.roster(['a'], status='serving', se_public_key='other')])
        with self.assertRaises(ValueError):
            roster_identity(self.raw, [self.roster(['a'], status='serving')] * 2)
        self.assertFalse(
            roster_identity(self.raw, [self.roster(['b'], status='serving')])['servingEligible']
        )

    def test_resume_surfaces_selected_current_model_unavailability(self):
        self.o.state.update(mode='observe', endsAt=None)
        self.o.catalog[0]['required_provider_capabilities'] = ['apple_m5']
        self.o.identity_hardware = False
        before = copy.deepcopy(self.o.state)
        status = self.o.demand_resume_status()
        self.assertFalse(status['available'])
        self.assertIn(
            'Runtime support is not yet verified for automatic selection', status['reason']
        )
        self.assertNotIn('include the serving model', status['reason'])
        self.assertEqual(self.o.state, before)
        with self.assertRaisesRegex(ValueError, 'Runtime support is not yet verified'):
            self.o.control_action(
                {
                    'action': 'resume-demand',
                    'expectedControl': self.o.control_version(),
                    'currentModel': 'a',
                }
            )
        self.o.runner.assert_not_called()

    def test_unselected_current_or_missing_alternative_keeps_selection_guidance(self):
        for selected in (['b'], ['a']):
            self.o.state.update(mode='observe', endsAt=None, models=selected)
            status = self.o.demand_resume_status()
            self.assertFalse(status['available'])
            self.assertIn('include the serving model', status['reason'])
            self.o.runner.assert_not_called()


if __name__ == '__main__':
    unittest.main()
