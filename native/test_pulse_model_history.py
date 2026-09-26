import json
import tempfile
import unittest
from unittest.mock import Mock

from history import History
from live_earnings import EarningsPulse
from traffic_pulse import TrafficPulse
from provider_sessions import ProviderSessions
from pulse_history import model_history


class ModelPulseHistoryTests(unittest.TestCase):
    def setUp(self):
        self.h = History(':memory:')
        self.addCleanup(self.h.close)
        ProviderSessions(self.h, tempfile.gettempdir())
        self.earnings, self.traffic = EarningsPulse(self.h, Mock()), TrafficPulse(self.h)
        for sid, scope, models in [
            (1, 'this-mac', ['gemma']),
            (2, 'this-mac', ['oss']),
            (3, 'another-mac', ['foreign']),
            (4, 'this-mac', ['gemma']),
            (5, 'this-mac', ['oss', 'gemma']),
        ]:
            self.h.db.execute(
                'INSERT INTO provider_sessions VALUES(?,?,?)',
                (sid, scope, json.dumps({'models': models, '_private': 'never-return'})),
            )

    def save(self, sid, at, rate, account='one', status='live'):
        self.earnings.record_rate(
            account,
            {
                'sessionId': sid,
                'at': at,
                'status': status,
                'windows': {'60': {'ratePerHour': rate}, '300': {'ratePerHour': rate}},
            },
        )
        self.traffic.save(
            account,
            sid,
            at,
            at + 3,
            windows={
                '60': {'tokensPerSecond': rate, 'requestsPerMinute': rate * 2},
                '300': {'tokensPerSecond': rate, 'requestsPerMinute': rate * 2},
            }
            if status == 'live'
            else {},
        )

    def read(self, table, current=2, start=0, end=300, metric='tokens', account='one'):
        return model_history(
            self.h,
            account,
            current,
            start,
            end,
            table,
            lambda sid, step: (
                self.earnings.rate_history(account, sid, start, end, bucket_seconds=step)
                if table == 'pulse_rates'
                else self.traffic.rate_history(account, sid, start, end, metric, step)
            ),
        )

    def test_both_pulses_keep_previous_models_but_never_other_account_or_device(self):
        for sid, at, rate, account in [
            (1, 100, 0.1, 'one'),
            (2, 150, 0.2, 'one'),
            (3, 160, 99, 'one'),
            (1, 100, 99, 'other'),
        ]:
            self.save(sid, at, rate, account)
        for table in ('pulse_rates', 'traffic_intervals'):
            data = self.read(table)
            self.assertEqual(data['sessionId'], 2)
            self.assertEqual(data['scope'], 'models')
            self.assertEqual([s['models'] for s in data['sessions']], [['gemma'], ['oss']])
            self.assertEqual([p['sessionId'] for p in data['samples']], [1, 2])
            for point, expected in zip(data['samples'], [0.1, 0.2]):
                self.assertAlmostEqual(point['rate60'], expected)
            self.assertNotIn('this-mac', json.dumps(data))
            self.assertNotIn('never-return', json.dumps(data))
        for point, expected in zip(
            self.read('traffic_intervals', metric='requests')['samples'], [0.2, 0.4]
        ):
            self.assertAlmostEqual(point['rate60'], expected)

    def test_model_switch_can_read_history_before_the_new_session_has_samples(self):
        self.save(1, 100, 0.1)
        for table in ('pulse_rates', 'traffic_intervals'):
            self.assertEqual(self.read(table)['samples'][0]['sessionId'], 1)

    def test_custom_ranges_repeated_models_and_pairs_retain_session_identity(self):
        self.save(1, 100, 0.1)
        self.save(2, 150, 0.2)
        self.save(4, 200, 0.3)
        self.save(5, 250, 0.4)
        for table in ('pulse_rates', 'traffic_intervals'):
            data = self.read(table, current=5, start=199, end=280)
            self.assertEqual([s['id'] for s in data['sessions']], [4, 5])
            self.assertEqual(data['sessions'][1]['models'], ['gemma', 'oss'])
            self.assertEqual(data['coverageStart'], 100)
            self.assertEqual(self.read(table, start=400, end=500)['samples'], [])

    def test_missing_identity_never_widens_to_unscoped_history(self):
        self.save(1, 100, 0.1)
        for table in ('pulse_rates', 'traffic_intervals'):
            for sid, account in [(None, 'one'), (999, 'one'), (2, ''), (2, 'other')]:
                self.assertEqual(self.read(table, current=sid, account=account)['samples'], [])

    def test_bucket_resolution_cannot_mix_sessions_or_turn_cold_into_zero(self):
        # A long range puts two model sessions within the same coarse bucket.
        self.save(1, 100, 0)
        self.save(2, 104, 0.8)
        self.save(2, 110, 0, status='paused')
        self.save(4, 100000, 0.5)
        for table in ('pulse_rates', 'traffic_intervals'):
            data = self.read(table, current=4, end=100100)
            self.assertGreater(data['bucketSeconds'], 100)
            a = [p for p in data['samples'] if p['sessionId'] == 1]
            b = [p for p in data['samples'] if p['sessionId'] == 2]
            self.assertEqual(a[0]['rate60'], 0)
            self.assertIsNone(b[0]['rate60'])

    def test_many_sessions_share_one_resolution_budget(self):
        for sid in range(10, 30):
            self.h.db.execute(
                'INSERT INTO provider_sessions VALUES(?,?,?)',
                (sid, 'this-mac', json.dumps({'models': [str(sid)]})),
            )
            for at in range((sid - 10) * 1000, (sid - 9) * 1000, 3):
                self.save(sid, at, 0.1)
        for table in ('pulse_rates', 'traffic_intervals'):
            data = self.read(table, current=29, end=20000)
            self.assertLess(len(data['samples']), 700)
            self.assertEqual(len(data['sessions']), 20)


if __name__ == '__main__':
    unittest.main()
