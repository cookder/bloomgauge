"""Read-only private reporting shares owner guards and cannot hold controls."""

import json
import threading
import time
import unittest
import urllib.error
from unittest.mock import Mock, patch
import network_contributions
import test_remote


class NetworkContributionsHTTPTests(unittest.TestCase):
    read = test_remote.RemoteHTTPTests.read
    denied = test_remote.RemoteHTTPTests.denied

    def setUp(self):
        test_remote.RemoteHTTPTests.setUp(self)
        self.c = self.local.RequestHandlerClass.collector
        self.c.account = 'account'
        self.c.optimizer.live = {'account': 'account', 'device': 'mac'}
        self.path = '/api/network/contributions?metric=activity&from=1790099400&to=1790100000'

    def tearDown(self):
        for server in (self.local, self.phone):
            server.shutdown()
            server.server_close()
        self.c.close()
        self.c.history.close()
        self.tmp.cleanup()

    def test_owner_local_and_phone_get_contract_without_compute_or_entitlement_gate(self):
        self.c.optimizer.snapshot = Mock(side_effect=AssertionError('No optimizer decision'))
        self.c.optimizer.store.evidence = Mock(side_effect=AssertionError('No warm-hour evidence'))
        writes = self.c.history.db.total_changes
        for server, headers in ((self.local, {}), (self.phone, self.headers)):
            for metric in ('activity', 'requests', 'tokens', 'earnings'):
                with self.read(server, self.path.replace('activity', metric), headers) as response:
                    value = json.load(response)
                self.assertEqual(value['schemaVersion'], 1)
                self.assertEqual(value['metric'], metric)
                self.assertLessEqual(len(value['points']), 600)
                self.assertNotIn('account', value)
                self.assertNotIn('device', value)
                self.assertFalse(value['networkMoney']['available'])
        self.assertEqual(self.c.history.db.total_changes, writes)
        self.c.optimizer.snapshot.assert_not_called()
        self.c.optimizer.store.evidence.assert_not_called()
        self.denied(self.phone, self.path)
        self.denied(
            self.phone, self.path, {**self.headers, 'Tailscale-User-Login': 'foreign@example.com'}
        )
        self.denied(self.phone, self.path, {**self.headers, 'Origin': 'https://foreign.example'})
        self.denied(self.local, self.path, {'Origin': 'https://foreign.example'})

    def test_query_rejection_and_post_do_not_change_settings(self):
        state = json.dumps(self.c.optimizer.state, sort_keys=True)
        for query in (
            'metric=activity&from=1&to=2&account=foreign',
            'metric=activity&from=1&to=2&device=other',
            'metric=earnings&from=NaN&to=2',
            'metric=activity&from=2&to=1',
            'metric=activity&from=1&to=2&metric=tokens',
            'metric=activity&from=1',
            'metric=unknown&from=1&to=2',
            'metric=activity&from=1&to=9999999999',
        ):
            with self.assertRaises(urllib.error.HTTPError) as failure:
                self.read(self.local, '/api/network/contributions?' + query)
            self.assertEqual(failure.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError):
            self.read(
                self.local, self.path, {'Content-Type': 'application/json'}, b'{"action":"switch"}'
            )
        self.assertEqual(json.dumps(self.c.optimizer.state, sort_keys=True), state)

    def test_slow_report_and_singleflight_leave_both_control_routes_responsive(self):
        entered = threading.Event()
        release = threading.Event()
        errors = []
        original = network_contributions.report
        self.c.optimizer.manual_snapshot = Mock(return_value={'models': [], 'controlError': None})

        def slow(*args, **kwargs):
            entered.set()
            release.wait(3)
            return original(*args, **kwargs)

        def request():
            try:
                with self.read(self.local, self.path) as response:
                    json.load(response)
            except Exception as error:
                errors.append(error)

        with patch.object(network_contributions, 'report', side_effect=slow):
            thread = threading.Thread(target=request)
            thread.start()
            self.assertTrue(entered.wait(1))
            try:
                began = time.monotonic()
                for path in ('/api/model-control', '/api/optimizer/control'):
                    with self.read(self.local, path) as response:
                        json.load(response)
                self.assertLess(time.monotonic() - began, 0.5)
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    self.read(self.local, self.path)
                self.assertEqual(failure.exception.code, 503)
            finally:
                release.set()
                thread.join(4)
        self.assertFalse(errors)

    def test_report_failure_is_private_and_controls_stay_available(self):
        self.c.optimizer.manual_snapshot = Mock(return_value={'models': [], 'controlError': None})
        with patch.object(
            self.c.network_contributions, 'get', side_effect=RuntimeError('private-account-details')
        ):
            with self.assertRaises(urllib.error.HTTPError) as failure:
                self.read(self.local, self.path)
            self.assertEqual(failure.exception.code, 503)
            self.assertNotIn('private-account', failure.exception.read().decode())
        for path in ('/api/model-control', '/api/optimizer/control'):
            with self.read(self.local, path) as response:
                self.assertEqual(response.status, 200)


if __name__ == '__main__':
    unittest.main()
