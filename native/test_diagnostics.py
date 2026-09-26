import json
from pathlib import Path
import tempfile
import time
import unittest
import urllib.error
from unittest.mock import patch

from collector import Collector
from diagnostics import build_report, MAX_BYTES, SWITCH_FAILURE_CODES
from model_readiness import session_key
import test_remote


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        # Whole seconds: report ages round timestamps to 1e-4 s, so a fractional now made exact ages flaky.
        self.tmp = tempfile.TemporaryDirectory()
        self.now = float(int(time.time()))
        self.c = Collector(home=self.tmp.name, data_path=Path(self.tmp.name) / 'history.sqlite3')
        self.c.account = 'private-account@example.com'
        self.c.optimizer.live = {'account': self.c.account, 'device': 'private-device-id'}
        self.c.optimizer.identity_ok = True
        self.c.optimizer.identity_at = self.now
        self.c.snapshot = {
            'at': self.now,
            'provider': {
                'online': True,
                'active': True,
                'model': 'gemma-4-26b-qat-4bit',
                'version': '0.8.16',
                'tracking': {'counting': True},
            },
            'monitor': {'status': 'missing'},
        }
        self.c.earnings = {
            'status': 'error',
            'updatedAt': self.now,
            'error': '401 at https://private.ts.net/path?token=DO-NOT-EXPORT',
        }
        self.c.hardware = {
            'chip': 'Apple M5',
            'memoryTotalGB': 48,
            'memoryAvailableGB': 12,
            'thermal': 'Nominal',
            'powerSource': 'AC Power',
            'username': 'DO-NOT-EXPORT',
        }
        self.c.hardware_at = self.now
        self.c.optimizer.state.update(mode='demand', privateKey='DO-NOT-EXPORT')

    def tearDown(self):
        self.c.close()
        self.c.history.close()
        self.tmp.cleanup()

    def record(
        self,
        account=None,
        device='private-device-id',
        model='gemma-4-26b-qat-4bit',
        detail='memory /Users/private/DO-NOT-EXPORT',
    ):
        account = account or self.c.account
        self.c.optimizer.decisions.record(
            account,
            device,
            self.now - 20,
            'demand',
            model,
            'private-model@example.com',
            'waiting',
            detail,
        )
        self.c.optimizer.store.event(account, device, self.now - 10, 'failed', model, detail, 12)

    def test_allowlist_strips_nested_secrets_and_preserves_actionable_categories(self):
        self.record()
        self.record(account='foreign-account', model='foreign-model')
        result = build_report(self.c, now=self.now)
        encoded = json.dumps(result)
        for secret in [
            'private-account',
            'private-device',
            'private.ts.net',
            'DO-NOT-EXPORT',
            '/Users/',
            'private-model@',
            'foreign-model',
            'gemma-4-26b-qat-4bit',
        ]:
            self.assertNotIn(secret, encoded)
        self.assertEqual(result['sources']['earnings']['errorCategory'], 'authentication')
        self.assertEqual(result['hardware']['thermalState'], 'Nominal')
        self.assertEqual(result['optimizer']['recentDecisions'][0]['reasonCategory'], 'memory')
        self.assertEqual(len(result['optimizer']['recentEvents']), 1)
        self.assertEqual(result['provider']['model']['family'], 'Gemma')
        self.assertEqual(result['provider']['model']['parameterBillions'], 26)
        self.assertNotIn('earnings', result)
        self.assertEqual(
            result['optimizer']['recentEvents'][0]['model']['alias'],
            result['provider']['model']['alias'],
        )

    def test_unknown_strings_never_leak_through_enum_or_hardware_fields(self):
        secret = 'DO-NOT-EXPORT@example.com'
        self.c.hardware.update(chip=secret, powerSource=secret, thermal=secret)
        self.c.snapshot['provider'].update(model=secret, version=secret)
        self.c.optimizer.status = secret
        self.c.optimizer.state['mode'] = secret
        self.c.optimizer.warmup['status'] = secret
        self.c.earnings['status'] = secret
        self.record(model=secret, detail=secret)
        self.assertNotIn(secret, json.dumps(build_report(self.c, now=self.now)))

    def test_legacy_recovery_message_does_not_invent_memory_failure(self):
        detail = (
            'Warm-up or switching failed. A safe, idle recovery could not be verified; '
            'no busy restart or cache purge was forced. Automatic switching is paused; '
            'check Darkbloom on the Mac.'
        )
        self.record(detail=detail)
        report = build_report(self.c, now=self.now)
        self.assertEqual(report['optimizer']['recentEvents'][0]['reasonCategory'], 'failed')
        self.assertIsNone(report['optimizer']['lastSwitchFailure'])
        # The fix is scoped to support exports, not economic decision records.
        self.assertEqual(report['optimizer']['recentDecisions'][0]['reasonCategory'], 'memory')
        self.record(detail='Waiting for enough available memory to pre-warm.')
        self.assertIn(
            'memory',
            [
                row['reasonCategory']
                for row in build_report(self.c, now=self.now)['optimizer']['recentEvents']
            ],
        )

    def test_structured_failure_keeps_primary_cause_separate_from_recovery(self):
        for code in SWITCH_FAILURE_CODES:
            with self.subTest(code=code):
                self.c.optimizer.state['lastSwitchFailure'] = {
                    'at': self.now - 15,
                    'model': 'gemma-4-26b-qat-4bit',
                    'stage': 'verify',
                    'code': code,
                    'recovery': 'blocked',
                    'recoveryCode': 'idle-not-verified',
                    'elapsedSeconds': 372,
                    'detail': '/Users/private/DO-NOT-EXPORT',
                    'account': 'DO-NOT-EXPORT',
                }
                report = build_report(self.c, now=self.now)
                failure = report['optimizer']['lastSwitchFailure']
                self.assertEqual(failure['code'], code)
                self.assertEqual(failure['recoveryCode'], 'idle-not-verified')
                self.assertEqual(failure['recovery'], 'blocked')
                self.assertEqual(failure['ageSeconds'], 15)
                self.assertEqual(failure['elapsedSeconds'], 372)
                self.assertEqual(failure['model']['alias'], report['provider']['model']['alias'])
                self.assertNotIn('DO-NOT-EXPORT', json.dumps(report))

    def test_failure_unknown_fields_and_invalid_numbers_are_not_exported(self):
        secret = 'DO-NOT-EXPORT@example.com'
        self.c.optimizer.state['lastSwitchFailure'] = {
            'at': self.now + 30,
            'model': secret,
            'stage': secret,
            'code': secret,
            'recovery': secret,
            'recoveryCode': secret,
            'elapsedSeconds': float('nan'),
            'detail': secret,
            'privatePath': secret,
        }
        report = build_report(self.c, now=self.now)
        failure = report['optimizer']['lastSwitchFailure']
        self.assertEqual(
            {failure[key] for key in ('stage', 'code', 'recovery', 'recoveryCode')}, {'unknown'}
        )
        self.assertIsNone(failure['ageSeconds'])
        self.assertIsNone(failure['elapsedSeconds'])
        self.assertNotIn(secret, json.dumps(report, allow_nan=False))
        self.c.optimizer.live['account'] = 'other-account'
        self.assertIsNone(build_report(self.c, now=self.now)['optimizer']['lastSwitchFailure'])

    def test_cache_attempt_and_current_capacity_are_redacted_and_not_permission_proof(self):
        raw = {
            'attestation_public_key': 'DO-NOT-EXPORT',
            'pid': 123,
            'started_at': self.now - 100,
            'written_at': self.now - 3,
            'advertised_models': ['gemma-4-26b-qat-4bit'],
            'warm_models': ['gemma-4-26b-qat-4bit'],
            'inference_active': True,
            'capacity': {
                'gpu_memory_active_gb': 14.2,
                'gpu_memory_cache_gb': 0.3,
                'token': 'DO-NOT-EXPORT',
            },
        }
        self.c.optimizer.raw = raw
        self.c.hardware['cachedFilesGB'] = 40
        self.c.optimizer.state['cacheRecovery'] = {
            'at': self.now - 70,
            'session': session_key(raw),
            'status': 'failed',
            'detail': 'Needs authorization /Users/private/DO-NOT-EXPORT',
        }
        report = build_report(self.c, now=self.now)
        cleanup = report['optimizer']['cacheRecovery']
        self.assertEqual(cleanup, {'ageSeconds': 70, 'status': 'failed', 'currentSession': True})
        self.assertEqual(report['hardware']['cachedFilesGB'], 40)
        self.assertEqual(report['provider']['gpuActiveGB'], 14.2)
        self.assertEqual(report['provider']['gpuCacheGB'], 0.3)
        self.assertEqual(report['provider']['selectedModelCount'], 1)
        self.assertEqual(report['provider']['warmModelCount'], 1)
        self.assertTrue(report['provider']['inferenceActive'])
        self.assertEqual(report['provider']['sampleAgeSeconds'], 3)
        encoded = json.dumps(report)
        for private in ('DO-NOT-EXPORT', '/Users/', 'gemma-4-26b-qat-4bit', session_key(raw)):
            self.assertNotIn(private, encoded)
        self.c.optimizer.raw = {**raw, 'pid': 124}
        self.assertFalse(
            build_report(self.c, now=self.now)['optimizer']['cacheRecovery']['currentSession']
        )
        self.c.optimizer.raw = {}
        self.assertIsNone(
            build_report(self.c, now=self.now)['optimizer']['cacheRecovery']['currentSession']
        )
        self.c.optimizer.live['account'] = 'other-account'
        self.assertIsNone(build_report(self.c, now=self.now)['optimizer']['cacheRecovery'])

    def test_missing_or_malformed_capacity_stays_unknown_not_zero(self):
        self.c.optimizer.raw = {
            'written_at': self.now + 60,
            'advertised_models': ['ok', {}],
            'warm_models': None,
            'inference_active': 1,
            'capacity': {'gpu_memory_active_gb': float('inf'), 'gpu_memory_cache_gb': -1},
        }
        self.c.hardware['cachedFilesGB'] = True
        self.c.optimizer.state.update(lastSwitchFailure=[], cacheRecovery='DO-NOT-EXPORT')
        report = build_report(self.c, now=self.now)
        for key in (
            'sampleAgeSeconds',
            'selectedModelCount',
            'warmModelCount',
            'inferenceActive',
            'gpuActiveGB',
            'gpuCacheGB',
        ):
            self.assertIsNone(report['provider'][key], key)
        self.assertIsNone(report['hardware']['cachedFilesGB'])
        self.assertIsNone(report['optimizer']['lastSwitchFailure'])
        self.assertIsNone(report['optimizer']['cacheRecovery'])
        json.dumps(report, allow_nan=False)

    def test_optional_earnings_use_current_scope_signed_credits_and_settlement_tail(self):
        with self.c.history.lock:
            db = self.c.history.db
            db.execute('INSERT INTO opt_identity VALUES(?,?)', ('private-device-id', 'provider-1'))
            for ident, account, provider, model, amount, at in [
                (1, self.c.account, 'provider-1', 'gemma', 100000, self.now - 500),
                (2, self.c.account, 'provider-1', 'gemma', -10000, self.now - 300),
                (3, self.c.account, 'provider-1', 'base_reward', 990000, self.now - 300),
                (4, self.c.account, 'other-device', 'gemma', 990000, self.now - 300),
                (5, 'foreign', 'provider-1', 'gemma', 990000, self.now - 300),
                (6, self.c.account, 'provider-1', 'gemma', 990000, self.now - 20),
                (7, self.c.account, 'provider-1', 'gemma', 990000, self.now - 90000),
            ]:
                db.execute(
                    'INSERT INTO opt_credits VALUES(?,?,?,?,?,?,?)',
                    (account, ident, provider, at, model, amount, 20),
                )
            db.commit()
        earnings = build_report(self.c, True, now=self.now)['earnings']
        self.assertEqual(earnings['recordedInferenceUsd'], 0.09)
        self.assertEqual(earnings['recordedCreditCount'], 2)
        self.assertIn('Missing periods', earnings['coverage'])

    def test_mismatch_stale_future_and_missing_stay_unknown(self):
        self.record()
        self.c.optimizer.live['account'] = 'previous-account'
        self.c.snapshot['at'] = self.now + 900
        self.c.hardware['memoryTotalGB'] = float('nan')
        result = build_report(self.c, True, now=self.now)
        self.assertEqual(result['optimizer']['recentDecisions'], [])
        self.assertFalse(result['optimizer']['scopeMatched'])
        self.assertFalse(result['optimizer']['identityVerified'])
        self.assertIsNone(result['sources']['collector']['sampleAgeSeconds'])
        self.assertIsNone(result['hardware']['memoryTotalGB'])
        self.assertFalse(result['earnings']['available'])
        json.dumps(result, allow_nan=False)

    def test_changed_account_during_capture_is_not_exported(self):
        original = self.c.network.snapshot

        def changed():
            self.c.account = 'new-owner'
            return original()

        with patch.object(self.c.network, 'snapshot', changed):
            with self.assertRaisesRegex(ValueError, 'Account changed'):
                build_report(self.c, now=self.now)

    def test_large_history_is_bounded_and_read_only(self):
        for i in range(70):
            self.c.optimizer.store.event(
                self.c.account,
                'private-device-id',
                self.now - i,
                'failed',
                'gpt-oss-20b',
                'private detail ' + str(i),
                3,
            )
        before = self.c.history.db.total_changes
        result = build_report(self.c, now=self.now)
        self.assertEqual(before, self.c.history.db.total_changes)
        self.assertEqual(len(result['optimizer']['recentEvents']), 40)
        self.assertTrue(result['optimizer']['eventsTruncated'])
        self.assertLess(len(json.dumps(result).encode()), MAX_BYTES)
        self.assertIsNone(self.c.optimizer.state.get('pending'))


class DiagnosticsHTTPTests(unittest.TestCase):
    setUp = test_remote.RemoteHTTPTests.setUp
    tearDown = test_remote.RemoteHTTPTests.tearDown
    read = test_remote.RemoteHTTPTests.read
    denied = test_remote.RemoteHTTPTests.denied

    def test_owner_origin_action_and_body_are_required_and_get_cannot_export(self):
        path = '/api/diagnostics/preview'
        body = b'{"includeEarnings":false}'
        local = {'Content-Type': 'application/json', 'X-Bloom-Action': 'diagnostics'}
        remote = {**local, **self.headers, 'Origin': 'https://' + test_remote.HOST + ':8443'}
        for change in (
            {'Origin': 'null'},
            {'Origin': 'https://evil.example'},
            {'Tailscale-User-Login': 'other@example.com'},
            {'X-Bloom-Action': 'optimizer'},
            {'Sec-Fetch-Site': 'cross-site'},
        ):
            self.denied(self.phone, path, {**remote, **change}, body)
        for key in ('Origin', 'Tailscale-User-Login', 'X-Bloom-Action'):
            self.denied(self.phone, path, {k: v for k, v in remote.items() if k != key}, body)
        self.denied(self.local, path, {'Content-Type': 'application/json'}, body)
        for payload in (
            b'{}',
            b'null',
            b'[]',
            b'{"includeEarnings":1}',
            b'{"includeEarnings":false,"account":"foreign"}',
            b' ' * 257,
        ):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.read(self.local, path, local, payload)
            self.assertEqual(error.exception.code, 400)
        for server, headers in ((self.local, local), (self.phone, remote)):
            with self.read(server, path, headers, body) as response:
                report = json.load(response)
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
                self.assertEqual(report['schema'], 'bloom-diagnostics-v1')
                self.assertNotIn('earnings', report)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.read(self.local, path)
        self.assertEqual(error.exception.code, 404)


if __name__ == '__main__':
    unittest.main()
