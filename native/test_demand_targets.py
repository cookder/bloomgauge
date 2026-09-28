import copy
import unittest
from demand_targets import GEMMA, chosen_goal, target_status, return_evidence
from demand_optimizer import decide, policy, estimate
from test_demand_optimizer import NOW, summary, minutes
from test_demand_fallback import rows, MODEL


def evidence(rate=0.05, now=NOW):
    return {'minutes': minutes(rate, now - 2520, 40)}


def goal(rate=0.05, since=NOW - 7200, rules=None, now=NOW):
    return target_status(
        evidence(rate, now), since, now, rules or policy(), live_paid={'fresh': True, 'rate': rate}
    )


def options(rate=0.05):
    r = rows()
    r[0]['earningsTarget'] = goal(rate)
    gemma = copy.deepcopy(r[2])
    gemma['id'] = GEMMA
    gemma['returnEvidence'] = return_evidence(
        {**summary(0.16), 'established': True, 'evidenceHours': 70, 'asOf': NOW - 86400}, NOW
    )
    return r + [gemma]


def choose(data=None, current='a', rates=None, idle=0, runs=None, trial=None):
    data = data or options()
    measured = (
        next((r.get('earningsTarget') for r in data if r['id'] == current), None) or {}
    ).get('rate')
    current_rate = measured if isinstance(measured, (int, float)) else 0
    return decide(
        data,
        current,
        {current: {**summary(current_rate), 'asOf': NOW - 120, 'recent': True}, **(rates or {})},
        [],
        runs or [],
        policy(),
        NOW,
        activity={'fresh': True, 'idleSeconds': idle},
        trial=trial,
    )


class ChosenGoalTests(unittest.TestCase):
    def test_placeholder_default_is_not_a_goal(self):
        self.assertIsNone(chosen_goal(policy()))
        self.assertIsNone(chosen_goal({}))
        self.assertIsNone(chosen_goal(None))
        self.assertEqual(chosen_goal(policy({'targetUsdPerHour': 0.2})), 0.2)
        self.assertEqual(chosen_goal(policy({'targetUsdPerHour': 0.08})), 0.08)


class TargetEvidenceTests(unittest.TestCase):
    def test_persistent_small_paid_jobs_can_trigger_without_total_idle(self):
        g = goal()
        self.assertTrue(g['ready'])
        self.assertEqual(g['status'], 'below_target')
        self.assertAlmostEqual(g['rate'], 0.05)
        self.assertAlmostEqual(g['dailyUsd'], 2.88)
        self.assertGreaterEqual(g['warmMinutes'], 30)

    def test_alignment_does_not_turn_fresh_earnings_stale(self):
        for offset in range(0, 300, 15):
            with self.subTest(offset=offset):
                self.assertTrue(goal(now=NOW + offset)['ready'])

    def test_minimum_run_and_current_provider_start_are_required(self):
        self.assertFalse(goal(since=NOW - 1500)['ready'])
        for since in (None, NOW + 60, NOW - 600):
            self.assertFalse(goal(since=since)['ready'])
        self.assertFalse(target_status(evidence(), NOW - 7200, NOW, policy(), NOW - 600)['ready'])

    def test_brief_dips_near_the_protect_level_and_paid_bursts_do_not_trigger(self):
        protect = policy({'protectUsdPerHour': 0.11})
        for rate in (0.115, 0.12, 0.5):
            self.assertFalse(goal(rate, rules=protect)['ready'])
        h = evidence()
        for m in h['minutes'][-5:]:
            m['usd'] = 0.5 / 60
        self.assertFalse(target_status(h, NOW - 7200, NOW, protect)['ready'])
        h = evidence(0.15)
        for m in h['minutes'][-10:]:
            m['usd'] = 0
        self.assertFalse(target_status(h, NOW - 7200, NOW, protect)['ready'])

    def test_missing_unsettled_cold_and_duplicate_minutes_are_not_zero_earnings(self):
        for h in (
            {'minutes': []},
            {'minutes': minutes(0.05, NOW - 900, 12)},
            {'minutes': [{**m, 'seconds': 20} for m in evidence()['minutes']]},
            {'minutes': minutes(0.05, NOW - 80, 40)},
        ):
            self.assertFalse(target_status(h, NOW - 7200, NOW, policy())['ready'])
        h = evidence()
        h['minutes'] *= 3
        self.assertEqual(
            target_status(h, NOW - 7200, NOW, policy(), live_paid={'fresh': True, 'rate': 0.05}),
            goal(),
        )
        h = evidence()
        h['minutes'] = h['minutes'][:15] + h['minutes'][25:]
        g = target_status(h, NOW - 7200, NOW, policy())
        self.assertTrue(g['ready'])
        self.assertEqual(g['warmMinutes'], 30)
        self.assertEqual(g['coveragePercent'], 75)
        self.assertAlmostEqual(g['rate'], 0.05)
        h['minutes'] = h['minutes'][1:]
        self.assertFalse(target_status(h, NOW - 7200, NOW, policy())['ready'])

    def test_scattered_small_gaps_do_not_erase_valid_earlier_blocks(self):
        h = evidence()
        h['minutes'] = [m for i, m in enumerate(h['minutes']) if i not in (3, 9, 14, 21, 29)]
        g = target_status(h, NOW - 7200, NOW, policy())
        self.assertTrue(g['ready'])
        self.assertEqual(g['warmMinutes'], 35)
        self.assertAlmostEqual(g['rate'], 0.05)

    def test_stale_or_unknown_paid_data_cannot_trigger(self):
        h = evidence()
        h['minutes'] = h['minutes'][:-5]
        self.assertFalse(target_status(h, NOW - 7200, NOW, policy())['ready'])
        self.assertTrue(goal(0)['ready'])  # covered, settled warm idle is real
        for bad in (float('nan'), float('inf'), float('-inf'), True, None):
            h = evidence()
            h['minutes'] = [{**m, 'usd': bad} for m in h['minutes']]
            self.assertFalse(target_status(h, NOW - 7200, NOW, policy())['ready'])

    def test_signed_correction_preserves_covered_time_and_net_paid_rate(self):
        h = evidence(0.12)
        h['minutes'][-5]['usd'] = -0.05
        g = target_status(
            h,
            NOW - 7200,
            NOW,
            policy({'protectUsdPerHour': 0.11}),
            live_paid={'fresh': True, 'rate': 0},
        )
        self.assertEqual(g['warmMinutes'], 40)
        self.assertEqual(g['coveragePercent'], 100)
        self.assertAlmostEqual(g['rate'], 0.042)
        self.assertAlmostEqual(g['fastRate'], -0.504)
        self.assertEqual(g['status'], 'watching')
        self.assertFalse(g['ready'])

    def test_sustained_signed_loss_is_low_pay_not_missing_coverage(self):
        g = target_status(
            evidence(-0.03), NOW - 7200, NOW, policy(), live_paid={'fresh': True, 'rate': -0.02}
        )
        self.assertEqual(g['warmMinutes'], 40)
        self.assertEqual(g['coveragePercent'], 100)
        self.assertAlmostEqual(g['rate'], -0.03)
        self.assertAlmostEqual(g['fastRate'], -0.03)
        self.assertTrue(g['ready'])
        self.assertEqual(g['status'], 'below_target')

    def test_signed_money_does_not_allow_negative_time_or_partial_coverage(self):
        for key, value in (('at', -1), ('seconds', -60), ('seconds', 30)):
            h = evidence(-0.03)
            h['minutes'] = [{**m, key: value} for m in h['minutes']]
            with self.subTest(key=key, value=value):
                g = target_status(
                    h, NOW - 7200, NOW, policy(), live_paid={'fresh': True, 'rate': -0.02}
                )
                self.assertEqual(g['warmMinutes'], 0)
                self.assertFalse(g['ready'])

    def test_goal_is_bounded_and_old_settings_gain_default_without_reset(self):
        self.assertEqual(policy({'fallbackEnabled': 0})['targetUsdPerHour'], 0.12)
        self.assertTrue(goal(0.13, rules=policy({'targetUsdPerHour': 0.20}))['ready'])
        for bad in (0, 0.121, 1, True, '0.12', None):
            with self.assertRaises(ValueError):
                policy({'targetUsdPerHour': bad})

    def test_return_preference_needs_repeated_recent_history_but_is_not_a_forecast(self):
        for observed in (
            None,
            summary(0.15),
            {**summary(0.15), 'established': True, 'asOf': NOW - 8 * 86400},
        ):
            self.assertFalse(return_evidence(observed, NOW)['qualified'])
        h = {
            'hours': 70,
            'days': 4,
            'jobs': 2000,
            'minutes': sum(
                (
                    minutes(0.15, NOW - (i + 1) * 86400, 24 * 60)
                    for i in range(policy()['maxSwitchesPerDay'])
                ),
                [],
            ),
        }
        e = estimate(h, {}, {}, NOW)
        self.assertTrue(e['established'])
        self.assertFalse(e['forecastUsable'])
        self.assertTrue(return_evidence(e, NOW)['qualified'])


class TargetDecisionTests(unittest.TestCase):
    def test_gemma_is_preferred_over_fallback_for_persistent_low_earnings(self):
        d = choose()
        self.assertEqual(d['target'], GEMMA)
        self.assertTrue(d['escapeReady'])
        self.assertEqual(d['kind'], 'explore')
        self.assertEqual(d['explorationTrigger'], 'earnings_target')
        self.assertEqual(d['preferredReturn']['selection'], 'preferred_return')
        self.assertIsNone(d['fallback']['selection'])
        self.assertIsNone(next(r for r in d['opportunities'] if r['model'] == GEMMA)['netGainUsd'])

    def test_failed_trial_and_last_daily_attempt_prefer_gemma_over_oss(self):
        r = options(0.15)
        r[0]['earningsTarget']['livePaid'] = {'fresh': True, 'rate': 0}
        for kwargs in (
            {
                'trial': {
                    'current': True,
                    'complete': True,
                    'settled': True,
                    'status': 'no_paid_work',
                },
                'idle': 120,
            },
            {
                'runs': [
                    {'at': NOW - i, 'downtime': 10}
                    for i in range(policy()['maxSwitchesPerDay'] - 1)
                ],
                'idle': 1200,
            },
        ):
            self.assertEqual(choose(r, **kwargs)['target'], GEMMA)

    def test_higher_verified_earnings_beat_gemma_and_short_history_can_still_be_tried(self):
        self.assertEqual(choose(rates={'new': summary(0.5)})['target'], 'new')
        r = options()
        r[-1]['selected'] = False
        d = choose(r)
        self.assertEqual(d['target'], 'new')
        self.assertEqual(d['kind'], 'explore')

    def test_low_paying_fallback_cannot_replace_better_current_earnings(self):
        r = options(0.07)
        r[2]['selected'] = False
        r[-1]['selected'] = False
        d = choose(r)
        self.assertIsNone(d['target'])
        self.assertIn(
            'does not improve', next(r for r in d['opportunities'] if r['model'] == MODEL)['reason']
        )

    def test_goal_does_not_override_missing_activity_or_warm_data(self):
        for activity in ({'fresh': False, 'idleSeconds': 0}, {'fresh': False, 'idleSeconds': 9999}):
            d = decide(options(), 'a', {}, [], [], policy(), NOW, activity=activity)
            self.assertIsNone(d['target'])
            self.assertFalse(d['earningsTarget']['ready'])
        r = options()
        r[0]['earningsTarget'] = goal(since=NOW - 600)
        self.assertIsNone(choose(r)['target'])

    def test_selected_local_demand_memory_and_budget_guards_still_apply(self):
        for change in (
            {'selected': False},
            {'available': False},
            {'loadBudget': None},
            {'signal': {**options()[-1]['signal'], 'sustained': {'qualified': False}}},
        ):
            r = options()
            r[-1].update(change)
            self.assertNotEqual(choose(r)['target'], GEMMA)
        self.assertIsNone(
            choose(
                runs=[{'at': NOW - i, 'downtime': 10} for i in range(policy()['maxSwitchesPerDay'])]
            )['target']
        )
        self.assertIsNone(
            choose(trial={'current': True, 'status': 'running', 'trialMinutes': 20})['target']
        )

    def test_paying_gemma_and_hot_other_models_are_not_forced_to_a_named_model(self):
        r = options(0.5)
        self.assertIsNone(choose(r)['target'])
        r = options()
        r[-1]['earningsTarget'] = goal(0.5)
        self.assertIsNone(choose(r, current=GEMMA)['target'])


if __name__ == '__main__':
    unittest.main()


class ProtectLevelTests(unittest.TestCase):
    def test_pace_below_the_protect_level_allows_learning_and_above_it_protects(self):
        self.assertTrue(goal(0.15)['ready'])  # default $0.20: a $0.15 pace may use learning time
        self.assertEqual(
            goal(0.15, rules=policy({'protectUsdPerHour': 0.10}))['status'], 'productive'
        )
        self.assertEqual(goal(0.15)['protectUsdPerHour'], 0.20)

    def test_earnings_target_no_longer_changes_decisions(self):
        for target in (0.08, 0.25):
            self.assertEqual(
                goal(0.15, rules=policy({'targetUsdPerHour': target}))['ready'], goal(0.15)['ready']
            )
