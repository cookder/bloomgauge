"""Backend logging: repeats suppressed per handler, warnings kept for diagnostics."""

import logging
import tempfile
import unittest
from pathlib import Path

import bloom_log


class BloomLogTests(unittest.TestCase):
    def test_file_log_recent_warnings_and_repeat_suppression(self):
        root = logging.getLogger('bloom')
        root.handlers.clear()
        root._bloom_configured = False
        bloom_log.recent.clear()
        with tempfile.TemporaryDirectory() as tmp:
            bloom_log.setup(Path(tmp) / 'history.sqlite3')
            log = logging.getLogger('bloom.test')
            for _ in range(5):
                try:
                    raise ValueError('boom')
                except ValueError:
                    log.exception('Loop failed')
            log.warning('Something else')
            for h in root.handlers:
                h.flush()
            text = (Path(tmp) / 'logs' / 'bloom.log').read_text()
            self.assertEqual(text.count('Loop failed'), 1)
            self.assertIn('Something else', text)
            self.assertEqual(
                [w['message'] for w in bloom_log.recent_warnings()],
                ['Loop failed', 'Something else'],
            )
            self.assertEqual(bloom_log.recent_warnings()[0]['error'], 'ValueError')
            for h in list(root.handlers):
                h.close()
                root.removeHandler(h)
            root._bloom_configured = False


if __name__ == '__main__':
    unittest.main()
