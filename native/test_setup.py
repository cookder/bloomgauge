import json
from pathlib import Path
import tempfile
import unittest
import urllib.error
from collector import Collector
from history import History
from setup import Setup, earnings_connection
import test_remote


class SetupTests(unittest.TestCase):
    def test_connection_distinguishes_auth_failure_from_loading_and_network(self):
        cases = [
            ({'status': 'connecting', 'updatedAt': None}, True, 'connecting'),
            (
                {'status': 'missing', 'error': 'Login expired. Sign in again using Darkbloom.'},
                True,
                'sign_in_required',
            ),
            (
                {
                    'status': 'missing',
                    'error': 'Darkbloom returned HTTP 503; retrying automatically.',
                },
                True,
                'unavailable',
            ),
            (
                {
                    'status': 'missing',
                    'error': 'Could not reach Darkbloom; retrying automatically.',
                },
                True,
                'unavailable',
            ),
            (
                {
                    'status': 'missing',
                    'error': 'The earnings response could not be read; retrying automatically.',
                },
                True,
                'unavailable',
            ),
            ({'status': 'missing'}, False, 'sign_in_required'),
            ({'status': 'ok', 'updatedAt': 970}, True, 'connected'),
            ({'status': 'ok', 'updatedAt': 939}, True, 'stale'),
            ({'status': 'ok', 'updatedAt': 1001}, True, 'stale'),
            (
                {'status': 'stale', 'updatedAt': 970, 'error': 'Reconnecting to Darkbloom…'},
                True,
                'stale',
            ),
        ]
        for earnings, login, expected in cases:
            with self.subTest(earnings=earnings, login=login):
                state = earnings_connection(earnings, login, 1000)
                self.assertEqual(state['status'], expected)
                if expected in ('unavailable', 'stale', 'connecting'):
                    self.assertNotIn('Sign in again', state['detail'])

    def test_setup_exposes_sanitized_actionable_connection_and_keeps_mode(self):
        with tempfile.TemporaryDirectory() as home:
            c = Collector(home=home)
            try:
                token = Path(home) / '.darkbloom/auth_token'
                token.parent.mkdir(parents=True, exist_ok=True)
                token.write_text('synthetic-secret')
                c.earnings_error('Login expired. Sign in again using Darkbloom.')
                state = c.setup.status(c)
                self.assertTrue(state['loginPresent'])
                self.assertFalse(state['earningsConnected'])
                self.assertEqual(state['earningsConnection']['status'], 'sign_in_required')
                self.assertIn('Sign in again', state['earningsConnection']['detail'])
                c.earnings_error(
                    'Unexpected failure at https://private.invalid/?token=synthetic-secret'
                )
                state = c.setup.status(c)
                self.assertEqual(state['earningsConnection']['status'], 'unavailable')
                self.assertNotIn('synthetic-secret', json.dumps(state))
                self.assertEqual(c.optimizer.state['mode'], 'observe')
                self.assertEqual(set(c.setup.status(c, remote=True)), {'localOnly', 'completed'})
            finally:
                c.close()
                c.history.close()

    def test_new_install_observes_without_tariff_or_source_data(self):
        with tempfile.TemporaryDirectory() as home:
            c = Collector(home=home)
            state = c.setup.status(c)
            self.assertFalse(state['completed'])
            self.assertFalse(state['loginPresent'])
            self.assertEqual(c.optimizer.state['mode'], 'observe')
            self.assertIsNone(c.energy.tariff()['rate'])
            c.setup.complete({'action': 'complete', 'understood': True})
            self.assertTrue(c.setup.status(c)['completed'])
            self.assertEqual(c.optimizer.state['mode'], 'observe')
            c.close()
            c.history.close()

    def test_pending_survives_reopen_existing_install_preserves_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'test.sqlite3'
            c = Collector(home=directory, data_path=path)
            c.close()
            c.history.close()
            c = Collector(home=directory, data_path=path)
            self.assertFalse(c.setup.status(c)['completed'])
            c.close()
            c.history.close()
        h = History(':memory:')
        h.cache('optimizer-settings', {'mode': 'demand'})
        Setup(h, Path('/nonexistent'))
        self.assertTrue(h.cache('setup-v1')['completed'])
        self.assertEqual(h.cache('optimizer-settings'), {'mode': 'demand'})
        h.close()

    def test_unknown_electricity_cost_remains_unknown_and_saved_tariff_is_kept(self):
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / 'test.sqlite3'
            c = Collector(home=home, data_path=path)
            for at in range(600, 661):
                c.energy.observe(
                    'account', 'mac', {'at': at, 'systemWatts': 60, 'powerSource': 'AC Power'}, at
                )
            d = c.energy.report('account', 'mac', 600, 660, 1000)
            self.assertAlmostEqual(d['totals']['kwh'], 0.001)
            self.assertIsNone(d['totals']['costUsd'])
            self.assertIsNone(d['comparison']['afterCostUsd'])
            c.energy.configure(
                {
                    'rate': 0.123,
                    'label': 'Saved customer tariff',
                    'expectedId': c.energy.tariff()['id'],
                }
            )
            c.close()
            c.history.close()
            c = Collector(home=home, data_path=path)
            self.assertEqual(c.energy.tariff()['rate'], 0.123)
            c.close()
            c.history.close()

    def test_no_implicit_automation_permission(self):
        h = History(':memory:')
        s = Setup(h, Path('/nonexistent'))
        for data in (
            None,
            [],
            {},
            {'action': 'complete', 'understood': 1},
            {'action': 'complete', 'understood': False},
            {'action': 'complete', 'understood': True, 'mode': 'demand'},
        ):
            with self.assertRaises(ValueError):
                s.complete(data)
        self.assertFalse(h.cache('setup-v1')['completed'])
        h.close()


class SetupHTTPTests(unittest.TestCase):
    setUp = test_remote.RemoteHTTPTests.setUp
    tearDown = test_remote.RemoteHTTPTests.tearDown
    read = test_remote.RemoteHTTPTests.read
    denied = test_remote.RemoteHTTPTests.denied

    def test_phone_cannot_configure_and_remote_read_is_minimal(self):
        with self.read(self.phone, '/api/setup', self.headers) as response:
            self.assertEqual(set(json.load(response)), {'localOnly', 'completed'})
        body = json.dumps({'action': 'complete', 'understood': True}).encode()
        headers = {'Content-Type': 'application/json', 'X-Bloom-Action': 'setup'}
        self.denied(self.phone, '/api/setup', {**self.headers, **headers}, body)
        self.denied(self.local, '/api/setup', {'Content-Type': 'application/json'}, body)
        self.denied(
            self.local, '/api/setup', {**headers, 'Origin': 'https://foreign.example'}, body
        )
        with self.read(self.local, '/api/setup', headers, body) as response:
            self.assertTrue(json.load(response)['completed'])


if __name__ == '__main__':
    unittest.main()
