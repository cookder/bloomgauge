import copy, unittest, urllib.error
from unittest.mock import patch
import test_multi_model_reporting as fixtures
from test_multi_model_reporting import T, daemon


class ReportingRecheckTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.MultiModelCollectorTests()
        self.f.setUp()
        self.f.run_ready()
        self.f.step(63, True)
        self.o = self.f.c.optimizer

    def tearDown(self):
        self.f.tearDown()

    def refresh(self, delta):
        with patch('optimizer.time.monotonic', return_value=0):
            self.o.refresh(T + delta)

    def recover(self):
        self.f.step(66, warm_models=list('ab'))
        self.f.step(
            69, warm_models=list('abc'), stats={'requests_served': 80, 'tokens_generated': 1600}
        )

    def test_new_warm_scope_requests_fresh_identity_without_reusing_old_proof(self):
        self.recover()
        calls = self.f.c.network.fetch.call_count
        self.assertTrue(self.o.reporting_recheck_requested)
        self.assertIsNone(self.o.reporting_roster.proof)
        self.assertEqual(self.f.c.snapshot['pulse']['status'], 'unmatched')
        self.refresh(69)
        self.assertEqual(self.f.c.network.fetch.call_count, calls)
        self.f.step(78)
        self.refresh(78)
        self.assertEqual(self.f.c.network.fetch.call_count, calls + 1)
        self.assertEqual(self.o.reporting_roster.proof['at'], T + 78)
        self.assertEqual(self.f.step(78)['pulse']['status'], 'paused')
        self.assertEqual(
            self.f.step(81, stats={'requests_served': 82, 'tokens_generated': 1700})['pulse'][
                'status'
            ],
            'live',
        )
        self.assertFalse(self.o.identity_ok)
        self.assertEqual(
            self.f.c.history.db.execute('SELECT COUNT(*) FROM opt_ready_minutes').fetchone()[0], 0
        )

    def test_failed_recheck_keeps_fail_closed_and_normal_retry_for_unchanged_scope(self):
        self.recover()
        self.f.c.network.fetch.side_effect = urllib.error.URLError('synthetic timeout')
        self.f.step(78)
        self.refresh(78)
        calls = self.f.c.network.fetch.call_count
        self.assertFalse(self.o.identity_ok)
        self.assertFalse(self.o.device_identity_ok)
        self.assertFalse(self.o.identity_hardware)
        self.assertIsNone(self.o.reporting_roster.proof)
        self.assertEqual(self.o.next_identity, T + 138)
        for delta in (81, 93, 108, 123):
            self.f.step(delta)
            self.refresh(delta)
            self.assertEqual(self.f.c.network.fetch.call_count, calls)
            self.assertIsNone(self.o.reporting_roster.proof)

    def test_cold_pending_invalid_and_solo_pair_states_never_request_recheck(self):
        for changes in (
            {'warm_models': list('ab')},
            {'written_at': T + 40},
            {'advertised_models': ['a']},
            {'advertised_models': ['a', 'b']},
            {'trust': {'status': 'offline'}},
        ):
            self.o.invalidate_reporting_identity()
            raw = {**daemon(T + 69), **changes}
            self.assertIsNone(self.o.reporting_identity(raw, T + 69, 'account'))
            self.assertFalse(self.o.reporting_recheck_requested)
        self.o.state['pending'] = {'fixture': True}
        self.assertIsNone(self.o.reporting_identity(daemon(T + 69), T + 69, 'account'))
        self.assertFalse(self.o.reporting_recheck_requested)

    def test_repeated_cold_warm_transitions_do_not_make_roster_calls_more_than_every15_seconds(
        self,
    ):
        calls = []
        clock = {'delta': 66}

        def failed(path):
            calls.append(clock['delta'])
            raise urllib.error.URLError('synthetic timeout')

        self.f.c.network.fetch.side_effect = failed
        for delta in range(66, 109, 3):
            clock['delta'] = delta
            self.f.step(delta, warm_models=list('ab') if delta % 6 == 0 else list('abc'))
            self.refresh(delta)
        self.assertGreaterEqual(len(calls), 2)
        self.assertTrue(all(b - a >= 15 for a, b in zip(calls, calls[1:])), calls)
        self.assertFalse(self.o.identity_ok)
        self.assertIsNone(self.o.reporting_roster.proof)


if __name__ == '__main__':
    unittest.main()
