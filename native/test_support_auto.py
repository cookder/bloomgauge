"""Opt-in automatic problem reports: off by default, explicit, same access checks."""

import json
import unittest
import urllib.error
import urllib.request

import test_support_reports as base


class AutoSendTests(unittest.TestCase):
    setUp = base.SupportHTTPTests.setUp
    tearDown = base.SupportHTTPTests.tearDown
    post = base.SupportHTTPTests.post
    url = base.SupportHTTPTests.url

    def get(self, server=None):
        server = server or self.local
        with urllib.request.urlopen(self.url(server, '/api/support/auto'), timeout=3) as response:
            return json.load(response)

    def test_off_by_default_then_persists_an_explicit_choice(self):
        self.assertEqual(self.get(), {'autoSend': False})
        self.assertEqual(self.post('auto', {'autoSend': True}), {'autoSend': True})
        self.assertEqual(self.get(), {'autoSend': True})
        self.assertIs(self.collector.history.cache('support-auto-send-v1')['enabled'], True)
        self.assertEqual(self.post('auto', {'autoSend': False}), {'autoSend': False})
        self.assertEqual(self.calls, [])

    def test_phone_can_turn_it_off_but_only_the_mac_can_opt_in(self):
        with self.assertRaises(urllib.error.HTTPError) as result:
            self.post('auto', {'autoSend': True}, self.phone, self.phone_headers)
        self.assertEqual(
            (result.exception.code, json.load(result.exception)['status']), (403, 'mac_only')
        )
        self.assertEqual(self.get(), {'autoSend': False})
        self.post('auto', {'autoSend': True})
        self.assertEqual(
            self.post('auto', {'autoSend': False}, self.phone, self.phone_headers),
            {'autoSend': False},
        )

    def test_rejects_other_values_foreign_origins_and_missing_action(self):
        for value in ({'autoSend': 1}, {'autoSend': 'yes'}, {}, {'autoSend': True, 'extra': 1}):
            with self.subTest(value=value), self.assertRaises(urllib.error.HTTPError) as result:
                self.post('auto', value)
            self.assertEqual(result.exception.code, 400)
        for server, headers in (
            (self.local, {}),
            (self.local, {**self.headers, 'Origin': 'https://evil.invalid'}),
            (self.phone, {k: v for k, v in self.phone_headers.items() if k != 'Origin'}),
        ):
            with self.assertRaises(urllib.error.HTTPError) as result:
                self.post('auto', {'autoSend': False}, server, headers)
            self.assertEqual(result.exception.code, 403)
        self.assertEqual(self.get(), {'autoSend': False})

    def test_disabled_network_reports_off_even_if_saved_on(self):
        self.collector.history.cache('support-auto-send-v1', {'enabled': True, 'changedAt': 1})
        self.collector.support_reports.network_enabled = False
        self.assertEqual(self.get(), {'autoSend': False})


if __name__ == '__main__':
    unittest.main()
