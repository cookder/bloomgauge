import unittest
from unittest.mock import patch
from history import History
from optimizer_store import OptimizerStore
from earnings_target import report
from decision_journal import DecisionJournal
import test_remote
import json

NOW = 1800000000  # hour aligned; complete windows end before settlement lag


class TargetReportTests(unittest.TestCase):
    def setUp(self):
        self.h = History(':memory:')
        self.s = OptimizerStore(self.h)
        self.s.identity('mac', 'provider')
        self.start = NOW - 86400
        self.end = NOW
        self.h.db.execute('INSERT INTO opt_coverage VALUES(?,?,?)', ('owner', self.start, NOW))
        self.h.db.commit()

    def tearDown(self):
        self.h.close()

    def credit(self, at, amount=120000, model='a', provider='provider', account='owner'):
        n = self.h.db.execute('SELECT COUNT(*) FROM opt_credits').fetchone()[0] + 1
        self.h.db.execute(
            'INSERT INTO opt_credits VALUES(?,?,?,?,?,?,?)',
            (account, n, provider, at, model, amount, 20),
        )
        self.h.db.commit()

    def get(self, **kwargs):
        kwargs.setdefault('target', 0.12)
        return report(
            self.s,
            kwargs.pop('account', 'owner'),
            kwargs.pop('device', 'mac'),
            kwargs.pop('start', self.start),
            kwargs.pop('end', self.end),
            kwargs.pop('now', NOW + 120),
            **kwargs,
        )

    def test_clock_hours_include_quiet_cold_and_switch_time_without_warm_normalization(self):
        self.credit(self.start + 1, 240000)
        self.h.db.execute(
            'INSERT INTO opt_events(account,device,at,kind,model,detail,downtime) VALUES(?,?,?,?,?,?,?)',
            ('owner', 'mac', self.start + 1800, 'switched', 'a', 'loaded', 60),
        )
        self.h.db.commit()
        r = self.get()
        self.assertEqual(r['usd'], 0.24)
        self.assertAlmostEqual(r['clockUsdPerHour'], 0.01)
        self.assertEqual(r['completeHours'], 24)
        self.assertEqual(r['metHours'], 1)
        self.assertEqual(r['longestBelowHours'], 23)
        self.assertEqual(r['switchSeconds'], 60)
        self.assertEqual(r['switchCount'], 1)

    def test_scope_and_model_filters_keep_account_base_separate(self):
        self.credit(self.start + 1, 50000)
        self.credit(self.start + 2, 20000, model='b')
        self.credit(self.start + 3, 60000, model='base_reward', provider='')
        self.credit(self.start + 4, 999999, account='other')
        self.credit(self.start + 5, 999999, provider='another-mac')
        r = self.get()
        self.assertAlmostEqual(r['usd'], 0.13)
        self.assertEqual(r['metHours'], 1)
        self.assertEqual(r['models'], ['a', 'b'])
        self.assertEqual(self.get(model='@inference')['usd'], 0.07)
        r = self.get(model='a')
        self.assertEqual(r['usd'], 0.05)
        self.assertEqual(r['accountBaseUsd'], 0.06)
        self.assertFalse(r['includesBase'])
        self.assertEqual(self.get(account='other', device='other')['usd'], 0)

    def test_partial_unsettled_and_missing_hours_are_unknown_not_zero(self):
        self.h.db.execute('DELETE FROM opt_coverage')
        self.h.db.execute(
            'INSERT INTO opt_coverage VALUES(?,?,?)', ('owner', self.start + 7200, NOW)
        )
        self.h.db.commit()
        r = self.get()
        self.assertEqual(r['completeHours'], 22)
        self.assertEqual(r['unknownHours'], 2)
        self.assertFalse(r['completeCoverage'])
        self.assertIsNone(r['clockUsdPerHour'])
        self.assertEqual(r['hourly'][0]['status'], 'unknown')
        r = self.get(start=self.start + 7300, now=NOW)
        self.assertEqual(r['hourly'][0]['status'], 'partial')
        self.assertEqual(r['hourly'][-1]['status'], 'settling')
        self.assertEqual(r['completeHours'], 20)

    def test_coverage_intervals_union_without_counting_overlaps_twice(self):
        self.h.db.execute('INSERT INTO opt_coverage VALUES(?,?,?)', ('owner', self.start + 1, NOW))
        self.h.db.commit()
        r = self.get()
        self.assertEqual(r['coveredSeconds'], 86400)
        self.assertEqual(r['completeHours'], 24)

    def test_live_poll_tail_does_not_hide_settled_clock_rate(self):
        self.h.db.execute('UPDATE opt_coverage SET end=?', (NOW - 2,))
        self.h.db.commit()
        self.credit(self.start + 1, 240000)
        self.credit(NOW - 60, 100000)
        r = self.get(now=NOW)
        self.assertFalse(r['completeCoverage'])
        self.assertAlmostEqual(r['clockUsdPerHour'], 0.24 * 3600 / (86400 - 120))
        self.assertAlmostEqual(r['usd'], 0.34)
        self.assertEqual(r['settledTo'], NOW - 120)

    def test_unknown_hour_breaks_observed_shortfall_streak(self):
        self.h.db.execute('DELETE FROM opt_coverage')
        self.h.db.executemany(
            'INSERT INTO opt_coverage VALUES(?,?,?)',
            [('owner', self.start, self.start + 3600 * 10), ('owner', self.start + 3600 * 11, NOW)],
        )
        self.h.db.commit()
        r = self.get()
        self.assertEqual(r['longestBelowHours'], 13)

    def test_long_range_retains_exact_totals_and_bounds_map(self):
        start = NOW - 90 * 86400
        self.h.db.execute('INSERT INTO opt_coverage VALUES(?,?,?)', ('owner', start, NOW))
        self.h.db.commit()
        self.credit(start + 1, 500000)
        r = self.get(start=0)
        self.assertTrue(r['chartTruncated'])
        self.assertLessEqual(len(r['hourly']), 745)
        self.assertEqual(r['completeHours'], 90 * 24)
        self.assertEqual(r['usd'], 0.5)

    def test_no_goal_reports_hours_without_judging_them(self):
        self.credit(self.start + 1, 240000)
        r = self.get(target=None)
        self.assertEqual((r['usd'], r['completeHours']), (0.24, 24))
        goal_fields = ('targetUsdPerHour', 'dailyTargetUsd', 'metHours', 'metPercent')
        for key in (*goal_fields, 'longestBelowHours'):
            self.assertIsNone(r[key], key)
        self.assertEqual({h['status'] for h in r['hourly']}, {'complete'})
        # The user's goal, not a built-in $0.12, decides met and below.
        r = self.get(target=0.2)
        self.assertEqual((r['metHours'], r['dailyTargetUsd']), (1, 0.2 * 24))
        self.assertEqual(self.get(target=0.25)['metHours'], 0)

    def test_account_switch_and_no_history_leave_empty_result(self):
        r = self.get(account='unknown')
        self.assertEqual(r['completeHours'], 0)
        self.assertIsNone(r['metPercent'])
        self.assertEqual(r['usd'], 0)

    def test_invalid_inputs_are_rejected_and_read_has_no_writes(self):
        before = self.h.db.total_changes
        self.get()
        self.assertEqual(self.h.db.total_changes, before)
        for values in (
            {'start': -1},
            {'end': self.start},
            {'end': float('nan')},
            {'target': 0},
            {'target': float('nan')},
            {'model': ''},
            {'model': True},
        ):
            with self.assertRaises(ValueError):
                self.get(**values)


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.h = History(':memory:')
        self.j = DecisionJournal(self.h)

    def tearDown(self):
        self.h.close()

    def record(self, at, reason='Waiting for memory.', target='b', phase='waiting'):
        self.j.record(
            'owner',
            'mac',
            at,
            'demand',
            'a',
            target,
            phase,
            reason,
            {'sourceAt': at - 5},
            {'seconds': 60, 'requiredSeconds': 300},
        )

    def test_repeated_reads_and_duplicate_ticks_never_advance_elapsed_time(self):
        self.record(100)
        self.record(115)
        self.record(115)
        r = self.j.snapshot('owner', 'mac', 120)
        self.assertEqual(r['current']['observedSeconds'], 15)
        before = self.h.db.total_changes
        for _ in range(5):
            self.assertEqual(self.j.snapshot('owner', 'mac', 120), r)
        self.assertEqual(self.h.db.total_changes, before)

    def test_reason_and_target_transitions_are_preserved(self):
        self.record(100)
        self.record(115)
        self.record(130, 'Confirming opportunity.')
        self.record(145, 'Confirming opportunity.', 'c')
        r = self.j.snapshot('owner', 'mac', 150)
        self.assertEqual(len(r['history']), 3)
        self.assertEqual(r['history'][0]['target'], 'c')
        self.assertEqual(r['history'][1]['code'], 'confirmation')
        self.assertEqual(r['totals'][0], {'code': 'memory', 'seconds': 15})

    def test_restart_gap_does_not_manufacture_observed_hold_minutes(self):
        self.record(100)
        self.record(115)
        self.record(900)
        r = self.j.snapshot('owner', 'mac', 1000)
        self.assertFalse(r['fresh'])
        self.assertEqual(len(r['history']), 2)
        self.assertEqual(r['totals'][0]['seconds'], 15)
        self.assertIsNone(self.j.snapshot('other', 'mac', 1000)['current'])

    def test_future_record_is_not_published_and_reversed_time_is_ignored(self):
        self.record(100)
        self.record(90)
        self.assertIsNone(self.j.snapshot('owner', 'mac', 99)['current'])


class TargetHTTPTests(test_remote.RemoteHTTPBase):
    def test_target_uses_current_identity_and_existing_phone_owner_check(self):
        c = self.local.RequestHandlerClass.collector
        c.account = 'current'
        c.optimizer.live = {'account': 'current', 'device': 'this-mac'}
        c.optimizer.state = {'demandPolicy': {}}
        with patch('earnings_target.report', return_value={'scope': 'fixture'}) as build:
            with self.read(self.local, '/api/earnings-target') as response:
                json.load(response)
            # The policy's placeholder $0.12 is not a goal; the user's choice is.
            self.assertIsNone(build.call_args.args[6])
            c.optimizer.state = {'demandPolicy': {'targetUsdPerHour': 0.2}}
            with self.read(self.local, '/api/earnings-target') as response:
                json.load(response)
            self.assertEqual(build.call_args.args[6], 0.2)
            for server, headers in ((self.local, {}), (self.phone, self.headers)):
                with self.read(
                    server, '/api/earnings-target?account=foreign&device=foreign', headers
                ) as response:
                    self.assertEqual(json.load(response)['scope'], 'fixture')
                self.assertEqual(build.call_args.args[1:3], ('current', 'this-mac'))
            c.optimizer.live = {'account': 'old', 'device': 'this-mac'}
            with self.read(self.local, '/api/earnings-target') as response:
                json.load(response)
            self.assertEqual(build.call_args.args[1:3], ('', ''))
        self.denied(self.phone, '/api/earnings-target')


if __name__ == '__main__':
    unittest.main()
