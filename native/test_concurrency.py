import copy, unittest
from datetime import datetime, timezone
from reputation import concurrency
from test_reputation import ReputationTests, provider


def metrics(**overrides):
    return {
        'online': True,
        'last_heartbeat': datetime.fromtimestamp(995, timezone.utc).isoformat(),
        'pending_requests': 3,
        'max_concurrency': 8,
        'backend_capacity': {
            'slots': [
                {
                    'model': 'model-a',
                    'state': 'running',
                    'num_running': 2,
                    'num_waiting': 1,
                    'max_concurrency': 4,
                    'secret': 'not-forwarded',
                }
            ]
        },
        **overrides,
    }


class ConcurrencyTests(unittest.TestCase):
    def test_go_nanosecond_heartbeat_on_macos_python(self):
        c = concurrency(metrics(last_heartbeat='1970-01-01T00:16:35.123456789Z'), 1000)
        self.assertIsNotNone(c)
        self.assertAlmostEqual(c['at'], 995.123456)

    def test_counts_remain_distinct_and_allowlisted(self):
        c = concurrency(metrics(), 1000)
        self.assertEqual((c['pending'], c['limit']), (3, 8))
        self.assertEqual(
            c['slots'],
            [{'model': 'model-a', 'state': 'running', 'running': 2, 'waiting': 1, 'limit': 4}],
        )

    def test_idle_missing_invalid_and_offline_are_distinct(self):
        self.assertEqual(concurrency(metrics(pending_requests=0), 1000)['pending'], 0)
        for bad in [None, True, -1, 1.5, '3']:
            self.assertIsNone(concurrency(metrics(pending_requests=bad), 1000)['pending'])
        self.assertIsNone(concurrency(metrics(online=False), 1000))
        self.assertIsNone(concurrency(metrics(last_heartbeat='1970-01-01T00:01:00+00:00'), 1000))
        self.assertIsNone(concurrency(metrics(last_heartbeat='1970-01-01T00:16:35'), 1000))
        self.assertIsNone(concurrency(metrics(last_heartbeat='1970-01-01T01:00:00+00:00'), 1000))
        self.assertEqual(concurrency(metrics(backend_capacity=None), 1000)['slots'], [])
        self.assertIsNone(concurrency(metrics(backend_capacity={'slots': [None]}), 1000))


class ConcurrencySessionTests(ReputationTests):
    def setUp(self):
        super().setUp()

        class Sessions:
            current = {
                'id': 7,
                'status': 'active',
                'performance': {'status': 'counting', 'segmentStartedAt': 900},
            }
            accepted = 7

            def snapshot(self, now):
                return {'current': copy.deepcopy(self.current)}

            def reputation_observation(self, *args):
                return self.accepted

        self.r.sessions = Sessions()
        self.r.identity_context = lambda now: 'verified-connection'

    def sample(self):
        return self.ingest(
            providers=[
                {**provider(), **metrics(), 'models': ['model-a'], 'id': 'verified-connection'}
            ],
            requestedAt=990,
        )

    def test_matched_fresh_session_only_and_age_expiry(self):
        self.assertIsNotNone(self.sample()['data']['concurrency'])
        self.assertIsNone(self.r.snapshot(1036)['data']['concurrency'])
        self.r.sessions.current['id'] = 8
        self.assertIsNone(self.r.snapshot(1001)['data']['concurrency'])

    def test_cold_gap_or_rejected_identity_cannot_reuse_counts(self):
        self.sample()
        self.r.sessions.current['performance']['status'] = 'paused'
        self.assertIsNone(self.r.snapshot(1001)['data']['concurrency'])
        self.r.sessions.current['performance'] = {'status': 'counting', 'segmentStartedAt': 1000}
        self.assertIsNone(self.r.snapshot(1001)['data']['concurrency'])
        self.r.sessions.accepted = False
        self.assertIsNone(
            self.ingest(sequence=2, providers=[{**provider(), **metrics()}], requestedAt=1000)[
                'data'
            ]['concurrency']
        )

    def test_signin_failure_hides_concurrency_but_keeps_reputation(self):
        self.sample()
        s = self.r.ingest({'sequence': 2, 'status': 'auth_required'}, 1001)
        self.assertEqual(s['data']['score'], 0.93)
        self.assertIsNone(s['data']['concurrency'])
