"""Guarded local integration checks; no hosted traffic or real provider actions."""

import copy
import json
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from collector import Collector
import test_remote


class FakeReporter:
    def __init__(self):
        self.enabled = False
        self.pending = False
        self.last_sent = None
        self.error = None
        self.calls = []

    def status(self):
        # Sensitive implementation fields must never escape the adapter.
        return {
            'enabled': self.enabled,
            'deletionPending': self.pending,
            'busy': False,
            'lastSentAt': self.last_sent,
            'error': self.error,
            'analyticsId': 'private-id',
            'secret': 'private-secret',
        }

    def update_metadata(self, data):
        self.calls.append(('metadata', data))

    def observe(self, **data):
        self.calls.append(('observe', data))

    def tick(self):
        self.calls.append(('tick', None))

    def record(self, flag, **options):
        if self.enabled:
            self.calls.append(('record', flag))

    def set_consent(self, enabled):
        self.enabled = enabled
        if not enabled:
            self.pending = True

    def retry_delete(self):
        self.calls.append(('retry', None))

    def close(self):
        pass


class UsageHTTPTests(unittest.TestCase):
    read = test_remote.RemoteHTTPTests.read
    denied = test_remote.RemoteHTTPTests.denied

    def setUp(self):
        test_remote.RemoteHTTPTests.setUp(self)
        self.c = self.local.RequestHandlerClass.collector
        self.c.usage.close()
        self.reporter = FakeReporter()
        self.c.usage.reporter = self.reporter
        self.write_headers = {'Content-Type': 'application/json', 'X-Bloom-Action': 'usage'}

    def tearDown(self):
        self.c.close()
        test_remote.RemoteHTTPTests.tearDown(self)
        self.c.history.close()

    def post(self, data, server=None, headers=None):
        with self.read(
            server or self.local,
            '/api/usage',
            headers or self.write_headers,
            json.dumps(data).encode(),
        ) as response:
            return json.load(response)

    def test_status_is_allowlisted_and_never_exposes_identifiers(self):
        for server, headers, remote in ((self.local, {}, False), (self.phone, self.headers, True)):
            with self.read(server, '/api/usage', headers) as response:
                data = json.load(response)
            self.assertEqual(
                set(data),
                {
                    'schema',
                    'appVersion',
                    'localOnly',
                    'enabled',
                    'deletionPending',
                    'sending',
                    'consentSaved',
                    'lastSentAt',
                    'lastError',
                    'invitationEligible',
                    'invitationOffered',
                },
            )
            self.assertEqual(data['localOnly'], remote)
            self.assertFalse(data['enabled'])
            self.assertNotIn('private', json.dumps(data))
        self.denied(self.phone, '/api/usage')

    def test_success_timestamp_and_unsaved_optout_remain_explicit(self):
        self.reporter.last_sent = 1789830000.5
        with self.read(self.local, '/api/usage') as response:
            data = json.load(response)
        self.assertEqual(data['lastSentAt'], 1789830000.5)
        self.assertTrue(data['consentSaved'])
        self.reporter.pending = True
        self.reporter.error = 'opt_out_not_saved'
        with self.read(self.local, '/api/usage') as response:
            data = json.load(response)
        self.assertFalse(data['enabled'])
        self.assertFalse(data['consentSaved'])
        self.assertTrue(data['deletionPending'])
        self.assertIn('Keep Bloomkeeper open and retry before quitting', data['lastError'])

    def test_consent_and_deletion_are_mac_only_with_action_and_origin_guards(self):
        body = json.dumps({'action': 'consent', 'enabled': True}).encode()
        self.denied(self.local, '/api/usage', {'Content-Type': 'application/json'}, body)
        self.denied(
            self.local,
            '/api/usage',
            {**self.write_headers, 'Origin': 'https://foreign.example'},
            body,
        )
        phone = {**self.headers, **self.write_headers, 'Origin': 'https://' + self.headers['Host']}
        for data in (
            {'action': 'consent', 'enabled': True},
            {'action': 'consent', 'enabled': False},
            {'action': 'retry-delete'},
            {'action': 'offer-invitation'},
            {'action': 'dismiss-invitation'},
        ):
            self.denied(self.phone, '/api/usage', phone, json.dumps(data).encode())
        self.assertFalse(self.reporter.enabled)
        self.assertTrue(self.post({'action': 'consent', 'enabled': True})['enabled'])
        result = self.post({'action': 'consent', 'enabled': False})
        self.assertFalse(result['enabled'])
        self.assertTrue(result['deletionPending'])

    def test_rejects_extra_payload_fields_and_nonboolean_consent(self):
        for data in (
            {'action': 'consent', 'enabled': 1},
            {'action': 'consent', 'enabled': True, 'installationId': 'private'},
            {'action': 'dashboard-opened', 'url': 'https://private.example'},
            [],
            {'action': 'unknown'},
            {'action': 'trial-requested'},
        ):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.post(data)
            self.assertEqual(error.exception.code, 400)
        self.assertFalse(self.reporter.enabled)

    def test_only_explicit_dashboard_event_after_setup_counts_phone_use(self):
        self.reporter.enabled = True
        phone = {**self.headers, **self.write_headers, 'Origin': 'https://' + self.headers['Host']}
        self.post({'action': 'dashboard-opened'}, self.phone, phone)
        self.assertFalse(any(kind == 'record' for kind, _ in self.reporter.calls))
        self.c.setup.complete({'action': 'complete', 'understood': True})
        self.c.usage.observe()
        self.assertFalse(any(kind == 'record' for kind, _ in self.reporter.calls))
        self.denied(
            self.phone,
            '/api/usage',
            {**self.headers, **self.write_headers},
            b'{"action":"dashboard-opened"}',
        )
        self.post({'action': 'dashboard-opened'}, self.phone, phone)
        self.assertEqual(
            [value for kind, value in self.reporter.calls if kind == 'record'],
            ['dashboardOpened', 'phoneUsed'],
        )

    def test_telemetry_actions_never_change_optimizer(self):
        before = copy.deepcopy(self.c.optimizer.state)
        self.post({'action': 'consent', 'enabled': True})
        self.post({'action': 'consent', 'enabled': False})
        self.post({'action': 'retry-delete'})
        self.assertEqual(self.c.optimizer.state, before)

    def test_background_observation_sanitizes_hardware_and_does_not_count_open(self):
        self.reporter.enabled = True
        self.c.hardware = {'chip': 'Apple M5 Pro', 'memoryTotalGB': 48, 'serial': 'private-serial'}
        self.c.earnings_error('private account and raw upstream detail')
        self.c.optimizer.state['mode'] = 'demand'
        self.c.usage.observe()
        metadata = next(data for kind, data in self.reporter.calls if kind == 'metadata')
        self.assertEqual(set(metadata), {'osMajor', 'chipFamily', 'memoryBand'})
        self.assertEqual(metadata['memoryBand'], '33-64')
        snapshot = next(data for kind, data in self.reporter.calls if kind == 'observe')
        self.assertEqual(
            snapshot,
            {'setup_completed': False, 'setup_error': 'connection', 'optimizer_used': True},
        )
        self.assertFalse(any(kind == 'record' for kind, _ in self.reporter.calls))

    def test_coarse_connection_error_covers_missing_and_stale_readings(self):
        self.reporter.enabled = True
        for prior_at, expected_status in ((None, 'missing'), (100, 'stale')):
            self.reporter.calls.clear()
            self.c.earnings['updatedAt'] = prior_at
            self.c.earnings_error('private upstream failure details')
            self.assertEqual(self.c.earnings['status'], expected_status)
            self.c.usage.observe()
            snapshot = next(data for kind, data in self.reporter.calls if kind == 'observe')
            self.assertEqual(snapshot['setup_error'], 'connection')
            self.assertNotIn('private', json.dumps(snapshot))


class UsageFailureIsolationTests(unittest.TestCase):
    def test_reporting_initialization_failure_does_not_break_setup(self):
        with (
            tempfile.TemporaryDirectory() as home,
            patch('usage_integration.UsageReporter', side_effect=OSError('private path')),
        ):
            c = Collector(home=home)
            self.assertEqual(c.optimizer.state['mode'], 'observe')
            self.assertFalse(c.usage.status()['enabled'])
            self.assertNotIn('private path', json.dumps(c.usage.status()))
            c.setup.complete({'action': 'complete', 'understood': True})
            c.usage.observe()
            c.close()
            c.history.close()

    def test_preview_explicitly_disables_hosted_transport(self):
        with (
            tempfile.TemporaryDirectory() as home,
            patch('usage_integration.UsageReporter') as factory,
        ):
            c = Collector(home=home, usage_network_enabled=False)
            self.assertIs(factory.call_args.kwargs['network_enabled'], False)
            self.assertIsNone(factory.call_args.args[0])
            c.close()
            c.history.close()


if __name__ == '__main__':
    unittest.main()
