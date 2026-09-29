import copy, subprocess, unittest
from unittest.mock import Mock, patch
from cache_recovery import file_cache_blocked, clear_file_cache, CacheRecoveryError
from prewarm import WarmupError
from optimizer import ExternalChange, session_key
import test_prewarm as fixtures


class CachePolicyTests(unittest.TestCase):
    def setUp(self):
        self.raw = {
            'warm_models': [],
            'inference_active': False,
            'capacity': {'gpu_memory_active_gb': 0, 'gpu_memory_cache_gb': 0},
        }
        self.hw = {'memoryAvailableGB': 17, 'cachedFilesGB': 20}

    def test_known_hashing_cache_pattern_matches(self):
        self.assertTrue(file_cache_blocked(self.raw, self.hw, 30))

    def test_warm_busy_real_gpu_allocations_and_sufficient_memory_never_purge(self):
        for change in (
            {'warm_models': ['a']},
            {'inference_active': True},
            {'capacity': {'gpu_memory_active_gb': 12, 'gpu_memory_cache_gb': 0}},
        ):
            self.assertFalse(file_cache_blocked({**self.raw, **change}, self.hw, 30))
        self.assertFalse(file_cache_blocked(self.raw, {**self.hw, 'memoryAvailableGB': 35}, 30))

    def test_missing_invalid_or_insufficient_cache_evidence_never_purges(self):
        for change in (
            {'cachedFilesGB': 1},
            {'cachedFilesGB': 5},
            {'cachedFilesGB': None},
            {'memoryAvailableGB': float('nan')},
        ):
            self.assertFalse(file_cache_blocked(self.raw, {**self.hw, **change}, 30))
        self.assertFalse(file_cache_blocked({**self.raw, 'capacity': {}}, self.hw, 30))

    def test_only_exact_noninteractive_os_purge_command_can_run(self):
        runner = Mock(return_value=Mock(returncode=0))
        clear_file_cache(runner)
        self.assertEqual(runner.call_args.args[0], ['/usr/bin/sudo', '-n', '/usr/sbin/purge'])
        self.assertEqual(runner.call_args.kwargs['stdin'], subprocess.DEVNULL)
        self.assertNotIn('shell', runner.call_args.kwargs)

    def test_auth_errors_and_timeouts_are_visible_and_redacted(self):
        with self.assertRaisesRegex(CacheRecoveryError, 'authorization'):
            clear_file_cache(Mock(return_value=Mock(returncode=1, stderr='private')))
        with self.assertRaisesRegex(CacheRecoveryError, 'could not finish'):
            clear_file_cache(Mock(side_effect=subprocess.TimeoutExpired('private', 45)))


# Reuse only the fixture, without inheriting/re-running unrelated tests.
class CacheControllerTests(unittest.TestCase):
    setUp = fixtures.WarmupControllerTests.setUp
    tearDown = fixtures.WarmupControllerTests.tearDown
    advance = fixtures.WarmupControllerTests.advance

    def cold(self):
        self.raw['warm_models'] = []
        self.raw['capacity'] = {'gpu_memory_active_gb': 0, 'gpu_memory_cache_gb': 0}
        self.o.raw = copy.deepcopy(self.raw)
        self.o.live['hardware'].update(memoryAvailableGB=10, cachedFilesGB=24)
        self.o.warmup = {'session': session_key(self.raw), 'model': 'a', 'status': 'warming'}

    def test_memory_gate_allows_only_the_targeted_recovery_path(self):
        self.cold()
        self.assertIn('memory', self.o.prewarm_reason(self.raw, self.now))
        self.assertIsNone(self.o.prewarm_reason(self.raw, self.now, allow_cache_recovery=True))
        self.o.live['hardware']['cachedFilesGB'] = 1
        self.assertIn(
            'memory', self.o.prewarm_reason(self.raw, self.now, allow_cache_recovery=True)
        )

    def test_clear_cache_then_wait_for_new_memory_sample(self):
        self.cold()

        def clear(runner):
            self.o.live['at'] = self.now + 1
            self.o.live['hardware']['memoryAvailableGB'] = 35

        with patch('optimizer.clear_file_cache', side_effect=clear) as run:
            self.o.recover_file_cache(self.raw, 'a', ['--local-endpoint'])
        run.assert_called_once()
        self.assertEqual(self.o.state['cacheRecovery']['status'], 'cleared')
        self.assertGreaterEqual(self.now, 1788841848)

    def test_one_attempt_per_session_and_global_cooldown_persist(self):
        self.cold()
        self.o.state['cacheRecovery'] = {'session': session_key(self.raw), 'at': self.now - 700}
        with patch('optimizer.clear_file_cache') as run:
            with self.assertRaisesRegex(WarmupError, 'already attempted'):
                self.o.recover_file_cache(self.raw, 'a', ['--local-endpoint'])
            self.o.state['cacheRecovery'] = {'session': 'other', 'at': self.now - 100}
            with self.assertRaisesRegex(WarmupError, 'cooling down'):
                self.o.recover_file_cache(self.raw, 'a', ['--local-endpoint'])
        run.assert_not_called()

    def test_a_load_after_the_cleanup_allows_one_more_in_the_session(self):
        """Sep 28: each idle unload leaves the weights in file cache again; the once-per-session
        limit left the Mac dark after the second unload of a long-running provider."""
        self.cold()
        key = session_key(self.raw)
        cleared = {'session': key, 'at': self.now - 700, 'status': 'cleared'}
        snapshot = {**copy.deepcopy(self.o.live), 'at': self.now}
        for warm, written, status in (
            ([], self.now, 'cleared'),  # never loaded: still one attempt
            (['a'], self.now - 800, 'cleared'),  # a reading from before the cleanup
            (['a'], self.now, 'loaded'),
        ):
            self.o.state['cacheRecovery'] = dict(cleared)
            self.o.observe('acct', {**self.raw, 'warm_models': warm, 'written_at': written}, snapshot)
            self.assertEqual(self.o.state['cacheRecovery']['status'], status)
        self.o.live['hardware'].update(memoryAvailableGB=10, cachedFilesGB=24)

        def clear(runner):
            self.o.live['at'] = self.now + 1

        with patch('optimizer.clear_file_cache', side_effect=clear) as run:
            self.o.recover_file_cache(self.raw, 'a', ['--local-endpoint'])
            run.assert_called_once()
            self.assertEqual(self.o.state['cacheRecovery']['status'], 'cleared')
            self.now += 700
            with self.assertRaisesRegex(WarmupError, 'already attempted'):
                self.o.recover_file_cache(self.raw, 'a', ['--local-endpoint'])

    def test_busy_or_changed_session_never_clears(self):
        self.cold()
        old = copy.deepcopy(self.raw)
        self.raw['inference_active'] = True
        with patch('optimizer.clear_file_cache') as run:
            with self.assertRaisesRegex(WarmupError, 'idle'):
                self.o.recover_file_cache(old, 'a', ['--local-endpoint'])
            self.raw['started_at'] += 1
            with self.assertRaises(ExternalChange):
                self.o.recover_file_cache(old, 'a', ['--local-endpoint'])
        run.assert_not_called()

    def test_failed_authorization_is_not_reported_as_cleared(self):
        self.cold()
        with patch(
            'optimizer.clear_file_cache', side_effect=CacheRecoveryError('Needs authorization')
        ):
            with self.assertRaisesRegex(WarmupError, 'authorization'):
                self.o.recover_file_cache(self.raw, 'a', ['--local-endpoint'])
        self.assertEqual(self.o.state['cacheRecovery']['status'], 'failed')
        self.assertEqual(self.o.h.cache('optimizer-settings')['cacheRecovery']['status'], 'failed')

    def test_stale_samples_do_not_start_inference_after_purge(self):
        self.cold()
        with patch('optimizer.clear_file_cache'):
            with self.assertRaisesRegex(WarmupError, 'fresh memory'):
                self.o.recover_file_cache(self.raw, 'a', ['--local-endpoint'])
        self.assertEqual(self.o.state['cacheRecovery']['status'], 'cleared')


if __name__ == '__main__':
    unittest.main()
