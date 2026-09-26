import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from history import History
from network_weekly import report


def epoch(s, zone='America/Chicago'):
    return datetime.fromisoformat(s).replace(tzinfo=ZoneInfo(zone)).timestamp()


class NetworkWeeklyTests(unittest.TestCase):
    def setUp(self):
        self.h = History(':memory:')
        self.start = epoch('2026-09-07T00:00:00')  # Monday

    def tearDown(self):
        self.h.db.close()

    def add(self, at, seconds, rpm=60, tps=10):
        self.h.db.execute(
            'INSERT INTO network VALUES(?,?,?,?,?)',
            (at, seconds, rpm * seconds / 60, 0, tps * seconds),
        )

    def result(self, hours=1):
        return report(
            self.h,
            self.start,
            self.start + hours * 3600,
            self.start + hours * 3600,
            'America/Chicago',
        )

    def test_empty_is_unknown_not_zero(self):
        result = self.result(168)
        self.assertEqual(len(result['cells']), 168)
        self.assertTrue(all(c['requestsPerMinute'] is None for c in result['cells']))

    def test_mixed_resolution_is_duration_weighted_and_zero_is_real(self):
        self.add(self.start, 1800, 120)
        for minute in range(30, 60):
            self.add(self.start + minute * 60, 60, 0, 0)
        c = self.result()['cells'][0]
        self.assertEqual(c['requestsPerMinute'], 60)
        self.assertEqual(c['tokensPerSecond'], 5)
        self.assertEqual(c['days'], 1)

    def test_missing_and_partial_hours_not_ranked(self):
        self.add(self.start, 1800, 3000)
        c = self.result(2)['cells'][0]
        self.assertIsNone(c['requestsPerMinute'])
        self.assertEqual(c['observedSeconds'], 1800)
        self.assertEqual(c['dates'][0]['coverage'], 0.5)
        self.assertIsNone(self.result(2)['cells'][1]['requestsPerMinute'])

    def test_repeats_average_rates_not_total_counts(self):
        self.add(self.start, 3600, 100)
        self.add(self.start + 7 * 86400, 3600, 200)
        c = self.result(169)['cells'][0]
        self.assertEqual(c['requestsPerMinute'], 150)
        self.assertEqual(c['days'], 2)
        self.assertEqual(c['possibleDays'], 2)

    def test_coarse_boundary_and_current_hour_excluded(self):
        self.add(self.start, 4 * 3600, 1000)
        self.add(self.start + 4 * 3600, 3600, 100)
        self.add(self.start + 5 * 3600, 60, 9999)
        r = self.result(5.5)
        self.assertEqual(r['cells'][4]['requestsPerMinute'], 100)
        self.assertIsNone(r['cells'][5]['requestsPerMinute'])
        self.assertEqual(r['boundaryBuckets'], 1)
        # A custom partial start must not scale a whole-hour count into its tail.
        r = report(
            self.h,
            self.start + 4.5 * 3600,
            self.start + 6 * 3600,
            self.start + 6 * 3600,
            'America/Chicago',
        )
        self.assertTrue(all(c['requestsPerMinute'] is None for c in r['cells']))

    def test_older_coarse_rows_are_counted_as_excluded(self):
        self.add(self.start, 3600, 60)
        self.add(self.start + 3600, 14400, 1000)
        self.assertEqual(self.result(5)['coarseBuckets'], 1)

    def test_dst_repeat_and_spring_gap(self):
        start = epoch('2026-11-01T00:00:00')
        for h in range(4):
            self.add(start + h * 3600, 3600, 60)
        r = report(self.h, start, start + 4 * 3600, start + 4 * 3600, 'America/Chicago')
        c = r['cells'][6 * 24 + 1]
        self.assertEqual(c['expectedSeconds'], 7200)
        self.assertEqual(c['days'], 1)
        self.assertEqual(c['requestsPerMinute'], 60)
        self.h.db.execute('DELETE FROM network')
        start = epoch('2026-03-08T00:00:00')
        for h in range(3):
            self.add(start + h * 3600, 3600, 60)
        r = report(self.h, start, start + 3 * 3600, start + 3 * 3600, 'America/Chicago')
        self.assertEqual(r['cells'][6 * 24 + 2]['possibleDays'], 0)
        self.assertEqual(r['cells'][6 * 24 + 3]['days'], 1)

    def test_timezone_and_bad_inputs(self):
        self.add(self.start, 3600, 60)
        r = report(self.h, self.start, self.start + 3600, self.start + 3600, 'UTC')
        self.assertEqual(r['cells'][5]['requestsPerMinute'], 60)
        for start, end, zone in [
            (float('nan'), 1, 'UTC'),
            (2, 1, 'UTC'),
            (-1, 2, 'UTC'),
            (0, 1, 'fake/zone'),
            (0, float('inf'), 'UTC'),
        ]:
            with self.assertRaises(ValueError):
                report(self.h, start, end, self.start, zone)


if __name__ == '__main__':
    unittest.main()
