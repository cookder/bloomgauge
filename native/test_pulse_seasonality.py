"""Time/day comparisons must stay grounded in qualified warm observations."""

import os
import time
import unittest
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import Mock

from history import History
from live_earnings import EarningsPulse, historical_baseline
from model_combinations import selection_key
from model_projection import ModelProjection
from optimizer_store import OptimizerStore, device_id


@contextmanager
def local_timezone(name):
    previous = os.environ.get('TZ')
    os.environ['TZ'] = name
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = previous
        time.tzset()


def stamp(value):
    return datetime.fromisoformat(value).timestamp()


def minutes(value, rate=0.1, count=60):
    start = stamp(value)
    return [{'at': start + i * 60, 'seconds': 60, 'usd': rate / 60} for i in range(count)]


class SeasonalBaselineTests(unittest.TestCase):
    def setUp(self):
        self.zone = local_timezone('America/New_York')
        self.zone.__enter__()
        self.now = stamp('2026-09-13T01:30:00-04:00')  # Sunday, overnight.

    def tearDown(self):
        self.zone.__exit__(None, None, None)

    def baseline(self, rows, now=None):
        return historical_baseline(rows, ['a'], self.now if now is None else now)

    def test_learning_preserves_observed_coverage_without_fabricating_reference(self):
        a = self.baseline(minutes('2026-09-12T01:00:00-04:00', count=29))
        self.assertIsNone(a['ratePerHour'])
        self.assertEqual(a['scope'], 'learning')
        self.assertEqual(a['hours'], 29 / 60)
        self.assertEqual(self.baseline([])['days'], 0)

    def test_broad_fallback_keeps_duration_weighted_warm_idle_and_signed_money(self):
        rows = minutes('2026-09-12T01:00:00-04:00', 0.3, 30)
        rows += minutes('2026-09-11T01:00:00-04:00', 0, 60)
        rows += minutes('2026-09-10T01:00:00-04:00', -0.1, 30)
        a = self.baseline(rows)
        self.assertEqual(a['scope'], 'hour')
        self.assertAlmostEqual(a['ratePerHour'], 0.05)
        # A single earlier date still supplies the original 30-minute fallback.
        b = self.baseline(rows[:30])
        self.assertEqual(b['scope'], 'model')
        self.assertAlmostEqual(b['ratePerHour'], 0.3)
        self.assertEqual(b['totalHours'], 0.5)
        self.assertIsNone(b['dayWeightCapHours'])

    def test_same_weekday_beats_daytype_and_all_hours(self):
        rows = sum(
            (minutes(f'2026-{day}T01:00:00-04:00', 0.06) for day in ('08-23', '08-30', '09-06')), []
        )
        rows += minutes('2026-09-12T01:00:00-04:00', 0.8)
        rows += minutes('2026-09-11T13:00:00-04:00', 2, 180)
        a = self.baseline(rows)
        self.assertEqual(a['scope'], 'weekday_hour')
        self.assertAlmostEqual(a['ratePerHour'], 0.06)
        self.assertEqual((a['hours'], a['days']), (3, 3))
        self.assertEqual((a['totalHours'], a['totalDays']), (7, 5))
        self.assertEqual((a['localWeekday'], a['localHour']), (6, 1))
        self.assertIn('Sundays', a['detail'])

    def test_weekend_compares_with_weekends_before_weekday_traffic(self):
        rows = sum(
            (minutes(f'2026-09-{day}T01:00:00-04:00', 0.08) for day in ('05', '06', '12')), []
        )
        rows += sum(
            (minutes(f'2026-09-{day}T01:00:00-04:00', 0.7) for day in ('09', '10', '11')), []
        )
        a = self.baseline(rows)
        self.assertEqual(a['scope'], 'daytype_hour')
        self.assertAlmostEqual(a['ratePerHour'], 0.08)
        self.assertIn('weekends', a['detail'])

    def test_weekday_group_and_same_clock_hour_fallback(self):
        rows = sum(
            (minutes(f'2026-09-{day}T01:00:00-04:00', 0.1) for day in ('09', '10', '11')), []
        )
        weekday = self.baseline(rows, stamp('2026-09-14T01:30:00-04:00'))
        self.assertEqual(weekday['scope'], 'daytype_hour')
        self.assertIn('weekdays', weekday['detail'])
        weekend = self.baseline(rows)
        self.assertEqual(weekend['scope'], 'hour')
        self.assertIn('all days', weekend['detail'])
        self.assertAlmostEqual(weekend['ratePerHour'], 0.1)

    def test_minimum_dates_and_coverage_cannot_be_met_by_short_bursts(self):
        two_dates = minutes('2026-09-05T01:00:00-04:00', count=120)
        two_dates += minutes('2026-09-06T01:00:00-04:00', count=120)
        self.assertEqual(self.baseline(two_dates)['scope'], 'model')
        short_dates = sum(
            (minutes(f'2026-09-{day:02}T01:00:00-04:00', count=29) for day in range(5, 12)), []
        )
        self.assertEqual(self.baseline(short_dates)['scope'], 'model')
        under = sum(
            (minutes(f'2026-09-{day}T01:00:00-04:00', count=39) for day in ('05', '06', '12')), []
        )
        threshold = sum(
            (minutes(f'2026-09-{day}T01:00:00-04:00', count=40) for day in ('05', '06', '12')), []
        )
        self.assertEqual(self.baseline(under)['scope'], 'model')
        self.assertEqual(self.baseline(threshold)['scope'], 'daytype_hour')

    def test_long_burst_date_does_not_dominate_the_contextual_average(self):
        rows = minutes('2026-09-05T00:00:00-04:00', 0.9, 180)
        rows += minutes('2026-09-06T01:00:00-04:00', 0.1)
        rows += minutes('2026-09-12T01:00:00-04:00', 0.1)
        a = self.baseline(rows)
        self.assertEqual(a['scope'], 'daytype_hour')
        self.assertAlmostEqual(a['ratePerHour'], 1.1 / 3)
        self.assertLess(a['ratePerHour'], 2.9 / 5)
        self.assertEqual(a['hours'], 5)  # Honest observed coverage, not capped weight.
        self.assertEqual(a['dayWeightCapHours'], 1)
        self.assertIn('one warm hour of weight', a['detail'])

    def test_night_window_wraps_midnight_and_excludes_daytime(self):
        rows = minutes('2026-09-09T23:00:00-04:00', 0.06)
        rows += minutes('2026-09-10T00:00:00-04:00', 0.06)
        rows += minutes('2026-09-11T01:00:00-04:00', 0.06)
        rows += minutes('2026-09-12T12:00:00-04:00', 0.8, 180)
        a = self.baseline(rows, stamp('2026-09-13T00:15:00-04:00'))
        self.assertEqual(a['scope'], 'hour')
        self.assertEqual(a['hours'], 3)
        self.assertAlmostEqual(a['ratePerHour'], 0.06)

    def test_dst_fall_back_uses_actual_seconds_and_one_local_date(self):
        rows = minutes('2026-11-01T01:00:00-04:00', 0.1)
        rows += minutes('2026-11-01T01:00:00-05:00', 0.1)
        rows += minutes('2026-10-25T01:00:00-04:00', 0.1)
        rows += minutes('2026-10-18T01:00:00-04:00', 0.1)
        a = self.baseline(rows, stamp('2026-11-08T01:15:00-05:00'))
        self.assertEqual(a['scope'], 'weekday_hour')
        self.assertEqual((a['hours'], a['days']), (4, 3))
        self.assertEqual(a['timezone'], 'EST')
        self.assertAlmostEqual(a['ratePerHour'], 0.1)

    def test_timezone_conversion_changes_matching_local_clock_not_instants(self):
        rows = sum(
            (minutes(f'2026-09-{day}T04:00:00+00:00', 0.1) for day in ('05', '06', '12')), []
        )
        a = self.baseline(rows, stamp('2026-09-13T04:00:00+00:00'))
        self.assertEqual(a['scope'], 'daytype_hour')
        self.assertEqual(a['localHour'], 0)
        with local_timezone('America/Los_Angeles'):
            b = self.baseline(rows, stamp('2026-09-13T04:00:00+00:00'))
        # The same instants are now Friday/Saturday evenings; only two weekend
        # dates exist, so this falls back to the honest any-day hour comparison.
        self.assertEqual((b['scope'], b['localHour'], b['localWeekday']), ('hour', 21, 5))
        self.assertAlmostEqual(b['ratePerHour'], a['ratePerHour'])


class SeasonalPulseIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.zone = local_timezone('America/New_York')
        self.zone.__enter__()
        self.now = stamp('2026-09-13T01:30:00-04:00')
        self.h = History(':memory:')
        self.store = OptimizerStore(self.h)
        self.projection = ModelProjection(self.store)
        self.pulse = EarningsPulse(self.h, self.projection)
        self.raw = {'attestation_public_key': 'local-device'}
        self.device = device_id(self.raw)
        self.store.identity(self.device, 'local-provider')
        self.session = {'id': 2, 'models': ['a'], 'startedAt': self.now - 900, 'status': 'active'}

    def tearDown(self):
        self.h.close()
        self.zone.__exit__(None, None, None)

    def snapshot(self, now=None):
        return self.pulse.snapshot(
            'account',
            self.raw,
            self.session,
            {'status': 'missing'},
            None,
            self.now if now is None else now,
        )['baseline']

    def record(
        self,
        rows,
        model='a',
        account='account',
        device=None,
        table='opt_ready_minutes',
        coverage=True,
    ):
        device = device or self.device
        for row in rows:
            self.h.db.execute(
                f'INSERT INTO {table} VALUES(?,?,?,?,?,?,?,?)',
                (account, device, row['at'], model, row['seconds'], 0, 0, 0),
            )
        if coverage:
            self.h.db.execute(
                'INSERT OR REPLACE INTO opt_coverage VALUES(?,?,?)',
                (account, rows[0]['at'], rows[-1]['at'] + 60),
            )

    def test_exact_scope_earlier_sessions_full_warm_settlement_and_coverage(self):
        # Three complete qualified idle hours establish the contextual zero.
        for day in ('05', '06', '12'):
            self.record(minutes(f'2026-09-{day}T01:00:00-04:00', 0))
        self.record(minutes('2026-09-13T01:15:00-04:00', 0, 30))  # Current session/future.
        self.record(minutes('2026-08-01T01:00:00-04:00', 0))  # Outside 30 days.
        self.record(minutes('2026-09-04T01:00:00-04:00', 0), account='another')
        self.record(minutes('2026-09-03T01:00:00-04:00', 0), device='another')
        self.record(minutes('2026-09-02T01:00:00-04:00', 0), model='b')
        self.record(minutes('2026-09-01T01:00:00-04:00', 0), model=selection_key(['a', 'b']))
        self.record(minutes('2026-08-31T01:00:00-04:00', 0), table='opt_minutes')
        self.record(minutes('2026-08-30T01:00:00-04:00', 0), coverage=False)
        partial = minutes('2026-08-29T01:00:00-04:00', 0)
        for row in partial:
            row['seconds'] = 59
        self.record(partial)
        a = self.snapshot()
        self.assertEqual(
            (a['scope'], a['hours'], a['totalHours'], a['ratePerHour']), ('daytype_hour', 3, 3, 0)
        )
        self.session['models'] = ['a', 'b']
        b = self.snapshot()
        self.assertEqual((b['scope'], b['hours'], b['models']), ('model', 1, ['a', 'b']))

    def test_local_hour_boundary_invalidates_baseline_before_cache_timeout(self):
        fake = Mock()
        fake.evidence.return_value = []
        pulse = EarningsPulse(self.h, fake)
        before = stamp('2026-09-13T01:59:50-04:00')
        a = pulse.snapshot('account', self.raw, self.session, {'status': 'missing'}, None, before)
        b = pulse.snapshot(
            'account', self.raw, self.session, {'status': 'missing'}, None, before + 20
        )
        self.assertEqual(fake.evidence.call_count, 2)
        self.assertEqual((a['baseline']['localHour'], b['baseline']['localHour']), (1, 2))
        pulse.snapshot('account', self.raw, self.session, {'status': 'missing'}, None, before + 25)
        self.assertEqual(fake.evidence.call_count, 2)


if __name__ == '__main__':
    unittest.main()
