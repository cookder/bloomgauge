import json
import unittest
import urllib.error
import test_remote
from community_insights import CommunityInsights


class CommunityHTTPTests(unittest.TestCase):
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

    def test_private_reads_and_no_network_writes(self):
        collector = self.local.RequestHandlerClass.collector
        collector.community_insights = CommunityInsights(self.tmp.name)
        for server, headers in ((self.local, {}), (self.phone, self.headers)):
            with self.read(server, '/api/community-insights', headers) as response:
                self.assertEqual(json.load(response)['status'], 'waiting')
            with self.assertRaises(urllib.error.HTTPError) as result:
                self.read(
                    server,
                    '/api/community-insights',
                    {**headers, 'Content-Type': 'application/json'},
                    b'{}',
                )
            self.assertEqual(result.exception.code, 501)
        self.denied(self.phone, '/api/community-insights')
        for changes in (
            {'Origin': 'https://foreign.example'},
            {'Sec-Fetch-Site': 'cross-site'},
            {'Tailscale-User-Login': 'other@example.com'},
            {'X-Forwarded-Proto': 'http'},
        ):
            self.denied(self.phone, '/api/community-insights', {**self.headers, **changes})
        self.assertFalse(collector.community_insights.path.exists())


if __name__ == '__main__':
    unittest.main()
