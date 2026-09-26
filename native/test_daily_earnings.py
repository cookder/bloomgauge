import json
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import patch
from history import History
from optimizer_store import OptimizerStore
from daily_earnings import report
import test_remote


class DailyTests(unittest.TestCase):
    def setUp(self):
        self.h = History(':memory:')
        self.s = OptimizerStore(self.h)
        self.s.identity('mac', 'provider')
        self.zone = 'America/Chicago'
        self.start = self.at('2026-09-01')
        self.end = self.at('2026-09-08')
        self.h.db.execute('INSERT INTO opt_coverage VALUES(?,?,?)', ('owner', self.start, self.end))
        self.h.db.commit()

    def tearDown(self):
        self.h.close()

    def at(self, date, zone=None):
        return datetime.fromisoformat(date).replace(tzinfo=ZoneInfo(zone or self.zone)).timestamp()

    def credit(self, at, amount, model='a', provider='provider', account='owner'):
        n = self.h.db.execute('SELECT COUNT(*) FROM opt_credits').fetchone()[0] + 1
        self.h.db.execute(
            'INSERT INTO opt_credits VALUES(?,?,?,?,?,?,?)',
            (account, n, provider, at, model, amount, 20),
        )
        self.h.db.commit()

    def get(self, **kw):
        return report(
            self.s,
            kw.pop('account', 'owner'),
            kw.pop('device', 'mac'),
            kw.pop('start', self.start),
            kw.pop('end', self.end),
            kw.pop('now', self.end + 180),
            kw.pop('timezone', self.zone),
            **kw,
        )

    def test_exact_calendar_boundaries_and_signed_credits(self):
        boundary = self.at('2026-09-02')
        self.credit(boundary - 1, 3000000)
        self.credit(boundary, 1000000)
        self.credit(boundary + 1, -200000)
        r = self.get()
        self.assertEqual(r['usd'], 3.8)
        self.assertEqual([d['usd'] for d in r['days'][:2]], [3, 0.8])
        self.assertEqual(r['completeDays'], 7)
        self.assertAlmostEqual(r['averageDayUsd'], 3.8 / 7)

    def test_scope_filters_base_and_foreign_provider(self):
        self.credit(self.start + 1, 100000)
        self.credit(self.start + 2, 200000, model='b')
        self.credit(self.start + 3, 50000, model='base_reward', provider='')
        self.credit(self.start + 4, 9999999, provider='foreign')
        self.credit(self.start + 5, 9999999, account='foreign')
        self.assertEqual(self.get()['usd'], 0.35)
        self.assertEqual(self.get(model='a')['usd'], 0.1)
        self.assertEqual(self.get(model='@inference')['usd'], 0.3)
        self.assertEqual(self.get(model='a')['baseUsd'], 0.05)
        self.assertEqual(self.get()['models'], ['a', 'b'])
        self.assertTrue(all(d['status'] == 'unknown' for d in self.get(account='absent')['days']))

    def test_missing_coverage_is_unknown_today_not_compared_with_full_days(self):
        self.h.db.execute('DELETE FROM opt_coverage')
        self.h.db.execute(
            'INSERT INTO opt_coverage VALUES(?,?,?)', ('owner', self.start + 86400, self.end - 30)
        )
        self.h.db.commit()
        r = self.get(now=self.end)
        self.assertEqual(r['days'][0]['status'], 'unknown')
        self.assertEqual(r['days'][-1]['status'], 'settling')
        self.assertEqual(r['completeDays'], 5)
        r = self.get(end=self.end - 3600, now=self.end - 3600)
        self.assertEqual(r['days'][-1]['status'], 'today')
        self.assertNotEqual(r['days'][-1]['status'], 'complete')

    def test_custom_boundary_not_extrapolated_or_counted_in_average(self):
        self.credit(self.start + 60, 500000)
        self.credit(self.start + 7200, 1000000)
        r = self.get(start=self.start + 3600, end=self.start + 9000)
        self.assertEqual(r['usd'], 1)
        self.assertEqual(r['days'][0]['status'], 'partial')
        self.assertIsNone(r['averageDayUsd'])

    def test_daylight_saving_days_are_23_and_25_hours(self):
        for date, seconds in [('2026-03-08', 23 * 3600), ('2026-11-01', 25 * 3600)]:
            start = self.at(date)
            end = (
                datetime.fromtimestamp(start, ZoneInfo(self.zone)) + timedelta(days=1)
            ).timestamp()
            self.h.db.execute('INSERT INTO opt_coverage VALUES(?,?,?)', ('owner', start, end))
            self.h.db.commit()
            self.credit(end - 1, 100000)
            self.credit(end, 900000)
            r = self.get(start=start, end=end, now=end + 180)
            self.assertEqual(len(r['days']), 1)
            self.assertEqual(r['usd'], 0.1)
            self.assertEqual(r['days'][0]['end'] - r['days'][0]['at'], seconds)

    def test_half_hour_timezone_and_overlap_coverage(self):
        self.zone = 'Asia/Kolkata'
        start = self.at('2026-09-02')
        end = self.at('2026-09-03')
        self.h.db.execute('INSERT INTO opt_coverage VALUES(?,?,?)', ('owner', start, end))
        self.h.db.commit()
        self.credit(start - 1, 400000)
        self.credit(start, 100000)
        self.credit(end - 1, 200000)
        r = self.get(start=start, end=end)
        self.assertEqual(r['usd'], 0.3)
        self.assertEqual(r['days'][0]['status'], 'complete')

    def test_long_range_bounds_calendar_but_preserves_total_and_old_custom(self):
        start = self.start - 500 * 86400
        self.h.db.execute('INSERT INTO opt_coverage VALUES(?,?,?)', ('owner', start, self.end))
        self.h.db.commit()
        self.credit(start + 1, 4000000)
        r = self.get(start=0)
        self.assertTrue(r['chartTruncated'])
        self.assertEqual(len(r['days']), 366)
        self.assertEqual(r['usd'], 4)
        self.assertEqual(self.get(start=start, end=start + 86400)['days'][0]['usd'], 4)

    def test_read_only_and_rejects_invalid_inputs(self):
        before = self.h.db.total_changes
        self.get()
        self.assertEqual(before, self.h.db.total_changes)
        for kw in [
            {'start': -1},
            {'end': float('nan')},
            {'end': self.start},
            {'timezone': 'invalid/zone'},
            {'timezone': '/etc/passwd'},
            {'model': True},
            {'model': ''},
            {'start': float('inf')},
        ]:
            with self.assertRaises(ValueError):
                self.get(**kw)


class DailyHTTPTests(test_remote.RemoteHTTPBase):
    def test_current_identity_and_private_phone_guards(self):
        c = self.local.RequestHandlerClass.collector
        c.account = 'current'
        c.optimizer.live = {'account': 'current', 'device': 'this-mac'}
        with patch('daily_earnings.report', return_value={'scope': 'fixture'}) as build:
            for server, headers in ((self.local, {}), (self.phone, self.headers)):
                with self.read(
                    server,
                    '/api/earnings-daily?account=foreign&device=foreign&timezone=UTC',
                    headers,
                ) as response:
                    self.assertEqual(json.load(response)['scope'], 'fixture')
                self.assertEqual(build.call_args.args[1:3], ('current', 'this-mac'))
            c.optimizer.live = {'account': 'previous', 'device': 'another-mac'}
            with self.read(self.local, '/api/earnings-daily') as response:
                response.read()
            self.assertEqual(build.call_args.args[1:3], ('', ''))
        with self.assertRaises(Exception):
            self.read(self.phone, '/api/earnings-daily', {})


if __name__ == '__main__':
    unittest.main()
