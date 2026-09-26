"""Synthetic explicit-consent reporting tests; all outbound transport is mocked."""

import copy
import http.client
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import Mock, patch

import support_reports as reports
from collector import Collector, Handler, ThreadingHTTPServer
from remote import Remote


INPUT = {'category': 'manual', 'context': 'help', 'description': '', 'contact': ''}
SENSITIVE = 'private@example.invalid /Users/private https://private.ts.net/token=PRIVATE'


def captured():
    return {
        'app': {'version': '1.36.7', 'macOS': '26.6.2'},
        'hardware': {'chip': 'Apple M1 Ultra', 'memoryTotalGB': 128, 'private': SENSITIVE},
        'provider': {'online': True, 'version': '0.9.6', 'model': SENSITIVE},
        'optimizer': {
            'mode': 'demand',
            'status': 'warming',
            'recentEvents': [SENSITIVE],
            'lastSwitchFailure': {
                'code': 'readiness-timeout',
                'recoveryCode': 'idle-not-verified',
                'detail': SENSITIVE,
            },
        },
        'sources': {
            'earnings': {'status': 'error', 'errorCategory': 'authentication', 'raw': SENSITIVE},
            'monitor': {'status': 'ok', 'errorCategory': None},
            'network': {'status': 'stale'},
        },
        'earnings': {'usd': 45.879123},
        'account': SENSITIVE,
        'license': SENSITIVE,
        'rawLog': SENSITIVE,
    }


class SupportTests(unittest.TestCase):
    def setUp(self):
        self.now = 1790000000
        self.clock = 1000
        self.calls = []
        self.source = captured()
        self.capture = Mock(side_effect=lambda remote: copy.deepcopy(self.source))
        self.reporter = reports.SupportReports(
            None,
            transport=self.transport,
            now=lambda: self.now,
            monotonic=lambda: self.clock,
            capture=self.capture,
        )

    def transport(self, url, body, headers, timeout):
        self.calls.append((url, body, headers, timeout))
        return 200, {'status': 'sent', 'reportId': json.loads(body)['id']}

    def preview(self, data=None, remote=False):
        return self.reporter.preview(INPUT if data is None else data, remote)

    @staticmethod
    def consent(preview):
        return {
            'reportId': preview['report']['id'],
            'reviewToken': preview['reviewToken'],
            'confirmed': True,
        }

    def error(self, expected, callback):
        with self.assertRaises(reports.SupportError) as result:
            callback()
        self.assertEqual(result.exception.status, expected)
        self.assertNotIn(SENSITIVE, str(result.exception))
        return result.exception

    def test_initialization_and_preview_never_send_or_create_persistent_state(self):
        self.assertEqual(self.calls, [])
        self.assertEqual(self.reporter.previews, {})
        preview = self.preview()
        self.capture.assert_called_once_with(False)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.reporter.submissions, [])
        self.assertRegex(preview['report']['id'], r'^[0-9a-f]{32}$')
        self.assertRegex(preview['reviewToken'], r'^[0-9a-f]{64}$')

    def test_coarse_allowlist_excludes_earnings_identifiers_logs_and_urls(self):
        preview = self.preview()
        report = preview['report']
        self.assertEqual(
            set(report),
            {
                'schema',
                'id',
                'generatedAt',
                'appVersion',
                'surface',
                'category',
                'context',
                'description',
                'contact',
                'diagnostics',
            },
        )
        diag = report['diagnostics']
        self.assertEqual(
            set(diag),
            {
                'available',
                'osMajor',
                'chipFamily',
                'memoryBand',
                'providerOnline',
                'providerVersion',
                'optimizerMode',
                'optimizerStatus',
                'failureCode',
                'recoveryCode',
                'sources',
            },
        )
        self.assertEqual(
            (diag['chipFamily'], diag['osMajor'], diag['memoryBand']), ('M1 Ultra', 26, '65-128')
        )
        self.assertEqual(diag['failureCode'], 'readiness-timeout')
        self.assertEqual(
            diag['sources'][0], {'name': 'earnings', 'status': 'error', 'error': 'authentication'}
        )
        encoded = json.dumps(report)
        for secret in (
            SENSITIVE,
            '45.879123',
            'private.ts.net',
            'license',
            'rawLog',
            'recentEvents',
        ):
            self.assertNotIn(secret, encoded)
        self.assertEqual(report['contact'], '')
        self.assertEqual(report['description'], '')

    def test_malformed_diagnostic_fields_cannot_escape_projection(self):
        self.source = {
            key: {nested: SENSITIVE for nested in value} if isinstance(value, dict) else value
            for key, value in self.source.items()
        }
        diag = self.preview()['report']['diagnostics']
        self.assertNotIn(SENSITIVE, json.dumps(diag))
        self.assertEqual(diag['osMajor'], 0)
        self.assertEqual(diag['chipFamily'], 'Other')
        self.assertEqual(diag['memoryBand'], 'unknown')
        self.assertIsNone(diag['providerOnline'])
        self.assertEqual(diag['failureCode'], 'none')

    def test_switch_failures_older_than_a_day_are_not_reported(self):
        failure = self.source['optimizer']['lastSwitchFailure']
        failure['ageSeconds'] = 86000
        self.assertEqual(
            self.preview()['report']['diagnostics']['failureCode'], 'readiness-timeout'
        )
        failure['ageSeconds'] = 90000
        diag = self.preview()['report']['diagnostics']
        self.assertEqual((diag['failureCode'], diag['recoveryCode']), ('none', 'none'))

    def test_capture_failure_still_produces_reviewable_unknown_diagnostics(self):
        self.capture.side_effect = RuntimeError(SENSITIVE)
        result = self.preview()['report']
        self.assertFalse(result['diagnostics']['available'])
        self.assertIsNone(result['diagnostics']['providerOnline'])
        self.assertEqual(result['diagnostics']['failureCode'], 'unknown')
        self.assertNotIn(SENSITIVE, json.dumps(result))
        self.assertEqual(self.calls, [])

    def test_typed_diagnostics_ranges_and_future_chips(self):
        for raw, expected in (
            (16, 'up-to-16'),
            (24, '17-32'),
            (48, '33-64'),
            (128, '65-128'),
            (256, 'over-128'),
            (float('inf'), 'unknown'),
            (True, 'unknown'),
            (-1, 'unknown'),
        ):
            with self.subTest(memory=raw):
                self.assertEqual(
                    reports.summary({'hardware': {'memoryTotalGB': raw}})['memoryBand'], expected
                )
        self.assertEqual(
            reports.summary({'hardware': {'chip': 'Apple M12 Max'}})['chipFamily'], 'M12 Max'
        )
        self.assertEqual(reports.summary({'provider': {'online': 1}})['providerOnline'], None)

    def test_preview_inputs_exact_typed_and_bounded(self):
        bad = [
            None,
            [],
            {},
            {**INPUT, 'earnings': True},
            {**INPUT, 'diagnostics': captured()},
            {**INPUT, 'category': 'unknown'},
            {**INPUT, 'category': {}},
            {**INPUT, 'context': None},
            {**INPUT, 'description': False},
            {**INPUT, 'description': 'a' * 2001},
            {**INPUT, 'contact': 'a' * 255},
            {**INPUT, 'contact': 'x\ny'},
            {**INPUT, 'description': '\x00'},
            {**INPUT, 'description': '\ud800'},
        ]
        for value in bad:
            with self.subTest(value=str(value)[:70]), self.assertRaises((ValueError, UnicodeError)):
                self.reporter.preview(value)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.capture.call_count, 0)

    def test_only_deliberate_user_text_is_preserved_and_unicode_fits(self):
        preview = self.preview({**INPUT, 'description': '👩' * 2000, 'contact': SENSITIVE})
        self.assertEqual(preview['report']['description'], '👩' * 2000)
        self.assertEqual(preview['report']['contact'], SENSITIVE)
        self.reporter.send(self.consent(preview))
        self.assertLessEqual(len(self.calls[0][1]), reports.MAX_BYTES)

    def test_frozen_review_not_later_state_or_changed_preview_is_sent(self):
        preview = self.preview()
        expected = copy.deepcopy(preview['report'])
        self.source['optimizer']['status'] = 'ready'
        preview['report']['diagnostics']['failureCode'] = SENSITIVE
        preview['report']['description'] = 'edited without new preview'
        result = self.reporter.send(self.consent(preview))
        self.assertEqual(result, {'status': 'sent', 'reportId': expected['id']})
        url, body, headers, timeout = self.calls[0]
        self.assertEqual(json.loads(body), expected)
        self.assertEqual(url, reports.ENDPOINT)
        self.assertEqual(timeout, 15)
        self.assertNotEqual(headers['Authorization'], 'Bearer ' + preview['reviewToken'])
        self.assertEqual(self.capture.call_count, 1)

    def test_each_new_report_has_independent_identifier_and_secrets(self):
        first, second = self.preview(), self.preview()
        self.assertNotEqual(first['report']['id'], second['report']['id'])
        self.assertNotEqual(first['reviewToken'], second['reviewToken'])
        self.reporter.send(self.consent(first))
        self.reporter.send(self.consent(second))
        self.assertNotEqual(self.calls[0][2]['Authorization'], self.calls[1][2]['Authorization'])

    def test_send_requires_exact_frozen_review_token_and_true_consent(self):
        preview = self.preview()
        consent = self.consent(preview)
        for value in (
            None,
            {},
            {**consent, 'confirmed': 1},
            {**consent, 'confirmed': False},
            {**consent, 'report': preview['report']},
            {**consent, 'reportId': '../x'},
            {**consent, 'reviewToken': True},
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.reporter.send(value)
        self.error('expired', lambda: self.reporter.send({**consent, 'reviewToken': '0' * 64}))
        self.error('expired', lambda: self.reporter.send(consent, remote=True))
        self.assertEqual(self.calls, [])

    def test_sent_retries_are_idempotent_and_unsent_retries_reuse_bytes_and_secret(self):
        preview = self.preview()
        consent = self.consent(preview)

        def lost_ack(url, body, headers, timeout):
            self.transport(url, body, headers, timeout)
            raise TimeoutError(SENSITIVE)

        self.reporter.transport = lost_ack
        self.error('unconfirmed', lambda: self.reporter.send(consent))
        self.assertEqual(len(self.calls), 1)
        self.reporter.transport = self.transport
        result = self.reporter.send(consent)
        self.assertEqual(self.calls[0], self.calls[1])
        self.assertEqual(self.reporter.send(consent), result)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(len(self.reporter.submissions), 1)

    def test_only_matching_complete_ack_is_success(self):
        preview = self.preview()
        consent = self.consent(preview)
        for status, ack in (
            (202, {'status': 'sent', 'reportId': consent['reportId']}),
            (200, None),
            (200, {'status': 'sent', 'reportId': '0' * 32}),
            (200, {'status': 'sent', 'reportId': consent['reportId'], 'error': SENSITIVE}),
            (200, {'reportId': consent['reportId']}),
        ):
            self.reporter.transport = Mock(return_value=(status, ack))
            self.error('unconfirmed', lambda: self.reporter.send(consent))
        self.assertFalse(self.reporter.previews[consent['reportId']]['sent'])

    def test_review_expiry_does_not_extend_on_retry_or_wall_clock_change(self):
        consent = self.consent(self.preview())
        self.now -= 86400
        self.clock += reports.PREVIEW_SECONDS
        self.error('expired', lambda: self.reporter.send(consent))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.reporter.previews, {})

    def test_memory_bound_and_expired_previews_are_pruned(self):
        for _ in range(reports.MAX_PREVIEWS):
            self.preview()
        self.error('preview_limit', self.preview)
        self.assertEqual(len(self.reporter.previews), reports.MAX_PREVIEWS)
        self.clock += reports.PREVIEW_SECONDS
        self.preview()
        self.assertEqual(len(self.reporter.previews), 1)

    def test_five_new_reports_hourly_but_explicit_retry_same_report_allowed(self):
        self.reporter.transport = Mock(return_value=(503, None))
        previews = [self.preview() for _ in range(6)]
        for preview in previews[:5]:
            self.error('unconfirmed', lambda: self.reporter.send(self.consent(preview)))
        self.error('rate_limited', lambda: self.reporter.send(self.consent(previews[5])))
        self.error('unconfirmed', lambda: self.reporter.send(self.consent(previews[0])))
        self.clock += 3600
        self.error('unconfirmed', lambda: self.reporter.send(self.consent(self.preview())))
        self.assertEqual(len(self.reporter.submissions), 1)

    def automatic(self, category='connection', context='models'):
        data = {**INPUT, 'category': category, 'context': context, 'automatic': True}
        return self.reporter.send(self.consent(self.reporter.preview(data)))

    def test_automatic_reports_need_the_opt_in_and_carry_no_user_text(self):
        self.error('rate_limited', self.automatic)
        self.reporter.set_auto({'autoSend': True})
        for extra in ({'description': 'hi'}, {'contact': '@me'}, {'automatic': 1}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.reporter.preview({**INPUT, 'automatic': True, **extra})
        self.assertEqual(self.automatic()['status'], 'sent')
        self.assertEqual(len(self.calls), 1)

    def test_same_problem_sends_once_a_day_whatever_the_page_or_window(self):
        self.reporter.set_auto({'autoSend': True})
        self.automatic(context='models')
        # Another page, a phone view or a reloaded window: still the same problem.
        refused = self.error('rate_limited', lambda: self.automatic(context='overview'))
        self.assertEqual(refused.response['error'], reports.AUTO_LIMITED)
        self.now += reports.AUTO_SAME_CATEGORY_SECONDS
        self.error('rate_limited', lambda: self.automatic(context='earnings'))
        self.now += reports.AUTO_SAME_PROBLEM_SECONDS
        self.assertEqual(self.automatic()['status'], 'sent')
        self.assertEqual(len(self.calls), 2)
        # A manual report is never held back by automatic ones.
        self.assertEqual(self.reporter.send(self.consent(self.preview()))['status'], 'sent')

    def test_a_changed_problem_waits_six_hours_and_three_a_day_at_most(self):
        self.reporter.set_auto({'autoSend': True})
        self.automatic()
        self.source['optimizer']['lastSwitchFailure']['code'] = 'startup-timeout'
        self.error('rate_limited', self.automatic)
        self.automatic(category='model')
        self.now += reports.AUTO_SAME_CATEGORY_SECONDS
        self.automatic()
        self.source['provider']['online'] = False
        self.now += reports.AUTO_SAME_CATEGORY_SECONDS
        self.error('rate_limited', self.automatic)
        self.assertEqual(len(self.calls), 3)

    def test_a_failed_automatic_send_does_not_use_up_the_limit(self):
        self.reporter.set_auto({'autoSend': True})
        ok = self.transport
        self.reporter.transport = lambda *args: (self.calls.append(args), (503, None))[1]
        data = {**INPUT, 'category': 'connection', 'context': 'models', 'automatic': True}
        failed = self.reporter.preview(data)
        self.error('unconfirmed', lambda: self.reporter.send(self.consent(failed)))
        self.assertEqual(self.reporter._auto_sent(), [])
        # Delivery works again: the same problem is still sent, and only once.
        self.reporter.transport = ok
        self.assertEqual(self.reporter.send(self.consent(failed))['status'], 'sent')
        self.error('rate_limited', self.automatic)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(len(self.reporter._auto_sent()), 1)

    def test_automatic_reports_leave_the_manual_hourly_limit_alone(self):
        self.reporter.set_auto({'autoSend': True})
        for category in ('connection', 'model', 'setup'):
            self.automatic(category=category)
        for _ in range(reports.MAX_NEW_PER_HOUR):
            self.assertEqual(self.reporter.send(self.consent(self.preview()))['status'], 'sent')
        self.error('rate_limited', lambda: self.reporter.send(self.consent(self.preview())))
        self.assertEqual(len(self.calls), 3 + reports.MAX_NEW_PER_HOUR)

    def test_the_limit_survives_a_restart_and_two_windows_at_once(self):
        store = reports._MemoryStore()
        self.reporter.store = store
        self.reporter.set_auto({'autoSend': True})
        data = {**INPUT, 'category': 'connection', 'context': 'models', 'automatic': True}
        first, second = self.reporter.preview(data), self.reporter.preview(data)
        self.reporter.send(self.consent(first))
        self.error('rate_limited', lambda: self.reporter.send(self.consent(second)))
        restarted = reports.SupportReports(
            None,
            transport=self.transport,
            now=lambda: self.now,
            monotonic=lambda: self.clock,
            capture=self.capture,
            store=store,
        )
        with self.assertRaises(reports.SupportError):
            restarted.preview(data)
        self.assertEqual(len(self.calls), 1)

    def test_disabled_and_setup_preview_and_closed_never_contact_service(self):
        consent = self.consent(self.preview())
        self.error('unavailable', lambda: self.reporter.send(consent, preview_only=True))
        self.reporter.network_enabled = False
        self.error('unavailable', lambda: self.reporter.send(consent))
        self.reporter.network_enabled = True
        self.reporter.close()
        self.error('unavailable', lambda: self.reporter.send(consent))
        self.error('unavailable', self.preview)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.reporter.previews, {})

    def test_inflight_serializes_duplicate_and_other_reports_and_bounds_caller(self):
        started, release, completed = threading.Event(), threading.Event(), threading.Event()

        def blocked(url, body, headers, timeout):
            started.set()
            release.wait(2)
            result = self.transport(url, body, headers, timeout)
            completed.set()
            return result

        self.reporter.transport = blocked
        first, second = self.consent(self.preview()), self.consent(self.preview())
        with patch.object(reports, 'TIMEOUT', 0.025):
            self.error('unconfirmed', lambda: self.reporter.send(first))
        self.assertTrue(started.is_set())
        self.error('busy', lambda: self.reporter.send(first))
        self.error('busy', lambda: self.reporter.send(second))
        self.assertEqual(len(self.reporter.submissions), 1)
        release.set()
        self.assertTrue(completed.wait(2))
        deadline = time.monotonic() + 2
        while self.reporter.inflight and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertEqual(self.reporter.send(first)['status'], 'sent')
        self.assertEqual(len(self.calls), 1)


class TransportTests(unittest.TestCase):
    def test_explicit_depth_budget_rejects_before_interpreter_json_decoder(self):
        for depth in (reports.MAX_JSON_DEPTH + 1, 2000):
            for body in (
                b'[' * depth + b'0' + b']' * depth,
                b'{"a":' * depth + b'0' + b'}' * depth,
            ):
                with (
                    self.subTest(depth=depth, body=body[:20]),
                    patch.object(reports.json, 'loads') as decoder,
                ):
                    with self.assertRaises(ValueError):
                        reports.loads(body)
                    decoder.assert_not_called()

    def test_depth_boundary_and_brackets_in_escaped_user_text(self):
        body = b'[' * reports.MAX_JSON_DEPTH + b'0' + b']' * reports.MAX_JSON_DEPTH
        self.assertEqual(reports.loads(body), json.loads(body))
        value = {
            'description': (
                'Brackets [{]} and quote " and backslash \\ and escaped quote \\" ' * 40
            ),
            'contact': 'slack-user',
        }
        body = json.dumps(value).encode()
        self.assertEqual(reports.loads(body), value)
        # Escaped quotes must not hide nesting that begins after the string ends.
        deep = (
            json.dumps('quoted " \\ } [').encode()
            + b','
            + b'[' * reports.MAX_JSON_DEPTH
            + b'0'
            + b']' * reports.MAX_JSON_DEPTH
        )
        with self.assertRaises(ValueError):
            reports.loads(b'[' + deep + b']')

    def test_strict_json_rejects_duplicates_invalid_unicode_constants_and_oversize(self):
        for value in (
            b'{"a":1,"a":2}',
            b'{"x":{"a":1,"a":2}}',
            b'NaN',
            b'Infinity',
            b'\xff',
            b' ' * (reports.MAX_BYTES + 1),
            b'[' * 2000 + b']' * 2000,
        ):
            with self.subTest(value=value[:40]), self.assertRaises(ValueError):
                reports.loads(value)
        self.assertEqual(reports.loads(b'{"a":1}'), {'a': 1})

    def test_transport_fixed_https_destination_no_proxy_redirect_or_unbounded_ack(self):
        opener = Mock()
        response = Mock(status=200)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{}'
        opener.open.return_value = response
        with patch.object(urllib.request, 'build_opener', return_value=opener) as build:
            self.assertEqual(
                reports._transport(
                    reports.ENDPOINT, b'{}', {'Authorization': 'Bearer synthetic'}, 15
                ),
                (200, {}),
            )
            handlers = build.call_args.args
            self.assertEqual(handlers[0].proxies, {})
            self.assertIsInstance(handlers[1], reports._NoRedirect)
            self.assertIsNone(handlers[1].redirect_request(None))
            request = opener.open.call_args.args[0]
            self.assertEqual(request.full_url, reports.ENDPOINT)
            self.assertEqual(request.get_method(), 'POST')
            response.read.assert_called_with(reports.MAX_ACK_BYTES + 1)
            response.read.return_value = b'x' * (reports.MAX_ACK_BYTES + 1)
            self.assertEqual(reports._transport(reports.ENDPOINT, b'{}', {}, 15), (200, None))
        for url in (
            'http://bloomformac.com/api/support/v1',
            'https://evil.invalid/',
            reports.ENDPOINT + '?redirect=x',
        ):
            with self.assertRaises(ValueError):
                reports._transport(url, b'{}', {}, 15)

    def test_http_errors_return_no_untrusted_body_or_followup(self):
        opener = Mock()
        error = urllib.error.HTTPError(
            reports.ENDPOINT, 302, SENSITIVE, {}, io.BytesIO(SENSITIVE.encode())
        )
        opener.open.side_effect = error
        with patch.object(urllib.request, 'build_opener', return_value=opener):
            self.assertEqual(reports._transport(reports.ENDPOINT, b'{}', {}, 15), (302, None))
        opener.open.assert_called_once()


class SupportHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.collector = Collector(
            home=root,
            usage_network_enabled=False,
            discovery_enabled=False,
            support_network_enabled=False,
        )
        self.calls = []

        def transport(url, body, headers, timeout):
            self.calls.append((url, body))
            return 200, {'status': 'sent', 'reportId': json.loads(body)['id']}

        self.collector.support_reports = reports.SupportReports(self.collector, transport=transport)
        remote = Remote(root / 'remote.json')
        remote.trusted = {'host': 'bloom.example.ts.net', 'owner': 'owner@example.com'}
        remote.checked = time.monotonic()
        self.remote = remote

        class Local(Handler):
            pass

        Local.collector, Local.remote, Local.dev, Local.static_root = (
            self.collector,
            remote,
            False,
            root,
        )

        class Phone(Local):
            remote_view = True

        self.local = ThreadingHTTPServer(('127.0.0.1', 0), Local)
        self.phone = ThreadingHTTPServer(('127.0.0.1', 0), Phone)
        self.headers = {'Content-Type': 'application/json', 'X-Bloom-Action': 'support'}
        self.phone_headers = {
            **self.headers,
            'Host': 'bloom.example.ts.net:8443',
            'Tailscale-User-Login': 'owner@example.com',
            'X-Forwarded-Proto': 'https',
            'Origin': 'https://bloom.example.ts.net:8443',
        }
        for server in (self.local, self.phone):
            threading.Thread(
                target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True
            ).start()

    def tearDown(self):
        for server in (self.local, self.phone):
            server.shutdown()
            server.server_close()
        self.collector.close()
        self.collector.history.close()
        self.tmp.cleanup()

    def url(self, server, path):
        # Tailscale Serve adds the phone secret to every path.
        prefix = f'/{self.remote.secret}' if server is self.phone else ''
        return f'http://127.0.0.1:{server.server_port}{prefix}{path}'

    def post(self, path, data, server=None, headers=None):
        server = server or self.local
        request = urllib.request.Request(
            self.url(server, '/api/support/' + path),
            data=json.dumps(data).encode(),
            headers=headers if headers is not None else self.headers,
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            return json.load(response)

    def test_local_and_authenticated_phone_send_require_separate_explicit_review(self):
        for server, headers, surface in (
            (self.local, self.headers, 'mac'),
            (self.phone, self.phone_headers, 'phone'),
        ):
            preview = self.post('preview', INPUT, server, headers)
            self.assertEqual(preview['report']['surface'], surface)
            self.assertTrue(preview['report']['diagnostics']['available'])
            self.assertEqual(
                self.post('send', SupportTests.consent(preview), server, headers),
                {'status': 'sent', 'reportId': preview['report']['id']},
            )
        self.assertEqual(len(self.calls), 2)

    def test_foreign_origins_wrong_owners_missing_action_and_phone_origin_rejected(self):
        for server, headers in (
            (self.local, {}),
            (self.local, {**self.headers, 'Origin': 'https://evil.invalid'}),
            (self.local, {**self.headers, 'Sec-Fetch-Site': 'cross-site'}),
            (self.local, {**self.phone_headers}),
            (self.phone, {**self.phone_headers, 'Tailscale-User-Login': 'other@example.com'}),
            (
                self.phone,
                {key: value for key, value in self.phone_headers.items() if key != 'Origin'},
            ),
        ):
            for action in ('preview', 'send'):
                with self.assertRaises(urllib.error.HTTPError) as result:
                    self.post(action, INPUT, server, headers)
                self.assertEqual(result.exception.code, 403)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.collector.support_reports.previews, {})

    def test_setup_preview_allows_review_but_cannot_send_even_with_enabled_mock(self):
        self.local.RequestHandlerClass.setup_preview = True
        preview = self.post('preview', INPUT)
        with self.assertRaises(urllib.error.HTTPError) as result:
            self.post('send', SupportTests.consent(preview))
        self.assertEqual(result.exception.code, 503)
        self.assertEqual(json.load(result.exception)['status'], 'unavailable')
        self.assertEqual(self.calls, [])

    def test_ambiguous_headers_chunking_extra_keys_and_duplicate_json_rejected(self):
        base = [
            ('Host', f'127.0.0.1:{self.local.server_port}'),
            ('X-Bloom-Action', 'support'),
            ('Content-Type', 'application/json'),
        ]
        payload = json.dumps(INPUT).encode()
        for extra, expected in (
            ([('X-Bloom-Action', 'support')], 403),
            (
                [
                    ('Origin', f'http://127.0.0.1:{self.local.server_port}'),
                    ('Origin', 'https://evil.invalid'),
                ],
                403,
            ),
            ([('Content-Type', 'application/json')], 400),
            ([('Content-Length', str(len(payload)))], 400),
            ([('Transfer-Encoding', 'chunked')], 400),
        ):
            connection = http.client.HTTPConnection('127.0.0.1', self.local.server_port, timeout=3)
            connection.putrequest('POST', '/api/support/preview', skip_host=True)
            for name, value in base + [('Content-Length', str(len(payload)))] + extra:
                connection.putheader(name, value)
            connection.endheaders(payload)
            response = connection.getresponse()
            self.assertEqual(response.status, expected)
            response.read()
            connection.close()
        with self.assertRaises(urllib.error.HTTPError) as result:
            self.post('preview', {**INPUT, 'account': SENSITIVE})
        self.assertEqual(result.exception.code, 400)
        connection = http.client.HTTPConnection('127.0.0.1', self.local.server_port, timeout=3)
        connection.request(
            'POST',
            '/api/support/preview',
            payload[:-1] + b',"contact":"duplicate"}',
            headers=self.headers,
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 400)
        response.read()
        connection.close()
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
