import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.error

from collector import Collector
from machines import Machines, NoRedirect, fetch_summary, local_summary, origin, validate_summary
import test_remote


class MachineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.c = Collector(Path(self.tmp.name))
        self.now = 1800000030
        self.c.machines.clock = lambda: self.now
        self.c.account = 'private-account'
        self.c.optimizer.live = {'account': 'private-account', 'device': 'private-device'}
        self.c.optimizer.store.identity('private-device', 'provider-one')
        self.c.snapshot = {
            'at': self.now,
            'hardware': {
                'chip': 'M5 Pro',
                'memoryTotalGB': 48,
                'cpuTemp': 50,
                'gpuTemp': 70,
                'gpuPercent': 90,
            },
            'provider': {'tracking': {'counting': True}},
            'earnings': {'status': 'ok', 'balance': 9999},
            'pulse': {
                'updatedAt': self.now,
                'status': 'live',
                'models': ['gemma'],
                'windows': {'60': {'ratePerHour': 0.9}, '300': {'ratePerHour': 0.15}},
            },
        }
        self.c.history.db.executemany(
            'INSERT INTO opt_credits VALUES(?,?,?,?,?,?,?)',
            [
                ('private-account', 1, 'provider-one', self.now - 200, 'gemma', 100000, 100),
                ('private-account', 2, 'provider-other', self.now - 200, 'nemotron', 500000, 100),
                ('private-account', 3, 'provider-one', self.now - 200, 'base_reward', 200000, 0),
                ('other-account', 4, 'provider-one', self.now - 200, 'gemma', 900000, 100),
            ],
        )
        self.c.history.db.execute(
            'INSERT INTO opt_coverage VALUES(?,?,?)',
            ('private-account', self.now - 86400, self.now),
        )
        self.c.history.db.commit()

    def tearDown(self):
        self.c.close()
        self.c.history.close()
        self.tmp.cleanup()

    def report(self, hours=24, installation='1' * 32, device='b' * 64, **updates):
        r = local_summary(self.c, hours, self.now)
        r.update(installation=installation, deviceKey=device, **updates)
        return r

    def connect(self, name='Studio', url='https://studio.test.ts.net:8443'):
        self.c.machines.action({'action': 'add', 'name': name, 'url': url})

    def settled(self, hours=24):
        self.c.machines.snapshot(hours)
        for f in list(self.c.machines.pending.values()):
            f.result(timeout=2)
        return self.c.machines.snapshot(hours)

    def test_device_inference_scope_fahrenheit_and_no_private_payload(self):
        r = local_summary(self.c, 24, self.now)
        self.assertAlmostEqual(r['knownInferenceUsd'], 0.1)
        self.assertEqual(r['coveredSeconds'], 86400 - 120)
        self.assertEqual(r['cpuTempF'], 122)
        self.assertEqual(r['gpuTempF'], 158)
        for secret in ('private-account', 'private-device', 'provider-one', '9999'):
            self.assertNotIn(secret, json.dumps(r))
        self.assertNotIn('balance', r)
        self.assertTrue(r['ready'])
        self.assertEqual(r['ratePerHour'], 0.15)

    def test_identity_missing_is_unknown_not_zero(self):
        self.c.optimizer.live['device'] = 'unknown'
        r = local_summary(self.c, 24, self.now)
        self.assertIsNone(r['knownInferenceUsd'])
        self.assertIsNone(r['deviceKey'])
        self.assertIsNone(self.c.machines.snapshot()['knownInferenceUsd'])

    def test_name_persists_and_updates_current_device_label_immediately(self):
        self.c.machines.action({'action': 'rename', 'name': 'Desk Mac'})
        self.assertEqual(self.c.snapshot['deviceName'], 'Desk Mac')
        loaded = Machines(self.c)
        self.assertEqual(loaded.name, 'Desk Mac')
        loaded.close()

    def test_account_change_does_not_mix_identities(self):
        self.c.account = 'different'
        self.assertIsNone(local_summary(self.c, 24, self.now)['knownInferenceUsd'])

    def test_partial_coverage_signed_credits_and_stale_metrics(self):
        self.c.history.db.execute('DELETE FROM opt_coverage')
        self.c.history.db.execute('UPDATE opt_credits SET micro_usd=-120000 WHERE id=1')
        r = local_summary(self.c, 24, self.now)
        self.assertEqual(r['knownInferenceUsd'], -0.12)
        self.assertEqual(r['coveredSeconds'], 0)
        self.now += 60
        r = local_summary(self.c, 24, self.now)
        self.assertFalse(r['ready'])
        self.assertFalse(r['earningsFresh'])
        self.assertIsNone(r['ratePerHour'])
        self.assertIsNone(r['gpuTempF'])
        self.assertIsNone(self.c.machines.snapshot()['knownInferenceUsd'])

    def test_ranges_validation_and_half_open_bounds(self):
        self.c.history.db.execute(
            'INSERT INTO opt_credits VALUES(?,?,?,?,?,?,?)',
            ('private-account', 5, 'provider-one', self.now, 'gemma', 900000, 100),
        )
        for hours in (1, 24, 168):
            self.assertEqual(local_summary(self.c, hours, self.now)['knownInferenceUsd'], 0.1)
        for hours in (0, 2, 169, 24.0, True):
            with self.assertRaises(ValueError):
                local_summary(self.c, hours, self.now)

    def test_restricted_origins_no_credentials_paths_or_public_hosts(self):
        self.assertEqual(
            origin('https://studio.test.ts.net:8443/'), 'https://studio.test.ts.net:8443'
        )
        for value in (
            'http://studio.test.ts.net:8443',
            'https://studio.test.ts.net',
            'https://studio.test.ts.net:443',
            'https://studio.test.ts.net:8443/api',
            'https://user@studio.test.ts.net:8443',
            'https://studio.test.ts.net:8443?key=x',
            'https://studio.test.ts.net:8443#x',
            'https://127.0.0.1:8443',
            'https://evil.com:8443',
            'https://studio.test.ts.net.evil.com:8443',
            'file:///etc/passwd',
            None,
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                origin(value)
        with self.assertRaises(ValueError):
            NoRedirect().redirect_request(None, None, None, None, None, None)

    def test_validation_strips_extras_and_rejects_malformed_reports(self):
        r = self.report()
        r['account'] = 'secret'
        self.assertNotIn('account', validate_summary(r, 24))
        # Peers on 1.36.45 and older send the paid-era 'pro' flag; later ones may not.
        self.assertIs(r['pro'], True)
        self.assertNotIn('pro', validate_summary(r, 24))
        self.assertEqual(
            validate_summary({k: v for k, v in r.items() if k != 'pro'}, 24),
            validate_summary(r, 24),
        )
        for change in (
            {'at': float('nan')},
            {'hours': 168},
            {'schema': 2},
            {'ready': 'yes'},
            {'deviceKey': 'bad'},
            {'models': ['x'] * 6},
            {'coveredSeconds': 86401},
            {'from': -1},
            {'to': self.now - 5},
            {'scope': {'secret': 1}},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_summary({**r, **change}, 24)

    def test_pair_persists_and_duplicate_self_rejected(self):
        self.c.machines.fetcher = lambda url, h: self.report(h)
        self.connect()
        loaded = Machines(self.c, clock=lambda: self.now)
        self.assertEqual(loaded.peers, self.c.machines.peers)
        loaded.close()
        with self.assertRaises(ValueError):
            self.connect()
        with self.assertRaises(ValueError):
            self.connect(url='https://second.test.ts.net:8443')
        self.c.machines.fetcher = lambda u, h: self.report(h, installation=self.c.installation)
        with self.assertRaises(ValueError):
            self.connect(url='https://self.test.ts.net:8443')
        self.assertEqual(len(self.c.machines.peers), 1)

    def test_ten_mac_limit_and_removal_never_changes_optimizer(self):
        self.c.optimizer.state['mode'] = 'demand'
        self.c.machines.fetcher = lambda u, h: self.report(h, installation=u.split('//')[1][0] * 32)
        for i in range(1, 10):
            self.connect(str(i), f'https://{i}.test.ts.net:8443')
        with self.assertRaises(ValueError):
            self.connect('Tenth remote', 'https://a.test.ts.net:8443')
        self.c.machines.action({'action': 'remove', 'installation': '1' * 32})
        self.assertEqual(len(self.c.machines.peers), 8)
        self.assertEqual(self.c.optimizer.state['mode'], 'demand')

    def test_connected_total_and_duplicate_physical_device(self):
        self.c.machines.fetcher = lambda u, h: self.report(h)
        self.connect()
        r = self.settled()
        self.assertEqual(r['included'], 2)
        self.assertEqual(r['knownInferenceUsd'], 0.2)
        self.assertTrue(r['complete'])
        self.c.machines.fetcher = lambda u, h: self.report(
            h, device=local_summary(self.c, h, self.now)['deviceKey']
        )
        self.now += 16
        self.c.snapshot['at'] = self.now
        r = self.settled()
        self.assertEqual(r['included'], 1)
        self.assertEqual(r['machines'][1]['status'], 'duplicate-device')

    def test_failed_connection_keeps_history_but_excludes_total(self):
        self.c.machines.fetcher = lambda u, h: self.report(h)
        self.connect()
        self.settled()
        self.c.machines.fetcher = lambda *args: (_ for _ in ()).throw(ValueError('secret error'))
        self.now += 16
        self.c.snapshot['at'] = self.now
        r = self.settled()
        self.assertEqual(r['machines'][1]['status'], 'unreachable')
        self.assertIsNotNone(r['machines'][1]['report'])
        self.assertFalse(r['complete'])
        self.assertEqual(r['included'], 1)
        self.assertNotIn('secret error', json.dumps(r))

    def test_identity_change_is_not_silently_trusted(self):
        self.c.machines.fetcher = lambda u, h: self.report(h)
        self.connect()
        self.settled()
        self.now += 16
        self.c.snapshot['at'] = self.now
        self.c.machines.fetcher = lambda u, h: self.report(h, installation='2' * 32)
        r = self.settled()
        self.assertEqual(r['machines'][1]['status'], 'identity-changed')
        self.assertEqual(r['included'], 1)

    def test_peer_work_is_read_only_coalesced_and_bounded(self):
        self.c.machines.fetcher = lambda u, h: self.report(h)
        self.connect()
        with patch.object(
            self.c.optimizer, 'runner', side_effect=AssertionError('No provider control')
        ):
            for i in range(25):
                self.c.machines.snapshot((1, 24, 168)[i % 3], remote=True)
            self.assertLessEqual(len(self.c.machines.pending), 4)
            self.assertFalse(self.c.machines.snapshot(remote=True)['canManage'])


class MachineHTTPTests(unittest.TestCase):
    setUp = test_remote.RemoteHTTPTests.setUp
    tearDown = test_remote.RemoteHTTPTests.tearDown
    read = test_remote.RemoteHTTPTests.read
    denied = test_remote.RemoteHTTPTests.denied

    def test_owner_only_read_local_only_connections_action_header_and_origins(self):
        for server, headers in ((self.local, {}), (self.phone, self.headers)):
            with self.read(server, '/api/machines', headers) as response:
                r = json.load(response)
                self.assertEqual(r['canManage'], server is self.local)
                self.assertEqual(len(r['machines']), 1)
            with self.read(server, '/api/machines/summary', headers) as response:
                self.assertEqual(json.load(response)['schema'], 1)
        for path in ('/api/machines', '/api/machines/summary'):
            self.denied(self.phone, path, {'Host': test_remote.HOST + ':8443'})
            self.denied(
                self.phone, path, {**self.headers, 'Tailscale-User-Login': 'different@example.com'}
            )
        payload = json.dumps({'action': 'rename', 'name': 'Studio'}).encode()
        headers = {'Content-Type': 'application/json', 'X-Bloom-Action': 'machines'}
        self.denied(
            self.phone,
            '/api/machines',
            {**self.headers, **headers, 'Origin': 'https://' + test_remote.HOST + ':8443'},
            payload,
        )
        self.denied(self.local, '/api/machines', {'Content-Type': 'application/json'}, payload)
        self.denied(
            self.local, '/api/machines', {**headers, 'Origin': 'https://evil.example'}, payload
        )
        with self.read(self.local, '/api/machines', headers, payload) as response:
            self.assertTrue(json.load(response)['saved'])
        with self.read(self.local, '/api/machines/summary') as response:
            self.assertEqual(json.load(response)['name'], 'Studio')

    def test_bad_ranges_and_preview_cannot_connect_to_real_peers(self):
        for q in ('hours=2', 'hours=nan', 'hours=24&hours=1', 'url=x'):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.read(self.local, '/api/machines?' + q)
            self.assertEqual(error.exception.code, 400)
        self.local.RequestHandlerClass.setup_preview = True
        headers = {'Content-Type': 'application/json', 'X-Bloom-Action': 'machines'}
        payload = json.dumps(
            {'action': 'add', 'name': 'Mac', 'url': 'https://test.test.ts.net:8443'}
        ).encode()
        self.denied(self.local, '/api/machines', headers, payload)


if __name__ == '__main__':
    unittest.main()
