import json
import unittest
from unittest.mock import Mock
import test_remote


class PredictiveHTTPTests(unittest.TestCase):
    setUp = test_remote.RemoteHTTPTests.setUp
    tearDown = test_remote.RemoteHTTPTests.tearDown
    read = test_remote.RemoteHTTPTests.read
    denied = test_remote.RemoteHTTPTests.denied

    def test_report_uses_current_identity_and_private_read_guards(self):
        c = self.local.RequestHandlerClass.collector
        c.account = 'current'
        c.optimizer.live = {'account': 'current', 'device': 'this-mac'}
        c.predictive_lab.report = Mock(return_value={'enabled': True})
        path = '/api/predictive-lab?from=1&to=2&account=foreign&device=foreign'
        for server, headers in ((self.local, {}), (self.phone, self.headers)):
            with self.read(server, path, headers) as r:
                self.assertTrue(json.load(r)['enabled'])
            self.assertEqual(c.predictive_lab.report.call_args.args[:2], ('current', 'this-mac'))
        self.denied(self.phone, path)
        self.denied(
            self.phone, path, {**self.headers, 'Tailscale-User-Login': 'foreign@example.com'}
        )


if __name__ == '__main__':
    unittest.main()
