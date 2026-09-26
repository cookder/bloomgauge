"""Honest alignment, completed counts, paid scope and isolated read behavior."""

import json
from pathlib import Path
import random
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

import network_contributions as module

NOW = 1790100000


class ContributionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'history.sqlite3'
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""PRAGMA journal_mode=WAL;
            CREATE TABLE opt_network(at INTEGER,model TEXT,active REAL,queued REAL,warm REAL,routable REAL,PRIMARY KEY(model,at));
            CREATE INDEX opt_network_time ON opt_network(at);
            CREATE TABLE network(at INTEGER PRIMARY KEY,seconds INTEGER,requests INTEGER,prompt_tokens INTEGER,completion_tokens INTEGER);
            CREATE TABLE opt_credits(account TEXT,id INTEGER,provider TEXT,at REAL,model TEXT,micro_usd INTEGER,tokens INTEGER,PRIMARY KEY(account,id));
            CREATE INDEX opt_credit_time ON opt_credits(account,at);
            CREATE TABLE opt_identity(device TEXT,provider TEXT,PRIMARY KEY(device,provider));
            CREATE TABLE opt_coverage(account TEXT,start REAL,end REAL,PRIMARY KEY(account,start));
            INSERT INTO opt_identity VALUES('mac','p'),('mac','p-old'),('foreign-mac','foreign-provider');""")
        self.h = SimpleNamespace(db=self.db, lock=threading.RLock())
        self.credit_id = 0

    def frame(self, at, values):
        self.db.executemany(
            'INSERT INTO opt_network VALUES(?,?,?,?,?,?)',
            [(at, m, v, 0, 1, 1) for m, v in values.items()],
        )

    def usage(self, at, seconds=60, requests=100, output=200, prompt=999999):
        self.db.execute(
            'INSERT INTO network VALUES(?,?,?,?,?)', (at, seconds, requests, prompt, output)
        )

    def credit(self, at, model='a', micro=1000, account='account', provider='p'):
        self.credit_id += 1
        self.db.execute(
            'INSERT INTO opt_credits VALUES(?,?,?,?,?,?,?)',
            (account, self.credit_id, provider, at, model, micro, 999999),
        )

    def coverage(self, start, end, account='account'):
        self.db.execute('INSERT INTO opt_coverage VALUES(?,?,?)', (account, start, end))

    def result(self, metric='activity', start=NOW - 600, end=NOW, account='account', device='mac'):
        self.db.commit()
        return module.report(self.h, metric, start, end, NOW, account, device, time.monotonic() + 5)

    def values(self, result, point=0):
        return dict(zip([s['id'] for s in result['series']], result['points'][point]['values']))

    def test_latest_whole_source_frame_does_not_borrow_missing_model(self):
        start = NOW - 18000  # 60-second chart intervals, two common source slots.
        self.frame(start + 2, {'a': 1, 'b': 100})
        self.frame(start + 20, {'a': 10})  # Whole frame replaces the earlier one.
        self.frame(start + 32, {'a': 30, 'b': 200})
        result = self.result(start=start)
        self.assertEqual(result['bucketSeconds'], 60)
        self.assertEqual(self.values(result), {'model:a': 20, 'model:b': None})
        self.assertEqual(result['points'][0]['coverageFraction'], 1)
        self.assertEqual(result['coverage']['observedSeconds'], 60)
        self.assertIsNone(result['summary']['total'])

    def test_same_timestamp_values_zero_and_invalid_are_distinct(self):
        start = NOW - 60
        self.frame(start + 2, {'zero': 0, 'valid': 2, 'invalid': float('inf')})
        self.frame(start + 32, {'zero': 0, 'valid': 6})
        result = self.result(start=start)
        self.assertEqual(self.values(result)['model:zero'], 0)
        self.assertEqual(self.values(result)['model:valid'], 2)
        self.assertIsNone(self.values(result)['model:invalid'])
        self.assertIsNone(self.values(result, 1)['model:invalid'])
        self.assertEqual(result['status'], 'partial')
        json.dumps(result, allow_nan=False)

    def test_common_period_weight_uses_qualified_intervals_only(self):
        start = NOW - 75000  # Five source slots per 150-second interval.
        for i in range(4):
            self.frame(start + i * 30 + 2, {'a': 2, 'b': 4})
        for i in range(5):
            self.frame(start + 150 + i * 30 + 2, {'a': 10, 'b': 20})
        for i in range(3):
            self.frame(start + 300 + i * 30 + 2, {'a': 1000, 'b': 2000})
        result = self.result(start=start)
        self.assertAlmostEqual(result['summary']['values'][0], (4 * 4 + 20 * 5) / 9)
        self.assertAlmostEqual(result['summary']['values'][1], (2 * 4 + 10 * 5) / 9)
        self.assertAlmostEqual(result['summary']['total'], (6 * 4 + 30 * 5) / 9)
        self.assertEqual(result['points'][2]['values'], [None, None])
        self.assertEqual(result['coverage']['observedSeconds'], 360)

    def test_other_never_turns_missing_models_into_zero(self):
        start = NOW - 18000
        values = {f'm{i:02}': 100 - i for i in range(10)}
        self.frame(start + 2, values)
        self.frame(start + 32, {m: v for m, v in values.items() if m != 'm09'})
        result = self.result(start=start)
        self.assertEqual(len(result['series']), 9)
        self.assertIsNone(self.values(result)['other'])
        self.assertTrue(all(v is not None for k, v in self.values(result).items() if k != 'other'))
        self.assertIsNone(result['summary']['total'])

    def test_partial_first_and_last_source_slots_are_withheld(self):
        start = NOW - 120
        for i in range(4):
            self.frame(start + i * 30 + 15, {'a': i})
        result = self.result(start=start + 10, end=NOW - 10)
        self.assertEqual(result['coverage']['observedSeconds'], 60)
        self.assertIsNone(result['points'][0]['values'][0])
        self.assertIsNone(result['points'][-1]['values'][0])

    def test_completed_requests_and_output_tokens_never_gain_model_allocation(self):
        start = NOW - 120
        self.usage(start, requests=120, output=600)
        self.usage(start + 60, requests=0, output=0)
        self.frame(start + 2, {'popular': 100000})
        for metric, rate, total in [('requests', 120, 120), ('tokens', 10, 600)]:
            result = self.result(metric, start)
            self.assertEqual(result['attribution'], 'unavailable')
            self.assertEqual([s['id'] for s in result['series']], ['unattributed'])
            self.assertEqual(result['points'][0]['values'], [rate])
            self.assertEqual(result['points'][1]['values'], [0])
            self.assertEqual(result['summary']['total'], total)
            self.assertEqual(result['coverage']['fraction'], 1)
            self.assertFalse(result['networkMoney']['available'])

    def test_coarse_source_and_requested_boundaries_are_not_interpolated(self):
        start = NOW - 1800
        self.usage(start, seconds=600, requests=900)
        self.usage(start + 600, seconds=600, requests=1200)
        self.usage(start + 1200, seconds=600, requests=1800)
        result = self.result('requests', start + 100, NOW - 100)
        self.assertEqual(result['bucketSeconds'], 600)
        self.assertEqual(result['summary']['total'], 1200)
        self.assertEqual([p['values'] for p in result['points']], [[None], [120], [None]])
        self.assertEqual(result['coverage']['observedSeconds'], 600)

    def test_recorded_counts_survive_insufficient_chart_coverage(self):
        start = NOW - 36000  # 120-second output step; only half the first bin recorded.
        self.usage(start, requests=0, output=0)
        result = self.result('requests', start)
        self.assertEqual(result['bucketSeconds'], 120)
        self.assertEqual(result['points'][0]['values'], [None])
        self.assertEqual(result['summary']['total'], 0)
        self.assertEqual(result['coverage']['observedSeconds'], 60)
        self.assertEqual(result['status'], 'partial')

    def test_invalid_and_overlapping_usage_is_not_counted_twice(self):
        start = NOW - 600
        self.usage(start, seconds=120, requests=20)
        self.usage(start + 60, requests=999)
        self.usage(start + 120, seconds=0, requests=999)
        self.usage(start + 240, requests=-1)
        result = self.result('requests', start)
        self.assertEqual(result['summary']['total'], 20)
        self.assertEqual(result['coverage']['observedSeconds'], 120)

    def test_earnings_exact_account_device_rollover_base_and_signed_scope(self):
        start = NOW - 120
        self.coverage(start, NOW)
        self.credit(start + 1, micro=10000)
        self.credit(start + 2, micro=-2000)
        self.credit(start + 3, micro=5000, provider='p-old')
        self.credit(start + 4, micro=1000000, provider='foreign-provider')
        self.credit(start + 5, micro=1000000, account='foreign-account')
        self.credit(start + 6, micro=1000000, model='base_reward')
        self.credit(NOW, micro=1000000)
        result = self.result('earnings', start)
        self.assertAlmostEqual(result['summary']['total'], 0.013)
        self.assertAlmostEqual(result['points'][0]['values'][0], 0.78)
        self.assertEqual(result['points'][1]['values'], [0])
        self.assertEqual(result['scope'], 'this_mac')
        text = json.dumps(result)
        self.assertNotIn('foreign-account', text)
        self.assertNotIn('foreign-provider', text)
        self.assertEqual(result['status'], 'ok')

    def test_unknown_model_equal_rank_does_not_break_sort_or_claim_attribution(self):
        start = NOW - 60
        self.coverage(start, NOW)
        self.credit(start + 1, micro=1000)
        self.credit(start + 2, model=None, micro=500)
        self.credit(start + 3, model='Unknown', micro=500)
        result = self.result('earnings', start)
        self.assertEqual([s['id'] for s in result['series']], ['model:a', 'unattributed'])
        self.assertEqual(result['summary']['values'], [0.001, 0.001])
        self.assertEqual(result['summary']['total'], 0.002)

    def test_poll_coverage_union_keeps_gaps_and_elapsed_rate(self):
        start = NOW - 120
        self.coverage(start, start + 25)
        self.coverage(start + 20, start + 50)
        self.coverage(start, NOW, 'foreign-account')
        self.credit(start + 1, micro=1000)
        self.credit(start + 61, micro=2000)
        result = self.result('earnings', start)
        self.assertEqual(result['coverage']['observedSeconds'], 50)
        self.assertAlmostEqual(
            result['points'][0]['values'][0], 0.06
        )  # elapsed minute, not50covered seconds.
        self.assertEqual(result['points'][1]['values'], [None])
        self.assertEqual(result['summary']['total'], 0.003)

    def test_covered_zero_missing_poll_and_missing_mac_mapping_differ(self):
        start = NOW - 60
        empty = self.result('earnings', start)
        self.assertIsNone(empty['summary']['total'])
        self.assertEqual(empty['status'], 'empty')
        self.coverage(start, NOW)
        covered = self.result('earnings', start)
        self.assertEqual(covered['summary']['total'], 0)
        self.assertEqual(covered['points'][0]['values'], [0])
        for scope in [('account', 'new-mac'), ('', 'mac'), ('account', '')]:
            missing = self.result('earnings', start, account=scope[0], device=scope[1])
            self.assertEqual(missing['status'], 'unavailable')
            self.assertIsNone(missing['summary']['total'])

    def test_invalid_credit_remains_unknown_even_when_poll_is_covered(self):
        start = NOW - 60
        self.coverage(start, NOW)
        self.credit(start + 1, micro=None)
        self.credit(start + 2, micro=1000, model='valid')
        result = self.result('earnings', start)
        self.assertAlmostEqual(self.values(result)['model:valid'], 0.06)
        self.assertIsNone(self.values(result)['model:a'])
        self.assertIsNone(result['summary']['total'])
        self.assertEqual(result['status'], 'partial')

    def test_model_count_and_response_size_are_bounded(self):
        start = NOW - 60
        self.coverage(start, NOW)
        for i in range(128):
            self.credit(start + 1, model=f'm{i}', micro=i)
        result = self.result('earnings', start)
        self.assertEqual(len(result['series']), 9)
        self.assertEqual(result['summary']['total'], sum(range(128)) / 1e6)
        self.credit(start + 2, model='overflow')
        with self.assertRaises(module.ContributionsUnavailable):
            self.result('earnings', start)

    def test_query_limits_and_clipping(self):
        good = f'metric=activity&from={NOW - 60}&to={NOW + 60}'
        self.assertEqual(module.query(good, NOW), ('activity', NOW - 60, NOW))
        for raw in (
            '',
            'metric=wat&from=1&to=2',
            'metric=activity&from=NaN&to=2',
            'metric=activity&from=1&to=Infinity',
            'metric=activity&from=2&to=1',
            'metric=activity&from=-1&to=2',
            f'metric=activity&from={NOW}&to={NOW + 1}',
            good + '&account=foreign',
            good + '&metric=tokens',
            good + '&x=1&y=2',
            f'metric=activity&from={NOW - 32 * 86400}&to={NOW}',
            good + 'x' * 2000,
        ):
            with self.subTest(raw=raw[:80]), self.assertRaises(ValueError):
                module.query(raw, NOW)

    def test_layout_stays_under_600_with_utc_edges_and_subseconds(self):
        rng = random.Random(603)
        for _ in range(1000):
            start = rng.random() * NOW
            end = start + rng.uniform(0.000001, 31 * 86400)
            step, low, points = module.layout(start, end, rng.choice([30, 60, 1800, 14400, 43200]))
            self.assertLessEqual(len(points), 600)
            self.assertEqual(points[0]['from'], start)
            self.assertEqual(points[-1]['to'], end)
            self.assertTrue(all(p['to'] > p['from'] for p in points))
            self.assertTrue(
                all(p['from'] == points[i - 1]['to'] for i, p in enumerate(points) if i)
            )
            self.assertEqual(low % step, 0)

    def test_get_caches_copies_and_rejects_scope_change_during_work(self):
        self.db.commit()
        scope = ['account', 'mac']
        reader = module.NetworkContributions(self.h, lambda: scope)
        raw = urlencode({'metric': 'earnings', 'from': NOW - 60, 'to': NOW})
        original = module.report
        with patch.object(module, 'report', wraps=original) as mocked:
            first = reader.get(raw, NOW)
            first['notes'].append('changed')
            second = reader.get(raw, NOW)
            self.assertNotIn('changed', second['notes'])
            self.assertEqual(mocked.call_count, 1)
            scope[:] = ['account', 'new-mac']
            self.assertEqual(reader.get(raw, NOW)['status'], 'unavailable')
            self.assertEqual(mocked.call_count, 2)
        reader.cache.clear()
        scope[:] = ['account', 'mac']

        def changing(*args, **kwargs):
            result = original(*args, **kwargs)
            scope[:] = ['foreign-account', 'mac']
            return result

        with (
            patch.object(module, 'report', side_effect=changing),
            self.assertRaises(module.ContributionsUnavailable),
        ):
            reader.get(raw, NOW)
        self.assertFalse(reader.cache)
        self.assertFalse(reader.inflight)

    def test_singleflight_does_not_hold_the_reader_lock_and_bounds_parallel_reports(self):
        self.db.commit()
        entered = threading.Event()
        release = threading.Event()
        errors = []
        reader = module.NetworkContributions(self.h, lambda: ('account', 'mac'))
        raw = urlencode({'metric': 'activity', 'from': NOW - 60, 'to': NOW})
        original = module.report

        def slow(*args, **kwargs):
            entered.set()
            release.wait(2)
            return original(*args, **kwargs)

        def work():
            try:
                reader.get(raw, NOW)
            except Exception as error:
                errors.append(error)

        with patch.object(module, 'report', side_effect=slow):
            worker = threading.Thread(target=work)
            worker.start()
            self.assertTrue(entered.wait(1))
            try:
                began = time.monotonic()
                with self.assertRaises(module.ContributionsUnavailable):
                    reader.get(raw, NOW)
                self.assertLess(time.monotonic() - began, 0.5)
                with reader.lock:
                    reader.inflight.add(('synthetic-other',))
                with self.assertRaises(module.ContributionsUnavailable):
                    reader.get(raw.replace('activity', 'requests'), NOW)
            finally:
                release.set()
                worker.join(3)
        self.assertFalse(errors)

    def test_cache_age_rollback_and_entry_count_are_bounded(self):
        self.db.commit()
        reader = module.NetworkContributions(self.h, lambda: ('account', 'mac'))
        raw = urlencode({'metric': 'activity', 'from': NOW - 60, 'to': NOW - 1})
        original = module.report
        with patch.object(module, 'report', wraps=original) as mocked:
            reader.get(raw, NOW)
            reader.get(raw, NOW - 1)
            self.assertEqual(mocked.call_count, 2)  # A backward clock cannot reuse a future report.
            cache_time = next(iter(reader.cache.values()))[0]
            with patch.object(module.time, 'monotonic', return_value=cache_time + 31):
                reader.get(raw, NOW)
            self.assertEqual(mocked.call_count, 3)
        for i in range(10):
            reader.get(urlencode({'metric': 'activity', 'from': NOW - 120 - i, 'to': NOW - 1}), NOW)
        self.assertEqual(len(reader.cache), 8)

    def test_read_snapshot_is_query_only_and_does_not_block_a_wal_writer(self):
        start = NOW - 60
        self.frame(start + 2, {'a': 1})
        self.db.commit()
        entered = threading.Event()
        release = threading.Event()
        results = []
        failures = []
        original = module.activity

        def paused(db, *args):
            self.assertEqual(db.execute('PRAGMA query_only').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT active FROM opt_network').fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                db.execute('DELETE FROM opt_network')
            entered.set()
            release.wait(2)
            return original(db, *args)

        def work():
            try:
                results.append(
                    module.report(
                        self.h, 'activity', start, NOW, NOW, deadline=time.monotonic() + 5
                    )
                )
            except Exception as error:
                failures.append(error)

        with patch.object(module, 'activity', side_effect=paused):
            worker = threading.Thread(target=work)
            worker.start()
            self.assertTrue(entered.wait(1))
            try:
                began = time.monotonic()
                with self.h.lock:
                    self.db.execute('UPDATE opt_network SET active=7')
                    self.db.commit()
                self.assertLess(time.monotonic() - began, 0.5)
            finally:
                release.set()
                worker.join(3)
        self.assertFalse(failures)
        self.assertEqual(results[0]['points'][0]['values'], [1])
        self.assertEqual(self.result(start=start)['points'][0]['values'], [7])

    def test_sql_deadline_failure_is_bounded_and_cleanup_allows_next_report(self):
        self.db.commit()
        reader = module.NetworkContributions(self.h, lambda: ('account', 'mac'), timeout=0.01)
        raw = urlencode({'metric': 'activity', 'from': NOW - 60, 'to': NOW})

        def expensive(db, *args):
            db.execute(
                'WITH RECURSIVE c(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM c WHERE x<100000000) SELECT sum(x) FROM c'
            ).fetchone()

        began = time.monotonic()
        with (
            patch.object(module, 'activity', side_effect=expensive),
            self.assertRaises(module.ContributionsUnavailable),
        ):
            reader.get(raw, NOW)
        self.assertLess(time.monotonic() - began, 1)
        self.assertFalse(reader.inflight)
        self.assertEqual(reader.get(raw, NOW)['status'], 'empty')

    def test_reports_never_write_fetch_or_run_provider_commands(self):
        start = NOW - 60
        self.coverage(start, NOW)
        self.db.commit()
        before = self.db.total_changes
        with (
            patch('subprocess.run', side_effect=AssertionError('No CLI')),
            patch('urllib.request.urlopen', side_effect=AssertionError('No network')),
        ):
            for metric in module.METRICS:
                result = self.result(metric, start)
                self.assertLessEqual(len(result['points']), 600)
                json.dumps(result, allow_nan=False)
        self.assertEqual(self.db.total_changes, before)


if __name__ == '__main__':
    unittest.main()
