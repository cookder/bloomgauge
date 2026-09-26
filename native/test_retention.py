"""90-day retention trims only fine-grained tables and keeps optimizer evidence."""

import tempfile
import unittest
from pathlib import Path

import retention
from history import History


class RetentionTests(unittest.TestCase):
    def test_old_fine_grained_rows_go_and_evidence_stays(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = History(Path(tmp) / 'h.sqlite3')
            now = 200 * 86400
            with h.lock:
                h.db.execute(
                    'CREATE TABLE IF NOT EXISTS pulse_rates(account TEXT,session INTEGER,at INTEGER,rate60 REAL,rate300 REAL,PRIMARY KEY(account,session,at))'
                )
                h.db.execute('CREATE TABLE IF NOT EXISTS opt_credits(at REAL, model TEXT)')
                h.db.executemany(
                    'INSERT INTO pulse_rates VALUES(?,?,?,?,?)',
                    [('a', 1, t, 1, 1) for t in range(0, 200 * 86400, 3600)],
                )
                h.db.executemany(
                    'INSERT OR REPLACE INTO samples(at,tokens) VALUES(?,?)',
                    [(t, 1) for t in range(0, 200 * 86400, 3600)],
                )
                h.db.executemany(
                    'INSERT INTO opt_credits VALUES(?,?)',
                    [(t, 'm') for t in range(0, 200 * 86400, 86400)],
                )
                h.db.commit()
            old = retention.BATCH
            retention.BATCH = 100  # exercise several batches
            try:
                deleted = retention.prune(h, now, pause=0)
            finally:
                retention.BATCH = old
            self.assertEqual(deleted['pulse_rates'], 110 * 24)
            self.assertEqual(deleted['samples'], 110 * 24)
            with h.lock:
                self.assertEqual(
                    h.db.execute('SELECT MIN(at) FROM pulse_rates').fetchone()[0], 110 * 86400
                )
                self.assertEqual(
                    h.db.execute('SELECT COUNT(*) FROM opt_credits').fetchone()[0], 200
                )
            self.assertEqual(retention.prune(h, now, pause=0), {})
            h.close()


if __name__ == '__main__':
    unittest.main()
