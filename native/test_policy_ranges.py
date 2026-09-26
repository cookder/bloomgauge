"""Typed optimizer controls: any on-step value within range, matching the UI's ranges."""

import unittest

from demand_optimizer import POLICY, policy


class PolicyRangeTests(unittest.TestCase):
    def test_on_step_values_inside_ranges_are_accepted(self):
        rules = policy(
            {
                'idleEscapeMinutes': 35,
                'trialMinutes': 25,
                'minRunMinutes': 45,
                'confirmationMinutes': 7,
                'minimumNetUsd': 0.015,
                'memoryHeadroomGB': 1.5,
                'maxSwitchesPerDay': 30,
                'improvementPercent': 15,
            }
        )
        self.assertEqual(rules['idleEscapeMinutes'], 35)
        self.assertEqual(rules['minimumNetUsd'], 0.015)

    def test_out_of_range_or_off_step_values_are_rejected(self):
        for rules in (
            {'idleEscapeMinutes': 5},
            {'memoryHeadroomGB': 0.5},
            {'maxSwitchesPerDay': 49},
            {'idleEscapeMinutes': 37},
            {'minimumNetUsd': 0.013},
            {'trialMinutes': 61},
            {'confirmationMinutes': 20, 'minRunMinutes': 15},
            {'targetUsdPerHour': 0.13},
        ):
            with self.subTest(rules=rules), self.assertRaises(ValueError):
                policy(rules)

    def test_defaults_are_inside_their_ranges(self):
        self.assertEqual(policy(dict(POLICY)), POLICY)


if __name__ == '__main__':
    unittest.main()
