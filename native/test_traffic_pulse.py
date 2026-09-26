import json
import pathlib
import tempfile
import unittest

from history import History
from traffic_pulse import TrafficPulse

T = 1789280000


class TrafficPulseTests(unittest.TestCase):
    def setUp(self):
        self.h = History(':memory:')
        self.pulse = TrafficPulse(self.h)
        self.account = 'account-one'
        self.raw = {
            'pid': 123,
            'started_at': T - 100,
            'written_at': T,
            'attestation_public_key': 'device-one',
            'advertised_models': ['a'],
            'stats': {'tokens_generated': 1000, 'requests_served': 100},
        }
        self.session = {
            'id': 4,
            'models': ['a'],
            'status': 'active',
            'performance': {'status': 'counting'},
        }
        self.tracking = {'counting': True, 'verifiedAt': T - 10}

    def tearDown(self):
        self.h.close()

    def sample(self, seconds, tokens=None, requests=None, **extra):
        self.raw['written_at'] = T + seconds
        if tokens is not None:
            self.raw['stats']['tokens_generated'] = 1000 + tokens
        if requests is not None:
            self.raw['stats']['requests_served'] = 100 + requests
        return self.pulse.observe(
            self.account, self.raw, self.session, self.tracking, T + seconds + extra.get('delay', 0)
        )

    def collect(self, until=60, interval=3, token_rate=10, request_rate=1):
        for seconds in range(0, until + 1, interval):
            result = self.sample(seconds, seconds * token_rate, seconds * request_rate)
        return result

    def test_first_observation_does_not_turn_lifetime_totals_into_a_burst(self):
        result = self.sample(0)
        self.assertEqual(result['status'], 'warming')
        self.assertIsNone(result['windows']['60']['tokensPerSecond'])
        self.assertEqual(result['windows']['60']['seconds'], 0)

    def test_rolling_rates_use_source_time_and_exclude_display_repeats(self):
        result = self.collect(60)
        self.assertEqual(result['windows']['60']['tokensPerSecond'], 10)
        self.assertEqual(result['windows']['60']['requestsPerMinute'], 60)
        count = self.h.db.execute('SELECT COUNT(*) FROM traffic_intervals').fetchone()[0]
        repeated = self.sample(60, delay=2)
        self.assertEqual(repeated['windows'], result['windows'])
        self.assertEqual(
            self.h.db.execute('SELECT COUNT(*) FROM traffic_intervals').fetchone()[0], count
        )

    def test_complete_window_clips_partial_first_counter_interval(self):
        self.sample(0, 0, 0)
        for seconds in (9, 18, 27, 36, 45, 54, 63):
            result = self.sample(seconds, seconds * 10, seconds)
        window = result['windows']['60']
        self.assertEqual(window['start'], T + 3)
        self.assertEqual(window['seconds'], 60)
        self.assertEqual(window['outputTokens'], 600)
        self.assertEqual(window['requests'], 60)

    def test_idle_is_a_real_zero_after_twenty_warm_seconds(self):
        result = self.collect(21, token_rate=0, request_rate=0)
        self.assertEqual(result['status'], 'live')
        self.assertEqual(result['windows']['60']['tokensPerSecond'], 0)
        self.assertEqual(result['windows']['60']['requestsPerMinute'], 0)
        self.assertIsNone(self.collect(18)['windows']['60']['tokensPerSecond'])

    def test_cold_time_and_prewarm_counters_never_count(self):
        self.collect(30)
        self.tracking['counting'] = False
        cold = self.sample(33, 5000, 1000)
        self.assertEqual(cold['status'], 'paused')
        self.assertIsNone(cold['windows']['60']['tokensPerSecond'])
        self.tracking.update(counting=True, verifiedAt=T + 34)
        self.sample(36, 9000, 2000)
        for s in range(39, 60, 3):
            result = self.sample(s, 9000 + (s - 36) * 10, 2000 + (s - 36))
        self.assertEqual(result['windows']['60']['tokensPerSecond'], 10)
        self.assertEqual(result['windows']['60']['seconds'], 21)
        history = self.pulse.rate_history(self.account, 4, T, T + 60)
        self.assertTrue(any(r['rate60'] is None and r['at'] > T + 30 for r in history['samples']))

    def test_stale_offline_and_unmatched_sources_clear_the_current_reading(self):
        self.collect(30)
        self.assertEqual(self.sample(30, delay=16)['status'], 'stale')
        self.session['status'] = 'ended'
        self.assertEqual(self.sample(33)['status'], 'offline')
        self.session['status'] = 'active'
        self.raw['advertised_models'] = ['b']
        self.assertEqual(self.sample(36)['status'], 'unmatched')

    def test_shared_session_readiness_and_proof_are_required(self):
        self.session['performance']['status'] = 'paused'
        self.assertEqual(self.sample(0)['status'], 'paused')
        self.session['performance']['status'] = 'counting'
        self.tracking['verifiedAt'] = T + 10
        self.assertEqual(self.sample(0)['status'], 'paused')
        self.tracking['verifiedAt'] = None
        self.assertEqual(self.sample(3)['status'], 'paused')

    def test_gaps_clock_reversal_and_resets_establish_new_counter_baselines(self):
        self.collect(30)
        self.assertEqual(self.sample(60, 600, 60)['status'], 'warming')
        self.assertEqual(self.sample(57, 570, 57)['status'], 'warming')
        self.assertEqual(self.sample(60, 0, 0)['status'], 'warming')
        for s in range(63, 84, 3):
            result = self.sample(s, (s - 60) * 10, s - 60)
        self.assertEqual(result['windows']['60']['tokensPerSecond'], 10)

    def test_changed_counters_at_the_same_timestamp_are_not_a_burst(self):
        self.collect(30)
        result = self.sample(30, 5000, 500)
        self.assertEqual(result['status'], 'warming')
        self.assertEqual(result['windows']['60']['seconds'], 0)

    def test_account_session_device_model_set_and_proof_changes_break_continuity(self):
        mutations = [
            lambda: setattr(self, 'account', 'account-two'),
            lambda: self.session.update(id=5),
            lambda: self.raw.update(attestation_public_key='device-two'),
            lambda: self.raw.update(pid=999),
            lambda: (
                self.raw.update(advertised_models=['a', 'b']),
                self.session.update(models=['a', 'b']),
            ),
            lambda: self.tracking.update(verifiedAt=T + 30),
        ]
        for mutate in mutations:
            self.pulse = TrafficPulse(self.h)
            self.collect(30)
            mutate()
            self.assertEqual(self.sample(33, 330, 33)['status'], 'warming')

    def test_pair_totals_are_kept_joint_and_reordering_is_not_a_new_model_set(self):
        self.raw['advertised_models'] = ['a', 'b']
        self.session['models'] = ['a', 'b']
        self.collect(30)
        self.raw['advertised_models'] = ['b', 'a']
        result = self.sample(33, 330, 33)
        self.assertEqual(result['status'], 'live')
        self.assertEqual(result['models'], ['a', 'b'])
        self.assertNotIn('device-one', json.dumps(result))
        self.assertNotIn('account-one', json.dumps(result))

    def test_invalid_counters_do_not_synthesize_zero_or_hide_the_other_metric(self):
        for value in (None, True, -1, 1.5, 2**60, float('nan')):
            self.pulse = TrafficPulse(self.h)
            self.raw['stats']['requests_served'] = value
            for s in range(0, 24, 3):
                result = self.sample(s, s * 10)
            self.assertEqual(result['windows']['60']['tokensPerSecond'], 10)
            self.assertIsNone(result['windows']['60']['requestsPerMinute'])

    def test_malformed_model_members_fail_closed(self):
        for selected in (['a', None], [1], 'a', {}):
            self.raw['advertised_models'] = selected
            self.assertEqual(self.sample(0)['status'], 'unmatched')

    def test_reference_uses_earlier_observed_warm_time_and_survives_reopen(self):
        result = self.collect(600)
        self.assertEqual(result['baseline']['seconds'], 300)
        self.assertEqual(result['baseline']['tokensPerSecond'], 10)
        self.assertEqual(result['baseline']['requestsPerMinute'], 60)
        self.pulse = TrafficPulse(self.h)
        result = self.sample(603, 100000, 10000)
        self.assertEqual(result['status'], 'warming')
        self.assertEqual(result['baseline']['tokensPerSecond'], 10)

    def test_newest_burst_and_cold_time_do_not_bias_reference(self):
        self.collect(300)
        self.tracking['counting'] = False
        self.sample(330)
        self.tracking.update(counting=True, verifiedAt=T + 330)
        for s in range(333, 631, 3):
            result = self.sample(s, 3000 + (s - 333) * 100, 300 + (s - 333) * 10)
        self.assertEqual(result['baseline']['seconds'], 300)
        self.assertEqual(result['baseline']['tokensPerSecond'], 10)
        self.assertEqual(result['windows']['60']['tokensPerSecond'], 100)

    def test_short_reference_remains_unavailable(self):
        result = self.collect(570)
        self.assertEqual(result['baseline']['seconds'], 270)
        self.assertIsNone(result['baseline']['tokensPerSecond'])

    def test_missing_metric_does_not_permanently_poison_its_reference(self):
        self.raw['stats']['requests_served'] = None
        self.sample(0, 0)
        self.sample(3, 30)
        self.sample(6, 60, 6)
        for s in range(9, 634, 3):
            result = self.sample(s, s * 10, s)
        baseline = result['baseline']
        self.assertEqual(baseline['tokensPerSecond'], 10)
        self.assertEqual(baseline['requestsPerMinute'], 60)
        self.assertEqual(baseline['tokensSeconds'] - baseline['requestsSeconds'], 6)

    def test_reopening_never_bridges_an_unobserved_interval(self):
        self.collect(30)
        self.pulse = TrafficPulse(self.h)
        result = self.sample(60, 9999, 9999)
        self.assertEqual(result['status'], 'warming')
        self.assertEqual(result['windows']['60']['seconds'], 0)
        rows = self.pulse.rate_history(self.account, 4, T, T + 60)['samples']
        self.assertTrue(any(r['rate60'] is None and T + 30 < r['at'] < T + 60 for r in rows))

    def test_saved_chart_survives_reopening_a_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / 'history.sqlite3'
            h = History(path)
            pulse = TrafficPulse(h)
            pulse.save(
                'account', 4, 10, 13, 6, 1, {'60': {'tokensPerSecond': 2, 'requestsPerMinute': 20}}
            )
            h.close()
            h = History(path)
            try:
                rows = TrafficPulse(h).rate_history('account', 4, 0, 30)['samples']
                self.assertEqual(rows[0]['rate60'], 2)
            finally:
                h.close()


class TrafficHistoryTests(unittest.TestCase):
    def setUp(self):
        self.h = History(':memory:')
        self.pulse = TrafficPulse(self.h)

    def tearDown(self):
        self.h.close()

    def save(self, start, end, rate, account='account', session=4):
        windows = {
            '60': {
                'tokensPerSecond': rate,
                'requestsPerMinute': None if rate is None else rate * 6,
            },
            '300': {
                'tokensPerSecond': rate,
                'requestsPerMinute': None if rate is None else rate * 6,
            },
        }
        self.pulse.save(account, session, start, end, windows=windows)

    def test_history_cannot_borrow_another_account_or_session(self):
        self.save(10, 13, 2)
        self.save(10, 13, 999, account='other')
        self.save(10, 13, 999, session=5)
        self.assertEqual(self.pulse.rate_history('account', 4, 0, 100)['samples'][0]['rate60'], 2)
        self.assertEqual(self.pulse.rate_history('', 4, 0, 100)['samples'], [])
        self.assertEqual(self.pulse.rate_history('account', None, 0, 100)['samples'], [])
        self.assertEqual(self.pulse.rate_history('account', 999, 0, 100)['samples'], [])

    def test_metrics_validate_and_select_their_real_units(self):
        self.save(10, 13, 2)
        self.assertEqual(
            self.pulse.rate_history('account', 4, 0, 100, 'requests')['samples'][0]['rate60'], 12
        )
        with self.assertRaises(ValueError):
            self.pulse.rate_history('account', 4, 0, 100, 'invalid')

    def test_requested_boundaries_clip_intervals_and_weights(self):
        self.save(0, 2, 10)
        self.save(2, 6, 20)
        data = self.pulse.rate_history('account', 4, 1, 4)
        self.assertEqual(len(data['samples']), 1)
        self.assertAlmostEqual(data['samples'][0]['rate60'], (10 + 40) / 3)
        self.assertEqual(data['samples'][0]['at'], 2.5)

    def test_gaps_break_the_line_even_inside_large_buckets(self):
        self.save(0, 1, 10)
        self.save(2, 3, 20)
        self.assertIsNone(self.pulse.rate_history('account', 4, 0, 3)['samples'][0]['rate60'])
        self.save(100, 103, 0)
        rows = self.pulse.rate_history('account', 4, 0, 103)['samples']
        self.assertTrue(any(r['rate60'] is None and 3 < r['at'] < 100 for r in rows))
        self.assertEqual(rows[-1]['rate60'], 0)

    def test_null_markers_are_not_zero_and_taint_a_shared_bucket(self):
        self.save(0, 2, 10)
        self.save(2, 2, None)
        self.save(2, 3, 20)
        self.assertIsNone(self.pulse.rate_history('account', 4, 0, 3)['samples'][0]['rate60'])

    def test_large_queries_remain_bounded_and_time_weighted(self):
        for at in range(0, 36000, 3):
            self.save(at, at + 3, 10 if at < 18000 else 20)
        data = self.pulse.rate_history('account', 4, 0, 36000)
        self.assertLessEqual(len(data['samples']), 601)
        self.assertEqual(data['bucketSeconds'], 60)
        self.assertEqual(data['count'], 12000)
        self.assertEqual(data['samples'][0]['rate60'], 10)
        self.assertEqual(data['samples'][-1]['rate60'], 20)

    def test_no_points_outside_requested_saved_history(self):
        self.save(10, 13, 2)
        self.assertEqual(self.pulse.rate_history('account', 4, 20, 30)['samples'], [])
        self.assertEqual(self.pulse.rate_history('account', 4, 0, 5)['samples'], [])


if __name__ == '__main__':
    unittest.main()
