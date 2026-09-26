import json
import pathlib
import random
import tempfile
import threading
import time
import unittest
from datetime import datetime
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo
from history import History
from optimizer_store import OptimizerStore
from model_combinations import selection_key
import model_insights as insights


class InsightsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.h = History(pathlib.Path(self.tmp.name) / 'history.sqlite3')
        self.addCleanup(self.h.close)
        self.store = OptimizerStore(self.h)
        self.zone = ZoneInfo('UTC')
        self.now = datetime(2026, 9, 22, 12, tzinfo=self.zone).timestamp()
        self.store.identity('mac', 'provider')
        self.store.identity('other-mac', 'foreign')
        self.h.db.execute(
            'INSERT INTO opt_coverage VALUES(?,?,?)',
            ('account', self.now - insights.LOOKBACK, self.now),
        )
        self.h.db.commit()
        self.credit_id = 0

    def ready(self, at, model='a', seconds=60, account='account', device='mac'):
        self.h.db.execute(
            'INSERT OR REPLACE INTO opt_ready_minutes VALUES(?,?,?,?,?,?,?,?)',
            (account, device, at, model, seconds, 1, 100, 10),
        )

    def credit(
        self,
        at,
        model='a',
        usd=1000,
        tokens=100,
        details=False,
        account='account',
        provider='provider',
    ):
        self.credit_id += 1
        self.h.db.execute(
            'INSERT INTO opt_credits VALUES(?,?,?,?,?,?,?)',
            (account, self.credit_id, provider, at, model, usd, tokens or 0),
        )
        if details:
            self.h.db.execute(
                'INSERT INTO workload_tokens VALUES(?,?,?,?)',
                (account, self.credit_id, None, tokens),
            )

    def seasonal(self, warm=10, active=4, queued=1, local=True, days=(7, 14, 21)):
        for ago in days:
            for minute in range(-120, 600):  # 10:00-22:00: supports neighboring clock cohorts.
                at = self.now - ago * 86400 + minute * 60
                if local:
                    self.ready(at)
                    self.credit(at + 10)
                for offset in (2, 32):
                    self.h.db.execute(
                        'INSERT INTO opt_network VALUES(?,?,?,?,?,?)',
                        (at + offset, 'a', active, queued, warm, warm),
                    )
        self.h.db.commit()

    def result(self, models=('a',), zone=None):
        self.h.db.commit()
        return insights.report(self.store, 'account', 'mac', models, zone or self.zone, self.now)

    def test_paid_scope_idle_denominator_adjustments_and_missing_vs_zero_output(self):
        at = self.now - 600
        self.ready(at)
        self.ready(at + 60)
        self.credit(at + 1, usd=10000, tokens=20, details=True)
        self.credit(at + 2, usd=10000, tokens=0, details=True)
        self.credit(at + 3, usd=10000, tokens=0)  # ambiguous legacy zero stays absent
        self.credit(at + 4, usd=-5000, tokens=9999, details=True)
        self.credit(at + 5, usd=1000000, provider='foreign')
        self.credit(at + 6, usd=1000000, account='other-account')
        self.credit(at + 7, model='base_reward', usd=1000000)
        self.ready(at + 120, selection_key(['a', 'b']))
        self.credit(at + 121, usd=1000000)  # pair earnings cannot enter solo evidence
        self.credit(at + 181, usd=1000000)  # unverified/cold
        self.ready(at + 240, seconds=30)
        self.credit(at + 241, usd=1000000)
        self.ready(self.now - 60)
        self.credit(self.now - 59, usd=1000000)  # unsettled
        changes = self.h.db.total_changes
        row = self.result()['models'][0]
        self.assertAlmostEqual(row['observed']['confirmedInferenceUsd'], 0.025)
        self.assertAlmostEqual(row['observed']['usdPerWarmHour'], 0.75)
        self.assertAlmostEqual(row['observed']['warmHours'], 2 / 60)
        self.assertEqual(
            (row['observed']['creditedRequests'], row['observed']['adjustments']), (3, 1)
        )
        self.assertEqual(row['requestSize']['meanOutputTokens'], 10)
        self.assertEqual(row['requestSize']['outputSamples'], 2)
        self.assertAlmostEqual(row['requestSize']['outputCoverage'], 2 / 3)
        self.assertEqual(self.h.db.total_changes, changes)

    def test_covered_idle_zero_differs_from_missing_model(self):
        self.ready(self.now - 600)
        rows = self.result(('a', 'missing'))['models']
        self.assertEqual(rows[0]['observed']['usdPerWarmHour'], 0)
        self.assertEqual(rows[0]['observed']['status'], 'observed')
        self.assertIsNone(rows[0]['requestSize']['meanOutputTokens'])
        self.assertIsNone(rows[1]['observed']['usdPerWarmHour'])
        self.assertEqual(rows[1]['observed']['status'], 'unknown')

    def test_credit_poll_gap_is_unknown_not_zero_or_measured_work(self):
        self.ready(self.now - 600)
        self.credit(self.now - 590)
        self.h.db.execute('DELETE FROM opt_coverage')
        row = self.result()['models'][0]
        self.assertIsNone(row['observed']['usdPerWarmHour'])
        self.assertEqual(row['requestSize']['outputSamples'], 0)

    def assert_evidence_parity(self, start, end, models=('a', 'b')):
        self.h.db.commit()
        original = self.store.evidence('account', 'mac', start, end, self.now)
        actual = insights.warm_evidence(
            self.store, 'account', 'mac', models, start, end, self.now, None
        )
        wanted = {
            m: {k: original[m][k] for k in ('hours', 'usd', 'jobs', 'usdPerHour', 'minutes')}
            for m in models
            if m in original and len(insights.members(m)) == 1
        }
        self.assertEqual(actual, wanted)
        return actual

    def test_narrow_evidence_matches_single_interval_exists_without_merging_coverage(self):
        at = self.now - 1800
        self.h.db.execute('DELETE FROM opt_coverage')
        intervals = [
            (at, at + 60),
            (at + 60, at + 90),
            (at + 90, at + 120),
            (at + 120, at + 170),
            (at + 150, at + 190),
            (at + 179, at + 241),
            (at + 185, at + 195),
            (at + 241, at + 300),
        ]
        self.h.db.executemany(
            'INSERT INTO opt_coverage VALUES(?,?,?)', [('account', a, b) for a, b in intervals]
        )
        for i in range(5):
            self.ready(at + i * 60)
            self.credit(at + i * 60 + 1)
        result = self.assert_evidence_parity(at, self.now)['a']
        self.assertEqual([m['at'] for m in result['minutes']], [at, at + 180])

    def test_narrow_evidence_differential_scope_boundaries_adjustments_and_subsets(self):
        rng = random.Random(7041)
        start = self.now - 86400
        self.h.db.execute('DELETE FROM opt_coverage')
        for i in range(200):
            at = start + i * 120
            self.h.db.execute(
                'INSERT INTO opt_coverage VALUES(?,?,?)',
                ('account', at + rng.choice([-30, 0, 30]), at + rng.choice([30, 60, 75, 240])),
            )
        combo = selection_key(['a', 'b'])
        for i in range(430):
            at = start + i * 60
            model = rng.choice(['a', 'b', 'other', combo])
            self.ready(
                at,
                model,
                rng.choice([60, 60, 59, 59.999999, 60.000001, 60.000002]),
                account=rng.choice(['account', 'account', 'foreign']),
                device=rng.choice(['mac', 'mac', 'other-mac']),
            )
            for _ in range(rng.randrange(4)):
                self.credit(
                    at + rng.randrange(60),
                    model=rng.choice(['a', 'b', 'other', 'base_reward']),
                    usd=rng.choice([-3000, 0, 1000, 10000]),
                    provider=rng.choice(['provider', 'provider', 'foreign']),
                )
        for offset in (-240, -180, -120, -60, 0, 60):
            self.ready(self.now + offset)
            self.credit(self.now + offset + 1)
        self.h.db.execute(
            'INSERT INTO opt_coverage VALUES(?,?,?)', ('account', self.now - 300, self.now + 300)
        )
        for models in (['a'], ['b', 'a'], ['missing', 'a', combo], [combo]):
            self.assert_evidence_parity(start, self.now + 180, models)
            self.assert_evidence_parity(start + 17, start + 13000, models)

    def test_complete_report_is_identical_to_original_evidence_reader(self):
        self.seasonal()
        result = self.result(('a', 'missing'))

        def original(view, account, device, models, start, end, now, deadline):
            data = view.evidence(account, device, start, end, now)
            return {m: data[m] for m in models if m in data and len(insights.members(m)) == 1}

        with patch.object(insights, 'warm_evidence', side_effect=original):
            reference = self.result(('a', 'missing'))
        self.assertEqual(result, reference)

    def test_complete_supported_window_and_unique_reused_evidence(self):
        self.seasonal()
        changes = self.h.db.total_changes
        with (
            patch('subprocess.run', side_effect=AssertionError('No CLI')),
            patch('urllib.request.urlopen', side_effect=AssertionError('No network')),
        ):
            value = self.result()
        row = value['models'][0]
        self.assertEqual(row['demandNext8h']['scope'], 'whole_network_model')
        self.assertEqual(row['demandNext8h']['meanConcurrentRequests'], 5)
        self.assertEqual(row['demandNext8h']['qualifiedHistoryHours'], 24)
        self.assertEqual(row['incomeNext8h']['status'], 'conditional')
        self.assertAlmostEqual(row['incomeNext8h']['usdPerWarmHour'], 0.06)
        self.assertEqual(row['incomeNext8h']['supportedSeconds'], 28800)
        self.assertEqual(row['incomeNext8h']['matchedWarmHours'], 36)
        self.assertEqual(row['incomeNext8h']['matchedCreditedRequests'], 2160)
        self.assertEqual(value['horizon']['to'] - value['horizon']['from'], 28800)
        self.assertEqual(self.h.db.total_changes, changes)
        json.dumps(value, allow_nan=False)

    def test_unknown_last_hour_suppresses_both_full_horizon_headlines(self):
        self.seasonal()
        for ago in (7, 14, 21):
            begin = self.now - ago * 86400 + 7 * 3600
            self.h.db.execute('DELETE FROM opt_network WHERE at>=? AND at<?', (begin, begin + 3600))
        row = self.result()['models'][0]
        for name in ('demandNext8h', 'incomeNext8h'):
            self.assertEqual(row[name]['status'], 'partial')
            self.assertEqual(row[name]['supportedSeconds'], 7 * 3600)
        self.assertIsNone(row['demandNext8h']['meanConcurrentRequests'])
        self.assertIsNone(row['incomeNext8h']['usdPerWarmHour'])

    def test_zero_warm_load_is_still_demand_but_never_income(self):
        self.seasonal(warm=0, active=0, queued=0, local=False)
        row = self.result()['models'][0]
        self.assertEqual(row['demandNext8h']['meanConcurrentRequests'], 0)
        self.assertEqual(row['demandNext8h']['status'], 'historical_pattern')
        self.assertEqual(row['incomeNext8h']['status'], 'unknown')

    def test_daytype_fallback_and_stale_history(self):
        self.seasonal(days=(1, 5, 6))  # Monday/Thursday/Wednesday, all weekdays
        row = self.result()['models'][0]
        self.assertEqual(row['demandNext8h']['basis'], 'daytype_hour')
        self.h.db.execute('UPDATE opt_network SET at=at-14*86400')
        row = self.result()['models'][0]
        self.assertEqual(row['demandNext8h']['status'], 'unknown')
        self.assertIsNone(row['incomeNext8h']['usdPerWarmHour'])

    def test_duplicate_polls_do_not_fill_a_missing_cadence_half(self):
        self.seasonal(local=False)
        self.h.db.execute('DELETE FROM opt_network WHERE CAST(at AS INT)%60=32')
        rows = self.h.db.execute('SELECT * FROM opt_network').fetchall()
        for row in rows:
            for offset in (5, 10, 15):
                self.h.db.execute(
                    'INSERT INTO opt_network VALUES(?,?,?,?,?,?)',
                    (
                        row['at'] + offset,
                        row['model'],
                        row['active'],
                        row['queued'],
                        row['warm'],
                        row['routable'],
                    ),
                )
        self.assertEqual(self.result()['models'][0]['demandNext8h']['status'], 'unknown')

    def test_negative_adjusted_income_cannot_forecast(self):
        self.seasonal()
        self.h.db.execute('UPDATE opt_credits SET micro_usd=-1000')
        row = self.result()['models'][0]
        self.assertLess(row['observed']['usdPerWarmHour'], 0)
        self.assertIsNone(row['incomeNext8h']['usdPerWarmHour'])
        self.assertEqual(row['requestSize']['outputSamples'], 0)

    def test_equal_pressure_at_a_different_load_and_supply_scale_is_not_comparable(self):
        self.seasonal()
        with insights.read_view(self.store) as view:
            earned = view.evidence(
                'account', 'mac', self.now - insights.LOOKBACK, self.now, self.now
            )['a']
            network = insights.network_minutes(
                view, ['a'], self.now - insights.LOOKBACK, self.now - 120
            )['a']
        part = insights.segments(self.now, self.now + 3600, self.zone)[0]
        target = {'pressure': 0.5, 'warm': 100, 'active': 40, 'load': 50}
        result = insights.income_part(
            earned,
            network,
            target,
            part,
            self.now,
            self.zone,
            {m['at']: 1 for m in earned['minutes']},
        )
        self.assertFalse(result['supported'])
        self.assertFalse(result['rows'])

    def test_dates_use_viewer_timezone_and_future_records_never_leak(self):
        at = self.now - 12 * 3600 - 60
        self.ready(at)
        self.credit(at + 1)
        self.ready(at + 60)
        self.credit(at + 61)
        self.ready(self.now + 600)
        self.credit(self.now + 601, usd=1000000)
        utc = self.result()['models'][0]['observed']
        viewer = self.result(zone=ZoneInfo('America/Chicago'))['models'][0]['observed']
        self.assertEqual((utc['days'], viewer['days']), (2, 1))
        self.assertEqual(utc['confirmedInferenceUsd'], 0.002)
        self.assertEqual(utc['confirmedInferenceUsd'], viewer['confirmedInferenceUsd'])

    def test_wal_read_does_not_hold_writer_lock_and_keeps_one_snapshot(self):
        self.ready(self.now - 600)
        self.credit(self.now - 599)
        self.h.db.commit()
        entered = threading.Event()
        release = threading.Event()
        results = []
        errors = []
        original = insights.calendar_load

        def slow(*args):
            entered.set()
            release.wait(3)
            return original(*args)

        def run():
            try:
                results.append(self.result())
            except Exception as error:
                errors.append(error)

        with patch.object(insights, 'calendar_load', side_effect=slow):
            worker = threading.Thread(target=run)
            worker.start()
            self.assertTrue(entered.wait(1))
            try:
                self.assertTrue(self.h.lock.acquire(timeout=0.1))
                try:
                    self.credit(self.now - 598, usd=999999)
                    self.h.db.commit()
                finally:
                    self.h.lock.release()
            finally:
                release.set()
                worker.join(3)
        self.assertFalse(errors)
        self.assertEqual(results[0]['models'][0]['observed']['confirmedInferenceUsd'], 0.001)

    def test_timeout_is_explicit_and_does_not_leave_a_broken_database(self):
        service = insights.ModelInsights(self.store, lambda: ('account', 'mac'), timeout=0)
        with self.assertRaises(insights.InsightsUnavailable):
            service.get('timezone=UTC&model=a', self.now)
        self.assertFalse(service.inflight)
        self.assertEqual(self.h.db.execute('SELECT 1').fetchone()[0], 1)

    def test_query_is_bounded_and_does_not_accept_scope_or_arbitrary_horizons(self):
        for raw in (
            '',
            'timezone=UTC',
            'timezone=bad/zone&model=a',
            'timezone=UTC&timezone=UTC&model=a',
            'timezone=UTC&model=a&model=a',
            'timezone=UTC&model=',
            'timezone=UTC&model=a&account=other',
            'timezone=UTC&model=a&hours=100',
            'timezone=UTC&model=' + ('a' * 513),
            'timezone=UTC&' + '&'.join('model=' + str(i) for i in range(33)),
        ):
            with self.subTest(raw=raw[:80]), self.assertRaises(ValueError):
                insights.query(raw)
        models, zone = insights.query('timezone=America%2FChicago&model=org%2Fmodel')
        self.assertEqual((models, zone.key), (['org/model'], 'America/Chicago'))


class TimeAndCacheTests(unittest.TestCase):
    def test_eight_elapsed_hours_across_dst_and_nonhour_zones(self):
        cases = [
            ('America/Chicago', (2026, 3, 8, 0, 30)),
            ('America/Chicago', (2026, 11, 1, 0, 30)),
            ('Australia/Lord_Howe', (2026, 10, 4, 0, 15)),
            ('Asia/Kathmandu', (2026, 9, 22, 23, 17)),
        ]
        for name, clock in cases:
            zone = ZoneInfo(name)
            start = datetime(*clock, tzinfo=zone).timestamp()
            parts = insights.segments(start, start + 28800, zone)
            self.assertEqual(sum(p['to'] - p['from'] for p in parts), 28800)
            self.assertEqual(parts[0]['from'], start)
            self.assertEqual(parts[-1]['to'], start + 28800)
            self.assertTrue(all(a['to'] == b['from'] for a, b in zip(parts, parts[1:])))
        spring = insights.segments(
            datetime(2026, 3, 8, 0, 30, tzinfo=ZoneInfo('America/Chicago')).timestamp(),
            datetime(2026, 3, 8, 0, 30, tzinfo=ZoneInfo('America/Chicago')).timestamp() + 28800,
            ZoneInfo('America/Chicago'),
        )
        self.assertNotIn(2, [p['hour'] for p in spring])
        fall_start = datetime(2026, 11, 1, 0, 30, tzinfo=ZoneInfo('America/Chicago')).timestamp()
        fall = insights.segments(fall_start, fall_start + 28800, ZoneInfo('America/Chicago'))
        self.assertEqual(sum(p['hour'] == 1 for p in fall), 2)

    def test_historical_fold_exposure_and_unfinished_hour(self):
        zone = ZoneInfo('America/Chicago')
        start = datetime(2026, 11, 1, 0, tzinfo=zone).timestamp()
        end = datetime(2026, 11, 1, 4, 30, tzinfo=zone).timestamp()
        hours = insights.historical_hours(start, end, zone)
        self.assertEqual(hours[('2026-11-01', 1)]['seconds'], 7200)
        self.assertNotIn(('2026-11-01', 4), hours)

    def test_cache_preserves_origin_invalidates_scope_and_request_order(self):
        scope = ['account', 'mac']
        service = insights.ModelInsights(None, lambda: tuple(scope))

        def fake(store, account, device, models, zone, now, deadline):
            return {'at': now, 'models': [{'id': m, 'owner': account} for m in models]}

        with patch.object(insights, 'report', side_effect=fake) as calculate:
            service.get('timezone=UTC&model=a&model=b', 1000)
            out = service.get('timezone=UTC&model=b&model=a', 1030)
            self.assertEqual(out['at'], 1000)
            self.assertEqual([r['id'] for r in out['models']], ['b', 'a'])
            self.assertEqual(calculate.call_count, 1)
            scope[0] = 'next-account'
            out = service.get('timezone=UTC&model=a&model=b', 1040)
            self.assertEqual(calculate.call_count, 2)
            self.assertEqual(out['models'][0]['owner'], 'next-account')
            service.get('timezone=America%2FChicago&model=a&model=b', 1040)
            self.assertEqual(calculate.call_count, 3)
            service.get('timezone=UTC&model=a&model=b', 1101)
            self.assertEqual(calculate.call_count, 4)

    def test_scope_change_during_computation_is_not_published(self):
        identity = Mock(side_effect=[('a', 'mac'), ('b', 'mac')])
        service = insights.ModelInsights(None, identity)
        with patch.object(insights, 'report', return_value={'at': 1000, 'models': [{'id': 'a'}]}):
            with self.assertRaises(insights.InsightsUnavailable):
                service.get('timezone=UTC&model=a', 1000)
        self.assertFalse(service.cache)
        self.assertFalse(service.inflight)

    def test_duplicate_slow_read_has_no_shared_control_lock_or_queue(self):
        entered = threading.Event()
        release = threading.Event()
        errors = []
        service = insights.ModelInsights(None, lambda: ('a', 'mac'))

        def slow(*args):
            entered.set()
            release.wait(2)
            return {'at': 1000, 'models': [{'id': 'a'}]}

        def run():
            try:
                service.get('timezone=UTC&model=a', 1000)
            except Exception as error:
                errors.append(error)

        with patch.object(insights, 'report', side_effect=slow):
            thread = threading.Thread(target=run)
            thread.start()
            self.assertTrue(entered.wait(1))
            try:
                self.assertTrue(service.lock.acquire(timeout=0.1))
                service.lock.release()
                with self.assertRaises(insights.InsightsUnavailable):
                    service.get('timezone=UTC&model=a', 1000)
            finally:
                release.set()
                thread.join(2)
        self.assertFalse(errors)


if __name__ == '__main__':
    unittest.main()
