"""Read-only traffic routes retain the existing local/private owner boundary."""

import json
import unittest
import urllib.error

import test_remote


class TrafficHTTPTests(unittest.TestCase):
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

    def test_current_session_metrics_on_local_and_authenticated_phone(self):
        collector = self.local.RequestHandlerClass.collector
        with collector.lock:
            collector.account = 'one'
            collector.snapshot['traffic'] = {'sessionId': 4}
            collector.traffic.save(
                'one', 4, 100, 103, 30, 1, {'60': {'tokensPerSecond': 10, 'requestsPerMinute': 20}}
            )
            collector.traffic.save(
                'other',
                4,
                100,
                103,
                999,
                99,
                {'60': {'tokensPerSecond': 333, 'requestsPerMinute': 1980}},
            )
        for server, headers in ((self.local, {}), (self.phone, self.headers)):
            for metric, expected in (('tokens', 10), ('requests', 20)):
                with self.read(
                    server, '/api/traffic-history?session=4&from=0&to=200&metric=' + metric, headers
                ) as response:
                    data = json.load(response)
                    self.assertEqual(data['samples'][0]['rate60'], expected)
                    self.assertNotIn('one', json.dumps(data))
                    self.assertEqual(data['sessionId'], 4)
            for query in (
                'session=5',
                'from=NaN',
                'to=Infinity',
                'from=-1',
                'from=200&to=100',
                'metric=invalid',
            ):
                with self.assertRaises(urllib.error.HTTPError) as result:
                    self.read(server, '/api/traffic-history?' + query, headers)
                self.assertEqual(result.exception.code, 400)

    def test_cross_model_history_on_both_routes_keeps_owner_and_stale_session_checks(self):
        c = self.local.RequestHandlerClass.collector
        with c.lock:
            c.account = 'one'
            c.snapshot['pulse'] = c.snapshot['traffic'] = {'sessionId': 4}
            for sid, scope, model in [
                (3, 'same', 'gemma'),
                (4, 'same', 'oss'),
                (5, 'other', 'foreign'),
            ]:
                c.history.db.execute(
                    'INSERT INTO provider_sessions VALUES(?,?,?)',
                    (sid, scope, json.dumps({'models': [model]})),
                )
                c.pulse.record_rate(
                    'one',
                    {
                        'at': 100 + sid * 10,
                        'sessionId': sid,
                        'status': 'live',
                        'windows': {'60': {'ratePerHour': 0.1}},
                    },
                )
                c.traffic.save(
                    'one',
                    sid,
                    100 + sid * 10,
                    103 + sid * 10,
                    windows={'60': {'tokensPerSecond': 10}},
                )
        for path in ('/api/pulse-history', '/api/traffic-history'):
            for server, headers in ((self.local, {}), (self.phone, self.headers)):
                with self.read(
                    server, path + '?scope=models&session=4&from=0&to=200', headers
                ) as response:
                    data = json.load(response)
                    self.assertEqual(data['scope'], 'models')
                    self.assertEqual([s['id'] for s in data['sessions']], [3, 4])
                    self.assertEqual({p['sessionId'] for p in data['samples']}, {3, 4})
                for query in ('scope=models&session=3', 'scope=invalid', 'scope=models&from=NaN'):
                    with self.assertRaises(urllib.error.HTTPError) as error:
                        self.read(server, path + '?' + query, headers)
                    self.assertEqual(error.exception.code, 400)
            self.denied(self.phone, path + '?scope=models')
            self.denied(
                self.phone,
                path + '?scope=models',
                {**self.headers, 'Origin': 'https://foreign.example'},
            )

    def test_missing_identity_and_foreign_origins_are_denied(self):
        self.denied(self.phone, '/api/traffic-history')
        self.denied(self.phone, '/api/traffic-history', {'Host': test_remote.HOST + ':8443'})
        self.denied(
            self.phone,
            '/api/traffic-history',
            {**self.headers, 'Tailscale-User-Login': 'other@example.com'},
        )
        self.denied(
            self.phone,
            '/api/traffic-history',
            {**self.headers, 'Origin': 'https://foreign.example'},
        )
        self.denied(self.local, '/api/traffic-history', {'X-Forwarded-Proto': 'https'})


if __name__ == '__main__':
    unittest.main()
