"""Optional contact details: sent only with consent, confirmed before saving, removable."""

import json
import unittest
import urllib.error
import urllib.request
from unittest.mock import Mock, patch

import test_support_reports as base
import user_contact as contact


class FakeHistory:
    def __init__(self):
        self.data = {}

    def cache(self, key, data=None):
        if data is not None:
            self.data[key] = json.loads(json.dumps(data))
            return data
        return self.data.get(key)


class UserContactTests(unittest.TestCase):
    def setUp(self):
        self.history = FakeHistory()
        self.calls = []
        self.code = {'POST': 200, 'DELETE': 204}
        self.user = contact.UserContact(
            self.history,
            transport=self.transport,
            now=lambda: 1790000000,
            app_version=lambda: '1.36.35',
        )

    def transport(self, method, url, body, headers, timeout):
        self.calls.append((method, url, json.loads(body), headers))
        code = self.code[method]
        if isinstance(code, Exception):
            raise code
        return code

    def save(self, value='andrew@example.com', consent=True):
        return self.user.action({'action': 'save', 'contact': value, 'consent': consent})

    def test_nothing_is_saved_or_sent_by_default(self):
        self.assertEqual(
            self.user.status(),
            {'schema': 1, 'contact': None, 'kind': None, 'savedAt': None, 'canChange': True},
        )
        self.assertEqual(self.calls, [])

    def test_save_sends_once_with_a_per_install_secret_then_updates_the_same_entry(self):
        self.assertEqual(self.save(' andrew@example.com ')['contact'], 'andrew@example.com')
        method, url, body, headers = self.calls[0]
        self.assertEqual((method, url), ('POST', contact.ENDPOINT))
        self.assertEqual(set(body), {'schema', 'id', 'contact', 'kind', 'appVersion'})
        self.assertEqual((body['kind'], body['appVersion']), ('email', '1.36.35'))
        secret = headers['Authorization'].removeprefix('Bearer ')
        self.assertRegex(secret, r'^[0-9a-f]{64}$')
        self.assertNotIn(secret, json.dumps(self.user.status()))
        self.assertEqual(self.save('@andrew s')['kind'], 'slack')
        self.assertEqual(self.calls[1][2]['id'], body['id'])
        self.assertEqual(self.calls[1][3]['Authorization'], headers['Authorization'])

    def test_requires_consent_and_a_recognisable_contact(self):
        for value, consent in (
            ('andrew@example.com', False),
            ('andrew@example.com', 'yes'),
            ('andrew', True),
            ('@', True),
            ('a@b', True),
            ('x' * 250 + '@example.com', True),
            ('@bad\nname', True),
            (5, True),
        ):
            with self.subTest(value=value, consent=consent), self.assertRaises(ValueError):
                self.save(value, consent)
        with self.assertRaises(ValueError):
            self.user.action(
                {'action': 'save', 'contact': 'a@example.com', 'consent': True, 'extra': 1}
            )
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.user.status()['contact'])

    def test_unconfirmed_save_keeps_nothing(self):
        for failure in (503, 409, OSError('offline')):
            self.code['POST'] = failure
            with self.subTest(failure=failure), self.assertRaises(contact.ContactError):
                self.save()
            self.assertIsNone(self.user.status()['contact'])

    def test_remove_deletes_the_website_copy_before_forgetting(self):
        self.save()
        ident = self.calls[0][2]['id']
        self.code['DELETE'] = 503
        with self.assertRaises(contact.ContactError):
            self.user.action({'action': 'remove'})
        self.assertEqual(self.user.status()['contact'], 'andrew@example.com')
        self.code['DELETE'] = 204
        self.assertIsNone(self.user.action({'action': 'remove'})['contact'])
        self.assertEqual(
            self.calls[-1][0:3], ('DELETE', contact.ENDPOINT, {'schema': 1, 'id': ident})
        )
        self.user.action({'action': 'remove'})
        self.assertEqual(len(self.calls), 3)

    def test_phone_and_offline_copies_cannot_change_it(self):
        with self.assertRaises(contact.ContactError) as result:
            self.user.action(
                {'action': 'save', 'contact': 'a@example.com', 'consent': True}, remote=True
            )
        self.assertEqual(result.exception.status, 'phone')
        self.assertFalse(self.user.status(remote=True)['canChange'])
        self.user.network_enabled = False
        with self.assertRaises(contact.ContactError):
            self.save()
        self.assertEqual(self.calls, [])

    def test_transport_identifies_bloom_and_never_follows_redirects(self):
        response = Mock(status=200)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        with patch.object(contact.urllib.request, 'build_opener', return_value=opener) as build:
            self.assertEqual(
                contact._transport(
                    'POST', contact.ENDPOINT, b'{}', {'Authorization': 'Bearer x'}, 8
                ),
                200,
            )
        request = opener.open.call_args.args[0]
        self.assertTrue(request.get_header('User-agent').startswith('Bloom'))
        self.assertEqual(build.call_args.args[0].proxies, {})
        self.assertIsInstance(build.call_args.args[1], contact._NoRedirect)
        response.read.assert_not_called()
        with self.assertRaises(ValueError):
            contact._transport('POST', 'https://example.com/', b'{}', {}, 8)


class ContactHTTPTests(unittest.TestCase):
    setUp = base.SupportHTTPTests.setUp
    tearDown = base.SupportHTTPTests.tearDown
    url = base.SupportHTTPTests.url

    def request(self, data=None, server=None, headers=None):
        server = server or self.local
        url = self.url(server, '/api/contact')
        if data is None:
            request = urllib.request.Request(url, headers=headers or {})
        else:
            request = urllib.request.Request(
                url,
                data=json.dumps(data).encode(),
                headers=headers
                if headers is not None
                else {'Content-Type': 'application/json', 'X-Bloom-Action': 'contact'},
            )
        with urllib.request.urlopen(request, timeout=3) as response:
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            return json.load(response)

    def test_status_save_and_remove_over_http(self):
        sent = []
        self.collector.contact.network_enabled = True
        self.collector.contact.transport = lambda method, url, body, headers, timeout: (
            sent.append(method) or (200 if method == 'POST' else 204)
        )
        self.assertIsNone(self.request()['contact'])
        self.assertEqual(
            self.request({'action': 'save', 'contact': '@copyfax', 'consent': True})['contact'],
            '@copyfax',
        )
        self.assertEqual(self.request()['kind'], 'slack')
        self.assertIsNone(self.request({'action': 'remove'})['contact'])
        self.assertEqual(sent, ['POST', 'DELETE'])

    def test_rejects_missing_action_header_phone_and_bad_input(self):
        self.collector.contact.network_enabled = True
        self.collector.contact.transport = Mock(return_value=200)
        with self.assertRaises(urllib.error.HTTPError) as result:
            self.request({'action': 'remove'}, headers={'Content-Type': 'application/json'})
        self.assertEqual(result.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as result:
            self.request(
                {'action': 'save', 'contact': 'a@example.com', 'consent': True},
                self.phone,
                {**self.phone_headers, 'X-Bloom-Action': 'contact'},
            )
        self.assertEqual(result.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as result:
            self.request({'action': 'save', 'contact': 'nope', 'consent': True})
        self.assertEqual(result.exception.code, 400)
        self.assertIn('Slack handle', json.load(result.exception)['error'])
        self.collector.contact.transport.assert_not_called()


if __name__ == '__main__':
    unittest.main()
