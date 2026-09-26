import copy
import json
import math
import sqlite3
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from predictive_lab import (
    STEP,
    VERSION,
    PredictiveLab,
    series,
    predict,
    portfolio,
    evaluation,
    income_scenario,
)


class PredictiveTests(unittest.TestCase):
    def points(self, days=8):
        return {
            t: {
                'pressure': 1 + 0.5 * math.sin(2 * math.pi * t / 86400),
                'warm': 10.0,
                'active': 10.0,
                'queued': 0.0,
            }
            for t in range(1728000000, 1728000000 + days * 86400 + STEP, STEP)
        }

    def test_training_never_sees_future_targets(self):
        p = self.points()
        origin = max(p) - 86400
        a = predict(p, origin, 12)
        for t in p:
            if t > origin:
                p[t]['pressure'] = 1000000
        self.assertEqual(a, predict(p, origin, 12))
        self.assertLessEqual(a['trainedThrough'], origin)
        self.assertIsNotNone(a['prediction'])

    def test_sparse_or_gapped_data_does_not_become_zero(self):
        p = self.points(1)
        at = max(p)
        self.assertIsNone(predict(p, at, 12)['prediction'])
        del p[at - STEP]
        self.assertIsNone(predict(p, at, 12))

    def test_constant_series_remains_constant(self):
        p = self.points()
        for v in p.values():
            v['pressure'] = 2.5
        self.assertAlmostEqual(predict(p, max(p), 12)['prediction'], 2.5)

    def test_bucket_coverage_deduplicates_polls_and_does_not_fill_gaps(self):
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        db.execute(
            'CREATE TABLE opt_network(at INTEGER,model TEXT,active REAL,queued REAL,warm REAL,routable REAL)'
        )
        for t in list(range(300, 600, 30)) + list(range(601, 608)):
            db.execute('INSERT INTO opt_network VALUES(?,?,?,?,?,?)', (t, 'a', 2, 0, 1, 1))
        store = SimpleNamespace(h=SimpleNamespace(db=db, lock=threading.RLock()))
        self.assertEqual(list(series(store, 300, 900)['a']), [600])
        self.assertEqual(series(store, 300, 599), {})
        db.close()

    def test_prospective_accuracy_requires_mature_complete_same_targets(self):
        p = self.points()
        at = max(p) - 3600
        packet = {
            'origin': at,
            'models': [
                {'id': 'a', 'forecasts': [{'minutes': 60, 'prediction': 2.0, 'baseline': 1.0}]}
            ],
        }
        self.assertEqual(evaluation([packet], {'a': p}, at + 3600)[1]['windows'], 0)
        self.assertEqual(evaluation([packet, packet], {'a': p}, at + 3720)[1]['windows'], 1)
        del p[at + STEP]
        self.assertEqual(evaluation([packet], {'a': p}, at + 3720)[1]['windows'], 0)

    def test_pair_variance_is_unknown_without_repeated_joint_history(self):
        key = '@combo:["a","b"]'
        e = {
            key: {
                'hours': 0.25,
                'usd': 0.01,
                'usdPerHour': 0.04,
                'perModel': {'a': {'usd': 0.004}, 'b': {'usd': 0.006}},
                'minutes': [{'at': i * 60, 'usd': 0.01 / 15} for i in range(15)],
            }
        }
        row = portfolio(e)[0]
        self.assertEqual(row['members'], ['a', 'b'])
        self.assertEqual(row['blocks'], 1)
        self.assertIsNone(row['stddevUSDPerHour'])
        self.assertEqual(row['status'], 'limited')
        self.assertAlmostEqual(sum(m['usd'] for m in row['perModel'].values()), row['usd'])

    def test_missing_income_is_unknown_not_zero_or_sum_of_solo_rates(self):
        self.assertIsNone(income_scenario({}, {}, None, 10, 10000))
        s = income_scenario({}, {}, 2, 10, 10000)
        self.assertIsNone(s['usdPerWarmHour'])
        self.assertEqual(s['status'], 'limited')

    def test_record_is_immutable_and_scoped(self):
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        store = SimpleNamespace(
            h=SimpleNamespace(db=db, lock=threading.RLock()), evidence=lambda *args: {}
        )
        lab = PredictiveLab(store, True)
        p = self.points()
        at = max(p)
        with (
            patch('predictive_lab.series', return_value={'a': p}),
            patch('predictive_lab.network_minutes', return_value={}),
            patch('predictive_lab.read_view') as view,
        ):
            view.return_value.__enter__.return_value = store
            lab.record('account', 'device', at + 1, ['a'])
            original = db.execute('SELECT data FROM predictive_observations').fetchone()[0]
            lab.next_record = 0
            lab.record('account', 'device', at + 2, [])
            self.assertEqual(
                db.execute('SELECT data FROM predictive_observations').fetchone()[0], original
            )
            lab.next_record = 0
            lab.record('different', 'device', at + 2, [])
            self.assertEqual(
                db.execute('SELECT COUNT(*) FROM predictive_observations').fetchone()[0], 2
            )
        db.close()

    def test_beta_disabled_does_not_create_table(self):
        db = sqlite3.connect(':memory:')
        lab = PredictiveLab(SimpleNamespace(h=SimpleNamespace(db=db)), False)
        lab.record('a', 'b', 0, [])
        self.assertEqual(db.execute('SELECT COUNT(*) FROM sqlite_master').fetchone()[0], 0)
        self.assertEqual(lab.report('a', 'b', 0, 1, 1), {'enabled': False})

    def test_all_history_and_custom_ranges_keep_the_requested_history(self):
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        store = SimpleNamespace(
            h=SimpleNamespace(db=db, lock=threading.RLock()), evidence=lambda *args: {}
        )
        lab = PredictiveLab(store, True)
        with (
            patch('predictive_lab.read_view') as view,
            patch('predictive_lab.series', return_value={}),
        ):
            view.return_value.__enter__.return_value = store
            self.assertEqual(lab.report('a', 'b', 0, 1800000000, 1800000000)['from'], 0)
            self.assertEqual(lab.report('a', 'b', 100, 200, 1800000000)['to'], 200)
            for start, end in [
                (float('nan'), 200),
                (-1, 200),
                (200, 100),
                (1800000001, 1800000002),
            ]:
                with self.assertRaises(ValueError):
                    lab.report('a', 'b', start, end, 1800000000)
        db.close()


if __name__ == '__main__':
    unittest.main()
