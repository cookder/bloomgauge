"""Regressions for leaving productive Gemma on lagged shortfall evidence."""

import copy
import unittest
from demand_targets import fresh_paid, target_status, high_earnings
from demand_optimizer import decide, policy
from test_demand_optimizer import NOW, candidate, summary
from test_demand_targets import evidence


def live(rate=0.113):
    raw = {'advertised_models': ['gemma'], 'started_at': NOW - 7200, 'written_at': NOW - 1}
    session = {
        'id': 65,
        'models': ['gemma'],
        'status': 'active',
        'performance': {'status': 'counting'},
        'startedAt': NOW - 7200,
        'providerStartedAt': NOW - 7200,
        'lastSeenAt': NOW - 1,
    }
    pulse = {
        'sessionId': 65,
        'models': ['gemma'],
        'status': 'live',
        'at': NOW - 1,
        'updatedAt': NOW - 5,
        'windows': {
            '300': {'start': NOW - 305, 'end': NOW - 5, 'seconds': 300, 'ratePerHour': rate}
        },
    }
    return pulse, session, raw


# These regressions protect a productive Gemma at the old $0.09 floor, now the user's protect level.
FLOOR = policy({'protectUsdPerHour': 0.09})


def select(settled=0.095, current=0.113, predicted=None, idle=0, trial=None, rules=FLOOR):
    p, s, r = live(current)
    goal = target_status(
        evidence(settled), NOW - 7200, NOW, rules, live_paid=fresh_paid(p, s, r, 'gemma', NOW)
    )
    rows = [candidate('gemma'), candidate('qwen')]
    for row in rows:
        row['signal']['sustained'] = {
            'qualified': True,
            'pressure': 1.51,
            'seconds': 600,
            'samples': 20,
            'sourceAt': NOW - 30,
        }
    rows[0]['earningsTarget'] = goal
    # The reported switch had normal, falling concurrency and no usable forecast.
    rows[1]['signal'].update(status='normal', load=139, pressure=1.51)
    rates = {'gemma': {**summary(settled), 'recent': True, 'recentGuardRate': settled}}
    if predicted is not None:
        rates['qwen'] = predicted
    return decide(
        rows,
        'gemma',
        rates,
        [],
        [],
        rules,
        NOW,
        activity={'fresh': True, 'idleSeconds': idle},
        trial=trial,
    )


class ProductiveSwitchTests(unittest.TestCase):
    def test_reported_gemma_qwen_dispatch_is_prevented(self):
        d = select(predicted={**summary(0.059683), 'forecastUsable': False})
        self.assertIsNone(d['target'])
        self.assertFalse(d['earningsTarget']['ready'])
        self.assertIn(d['earningsTarget']['status'], ('productive', 'high_earnings'))
        self.assertIn('protect level', d['reason'])

    def test_slightly_below_goal_is_not_itself_an_experiment(self):
        for rate in (0.09, 0.095, 0.10, 0.105, 0.119):
            with self.subTest(rate=rate):
                self.assertIsNone(select(rate, rate)['target'])

    def test_recovery_is_checked_before_lagged_history_catches_up(self):
        d = select(0.04, 0.113)
        self.assertIsNone(d['target'])
        self.assertEqual(d['earningsTarget']['livePaid']['rate'], 0.113)

    def test_new_paid_credits_override_an_older_unpaid_trial_result(self):
        d = select(0, 0.113, trial={'current': True, 'complete': True, 'status': 'no_paid_work'})
        self.assertIsNone(d['target'])
        self.assertIsNone(d['explorationTrigger'])

    def test_higher_paid_forecast_still_switches_from_productive_work(self):
        d = select(predicted=summary(0.25))
        self.assertEqual(d['target'], 'qwen')
        self.assertEqual(d['kind'], 'earnings')

    def test_recovery_also_protects_earnings_gain_comparison(self):
        d = select(0.095, 0.4, summary(0.25))
        self.assertIsNone(d['target'])
        self.assertEqual(d['baseline']['upper'], 0.4)

    def test_substantial_low_pay_keeps_unknown_and_sparse_zero_candidates_eligible(self):
        for predicted in (None, {**summary(0), 'forecastUsable': False}):
            d = select(0.04, 0.04, predicted)
            self.assertEqual(d['target'], 'qwen')
            self.assertTrue(d['escapeReady'])
            self.assertEqual(d['kind'], 'explore')
            self.assertIsNone(d['opportunities'][0]['netGainUsd'])

    def test_known_lower_paid_forecast_cannot_hide_inside_a_demand_trial(self):
        d = select(0.07, 0.07, summary(0.03))
        self.assertIsNone(d['target'])
        self.assertIn(
            'does not support',
            next(r for r in d['opportunities'] if r['model'] == 'qwen')['reason'],
        )

    def test_real_idle_escape_is_not_disabled_by_earlier_profitable_minutes(self):
        d = select(0.10, 0, idle=1200)
        self.assertEqual(d['target'], 'qwen')
        self.assertTrue(d['escapeReady'])
        self.assertEqual(d['explorationTrigger'], 'idle')

    def test_default_protect_level_lets_learning_measure_below_twenty_cents(self):
        d = select(0.113, 0.113, rules=policy())
        self.assertEqual(d['target'], 'qwen')
        self.assertEqual(d['explorationTrigger'], 'earnings_target')
        self.assertIn('protect level', d['reason'])

    def test_learning_time_off_blocks_learning_runs(self):
        d = select(0.04, 0.04, rules=policy({'learningMinutesPerDay': 0}))
        self.assertIsNone(d['target'])
        self.assertIn(
            'Learning time is off',
            next(r for r in d['opportunities'] if r['model'] == 'qwen')['reason'],
        )

    def test_missing_fresh_credits_cannot_assert_a_shortfall_trial(self):
        g = target_status(evidence(0.04), NOW - 7200, NOW, policy(), live_paid={'fresh': False})
        self.assertFalse(g['ready'])
        self.assertEqual(g['status'], 'stale')


class FreshPaidTests(unittest.TestCase):
    def test_exact_session_full_warm_window_is_accepted(self):
        p, s, r = live()
        v = fresh_paid(p, s, r, 'gemma', NOW)
        self.assertTrue(v['fresh'])
        self.assertEqual(v['rate'], 0.113)

    def test_old_model_session_and_restarted_provider_are_rejected(self):
        for kind, key, value in [
            ('pulse', 'sessionId', 64),
            ('pulse', 'models', ['qwen']),
            ('session', 'providerStartedAt', NOW - 4000),
            ('session', 'status', 'ended'),
            ('raw', 'advertised_models', ['qwen']),
        ]:
            p, s, r = live()
            {'pulse': p, 'session': s, 'raw': r}[kind][key] = value
            with self.subTest(kind=kind, key=key):
                self.assertFalse(fresh_paid(p, s, r, 'gemma', NOW)['fresh'])

    def test_stale_feed_or_unready_model_is_not_a_recovery(self):
        for kind, key, value in [
            ('pulse', 'at', NOW - 16),
            ('pulse', 'updatedAt', NOW - 46),
            ('pulse', 'status', 'stale'),
            ('pulse', 'at', NOW + 1),
            ('session', 'performance', {'status': 'paused'}),
            ('raw', 'written_at', NOW - 16),
        ]:
            p, s, r = live()
            {'pulse': p, 'session': s, 'raw': r}[kind][key] = value
            with self.subTest(kind=kind, key=key):
                self.assertFalse(fresh_paid(p, s, r, 'gemma', NOW)['fresh'])

    def test_partial_warm_gap_future_source_and_bad_rates_are_rejected(self):
        for change in (
            {'seconds': 239},
            {'end': NOW - 61},
            {'end': NOW + 1},
            {'ratePerHour': float('nan')},
            {'ratePerHour': float('inf')},
            {'ratePerHour': float('-inf')},
            {'ratePerHour': True},
            {'seconds': -1},
            {'start': -1},
            {'end': -1},
            {'seconds': 301},
            {'start': NOW - 100},
        ):
            p, s, r = live()
            p['windows']['300'].update(change)
            with self.subTest(change=change):
                self.assertFalse(fresh_paid(p, s, r, 'gemma', NOW)['fresh'])

    def test_signed_fresh_rate_revokes_a_supported_high_earnings_hold(self):
        p, s, r = live(-0.02)
        paid = fresh_paid(p, s, r, 'gemma', NOW)
        self.assertTrue(paid['fresh'])
        self.assertEqual(paid['rate'], -0.02)
        self.assertEqual(paid['seconds'], 300)
        h = evidence(0.25)
        self.assertTrue(high_earnings(h, NOW - 7200, NOW, {'fresh': False})['active'])
        self.assertFalse(high_earnings(h, NOW - 7200, NOW, paid)['active'])
        self.assertFalse(
            target_status(h, NOW - 7200, NOW, policy(), live_paid=paid)['highEarnings']['active']
        )

    def test_signed_fresh_rate_still_requires_fresh_matching_session_proof(self):
        for change in ('stale', 'session', 'cold', 'partial'):
            p, s, r = live(-0.02)
            if change == 'stale':
                p['updatedAt'] = NOW - 46
            if change == 'session':
                p['sessionId'] += 1
            if change == 'cold':
                s['performance']['status'] = 'paused'
            if change == 'partial':
                p['windows']['300']['seconds'] = 239
            paid = fresh_paid(p, s, r, 'gemma', NOW)
            with self.subTest(change=change):
                self.assertFalse(paid['fresh'])
                self.assertTrue(high_earnings(evidence(0.25), NOW - 7200, NOW, paid)['active'])

    def test_missing_readings_are_unknown(self):
        self.assertFalse(fresh_paid(None, None, None, 'gemma', NOW)['fresh'])


if __name__ == '__main__':
    unittest.main()
