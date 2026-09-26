"""Decision economics, evidence scope and durable attempt accounting."""

import unittest
from datetime import datetime
from history import History
from optimizer_store import OptimizerStore
from demand_optimizer import DemandOptimizer, POLICY, policy, decide, estimate, switch_seconds

NOW = datetime(2026, 9, 14, 12, 0).timestamp()


def minutes(rate, start, count=360):
    return [{'at': start + i * 60, 'seconds': 60, 'usd': rate / 60} for i in range(count)]


def summary(rate):
    return {
        'rate': rate,
        'lower': max(0, rate * 0.85),
        'upper': max(0, rate * 1.15),
        'hours': 30,
        'days': 4,
        'blocks': 30,
        'scope': 'weekday_time',
        'forecastUsable': True,
    }


def candidate(model):
    return {
        'id': model,
        'selected': True,
        'available': True,
        'evidence': {'eligible': True},
        'loadBudget': {'afterUnloadGB': 40, 'requiredGB': 30},
        'signal': {
            'status': 'spike',
            'load': 10,
            'pressure': 2,
            'coverage': 1,
            'observedAt': NOW - 30,
            'loadRatio': 3,
            'pressureRatio': 3,
        },
    }


def decision(old=0.15, new=0.5, changes=None, events=None, runs=None):
    rows = [candidate('a'), {**candidate('b'), **(changes or {})}]
    return decide(
        rows, 'a', {'a': summary(old), 'b': summary(new)}, events or [], runs or [], policy(), NOW
    )


class DemandPolicyTests(unittest.TestCase):
    def test_gain_charges_only_the_next_load(self):
        d = decision()
        self.assertEqual(d['target'], 'b')
        r = next(r for r in d['opportunities'] if r['model'] == 'b')
        self.assertEqual(r['outboundSeconds'], 120)
        self.assertEqual(r['returnSeconds'], 120)
        cost = 0.5 * 0.85 * 120 / 3600
        self.assertAlmostEqual(r['switchCostUsd'], cost)
        self.assertAlmostEqual(r['netGainUsd'], 0.5 * 0.85 - 0.15 * 1.15 - cost)
        self.assertGreater(r['paybackMinutes'], 0)

    def test_popular_poorly_paid_model_never_wins(self):
        signal = {**candidate('b')['signal'], 'load': 1e6, 'pressure': 1e6}
        self.assertIsNone(decision(0.2, 0.1, {'signal': signal})['target'])

    def test_realistic_load_time_allows_a_modest_improvement(self):
        self.assertEqual(decision(0.15, 0.25)['target'], 'b')

    def test_missing_evidence_memory_negative_and_unselected_models_do_not_win(self):
        for change in (
            {'loadBudget': None},
            {'available': False},
            {'selected': False},
            {'loadBudget': {'afterUnloadGB': 30.5, 'requiredGB': 30}},
        ):
            with self.subTest(change=change):
                self.assertIsNone(decision(changes=change)['target'])
        self.assertIsNone(decision(0.15, -0.1)['target'])

    def test_fresh_sustained_nonzero_warm_demand_required(self):
        for change in (
            {'status': 'watching'},
            {'status': 'stale'},
            {'observedAt': NOW - 91},
            {'observedAt': NOW + 1},
            {'coverage': 0.7},
            {'load': 0},
            {'pressure': None},
        ):
            with self.subTest(change=change):
                self.assertIsNone(
                    decision(changes={'signal': {**candidate('b')['signal'], **change}})['target']
                )

    def test_confirmed_zero_paid_pace_can_be_replaced(self):
        self.assertEqual(decision(0, 0.3)['target'], 'b')

    def test_attempt_and_downtime_budgets(self):
        self.assertIsNone(
            decision(
                runs=[{'at': NOW - i, 'downtime': 10} for i in range(POLICY['maxSwitchesPerDay'])]
            )['target']
        )
        self.assertIsNone(decision(runs=[{'at': NOW - 100, 'downtime': 1700}])['target'])
        self.assertEqual(decision(runs=[{'at': NOW - 86401, 'downtime': 3600}])['target'], 'b')
        self.assertIsNone(decision(runs=[{'at': NOW - 100, 'reservedSeconds': 1700}])['target'])

    def test_failure_cooldown_is_cleared_by_later_success(self):
        bad = {'at': NOW - 120, 'model': 'b', 'kind': 'recovered', 'downtime': 200}
        self.assertIsNone(decision(events=[bad])['target'])
        good = {'at': NOW - 60, 'model': 'b', 'kind': 'switched', 'downtime': 90}
        self.assertEqual(decision(events=[bad, good])['target'], 'b')
        self.assertEqual(decision(events=[{**bad, 'at': NOW - 21601}])['target'], 'b')

    def test_measured_cost_never_uses_future_events(self):
        events = [
            {'at': NOW - i - 1, 'kind': 'switched', 'model': 'b', 'downtime': s}
            for i, s in enumerate((50, 80, 100))
        ]
        seconds, scope = switch_seconds(events, 'b', NOW)
        self.assertEqual(scope, 'measured')
        self.assertEqual(seconds, 90)
        self.assertEqual(
            switch_seconds(
                events + [{'at': NOW + 60, 'kind': 'switched', 'model': 'b', 'downtime': 900}],
                'b',
                NOW,
            ),
            (seconds, scope),
        )
        self.assertEqual(switch_seconds(events[:1], 'b', NOW), (50, 'limited_history'))

    def test_control_bounds(self):
        self.assertEqual(policy(), POLICY)
        for rules in (
            {'minRunMinutes': 1},
            {'maxSwitchesPerDay': True},
            {'memoryHeadroomGB': 0},
            {'minimumNetUsd': float('nan')},
            {'ignoreIdle': True},
            [],
        ):
            with self.subTest(rules=rules), self.assertRaises(ValueError):
                policy(rules)
        self.assertEqual(policy({'minRunMinutes': 30})['minRunMinutes'], 30)


class DemandEstimateTests(unittest.TestCase):
    def test_fast_paid_burst_protects_current_model_before_thirty_minute_average_catches_up(self):
        e = {
            'hours': 7,
            'jobs': 200,
            'minutes': minutes(0.1, NOW - 86400)
            + minutes(0.1, NOW - 1920, 25)
            + minutes(0.8, NOW - 420, 5),
        }
        r = estimate(e, {}, {}, NOW, NOW - 3600)
        self.assertGreaterEqual(r['upper'], 0.8 * 1.15 - 0.000001)
        self.assertAlmostEqual(r['recentGuardRate'], 0.8)
        rows = [candidate('a'), candidate('b')]
        self.assertIsNone(
            decide(rows, 'a', {'a': r, 'b': summary(0.5)}, [], [], policy(), NOW)['target']
        )

    def test_old_candidate_data_is_not_treated_as_current_evidence(self):
        rows = [candidate('a'), candidate('b')]
        result = decide(
            rows,
            'a',
            {'a': summary(0.1), 'b': {**summary(1), 'asOf': NOW - 8 * 86400}},
            [],
            [],
            policy(),
            NOW,
        )
        self.assertIsNone(result['target'])
        self.assertIn(
            'seven days', next(r for r in result['opportunities'] if r['model'] == 'b')['reason']
        )

    def test_pressure_never_multiplies_or_extrapolates_earnings(self):
        e = {'hours': 6, 'jobs': 20, 'minutes': minutes(0.1, NOW - 86400)}
        a = estimate(e, {}, {'pressure': 1, 'load': 1}, NOW)
        b = estimate(e, {}, {'pressure': 10000, 'load': 10000}, NOW)
        self.assertEqual(a, b)
        self.assertAlmostEqual(a['rate'], 0.1)
        self.assertFalse(b['demandMatched'])

    def test_current_session_burst_overrides_old_average(self):
        e = {
            'hours': 6.5,
            'jobs': 200,
            'minutes': minutes(0.1, NOW - 86400) + minutes(0.6, NOW - 1920, 30),
        }
        r = estimate(e, {}, {}, NOW, NOW - 3600)
        self.assertTrue(r['recent'])
        self.assertAlmostEqual(r['rate'], 0.6)
        self.assertGreater(r['upper'], 0.6)
        self.assertFalse(estimate(e, {}, {}, NOW, NOW - 600)['recent'])

    def test_future_unpaid_minutes_never_inflate_estimate(self):
        e = {
            'hours': 6,
            'jobs': 100,
            'minutes': minutes(0.1, NOW - 86400) + minutes(1000, NOW - 60, 60),
        }
        r = estimate(e, {}, {}, NOW, NOW - 3600)
        self.assertAlmostEqual(r['rate'], 0.1)
        self.assertFalse(r['recent'])

    def test_measured_idle_current_session_does_not_inherit_old_paid_rate(self):
        e = {
            'hours': 7,
            'jobs': 100,
            'minutes': minutes(0.8, NOW - 86400) + minutes(0, NOW - 1920, 30),
        }
        r = estimate(e, {}, {}, NOW, NOW - 3600)
        self.assertTrue(r['recent'])
        self.assertEqual(r['upper'], 0)

    def test_time_and_observed_demand_matching(self):
        matching = minutes(0.4, NOW - 7 * 86400 - 3600, 120) + minutes(
            0.4, NOW - 14 * 86400 - 3600, 120
        )
        other = minutes(0.05, NOW - 2 * 86400 - 8 * 3600, 360)
        n = {int(m['at'] // 60) * 60: {'pressure': 2, 'load': 10, 'warm': 5} for m in matching}
        e = {'hours': 10, 'jobs': 200, 'minutes': sorted(matching + other, key=lambda m: m['at'])}
        r = estimate(e, n, {'pressure': 2, 'load': 10}, NOW)
        self.assertEqual(r['scope'], 'weekday_time')
        self.assertTrue(r['demandMatched'])
        self.assertAlmostEqual(r['rate'], 0.4)


class DemandStorageTests(unittest.TestCase):
    def setUp(self):
        self.h = History(':memory:')
        self.store = OptimizerStore(self.h)
        self.auto = DemandOptimizer(self.h, self.store)

    def tearDown(self):
        self.h.close()

    def test_durable_scoped_attempt_and_single_completion(self):
        d = decision()
        run = self.auto.begin('account', 'device', d, NOW)
        self.assertEqual(len(self.auto.runs('account', 'device', NOW)), 1)
        self.assertEqual(self.auto.runs('other', 'device', NOW), [])
        self.assertEqual(self.auto.runs('account', 'other', NOW), [])
        self.auto.finish(run, 'other', 'device', NOW + 70, 'switched', 70)
        self.assertEqual(self.auto.runs('account', 'device', NOW + 100)[0]['result'], 'starting')
        self.auto.finish(run, 'account', 'device', NOW + 80, 'switched', 80)
        self.auto.finish(run, 'account', 'device', NOW + 90, 'failed', 90)
        r = DemandOptimizer(self.h, self.store).runs('account', 'device', NOW + 100)[0]
        self.assertEqual(r['result'], 'switched')
        self.assertEqual(r['downtime'], 80)
        self.assertEqual(r['decision']['candidate']['model'], 'b')
        with self.assertRaises(ValueError):
            self.auto.begin('account', 'device', d, NOW + 31)

    def test_actual_qualified_evidence_ignores_other_devices_and_cold_minutes(self):
        self.store.identity('device', 'provider')
        start = NOW - 2 * 86400
        with self.h.lock:
            self.h.db.execute('INSERT INTO opt_coverage VALUES(?,?,?)', ('account', start, NOW))
            for model, device, seconds in [
                ('a', 'device', 60),
                ('b', 'foreign', 60),
                ('c', 'device', 30),
            ]:
                self.h.db.executemany(
                    'INSERT INTO opt_ready_minutes VALUES(?,?,?,?,?,?,?,?)',
                    [
                        ('account', device, start + i * 60, model, seconds, 1, 10, 1)
                        for i in range(360)
                    ],
                )
            self.h.db.execute(
                'INSERT INTO opt_credits VALUES(?,?,?,?,?,?,?)',
                ('account', 1, 'provider', start + 1, 'a', 100000, 10),
            )
            self.h.db.execute(
                'INSERT INTO opt_credits VALUES(?,?,?,?,?,?,?)',
                ('account', 2, 'foreign-provider', start + 1, 'a', 999000000, 10),
            )
            self.h.db.commit()
        e, _ = self.auto.evidence('account', 'device', ['a', 'b', 'c'], NOW)
        self.assertAlmostEqual(e['a']['usd'], 0.1)
        self.assertNotIn('b', e)
        self.assertNotIn('c', e)
        self.assertEqual(self.auto.evidence('foreign-account', 'device', ['a'], NOW)[0], {})

    def test_precommit_budget_cannot_be_reused_from_an_older_preview(self):
        d = decision()
        first = self.auto.begin('account', 'device', d, NOW)
        with self.assertRaisesRegex(ValueError, 'budget changed'):
            self.auto.begin('account', 'device', d, NOW)
        self.auto.finish(first, 'account', 'device', NOW, 'switched', 30)
        for _ in range(POLICY['maxSwitchesPerDay'] - 1):
            row = self.auto.begin('account', 'device', d, NOW)
            self.auto.finish(row, 'account', 'device', NOW, 'switched', 30)
        with self.assertRaisesRegex(ValueError, 'budget changed'):
            self.auto.begin('account', 'device', d, NOW)


if __name__ == '__main__':
    unittest.main()
