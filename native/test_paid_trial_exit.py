"""Completed low-paid trials retain the independently qualified earnings lane."""

import copy
import unittest
from demand_optimizer import decide, policy
from test_demand_optimizer import NOW, candidate, summary


class PaidTrialExitTests(unittest.TestCase):
    def setUp(self):
        self.rows = [candidate('current'), candidate('better')]
        self.rows[0]['earningsTarget'] = {
            'ready': True,
            'rate': 0.03,
            'fastRate': 0.03,
            'usdPerHour': 0.12,
            'warmMinutes': 40,
            'livePaid': {'fresh': True, 'rate': 0.03},
            'highEarnings': {'active': False},
        }
        self.rows[1]['signal'].update(
            status='normal', load=60, pressure=0.2, sustained={'qualified': False, 'pressure': 0.2}
        )
        self.rates = {'current': summary(0.03), 'better': {**summary(0.2), 'asOf': NOW - 600}}
        self.trial = {
            'current': True,
            'complete': True,
            'settled': True,
            'status': 'productive',
            'trialMinutes': 20,
        }
        self.activity = {'fresh': True, 'idleSeconds': 0}

    def choose(self, **kwargs):
        return decide(
            self.rows,
            'current',
            self.rates,
            kwargs.get('events', []),
            kwargs.get('runs', []),
            policy(),
            NOW,
            NOW - 3600,
            self.activity,
            self.trial,
        )

    def test_completed_shortfall_preserves_profitable_earnings_lane(self):
        result = self.choose()
        self.assertEqual(result['target'], 'better')
        self.assertEqual(result['kind'], 'earnings')
        self.assertTrue(result['completedTrialExit'])
        self.assertFalse(result['escapeReady'])
        self.assertIsNone(result['explorationTrigger'])
        self.assertIn('net-gain', result['reason'])
        self.assertNotIn('Starting a demand trial', result['reason'])
        row = next(r for r in result['opportunities'] if r['model'] == 'better')
        self.assertGreater(row['netGainUsd'], 0.02)
        self.assertFalse(row['sustained']['qualified'])

    def test_running_unsettled_or_unmatched_trial_does_not_gain_the_exception(self):
        original = copy.deepcopy(self.trial)
        for change in [
            {'complete': False, 'status': 'running'},
            {'settled': False},
            {'current': False},
        ]:
            self.trial = {**original, **change}
            with self.subTest(change=change):
                self.assertIsNone(self.choose()['target'])

    def test_missing_paid_forecast_stale_forecast_and_insufficient_net_gain_stay_held(self):
        original = copy.deepcopy(self.rates['better'])
        for change in [
            {'forecastUsable': False},
            {'asOf': NOW - 8 * 86400},
            {'asOf': NOW + 60},
            summary(0.06),
        ]:
            self.rates['better'] = {**original, **change}
            with self.subTest(change=change):
                self.assertIsNone(self.choose()['target'])

    def test_current_paid_recovery_and_missing_baseline_stay_protected(self):
        self.rows[0]['earningsTarget']['livePaid']['rate'] = 0.3
        self.assertIsNone(self.choose()['target'])
        self.rows[0]['earningsTarget']['livePaid']['rate'] = 0.03
        self.rates['current'] = None
        self.assertIsNone(self.choose()['target'])

    def test_candidate_safety_and_budgets_still_apply(self):
        original = copy.deepcopy(self.rows[1])
        for change in [
            {'selected': False},
            {'available': False},
            {'loadBudget': {'afterUnloadGB': 30.5, 'requiredGB': 30}},
            {'signal': {**original['signal'], 'observedAt': NOW - 91}},
            {'signal': {**original['signal'], 'load': 0, 'pressure': 0}},
        ]:
            self.rows[1] = {**original, **change}
            with self.subTest(change=change):
                self.assertIsNone(self.choose()['target'])
        self.rows[1] = original
        self.assertIsNone(self.choose(runs=[{'at': NOW - 60, 'downtime': 1790}])['target'])
        self.assertIsNone(
            self.choose(
                events=[{'at': NOW - 60, 'model': 'better', 'kind': 'recovered', 'downtime': 120}]
            )['target']
        )

    def test_sparse_selected_model_keeps_original_discovery_path(self):
        self.rows[0]['earningsTarget']['asOf'] = NOW - 120
        self.rates['current']['recent'] = True
        self.rates['better']['forecastUsable'] = False
        self.rows[1]['signal'].update(pressure=2, sustained={'qualified': True, 'pressure': 2})
        result = self.choose()
        self.assertEqual(result['target'], 'better')
        self.assertEqual(result['kind'], 'explore')
        self.assertTrue(result['escapeReady'])
        self.assertEqual(result['explorationTrigger'], 'earnings_target')

    def test_missing_fresh_paid_guard_does_not_promote_shortfall_trial(self):
        self.rows[0]['earningsTarget']['livePaid']['fresh'] = False
        self.assertIsNone(self.choose()['target'])

    def test_qualified_paid_exit_wins_over_high_pressure_discovery(self):
        discovery = candidate('unknown')
        discovery['signal'].update(pressure=100, sustained={'qualified': True, 'pressure': 100})
        self.rows.append(discovery)
        self.rates['unknown'] = None
        result = self.choose()
        self.assertEqual(result['target'], 'better')
        self.assertEqual(result['kind'], 'earnings')

    def test_discovery_keeps_its_existing_signal_status_rules(self):
        self.rows[0]['earningsTarget']['asOf'] = NOW - 120
        self.rates['current']['recent'] = True
        self.rows[1]['signal'].update(
            status='watching', pressure=2, sustained={'qualified': True, 'pressure': 2}
        )
        result = self.choose()
        self.assertEqual(result['target'], 'better')
        self.assertEqual(result['kind'], 'explore')


if __name__ == '__main__':
    unittest.main()
