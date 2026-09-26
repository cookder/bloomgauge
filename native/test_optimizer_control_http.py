"""Cached control endpoint retains local and owner-phone write boundaries."""

import json
import unittest
import urllib.error
from unittest.mock import Mock
import test_remote
import test_optimizer_control as fixtures


class ControlHTTPTests(unittest.TestCase):
    read = test_remote.RemoteHTTPTests.read
    denied = test_remote.RemoteHTTPTests.denied

    def setUp(self):
        test_remote.RemoteHTTPTests.setUp(self)
        self.fixture = fixtures.ControlTests()
        self.fixture.setUp()
        self.collector = self.local.RequestHandlerClass.collector
        self.original = self.collector.optimizer
        self.collector.optimizer = self.fixture.o
        self.collector.usage.observe = Mock(side_effect=AssertionError('No usage work in controls'))
        self.write = {
            **self.headers,
            'Content-Type': 'application/json',
            'X-Bloom-Action': 'optimizer',
            'Origin': 'https://' + test_remote.HOST + ':8443',
        }

    def tearDown(self):
        self.collector.optimizer = self.original
        for server in (self.local, self.phone):
            server.shutdown()
            server.server_close()
        self.collector.close()
        self.collector.history.close()
        self.fixture.tearDown()
        self.tmp.cleanup()

    def test_local_and_phone_get_are_history_free_and_private(self):
        for server, headers in ((self.local, {}), (self.phone, self.headers)):
            with self.read(server, '/api/optimizer/control', headers) as response:
                view = json.load(response)
            self.assertEqual(view['actualMode'], 'observe')
            self.assertEqual(view['automatic']['mode'], 'manual')
            self.assertNotIn('account', view)
            self.assertNotIn('device', view)
            self.assertNotIn('events', view)
        self.fixture.o.store.summary.assert_not_called()
        self.denied(
            self.phone,
            '/api/optimizer/control',
            {**self.headers, 'Tailscale-User-Login': 'foreign@example.com'},
        )

    def test_phone_post_rejects_foreign_origin_identity_and_missing_action(self):
        body = json.dumps(self.fixture.request()).encode()
        for change in (
            {'Origin': 'null'},
            {'Origin': 'https://foreign.example'},
            {'Tailscale-User-Login': 'foreign@example.com'},
            {'X-Bloom-Action': 'manual-model'},
        ):
            self.denied(self.phone, '/api/optimizer/control', {**self.write, **change}, body)
        self.denied(
            self.phone,
            '/api/optimizer/control',
            {k: v for k, v in self.write.items() if k != 'Origin'},
            body,
        )
        self.denied(
            self.local, '/api/optimizer/control', {'Content-Type': 'application/json'}, body
        )
        self.assertIsNone(self.fixture.control.operation)
        self.fixture.o.runner.assert_not_called()

    def test_accepted_post_lost_reply_is_reconciled_by_id_without_dispatch(self):
        data = self.fixture.request()
        body = json.dumps(data).encode()
        with self.read(self.phone, '/api/optimizer/control', self.write, body) as response:
            response.read()
        with self.read(self.phone, '/api/optimizer/control', self.headers) as response:
            view = json.load(response)
        self.assertEqual(view['operation']['id'], data['requestId'])
        self.assertEqual(view['lastRequestId'], data['requestId'])
        with self.read(self.phone, '/api/optimizer/control', self.write, body) as response:
            self.assertEqual(json.load(response)['operation']['id'], data['requestId'])
        self.assertEqual(len(self.fixture.control.requests), 1)
        self.fixture.o.runner.assert_not_called()
        self.fixture.o.store.summary.assert_not_called()
        self.collector.usage.observe.assert_not_called()

    def test_rejected_post_does_not_become_accepted_after_healthy_get(self):
        data = self.fixture.request(expectedControl='stale')
        with self.assertRaises(urllib.error.HTTPError) as failure:
            self.read(self.phone, '/api/optimizer/control', self.write, json.dumps(data).encode())
        self.assertEqual(failure.exception.code, 400)
        with self.read(self.phone, '/api/optimizer/control', self.headers) as response:
            view = json.load(response)
        self.assertNotEqual(view['lastRequestId'], data['requestId'])
        self.assertIsNone(view['operation'])
        self.assertEqual(view['actualMode'], 'observe')


if __name__ == '__main__':
    unittest.main()
