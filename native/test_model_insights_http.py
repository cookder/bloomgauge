"""Independent private analytics must not interfere with the model controls."""

import json
import threading
import time
import unittest
import urllib.error
from unittest.mock import Mock, patch
import model_insights
import test_remote


class ModelInsightsHTTPTests(unittest.TestCase):
    read = test_remote.RemoteHTTPTests.read
    denied = test_remote.RemoteHTTPTests.denied

    def setUp(self):
        test_remote.RemoteHTTPTests.setUp(self)
        self.c = self.local.RequestHandlerClass.collector
        self.c.account = 'account'
        self.c.optimizer.live = {'account': 'account', 'device': 'mac'}
        self.path = '/api/model-insights?timezone=America%2FChicago&model=a&model=never-observed'

    def tearDown(self):
        for server in (self.local, self.phone):
            server.shutdown()
            server.server_close()
        self.c.close()
        self.c.history.close()
        self.tmp.cleanup()

    def test_owner_local_and_phone_read_the_same_bounded_contract(self):
        for server, headers in ((self.local, {}), (self.phone, self.headers)):
            with self.read(server, self.path, headers) as response:
                value = json.load(response)
            self.assertEqual(value['schemaVersion'], 1)
            self.assertEqual(value['timezone'], 'America/Chicago')
            self.assertEqual(value['horizon']['seconds'], 28800)
            self.assertEqual([r['id'] for r in value['models']], ['a', 'never-observed'])
            for row in value['models']:
                self.assertEqual(row['demandNext8h']['scope'], 'whole_network_model')
                self.assertIsNone(row['observed']['usdPerWarmHour'])
                self.assertIsNone(row['incomeNext8h']['usdPerWarmHour'])
            self.assertNotIn('account', value)
            self.assertNotIn('device', value)
        self.denied(self.phone, self.path)
        self.denied(
            self.phone, self.path, {**self.headers, 'Tailscale-User-Login': 'foreign@example.com'}
        )
        self.denied(self.phone, self.path, {**self.headers, 'Origin': 'https://foreign.example'})

    def test_bad_queries_and_post_cannot_change_any_settings(self):
        state = json.dumps(self.c.optimizer.state, sort_keys=True)
        for query in (
            'timezone=UTC&model=a&account=foreign',
            'timezone=UTC&model=a&model=a',
            'timezone=bad&model=a',
            'model=a',
            'timezone=UTC&model=a&hours=9',
        ):
            with self.assertRaises(urllib.error.HTTPError) as failure:
                self.read(self.local, '/api/model-insights?' + query)
            self.assertEqual(failure.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError):
            self.read(
                self.local, self.path, {'Content-Type': 'application/json'}, b'{"action":"switch"}'
            )
        self.assertEqual(json.dumps(self.c.optimizer.state, sort_keys=True), state)

    def test_slow_insights_leave_cached_and_manual_controls_responsive(self):
        entered = threading.Event()
        release = threading.Event()
        errors = []
        original = model_insights.report
        self.c.optimizer.manual_snapshot = Mock(return_value={'models': [], 'controlError': None})

        def slow(*args):
            entered.set()
            release.wait(3)
            return original(*args)

        def request():
            try:
                with self.read(self.local, self.path) as response:
                    json.load(response)
            except Exception as error:
                errors.append(error)

        with patch.object(model_insights, 'report', side_effect=slow):
            thread = threading.Thread(target=request)
            thread.start()
            self.assertTrue(entered.wait(1))
            try:
                started = time.monotonic()
                for path in ('/api/model-control', '/api/optimizer/control'):
                    with self.read(self.local, path) as response:
                        json.load(response)
                self.assertLess(time.monotonic() - started, 0.5)
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    self.read(self.local, self.path)
                self.assertEqual(failure.exception.code, 503)
            finally:
                release.set()
                thread.join(4)
        self.assertFalse(errors)

    def test_failed_statistics_do_not_disable_controls_or_disclose_details(self):
        self.c.optimizer.manual_snapshot = Mock(return_value={'models': [], 'controlError': None})
        with patch.object(
            self.c.model_insights, 'get', side_effect=RuntimeError('private-data-must-not-escape')
        ):
            with self.assertRaises(urllib.error.HTTPError) as failure:
                self.read(self.local, self.path)
            self.assertEqual(failure.exception.code, 503)
            self.assertNotIn('private-data', failure.exception.read().decode())
        with self.read(self.local, '/api/model-control') as response:
            self.assertIsNone(json.load(response)['controlError'])


if __name__ == '__main__':
    unittest.main()
