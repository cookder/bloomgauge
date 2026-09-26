import json
import unittest
import urllib.error
import test_remote


class WeeklyHTTPTests(unittest.TestCase):
    setUp = test_remote.RemoteHTTPTests.setUp
    tearDown = test_remote.RemoteHTTPTests.tearDown
    read = test_remote.RemoteHTTPTests.read
    denied = test_remote.RemoteHTTPTests.denied

    def test_read_routes_validate_ranges_and_preserve_phone_guards(self):
        path = '/api/network/weekly?from=1788757200&to=1788760800'
        for server, headers in ((self.local, {}), (self.phone, self.headers)):
            with self.read(server, path, headers) as response:
                d = json.load(response)
                self.assertEqual(len(d['cells']), 168)
                self.assertEqual(d['timezone'], 'UTC')
            for query in ('from=NaN', 'from=200&to=100', 'timezone=invalid', 'to=Infinity'):
                with self.assertRaises(urllib.error.HTTPError) as e:
                    self.read(server, '/api/network/weekly?' + query, headers)
                self.assertEqual(e.exception.code, 400)
        self.denied(self.phone, path)
        self.denied(
            self.phone, path, {**self.headers, 'Tailscale-User-Login': 'another@example.com'}
        )
        self.denied(self.phone, path, {**self.headers, 'Origin': 'https://foreign.example.com'})


if __name__ == '__main__':
    unittest.main()
