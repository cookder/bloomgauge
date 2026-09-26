"""Owner-scoped demand readouts and explicit, narrow push subscription writes."""

import json
import unittest
import urllib.error
from unittest.mock import Mock, patch
import time
import test_remote
from web_push import WebPush


class DemandHTTPTests(unittest.TestCase):
    setUp = test_remote.RemoteHTTPTests.setUp
    read = test_remote.RemoteHTTPTests.read
    denied = test_remote.RemoteHTTPTests.denied

    def tearDown(self):
        for server in (self.local, self.phone):
            server.shutdown()
            server.server_close()
        collector = self.local.RequestHandlerClass.collector
        collector.close()
        collector.history.close()
        self.tmp.cleanup()

    def test_notifications_require_owner_origin_action_and_bounded_body(self):
        collector = self.local.RequestHandlerClass.collector
        collector.account = 'current'
        collector.web_push = Mock()
        collector.web_push.subscribe.return_value = {'supported': True, 'subscriptionCount': 1}
        headers = {
            **self.headers,
            'Content-Type': 'application/json',
            'X-Bloom-Action': 'demand-alerts',
            'Origin': 'https://' + test_remote.HOST + ':8443',
        }
        path = '/api/demand-alerts/notifications'
        body = json.dumps({'action': 'subscribe', 'subscription': {'endpoint': 'fixture'}}).encode()
        for changes in (
            {'Origin': 'null'},
            {'Origin': 'https://foreign.example'},
            {'Tailscale-User-Login': 'foreign@example.com'},
            {'X-Bloom-Action': 'optimizer'},
            {'Sec-Fetch-Site': 'cross-site'},
            {'X-Forwarded-Proto': 'http'},
        ):
            self.denied(self.phone, path, {**headers, **changes}, body)
        for key in ('Origin', 'Tailscale-User-Login', 'X-Bloom-Action'):
            self.denied(self.phone, path, {k: v for k, v in headers.items() if k != key}, body)
        for payload in (
            b'[]',
            b'null',
            b'not json',
            b' ' * 8193,
            b'{"action":"switch"}',
            b'{"action":"subscribe","subscription":{},"extra":1}',
        ):
            with self.assertRaises(urllib.error.HTTPError) as result:
                self.read(self.phone, path, headers, payload)
            self.assertEqual(result.exception.code, 400)
        collector.web_push.subscribe.assert_not_called()
        with self.read(self.phone, path, headers, body) as response:
            self.assertEqual(json.load(response)['subscriptionCount'], 1)
        collector.web_push.subscribe.assert_called_once_with('current', {'endpoint': 'fixture'})
        self.denied(self.local, path, {'Content-Type': 'application/json'}, body)

    def test_reads_never_accept_a_foreign_account_or_device_parameter(self):
        collector = self.local.RequestHandlerClass.collector
        collector.account = 'current'
        collector.optimizer.live = {'account': 'current', 'device': 'this-mac'}
        collector.demand_alerts.snapshot = Mock(return_value={'models': [], 'alerts': []})
        for server, headers in ((self.local, {}), (self.phone, self.headers)):
            with self.read(
                server, '/api/demand-alerts?account=foreign&device=foreign', headers
            ) as response:
                self.assertEqual(json.load(response)['models'], [])
            self.assertEqual(
                collector.demand_alerts.snapshot.call_args.args[:2], ('current', 'this-mac')
            )
        collector.optimizer.live = {'account': 'old', 'device': 'old-mac'}
        with self.read(self.local, '/api/demand-alerts') as response:
            json.load(response)
        self.assertEqual(collector.demand_alerts.snapshot.call_args.args[:2], ('', ''))
        self.denied(self.phone, '/api/demand-alerts')

    def test_test_notifications_use_current_owner_and_existing_write_guards(self):
        collector = self.local.RequestHandlerClass.collector
        collector.account = 'current'
        collector.web_push = Mock()
        collector.web_push.test_notification.return_value = {'testQueued': True}
        path = '/api/demand-alerts/notifications'
        headers = {
            **self.headers,
            'Content-Type': 'application/json',
            'X-Bloom-Action': 'demand-alerts',
            'Origin': 'https://' + test_remote.HOST + ':8443',
        }
        body = json.dumps({'action': 'test', 'subscriptionId': 'registered-device'}).encode()
        for change in (
            {'Origin': 'null'},
            {'Tailscale-User-Login': 'foreign@example.com'},
            {'X-Bloom-Action': 'manual-model'},
            {'Sec-Fetch-Site': 'cross-site'},
        ):
            self.denied(self.phone, path, {**headers, **change}, body)
        for payload in (
            {'action': 'test'},
            {'action': 'test', 'subscriptionId': 'registered-device', 'account': 'foreign'},
            {'action': 'test', 'subscriptionId': 'registered-device', 'endpoint': 'arbitrary'},
        ):
            with self.assertRaises(urllib.error.HTTPError) as result:
                self.read(self.phone, path, headers, json.dumps(payload).encode())
            self.assertEqual(result.exception.code, 400)
        collector.web_push.test_notification.assert_not_called()
        with self.read(self.phone, path, headers, body) as response:
            self.assertTrue(json.load(response)['testQueued'])
        collector.web_push.test_notification.assert_called_once_with('current', 'registered-device')
        collector.web_push.test_notification.side_effect = ValueError(
            'Wait for the notification retry delay before sending another test.'
        )
        with self.assertRaises(urllib.error.HTTPError) as result:
            self.read(self.phone, path, headers, body)
        self.assertEqual(result.exception.code, 400)
        self.assertIn('retry delay', json.load(result.exception)['error'])

    def test_sender_contact_configuration_is_local_only_and_never_returned(self):
        collector = self.local.RequestHandlerClass.collector
        collector.account = 'current'
        collector.web_push = WebPush(self.root)
        path = '/api/demand-alerts/notifications'
        body = json.dumps(
            {'action': 'configure-contact', 'contact': 'operator@bloom-demo.com'}
        ).encode()
        headers = {
            **self.headers,
            'Content-Type': 'application/json',
            'X-Bloom-Action': 'demand-alerts',
            'Origin': 'https://' + test_remote.HOST + ':8443',
        }
        self.denied(self.phone, path, headers, body)
        self.assertIsNone(collector.web_push.state)
        local = {'Content-Type': 'application/json', 'X-Bloom-Action': 'demand-alerts'}
        with self.read(self.local, path, local, body) as response:
            result = json.load(response)
        self.assertTrue(result['contactConfigured'])
        self.assertNotIn('operator@', json.dumps(result))
        for server, read_headers in ((self.local, {}), (self.phone, self.headers)):
            with self.read(server, path, read_headers) as response:
                self.assertNotIn('operator@', response.read().decode())

    def test_background_scan_preserves_demand_events_without_phone_push(self):
        collector = self.local.RequestHandlerClass.collector
        collector.account = 'current'
        collector.optimizer.live = {
            'account': 'current',
            'device': 'this-mac',
            'provider': {'model': 'a', 'tracking': {'counting': True}},
        }
        collector.optimizer.raw = {'advertised_models': ['a', 'b']}
        collector.optimizer.discovery_at = time.time()
        collector.optimizer.candidates = Mock(
            return_value=[
                {'id': 'a', 'available': True},
                {'id': 'b', 'available': True},
                {'id': 'too-large', 'available': False},
            ]
        )
        collector.demand_alerts.scan = Mock(
            return_value={'alerts': [], 'newAlerts': [{'id': 12, 'model': 'b'}]}
        )
        collector.web_push.enqueue = Mock()
        with patch.object(collector.stop, 'wait', side_effect=lambda _: collector.stop.set()):
            collector.demand_loop()
        self.assertEqual(
            collector.demand_alerts.scan.call_args.args[:4],
            ('current', 'this-mac', ['a', 'b'], ['a', 'b']),
        )
        collector.web_push.enqueue.assert_not_called()

    def test_policy_update_uses_owner_origin_action_and_stale_control_guards(self):
        from test_demand_policy_update import PolicyUpdateTests

        fixture = PolicyUpdateTests()
        fixture.setUp()
        collector = self.local.RequestHandlerClass.collector
        original = collector.optimizer
        try:
            optimizer = fixture.o
            collector.optimizer = optimizer
            before = json.loads(json.dumps(optimizer.state))
            headers = {
                **self.headers,
                'Content-Type': 'application/json',
                'X-Bloom-Action': 'optimizer',
                'Origin': 'https://' + test_remote.HOST + ':8443',
            }
            body = json.dumps(fixture.request()).encode()
            for change in (
                {'Origin': 'null'},
                {'Origin': 'https://foreign.example'},
                {'Tailscale-User-Login': 'foreign@example.com'},
                {'X-Bloom-Action': 'manual-model'},
            ):
                self.denied(self.phone, '/api/optimizer', {**headers, **change}, body)
            self.denied(
                self.phone,
                '/api/optimizer',
                {k: v for k, v in headers.items() if k != 'Origin'},
                body,
            )
            self.denied(self.local, '/api/optimizer', {'Content-Type': 'application/json'}, body)
            self.assertEqual(optimizer.state, before)
            with self.read(self.phone, '/api/optimizer', headers, body) as response:
                json.load(response)
            self.assertEqual(optimizer.state['demandPolicy']['maxSwitchesPerDay'], 24)
            self.assertEqual(optimizer.state['startedAt'], before['startedAt'])
            self.assertEqual(optimizer.state['models'], before['models'])
            with self.assertRaises(urllib.error.HTTPError) as result:
                self.read(self.phone, '/api/optimizer', headers, body)
            self.assertEqual(result.exception.code, 400)
            local = {'Content-Type': 'application/json', 'X-Bloom-Action': 'optimizer'}
            with self.read(
                self.local,
                '/api/optimizer',
                local,
                json.dumps(fixture.request({'maxSwitchesPerDay': 12})).encode(),
            ) as response:
                json.load(response)
            self.assertEqual(optimizer.state['demandPolicy']['maxSwitchesPerDay'], 12)
            optimizer.runner.assert_not_called()
        finally:
            collector.optimizer = original
            fixture.tearDown()

    def test_demand_start_and_pause_use_real_controller_with_phone_guards(self):
        from test_optimizer import ControllerTests

        fixture = ControllerTests()
        fixture.setUp()
        collector = self.local.RequestHandlerClass.collector
        original = collector.optimizer
        try:
            optimizer = fixture.o
            collector.optimizer = optimizer
            optimizer.state['mode'] = 'observe'
            optimizer.snapshot.return_value.update(controlError=None, identityVerified=True)
            version = optimizer.control_version()
            data = {
                'action': 'start',
                'mode': 'demand',
                'models': ['a', 'b'],
                'demandPolicy': {'minRunMinutes': 30},
                'expectedControl': version,
            }
            headers = {
                **self.headers,
                'Content-Type': 'application/json',
                'X-Bloom-Action': 'optimizer',
                'Origin': 'https://' + test_remote.HOST + ':8443',
            }
            body = json.dumps(data).encode()
            for change in (
                {'Origin': 'null'},
                {'Tailscale-User-Login': 'foreign@example.com'},
                {'X-Bloom-Action': 'manual-model'},
            ):
                self.denied(self.phone, '/api/optimizer', {**headers, **change}, body)
            for change in ({'expectedControl': 'stale'}, {'demandPolicy': {'memoryHeadroomGB': 0}}):
                with self.assertRaises(urllib.error.HTTPError) as result:
                    self.read(
                        self.phone,
                        '/api/optimizer',
                        headers,
                        json.dumps({**data, **change}).encode(),
                    )
                self.assertEqual(result.exception.code, 400)
            with self.read(self.phone, '/api/optimizer', headers, body) as response:
                json.load(response)
            self.assertEqual(optimizer.state['mode'], 'demand')
            with self.assertRaises(urllib.error.HTTPError) as result:
                self.read(self.phone, '/api/optimizer', headers, body)
            self.assertEqual(result.exception.code, 400)
            pause = json.dumps(
                {'action': 'pause', 'expectedControl': optimizer.control_version()}
            ).encode()
            with self.read(self.phone, '/api/optimizer', headers, pause) as response:
                json.load(response)
            self.assertEqual(optimizer.state['mode'], 'observe')
            optimizer.runner.assert_not_called()
        finally:
            collector.optimizer = original
            fixture.tearDown()


if __name__ == '__main__':
    unittest.main()
