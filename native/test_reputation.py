import json, pathlib, tempfile, unittest
from history import History
from reputation import Reputation, normalize


def provider(key='this-mac', score=0.93):
    return {
        'se_public_key': key,
        'trust_level': 'hardware',
        'status': 'serving',
        'account_id': 'must-not-be-returned',
        'provider_key': 'must-not-be-returned',
        'reputation': {
            'score': score,
            'total_jobs': 100,
            'successful_jobs': 99,
            'failed_jobs': 1,
            'total_uptime_seconds': 72000,
            'avg_response_time_ms': 1500,
            'challenges_passed': 9,
            'challenges_failed': 1,
        },
    }


class ReputationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name)
        (self.home / '.darkbloom').mkdir()
        self.identity('this-mac')
        self.h = History(':memory:')
        self.r = Reputation(self.h, self.home, 'native-secret')

    def tearDown(self):
        self.h.close()
        self.tmp.cleanup()

    def identity(self, key):
        (self.home / '.darkbloom/daemon-state.json').write_text(
            json.dumps({'attestation_public_key': key})
        )

    def ingest(self, sequence=1, **kwargs):
        return self.r.ingest(
            {
                'sequence': sequence,
                'status': 'ok',
                'providers': [provider('other-mac', 0.1), provider()],
                **kwargs,
            },
            1000,
        )

    def test_exact_mac_match_official_score_and_allowlisted_fields(self):
        result = self.ingest()
        self.assertEqual(result['data']['score'], 0.93)
        self.assertEqual(result['data']['successfulJobs'], 99)
        self.assertEqual(result['status'], 'ok')
        serialized = json.dumps(result)
        for secret in (
            'native-secret',
            'must-not-be-returned',
            'this-mac',
            'other-mac',
            'se_public_key',
        ):
            self.assertNotIn(secret, serialized)

    def test_no_matching_machine_and_duplicates_never_select_another(self):
        for rows in ([provider('other')], [provider(), provider()], []):
            self.ingest(sequence=self.r.sequence + 1)
            result = self.ingest(sequence=self.r.sequence + 1, providers=rows)
            self.assertEqual(result['status'], 'unmatched')
            self.assertIsNone(result['data'])

    def test_stale_auth_and_outage_preserve_labelled_last_reading(self):
        self.ingest()
        self.assertEqual(self.r.snapshot(1151)['status'], 'stale')
        for i, status in enumerate(('auth_required', 'unavailable', 'connecting'), 2):
            result = self.r.ingest({'sequence': i, 'status': status}, 1200)
            self.assertEqual(result['data']['updatedAt'], 1000)
            self.assertEqual(result['status'], status)

    def test_disconnect_clears_cache_and_rejects_late_result(self):
        self.ingest()
        result = self.r.ingest({'sequence': 3, 'status': 'disconnected'}, 1020)
        self.assertIsNone(result['data'])
        self.assertIsNone(self.ingest(sequence=2)['data'])
        restarted = Reputation(self.h, self.home)
        self.assertIsNone(restarted.snapshot()['data'])

    def test_restart_uses_only_this_macs_cache_and_marks_it_stale(self):
        self.ingest()
        restarted = Reputation(self.h, self.home)
        self.assertEqual(restarted.snapshot(1001)['status'], 'stale')
        self.identity('new-device')
        self.assertIsNone(restarted.snapshot()['data'])

    def test_zero_is_a_real_score_missing_values_stay_unknown(self):
        row = provider(score=0)
        row['reputation'] = {'score': 0}
        result = self.ingest(providers=[row])['data']
        self.assertEqual(result['score'], 0)
        self.assertIsNone(result['totalJobs'])

    def test_malformed_scores_and_counters_are_unknown_not_a_rejected_reading(self):
        for score in (None, -1, 1.1, float('nan'), float('inf'), True, '0.9'):
            result = normalize(provider(score=score))
            self.assertIsNone(result['score'])
            self.assertEqual(result['totalJobs'], 100)
        for value in (-1, 0.5, float('nan'), True, '7'):
            p = provider()
            p['reputation']['successful_jobs'] = value
            result = normalize(p)
            self.assertIsNone(result['successfulJobs'])
            self.assertEqual((result['totalJobs'], result['failedJobs']), (100, 1))
            self.assertEqual(result['score'], 0.93)

    def test_darkbloom_0_9_10_reading_without_a_score_is_kept(self):
        # coordinator/api/me_handlers.go myReputation: counts only, no composite score.
        row = provider()
        del row['reputation']['score']
        result = self.ingest(providers=[row])
        self.assertEqual(result['status'], 'ok')
        self.assertIsNone(result['data']['score'])
        self.assertEqual(result['data']['successfulJobs'], 99)
        self.assertEqual(result['data']['challengesPassed'], 9)
        # Saved like any reading: a restart shows it as the last reading.
        self.assertIsNone(Reputation(self.h, self.home).snapshot(1001)['data']['score'])
        self.assertEqual(Reputation(self.h, self.home).snapshot(1001)['data']['totalJobs'], 100)

    def test_merged_counters_that_disagree_null_the_total_only(self):
        # The coordinator adds stored counters to live ones; the parts can exceed the total.
        p = provider()
        p['reputation']['successful_jobs'] = 101
        result = normalize(p)
        self.assertIsNone(result['totalJobs'])
        self.assertEqual((result['successfulJobs'], result['failedJobs']), (101, 1))
        self.assertEqual(result['challengesFailed'], 1)
        p['reputation'] = 'not-an-object'
        self.assertIsNone(normalize(p)['score'])

    def test_a_row_without_reputation_is_unavailable_and_keeps_the_last_reading(self):
        self.ingest()
        for sequence, reputation in ((2, None), (3, {}), (4, {'score': 'x', 'total_jobs': -1})):
            row = provider()
            if reputation is None:
                del row['reputation']
            else:
                row['reputation'] = reputation
            result = self.ingest(sequence=sequence, providers=[row])
            self.assertEqual(result['status'], 'unavailable')
            self.assertEqual(result['data']['successfulJobs'], 99)
            self.assertEqual(result['data']['updatedAt'], 1000)
        # Not saved over the good reading either.
        saved = Reputation(self.h, self.home).snapshot(1001)['data']
        self.assertEqual((saved['totalJobs'], saved['score']), (100, 0.93))
        # A real reading afterwards is taken as usual.
        self.assertEqual(self.ingest(sequence=5)['status'], 'ok')

    def test_no_daemon_identity_does_not_use_an_account_wide_guess(self):
        (self.home / '.darkbloom/daemon-state.json').unlink()
        result = self.ingest()
        self.assertFalse(result['identityAvailable'])
        self.assertIsNone(result['data'])


if __name__ == '__main__':
    unittest.main()


class TrustTierTests(unittest.TestCase):
    def test_app_attest_macs_show_their_tier(self):
        # Macs verified through App Attest without Darkbloom MDM are 'self_signed'.
        p = provider()
        p['trust_level'] = 'self_signed'
        self.assertEqual(normalize(p)['trustLevel'], 'self_signed')
        p['trust_level'] = 'made_up'
        self.assertIsNone(normalize(p)['trustLevel'])
