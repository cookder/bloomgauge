import pathlib
import tempfile
import unittest
from unittest import mock
from history import History
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


if __name__ == '__main__':
    unittest.main()
