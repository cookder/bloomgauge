import json
import os
import time
import unittest
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

from demand_alerts import DemandAlerts, usual_levels
from history import History
from model_combinations import selection_key
from optimizer_store import OptimizerStore

NOW = datetime.fromisoformat('2026-09-13T01:30:00-04:00').timestamp()


class DemandAlertTests(unittest.TestCase):
    def setUp(self):
        self.old_tz = os.environ.get('TZ')
        os.environ['TZ'] = 'America/New_York'
        time.tzset()
        self.h = History(':memory:')
        self.store = OptimizerStore(self.h)
        self.store.evidence = Mock(return_value={})
        self.scanner = DemandAlerts(self.h, self.store)

    def tearDown(self):
        self.h.close()
        if self.old_tz is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = self.old_tz
        time.tzset()

    def samples(self, start, count, load=2, warm=4, model='a', queued=0, offset=0):
        self.h.db.executemany(
            'INSERT OR REPLACE INTO opt_network VALUES(?,?,?,?,?,?)',
            [(int(start + i * 30 + offset), model, load, queued, warm, warm) for i in range(count)],
        )

    def baseline(self, load=2, warm=4, model='a'):
        self.samples(NOW - 86400, 240, load, warm, model)

    def recent(self, now=NOW, load=6, warm=4, model='a', count=10, queued=0):
        self.samples(int(now // 30) * 30 - 300, count, load, warm, model, queued)

    def scan(self, now=NOW, models=('a',), account='account', device='device', current=('a',)):
        return self.scanner.scan(account, device, list(models), list(current), now)

    def three(self, start=NOW, **kwargs):
        result = None
        for index in range(3):
            now = start + index * 60
            self.recent(now, **kwargs)
            result = self.scan(now)
        return result

    def test_learning_without_two_hours_of_prior_network_observations(self):
        self.recent()
        a = self.scan()
        self.assertEqual(a['models'][0]['status'], 'learning')
        self.assertEqual(a['models'][0]['baselineHours'], 0)
        self.assertEqual(a['newAlertIds'], [])

    def test_three_distinct_fresh_minute_scans_emit_once(self):
        self.baseline()
        self.recent()
        a = self.scan()
        self.assertEqual((a['models'][0]['status'], a['models'][0]['streak']), ('watching', 1))
        for offset in (1, 10, 59):
            self.assertEqual(self.scan(NOW + offset)['newAlertIds'], [])
        self.recent(NOW + 60)
        b = self.scan(NOW + 60)
        self.assertEqual((b['models'][0]['status'], b['models'][0]['streak']), ('watching', 2))
        self.recent(NOW + 120)
        c = self.scan(NOW + 120)
        self.assertEqual(c['models'][0]['status'], 'spike')
        self.assertEqual(len(c['newAlertIds']), 1)
        self.assertEqual(c['alerts'][0]['id'], c['newAlertIds'][0])
        self.assertEqual(c['newAlerts'][0]['id'], c['newAlertIds'][0])
        self.assertEqual(self.scan(NOW + 121)['newAlertIds'], [])
        self.assertEqual(self.scanner.snapshot('account', 'device', NOW + 121)['newAlertIds'], [])
        self.assertIn('concurrency', c['alerts'][0]['detail'])

    def test_duplicate_samples_cannot_fake_coverage_and_latest_bucket_wins(self):
        self.baseline()
        for offset in range(20):
            self.samples(NOW - 300, 7, 6, offset=offset)
        a = self.scan()['models'][0]
        self.assertEqual(a['status'], 'stale')
        self.assertAlmostEqual(a['coverage'], 0.7)
        self.h.db.execute('DELETE FROM opt_network WHERE at>=?', (NOW - 300,))
        self.recent(NOW + 60, load=99)
        self.samples(NOW + 60 - 300, 10, 2, offset=5)
        b = self.scan(NOW + 60)['models'][0]
        self.assertEqual(b['coverage'], 1)
        self.assertEqual(b['load'], 2)
        self.assertEqual(b['status'], 'normal')

    def test_future_samples_and_old_history_are_excluded(self):
        self.samples(NOW - 31 * 86400, 240)
        self.recent()
        self.samples(NOW + 30, 300, 99)
        a = self.scan()['models'][0]
        self.assertEqual(a['status'], 'learning')
        self.assertEqual(a['baselineHours'], 0)
        self.assertEqual(a['load'], 6)
        self.assertLessEqual(a['observedAt'], NOW)

    def test_zero_warm_capacity_has_no_fabricated_denominator(self):
        self.baseline()
        self.recent(warm=0)
        a = self.scan()['models'][0]
        self.assertEqual(a['status'], 'no_headroom')
        self.assertIsNone(a['pressure'])
        self.assertIsNone(a['pressureRatio'])
        self.assertIsNone(a['loadRatio'])

    def test_zero_baseline_does_not_create_infinite_spike(self):
        self.baseline(load=0)
        a = self.three()
        row = a['models'][0]
        self.assertEqual(row['status'], 'learning')
        self.assertIsNone(row['loadRatio'])
        self.assertEqual(a['newAlertIds'], [])
        json.dumps(a, allow_nan=False)

    def test_both_load_and_per_provider_pressure_must_rise(self):
        self.baseline()
        self.recent(load=6, warm=12)
        a = self.scan()['models'][0]
        self.assertEqual(a['loadRatio'], 3)
        self.assertEqual(a['pressureRatio'], 1)
        self.assertEqual(a['status'], 'normal')

    def test_a_spike_is_2_8x_the_time_of_day_median(self):
        """calibration-2026-09-28.md (c): replaying Andrew's opt_network (Sep 13-28), 2x the old
        mean "usual" fired 339 alerts, 2x the time-of-day median 500 (x1.47) and 2.8x the median
        339 again (22 a day)."""
        from demand_alerts import SPIKE_MIN_LOAD, SPIKE_RATIO

        self.assertEqual((SPIKE_RATIO, SPIKE_MIN_LOAD), (2.8, 1))
        self.baseline(load=2, warm=4)
        self.recent(load=5.5, warm=4)  # 2.75x usual: normal now (a spike under the old 2x)
        self.assertEqual(self.scan()['models'][0]['status'], 'normal')
        self.recent(NOW + 60, load=5.6, warm=4)  # 2.8x
        self.assertEqual(self.scan(NOW + 60)['models'][0]['status'], 'watching')

    def test_meaningful_load_and_queued_work_are_distinct_from_completed_requests(self):
        self.baseline(load=0.1)
        self.recent(load=0.5)
        self.assertEqual(self.scan()['models'][0]['status'], 'normal')
        self.recent(NOW + 60, load=0, queued=3)
        row = self.scan(NOW + 60)['models'][0]
        self.assertEqual(row['status'], 'watching')
        self.assertEqual((row['active'], row['queued'], row['load']), (0, 3, 3))
        self.assertIn('not completed requests', row['detail'])

    def test_stale_gap_and_restart_break_incomplete_streaks(self):
        self.baseline()
        for index in range(2):
            self.recent(NOW + index * 60)
            self.scan(NOW + index * 60)
        self.scanner = DemandAlerts(self.h, self.store)
        self.recent(NOW + 600)
        a = self.scan(NOW + 600)
        self.assertEqual(a['models'][0]['streak'], 1)
        self.assertEqual(a['newAlertIds'], [])
        b = self.scan(NOW + 900)
        self.assertEqual(b['models'][0]['status'], 'stale')
        self.assertEqual(b['models'][0]['streak'], 0)
        self.recent(NOW + 960)
        self.assertEqual(self.scan(NOW + 960)['models'][0]['streak'], 1)

    def test_restart_preserves_alert_cooldown_and_requires_normal_rearm(self):
        self.baseline()
        first = self.three()
        self.scanner = DemandAlerts(self.h, self.store)
        self.assertEqual(self.scan(NOW + 121)['newAlertIds'], [])
        # Even after two hours, one uninterrupted spike produces no new alert.
        self.assertEqual(self.three(NOW + 7500)['newAlertIds'], [])
        self.assertFalse(self.scan(NOW + 7621)['models'][0]['rearmed'])
        # Ten minutes of continuously observed normal demand rearms it.
        for index in range(11):
            at = NOW + 7800 + index * 60
            self.recent(at, load=0)
            normal = self.scan(at)
        self.assertTrue(normal['models'][0]['rearmed'])
        second = self.three(NOW + 8460)
        self.assertEqual(len(second['newAlertIds']), 1)
        self.assertNotEqual(first['newAlertIds'], second['newAlertIds'])
        self.assertEqual(len(second['alerts']), 2)

    def test_normal_rearm_does_not_bypass_two_hour_cooldown(self):
        self.baseline()
        self.three()
        for index in range(11):
            at = NOW + 180 + index * 60
            self.recent(at, load=0)
            self.scan(at)
        a = self.three(NOW + 840)
        self.assertTrue(a['models'][0]['rearmed'])
        self.assertEqual(a['newAlertIds'], [])
        self.assertEqual(len(a['alerts']), 1)

    def test_stale_read_only_snapshot_clears_live_status_and_never_reemits(self):
        self.baseline()
        self.three()
        a = self.scanner.snapshot('account', 'device', NOW + 500)
        self.assertEqual(a['status'], 'stale')
        self.assertEqual(a['models'][0]['status'], 'stale')
        self.assertEqual(a['models'][0]['streak'], 0)
        self.assertEqual(a['newAlertIds'], [])
        self.assertEqual(len(a['alerts']), 1)
        self.assertEqual(self.scanner.snapshot('another', 'device', NOW)['alerts'], [])

    def test_model_eligibility_account_device_and_current_model_scope(self):
        self.baseline()
        self.baseline(model='unsupported')
        for index in range(3):
            at = NOW + index * 60
            self.recent(at)
            self.recent(at, model='unsupported')
            a = self.scan(at, current=())
        self.assertEqual([row['model'] for row in a['models']], ['a'])
        self.assertFalse(a['models'][0]['current'])
        self.assertEqual(self.scan(NOW + 180, account='another')['alerts'], [])
        self.assertEqual(self.scan(NOW + 180, device='another')['alerts'], [])
        self.assertEqual(self.scan(NOW + 180, models=())['alerts'], [])
        self.assertEqual(self.scan(NOW + 180, account='')['status'], 'unavailable')

    def test_paid_work_is_exact_model_historical_rate_not_spike_scaled(self):
        self.baseline()
        self.store.evidence.return_value = {
            'a': {'hours': 0.5, 'usdPerHour': 0.12},
            selection_key(['a', 'b']): {'hours': 5, 'usdPerHour': 9},
        }
        a = self.three()
        self.assertGreater(a['models'][0]['loadRatio'], 2)
        self.assertEqual(a['models'][0]['localRatePerHour'], 0.12)
        self.assertEqual(a['alerts'][0]['localRatePerHour'], 0.12)
        self.store.evidence.assert_called_once_with('account', 'device', NOW - 30 * 86400, NOW, NOW)
        self.store.evidence.return_value = {'a': {'hours': 0.49, 'usdPerHour': 9}}
        self.recent(NOW + 300)
        b = self.scan(NOW + 300)['models'][0]
        self.assertIsNone(b['localRatePerHour'])
        self.assertEqual(b['localEvidence'], 'untested')

    def test_active_opportunities_rank_by_recorded_local_rate_and_mark_untested(self):
        self.store.evidence.return_value = {
            'a': {'hours': 2, 'usdPerHour': 0.1},
            'b': {'hours': 1, 'usdPerHour': 0.3},
        }
        for model in ('a', 'b', 'c'):
            self.baseline(model=model)
            self.recent(model=model)
        a = self.scan(models=('a', 'b', 'c'))
        self.assertEqual([row['model'] for row in a['models']], ['b', 'a', 'c'])
        self.assertEqual(a['models'][-1]['localEvidence'], 'untested')

    def test_many_simultaneous_alerts_have_payloads_even_beyond_latest_twenty(self):
        models = tuple(f'model-{index:02}' for index in range(25))
        for model in models:
            self.baseline(model=model)
        for index in range(3):
            at = NOW + index * 60
            for model in models:
                self.recent(at, model=model)
            a = self.scan(at, models=models)
        self.assertEqual(len(a['newAlerts']), 25)
        self.assertEqual([event['id'] for event in a['newAlerts']], a['newAlertIds'])
        self.assertEqual(len(a['alerts']), 20)
        self.assertEqual(self.scanner.snapshot('account', 'device', NOW + 121)['newAlerts'], [])

    def test_simultaneous_scan_and_snapshot_calls_emit_one_event(self):
        self.baseline()
        for index in range(2):
            self.recent(NOW + index * 60)
            self.scan(NOW + index * 60)
        self.recent(NOW + 120)
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: self.scan(NOW + 120), range(12)))
            snapshots = list(
                pool.map(lambda _: self.scanner.snapshot('account', 'device', NOW + 120), range(12))
            )
        self.assertEqual(sum(len(result['newAlertIds']) for result in results), 1)
        self.assertTrue(all(result['newAlertIds'] == [] for result in snapshots))

    def test_historical_cache_refreshes_after_five_minutes_or_local_clock_boundary(self):
        self.baseline()
        self.recent()
        self.scan()
        for offset in (60, 120, 180, 240):
            self.recent(NOW + offset)
            self.scan(NOW + offset)
        self.assertEqual(self.store.evidence.call_count, 1)
        self.recent(NOW + 300)
        row = self.scan(NOW + 300)['models'][0]
        self.assertEqual(self.store.evidence.call_count, 2)
        self.assertEqual(row['baselineAsOf'], NOW + 300)
        before = datetime.fromisoformat('2026-09-13T01:59:30-04:00').timestamp()
        self.recent(before)
        self.scan(before)
        self.recent(before + 60)
        self.scan(before + 60)
        self.assertEqual(self.store.evidence.call_count, 4)

    def test_tiny_positive_baseline_never_emits_nonfinite_ratios(self):
        self.baseline(load=1e-320)
        self.recent()
        a = self.scan()
        self.assertEqual(a['models'][0]['status'], 'learning')
        self.assertIsNone(a['models'][0]['loadRatio'])
        json.dumps(a, allow_nan=False)

    def test_latest_alerts_are_bounded_while_full_history_remains_persisted(self):
        for index in range(30):
            self.h.db.execute(
                'INSERT INTO demand_alert_events(account,device,model,at,payload) VALUES(?,?,?,?,?)',
                (
                    'account',
                    'device',
                    'a',
                    NOW - index,
                    json.dumps({'model': 'a', 'at': NOW - index}),
                ),
            )
        self.baseline()
        self.recent(load=2)
        a = self.scan()
        self.assertEqual(len(a['alerts']), 20)
        self.assertEqual(
            self.h.db.execute('SELECT COUNT(*) FROM demand_alert_events').fetchone()[0], 30
        )

    def test_contextual_baseline_excludes_weekday_spike_history(self):
        for day in ('05', '06', '12'):
            start = datetime.fromisoformat(f'2026-09-{day}T01:00:00-04:00').timestamp()
            self.samples(start, 80, load=1)
        self.samples(NOW - 2 * 86400, 240, load=9)
        self.recent(load=3)
        row = self.scan()['models'][0]
        self.assertEqual(row['baselineScope'], 'daytype_hour')
        self.assertEqual((row['baselineHours'], row['baselineDays']), (2, 3))
        self.assertEqual(row['baselineLoad'], 1)
        self.assertEqual(row['loadRatio'], 3)

    def test_usual_is_the_median_so_one_busy_day_does_not_raise_it(self):
        # Three quiet weekend nights and one busy one; the mean would be 3.
        for day, load in (('05', 1), ('06', 1), ('12', 1), ('13', 9)):
            start = datetime.fromisoformat(f'2026-09-{day}T00:00:00-04:00').timestamp()
            self.samples(start, 120, load=load)
        a = usual_levels(self.h.db, ['a'], NOW)['a']
        self.assertEqual(a['baselineScope'], 'daytype_hour')
        self.assertEqual((a['baselineLoad'], a['baselinePressure']), (1, 0.25))
        self.assertEqual((a['baselineHours'], a['baselineDays']), (4, 4))

    def test_usual_counts_only_this_time_of_day(self):
        # 01:30: 00:00-02:59 counts; 22:00 and 04:00 do not.
        for day in ('05', '06', '12'):
            for hour, load in (('00', 1), ('02', 3), ('04', 9)):
                start = datetime.fromisoformat(f'2026-09-{day}T{hour}:00:00-04:00').timestamp()
                self.samples(start, 60, load=load)
            start = datetime.fromisoformat(f'2026-09-{day}T22:00:00-04:00').timestamp()
            self.samples(start, 60, load=9)
        a = usual_levels(self.h.db, ['a'], NOW)['a']
        self.assertEqual((a['baselineScope'], a['baselineDays']), ('daytype_hour', 3))
        self.assertEqual(a['baselineHours'], 3)
        self.assertEqual(a['baselineLoad'], 2)  # median of 180 ones and 180 threes

    def test_usual_falls_back_to_all_hours_median_while_learning(self):
        self.samples(NOW - 6 * 3600, 240, load=4)
        self.samples(NOW - 5 * 3600, 60, load=100)
        a = usual_levels(self.h.db, ['a', 'b'], NOW)
        self.assertEqual((a['a']['baselineScope'], a['a']['baselineLoad']), ('all_hours', 4))
        self.assertEqual(a['b']['baselineScope'], 'learning')
        self.assertIsNone(a['b']['baselinePressure'])

    def test_dst_repeated_hour_counts_elapsed_samples_and_only_one_local_date(self):
        for at in (
            '2026-11-01T01:00:00-04:00',
            '2026-11-01T01:00:00-05:00',
            '2026-10-25T01:00:00-04:00',
            '2026-10-18T01:00:00-04:00',
        ):
            self.samples(datetime.fromisoformat(at).timestamp(), 120)
        now = datetime.fromisoformat('2026-11-08T01:30:00-05:00').timestamp()
        self.recent(now)
        row = self.scan(now)['models'][0]
        self.assertEqual(row['baselineScope'], 'daytype_hour')
        self.assertEqual((row['baselineHours'], row['baselineDays']), (4, 3))


if __name__ == '__main__':
    unittest.main()
