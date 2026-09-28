"""Field reports (Sep 25-28) mislabelled states: a switch failure was dropped when the scope
didn't match, historical optimization read as unknown, and so did a partial monitor. Each case
failed before its fix. The site's schema (bloom-storefront lib/support-protocol.ts) is fixed."""

import json
import unittest

import diagnostics
import support_reports
import test_diagnostics as diagnostics_fixture


class ReportTests(unittest.TestCase):
    """Bug 3: support reports (diagnostics -> support_reports.summary -> the site's schema)."""

    setUp = diagnostics_fixture.DiagnosticsTests.setUp
    tearDown = diagnostics_fixture.DiagnosticsTests.tearDown

    def report(self):
        result = diagnostics.build_report(self.c, now=self.now)
        return result, support_reports.summary(result)

    def failure(self, age):
        self.c.optimizer.state['lastSwitchFailure'] = {
            'at': self.now - age,
            'model': 'gemma-4-26b-qat-4bit',
            'stage': 'verify',
            'code': 'startup-timeout',
            'recovery': 'restored',
            'recoveryCode': 'restored',
        }

    def test_recent_failure_is_reported_when_the_scope_does_not_match(self):
        self.failure(3600)
        for live in (
            {'account': 'another-account', 'device': 'private-device-id'},
            {'account': self.c.account, 'device': ''},  # new key not read yet
            {},
        ):
            with self.subTest(live=live):
                self.c.optimizer.live = live
                result, summary = self.report()
                self.assertFalse(result['optimizer']['scopeMatched'])
                self.assertEqual(
                    result['optimizer']['lastSwitchFailure']['code'], 'startup-timeout'
                )
                self.assertEqual(
                    (summary['failureCode'], summary['recoveryCode']),
                    ('startup-timeout', 'restored'),
                )
                self.assertNotIn('private-device-id', json.dumps(result))
        # A day-old failure outside the matched scope still stays out.
        self.failure(86400 + 60)
        self.assertIsNone(self.report()[0]['optimizer']['lastSwitchFailure'])

    def test_historical_optimization_reports_as_best(self):
        self.c.optimizer.state['mode'] = 'optimize'
        self.c.optimizer.decisions.record(
            self.c.account, 'private-device-id', self.now - 20, 'optimize', 'a', None, 'waiting', ''
        )
        result, summary = self.report()
        self.assertEqual(result['optimizer']['mode'], 'best')
        self.assertEqual(result['optimizer']['recentDecisions'][0]['mode'], 'best')
        self.assertEqual(summary['optimizerMode'], 'best')

    def test_partial_and_connecting_sources_map_to_allowed_statuses(self):
        self.c.snapshot['monitor'] = {'status': 'partial', 'updatedAt': self.now}
        self.c.earnings = {'status': 'connecting', 'updatedAt': None, 'error': None}
        result, summary = self.report()
        self.assertEqual(result['sources']['monitor']['status'], 'ok')
        self.assertEqual(result['sources']['earnings']['status'], 'loading')
        statuses = {s['name']: s['status'] for s in summary['sources']}
        self.assertEqual((statuses['monitor'], statuses['earnings']), ('ok', 'loading'))
        self.assertLessEqual(set(statuses.values()), diagnostics.STATUSES | {'unknown'})



if __name__ == '__main__':
    unittest.main()
