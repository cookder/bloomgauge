"""Shadow pressure-curve estimator and its journal (optimizer redesign Phase 2)."""

import json
import unittest
from datetime import datetime
from history import History
from optimizer_store import OptimizerStore
import demand_curves as curves
from demand_curve_journal import CurveJournal

NOW = int(datetime(2026, 9, 25, 12, 0).timestamp()) // 1800 * 1800


def minute(at, usd=0.0, seconds=60):
    return {
        'at': at,
        'usd': usd,
        'seconds': seconds,
        'paidJobs': 1 if usd else 0,
        'requests': 1,
        'tokens': 60,
        'busy': 30,
    }


def signal(load, warm, pressure=None, age=30, status='normal'):
    return {
        'observedAt': NOW - age,
        'status': status,
        'coverage': 1,
        'load': load,
        'warmProviders': warm,
        'pressure': load / warm if pressure is None else pressure,
        'active': load,
    }


def history(pressures, rate, start, days=6):
    """Half-hour runs at each pressure on several days, paying rate(pressure) $/h."""
    minutes, network = [], {}
    for day in range(days):
        for i, pressure in enumerate(pressures):
            base = int(start + day * 86400 + i * 7200) // 1800 * 1800
            for k in range(30):
                at = base + k * 60
                minutes.append(minute(at, rate(pressure) / 60))
                network[at] = {'pressure': pressure}
    return {'minutes': minutes}, network


class SteadyTests(unittest.TestCase):
    def test_ramp_before_first_paid_minute_is_excluded(self):
        rows = [minute(i * 60) for i in range(5)] + [minute(i * 60, 0.01) for i in range(5, 10)]
        self.assertEqual([m['at'] for m in curves.steady(rows)], [i * 60 for i in range(5, 10)])

    def test_ramp_is_capped_at_fifteen_minutes(self):
        rows = [minute(i * 60) for i in range(20)]
        self.assertEqual([m['at'] for m in curves.steady(rows)], [i * 60 for i in range(15, 20)])

    def test_gap_over_five_minutes_starts_a_new_ramp(self):
        rows = [minute(0, 0.01), minute(60), minute(240), minute(700), minute(760, 0.01)]
        self.assertEqual([m['at'] for m in curves.steady(rows)], [0, 60, 240, 760])


class PeriodTests(unittest.TestCase):
    def test_half_hour_needs_ten_joined_minutes_and_settlement(self):
        rows = [minute(NOW - 3600 + i * 60, 0.002) for i in range(40)]
        network = {m['at']: {'pressure': 0.5} for m in rows[:9] + rows[30:39]}
        self.assertEqual(curves.periods(rows, network, NOW), [])
        network.update({m['at']: {'pressure': 1.0} for m in rows[9:12]})
        found = curves.periods(rows, network, NOW)
        self.assertEqual(len(found), 1)
        self.assertAlmostEqual(found[0]['usdPerHour'], 0.12)
        self.assertAlmostEqual(found[0]['pressure'], (9 * 0.5 + 3 * 1.0) / 12)
        # A minute inside the two-minute settlement lag never joins.
        network.update({m['at']: {'pressure': 0.5} for m in rows[39:]})
        self.assertEqual(len(curves.periods(rows, network, NOW)), 2)
        self.assertEqual(len(curves.periods(rows, network, rows[-1]['at'] + 60 + 119)), 1)


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.rate = lambda p: 0.1 * p**0.8
        a, na = history([0.2, 0.5, 1.0, 1.5], self.rate, NOW - 7 * 86400)
        b, nb = history([0.3, 0.6], lambda p: 0.03 * p, NOW - 7 * 86400 + 1800)
        self.evidence, self.network = {'a': a, 'b': b}, {'a': na, 'b': nb}

    def test_prediction_follows_pressure(self):
        low = curves.build(self.evidence, self.network, {'a': signal(4, 10)}, 'a', NOW)['a']
        high = curves.build(self.evidence, self.network, {'a': signal(12, 10)}, 'a', NOW)['a']
        self.assertEqual(low['basis'], 'measured')
        self.assertAlmostEqual(low['usdPerHour'], self.rate(0.4), delta=0.01)
        self.assertGreater(high['usdPerHour'], 2 * low['usdPerHour'])
        self.assertLessEqual(low['lower'], low['usdPerHour'])
        self.assertGreaterEqual(low['upper'], low['usdPerHour'])

    def test_model_not_serving_counts_this_mac_as_a_warm_provider(self):
        rows = curves.build(
            self.evidence, self.network, {'a': signal(9, 9), 'b': signal(9, 9)}, 'b', NOW
        )
        self.assertAlmostEqual(rows['a']['pressure'], 0.9)
        self.assertFalse(rows['a']['serving'])
        self.assertAlmostEqual(rows['b']['pressure'], 1.0)
        self.assertTrue(rows['b']['serving'])

    def test_never_run_model_gets_a_wide_prior(self):
        rows = curves.build(
            self.evidence, self.network, {'a': signal(5, 10), 'new': signal(5, 9)}, 'a', NOW
        )
        new = rows['new']
        self.assertEqual(new['basis'], 'prior')
        self.assertIsNotNone(new['usdPerHour'])
        self.assertGreater(
            new['upper'] / max(new['lower'], 1e-9), rows['a']['upper'] / rows['a']['lower']
        )

    def test_stale_or_missing_readings_give_no_prediction(self):
        for s in (
            signal(5, 10, age=200),
            signal(5, 10, status='stale'),
            {**signal(5, 10), 'coverage': 0.5},
            {},
        ):
            row = curves.build(self.evidence, self.network, {'a': s}, 'a', NOW)['a']
            self.assertIsNone(row['usdPerHour'])
            self.assertIsNone(row['pressure'])

    def test_extreme_pressure_saturates_and_is_flagged(self):
        spike = curves.build(self.evidence, self.network, {'a': signal(80, 10)}, 'a', NOW)['a']
        cap = curves.build(self.evidence, self.network, {'a': signal(20, 10)}, 'a', NOW)['a']
        self.assertTrue(spike['extrapolated'])
        self.assertAlmostEqual(spike['usdPerHour'], cap['usdPerHour'])
        self.assertGreater(spike['upper'], cap['upper'])

    def test_recent_pay_above_the_curve_carries_forward(self):
        base = curves.build(self.evidence, self.network, {'a': signal(5, 10)}, 'a', NOW)['a']
        start = int(NOW - 2000) // 60 * 60
        for k in range(25):
            self.evidence['a']['minutes'].append(minute(start + k * 60, 3 * self.rate(0.5) / 60))
            self.network['a'][start + k * 60] = {'pressure': 0.5}
        boosted = curves.build(self.evidence, self.network, {'a': signal(5, 10)}, 'a', NOW)['a']
        self.assertGreater(boosted['recentAdjustment'], 0)
        self.assertGreater(boosted['usdPerHour'], 1.5 * base['usdPerHour'])

    def test_pairs_are_not_estimated_and_empty_history_gives_nothing(self):
        combo = '@combo:["a","b"]'
        rows = curves.build(
            {**self.evidence, combo: self.evidence['a']},
            self.network,
            {'a': signal(5, 10)},
            combo,
            NOW,
        )
        self.assertNotIn(combo, rows)
        self.assertTrue(rows['a']['serving'])
        self.assertEqual(curves.build({}, {}, {'a': signal(5, 10)}, 'a', NOW), {})


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.h = History(':memory:')
        self.s = OptimizerStore(self.h)
        self.s.identity('mac', 'provider')
        self.h.db.execute('INSERT INTO opt_coverage VALUES(?,?,?)', ('owner', 0, NOW + 86400))
        self.journal = CurveJournal(self.s, enabled=True)
        self.credit = 0

    def tearDown(self):
        self.h.close()

    def run_model(self, start, count, usd_per_minute, model='a', pressure=(5, 0, 10)):
        for k in range(count):
            at = int(start) + k * 60
            self.h.db.execute(
                'INSERT OR REPLACE INTO opt_ready_minutes VALUES(?,?,?,?,?,?,?,?)',
                ('owner', 'mac', at, model, 60, 1, 60, 30),
            )
            for offset in (5, 35):
                self.h.db.execute(
                    'INSERT OR REPLACE INTO opt_network VALUES(?,?,?,?,?,?)',
                    (at + offset, model, *pressure, 20),
                )
            if usd_per_minute:
                self.credit += 1
                self.h.db.execute(
                    'INSERT INTO opt_credits VALUES(?,?,?,?,?,?,?)',
                    (
                        'owner',
                        self.credit,
                        'provider',
                        at + 30,
                        model,
                        round(usd_per_minute * 1e6),
                        60,
                    ),
                )

    def seed(self):
        for day in range(1, 5):
            self.run_model(NOW - day * 86400, 60, 0.002)

    def test_records_once_per_checkpoint_with_the_matched_rate(self):
        self.seed()
        self.journal.record('owner', 'mac', NOW + 10, {'a': signal(5, 10, age=10)}, 'a', None)
        self.journal.record('owner', 'mac', NOW + 70, {'a': signal(50, 10)}, 'a', None)
        rows = self.h.db.execute('SELECT checkpoint,data FROM demand_curve_observations').fetchall()
        self.assertEqual(len(rows), 1)
        packet = json.loads(rows[0]['data'])
        self.assertEqual(packet['origin'], NOW + 60)
        self.assertEqual(packet['targetEnd'], NOW + 60 + 1800)
        row = packet['models']['a']
        self.assertAlmostEqual(row['usdPerHour'], 0.12, delta=0.01)
        self.assertAlmostEqual(row['matched']['usdPerHour'], 0.12, delta=0.001)
        self.assertEqual(self.journal.latest('owner', 'mac', NOW + 100)['checkpoint'], NOW)

    def test_scores_the_next_half_hour_of_steady_pay(self):
        self.seed()
        self.journal.record('owner', 'mac', NOW + 10, {'a': signal(5, 10, age=20)}, 'a', None)
        pending = self.journal.evaluation('owner', 'mac', NOW + 600)
        self.assertEqual(pending['models'][0]['pendingWindows'], 1)
        self.run_model(NOW + 60, 30, 0.003)
        result = self.journal.evaluation('owner', 'mac', NOW + 60 + 1800 + 121 + 300)
        self.assertEqual(result['overall']['windows'], 1)
        self.assertEqual(result['serving']['windows'], 1)
        self.assertAlmostEqual(
            result['overall']['curveMAE'],
            abs(
                0.18
                - json.loads(
                    self.h.db.execute('SELECT data FROM demand_curve_observations').fetchone()[0]
                )['models']['a']['usdPerHour']
            ),
            places=4,
        )
        self.assertEqual(result['overall']['pairedWindows'], 1)
        self.assertEqual(result['status'], 'collecting')

    def test_short_or_ramping_runs_are_not_scored(self):
        self.seed()
        self.journal.record('owner', 'mac', NOW + 10, {'a': signal(5, 10, age=20)}, 'a', None)
        self.run_model(NOW + 60, 15, 0)  # unpaid ramp
        self.run_model(NOW + 60 + 900, 15, 0.003)  # 15 steady minutes, below the 20 needed
        result = self.journal.evaluation('owner', 'mac', NOW + 4000)
        self.assertEqual(result['overall']['windows'], 0)

    def test_prunes_old_rows_and_disabled_journal_is_inert(self):
        self.seed()
        self.journal.record('owner', 'mac', NOW + 10, {'a': signal(5, 10, age=20)}, 'a', None)
        self.journal.record(
            'owner', 'mac', NOW + 310, {'a': signal(5, 10, age=20 - 300)}, 'a', None
        )
        self.assertEqual(
            self.h.db.execute('SELECT COUNT(*) FROM demand_curve_observations').fetchone()[0], 2
        )
        self.journal.prune(NOW + 2 * 86400)
        self.assertEqual(
            [r[0] for r in self.h.db.execute('SELECT checkpoint FROM demand_curve_observations')],
            [NOW],
        )
        self.journal.prune(NOW + 46 * 86400)
        self.assertEqual(
            self.h.db.execute('SELECT COUNT(*) FROM demand_curve_observations').fetchone()[0], 0
        )
        off = CurveJournal(self.s, enabled=False)
        off.record('owner', 'mac', NOW + 900, {'a': signal(5, 10)}, 'a', None)
        self.assertIsNone(off.latest('owner', 'mac', NOW + 900))
        self.assertEqual(off.evaluation('owner', 'mac', NOW + 900)['status'], 'disabled')


if __name__ == '__main__':
    unittest.main()
