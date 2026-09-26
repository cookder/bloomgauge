import json
import unittest
import urllib.error
import test_remote


class OperatingHTTPTests(unittest.TestCase):
    setUp = test_remote.RemoteHTTPTests.setUp
    tearDown = test_remote.RemoteHTTPTests.tearDown
    read = test_remote.RemoteHTTPTests.read
    denied = test_remote.RemoteHTTPTests.denied

    def test_history_reads_and_input_validation_on_mac_and_private_phone(self):
        for server, headers in ((self.local, {}), (self.phone, self.headers)):
            for path in ('/api/energy', '/api/concurrency-history'):
                with self.read(server, path + '?from=0', headers) as response:
                    result = json.load(response)
                    self.assertEqual(result['samples'], [])
                    self.assertEqual(response.headers['Cache-Control'], 'no-store')
                for query in ('from=nan', 'to=inf', 'from=-1', 'from=20&to=10'):
                    with self.assertRaises(urllib.error.HTTPError) as e:
                        self.read(server, path + '?' + query, headers)
                    self.assertEqual(e.exception.code, 400)
                self.denied(self.phone, path)
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.read(self.local, '/api/concurrency-history?session=-1')
        self.assertEqual(e.exception.code, 400)

    def test_rate_edits_require_owner_exact_origin_action_and_current_version(self):
        path = '/api/energy/tariff'
        c = self.local.RequestHandlerClass.collector
        tariff = c.energy.tariff()
        headers = {
            **self.headers,
            'Content-Type': 'application/json',
            'X-Bloom-Action': 'energy-tariff',
            'Origin': 'https://' + test_remote.HOST + ':8443',
        }
        data = {'rate': 0.2, 'label': 'Fixture rate', 'expectedId': tariff['id']}
        body = json.dumps(data).encode()
        for change in (
            {'Origin': 'null'},
            {'Origin': 'https://elsewhere.example'},
            {'Tailscale-User-Login': 'other@example.com'},
            {'X-Bloom-Action': 'optimizer'},
            {'X-Forwarded-Proto': 'http'},
            {'Sec-Fetch-Site': 'cross-site'},
        ):
            self.denied(self.phone, path, {**headers, **change}, body)
        for key in ('Origin', 'X-Bloom-Action', 'Tailscale-User-Login'):
            self.denied(self.phone, path, {k: v for k, v in headers.items() if k != key}, body)
        for payload in (
            [],
            None,
            {**data, 'rate': True},
            {**data, 'extra': 'not allowed'},
            {**data, 'label': 'Bad\nlabel'},
        ):
            with self.assertRaises(urllib.error.HTTPError) as e:
                self.read(self.phone, path, headers, json.dumps(payload).encode())
            self.assertEqual(e.exception.code, 400)
        self.assertEqual(c.energy.tariff()['id'], tariff['id'])
        with self.read(self.phone, path, headers, body) as response:
            self.assertEqual(json.load(response)['rate'], 0.2)
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.read(self.phone, path, headers, body)
        self.assertEqual(e.exception.code, 400)
        local_headers = {'Content-Type': 'application/json', 'X-Bloom-Action': 'energy-tariff'}
        with self.read(
            self.local,
            path,
            local_headers,
            json.dumps({**data, 'rate': 0.3, 'expectedId': c.energy.tariff()['id']}).encode(),
        ) as response:
            self.assertEqual(json.load(response)['rate'], 0.3)


if __name__ == '__main__':
    unittest.main()
