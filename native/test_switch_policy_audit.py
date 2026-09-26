"""Measured switching costs and explicit, bounded local controls."""

import copy
import unittest
from demand_optimizer import (
    POLICY,
    POLICY_REVISION,
    PREVIOUS_DEFAULTS,
    upgraded_policy,
    policy,
    decide,
    switch_timing,
)
from test_demand_optimizer import NOW, candidate, summary, decision
from test_demand_trials import observations
from demand_optimizer import sustained_demand
from test_trial_economics import goal


class SwitchingAuditTests(unittest.TestCase):
    def event(self, seconds, model='b', age=30, kind='switched'):
        return dict(at=NOW - age, model=model, kind=kind, downtime=seconds)

    def test_fast_observed_loads_have_no_three_minute_floor(self):
        self.assertEqual(
            switch_timing([self.event(47)], 'b', NOW),
            dict(seconds=47, scope='limited_history', samples=1),
        )
        timings = [self.event(13, age=100), self.event(79, age=10)]
        self.assertEqual(switch_timing(timings, 'b', NOW)['seconds'], 79)

    def test_unknown_models_use_this_macs_other_solo_switches(self):
        events = [
            self.event(s, model='solo' + str(i), age=i + 1)
            for i, s in enumerate([47, 70, 78, 90, 129])
        ]
        t = switch_timing(events, 'new', NOW)
        self.assertEqual(t, dict(seconds=90, scope='mac_history', samples=5))
        self.assertEqual(switch_timing([], 'new', NOW)['seconds'], 120)
        # Foreign model combinations, failures and future/stale events cannot
        # manufacture a fast timing estimate for an untried solo model.
        bad = [self.event(1, age=-1), self.event(1, age=31 * 86400), self.event(1, kind='failed')]
        self.assertEqual(switch_timing(bad, 'b', NOW)['samples'], 0)

    def test_optional_return_changes_budget_but_not_projected_profit(self):
        fast = decision(events=[self.event(50), self.event(30, model='a')])
        slow = decision(events=[self.event(50), self.event(900, model='a')])
        a = next(r for r in fast['opportunities'] if r['model'] == 'b')
        b = next(r for r in slow['opportunities'] if r['model'] == 'b')
        self.assertAlmostEqual(a['netGainUsd'], b['netGainUsd'])
        self.assertNotEqual(a['returnSeconds'], b['returnSeconds'])

    def test_memory_shortfall_cannot_fake_an_earnings_target_trigger(self):
        blocked = {**candidate('blocked'), 'loadBudget': {'afterUnloadGB': 10, 'requiredGB': 30}}
        for rows in (
            [candidate('a'), blocked, candidate('b')],
            [candidate('a'), candidate('b'), blocked],
        ):
            result = decide(rows, 'a', {'a': summary(0.15)}, [], [], policy(), NOW)
            self.assertIsNone(result['explorationTrigger'])
            self.assertNotIn('below target', result['reason'])
            self.assertFalse(result['escapeReady'])

    def test_memory_failure_does_not_crash_an_idle_trial_with_unknown_earnings(self):
        rows = [
            candidate('a'),
            {**candidate('too-big'), 'loadBudget': {'afterUnloadGB': 10, 'requiredGB': 30}},
            {
                **candidate('b'),
                'signal': {
                    **candidate('b')['signal'],
                    'sustained': sustained_demand(observations(), NOW),
                },
            },
        ]
        result = decide(
            rows, 'a', {}, [], [], policy(), NOW, activity={'fresh': True, 'idleSeconds': 1200}
        )
        self.assertIsNone(result['target'])
        self.assertIn(
            'frozen incumbent',
            next(r for r in result['opportunities'] if r['model'] == 'b')['reason'],
        )

    def test_failure_wait_is_fifteen_minutes_and_explains_itself(self):
        result = decision(events=[self.event(60, kind='failed', age=899)])
        r = next(r for r in result['opportunities'] if r['model'] == 'b')
        self.assertIsNone(result['target'])
        self.assertIn('15 minutes', r['reason'])
        self.assertEqual(r['failureCooldownUntil'], NOW + 1)
        self.assertEqual(decision(events=[self.event(60, kind='failed', age=900)])['target'], 'b')

    def test_updated_retry_setting_shortens_an_old_trial_wait(self):
        rows = [
            candidate('a'),
            {
                **candidate('b'),
                'signal': {
                    **candidate('b')['signal'],
                    'sustained': sustained_demand(observations(), NOW),
                },
            },
        ]
        run = {
            'at': NOW - 7200,
            'model': 'b',
            'downtime': 60,
            'decision': {
                'outcome': {
                    'complete': True,
                    'status': 'no_traffic',
                    'windowEnd': NOW - 3600,
                    'cooldownUntil': NOW + 3600,
                }
            },
        }
        rows[0]['earningsTarget'] = goal(0)
        result = decide(
            rows,
            'a',
            {'a': {**summary(0), 'recent': True}},
            [],
            [run],
            policy(),
            NOW,
            activity={'fresh': True, 'idleSeconds': 1200},
        )
        self.assertEqual(result['target'], 'b')
        self.assertIsNone(
            next(r for r in result['opportunities'] if r['model'] == 'b')['trialCooldownUntil']
        )

    def test_old_defaults_upgrade_once_and_custom_controls_survive(self):
        old = {**policy(), **PREVIOUS_DEFAULTS}
        result = upgraded_policy(old)
        self.assertEqual(result, POLICY)
        custom = {
            **old,
            'maxSwitchesPerDay': 6,
            'minRunMinutes': 120,
            'trialCooldownMinutes': 240,
            'targetUsdPerHour': 0.2,
        }
        changed = upgraded_policy(custom)
        for k in ['maxSwitchesPerDay', 'minRunMinutes', 'trialCooldownMinutes', 'targetUsdPerHour']:
            self.assertEqual(changed[k], custom[k])
        self.assertEqual(upgraded_policy(old, POLICY_REVISION), old)
        self.assertEqual(old, {**policy(), **PREVIOUS_DEFAULTS})

    def test_two_prior_attempts_do_not_exhaust_the_new_budget(self):
        runs = [{'at': NOW - i - 1, 'downtime': s} for i, s in enumerate([129, 79])]
        self.assertEqual(decision(runs=runs)['target'], 'b')
        self.assertEqual(decision(runs=runs)['limits']['switchLimit'], 12)


if __name__ == '__main__':
    unittest.main()
