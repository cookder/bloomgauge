import math
import unittest
from forecast import monitor_hour_start, forecast, hour_start
from history import History

HOUR = hour_start(1788670800)


def monitor(elapsed=1800, work=0.5, base=0):
    return {
        'status': 'ok',
        'updatedAt': HOUR + elapsed,
        'observedAt': HOUR + elapsed,
        'coverageStartedAt': HOUR - 21600,
        'gaps': 0,
        'coverageIntervals': [],
        'hours': [
            {'at': HOUR - i * 3600, 'usd': 1, 'jobs': 360, 'categories': {}} for i in range(1, 7)
        ]
        + [{'at': HOUR, 'usd': work + base, 'jobs': 180, 'categories': {'base_reward': base}}],
    }


def minute_data(rate=10, elapsed=1800, start=0):
    return [
        {'at': HOUR + i, 'rate': rate, 'n': min(60, elapsed - i)} for i in range(start, elapsed, 60)
    ]


class ForecastTests(unittest.TestCase):
    def compute(self, m=None, minutes=None, recent=None, now=None, online=True):
        return forecast(
            now or HOUR + 1800,
            m or monitor(),
            {'online': online, 'tracking': {'counting': True}},
            minutes or [],
            recent or [],
        )

    def test_stable_pace_converges_to_expected_hour_total(self):
        r = self.compute(
            minutes=minute_data(),
            recent=[
                {'window': 300, 'seconds': 300, 'usdPerSecond': 1 / 3600, 'jobsPerSecond': 0.1},
                {'window': 900, 'seconds': 900, 'usdPerSecond': 1 / 3600, 'jobsPerSecond': 0.1},
            ],
        )
        self.assertAlmostEqual(r['earnings']['projected'], 1)
        self.assertAlmostEqual(r['throughput']['projected'], 36000)
        self.assertAlmostEqual(r['throughput']['expectedRate'], 10)

    def test_recent_demand_changes_move_the_projection(self):
        base = self.compute()['earnings']['projected']

        def recent(rate):
            return [
                {
                    'window': 300,
                    'seconds': 300,
                    'usdPerSecond': rate / 3600,
                    'jobsPerSecond': rate / 10,
                },
                {
                    'window': 900,
                    'seconds': 900,
                    'usdPerSecond': rate / 3600,
                    'jobsPerSecond': rate / 10,
                },
            ]

        self.assertGreater(self.compute(recent=recent(3))['earnings']['projected'], base)
        self.assertLess(self.compute(recent=recent(0))['earnings']['projected'], base)

    def test_base_reward_batch_does_not_extrapolate_as_new_job_income(self):
        m = monitor(base=0.2)
        for h in m['hours'][:-1]:
            h['usd'] = 1.2
            h['categories'] = {'base_reward': 0.2}
        self.assertAlmostEqual(self.compute(m)['earnings']['projected'], 1.2)
        self.assertEqual(m['hours'][-1]['usd'], 0.7)  # Confirmed ledger remains unchanged.

    def test_hour_end_converges_and_never_below_confirmed_earnings(self):
        m = monitor(elapsed=3599, work=8)
        r = self.compute(m, now=HOUR + 3599)['earnings']
        self.assertGreaterEqual(r['projected'], 8)
        self.assertLess(r['projected'], 8.01)
        next_hour = self.compute(m, now=HOUR + 3600)
        self.assertEqual(next_hour['hourStart'], HOUR + 3600)
        self.assertIsNone(next_hour['earnings']['projected'])

    def test_stale_offline_and_tracking_gaps_hide_forecast(self):
        for m in [
            dict(monitor(), status='stale'),
            dict(monitor(), coverageStartedAt=HOUR + 100),
            dict(monitor(), gaps=1, coverageIntervals=[{'start': HOUR + 2, 'end': HOUR + 30}]),
        ]:
            self.assertIsNone(self.compute(m)['earnings']['projected'])
        r = self.compute(minutes=minute_data(), online=False)
        self.assertIsNone(r['earnings']['projected'])
        self.assertIsNone(r['throughput']['expectedRate'])

    def test_insufficient_history_does_not_invent_a_forecast(self):
        m = monitor(elapsed=10, work=0.01)
        m['coverageStartedAt'] = HOUR
        m['hours'] = m['hours'][-1:]
        r = self.compute(m, now=HOUR + 10)
        self.assertIsNone(r['earnings']['projected'])
        self.assertIsNone(r['throughput']['expectedRate'])

    def test_incomplete_output_only_forecasts_remaining_tokens(self):
        r = self.compute(minutes=minute_data(start=900))['throughput']
        self.assertTrue(r['partial'])
        self.assertIsNone(r['projected'])
        self.assertAlmostEqual(r['additional'], 18000)
        self.assertEqual(r['actual'], 9000)

    def test_idle_periods_are_included_in_expected_throughput(self):
        points = minute_data()
        for row in points[-15:]:
            row['rate'] = 0
        r = self.compute(minutes=points)['throughput']
        self.assertGreaterEqual(r['expectedRate'], 0)
        self.assertLess(r['expectedRate'], 5)
        self.assertTrue(math.isfinite(r['projected']))


class ProjectionHistoryTests(unittest.TestCase):
    def setUp(self):
        self.h = History(':memory:')

    def tearDown(self):
        self.h.close()

    def test_recent_credits_use_verified_coverage_and_exclude_base_rewards(self):
        def credit(id, at, model):
            from datetime import datetime, timezone

            return {
                'id': id,
                'created_at': datetime.fromtimestamp(at, timezone.utc).isoformat(),
                'model': model,
                'amount_micro_usd': 1000000,
                'completion_tokens': 10,
            }

        self.h.save_credits(
            'a', [credit(1, HOUR + 300, 'gemma'), credit(2, HOUR + 900, 'base_reward')], HOUR + 1200
        )
        _, rates = self.h.forecast_inputs('a', HOUR, HOUR + 1250, HOUR + 1230)
        self.assertEqual(len(rates), 2)
        self.assertAlmostEqual(rates[1]['usdPerSecond'], 1 / 900)
        self.assertEqual(rates[0]['usdPerSecond'], 0)
        self.assertFalse(self.h.forecast_inputs('other', HOUR, HOUR + 1250, HOUR + 1200)[1])
        self.h.save_credits('a', [credit(3, HOUR + 1700, 'gemma')], HOUR + 1800)
        self.assertFalse(self.h.forecast_inputs('a', HOUR, HOUR + 1800, HOUR + 1800)[1])

    def test_hourly_output_includes_boundary_hours_and_keeps_gaps_unknown(self):
        for second in range(120):
            self.h.save_sample(HOUR + second, {'tokensPerSecond': 10}, {})
        self.h.save_sample(HOUR + 3600, {'tokensPerSecond': None}, {})
        rows = self.h.hourly_output(HOUR + 100, HOUR + 3700)['samples']
        self.assertEqual(rows[0]['outputTokens'], 1200)
        self.assertEqual(rows[0]['n'], 120)
        self.assertIsNone(rows[1]['outputTokens'])


if __name__ == '__main__':
    unittest.main()


class MonitorHourTests(unittest.TestCase):
    def test_follows_monitor_boundaries_even_off_the_local_hour(self):
        # A half-hour zone's Monitor hours start at :30 UTC.
        hours = [{'at': 1800}, {'at': 5400}]
        self.assertEqual(monitor_hour_start(hours, 5400 + 3599), 5400)
        self.assertEqual(monitor_hour_start(hours, 9000), 9000)
        self.assertEqual(monitor_hour_start([{'at': 7200}], 9000), 7200)
        self.assertEqual(monitor_hour_start([], HOUR + 100), HOUR)
