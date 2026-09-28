"""Temporary history only; no provider or network traffic."""

import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest

from history import History
from whats_changed import KEY, SEEN_LIMIT, WhatsChanged
import test_remote


class WhatsChangedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.history = History(Path(self.tmp.name) / 'history.sqlite')
        self.c = SimpleNamespace(
            history=self.history,
            optimizer=SimpleNamespace(lock=threading.RLock(), state={'startedAt': None}),
        )

    def tearDown(self):
        self.history.close()
        self.tmp.cleanup()

    def make(self, **kwargs):
        return WhatsChanged(self.c, now=lambda: 1_800_000_000.0, **kwargs)

    def test_brand_new_install_is_not_returning_and_stays_that_way(self):
        notes = self.make()
        self.assertEqual(
            notes.status(), {'schema': 1, 'available': True, 'returning': False, 'seen': []}
        )
        # Finishing setup later does not turn a new install into an upgrade.
        self.history.cache('setup-v1', {'completed': True})
        self.assertFalse(self.make().status()['returning'])

    def test_finished_setup_or_earlier_optimizer_use_means_an_earlier_version(self):
        self.history.cache('setup-v1', {'completed': True})
        self.assertTrue(self.make().status()['returning'])
        with self.history.lock:
            self.history.db.execute('DELETE FROM cache WHERE key=?', (KEY,))
        self.history.cache('setup-v1', {'completed': False})
        self.c.optimizer.state['startedAt'] = 1_700_000_000
        self.assertTrue(self.make().status()['returning'])

    def test_seen_is_saved_once_bounded_and_shared(self):
        notes = self.make()
        self.assertEqual(
            notes.action({'action': 'seen', 'id': 'optimizer-manager-2026-09'})['seen'],
            ['optimizer-manager-2026-09'],
        )
        notes.action({'action': 'seen', 'id': 'optimizer-manager-2026-09'})
        self.assertEqual(self.make().status()['seen'], ['optimizer-manager-2026-09'])
        for n in range(SEEN_LIMIT + 3):
            notes.action({'action': 'seen', 'id': f'note-{n}'})
        self.assertEqual(len(self.history.cache(KEY)['seen']), SEEN_LIMIT)

    def test_strict_payload_and_preview(self):
        notes = self.make()
        for data in (
            {'action': 'seen'},
            {'action': 'unseen', 'id': 'a'},
            {'action': 'seen', 'id': 'A B'},
            {'action': 'seen', 'id': 'x' * 65},
            {'action': 'seen', 'id': 'a', 'extra': 1},
            ['seen'],
        ):
            with self.assertRaises(ValueError):
                notes.action(data)
        with self.assertRaises(PermissionError):
            notes.action({'action': 'seen', 'id': 'a'}, preview=True)
        self.assertFalse(notes.status(preview=True)['available'])
        self.assertFalse(self.make(enabled=False).status()['available'])

    def test_bad_saved_state_is_kept_and_reported_unavailable(self):
        self.history.cache(KEY, {'schema': 99})
        notes = self.make()
        self.assertFalse(notes.status()['available'])
        with self.assertRaises(RuntimeError):
            notes.action({'action': 'seen', 'id': 'a'})
        self.assertEqual(self.history.cache(KEY), {'schema': 99})


class WhatsChangedHTTPTests(unittest.TestCase):
    read = test_remote.RemoteHTTPTests.read
    denied = test_remote.RemoteHTTPTests.denied

    def setUp(self):
        test_remote.RemoteHTTPTests.setUp(self)
        self.c = self.local.RequestHandlerClass.collector
        self.write = {'Content-Type': 'application/json', 'X-Bloom-Action': 'whats-changed'}

    def tearDown(self):
        self.c.close()
        test_remote.RemoteHTTPTests.tearDown(self)
        self.c.history.close()

    def test_mac_and_phone_share_seen_notes(self):
        body = json.dumps({'action': 'seen', 'id': 'optimizer-manager-2026-09'}).encode()
        self.denied(self.local, '/api/whats-changed', {'Content-Type': 'application/json'}, body)
        self.denied(self.phone, '/api/whats-changed', {**self.headers, **self.write}, body)
        phone = {**self.headers, **self.write, 'Origin': 'https://' + self.headers['Host']}
        with self.read(self.phone, '/api/whats-changed', phone, body) as response:
            self.assertEqual(json.load(response)['seen'], ['optimizer-manager-2026-09'])
        with self.read(self.local, '/api/whats-changed') as response:
            self.assertEqual(json.load(response)['seen'], ['optimizer-manager-2026-09'])


if __name__ == '__main__':
    unittest.main()
