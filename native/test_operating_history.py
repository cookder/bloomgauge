import copy
import math
import unittest
from history import History
from optimizer_store import OptimizerStore
from energy import Energy
from concurrency_history import ConcurrencyHistory
from opportunity_lab import smoothing_comparison


class EnergyTests(unittest.TestCase):
    def setUp(self):
        self.h = History(':memory:')
        self.store = OptimizerStore(self.h)
        self.e = Energy(self.store)
        self.store.identity('mac', 'provider')
        self.e.configure(
            {
                'rate': 170.12 / 1257,
                'label': 'Fixture bill average',
                'expectedId': self.e.tariff()['id'],
            },
            0,
        )

    def tearDown(self):
        self.h.db.close()

    def sample(self, at, watts=60, source='AC Power', account='a', device='mac'):
        self.e.observe(account, device, {'at': at, 'systemWatts': watts, 'powerSource': source}, at)

    def run_minutes(self, start=600, end=720, **kwargs):
        for at in range(start, end + 1):
            self.sample(at, **kwargs)

    def report(self, start=600, end=720, now=1000):
        return self.e.report('a', 'mac', start, end, now)

    def test_integrates_real_time_and_bill_rate_without_fabricated_wall_loss(self):
        self.run_minutes()
        d = self.report()
        self.assertAlmostEqual(d['totals']['kwh'], 0.002)
        self.assertAlmostEqual(d['totals']['costUsd'], 0.002 * 170.12 / 1257)
        self.assertEqual(d['totals']['seconds'], 120)
        self.assertAlmostEqual(d['samples'][0]['watts'], 60)
        self.assertIsNone(d['latest'])
        self.assertIsNone(d['comparison']['afterCostUsd'])
        self.assertAlmostEqual(self.report(600.5, 720)['totals']['seconds'], 60)

    def test_gaps_duplicate_clock_reset_unknown_and_identity_break_integration(self):
        self.sample(600)
        self.sample(601, 120)
        self.sample(601, 120)
        self.sample(607, 120)
        self.sample(608, None)
        self.sample(609)
        self.sample(610, account='other')
        self.sample(611)
        self.sample(612)
        d = self.report()
        self.assertEqual(d['totals']['seconds'], 2)
        self.assertAlmostEqual(d['totals']['kwh'], 150 / 3600000)
        self.assertIsNone(d['samples'][0]['watts'])
        self.assertEqual(self.e.report('a', 'other', 600, 720, 1000)['totals']['seconds'], 0)

    def test_battery_has_energy_without_assumed_electricity_cost(self):
        self.run_minutes(source='Battery Power')
        d = self.report()
        self.assertAlmostEqual(d['totals']['kwh'], 0.002)
        self.assertIsNone(d['totals']['costUsd'])
        self.assertEqual(d['totals']['acSeconds'], 0)
        self.assertIsNone(d['comparison']['afterCostUsd'])

    def test_rate_change_preserves_recorded_cost_and_rejects_stale_invalid_edits(self):
        self.run_minutes(600, 660)
        old = self.e.tariff()
        self.e.configure({'rate': 0.2, 'label': 'New rate', 'expectedId': old['id']}, 660)
        self.run_minutes(660, 720)
        d = self.report()
        self.assertAlmostEqual(d['totals']['costUsd'], 0.001 * old['rate'] + 0.001 * 0.2)
        self.assertEqual(len(d['tariffs']), 2)
        with self.assertRaises(ValueError):
            self.e.configure({'rate': 0.2, 'label': 'Stale', 'expectedId': old['id']})
        for rate in (True, -1, float('nan'), 11):
            with self.assertRaises(ValueError):
                self.e.configure({'rate': rate, 'label': 'Bad', 'expectedId': 2})

    def test_income_comparison_needs_same_complete_settled_ac_minutes(self):
        self.run_minutes()
        self.h.db.execute('INSERT INTO opt_coverage VALUES(?,?,?)', ('a', 600, 660))
        for i, model, provider, at in [
            (1, 'model', 'provider', 630),
            (2, 'base_reward', '', 630),
            (3, 'model', 'foreign', 630),
            (4, 'model', 'provider', 690),
        ]:
            self.h.db.execute(
                'INSERT INTO opt_credits VALUES(?,?,?,?,?,?,?)',
                ('a', i, provider, at, model, 10000, 5),
            )
        self.h.db.commit()
        d = self.report()
        c = d['comparison']
        self.assertEqual(c['seconds'], 60)
        self.assertAlmostEqual(c['inferenceUsd'], 0.01)
        self.assertAlmostEqual(c['accountBaseUsd'], 0.01)
        self.assertAlmostEqual(c['afterCostUsd'], 0.02 - 0.001 * 170.12 / 1257)
        self.assertEqual(self.report(now=700)['comparison']['seconds'], 0)

    def test_invalid_ranges_and_stale_power(self):
        for start, end in [(0, float('inf')), (-1, 2), (10, 10), (1001, 1002)]:
            with self.assertRaises(ValueError):
                self.report(start, end)
        self.e.observe('a', 'mac', {'at': 600, 'systemWatts': 60, 'powerSource': 'AC Power'}, 607)
        self.assertIsNone(self.e.latest)


def concurrency(at=600, session=1, models=None):
    return {
        'status': 'ok',
        'data': {
            'observedSessionId': session,
            'requestedAt': at,
            'score': 0.95,
            'failedJobs': 2,
            'totalJobs': 20,
            'responseTimeMs': 100,
            'concurrency': {
                'at': at,
                'pending': 3,
                'limit': 0,
                'slots': [
                    {'model': 'a', 'running': 2, 'waiting': 0, 'limit': 4, 'state': 'running'}
                ],
            },
        },
        'session': {
            'id': session,
            'models': models or ['a'],
            'startedAt': 500,
            'status': 'active',
            'performance': {'status': 'counting', 'segmentStartedAt': 500},
        },
    }


class ConcurrencyHistoryTests(unittest.TestCase):
    def setUp(self):
        self.h = History(':memory:')
        self.c = ConcurrencyHistory(OptimizerStore(self.h))

    def tearDown(self):
        self.h.db.close()

    def add(self, d, account='a', device='mac', now=None):
        self.c.observe(account, device, d, now or d['data']['requestedAt'])

    def report(self, **kw):
        return self.c.report('a', 'mac', 500, 1200, 1300, **kw)

    def test_unique_heartbeat_zero_and_missing_are_preserved(self):
        d = concurrency()
        self.add(d)
        self.add(d)
        d = concurrency(630)
        d['data']['concurrency']['slots'][0]['waiting'] = None
        self.add(d)
        r = self.report()
        self.assertEqual(r['observations'], 2)
        self.assertEqual(r['latest']['providerLimit'], 0)
        self.assertIsNone(r['latest']['waiting'])
        self.assertEqual(r['samples'][0]['waiting'], 0)
        self.assertFalse(r['fresh'])

    def test_stale_cold_wrong_session_and_source_before_segment_do_not_record(self):
        for change in ('stale', 'cold', 'session', 'segment'):
            d = concurrency()
            if change == 'stale':
                d['status'] = 'stale'
            if change == 'cold':
                d['session']['performance']['status'] = 'paused'
            if change == 'session':
                d['data']['observedSessionId'] = 2
            if change == 'segment':
                d['session']['performance']['segmentStartedAt'] = 601
            self.add(d)
        self.add(concurrency(), now=670)
        self.assertEqual(self.report()['observations'], 0)

    def test_scoping_session_gaps_and_failure_counter_resets(self):
        self.add(concurrency(600))
        d = concurrency(630)
        d['data']['failedJobs'] = 3
        self.add(d)
        self.add(concurrency(660, 2))
        self.add(concurrency(1000, 2))
        self.add(concurrency(1100), account='other')
        r = self.report()
        self.assertEqual(r['observations'], 4)
        self.assertEqual(r['failureIncrements'], 1)
        self.assertGreaterEqual(sum(p['running'] is None for p in r['samples']), 2)
        self.assertEqual(self.report(session=2)['observations'], 2)
        self.assertEqual(self.report(model='missing')['observations'], 0)

    def test_pair_filter_cannot_attribute_provider_reservations_to_one_model(self):
        self.add(concurrency(models=['a', 'b']))
        r = self.report(model='a')
        self.assertEqual(r['latest']['running'], 2)
        self.assertIsNone(r['latest']['pending'])
        self.assertIsNone(r['latest']['providerLimit'])


class SmoothingTests(unittest.TestCase):
    def rows(self, values):
        return [
            {
                'at': 600 + i * 60,
                'models': [
                    {'id': 'a', 'score': v, 'available': True},
                    {'id': 'b', 'score': 1, 'available': True},
                ],
            }
            for i, v in enumerate(values)
        ]

    def test_elapsed_time_ema_reduces_brief_rank_reversals_without_claiming_money(self):
        result = smoothing_comparison(self.rows([2, 0.9, 2, 0.9, 2]))
        raw, fast, slow = result['variants']
        self.assertEqual(raw['leaderChanges'], 4)
        self.assertEqual(raw['reversals'], 3)
        self.assertEqual(fast['leaderChanges'], 0)
        self.assertEqual(slow['leaderChanges'], 0)
        self.assertTrue(result['passive'])

    def test_gaps_reset_and_sustained_new_leader_measures_delay(self):
        rows = self.rows([2] * 5 + [0.1] * 20)
        r = smoothing_comparison(rows)['variants']
        self.assertEqual(r[0]['meanFollowSeconds'], 0)
        self.assertGreater(r[1]['meanFollowSeconds'], 0)
        rows[-1]['at'] += 2000
        r = smoothing_comparison(rows)['variants'][1]
        self.assertEqual(r['resets'], 1)

    def test_missing_model_and_null_scores_do_not_hold_old_leader(self):
        rows = self.rows([2, 2])
        rows[1]['models'][0]['score'] = None
        r = smoothing_comparison(rows)['variants'][1]
        self.assertEqual(r['leader'], 'b')
        self.assertEqual(r['resets'], 1)


if __name__ == '__main__':
    unittest.main()
