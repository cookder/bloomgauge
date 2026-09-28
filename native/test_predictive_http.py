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
        # Model research judges blocks against the user's goal, never a built-in $0.12.
        for saved, goal in ({}, None), ({'targetUsdPerHour': 0.12}, None), (
            {'targetUsdPerHour': 0.15},
            0.15,
        ):
            c.optimizer.state = {'demandPolicy': saved}
            with self.read(self.local, path) as r:
                json.load(r)
            self.assertEqual(c.predictive_lab.report.call_args.args[5], goal)
        self.denied(
            self.phone, path, {**self.headers, 'Tailscale-User-Login': 'foreign@example.com'}
        )


if __name__ == '__main__':
    unittest.main()
