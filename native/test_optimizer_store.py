import pathlib
import re
import tempfile
import unittest
from unittest import mock
from history import History
from demand_baselines import read_view
from optimizer_store import OptimizerStore


class EvidenceQueryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.h = History(pathlib.Path(self.tmp.name) / 'history.sqlite3')
        self.addCleanup(self.h.close)
        self.store = OptimizerStore(self.h)
        self.now = 1_790_000_040  # minute-aligned
        # A long-lived Mac collects a new provider id per daemon install.
        for i in range(250):
            self.store.identity('mac', f'provider-{i}')
        self.store.identity('other-mac', 'foreign')
        start = self.now - 3 * 86400
        self.h.db.execute('INSERT INTO opt_coverage VALUES(?,?,?)', ('account', start, self.now))
        credits, minutes = [], []
        for i in range(3 * 1440 - 3):
            at = start + i * 60
            minutes.append(('account', 'mac', at, 'a', 60, 1, 100, 10))
            credits.append(('account', 2 * i, f'provider-{i % 250}', at + 5, 'a', 1000, 10))
            credits.append(('account', 2 * i + 1, 'foreign', at + 6, 'a', 1_000_000, 10))
        self.h.db.executemany('INSERT INTO opt_ready_minutes VALUES(?,?,?,?,?,?,?,?)', minutes)
        self.h.db.executemany('INSERT INTO opt_credits VALUES(?,?,?,?,?,?,?)', credits)
        # Planner statistics taken while both tables were small (as on a real
        # install): the JOIN form then loops every provider over the whole window.
        self.h.db.execute('ANALYZE')
        self.h.db.execute("UPDATE sqlite_stat1 SET stat='100 100 1' WHERE tbl='opt_credits'")
        self.h.db.execute("UPDATE sqlite_stat1 SET stat='1 1 1' WHERE tbl='opt_identity'")
        self.h.db.commit()
        self.h.db.execute('ANALYZE sqlite_schema')

    def test_credit_scope_probes_identity_instead_of_rescanning_credits_per_provider(self):
        statements = []
        self.h.db.set_trace_callback(statements.append)
        try:
            result = self.store.evidence(
                'account', 'mac', self.now - 3 * 86400, self.now, self.now
            )['a']
        finally:
            self.h.db.set_trace_callback(None)
        # Only this Mac's providers count; the foreign provider's large credits never do.
        self.assertEqual(result['jobs'], 3 * 1440 - 3)
        self.assertAlmostEqual(result['usd'], result['jobs'] / 1000)
        credit_queries = [
            s for s in statements if 'opt_credits' in s and s.lstrip().upper().startswith('SELECT')
        ]
        self.assertTrue(credit_queries)
        for sql in credit_queries:
            plan = [row[3] for row in self.h.db.execute('EXPLAIN QUERY PLAN ' + sql)]
            # Credits drive the query; identity is never rescanned per credit or per provider.
            self.assertTrue(plan[0].startswith('SEARCH c USING INDEX opt_credit_time'), plan)
            self.assertFalse([p for p in plan if p.startswith(('SCAN i', 'SEARCH i'))], plan)

    def test_statistics_refresh_replaces_counts_taken_while_tables_were_small(self):
        self.assert_statistics_refresh()

    def test_statistics_refresh_on_sqlite_without_optimize_all_tables(self):
        with mock.patch('optimizer_store.sqlite3.sqlite_version_info', (3, 43, 2)):
            self.assert_statistics_refresh()

    def assert_statistics_refresh(self):
        stat = "SELECT stat FROM sqlite_stat1 WHERE tbl=? AND idx LIKE 'sqlite_autoindex_%'"
        self.assertEqual(self.h.db.execute(stat, ('opt_identity',)).fetchone()[0], '1 1 1')
        self.store.refresh_statistics()
        self.assertEqual(
            int(self.h.db.execute(stat, ('opt_identity',)).fetchone()[0].split()[0]), 251
        )
        # With analysis_limit, SQLite before 3.46 estimates large tables instead of counting.
        credits = 2 * (3 * 1440 - 3)
        self.assertAlmostEqual(
            int(self.h.db.execute(stat, ('opt_credits',)).fetchone()[0].split()[0]),
            credits,
            delta=credits // 10,
        )
        # Nothing changed tenfold since, so a second refresh leaves the statistics alone:
        # a marker count that re-analysis would overwrite must survive.
        self.h.db.execute("UPDATE sqlite_stat1 SET stat='250 1 1' WHERE tbl='opt_identity'")
        self.store.refresh_statistics()
        self.assertEqual(self.h.db.execute(stat, ('opt_identity',)).fetchone()[0], '250 1 1')


class PaidMinutesTests(unittest.TestCase):
    FULL = """SELECT CAST(c.at/60 AS INT)*60,c.model,SUM(c.micro_usd)/1000000.0,COUNT(*)
        FROM opt_credits c WHERE c.account=? AND c.at>=? AND c.at<? AND c.model!='base_reward'
        AND c.provider IN (SELECT provider FROM opt_identity WHERE device=?)
        GROUP BY CAST(c.at/60 AS INT),c.model"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.h = History(pathlib.Path(self.tmp.name) / 'history.sqlite3')
        self.addCleanup(self.h.close)
        self.store = OptimizerStore(self.h)
        self.now = 1_790_000_000.25
        for provider in ('p1', 'p2'):
            self.store.identity('mac', provider)
        self.store.identity('other-mac', 'foreign')
        self.next_id = 0
        # Credits on, just before and inside minute boundaries, for this Mac, another Mac,
        # base rewards and a second model, across three days.
        start = self.now - 3 * 86400
        self.add([(start + i * 17 + offset, model, provider)
                  for i in range(3 * 86400 // 17)
                  for offset, model, provider in ((0, 'a', 'p1'), (59.9999999, 'b', 'p2'))])
        self.add([(start + i * 600, 'base_reward', 'p1') for i in range(400)])
        self.add([(start + i * 60, 'a', 'foreign') for i in range(1000)])

    def add(self, credits):
        rows = []
        for at, model, provider in credits:
            self.next_id += 1
            rows.append(('acct', self.next_id, provider, at, model, 1000 + self.next_id % 7, 10))
        self.h.db.executemany('INSERT INTO opt_credits VALUES(?,?,?,?,?,?,?)', rows)
        self.h.db.commit()

    def full(self, db, start, end):
        return {(r[0], r[1]): {'usd': r[2], 'paidJobs': r[3]}
                for r in db.execute(self.FULL, ('acct', start, end, 'mac'))}

    def check(self, start=None, end=None):
        start = self.now - 2 * 86400 - 0.5 if start is None else start
        end = self.now - 120 if end is None else end
        got = self.store.paid_minutes.get(self.h.db, 'acct', 'mac', start, end)
        self.assertEqual(got, self.full(self.h.db, start, end))
        with read_view(self.store) as view:
            self.assertEqual(view.paid_minutes.get(view.h.db, 'acct', 'mac', start, end),
                             self.full(view.h.db, start, end))

    def test_matches_the_full_query_through_every_kind_of_write(self):
        self.check()
        # A day-old credit: well inside the window, long since kept in memory.
        old = self.h.db.execute('SELECT * FROM opt_credits WHERE at>? AND model=? ORDER BY at LIMIT 1',
                                (self.now - 86400, 'a')).fetchone()
        writes = [
            # A poll repeats a credit, corrected and moved (REPLACE fires no delete trigger).
            ('INSERT OR REPLACE INTO opt_credits VALUES(?,?,?,?,?,?,?)',
             (*tuple(old)[:3], old['at'] + 300, old['model'], 77, 10)),
            ('UPDATE opt_credits SET at=at+61.5 WHERE id=?', (old['id'] + 2,)),
            ('UPDATE opt_credits SET micro_usd=-500 WHERE id=?', (old['id'] + 4,)),
            ('DELETE FROM opt_credits WHERE id=?', (old['id'] + 6,)),
            ('INSERT INTO opt_identity VALUES(?,?)', ('mac', 'foreign')),
            ('DELETE FROM opt_identity WHERE device=? AND provider=?', ('mac', 'p2')),
        ]
        for sql, args in writes:
            with self.subTest(sql=sql):
                self.h.db.execute(sql, args)
                self.h.db.commit()
                self.check()
        self.now += 3600
        self.add([(self.now - 1800 + i, 'a', 'p1') for i in range(0, 1800, 7)])
        self.check()
        self.check(self.now - 7 * 86400, self.now - 86400 + 0.75)  # partly before any credit
        self.check(self.now - 40 * 86400, self.now)  # longer than the cache keeps

    def test_later_calls_read_only_new_and_changed_minutes(self):
        self.check()
        old = self.h.db.execute('SELECT id FROM opt_credits WHERE at>? ORDER BY at LIMIT 1',
                                (self.now - 86400,)).fetchone()[0]
        self.h.db.execute('UPDATE opt_credits SET micro_usd=5 WHERE id=?', (old,))
        self.h.db.commit()
        self.now += 600
        statements = []
        self.h.db.set_trace_callback(statements.append)
        try:
            self.store.paid_minutes.get(self.h.db, 'acct', 'mac', self.now - 2 * 86400, self.now - 120)
        finally:
            self.h.db.set_trace_callback(None)
        spans = [float(b) - float(a) for s in statements
                 for a, b in re.findall(r'c\.at>=([-\d.e+]+) AND c\.at<([-\d.e+]+)', s)]
        # The changed minute, ten new minutes and the window's edges; never the two days.
        self.assertTrue(spans)
        self.assertLess(max(spans), 900, spans)
        self.check()


if __name__ == '__main__':
    unittest.main()
