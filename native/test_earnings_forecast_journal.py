import json
import sqlite3
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from earnings_forecast import METHOD_VERSION
from earnings_forecast_journal import ForecastJournal
from test_earnings_forecast import NOW, SIGNAL, recent, minute


class Store:
    def __init__(self):
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        self.h = SimpleNamespace(db=db, lock=threading.RLock())
        self.data = {'a': {'minutes': recent()}}
        self.calls = []

    def evidence(self, account, device, start, end, now):
        self.calls.append((account, device, start, end, now))
        return self.data


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.s = Store()
        self.j = ForecastJournal(self.s, enabled=True)
        self.paid_patch = patch(
            'earnings_forecast_journal.read_paid_evidence',
            side_effect=lambda view, *args: view.evidence(*args),
        )
        self.paid_patch.start()

    def tearDown(self):
        self.paid_patch.stop()
        self.s.h.db.close()

    def record(self, now=NOW):
        self.j.record('owner', 'mac', now, {'a': SIGNAL})

    def test_next_whole_minute_target_has_no_completed_outcome_at_issue(self):
        self.record(NOW + 13)
        p = self.j.latest('owner', 'mac', NOW + 13)
        f = p['models']['a']
        self.assertEqual(p['origin'], NOW + 60)
        self.assertEqual(f['origin'], NOW + 60)
        self.assertEqual(f['issuedAt'], NOW + 13)
        self.assertLessEqual(f['paidEvidenceThrough'], f['issuedAt'] - 120)
        self.assertEqual(f['targetEnd'], NOW + 3660)

    def test_packets_immutable_and_checkpoint_deduplicated(self):
        self.record()
        raw = self.s.h.db.execute('SELECT data FROM earnings_forecast_observations').fetchone()[0]
        self.s.data = {'a': {'minutes': recent(99)}}
        self.record(NOW + 30)
        self.assertEqual(
            raw,
            self.s.h.db.execute('SELECT data FROM earnings_forecast_observations').fetchone()[0],
        )
        self.assertEqual(len(self.s.calls), 1)
        self.assertEqual(
            self.s.h.db.execute('SELECT COUNT(*) FROM earnings_forecast_observations').fetchone()[
                0
            ],
            1,
        )

    def test_separate_method_does_not_touch_original_shadow_study(self):
        self.s.h.db.execute('CREATE TABLE predictive_observations(data TEXT)')
        self.s.h.db.execute(
            'INSERT INTO predictive_observations VALUES(?)', ('immutable old cohort',)
        )
        self.record()
        self.assertEqual(
            self.s.h.db.execute('SELECT data FROM predictive_observations').fetchone()[0],
            'immutable old cohort',
        )

    def test_scoping_never_returns_other_account_device_or_future_packet(self):
        self.record()
        self.assertIsNone(self.j.latest('other', 'mac', NOW))
        self.assertIsNone(self.j.latest('owner', 'other', NOW))
        self.assertIsNone(self.j.latest('owner', 'mac', NOW - 1))

    def test_mature_fully_warm_future_hour_scored_and_signed(self):
        self.record()
        self.s.data = {'a': {'minutes': [minute(t, -0.002) for t in range(NOW, NOW + 3600, 60)]}}
        result = self.j.evaluation('owner', 'mac', NOW + 3720)
        row = result['models'][0]
        self.assertEqual(row['windows'], 1)
        self.assertAlmostEqual(row['maeUSDPerHour'], 0.18)
        self.assertAlmostEqual(row['biasUSDPerHour'], 0.18)
        self.assertEqual(result['intervalCalibration'], 'not_available')

    def test_unmatured_outcomes_not_scored(self):
        self.record()
        r = self.j.evaluation('owner', 'mac', NOW + 3719)['models'][0]
        self.assertEqual(r['windows'], 0)
        self.assertEqual(r['pendingWindows'], 1)
        self.assertIsNone(r['maeUSDPerHour'])

    def test_missing_or_switched_minute_censored_not_zero(self):
        self.record()
        self.s.data = {'a': {'minutes': [minute(t, 0.002) for t in range(NOW, NOW + 3540, 60)]}}
        r = self.j.evaluation('owner', 'mac', NOW + 3720)['models'][0]
        self.assertEqual(r['windows'], 0)
        self.assertEqual(r['censoredWindows'], 1)
        self.assertIsNone(r['maeUSDPerHour'])

    def test_unknown_forecast_not_scored_as_zero(self):
        self.s.data = {}
        self.record()
        r = self.j.evaluation('owner', 'mac', NOW + 3720)['models'][0]
        self.assertEqual(r['windows'], 0)
        self.assertEqual(r['unavailableForecasts'], 1)

    def test_exact_zero_is_a_scoreable_observation(self):
        self.s.data = {'a': {'minutes': recent(0)}}
        self.record()
        self.s.data = {'a': {'minutes': [minute(t, 0) for t in range(NOW, NOW + 3600, 60)]}}
        r = self.j.evaluation('owner', 'mac', NOW + 3720)['models'][0]
        self.assertEqual(r['windows'], 1)
        self.assertEqual(r['maeUSDPerHour'], 0)

    def test_clock_regression_does_not_backfill_old_checkpoint(self):
        self.record()
        self.record(NOW - 300)
        self.assertEqual(
            self.s.h.db.execute('SELECT COUNT(*) FROM earnings_forecast_observations').fetchone()[
                0
            ],
            1,
        )

    def test_hourly_scoring_excludes_overlapping_windows(self):
        self.s.data = {'a': {'minutes': [{**m, 'at': m['at'] + 60} for m in recent()]}}
        self.record(NOW + 61)
        shifted = [{**m, 'at': m['at'] + 3600} for m in recent()]
        self.s.data = {'a': {'minutes': shifted}}
        self.j.record('owner', 'mac', NOW + 3601, {'a': {**SIGNAL, 'observedAt': NOW + 3600}})
        self.s.data = {'a': {'minutes': [minute(t, 0.001) for t in range(NOW, NOW + 7260, 60)]}}
        r = self.j.evaluation('owner', 'mac', NOW + 7500)['models'][0]
        self.assertEqual(r['overlappingWindows'], 1)

    def test_disabled_journal_creates_no_table_and_no_records(self):
        s = Store()
        j = ForecastJournal(s, enabled=False)
        j.record('owner', 'mac', NOW, {'a': SIGNAL})
        self.assertIsNone(
            s.h.db.execute(
                "SELECT name FROM sqlite_master WHERE name='earnings_forecast_observations'"
            ).fetchone()
        )
        self.assertEqual(j.evaluation('owner', 'mac', NOW)['status'], 'disabled')
        s.h.db.close()


if __name__ == '__main__':
    unittest.main()
