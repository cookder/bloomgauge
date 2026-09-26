"""Conditional fallback preference, not a hard-coded income winner."""

import unittest
from demand_fallback import reliability, MODEL, assess
from demand_optimizer import decide, policy
from test_demand_optimizer import NOW, candidate, summary


def history(rate=0.04, blocks=12):
    start = NOW - 300 - blocks * 300
    return {
        'minutes': [
            {
                'at': start + i * 60,
                'seconds': 60,
                'usd': rate / 60,
                'paidJobs': 1 if rate else 0,
                'requests': 3,
                'tokens': 120,
            }
            for i in range(blocks * 5)
        ]
    }


def rows():
    result = [candidate('a'), candidate(MODEL), candidate('new')]
    for r in result:
        r['signal']['sustained'] = {
            'qualified': True,
            'pressure': 9 if r['id'] == MODEL else 2,
            'sourceAt': NOW - 30,
            'samples': 20,
        }
    result[0]['earningsTarget'] = {
        'ready': False,
        'rate': 0,
        'fastRate': 0,
        'asOf': NOW - 120,
        'warmMinutes': 30,
        'usdPerHour': 0.12,
        'productiveFloor': 0.09,
        'livePaid': {'fresh': True, 'rate': 0},
        'highEarnings': {'active': False},
    }
    result[1]['fallbackEvidence'] = reliability(history(), NOW)
    return result


def choose(data=None, failed=False, estimates=None, idle=1200, rules=None):
    trial = (
        {'current': True, 'complete': True, 'settled': True, 'status': 'no_paid_work', 'model': 'a'}
        if failed
        else None
    )
    data = data or rows()
    if failed:
        from demand_targets import target_status

        data[0]['earningsTarget'] = target_status(
            {}, None, NOW, rules or policy(), live_paid={'fresh': True, 'rate': 0}
        )
        data[0]['earningsTarget'].update(rate=0, fastRate=0, warmMinutes=30, asOf=NOW - 120)
    return decide(
        data,
        'a',
        {'a': {**summary(0), 'recent': True, 'asOf': NOW - 120}, **(estimates or {})},
        [],
        [],
        rules or policy(),
        NOW,
        activity={'fresh': True, 'idleSeconds': idle},
        trial=trial,
    )


class ReliabilityTests(unittest.TestCase):
    def test_steady_paid_work_is_observation_not_a_forecast(self):
        r = reliability(history(), NOW)
        self.assertTrue(r['qualified'])
        self.assertEqual(r['windows'], 12)
        self.assertEqual(r['trafficPercent'], 100)
        self.assertAlmostEqual(r['usdPerHour'], 0.04)
        self.assertNotIn('forecastUsable', r)

    def test_sparse_short_zero_and_unpaid_work_do_not_assert_reliability(self):
        for h in (history(blocks=5), history(0), {'minutes': []}):
            self.assertFalse(reliability(h, NOW)['qualified'])
        h = history()
        for m in h['minutes'][-10:]:
            m.update(usd=0, paidJobs=0)
        self.assertFalse(reliability(h, NOW)['qualified'])

    def test_last_ten_minutes_override_earlier_busy_history(self):
        h = history()
        for m in h['minutes'][-10:]:
            m.update(requests=0, tokens=0)
        r = reliability(h, NOW)
        self.assertGreater(r['trafficPercent'], 80)
        self.assertFalse(r['qualified'])

    def test_collection_gaps_do_not_stitch_separate_runs_into_steady_history(self):
        h = history()
        h['minutes'] = h['minutes'][:30] + h['minutes'][-25:]
        self.assertFalse(reliability(h, NOW)['qualified'])

    def test_old_future_partial_and_malformed_minutes_cannot_qualify(self):
        for change in (
            {'at': NOW - 90000},
            {'at': NOW + 60},
            {'at': NOW - 60},
            {'seconds': 59},
            {'tokens': float('nan')},
            {'usd': -1},
        ):
            h = history()
            h['minutes'] = [{**m, **change} for m in h['minutes']]
            self.assertFalse(reliability(h, NOW)['qualified'])

    def test_repeated_minute_inputs_cannot_inflate_coverage(self):
        h = history()
        h['minutes'] *= 2
        self.assertFalse(reliability(h, NOW)['qualified'])

    def test_preference_retires_when_network_or_current_local_traffic_changes(self):
        for changes in (
            {'observedAt': NOW - 91},
            {'coverage': 0.7},
            {'sustained': {'qualified': False}},
            {'status': 'no_headroom'},
        ):
            r = rows()
            r[1]['signal'].update(changes)
            self.assertFalse(assess(r, 'a', {}, True, NOW)['qualified'])
        for activity in ({'fresh': False}, {'fresh': True, 'idleSeconds': 300}):
            self.assertFalse(assess(rows(), MODEL, activity, True, NOW)['qualified'])
        self.assertTrue(
            assess(rows(), MODEL, {'fresh': True, 'idleSeconds': 0}, True, NOW)['qualified']
        )

    def test_selected_local_models_only_and_opt_out(self):
        for key in ('selected', 'available'):
            r = rows()
            r[1][key] = False
            self.assertFalse(assess(r, 'a', {}, True, NOW)['qualified'])
        self.assertFalse(assess(rows(), 'a', {}, False, NOW)['qualified'])


class FallbackDecisionTests(unittest.TestCase):
    def test_failed_trial_returns_to_fallback_with_no_fake_profit(self):
        r = rows()
        r[2]['selected'] = False
        d = choose(r, failed=True, idle=120)
        self.assertEqual(d['target'], MODEL)
        self.assertEqual(d['kind'], 'explore')
        self.assertTrue(d['escapeReady'])
        self.assertEqual(d['fallback']['selection'], 'fallback')
        r = next(r for r in d['opportunities'] if r['model'] == MODEL)
        self.assertIsNone(r['netGainUsd'])
        self.assertIsNone(r['paybackMinutes'])
        self.assertEqual(r['selectionReason'], 'fallback')

    def test_higher_established_earnings_beat_fallback_after_failed_trial(self):
        d = choose(failed=True, estimates={'new': summary(0.5)})
        self.assertEqual(d['target'], 'new')
        self.assertIsNone(d['fallback']['selection'])

    def test_constant_fallback_traffic_does_not_crowd_out_untried_models(self):
        d = choose()
        self.assertEqual(d['target'], 'new')
        self.assertGreater(
            next(r for r in d['opportunities'] if r['model'] == MODEL)['pressureScore'],
            next(r for r in d['opportunities'] if r['model'] == 'new')['pressureScore'],
        )

    def test_no_alternative_uses_fallback_but_still_waits_for_idle_timeout(self):
        r = rows()
        r[2]['selected'] = False
        d = choose(r, idle=600)
        self.assertEqual(d['target'], MODEL)
        self.assertFalse(d['escapeReady'])
        self.assertEqual(d['fallback']['selection'], 'fallback')
        self.assertTrue(choose(r, idle=1200)['escapeReady'])

    def test_no_trigger_cannot_turn_fallback_into_an_earnings_switch(self):
        d = choose(idle=0)
        self.assertIsNone(d['target'])
        self.assertTrue(d['fallback']['qualified'])

    def test_unreliable_gpt_oss_is_still_an_ordinary_trial_candidate(self):
        r = rows()
        r[1]['fallbackEvidence'] = reliability(history(0), NOW)
        r[2]['selected'] = False
        d = choose(r)
        self.assertEqual(d['target'], MODEL)
        self.assertFalse(d['fallback']['qualified'])
        self.assertIsNone(d['fallback']['selection'])

    def test_disabled_preference_restores_pressure_ranking(self):
        d = choose(rules=policy({'fallbackEnabled': 0}))
        self.assertEqual(d['target'], MODEL)
        self.assertFalse(d['fallback']['enabled'])
        self.assertIsNone(d['fallback']['selection'])
        for v in (True, 2, -1, '1', None):
            with self.assertRaises(ValueError):
                policy({'fallbackEnabled': v})

    def test_fallback_does_not_bypass_model_memory_daily_budget_or_trial_guards(self):
        for change in ({'loadBudget': None}, {'selected': False}, {'available': False}):
            r = rows()
            r[1].update(change)
            self.assertNotEqual(choose(r, failed=True)['target'], MODEL)
        d = decide(
            rows(),
            'a',
            {'a': {**summary(0), 'recent': True, 'asOf': NOW - 120}},
            [],
            [{'at': NOW - i, 'downtime': 1} for i in range(policy()['maxSwitchesPerDay'])],
            policy(),
            NOW,
            activity={'fresh': True, 'idleSeconds': 1200},
        )
        self.assertIsNone(d['target'])
        self.assertFalse(d['fallback']['eligible'])
        trial = {'current': True, 'status': 'running', 'trialMinutes': 20, 'complete': False}
        d = decide(
            rows(),
            'a',
            {'a': {**summary(0), 'recent': True, 'asOf': NOW - 120}},
            [],
            [],
            policy(),
            NOW,
            activity={'fresh': True, 'idleSeconds': 1200},
            trial=trial,
        )
        self.assertIsNone(d['target'])

    def test_unproductive_fallback_trial_keeps_its_cooldown(self):
        run = {
            'at': NOW - 600,
            'model': MODEL,
            'downtime': 50,
            'decision': {'outcome': {'complete': True, 'cooldownUntil': NOW + 600}},
        }
        d = decide(
            rows(),
            'a',
            {'a': {**summary(0), 'recent': True, 'asOf': NOW - 120}},
            [],
            [run],
            policy(),
            NOW,
            activity={'fresh': True, 'idleSeconds': 1200},
            trial={'current': True, 'complete': True, 'status': 'no_traffic'},
        )
        self.assertNotEqual(d['target'], MODEL)
        self.assertIn('local retry', d['fallback']['holdReason'])

    def test_last_attempt_is_reserved_for_return_instead_of_another_speculative_trial(self):
        runs = [{'at': NOW - i, 'downtime': 10} for i in range(policy()['maxSwitchesPerDay'] - 1)]
        d = decide(
            rows(),
            'a',
            {'a': {**summary(0), 'recent': True, 'asOf': NOW - 120}},
            [],
            runs,
            policy(),
            NOW,
            activity={'fresh': True, 'idleSeconds': 1200},
        )
        self.assertIsNone(d['target'])
        self.assertIsNone(d['fallback']['selection'])
        self.assertIn(
            'possible return', next(r for r in d['opportunities'] if r['model'] == 'new')['reason']
        )

    def test_a_paying_current_model_remains_protected(self):
        d = choose(failed=True, idle=120, estimates={'a': {**summary(0.2), 'recentGuardRate': 0.3}})
        self.assertIsNone(d['target'])


if __name__ == '__main__':
    unittest.main()
