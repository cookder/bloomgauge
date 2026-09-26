"""Timestamp pairing, sample sufficiency and optimizer integration."""

import unittest
from datetime import datetime
from history import History
from optimizer_store import OptimizerStore
from demand_baselines import network_minutes, conditional, report, summary, periods
from demand_optimizer import estimate, decide, policy
from test_demand_optimizer import candidate, summary as forecast

NOW = datetime(2026, 9, 14, 12, 0).timestamp()
SIGNAL = {
    'status': 'normal',
    'coverage': 1,
    'observedAt': NOW - 30,
    'pressure': 2,
    'active': 8,
    'load': 10,
    'warmProviders': 5,
}


def sample(at, usd=0.3 / 60, pressure=2, active=8, warm=5, requests=2):
    return {
        'at': at,
        'seconds': 60,
        'usd': usd,
        'paidJobs': 2 if usd else 0,
        'requests': requests,
        'tokens': 120,
        'busy': 30,
        'pressure': pressure,
        'active': active,
        'queued': max(0, pressure * warm - active),
        'warm': warm,
        'load': pressure * warm,
    }


def evidence(rows):
    return {
        'minutes': rows,
        'hours': len(rows) / 60,
        'jobs': sum(r['paidJobs'] for r in rows),
        'days': len({datetime.fromtimestamp(r['at']).date() for r in rows}),
    }


def network(rows):
    return {
        r['at']: {k: r[k] for k in ('active', 'queued', 'warm', 'load', 'pressure')} for r in rows
    }


class PairingTests(unittest.TestCase):
    def setUp(self):
        self.h = History(':memory:')
        self.s = OptimizerStore(self.h)
        self.at = NOW - 3600
        self.s.identity('mac', 'provider')
        self.h.db.execute('INSERT INTO opt_coverage VALUES(?,?,?)', ('owner', 0, NOW))

    def tearDown(self):
        self.h.close()

    def warm(self, at=None, seconds=60, account='owner', device='mac', model='a'):
        at = self.at if at is None else at
        self.h.db.execute(
            'INSERT INTO opt_ready_minutes VALUES(?,?,?,?,?,?,?,?)',
            (account, device, at, model, seconds, 2, 120, 30),
        )

    def net(self, at=None, active=8, queued=2, warm=5, model='a'):
        at = self.at if at is None else at
        self.h.db.execute(
            'INSERT OR REPLACE INTO opt_network VALUES(?,?,?,?,?,?)',
            (at, model, active, queued, warm, 10),
        )

    def paid(self, account='owner', provider='provider', model='a', amount=5000):
        n = self.h.db.execute('SELECT COUNT(*) FROM opt_credits').fetchone()[0] + 1
        self.h.db.execute(
            'INSERT INTO opt_credits VALUES(?,?,?,?,?,?,?)',
            (account, n, provider, self.at + 30, model, amount, 120),
        )

    def get(self, **kwargs):
        return report(self.s, 'owner', 'mac', 0, NOW, NOW, {'a': SIGNAL}, **kwargs)

    def test_pairs_local_traffic_and_scoped_paid_credits_at_same_minute(self):
        self.warm()
        self.net(self.at + 5)
        self.net(self.at + 35)
        self.paid()
        for kwargs in (
            {'provider': 'other'},
            {'account': 'other'},
            {'model': 'base_reward'},
            {'model': 'b'},
        ):
            self.paid(**kwargs, amount=900000)
        r = self.get()
        s = r['summary']
        self.assertEqual(s['usd'], 0.005)
        self.assertEqual(s['requestsPerMinute'], 2)
        self.assertEqual(s['tokensPerSecond'], 2)
        self.assertEqual(s['pressure'], 2)
        self.assertEqual(s['active'], 8)
        self.assertEqual(s['warm'], 5)
        self.assertEqual(s['busyPercent'], 50)

    def test_sparse_and_missing_network_are_unknown_not_zero(self):
        self.warm()
        self.paid()
        self.net(self.at + 5)
        r = self.get()
        self.assertEqual(r['summary']['hours'], 0)
        self.assertIsNone(r['summary']['usdPerHour'])
        self.assertEqual(r['baseline']['totalHours'], 1 / 60)

    def test_duplicate_poll_buckets_cannot_fake_coverage(self):
        self.warm()
        self.net(self.at + 5)
        self.net(self.at + 10)
        self.assertEqual(self.get()['summary']['hours'], 0)
        self.net(
            self.at + 32
        )  # Separate halves but less than 15 seconds apart after a late update.
        self.net(self.at + 28)
        self.assertEqual(self.get()['summary']['hours'], 0)

    def test_ratio_is_mean_of_concurrent_ratios_not_ratio_of_means(self):
        self.warm()
        self.net(self.at + 5, active=10, queued=0, warm=1)
        self.net(self.at + 35, active=10, queued=0, warm=10)
        self.assertEqual(self.get()['summary']['pressure'], 5.5)

    def test_zero_capacity_negative_and_null_samples_do_not_calibrate(self):
        self.warm()
        self.net(self.at + 5)
        for changes in ({'warm': 0}, {'active': -1}, {'queued': None}):
            self.net(self.at + 35, **changes)
            self.assertEqual(self.get()['summary']['hours'], 0)

    def test_genuine_covered_warm_idle_is_zero_earnings(self):
        self.warm()
        self.net(self.at + 5)
        self.net(self.at + 35)
        r = self.get()
        self.assertEqual(r['summary']['usdPerHour'], 0)
        self.assertFalse(r['baseline']['usable'])
        self.assertEqual(r['baseline']['weight'], 1)

    def test_cold_boundary_coverage_and_settlement_excluded(self):
        self.warm(seconds=59)
        self.net(self.at + 5)
        self.net(self.at + 35)
        self.paid()
        self.assertEqual(self.get()['summary']['hours'], 0)
        self.h.db.execute('UPDATE opt_ready_minutes SET seconds=60')
        self.h.db.execute('DELETE FROM opt_coverage')
        self.assertEqual(self.get()['summary']['hours'], 0)
        self.h.db.execute('INSERT INTO opt_coverage VALUES(?,?,?)', ('owner', 0, NOW))
        r = report(self.s, 'owner', 'mac', 0, self.at + 60, self.at + 179, {'a': SIGNAL})
        self.assertEqual(r['summary']['hours'], 0)

    def test_other_device_and_pair_times_never_become_solo_evidence(self):
        self.warm(device='other')
        self.warm(model=__import__('model_combinations').selection_key(['a', 'b']))
        self.net(self.at + 5)
        self.net(self.at + 35)
        self.paid()
        self.assertEqual(self.get(selected='a')['summary']['hours'], 0)

    def test_custom_period_requires_whole_warm_minutes_and_boundaries(self):
        self.warm()
        self.net(self.at + 5)
        self.net(self.at + 35)
        self.paid()
        r = report(self.s, 'owner', 'mac', self.at + 1, NOW, NOW, {'a': SIGNAL})
        self.assertEqual(r['summary']['hours'], 0)
        for start, end in ((0, 0), (0, float('inf')), (-1, NOW), (True, NOW)):
            with self.assertRaises(ValueError):
                report(self.s, 'owner', 'mac', start, end, NOW)

    def test_no_future_network_or_future_paid_periods(self):
        self.warm(NOW + 60)
        self.net(NOW + 65)
        self.net(NOW + 95)
        self.assertEqual(self.get()['summary']['hours'], 0)


class ConditionalTests(unittest.TestCase):
    def many(self):
        # Twelve hours at today's time across three weekday dates.
        return [
            sample(NOW - day * 86400 - 2 * 3600 + i * 60)
            for day in (1, 4, 5, 6)
            for i in range(240)
        ]

    def cond(self, rows, signal=None, now=NOW):
        return conditional(evidence(rows), network(rows), signal or SIGNAL, now)

    def test_sparse_zero_or_jackpot_stays_neutral(self):
        for usd in (0, 100):
            rows = [sample(NOW - 3600 + i * 60, usd) for i in range(30)]
            r = self.cond(rows)
            self.assertFalse(r['usable'])
            self.assertEqual(r['weight'], 1)

    def test_active_and_capacity_distinguish_equal_pressure(self):
        rows = self.many()
        for signal in (
            {**SIGNAL, 'active': 80, 'load': 100, 'warmProviders': 50},
            {**SIGNAL, 'active': 1},
        ):
            r = self.cond(rows, signal)
            self.assertEqual(r['hours'], 0)
            self.assertFalse(r['usable'])

    def test_near_zero_active_can_match_without_division_by_zero(self):
        rows = [sample(NOW - 3600 + i * 60, active=0) for i in range(30)]
        self.assertEqual(self.cond(rows, {**SIGNAL, 'active': 0})['hours'], 0.5)

    def test_fresh_current_context_required(self):
        for changed in (
            {'observedAt': NOW - 91},
            {'observedAt': NOW + 1},
            {'warmProviders': 0},
            {'active': None},
            {'coverage': 0.79},
        ):
            r = self.cond(self.many(), {**SIGNAL, **changed})
            self.assertIsNone(r['current'])
            self.assertFalse(r['usable'])

    def test_repeated_time_context_can_refine_but_lone_slot_cannot(self):
        rows = self.many()
        r = self.cond(rows)
        self.assertTrue(r['usable'])
        self.assertEqual(r['scope'], 'daytype_time')
        self.assertGreaterEqual(r['days'], 2)
        self.assertLess(r['hours'], 16)  # Sunday is not a weekday.
        rows = [sample(NOW - 86400 + i * 60) for i in range(30)]
        self.assertEqual(self.cond(rows)['scope'], 'similar_demand')

    def test_global_network_coverage_and_staleness_limit_forecasting(self):
        rows = self.many()
        e = evidence(rows)
        self.assertFalse(
            conditional(
                e, {k: v for i, (k, v) in enumerate(network(rows).items()) if i % 2}, SIGNAL, NOW
            )['usable']
        )
        old = [{**m, 'at': m['at'] - 14 * 86400} for m in rows]
        self.assertFalse(self.cond(old)['usable'])

    def test_sparse_substantial_blocks_do_not_count_as_independent_hours(self):
        rows = [sample(NOW - day * 86400 - i * 1800) for day in (1, 4, 5) for i in range(100)]
        self.assertFalse(self.cond(rows)['usable'])

    def test_conditional_forecast_depends_on_comparable_work_not_total_history(self):
        rows = self.many() + [
            sample(NOW - day * 86400 - 10 * 3600 + i * 60, usd=0.05 / 60, pressure=0.1, active=0)
            for day in (1, 4, 5, 6)
            for i in range(240)
        ]
        e = evidence(rows)
        c = self.cond(rows)
        r = estimate(e, network(rows), SIGNAL, NOW, condition=c)
        self.assertTrue(r['forecastUsable'])
        self.assertAlmostEqual(r['rate'], 0.3)
        self.assertEqual(c['weight'], 1.2)
        relevant = self.many()
        e = evidence(relevant)
        c = self.cond(relevant)
        r = estimate(e, network(relevant), SIGNAL, NOW, condition=c)
        self.assertLess(e['hours'], 24)
        self.assertFalse(r['established'])
        self.assertTrue(r['forecastUsable'])
        self.assertAlmostEqual(r['rate'], 0.3)

    def test_quiet_or_unmeasured_past_does_not_dilute_current_demand(self):
        relevant = self.many()
        quiet = [
            sample(NOW - day * 86400 - 10 * 3600 + i * 60, usd=0, pressure=0.1, active=0)
            for day in (1, 4, 5, 6)
            for i in range(240)
        ]
        all_rows = relevant + quiet
        c = conditional(evidence(all_rows), network(relevant), SIGNAL, NOW)
        self.assertLess(c['coverage'], 0.8)
        self.assertEqual(c['matchingCoverage'], 1)
        self.assertTrue(c['forecastUsable'])
        self.assertAlmostEqual(c['usdPerHour'], 0.3)
        c = self.cond(all_rows)
        self.assertAlmostEqual(c['usdPerHour'], 0.3)
        self.assertEqual(c['otherDemandHours'], 16)
        self.assertGreater(c['otherContextHours'], 0)

    def test_matched_paid_jobs_and_dates_are_required_even_with_large_unmatched_history(self):
        rows = [{**m, 'paidJobs': 0} for m in self.many()]
        c = self.cond(rows)
        self.assertTrue(c['usable'])
        self.assertFalse(c['forecastUsable'])
        e = evidence(rows)
        e['jobs'] = 999999
        e['hours'] = 999
        self.assertFalse(estimate(e, network(rows), SIGNAL, NOW, condition=c)['forecastUsable'])
        one_day = [sample(NOW - 12 * 3600 + i * 60) for i in range(600)]
        self.assertFalse(self.cond(one_day)['forecastUsable'])

    def test_sparse_jackpot_cannot_inflate_repeated_comparison(self):
        rows = self.many() + [sample(NOW - 3 * 3600, usd=100)]
        c = self.cond(rows)
        self.assertTrue(c['forecastUsable'])
        self.assertAlmostEqual(c['usdPerHour'], 0.3)

    def test_no_similar_demand_and_stale_data_do_not_become_zero_forecasts(self):
        for signal in (
            {**SIGNAL, 'pressure': 200, 'active': 800, 'load': 1000},
            {**SIGNAL, 'observedAt': NOW - 100},
        ):
            c = self.cond(self.many(), signal)
            self.assertIsNone(c['usdPerHour'])
            self.assertFalse(c['forecastUsable'])

    def test_repeated_matched_paid_gain_can_qualify_without_unrelated_hours(self):
        rows = self.many()
        c = self.cond(rows)
        e = evidence(rows)
        predicted = estimate(e, network(rows), SIGNAL, NOW, condition=c)
        decision = decide(
            [candidate('a'), candidate('b')],
            'a',
            {'a': forecast(0.1), 'b': predicted},
            [],
            [],
            policy(),
            NOW,
        )
        self.assertEqual(decision['target'], 'b')
        self.assertEqual(decision['kind'], 'earnings')
        # One short jackpot cannot support the same action.
        short = rows[:20]
        c = self.cond(short)
        predicted = estimate(evidence(short), network(short), SIGNAL, NOW, condition=c)
        decision = decide(
            [candidate('a'), candidate('b')],
            'a',
            {'a': forecast(0.1), 'b': predicted},
            [],
            [],
            policy(),
            NOW,
        )
        self.assertIsNone(decision['target'])

    def test_two_thin_exact_weekdays_do_not_discard_repeated_daytype_evidence(self):
        rows = self.many() + [
            sample(NOW - day * 86400 - 3600 + i * 60) for day in (7, 14) for i in range(60)
        ]
        c = self.cond(rows)
        self.assertEqual(c['scope'], 'daytype_time')
        self.assertTrue(c['forecastUsable'])
        self.assertGreaterEqual(c['days'], 3)

    def test_neutral_new_model_remains_a_trial_candidate_with_compared_history(self):
        from demand_optimizer import sustained_demand
        from test_demand_optimizer import NOW as TEST_NOW

        rows = [candidate('a'), candidate('new')]
        for r in rows:
            r['signal']['sustained'] = {'qualified': True, 'pressure': 2}
            r['conditional'] = self.cond([])
        d = decide(
            rows,
            'a',
            {'a': forecast(0.15), 'new': None},
            [],
            [],
            policy(),
            TEST_NOW,
            activity={'fresh': True, 'idleSeconds': 1200},
        )
        self.assertIsNone(d['target'])
        # Comparative trials require a fresh covered incumbent benchmark.
        # A historical estimate alone cannot authorize the experiment.
        rows[0]['earningsTarget'] = {
            'ready': False,
            'rate': 0,
            'fastRate': 0,
            'asOf': TEST_NOW - 120,
            'warmMinutes': 30,
            'usdPerHour': 0.12,
            'productiveFloor': 0.09,
            'livePaid': {'fresh': True, 'rate': 0},
            'highEarnings': {'active': False},
        }
        d = decide(
            rows,
            'a',
            {'a': forecast(0.15), 'new': None},
            [],
            [],
            policy(),
            TEST_NOW,
            activity={'fresh': True, 'idleSeconds': 1200},
        )
        self.assertEqual(d['target'], 'new')
        self.assertEqual(d['kind'], 'explore')
        self.assertIsNone(d['opportunities'][0]['netGainUsd'])

    def test_historical_trial_adjustments_share_one_cap(self):
        rows = [candidate('a'), candidate('b')]
        rows[1]['conditional'] = {'usable': True, 'weight': 100}
        for r in rows:
            r['signal'].update(sustained={'qualified': True, 'pressure': 2}, pressureRatio=1)
        d = decide(
            rows, 'a', {}, [], [], policy(), NOW, activity={'fresh': True, 'idleSeconds': 1200}
        )
        b = next(r for r in d['opportunities'] if r['model'] == 'b')
        self.assertEqual(b['historyWeight'], 1.2)
        self.assertEqual(b['pressureScore'], 2.4)

    def test_display_periods_bounded_but_preserve_all_matched_seconds(self):
        rows = [sample(NOW - i * 3600) for i in range(900)]
        step = max(
            1800,
            __import__('math').ceil(
                (max(m['at'] for m in rows) - min(m['at'] for m in rows)) / 600 / 1800
            )
            * 1800,
        )
        p = periods(rows, step)
        self.assertLessEqual(len(p), 602)
        self.assertAlmostEqual(sum(r['hours'] for r in p), 15)


if __name__ == '__main__':
    unittest.main()

import test_demand_http
import urllib.error, json
from unittest.mock import patch, Mock


class BaselineHTTPTests(unittest.TestCase):
    setUp = test_demand_http.DemandHTTPTests.setUp
    tearDown = test_demand_http.DemandHTTPTests.tearDown
    read = test_demand_http.DemandHTTPTests.read
    denied = test_demand_http.DemandHTTPTests.denied

    def test_owner_only_reads_are_scoped_and_never_accept_identity_from_query(self):
        c = self.local.RequestHandlerClass.collector
        c.optimizer.live = {'account': 'current-owner', 'device': 'this-mac'}
        c.optimizer.demand_auto.alerts.snapshot = Mock(return_value={'models': []})
        path = '/api/optimizer/baselines?from=0&to=200&account=foreign&device=foreign&model=a'
        with patch('demand_baselines.report', return_value={'models': []}) as fn:
            for server, headers in ((self.local, {}), (self.phone, self.headers)):
                with self.read(server, path, headers) as response:
                    self.assertEqual(json.load(response), {'models': []})
                args = fn.call_args.args
                self.assertEqual(args[1:5], ('current-owner', 'this-mac', 0, 200))
                self.assertEqual(args[-1], 'a')
            self.denied(self.phone, path)
            self.denied(
                self.phone, path, {**self.headers, 'Tailscale-User-Login': 'foreign@example.com'}
            )
            self.denied(self.phone, path, {**self.headers, 'Origin': 'https://foreign.example'})

    def test_ranges_are_bounded_and_empty_is_valid(self):
        for server, headers in ((self.local, {}), (self.phone, self.headers)):
            with self.read(server, '/api/optimizer/baselines?from=0', headers) as response:
                value = json.load(response)
                self.assertEqual(value['periods'], [])
                self.assertIsNone(value['summary']['usdPerHour'])
            for q in ('from=-1', 'from=nan', 'to=inf', 'from=20&to=10', 'model=' + 'a' * 513):
                with self.assertRaises(urllib.error.HTTPError) as ex:
                    self.read(server, '/api/optimizer/baselines?' + q, headers)
                self.assertEqual(ex.exception.code, 400)


class ConcurrentHistoryTests(unittest.TestCase):
    def test_demand_read_snapshot_is_readonly_consistent_and_allows_live_writes(self):
        import tempfile, sqlite3, threading
        from pathlib import Path
        from demand_baselines import read_view

        with tempfile.TemporaryDirectory() as directory:
            h = History(Path(directory) / 'with spaces.sqlite3')
            s = OptimizerStore(h)
            h.db.execute('INSERT INTO opt_network VALUES(?,?,?,?,?,?)', (1, 'a', 1, 0, 1, 1))
            h.db.commit()
            try:
                with read_view(s) as view:
                    self.assertEqual(
                        view.h.db.execute('SELECT active FROM opt_network').fetchone()[0], 1
                    )
                    result = []

                    def write():
                        with h.lock:
                            h.db.execute('UPDATE opt_network SET active=2')
                            h.db.commit()
                            result.append('written')

                    worker = threading.Thread(target=write)
                    worker.start()
                    worker.join(1)
                    self.assertEqual(result, ['written'])
                    self.assertEqual(
                        view.h.db.execute('SELECT active FROM opt_network').fetchone()[0], 1
                    )
                    with self.assertRaises(sqlite3.OperationalError):
                        view.h.db.execute('DELETE FROM opt_network')
                self.assertEqual(h.db.execute('SELECT active FROM opt_network').fetchone()[0], 2)
            finally:
                h.close()
