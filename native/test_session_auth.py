"""Mac listener changes need the app window's per-launch session cookie (review item 21, part A)."""

import json
import pathlib
import unittest
import urllib.error
import urllib.request

import test_support_reports as base
from swift_harness import compile_swift

TOKEN = 'a' * 36 + '-' + 'b' * 36


class SessionCookieTests(unittest.TestCase):
    setUp = base.SupportHTTPTests.setUp
    tearDown = base.SupportHTTPTests.tearDown
    url = base.SupportHTTPTests.url

    def request(self, server, path, data=None, headers=None):
        request = urllib.request.Request(
            self.url(server, path),
            data=json.dumps(data).encode() if data is not None else None,
            headers=headers or {},
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error) if error.headers.get('Content-Type', '').startswith(
                'application/json'
            ) else None

    def enforce(self):
        self.local.RequestHandlerClass.session_token = TOKEN

    def test_without_a_configured_token_nothing_changes(self):
        self.assertEqual(
            self.request(self.local, '/api/support/auto', {'autoSend': False}, self.headers)[0], 200
        )

    def test_changes_need_the_cookie_reads_do_not(self):
        self.enforce()
        code, body = self.request(self.local, '/api/support/auto', {'autoSend': True}, self.headers)
        self.assertEqual((code, body['status']), (403, 'session'))
        for cookie in (
            'bloom_session=wrong',
            'other=' + TOKEN,
            'bloom_session="' + TOKEN[:-1] + '"',
        ):
            self.assertEqual(
                self.request(
                    self.local,
                    '/api/support/auto',
                    {'autoSend': True},
                    {**self.headers, 'Cookie': cookie},
                )[0],
                403,
            )
        self.assertEqual(
            self.request(
                self.local,
                '/api/support/auto',
                {'autoSend': True},
                {**self.headers, 'Cookie': 'x=1; bloom_session=' + TOKEN},
            )[0],
            200,
        )
        self.assertEqual(self.request(self.local, '/api/support/auto')[0], 200)  # reads stay open
        self.assertEqual(
            self.request(self.local, '/api/contact')[0], 403
        )  # except personal details
        self.assertEqual(
            self.request(self.local, '/api/contact', headers={'Cookie': 'bloom_session=' + TOKEN})[
                0
            ],
            200,
        )

    def test_phone_and_native_routes_keep_their_own_checks(self):
        self.enforce()
        self.assertEqual(
            self.request(self.phone, '/api/support/auto', {'autoSend': False}, self.phone_headers)[
                0
            ],
            200,
        )
        # Native routes still need X-Bloom-Native, not the cookie.
        self.assertEqual(
            self.request(
                self.local, '/api/update/native', {}, {'Content-Type': 'application/json'}
            )[0],
            403,
        )
        self.assertNotEqual(
            self.request(
                self.local, '/api/update/native', {}, {'Content-Type': 'application/json'}
            )[1],
            {'status': 'session', 'error': 'Open BloomGauge on this Mac to change settings.'},
        )


class SessionCookiePolicyTests(unittest.TestCase):
    def test_cookie_is_http_only_strict_and_bound_to_the_local_dashboard(self):
        source = (pathlib.Path(__file__).parent / 'App.swift').read_text()
        policy = source.split(
            '// BEGIN session cookie policy (compiled directly by its regression test).'
        )[1].split('// END session cookie policy.')[0]
        harness = """
let token = String(repeating: "a", count: 36) + "-" + String(repeating: "b", count: 36)
let cookie = bloomSessionCookie(url: URL(string: "http://127.0.0.1:8765/")!, token: token)!
precondition(cookie.name == "bloom_session" && cookie.value == token && cookie.domain == "127.0.0.1" && cookie.path == "/")
precondition(cookie.isHTTPOnly && cookie.sameSitePolicy == .sameSiteStrict && cookie.isSessionOnly)
for url in ["https://127.0.0.1:8765/", "http://localhost:8765/", "http://127.0.0.1/", "http://example.com:8765/"] {
    precondition(bloomSessionCookie(url: URL(string: url)!, token: token) == nil)
}
for bad in ["short", token + ";x=1", token + " "] { precondition(bloomSessionCookie(url: URL(string: "http://127.0.0.1:8765/")!, token: bad) == nil) }
print("Session cookie policy passed")
"""
        import subprocess

        result = subprocess.run(
            [str(compile_swift('import Foundation\n' + policy + harness))],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
