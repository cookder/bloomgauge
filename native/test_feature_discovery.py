"""Synthetic time and temporary history only; no provider or analytics traffic."""

import copy
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error

from feature_discovery import FeatureDiscovery, KEY, READY_SECONDS, SNOOZE_SECONDS
from history import History
import test_remote


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'history.sqlite'
        self.history = History(self.path)
        self.wall = 1_800_000_000.0
        self.mono = 100.0
        self.c = SimpleNamespace(
            history=self.history,
            lock=threading.RLock(),
            snapshot=None,
            optimizer=SimpleNamespace(
                lock=threading.RLock(), state={'mode': 'observe', 'startedAt': None}
            ),
        )
        self.history.cache('setup-v1', {'completed': True})
        self.d = self.make()

    def tearDown(self):
        self.d.close()
        self.history.close()
        self.tmp.cleanup()

    def make(self, enabled=True):
        return FeatureDiscovery(
            self.c, enabled=enabled, now=lambda: self.wall, monotonic=lambda: self.mono
        )

    def sample(self, seconds=0, status='ok', online=True, error=None):
        self.wall += seconds
        self.mono += seconds
        self.c.snapshot = {
            'at': self.wall,
            'provider': {'online': online},
            'earnings': {
                'status': status,
                'error': error,
                'updatedAt': self.wall,
                'balance': 0,
                'lifetime': 0,
            },
        }
        self.d.observe()

    def ready(self):
        self.sample()
        for _ in range(READY_SECONDS // 15):
            self.sample(15)

    def test_two_hours_of_connected_monitoring_zero_earnings_is_enough(self):
        self.sample()
        for _ in range(479):
            self.sample(15)
        self.assertEqual(self.d.status()['features'], [])
        self.sample(15)
        self.assertEqual(self.d.state['healthySeconds'], 7200)
        self.assertEqual(
            self.d.status()['features'],
            [
                {'id': 'phone', 'destination': 'phone'},
                {'id': 'optimizer', 'destination': 'optimizer'},
            ],
        )

    def test_setup_history_install_age_and_preview_do_not_count(self):
        self.history.cache('setup-v1', {'completed': False, 'completedAt': self.wall - 999999})
        self.sample()
        self.sample(15)
        self.assertEqual(self.d.state['healthySeconds'], 0)
        self.history.cache('setup-v1', {'completed': True, 'completedAt': self.wall - 999999})
        self.sample(15)
        self.assertEqual(self.d.state['healthySeconds'], 0)
        self.d = self.make(enabled=False)
        self.sample(15)
        self.sample(15)
        self.assertEqual(self.d.status()['features'], [])
        with self.assertRaises(PermissionError):
            self.d.action({'action': 'dismiss', 'feature': 'phone'})
        self.assertIsNone(self.history.cache(KEY))

    def test_stale_error_and_offline_intervals_pause_elapsed_time(self):
        self.sample()
        self.sample(10)
        for status, online, error in [
            ('stale', True, None),
            ('ok', False, None),
            ('ok', True, 'reconnecting'),
        ]:
            self.sample(10, status, online, error)
            self.sample(10)
            self.assertEqual(self.d.state['healthySeconds'], 10)
        self.sample(10)
        self.assertEqual(self.d.state['healthySeconds'], 20)

    def test_sleep_closed_gaps_and_clock_jumps_do_not_count(self):
        self.sample()
        self.sample(10)
        self.sample(7200)
        self.assertEqual(self.d.state['healthySeconds'], 10)
        self.wall += 3600
        self.sample(10)
        self.assertEqual(self.d.state['healthySeconds'], 10)
        self.sample(10)
        self.assertEqual(self.d.state['healthySeconds'], 20)
        self.d.close()
        self.wall += 7200
        self.mono += 7200
        self.d = self.make()
        self.sample()
        self.assertEqual(self.d.state['healthySeconds'], 20)

    def test_duplicate_samples_and_browser_polling_never_add_time(self):
        self.sample()
        self.sample(10)
        for _ in range(10):
            self.d.observe()
            self.d.status()
        self.assertEqual(self.d.state['healthySeconds'], 10)
        self.wall += 600
        self.mono += 600
        self.d.observe()
        self.assertEqual(self.d.state['healthySeconds'], 10)
        self.assertEqual(self.d.status()['features'], [])

    def test_suppresses_stale_monitoring_after_becoming_ready(self):
        self.ready()
        self.wall += 11
        self.assertEqual(self.d.status()['features'], [])
        self.sample()
        self.c.snapshot['earnings']['updatedAt'] -= 61
        self.assertEqual(self.d.status()['features'], [])

    def test_dismiss_and_snooze_survive_process_and_database_reopen(self):
        self.ready()
        self.d.action({'action': 'dismiss', 'feature': 'phone'})
        self.d.action({'action': 'snooze', 'feature': 'optimizer'})
        self.d.close()
        self.history.close()
        self.history = History(self.path)
        self.c.history = self.history
        self.d = self.make()
        self.sample(15)
        self.assertEqual(self.d.status()['features'], [])
        self.sample(SNOOZE_SECONDS)
        self.assertEqual(
            self.d.status()['features'], [{'id': 'optimizer', 'destination': 'optimizer'}]
        )
        self.assertEqual(self.d.state['healthySeconds'], READY_SECONDS)

    def test_phone_configured_and_optimizer_started_stay_suppressed(self):
        self.ready()
        self.assertEqual(len(self.d.status(phone_configured=True)['features']), 1)
        self.c.optimizer.state.update(mode='observe', startedAt=self.wall - 400)
        self.sample(10)
        self.assertEqual(self.d.status()['features'], [])
        self.c.optimizer.state.update(mode='observe', startedAt=None)
        self.d = self.make()
        self.sample(10)
        self.assertEqual(self.d.status()['features'], [])

    def test_authenticated_phone_never_gets_phone_setup(self):
        self.ready()
        self.assertEqual(
            self.d.status(remote=True)['features'],
            [{'id': 'optimizer', 'destination': 'optimizer'}],
        )
        self.assertEqual(
            self.d.status()['features'][-1], {'id': 'optimizer', 'destination': 'optimizer'}
        )

    def test_open_only_snoozes_and_leaves_plan_provider_and_consent_untouched(self):
        self.ready()
        before = copy.deepcopy(self.c.optimizer.state)
        for feature in ('phone', 'optimizer'):
            self.d.action({'action': 'open', 'feature': feature})
        self.assertEqual(self.c.optimizer.state, before)
        self.assertIsNone(self.history.cache('usage-reporting'))
        self.assertEqual(self.d.status()['features'], [])
        self.assertFalse(any(s['used'] for s in self.d.state['features'].values()))

    def test_preview_read_does_not_record_phone_or_optimizer_use(self):
        self.ready()
        before = copy.deepcopy(self.d.state)
        self.assertEqual(
            self.d.status(remote=True, phone_configured=True, preview=True)['features'], []
        )
        self.assertEqual(self.d.state, before)

    def test_failed_save_is_not_a_successful_dismissal(self):
        self.ready()
        real_cache = self.history.cache

        def fail_write(key, data=None):
            if key == KEY and data is not None:
                raise OSError('test disk failure')
            return real_cache(key, data)

        with patch.object(self.history, 'cache', side_effect=fail_write):
            with self.assertRaises(OSError):
                self.d.action({'action': 'dismiss', 'feature': 'phone'})
            self.assertFalse(self.d.state['features']['phone']['dismissed'])
            self.assertFalse(self.d.status()['available'])
        self.d.action({'action': 'dismiss', 'feature': 'phone'})
        self.assertTrue(self.d.state['features']['phone']['dismissed'])

    def test_invalid_saved_preferences_suppress_instead_of_resetting(self):
        self.history.cache(KEY, {'schema': 99, 'features': {'phone': {'dismissed': True}}})
        self.d = self.make()
        self.sample()
        self.sample(15)
        self.assertFalse(self.d.status()['available'])
        with self.assertRaises(RuntimeError):
            self.d.action({'action': 'dismiss', 'feature': 'phone'})
        self.assertEqual(self.history.cache(KEY)['schema'], 99)

    def test_status_never_exposes_counter_preferences_or_identifiers(self):
        self.ready()
        self.assertEqual(set(self.d.status()), {'schema', 'available', 'localOnly', 'features'})


class DiscoveryHTTPTests(unittest.TestCase):
    read = test_remote.RemoteHTTPTests.read
    denied = test_remote.RemoteHTTPTests.denied

    def setUp(self):
        test_remote.RemoteHTTPTests.setUp(self)
        self.c = self.local.RequestHandlerClass.collector
        self.c.setup.complete({'action': 'complete', 'understood': True})
        self.c.snapshot.update(
            at=time.time(),
            provider={'online': True},
            earnings={'status': 'ok', 'updatedAt': time.time(), 'error': None},
        )
        self.c.discovery.state['healthySeconds'] = READY_SECONDS
        self.write_headers = {'Content-Type': 'application/json', 'X-Bloom-Action': 'discovery'}

    def tearDown(self):
        self.c.close()
        test_remote.RemoteHTTPTests.tearDown(self)
        self.c.history.close()

    def post(self, data, server=None, headers=None):
        with self.read(
            server or self.local,
            '/api/discovery',
            headers or self.write_headers,
            json.dumps(data).encode(),
        ) as response:
            return json.load(response)

    def test_owner_action_and_exact_origin_required(self):
        self.denied(self.phone, '/api/discovery')
        body = b'{"action":"dismiss","feature":"optimizer"}'
        self.denied(self.local, '/api/discovery', {'Content-Type': 'application/json'}, body)
        self.denied(
            self.local,
            '/api/discovery',
            {**self.write_headers, 'Origin': 'https://foreign.example'},
            body,
        )
        self.denied(self.phone, '/api/discovery', {**self.headers, **self.write_headers}, body)
        phone = {**self.headers, **self.write_headers, 'Origin': 'https://' + self.headers['Host']}
        self.assertEqual(
            self.post({'action': 'dismiss', 'feature': 'optimizer'}, self.phone, phone)['features'],
            [],
        )
        self.assertTrue(self.c.history.cache(KEY)['features']['optimizer']['dismissed'])

    def test_strict_payload_and_preview_write_guards(self):
        for data in (
            {'action': 'enable', 'feature': 'phone'},
            {'action': 'snooze', 'feature': 'phone', 'seconds': 0},
            [],
            {'action': 'dismiss', 'feature': 'unknown'},
        ):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.post(data)
            self.assertEqual(caught.exception.code, 400)
        self.local.RequestHandlerClass.setup_preview = True
        try:
            self.denied(
                self.local,
                '/api/discovery',
                self.write_headers,
                b'{"action":"dismiss","feature":"phone"}',
            )
            with self.read(self.local, '/api/discovery') as response:
                self.assertFalse(json.load(response)['available'])
        finally:
            self.local.RequestHandlerClass.setup_preview = False

    def test_prompt_actions_never_activate_features_or_analytics(self):
        before = copy.deepcopy(self.c.optimizer.state)
        remote = copy.deepcopy(self.remote.config)
        for feature in ('phone', 'optimizer'):
            self.post({'action': 'open', 'feature': feature})
        self.assertEqual(self.c.optimizer.state, before)
        self.assertEqual(self.remote.config, remote)
        self.assertFalse(self.c.usage.status()['enabled'])

    def test_phone_configuration_then_disable_does_not_reintroduce_suggestion(self):
        self.remote.config = {'enabled': True}
        headers = {'Content-Type': 'application/json', 'X-Bloom-Action': 'remote-access'}

        def disable(_):
            self.remote.config = {'enabled': False}
            return {'enabled': False}

        with patch.object(self.remote, 'action', side_effect=disable):
            with self.read(self.local, '/api/remote', headers, b'{"action":"disable"}') as response:
                self.assertFalse(json.load(response)['enabled'])
        self.assertTrue(self.c.history.cache(KEY)['features']['phone']['used'])
        self.assertFalse(self.c.usage.status()['enabled'])


if __name__ == '__main__':
    unittest.main()
