import pathlib
import tempfile
import unittest
from unittest.mock import Mock
from history import History
from live_earnings import EarningsPulse


class PulseHistoryTests(unittest.TestCase):
    def setUp(self):
        self.h = History(':memory:')
        self.addCleanup(self.h.close)
        self.pulse = EarningsPulse(self.h, Mock())

    def record(self, at, rate=0.15, status='live', account='one', session=4):
        self.pulse.record_rate(
            account,
            {
                'at': at,
                'sessionId': session,
                'status': status,
                'windows': {
                    '60': {'ratePerHour': rate},
                    '300': {'ratePerHour': rate * 2 if rate is not None else None},
                },
            },
        )

    def test_stores_exact_dollar_rates_for_both_meter_windows(self):
        self.record(100.9, 0.012345)
        self.assertEqual(
            self.pulse.rate_history('one', 4, 0, 200)['samples'],
            [{'at': 100, 'rate60': 0.012345, 'rate300': 0.02469}],
        )

    def test_cold_stale_unmatched_and_offline_are_gaps_not_zero(self):
        for i, state in enumerate(('paused', 'stale', 'unmatched', 'offline')):
            self.record(100 + i, status=state)
        self.assertTrue(
            all(
                r['rate60'] is None and r['rate300'] is None
                for r in self.pulse.rate_history('one', 4, 0, 200)['samples']
            )
        )

    def test_real_idle_zero_and_signed_adjustments_survive(self):
        self.record(100, 0)
        self.record(101, -0.1)
        self.assertEqual(
            [r['rate60'] for r in self.pulse.rate_history('one', 4, 0, 200)['samples']], [0, -0.1]
        )

    def test_nan_and_infinite_values_are_missing(self):
        for i, value in enumerate((float('nan'), float('inf'), -float('inf'))):
            self.record(100 + i, value)
        self.assertTrue(
            all(r['rate60'] is None for r in self.pulse.rate_history('one', 4, 0, 200)['samples'])
        )

    def test_account_and_session_boundaries_never_mix(self):
        self.record(100, 0.1)
        self.record(100, 9, account='other')
        self.record(100, 8, session=5)
        result = self.pulse.rate_history('one', 4, 0, 200)
        self.assertEqual(result['samples'][0]['rate60'], 0.1)
        self.assertEqual(result['count'], 1)
        self.assertEqual(self.pulse.rate_history('', 4, 0, 200)['samples'], [])
        self.assertEqual(self.pulse.rate_history('one', None, 0, 200)['samples'], [])

    def test_custom_range_and_opening_before_recording_do_not_fabricate_history(self):
        self.record(100)
        self.record(110)
        self.assertEqual(self.pulse.rate_history('one', 4, 0, 99)['samples'], [])
        result = self.pulse.rate_history('one', 4, 101, 120)
        self.assertEqual(result['coverageStart'], 100)
        self.assertEqual([r['at'] for r in result['samples']], [110])

    def test_closed_app_gap_breaks_the_line(self):
        self.record(100)
        self.record(200)
        self.assertEqual(
            [r['rate60'] for r in self.pulse.rate_history('one', 4, 0, 300)['samples']],
            [0.15, None, 0.15],
        )

    def test_long_ranges_are_bounded_and_partial_cold_buckets_stay_missing(self):
        for at in range(2000):
            self.record(at, 0.1, 'paused' if at == 2 else 'live')
        result = self.pulse.rate_history('one', 4, 0, 3000)
        self.assertEqual(result['bucketSeconds'], 4)
        self.assertEqual(len(result['samples']), 500)
        self.assertIsNone(result['samples'][0]['rate60'])
        self.assertAlmostEqual(result['samples'][1]['rate60'], 0.1)

    def test_repeated_second_replaces_instead_of_counting_twice(self):
        self.record(100.1, 0.1)
        self.record(100.9, 0.2)
        result = self.pulse.rate_history('one', 4, 0, 200)
        self.assertEqual(result['count'], 1)
        self.assertEqual(result['samples'][0]['rate60'], 0.2)

    def test_saved_rates_survive_reopening_bloom(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / 'history.sqlite3'
            h = History(path)
            p = EarningsPulse(h, Mock())
            p.record_rate(
                'one',
                {
                    'at': 100,
                    'sessionId': 4,
                    'status': 'live',
                    'windows': {'60': {'ratePerHour': 0.15}},
                },
            )
            h.close()
            h = History(path)
            p = EarningsPulse(h, Mock())
            try:
                self.assertEqual(p.rate_history('one', 4, 0, 200)['samples'][0]['rate60'], 0.15)
            finally:
                h.close()


if __name__ == '__main__':
    unittest.main()
