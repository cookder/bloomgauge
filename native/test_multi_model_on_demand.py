"""Customer regression (Jason, M1 Ultra, Darkbloom 0.9.10, beta 40, Sep 27): 11 models
offered, loaded on demand. Four aren't in Darkbloom's catalog and one catalog model
(Qwen3.8) needs apple_m5 + mlx_nax, so the coordinator's roster row lists six. After
`darkbloom models remove`, the running provider kept offering the four until restart."""

import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from model_combinations import selection_key
from model_readiness import readiness, session_key
from provider_reporting import offered_not_downloaded
from test_live_earnings import T, credit
import test_multi_model_reporting as fixtures

QWEN38 = 'EigenLabs/Qwen3.8-27B-4bit-mtp'
CATALOG = [
    'gemma-4-26b-qat-4bit',
    QWEN38,
    'nvidia-nemotron-3.5-lightning',
    'qwen3.5-35b-a3b',
    'qwen3.6-35b-a3b-vl-mtp-mxfp8',
    'gpt-oss-20b',
    'Qwen3.5-9B',
]
OFF_CATALOG = [
    'org/prefetched-model',
    'org/unmeasured-model',
    'qwen3-vl-30b-a3b-instruct',
    'qwen3.8-flash-next',
]
OFFERED = sorted(CATALOG + OFF_CATALOG)
ROUTED = sorted(m for m in CATALOG if m != QWEN38)
GEMMA, QWEN, VL = 'gemma-4-26b-qat-4bit', 'qwen3.5-35b-a3b', 'qwen3-vl-30b-a3b-instruct'


class OnDemandElevenModelTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.MultiModelCollectorTests()
        self.case.setUp()
        self.addCleanup(self.case.tearDown)
        self.c = self.case.c
        self.case.raw.update(
            advertised_models=OFFERED,
            current_model=VL,
            runtime_capabilities=[],
            stats={'requests_served': 10, 'tokens_generated': 100},
        )
        self.load(VL)  # Darkbloom's startup preload picked a model outside the catalog
        self.case.rows[0]['models'] = ROUTED
        self.tokens = 100

    def load(self, *models):
        self.case.raw.update(
            warm_models=list(models),
            slots=[{'model': m, 'kv_backend': 'paged'} for m in models],
        )

    def step(self, delta, work=False, refresh=False):
        if work:
            self.tokens += 50
        stats = {'requests_served': 10 + self.tokens // 50, 'tokens_generated': self.tokens}
        if delta % 30 == 0:
            self.case.paid(delta, [credit(delta, 100, T + delta - 1, GEMMA, 'this-mac')])
        return self.case.step(delta, refresh, stats=stats)

    def segment(self, snap):
        return snap['provider']['session']['performance']['segmentStartedAt']

    def reputation(self, now):
        row = {
            'se_public_key': 'fixture-key',
            'id': 'this-mac',
            'models': OFFERED,  # the owner API lists the registered inventory, unfiltered
            'trust_level': 'hardware',
            'status': 'serving',
            'online': True,
            'last_heartbeat': datetime.fromtimestamp(now - 2, timezone.utc).isoformat(),
            'pending_requests': 2,
            'max_concurrency': 8,
            'reputation': {
                'score': 0.9,
                'total_jobs': int(now - T),
                'successful_jobs': 1,
                'failed_jobs': 0,
            },
        }
        self.sequence = getattr(self, 'sequence', 0) + 1
        return self.c.reputation.ingest(
            {'sequence': self.sequence, 'status': 'ok', 'requestedAt': now, 'providers': [row]},
            now,
        )

    def test_statistics_count_through_on_demand_loading(self):
        # Roster: six of eleven. Only the off-catalog model is loaded, so nothing counts yet.
        first = self.step(0, refresh=True)
        self.assertEqual(self.c.optimizer.reporting_roster.routed, tuple(ROUTED))
        self.assertIn(
            'none of the 6 models the network sends this Mac work for are loaded yet',
            first['pulse']['detail'],
        )
        self.load(GEMMA, VL)
        self.step(3, work=True)
        live = self.step(6, work=True)
        self.assertEqual(live['pulse']['status'], 'live', live['pulse']['detail'])
        self.assertIn('1 of 6 models loaded now (gemma-4-26b-qat-4bit)', live['pulse']['detail'])
        self.assertIn('work for 6 of the 11 models your provider offers', live['pulse']['detail'])
        for delta in range(9, 64, 3):
            self.step(delta, work=delta % 9 == 0)
        proof = self.c.optimizer.reporting_roster.proof
        with self.assertLogs('bloom.reporting', 'INFO') as logs:
            # Darkbloom loads qwen for a request: a new segment, no pause, no new roster fetch.
            self.load(GEMMA, QWEN, VL)
            loaded = self.step(66, work=True)
            self.assertEqual(loaded['pulse']['status'], 'live')
            self.assertEqual(self.segment(loaded), T + 66)
            # It unloads idle gemma: again a new segment without a pause.
            self.load(QWEN, VL)
            unloaded = self.step(69)
            self.assertEqual(unloaded['pulse']['status'], 'live')
            self.assertEqual(self.segment(unloaded), T + 69)
            # The traffic meter starts its 20-second window again for the new set.
            self.assertEqual(unloaded['traffic']['status'], 'warming')
            # The off-catalog model unloading changes nothing that counts.
            self.load(QWEN)
            same = self.step(72)
            self.assertEqual(self.segment(same), T + 69)
        self.assertEqual(len(logs.records), 3)
        self.assertIn('1 of 11 offered loaded (qwen3.5-35b-a3b)', logs.output[-1])
        self.assertIs(self.c.optimizer.reporting_roster.proof, proof)
        self.assertFalse(self.c.optimizer.reporting_recheck_requested)
        # Session reputation and requests running together are recorded (J3).
        rep = self.reputation(T + 72)
        self.assertEqual(rep['data']['observedSessionId'], same['provider']['session']['id'])
        self.assertEqual(rep['data']['concurrency']['pending'], 2)
        self.assertEqual(self.reputation(T + 73)['session']['reputation']['status'], 'observed')
        # Per-model statistics (and the optimizer) stay with one model or a pair.
        self.assertFalse(self.c.optimizer.tracking(self.case.raw, T + 72)['counting'])
        self.assertIn(
            'Darkbloom manages the 11 models this Mac offers; pick one model in Model controls',
            self.c.optimizer.tracking(self.case.raw, T + 72)['detail'],
        )
        self.assertEqual(
            self.c.history.db.execute('SELECT COUNT(*) FROM opt_ready_minutes').fetchone()[0], 0
        )

    def test_removed_models_are_named_until_darkbloom_restarts(self):
        self.load(GEMMA)
        self.step(0, refresh=True)
        report = self.step(3, work=True)['provider']['multiModelReporting']
        self.assertEqual(report['offeredNotDownloaded'], [])  # the model list isn't read yet
        # `darkbloom models remove` for the four; BloomGauge's next model-list read.
        with self.c.optimizer.lock:
            self.c.optimizer.local = [{'id': m} for m in CATALOG]
            self.c.optimizer.discovery_at = T + 5
        snap = self.step(6, work=True)
        self.assertEqual(
            snap['provider']['multiModelReporting']['offeredNotDownloaded'], OFF_CATALOG
        )
        self.assertEqual(snap['pulse']['reporting']['offeredNotDownloaded'], OFF_CATALOG)
        with patch('optimizer.time.time', return_value=T + 6):
            view = self.c.optimizer.snapshot()
        self.assertEqual(view['reporting']['offeredNotDownloaded'], OFF_CATALOG)
        # Restarting Darkbloom: a new process offering what's on disk; nothing to name.
        self.case.raw.update(pid=456, started_at=T + 8, advertised_models=sorted(CATALOG))
        restarted = self.step(9, refresh=True)['provider']['multiModelReporting']
        self.assertEqual(restarted['offeredNotDownloaded'], [])
        self.assertEqual(len(restarted['models']), 7)


class OfferedNotDownloadedTests(unittest.TestCase):
    def test_names_offered_models_missing_from_a_newer_model_list(self):
        local = [{'id': m} for m in CATALOG]
        self.assertEqual(offered_not_downloaded(OFFERED, local, 200, 100), OFF_CATALOG)
        self.assertEqual(offered_not_downloaded(CATALOG, local, 200, 100), [])

    def test_unknown_older_or_unrelated_lists_name_nothing(self):
        local = [{'id': m} for m in CATALOG]
        for args in (
            (OFFERED, local, 99, 100),  # read before this set was offered: may predate a download
            (OFFERED, local, None, 100),
            (OFFERED, local, 200, None),
            (OFFERED, None, 200, 100),
            (OFFERED, [{'id': 'other'}], 200, 100),  # no offered model: another model folder?
            (OFFERED + [OFFERED[0]], local, 200, 100),
            (None, local, 200, 100),
        ):
            with self.subTest(args=args[2:]):
                self.assertEqual(offered_not_downloaded(*args), [])


class ReadinessCopyTests(unittest.TestCase):
    def raw(self, models, warm):
        return {
            'advertised_models': models,
            'warm_models': warm,
            'written_at': 100,
            'pid': 1,
            'started_at': 1,
            'attestation_public_key': 'key',
            'trust': {'status': 'online'},
        }

    def detail(self, models, warm):
        raw = self.raw(models, warm)
        proof = {
            'status': 'ready',
            'verifiedAt': 50,
            'model': selection_key(models),
            'session': session_key(raw),
        }
        result = readiness(raw, proof, 100, True)
        self.assertFalse(result['counting'])
        return result['detail']

    def test_a_cold_model_or_pair_member_is_named_with_what_happens_next(self):
        self.assertEqual(
            self.detail(['gemma'], []),
            "Statistics paused · gemma isn't loaded right now. Counting resumes when Darkbloom "
            'loads it again.',
        )
        self.assertEqual(
            self.detail(['a', 'b'], ['a']),
            'Statistics paused · a pair counts only while both models are loaded. '
            'Not loaded now: b.',
        )
        self.assertTrue(self.detail(['a', 'b'], None).endswith('Not loaded now: a, b.'))

    def test_three_or_more_models_point_to_one_model_for_per_model_statistics(self):
        self.assertEqual(
            self.detail(OFFERED, [GEMMA]),
            'Per-model statistics need one model or a pair. Darkbloom manages the 11 models '
            'this Mac offers; pick one model in Model controls to record them.',
        )


if __name__ == '__main__':
    unittest.main()
