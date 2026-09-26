import copy, time, unittest
from unittest.mock import Mock, patch
from cache_recovery import CacheRecoveryError
import test_manual_selection as selected


class StoppedCacheTests(unittest.TestCase):
    tearDown = selected.SelectionTests.tearDown
    write_plist = selected.SelectionTests.write_plist
    run_cli = selected.SelectionTests.run_cli
    provider_calls = selected.SelectionTests.provider_calls
    verified = selected.SelectionTests.verified
    stopped = selected.SelectionTests.stopped
    payload = selected.SelectionTests.payload
    admit = selected.SelectionTests.admit
    run_worker = selected.SelectionTests.run_worker
    row = selected.SelectionTests.row

    def setUp(self):
        selected.SelectionTests.setUp(self)
        self.stopped()
        self.o.live['hardware'].update(memoryAvailableGB=20, cachedFilesGB=12, at=time.time())
        self.permission = patch(
            'manual_selection.cache_permission',
            side_effect=lambda runner: {
                'status': 'ready',
                'detail': 'Exact no-password permission verified.',
                'checkedAt': time.time(),
            },
        ).start()
        self.cleanup = patch('manual_selection.clear_file_cache').start()
        self.o.stop.wait.side_effect = self.fresh
        # Stale provider GPU memory is deliberately large and must not help.
        self.raw['capacity'] = {'gpu_memory_active_gb': 40, 'gpu_memory_cache_gb': 20}
        self.o.raw = copy.deepcopy(self.raw)

    def fresh(self, seconds):
        self.o.live['hardware'].update(memoryAvailableGB=40, at=time.time())
        self.o.live['at'] = time.time()
        return False

    def run_selected(self):
        data = self.payload()
        self.admit(data)
        self.run_worker()
        return data

    def assert_stopped_failure(self):
        self.assertEqual(self.provider_calls(), [])
        self.assertFalse(self.o.state.get('pending'))
        self.assertEqual(self.o.state['mode'], 'observe')
        self.assertEqual(self.o.state['manualResult']['status'], 'failed')

    def test_eligible_attempt_never_counts_cache_or_stale_provider_as_available(self):
        r = self.row()
        self.assertTrue(r['canStart'])
        self.assertTrue(r['cacheRecovery']['needed'])
        self.assertTrue(r['cacheRecovery']['canAttempt'])
        self.assertEqual(r['loadBudget']['afterUnloadGB'], 20)
        self.assertGreater(r['loadBudget']['requiredGB'], 20)
        self.cleanup.assert_not_called()
        self.assertEqual(self.provider_calls(), [])

    def test_cleanup_once_then_fresh_memory_then_selected_start_verified(self):
        progress = []
        self.cleanup.side_effect = lambda runner: progress.append(
            self.o.h.cache('optimizer-settings')['manualResult']['detail']
        )
        original = self.fresh

        def fresh(seconds):
            progress.append(self.o.h.cache('optimizer-settings')['manualResult']['detail'])
            return original(seconds)

        self.o.stop.wait.side_effect = fresh
        data = self.run_selected()
        self.cleanup.assert_called_once()
        self.assertEqual(len(self.provider_calls()), 1)
        self.assertEqual(self.provider_calls()[0][-2:], ['--model', 'b'])
        self.assertEqual(self.o.state['manualResult']['status'], 'completed')
        self.assertIn('Clearing', progress[0])
        self.assertIn('fresh memory', progress[1])
        self.assertEqual(self.o.state['cacheRecovery']['requestId'], data['requestId'])
        self.admit(data)
        self.cleanup.assert_called_once()
        self.assertEqual(len(self.provider_calls()), 1)

    def test_post_cleanup_shortfall_stays_stopped_and_reports_measured_numbers(self):
        def low(seconds):
            self.o.live['hardware']['at'] = time.time()
            return False

        self.o.stop.wait.side_effect = low
        data = self.run_selected()
        self.assert_stopped_failure()
        self.cleanup.assert_called_once()
        self.assertIn('20.0 GB available', self.o.state['manualResult']['detail'])
        self.assertIn('more needed', self.o.state['manualResult']['detail'])
        r = self.row()
        self.assertFalse(r['canStart'])
        self.assertIsNotNone(r['cacheRecovery']['retryAt'])
        self.admit(data)
        self.cleanup.assert_called_once()

    def test_container_refresh_without_new_hardware_timestamp_never_starts(self):
        self.o.stop.wait.side_effect = lambda seconds: self.o.live.update(at=time.time()) or False
        self.run_selected()
        self.assert_stopped_failure()
        self.assertEqual(self.o.stop.wait.call_count, 10)
        self.assertIn('fresh hardware', self.o.state['manualResult']['detail'])

    def test_new_but_future_hardware_timestamp_fails_closed(self):
        self.o.stop.wait.side_effect = lambda seconds: (
            self.o.live['hardware'].update(at=time.time() + 60, memoryAvailableGB=40) or False
        )
        self.run_selected()
        self.assert_stopped_failure()
        self.assertIn('fresh hardware', self.o.state['manualResult']['detail'])

    def test_sudo_denial_never_starts_and_keeps_shared_cooldown(self):
        self.cleanup.side_effect = CacheRecoveryError(
            'Automatic cache cleanup needs authorization on the Mac.', code='cache-permission'
        )
        self.run_selected()
        self.assert_stopped_failure()
        self.assertEqual(self.o.state['cacheRecovery']['status'], 'failed')
        self.assertIn('authorization', self.o.state['manualResult']['detail'])
        self.assertFalse(self.row()['canStart'])

    def test_permission_required_or_unknown_disables_only_memory_recovery(self):
        for status in ['required', 'unknown']:
            self.o.manual_selection.permission = None
            self.permission.side_effect = lambda runner: {
                'status': status,
                'detail': 'Check permission on this Mac.',
                'checkedAt': time.time(),
            }
            r = self.row()
            self.assertFalse(r['canStart'])
            self.assertFalse(r['cacheRecovery']['canAttempt'])
            self.assertIn('permission', r['startReason'])
            self.o.live['hardware']['memoryAvailableGB'] = 40
            self.assertTrue(self.row()['canStart'])
            self.o.live['hardware']['memoryAvailableGB'] = 20
        self.cleanup.assert_not_called()

    def test_permission_revoked_after_admission_sends_no_cleanup_or_start(self):
        self.admit()
        self.permission.side_effect = lambda runner: {
            'status': 'required',
            'detail': 'Approval is required.',
            'checkedAt': time.time(),
        }
        self.run_worker()
        self.assert_stopped_failure()
        self.cleanup.assert_not_called()

    def test_running_recovery_cooldown_is_shared_with_stopped_start(self):
        self.o.state['cacheRecovery'] = {
            'session': 'previous-running',
            'at': time.time() - 100,
            'status': 'cleared',
        }
        self.assertFalse(self.row()['canStart'])
        self.assertGreater(self.row()['cacheRecovery']['retryAt'], time.time())
        self.o.state['cacheRecovery']['at'] = time.time() - 601
        self.assertTrue(self.row()['canStart'])

    def test_insufficient_unknown_or_nonfinite_file_cache_never_authorizes_attempt(self):
        for cache in [None, -1, 0, 1, float('nan'), True]:
            self.o.live['hardware']['cachedFilesGB'] = cache
            self.assertFalse(self.row()['canStart'])
        self.cleanup.assert_not_called()

    def test_stale_host_battery_or_invalid_template_never_authorizes_cleanup(self):
        self.o.live['hardware']['at'] = time.time() - 20
        self.assertFalse(self.row()['canStart'])
        self.o.live['hardware']['at'] = time.time()
        self.o.on_ac_power.return_value = False
        self.assertFalse(self.row()['canStart'])
        self.o.on_ac_power.return_value = True
        self.o.local[-1]['template_render_ok'] = False
        self.assertFalse(self.row()['canStart'])
        self.cleanup.assert_not_called()

    def test_state_change_in_slow_target_preflight_sends_nothing(self):
        self.admit()
        self.o.verify_local_target = Mock(
            side_effect=lambda *a: self.o.live.update(account='other')
        )
        self.run_worker()
        self.assert_stopped_failure()
        self.cleanup.assert_not_called()

    def test_external_provider_start_during_cleanup_prevents_selected_start(self):
        self.cleanup.side_effect = lambda runner: setattr(self.process, 'return_value', True)
        self.run_selected()
        self.assert_stopped_failure()
        self.cleanup.assert_called_once()

    def test_changed_account_target_settings_or_request_after_cleanup_sends_no_start(self):
        for field in ['account', 'target', 'settings', 'request']:
            if field != 'account':
                self.tearDown()
                self.setUp()

            def changed(runner):
                if field == 'account':
                    self.o.live['account'] = 'other'
                elif field == 'target':
                    self.o.state['selectionRequest']['model'] = 'a'
                elif field == 'settings':
                    self.args += ['--port', '9000']
                    self.write_plist()
                else:
                    self.o.state['pending']['requestId'] = 'replacement'

            self.cleanup.side_effect = changed
            self.run_selected()
            self.assertEqual(self.provider_calls(), [])
            self.assertEqual(self.o.state['manualResult']['status'], 'failed')

    def test_reopen_or_exact_retry_never_replays_cleanup(self):
        self.o.stop.wait.side_effect = lambda seconds: True
        data = self.run_selected()
        self.assert_stopped_failure()
        self.cleanup.assert_called_once()
        from optimizer import Optimizer

        reopened = Optimizer(self.h, self.net, self.tmp.name, self.stop, Mock())
        self.assertIsNone(reopened.worker)
        self.assertTrue(reopened.manual_selection.replay(data, 'mac'))
        self.cleanup.assert_called_once()

    def test_permission_cache_ttl_and_refresh_recheck_without_mutation(self):
        self.o.manual_snapshot()
        calls = self.permission.call_count
        self.o.manual_snapshot()
        self.assertEqual(self.permission.call_count, calls)
        self.o.manual_selection.permission['checkedAt'] = time.time() - 11
        self.o.manual_snapshot()
        self.assertEqual(self.permission.call_count, calls + 1)
        self.o.manual_action({'action': 'refresh'})
        self.assertEqual(self.permission.call_count, calls + 2)
        self.cleanup.assert_not_called()
        self.assertEqual(self.provider_calls(), [])

    def test_memory_drop_after_cleanup_validation_still_blocks_dispatch(self):
        original = self.o.manual_selection.start_row

        def checked(current, request):
            row = original(current, request)
            if self.cleanup.called and row and not row['cacheRecovery']['needed']:
                self.o.live['hardware']['memoryAvailableGB'] = 1
            return row

        self.o.manual_selection.start_row = checked
        self.run_selected()
        self.assert_stopped_failure()
        self.cleanup.assert_called_once()

    def test_persist_failure_prevents_cleanup_dispatch(self):
        self.admit()
        self.o.save = Mock(side_effect=OSError('synthetic save failed'))
        with self.assertRaises(OSError):
            self.run_worker()
        self.cleanup.assert_not_called()
        self.assertEqual(self.provider_calls(), [])


if __name__ == '__main__':
    unittest.main()
