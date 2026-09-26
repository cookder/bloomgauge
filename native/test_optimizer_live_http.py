"""Private read-only projection never falls through to analytics or commands."""

import copy
import json
import threading
import time
import unittest
import urllib.error
from unittest.mock import Mock, patch

import test_remote
import test_optimizer_live as fixtures


class OptimizerLiveHTTPTests(unittest.TestCase):
    read = test_remote.RemoteHTTPTests.read
    denied = test_remote.RemoteHTTPTests.denied

    def setUp(self):
        test_remote.RemoteHTTPTests.setUp(self)
        self.c = self.local.RequestHandlerClass.collector
        self.original = self.c.optimizer
        self.o = fixtures.fixture(time.time())
        self.c.optimizer = self.o
        self.c.account = self.o.live['account']
        self.o.live_projection.record(
            fixtures.decision(self.o.live['at']),
            copy.deepcopy(self.o.state),
            copy.deepcopy(self.o.live),
            copy.deepcopy(self.o.raw),
        )

    def tearDown(self):
        self.c.optimizer = self.original
        for server in (self.local, self.phone):
            server.shutdown()
            server.server_close()
        self.c.close()
        self.c.history.close()
        self.tmp.cleanup()

    def test_local_and_owner_phone_contract_without_analytics_or_provider_calls(self):
        for name in ('snapshot', 'demand_decision', 'read_state', 'read_options', 'runner', 'save'):
            setattr(self.o, name, Mock(side_effect=AssertionError('Forbidden in GET')))
        self.c.usage.observe = Mock(side_effect=AssertionError('No usage writes'))
        for server, headers in ((self.local, {}), (self.phone, self.headers)):
            with self.read(server, '/api/optimizer/live', headers) as response:
                value = json.load(response)
            self.assertEqual(value['schemaVersion'], 1)
            self.assertTrue(value['fresh'])
            self.assertEqual(value['comparisonTarget'], 'd')
            self.assertEqual(value['planningMinutes'], 60)
            self.assertNotIn('account', value)
            self.assertNotIn('device', value)
        self.denied(self.phone, '/api/optimizer/live')
        for headers in (
            {'Tailscale-User-Login': 'foreign@example.com'},
            {'Origin': 'https://foreign.example'},
        ):
            self.denied(self.phone, '/api/optimizer/live', {**self.headers, **headers})
        self.c.usage.observe.assert_not_called()

    def test_query_and_post_are_rejected_without_changes(self):
        before = copy.deepcopy(self.o.state)
        for suffix in ('?model=a', '?account=other', '?refresh=true'):
            with self.assertRaises(urllib.error.HTTPError) as failure:
                self.read(self.local, '/api/optimizer/live' + suffix)
            self.assertEqual(failure.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError):
            self.read(
                self.local,
                '/api/optimizer/live',
                {'Content-Type': 'application/json'},
                b'{"action":"switch"}',
            )
        self.assertEqual(self.o.state, before)

    def test_locked_history_does_not_delay_get(self):
        entered = threading.Event()
        release = threading.Event()

        def hold():
            with self.c.history.lock:
                entered.set()
                release.wait(3)

        thread = threading.Thread(target=hold)
        thread.start()
        self.assertTrue(entered.wait(1))
        try:
            start = time.monotonic()
            with self.read(self.local, '/api/optimizer/live') as response:
                value = json.load(response)
            self.assertLess(time.monotonic() - start, 0.5)
            self.assertEqual(value['comparisonTarget'], 'd')
        finally:
            release.set()
            thread.join(4)

    def test_error_is_bounded_and_account_change_clears_comparison(self):
        self.c.account = 'new-account'
        with self.read(self.local, '/api/optimizer/live') as response:
            value = json.load(response)
        self.assertFalse(value['fresh'])
        self.assertEqual(value['candidates'], [])
        with patch.object(
            self.o.live_projection,
            'snapshot',
            side_effect=RuntimeError('private-data-must-not-escape'),
        ):
            with self.assertRaises(urllib.error.HTTPError) as failure:
                self.read(self.local, '/api/optimizer/live')
            self.assertEqual(failure.exception.code, 503)
            self.assertNotIn('private-data', failure.exception.read().decode())


if __name__ == '__main__':
    unittest.main()
