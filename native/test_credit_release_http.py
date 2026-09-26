"""Credits and bundled notes stay within existing local/owner phone read access."""

import json
import unittest
import urllib.error
import test_remote
import test_credits_query as fixtures


class CreditReleaseHTTPTests(unittest.TestCase):
    read = test_remote.RemoteHTTPTests.read
    denied = test_remote.RemoteHTTPTests.denied

    def setUp(self):
        test_remote.RemoteHTTPTests.setUp(self)
        self.c = self.local.RequestHandlerClass.collector
        self.c.account = 'one'
        source = fixtures.CreditQueryTests()
        source.setUp()
        try:
            self.c.history.db.executemany('INSERT INTO credits VALUES(?,?,?,?,?,?)', source.rows)
            self.c.history.db.commit()
        finally:
            source.tearDown()

    def tearDown(self):
        for server in (self.local, self.phone):
            server.shutdown()
            server.server_close()
        self.c.close()
        self.c.history.close()
        self.tmp.cleanup()

    def test_sort_filter_stats_and_options_are_full_range_on_mac_and_phone(self):
        path = (
            '/api/credits?from=99&to=400&sort=amount-desc&category=inference&model=a&limit=1&page=2'
        )
        for server, headers in ((self.local, {}), (self.phone, self.headers)):
            with self.read(server, path, headers) as response:
                data = json.load(response)
            self.assertEqual(data['entries'][0]['id'], 1)
            self.assertEqual(data['count'], 4)
            self.assertEqual(data['summary']['totalUsd'], 2.5)
            self.assertEqual(data['models'], ['a', 'b', 'base_reward'])
            self.assertEqual(data['models'], data['modelOptions'])
            self.assertEqual(data['scope'], 'account')
            self.assertNotIn('private-other-model', str(data))
        self.denied(
            self.phone, path, {**self.headers, 'Tailscale-User-Login': 'foreign@example.com'}
        )

    def test_unknown_or_repeated_filters_are_rejected_and_existing_bounds_remain(self):
        for query in (
            'sort=anything',
            'category=cpu',
            'account=other',
            'model=a&model=b',
            'model=' + 'a' * 513,
        ):
            with self.subTest(query=query), self.assertRaises(urllib.error.HTTPError) as failure:
                self.read(self.local, '/api/credits?from=99&to=400&' + query)
            self.assertEqual(failure.exception.code, 400)
        with self.read(self.local, '/api/credits?from=99&to=400&page=-1&limit=999') as response:
            data = json.load(response)
        self.assertEqual((data['page'], data['limit']), (1, 250))

    def test_release_notes_source_run_never_claims_beta_is_installed(self):
        for server, headers in ((self.local, {}), (self.phone, self.headers)):
            with self.read(server, '/api/release-notes', headers) as response:
                data = json.load(response)
            self.assertEqual(data, {'installedVersion': 'development', 'release': None})
        self.denied(
            self.phone,
            '/api/release-notes',
            {**self.headers, 'Tailscale-User-Login': 'foreign@example.com'},
        )


if __name__ == '__main__':
    unittest.main()
