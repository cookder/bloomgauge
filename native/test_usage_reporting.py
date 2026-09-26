"""Synthetic usage tests: temporary files, fake clocks and no hosted traffic."""

import contextlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error

import usage_reporting as usage


class Clock:
    def __init__(self, value=1789819200):  # 2026-09-19 12:00 UTC
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class Transport:
    def __init__(self, post=200, delete=204):
        self.post = post
        self.delete = delete
        self.calls = []
        self.guard = threading.Lock()

    def __call__(self, method, url, body, headers, timeout):
        with self.guard:
            self.calls.append(
                {
                    'method': method,
                    'url': url,
                    'body': body,
                    'payload': json.loads(body),
                    'headers': dict(headers),
                    'timeout': timeout,
                    'thread': threading.get_ident(),
                }
            )
        result = self.post if method == 'POST' else self.delete
        if isinstance(result, Exception):
            raise result
        return result


class UsageReporterTests(unittest.TestCase):
    def test_upgrade_missing_trial_start_flag_preserves_consent_and_legacy_flags(self):
        reporter = self.reporter(network_enabled=False)
        self.consent(reporter)
        reporter.record('trialRequested', immediate=True)
        reporter.record('dashboardOpened')
        reporter.close()
        saved = self.saved()
        identity = (saved['analyticsId'], saved['secret'])
        for day in saved['days'].values():
            day['flags'].pop('trialStarted', None)
        (Path(self.directory.name) / usage.STATE_FILENAME).write_text(json.dumps(saved))
        reopened = self.reporter(app_version='1.36.5', network_enabled=False)
        self.assertTrue(reopened.status()['enabled'])
        self.assertEqual((reopened._state['analyticsId'], reopened._state['secret']), identity)
        flags = reopened._state['days'][usage._day(self.clock())]['flags']
        self.assertTrue(flags['trialRequested'])
        self.assertTrue(flags['dashboardOpened'])
        self.assertFalse(flags['trialStarted'])
        self.assertEqual(len(self.random_calls), 2)

    def test_trial_flag_upgrade_preserves_pending_deletion(self):
        reporter = self.reporter(network_enabled=False)
        self.consent(reporter)
        reporter.record('trialRequested', immediate=True)
        reporter.set_consent(False)
        reporter.close()
        saved = self.saved()
        reopened = self.reporter(app_version='1.36.5', network_enabled=False)
        self.assertFalse(reopened.status()['enabled'])
        self.assertTrue(reopened.status()['deletionPending'])
        self.assertEqual(reopened._state, saved)
        reopened.record('trialStarted', immediate=True)
        self.assertEqual(reopened._state, saved)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.clock = Clock()
        self.transport = Transport()
        self.random_calls = []
        self.reporters = []
        # Even a test that accidentally omits its fake transport cannot upload.
        self.default_transport = patch.object(
            usage, '_transport', side_effect=AssertionError('Unexpected real transport.')
        )
        self.default_transport.start()
        self.addCleanup(self.default_transport.stop)
        self.addCleanup(self.close_reporters)

    def random_hex(self, length):
        self.random_calls.append(length)
        return ('a' if length == 16 else 'b') * (length * 2)

    def reporter(self, **kwargs):
        options = {
            'state_dir': self.directory.name,
            'app_version': '1.36.2',
            'metadata': {'osMajor': 26, 'chipFamily': 'M4 Pro', 'memoryBand': '17-32'},
            'transport': self.transport,
            'now': self.clock,
            'random_hex': self.random_hex,
        }
        options.update(kwargs)
        reporter = usage.UsageReporter(**options)
        self.reporters.append(reporter)
        return reporter

    def close_reporters(self):
        for reporter in self.reporters:
            reporter.close()
        for reporter in self.reporters:
            self.idle(reporter)

    def idle(self, reporter):
        deadline = time.monotonic() + 3
        while reporter.status()['busy']:
            self.assertLess(time.monotonic(), deadline, 'Synthetic worker did not settle.')
            time.sleep(0.002)

    def consent(self, reporter):
        reporter.set_consent(True)
        self.idle(reporter)

    def saved(self):
        return json.loads((Path(self.directory.name) / usage.STATE_FILENAME).read_text())

    def test_off_default_has_no_network_credentials_files_or_worker(self):
        with patch.object(
            usage.threading, 'Thread', side_effect=AssertionError('No pre-consent worker.')
        ):
            reporter = self.reporter()
            reporter.observe(
                setup_completed=True,
                setup_error='connection',
                pro_activated=True,
                optimizer_used=True,
            )
            for flag in usage.FLAGS:
                reporter.record(flag, immediate=True)
            reporter.update_metadata({'osMajor': 15})
            reporter.tick()
            reporter.retry_delete()
            reporter.set_consent(False)
            for invalid in (1, 'yes', None, {}, []):
                reporter.set_consent(invalid)
        self.assertFalse(reporter.status()['enabled'])
        self.assertEqual(self.random_calls, [])
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    def test_payload_allowlist_separate_credentials_header_only(self):
        license_id = 'c' * 32
        reporter = self.reporter(
            metadata={
                'osMajor': 26,
                'chipFamily': 'M4 Pro',
                'memoryBand': '17-32',
                'installationId': license_id,
                'secret': 'license-secret',
                'email': 'private@example.test',
                'url': 'https://private.invalid',
            }
        )
        self.consent(reporter)
        self.assertEqual(self.random_calls, [16, 32])
        call = self.transport.calls[0]
        payload = call['payload']
        self.assertEqual(set(payload), usage.PAYLOAD_KEYS)
        self.assertEqual(payload['analyticsId'], 'a' * 32)
        self.assertNotEqual(payload['analyticsId'], license_id)
        self.assertEqual(payload['appVersion'], '1.36.2')
        self.assertEqual(payload['day'], '2026-09-19')
        self.assertTrue(all(type(payload[key]) is bool for key in usage.FLAGS))
        self.assertEqual(
            call['headers'],
            {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + 'b' * 64},
        )
        self.assertEqual(call['url'], usage.ENDPOINT)
        self.assertLessEqual(len(call['body']), usage.MAX_BODY)
        self.assertNotEqual(call['thread'], threading.get_ident())
        self.assertNotIn(b'b' * 64, call['body'])
        self.assertNotIn(license_id.encode(), call['body'])
        self.assertNotIn('private', json.dumps(payload))
        self.assertEqual(
            set(reporter.status()),
            {
                'enabled',
                'deletionPending',
                'busy',
                'lastSentAt',
                'nextAttemptAt',
                'error',
                'privacyUrl',
            },
        )
        self.assertNotIn('a' * 32, json.dumps(reporter.status()))
        self.assertNotIn('b' * 64, json.dumps(reporter.status()))
        self.assertEqual(reporter.status()['lastSentAt'], self.clock.value)

    def test_metadata_sanitization_replaces_coarse_fields_without_sending(self):
        reporter = self.reporter(
            app_version='1.36.2\nsecret',
            metadata={
                'osMajor': True,
                'chipFamily': 'M4 Pro serial private',
                'memoryBand': '32768 bytes',
            },
        )
        self.consent(reporter)
        payload = self.transport.calls[0]['payload']
        self.assertEqual(
            {key: payload[key] for key in ('appVersion', 'osMajor', 'chipFamily', 'memoryBand')},
            {'appVersion': '0.0.0', 'osMajor': 0, 'chipFamily': 'Other', 'memoryBand': 'unknown'},
        )
        reporter.update_metadata({'osMajor': 15, 'chipFamily': 'M5 Max', 'memoryBand': '65-128'})
        self.idle(reporter)
        self.assertEqual(len(self.transport.calls), 1)
        self.clock.advance(usage.REPORT_INTERVAL)
        reporter.tick()
        self.idle(reporter)
        self.assertEqual(self.transport.calls[-1]['payload']['chipFamily'], 'M5 Max')
        reporter.update_metadata({'osMajor': [26], 'chipFamily': [], 'memoryBand': {}})
        self.clock.advance(usage.REPORT_INTERVAL)
        reporter.tick()
        self.idle(reporter)
        self.assertEqual(self.transport.calls[-1]['payload']['osMajor'], 0)
        for unsupported in (-1, 1, 13, 100, True, '26'):
            self.assertEqual(usage._metadata({'osMajor': unsupported})['osMajor'], 0)

    def test_observing_collector_does_not_count_ui_phone_or_trial(self):
        reporter = self.reporter()
        self.consent(reporter)
        reporter.observe(setup_completed=True, pro_activated=True, optimizer_used=True)
        self.idle(reporter)
        payload = self.transport.calls[-1]['payload']
        self.assertTrue(payload['setupCompleted'])
        self.assertTrue(payload['proActivated'])
        self.assertTrue(payload['optimizerUsed'])
        self.assertFalse(payload['dashboardOpened'])
        self.assertFalse(payload['phoneUsed'])
        self.assertFalse(payload['trialRequested'])
        reporter.record('dashboardOpened', immediate=True)
        reporter.record('phoneUsed', immediate=True)
        reporter.record('optimizerUsed', immediate=True)
        reporter.record('arbitraryPrivateIdentifier', immediate=True)
        self.idle(reporter)
        self.assertEqual(len(self.transport.calls), 2)
        self.clock.advance(usage.REPORT_INTERVAL)
        reporter.tick()
        self.idle(reporter)
        payload = self.transport.calls[-1]['payload']
        self.assertTrue(payload['dashboardOpened'])
        self.assertTrue(payload['phoneUsed'])
        self.assertFalse(payload['trialRequested'])

    def test_immediate_changes_once_each_and_regular_six_hour_cadence(self):
        reporter = self.reporter()
        self.consent(reporter)
        for unused in range(10):
            reporter.set_consent(True)
            reporter.tick()
        self.idle(reporter)
        self.assertEqual(len(self.transport.calls), 1)
        reporter.record('trialRequested', immediate=True)
        self.idle(reporter)
        self.assertEqual(len(self.transport.calls), 2)
        reporter.record('trialRequested', immediate=True)
        reporter.observe(setup_completed=True, pro_activated=True)
        self.idle(reporter)
        self.assertEqual(len(self.transport.calls), 3)
        reporter.observe(setup_completed=True, pro_activated=True, setup_error='connection')
        self.idle(reporter)
        self.assertEqual(len(self.transport.calls), 4)
        for error in ('provider', 'none', 'setup', 'unknown'):
            reporter.observe(setup_completed=False, pro_activated=False, setup_error=error)
            reporter.observe(setup_completed=True, pro_activated=True, setup_error=error)
        self.idle(reporter)
        self.assertEqual(len(self.transport.calls), 4)
        self.clock.advance(usage.REPORT_INTERVAL - 1)
        reporter.tick()
        self.idle(reporter)
        self.assertEqual(len(self.transport.calls), 4)
        self.clock.advance(1)
        reporter.tick()
        self.idle(reporter)
        self.assertEqual(len(self.transport.calls), 5)

    def test_raw_setup_error_is_reduced_to_enum_and_never_logged(self):
        reporter = self.reporter()
        self.consent(reporter)
        output = io.StringIO()
        with contextlib.redirect_stderr(output), contextlib.redirect_stdout(output):
            reporter.observe(setup_error='token=secret/private-account-name')
            self.idle(reporter)
        self.assertEqual(self.transport.calls[-1]['payload']['setupError'], 'unknown')
        self.assertNotIn('token', json.dumps(self.saved()))
        self.assertEqual(output.getvalue(), '')

    def test_atomic_restricted_state_restart_preserves_flags_and_limits(self):
        reporter = self.reporter()
        self.consent(reporter)
        reporter.record('dashboardOpened')
        reporter.record('phoneUsed')
        reporter.observe(setup_completed=True)
        self.idle(reporter)
        calls = len(self.transport.calls)
        for filename in (usage.STATE_FILENAME, usage.LOCK_FILENAME):
            self.assertEqual(
                stat.S_IMODE((Path(self.directory.name) / filename).stat().st_mode), 0o600
            )
        self.assertFalse(list(Path(self.directory.name).glob('.usage-reporting-*.tmp')))
        saved = self.saved()
        self.assertNotIn('chipFamily', json.dumps(saved))
        self.assertNotIn('appVersion', saved)
        reporter.close()
        resumed = self.reporter()
        self.assertEqual(len(self.transport.calls), calls)
        resumed.tick()
        self.idle(resumed)
        self.assertEqual(len(self.transport.calls), calls)
        self.assertEqual(self.random_calls, [16, 32])
        self.clock.advance(usage.REPORT_INTERVAL)
        resumed.tick()
        self.idle(resumed)
        payload = self.transport.calls[-1]['payload']
        self.assertTrue(payload['dashboardOpened'])
        self.assertTrue(payload['phoneUsed'])
        self.assertTrue(payload['setupCompleted'])

    def test_state_dir_none_is_in_memory_and_preview_never_claims_success(self):
        reporter = self.reporter(state_dir=None, network_enabled=False)
        reporter.set_consent(True)
        reporter.record('dashboardOpened')
        reporter.observe(setup_completed=True)
        self.clock.advance(86400)
        reporter.tick()
        self.assertTrue(reporter.status()['enabled'])
        self.assertEqual(reporter.status()['error'], 'network_disabled')
        self.assertIsNone(reporter.status()['lastSentAt'])
        reporter.set_consent(False)
        reporter.retry_delete()
        self.assertTrue(reporter.status()['deletionPending'])
        self.assertFalse(reporter.status()['enabled'])
        self.assertFalse(reporter.status()['busy'])
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    def test_day_rollover_resets_activity_without_bypassing_interval(self):
        self.clock.value = 1789862100  # 2026-09-19 23:55 UTC
        reporter = self.reporter()
        self.consent(reporter)
        reporter.observe(setup_completed=True, pro_activated=True, optimizer_used=True)
        reporter.record('dashboardOpened')
        reporter.record('phoneUsed')
        self.idle(reporter)
        calls = len(self.transport.calls)
        self.clock.advance(600)
        reporter.observe(setup_completed=True, pro_activated=True)
        reporter.tick()
        self.idle(reporter)
        self.assertEqual(len(self.transport.calls), calls)
        self.clock.advance(usage.REPORT_INTERVAL - 600)
        reporter.tick()
        self.idle(reporter)
        payload = self.transport.calls[-1]['payload']
        self.assertEqual(payload['day'], '2026-09-20')
        self.assertTrue(payload['setupCompleted'])
        self.assertTrue(payload['proActivated'])
        self.assertFalse(payload['dashboardOpened'])
        self.assertFalse(payload['phoneUsed'])
        self.assertFalse(payload['optimizerUsed'])
        self.assertLessEqual(len(self.saved()['days']), 2)

    def test_failed_post_retries_are_finite_and_exception_text_not_retained(self):
        self.transport.post = OSError('private token email@example.test')
        reporter = self.reporter()
        self.consent(reporter)
        output = io.StringIO()
        with contextlib.redirect_stderr(output), contextlib.redirect_stdout(output):
            for delay in usage.RETRY_DELAYS:
                self.clock.advance(delay)
                reporter.tick()
                self.idle(reporter)
            for unused in range(10):
                reporter.tick()
                self.idle(reporter)
        self.assertEqual(len(self.transport.calls), 4)
        self.assertEqual(reporter.status()['error'], 'retry_paused')
        self.assertEqual(output.getvalue(), '')
        self.assertNotIn('private', json.dumps(self.saved()))
        self.assertNotIn('example', json.dumps(reporter.status()))
        self.clock.advance(usage.REPORT_INTERVAL)
        reporter.tick()
        self.idle(reporter)
        self.assertEqual(len(self.transport.calls), 5)

    def test_retry_budget_is_persisted_before_transport_and_survives_restart(self):
        seen = []

        def inspect(method, url, body, headers, timeout):
            state = self.saved()
            entry = state['days'][json.loads(body)['day']]
            seen.append((entry['attempts'], entry['retryAt']))
            return 503

        reporter = self.reporter(transport=inspect)
        self.consent(reporter)
        reporter.close()
        resumed = self.reporter(transport=inspect)
        resumed.tick()
        self.idle(resumed)
        self.assertEqual([attempt for attempt, retry_at in seen], [1])
        self.assertTrue(all(retry_at is not None for attempt, retry_at in seen))
        self.clock.advance(usage.RETRY_DELAYS[0])
        resumed.tick()
        self.idle(resumed)
        self.assertEqual([attempt for attempt, retry_at in seen], [1, 2])
        self.assertTrue(all(retry_at is not None for attempt, retry_at in seen))
        self.assertEqual(self.random_calls, [16, 32])

    def test_yesterday_only_retries_and_old_data_is_discarded(self):
        self.clock.value = 1789862340  # 2026-09-19 23:59 UTC
        self.transport.post = 503
        reporter = self.reporter()
        self.consent(reporter)
        self.clock.advance(120)
        reporter.tick()
        self.idle(reporter)
        self.assertEqual(len(self.transport.calls), 2)
        self.assertEqual(self.transport.calls[-1]['payload']['day'], '2026-09-19')
        self.clock.advance(2 * 86400)
        self.transport.post = 200
        reporter.tick()
        self.idle(reporter)
        self.assertEqual(len(self.transport.calls), 3)
        self.assertEqual(self.transport.calls[-1]['payload']['day'], '2026-09-22')
        self.assertEqual(set(self.saved()['days']), {'2026-09-22'})

    def test_daily_attempt_cap_cannot_be_reset_by_events(self):
        self.clock.value = 1789776000  # 2026-09-19 00:00 UTC
        self.transport.post = 503
        reporter = self.reporter()
        self.consent(reporter)
        day_start = self.clock.value
        for cycle in range(4):
            if cycle:
                self.clock.value = day_start + cycle * usage.REPORT_INTERVAL
                reporter.tick()
                self.idle(reporter)
            for delay in usage.RETRY_DELAYS:
                self.clock.advance(delay)
                reporter.tick()
                self.idle(reporter)
        self.assertEqual(len(self.transport.calls), usage.MAX_DAILY_ATTEMPTS)
        reporter.record('trialRequested', immediate=True)
        reporter.observe(setup_completed=True, setup_error='provider', pro_activated=True)
        reporter.tick()
        self.idle(reporter)
        self.assertEqual(len(self.transport.calls), usage.MAX_DAILY_ATTEMPTS)

    def test_opt_out_suppresses_queued_post_and_delete_waits_for_inflight(self):
        started = threading.Event()
        release = threading.Event()
        observed = []
        delegate = self.transport

        def blocked(method, url, body, headers, timeout):
            observed.append(method)
            if method == 'POST':
                started.set()
                if not release.wait(2):
                    raise TimeoutError('Synthetic gate timed out.')
            return delegate(method, url, body, headers, timeout)

        reporter = self.reporter(transport=blocked)
        self.addCleanup(release.set)
        reporter.set_consent(True)
        self.assertTrue(started.wait(2))
        # These changes would otherwise cause another immediate report.
        reporter.record('trialRequested', immediate=True)
        reporter.observe(setup_completed=True, pro_activated=True)
        state = reporter.set_consent(False)
        self.assertFalse(state['enabled'])
        self.assertTrue(state['deletionPending'])
        self.assertEqual(observed, ['POST'])
        rejected = reporter.set_consent(True)
        self.assertFalse(rejected['enabled'])
        self.assertEqual(rejected['error'], 'deletion_pending')
        reporter.record('dashboardOpened')
        reporter.retry_delete()
        saved = self.saved()
        self.assertEqual(set(saved), {'schema', 'enabled', 'analyticsId', 'secret', 'deletion'})
        self.assertEqual(self.random_calls, [16, 32])
        release.set()
        self.idle(reporter)
        self.assertEqual(observed, ['POST', 'DELETE'])
        self.assertEqual(
            self.transport.calls[-1]['payload'], {'schema': 1, 'analyticsId': 'a' * 32}
        )
        self.assertEqual(self.saved(), {'schema': 1, 'enabled': False})
        self.assertFalse(reporter.status()['deletionPending'])

    def test_offline_delete_retains_only_credentials_and_restarts_pending(self):
        reporter = self.reporter()
        self.consent(reporter)
        reporter.record('dashboardOpened')
        self.transport.delete = OSError('private offline reason')
        reporter.set_consent(False)
        self.idle(reporter)
        state = self.saved()
        self.assertEqual(set(state), {'schema', 'enabled', 'analyticsId', 'secret', 'deletion'})
        self.assertEqual(set(state['deletion']), {'attempts', 'lastAttemptAt', 'nextAttemptAt'})
        self.assertTrue(reporter.status()['deletionPending'])
        self.assertEqual(reporter.status()['error'], 'retry_pending')
        reporter.close()
        resumed = self.reporter()
        calls = len(self.transport.calls)
        resumed.tick()
        self.idle(resumed)
        self.assertEqual(len(self.transport.calls), calls)
        self.transport.delete = 204
        resumed.retry_delete()
        self.idle(resumed)
        self.assertFalse(resumed.status()['deletionPending'])
        self.assertEqual(self.saved(), {'schema': 1, 'enabled': False})
        self.assertEqual(self.random_calls, [16, 32])
        self.assertTrue(all(call['method'] == 'DELETE' for call in self.transport.calls[1:]))

    def test_automatic_deletion_retries_stop_until_explicit_retry(self):
        reporter = self.reporter()
        self.consent(reporter)
        self.transport.delete = 503
        reporter.set_consent(False)
        self.idle(reporter)
        for delay in usage.DELETE_DELAYS:
            self.clock.advance(delay)
            reporter.tick()
            self.idle(reporter)
        self.assertEqual(sum(call['method'] == 'DELETE' for call in self.transport.calls), 6)
        self.assertEqual(reporter.status()['error'], 'retry_paused')
        self.clock.advance(7 * 86400)
        reporter.tick()
        self.idle(reporter)
        self.assertEqual(len(self.transport.calls), 7)
        self.assertTrue(reporter.status()['deletionPending'])
        self.transport.delete = 204
        reporter.retry_delete()
        self.idle(reporter)
        self.assertEqual(len(self.transport.calls), 8)
        self.assertFalse(reporter.status()['deletionPending'])

    def test_successful_delete_reconsent_creates_new_identity(self):
        reporter = self.reporter(random_hex=lambda n: os.urandom(n).hex())
        self.consent(reporter)
        first_id = self.transport.calls[0]['payload']['analyticsId']
        first_secret = self.transport.calls[0]['headers']['Authorization']
        reporter.set_consent(False)
        self.idle(reporter)
        self.consent(reporter)
        last = self.transport.calls[-1]
        self.assertNotEqual(last['payload']['analyticsId'], first_id)
        self.assertNotEqual(last['headers']['Authorization'], first_secret)

    def test_close_does_not_block_and_replacement_cannot_race_inflight(self):
        started = threading.Event()
        release = threading.Event()

        def blocked(method, url, body, headers, timeout):
            started.set()
            if not release.wait(2):
                raise TimeoutError('Synthetic gate timed out.')
            return self.transport(method, url, body, headers, timeout)

        reporter = self.reporter(transport=blocked)
        self.addCleanup(release.set)
        reporter.set_consent(True)
        self.assertTrue(started.wait(2))
        reporter.set_consent(False)
        reporter.close()  # Must return while the fake network is blocked.
        blocked_replacement = self.reporter()
        self.assertEqual(blocked_replacement.status()['error'], 'state_unavailable')
        blocked_replacement.tick()
        blocked_replacement.set_consent(True)
        self.assertFalse(blocked_replacement.status()['enabled'])
        self.assertEqual(self.random_calls, [16, 32])
        release.set()
        self.idle(reporter)
        self.assertEqual([call['method'] for call in self.transport.calls], ['POST'])
        resumed = self.reporter()
        resumed.tick()
        self.idle(resumed)
        self.assertEqual([call['method'] for call in self.transport.calls], ['POST', 'DELETE'])
        self.assertFalse(resumed.status()['deletionPending'])

    def test_initial_storage_failure_does_not_upload_or_keep_volatile_consent(self):
        reporter = self.reporter()
        with patch.object(usage.os, 'replace', side_effect=OSError('disk full with private path')):
            reporter.set_consent(True)
        self.idle(reporter)
        self.assertFalse(reporter.status()['enabled'])
        self.assertEqual(reporter.status()['error'], 'storage_unavailable')
        self.assertEqual(self.transport.calls, [])
        self.assertFalse((Path(self.directory.name) / usage.STATE_FILENAME).exists())
        self.assertFalse(list(Path(self.directory.name).glob('.usage-reporting-*.tmp')))

    def test_failed_opt_out_save_stops_network_but_exposes_restart_limitation(self):
        reporter = self.reporter()
        self.consent(reporter)
        original = self.saved()
        with patch.object(usage.os, 'replace', side_effect=OSError('disk full with private path')):
            status = reporter.set_consent(False)
            self.assertFalse(status['enabled'])
            self.assertTrue(status['deletionPending'])
            self.assertEqual(status['error'], 'opt_out_not_saved')
            self.assertEqual(reporter.set_consent(True)['error'], 'opt_out_not_saved')
            self.assertEqual(self.saved(), original)
            self.assertTrue(self.saved()['enabled'])  # Cannot promise durable opt-out.
            reporter.observe(setup_completed=True, optimizer_used=True)
            reporter.record('trialRequested', immediate=True)
            self.clock.advance(usage.REPORT_INTERVAL)
            reporter.tick()
            reporter.retry_delete()
            self.idle(reporter)
            self.assertEqual(reporter.status()['error'], 'opt_out_not_saved')
            self.assertEqual(len(self.transport.calls), 1)
            self.assertEqual(
                set(reporter._state), {'schema', 'enabled', 'analyticsId', 'secret', 'deletion'}
            )
        reporter.tick()
        self.idle(reporter)
        self.assertEqual([call['method'] for call in self.transport.calls], ['POST', 'DELETE'])
        self.assertEqual(self.saved(), {'schema': 1, 'enabled': False})
        self.assertFalse(reporter.status()['deletionPending'])
        self.assertIsNone(reporter.status()['error'])

    def test_successful_remote_delete_with_failed_local_cleanup_stays_pending(self):
        reporter = self.reporter()
        self.consent(reporter)
        replace = os.replace

        def fail_only_cleanup(source, destination):
            if json.loads(Path(source).read_text()) == {'schema': 1, 'enabled': False}:
                raise OSError('Local cleanup blocked.')
            return replace(source, destination)

        with patch.object(usage.os, 'replace', side_effect=fail_only_cleanup):
            reporter.set_consent(False)
            self.idle(reporter)
            self.assertTrue(reporter.status()['deletionPending'])
            self.assertFalse(reporter.status()['enabled'])
            self.assertEqual(reporter.status()['error'], 'storage_unavailable')
            self.assertFalse(self.saved()['enabled'])
            self.assertIn('secret', self.saved())
            reporter.tick()
            reporter.retry_delete()
            self.idle(reporter)
            self.assertEqual([call['method'] for call in self.transport.calls], ['POST', 'DELETE'])
            self.assertTrue(reporter.status()['deletionPending'])
        reporter.tick()
        self.assertFalse(reporter.status()['deletionPending'])
        self.assertEqual(self.saved(), {'schema': 1, 'enabled': False})
        self.assertEqual([call['method'] for call in self.transport.calls], ['POST', 'DELETE'])

    def test_corrupt_or_symlink_state_fails_closed(self):
        state_path = Path(self.directory.name) / usage.STATE_FILENAME
        state_path.write_text('{"enabled":true,"secret":"private"}')
        reporter = self.reporter()
        reporter.tick()
        self.assertFalse(reporter.status()['enabled'])
        self.assertEqual(self.transport.calls, [])
        reporter.close()
        state_path.unlink()
        target = Path(self.directory.name) / 'other-private-file'
        target.write_text('leave me alone')
        state_path.symlink_to(target)
        reporter = self.reporter()
        reporter.tick()
        self.assertFalse(reporter.status()['enabled'])
        self.assertEqual(target.read_text(), 'leave me alone')
        self.assertEqual(self.transport.calls, [])

    def test_non_success_http_status_does_not_report_upload_or_delete_success(self):
        self.transport.post = 201
        reporter = self.reporter()
        self.consent(reporter)
        self.assertIsNone(reporter.status()['lastSentAt'])
        self.transport.delete = 200
        reporter.set_consent(False)
        self.idle(reporter)
        self.assertTrue(reporter.status()['deletionPending'])
        self.assertIn('secret', self.saved())


class StandardTransportTests(unittest.TestCase):
    def test_redirect_handler_does_not_follow_or_forward_credentials(self):
        handler = usage._NoRedirect()
        self.assertIsNone(
            handler.redirect_request(None, None, 307, 'redirect', {}, 'https://other.invalid')
        )

    def test_default_transport_fixed_endpoint_and_no_response_body_read(self):
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self, *args):
                raise AssertionError('Response bodies must not be read.')

        with patch.object(usage.urllib.request, 'build_opener') as build:
            build.return_value.open.return_value = Response()
            self.assertEqual(
                usage._transport(
                    'POST', usage.ENDPOINT, b'{}', {'Authorization': 'Bearer synthetic'}, 8
                ),
                200,
            )
            handlers = build.call_args.args
            self.assertTrue(any(isinstance(handler, usage._NoRedirect) for handler in handlers))
            proxies = next(
                handler
                for handler in handlers
                if isinstance(handler, usage.urllib.request.ProxyHandler)
            )
            self.assertEqual(proxies.proxies, {})
            request = build.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, usage.ENDPOINT)
            self.assertEqual(request.get_method(), 'POST')
            self.assertEqual(build.return_value.open.call_args.kwargs['timeout'], 8)
            with self.assertRaises(ValueError):
                usage._transport('POST', 'http://arbitrary.invalid', b'{}', {}, 8)
            self.assertEqual(build.call_count, 1)

    def test_http_error_returns_code_without_printing_body(self):
        output = io.StringIO()
        error = urllib.error.HTTPError(
            usage.ENDPOINT, 302, 'private-secret-message', {}, io.BytesIO(b'private-response')
        )
        with patch.object(usage.urllib.request, 'build_opener') as build:
            build.return_value.open.side_effect = error
            with contextlib.redirect_stderr(output), contextlib.redirect_stdout(output):
                self.assertEqual(usage._transport('POST', usage.ENDPOINT, b'{}', {}, 8), 302)
        self.assertEqual(output.getvalue(), '')


if __name__ == '__main__':
    unittest.main()
