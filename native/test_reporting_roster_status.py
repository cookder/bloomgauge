"""Coordinator busy status cannot interrupt verified, read-only 3+ reporting."""

import unittest
import urllib.error
import test_multi_model_reporting as fixtures


class ReportingRosterStatusTests(unittest.TestCase):
    def fixture(self, ready=True):
        case = fixtures.MultiModelCollectorTests()
        case.setUp()
        self.addCleanup(case.tearDown)
        if ready:
            result = case.run_ready()
            self.assertEqual(result['pulse']['status'], 'live')
            self.assertEqual(result['traffic']['status'], 'live')
        return case

    def assert_no_control_evidence(self, case, delta):
        self.assertFalse(case.c.optimizer.identity_ok)
        self.assertFalse(case.c.optimizer.tracking(case.raw, fixtures.T + delta)['counting'])
        self.assertEqual(case.c.optimizer.state['mode'], 'observe')
        self.assertEqual(
            case.c.history.db.execute('SELECT COUNT(*) FROM opt_ready_minutes').fetchone()[0], 0
        )
        case.c.optimizer.runner.assert_not_called()

    def test_online_serving_online_keeps_same_warm_session_and_live_pulses(self):
        case = self.fixture()
        original = case.c.snapshot
        session = original['provider']['session']['id']
        verified_at = original['provider']['tracking']['verifiedAt']
        for delta, status in ((66, 'serving'), (69, 'online'), (72, 'serving'), (75, 'online')):
            with self.subTest(status=status, delta=delta):
                case.rows[0]['status'] = status
                result = case.step(
                    delta,
                    True,
                    stats={'requests_served': 10 + delta, 'tokens_generated': 100 + delta * 20},
                )
                self.assertEqual(result['pulse']['status'], 'live')
                self.assertEqual(result['traffic']['status'], 'live')
                self.assertEqual(result['provider']['session']['id'], session)
                self.assertEqual(result['provider']['tracking']['verifiedAt'], verified_at)
                self.assertEqual(result['pulse']['sessionMicroUsd'], 600)
                self.assertEqual(case.c.optimizer.reporting_roster.proof['at'], fixtures.T + delta)
                self.assertTrue(case.c.optimizer.identity_hardware)
                self.assert_no_control_evidence(case, delta)

    def test_initial_serving_match_still_waits_for_actual_output_and_traffic_window(self):
        case = self.fixture(ready=False)
        case.rows[0]['status'] = 'serving'
        case.paid(0, [])
        self.assertEqual(case.step(0, True)['pulse']['status'], 'paused')
        self.assertIsNotNone(case.c.optimizer.reporting_roster.proof)
        self.assertEqual(case.step(3)['pulse']['status'], 'paused')
        for delta in range(6, 28, 3):
            result = case.step(
                delta, stats={'requests_served': 10 + delta, 'tokens_generated': 100 + delta * 20}
            )
            self.assertEqual(result['pulse']['status'], 'live')
            self.assertEqual(result['traffic']['status'], 'live' if delta >= 27 else 'warming')
            self.assert_no_control_evidence(case, delta)

    def test_invalid_roster_or_trust_revokes_even_when_recent_output_grows(self):
        invalid = (
            [{'status': value} for value in ('offline', 'unknown', 'busy', 'Serving', '', None)]
            + [
                {'status': 'serving', 'trust_level': value}
                for value in ('software', 'unknown', None)
            ]
            + [
                {'status': 'serving', 'se_public_key': 'other-device'},
                {'status': 'serving', 'models': ['a', 'b', 'd']},
                {'status': 'serving', 'provider_id': None},
            ]
        )
        for change in invalid:
            with self.subTest(change=change):
                case = self.fixture()
                case.rows[0].update(change)
                result = case.step(
                    66, True, stats={'requests_served': 76, 'tokens_generated': 1420}
                )
                self.assertEqual(result['pulse']['status'], 'unmatched')
                self.assertNotEqual(result['traffic']['status'], 'live')
                self.assertIsNone(case.c.optimizer.reporting_roster.proof)
                self.assert_no_control_evidence(case, 66)
                case.c.network.fetch.side_effect = urllib.error.URLError(
                    'fixture timeout after revoked proof'
                )
                self.assertEqual(case.step(69, True)['pulse']['status'], 'unmatched')
                self.assertIsNone(case.c.optimizer.reporting_roster.proof)

    def test_serving_status_never_bridges_changed_provider_identity(self):
        case = self.fixture()
        case.rows[0].update(status='serving', provider_id='replacement-provider')
        self.assertEqual(case.step(66, True)['pulse']['status'], 'paused')
        self.assertEqual(case.step(69)['pulse']['status'], 'paused')
        result = case.step(72, stats={'requests_served': 90, 'tokens_generated': 2000})
        self.assertEqual(result['pulse']['status'], 'live')
        self.assertNotEqual(result['traffic']['status'], 'live')
        self.assertEqual(
            result['provider']['session']['performance']['segmentStartedAt'], fixtures.T + 72
        )
        self.assertEqual(
            case.c.optimizer.reporting_roster.proof['provider'], 'replacement-provider'
        )
        self.assert_no_control_evidence(case, 72)


if __name__ == '__main__':
    unittest.main()
