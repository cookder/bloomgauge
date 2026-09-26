import copy
import time
import unittest
import uuid
from unittest.mock import Mock, patch
from optimizer_control import OptimizerControl, PLAN_FIELDS
from optimizer_start import OnStart
import test_optimizer_control_start as start_fixtures


PERMISSION = 'Sudoers entry: fixture\n    RunAsUsers: root\n    Options: !authenticate\n    Commands:\n        /usr/sbin/purge ""\n    Matched: /usr/sbin/purge\n'
PURGE = ['/usr/bin/sudo', '-n', '/usr/sbin/purge']


class OnCacheTests(unittest.TestCase):
    write_plist = start_fixtures.ControlStartTests.write_plist
    run_cli = start_fixtures.ControlStartTests.run_cli
    tearDown = start_fixtures.ControlStartTests.tearDown

    def setUp(self):
        start_fixtures.ControlStartTests.setUp(self)
        self.o.live['hardware'].update(memoryAvailableGB=1, cachedFilesGB=30)
        self.before = copy.deepcopy(self.o.state)
        self.purge_count = 0
        self.start_count = 0
        self.views = []
        self.on_purge = lambda: None
        self.on_wait = self.fresh_memory
        self.permission_result = Mock(returncode=0, stdout=PERMISSION)
        self.purge_result = Mock(returncode=0)
        self.o.runner.side_effect = self.run_commands
        self.o.stop.wait.side_effect = self.wait

    def run_commands(self, args, **kw):
        if args == ['/usr/bin/sudo', '-n', '-ll', '/usr/sbin/purge']:
            return self.permission_result
        if args == PURGE:
            self.assertTrue(self.o.state['providerRequests'][-1]['cacheRecoveryAttempted'])
            self.assertEqual(
                self.h.cache('optimizer-settings')['cacheRecovery']['status'], 'running'
            )
            self.purge_count += 1
            self.on_purge()
            return self.purge_result
        if len(args) > 1 and args[1] == 'start':
            self.start_count += 1
        return self.run_cli(args, **kw)

    def fresh_memory(self):
        now = time.time()
        self.o.live['at'] = now
        self.o.live['hardware'].update(at=now, memoryAvailableGB=44)

    def wait(self, seconds):
        if seconds == 2:
            self.on_wait()
        return False

    def cancel(self):
        self.control.projection(time.time())
        self.control.action(
            {
                'action': 'set-automatic',
                'enabled': False,
                'requestId': str(uuid.uuid4()),
                'expectedControl': self.control.snapshot()['controlVersion'],
            }
        )

    def admitted(self):
        self.control.action(self.data)
        worker = Mock()
        worker.is_alive.return_value = False
        with patch('provider_control.threading.Thread', return_value=worker) as factory:
            self.control.advance(time.time())
        return factory.call_args.kwargs['args']

    def execute(self, args=None):
        args = self.admitted() if args is None else args
        original = OnStart.progress

        def record(worker, detail):
            original(worker, detail)
            self.views.append(copy.deepcopy(self.control.projection(time.time())))

        with patch.object(OnStart, 'progress', record):
            self.p.run(*args)
        self.control.tick()
        self.final_view = self.control.snapshot()
        return self.o.state['providerResult']

    def assert_stopped(self, purges=0):
        self.assertEqual(self.purge_count, purges)
        self.assertEqual(self.start_count, 0)
        self.assertEqual(self.o.state['mode'], 'observe')
        self.assertFalse(self.o.state.get('pending'))
        self.assertIn(self.control.operation['status'], ('blocked', 'cancelled'))
        for key in PLAN_FIELDS:
            self.assertEqual(self.before.get(key), self.o.state.get(key), key)

    def test_recovery_once_new_memory_then_start_and_wait_for_readiness(self):
        result = self.execute()
        self.assertEqual(result['status'], 'completed')
        self.assertEqual((self.purge_count, self.start_count), (1, 1))
        self.assertEqual(self.o.state['mode'], 'observe')
        self.assertEqual(self.final_view['automatic']['mode'], 'on')
        self.assertEqual(self.final_view['automatic']['phase'], 'waiting')
        self.assertEqual(len(self.views), 3)
        for view in self.views:
            self.assertEqual(view['automatic']['mode'], 'on')
            self.assertEqual(view['operation']['status'], 'starting')
        self.control.action(self.data)
        self.control.tick()
        self.assertEqual((self.purge_count, self.start_count), (1, 1))
        self.o.store.summary.assert_not_called()

    def test_no_shortfall_starts_without_permission_probe_or_purge(self):
        self.o.live['hardware']['memoryAvailableGB'] = 44
        self.execute()
        self.assertEqual((self.purge_count, self.start_count), (0, 1))

    def test_denied_permission_does_not_purge_or_start(self):
        self.permission_result.returncode = 1
        self.assertIn('approval', self.execute()['detail'])
        self.assert_stopped()

    def test_purge_denied_leaves_actionable_failure_and_no_start(self):
        self.purge_result.returncode = 1
        self.assertIn('Enable cache cleanup', self.execute()['detail'])
        self.assert_stopped(1)

    def test_shared_cooldown_from_running_or_manual_cleanup(self):
        self.o.state['cacheRecovery'] = {'kind': 'manual-stopped-start', 'at': time.time()}
        detail = self.execute()['detail']
        self.assertIn('ten minutes after the previous cleanup', detail)
        self.assertNotIn('displayed time', detail)
        self.assert_stopped()

    def test_new_reading_must_have_enough_memory_without_cached_or_old_gpu_pages(self):
        self.o.live['provider']['memoryGB'] = 50
        self.o.raw['capacity'] = {'gpu_memory_cache_gb': 50}
        self.raw['capacity'] = {'gpu_memory_cache_gb': 50}

        def low():
            self.o.live['hardware'].update(at=time.time(), memoryAvailableGB=1)

        self.on_wait = low
        self.assertIn('more needed', self.execute()['detail'])
        self.assert_stopped(1)

    def test_old_hardware_timestamp_never_authorizes_start(self):
        def unchanged():
            self.o.live['hardware']['memoryAvailableGB'] = 44

        self.on_wait = unchanged
        self.assertIn('did not arrive', self.execute()['detail'])
        self.assert_stopped(1)

    def test_equal_cleanup_timestamp_never_authorizes_start(self):
        self.on_wait = lambda: self.o.live['hardware'].update(
            at=self.o.state['cacheRecovery']['clearedAt'], memoryAvailableGB=44
        )
        self.assertIn('did not arrive', self.execute()['detail'])
        self.assert_stopped(1)

    def test_future_hardware_reading_rejected(self):
        self.on_wait = lambda: self.o.live['hardware'].update(
            at=time.time() + 60, memoryAvailableGB=44
        )
        self.assertIn('fresh hardware', self.execute()['detail'])
        self.assert_stopped(1)

    def test_manual_before_cleanup_prevents_both_commands(self):
        args = self.admitted()
        self.cancel()
        self.execute(args)
        self.assert_stopped()

    def test_manual_during_cleanup_prevents_start_and_enable(self):
        self.on_purge = self.cancel
        self.execute()
        self.assert_stopped(1)

    def test_manual_during_measurement_prevents_start_and_enable(self):
        self.on_wait = self.cancel
        self.execute()
        self.assert_stopped(1)

    def test_plan_change_during_cleanup_prevents_start(self):
        self.on_purge = lambda: self.o.state.update(blockHours=3)
        self.execute()
        self.assertEqual(self.start_count, 0)
        self.assertEqual(self.o.state['mode'], 'observe')

    def test_account_change_during_cleanup_prevents_start(self):
        self.on_purge = lambda: self.o.live.update(account='another')
        self.execute()
        self.assert_stopped(1)

    def test_device_change_during_cleanup_prevents_start(self):
        self.on_purge = lambda: self.o.live.update(device='another')
        self.execute()
        self.assert_stopped(1)

    def test_session_change_during_cleanup_prevents_start(self):
        self.on_purge = lambda: self.raw.update(pid=999)
        self.execute()
        self.assert_stopped(1)

    def test_launch_change_during_measurement_prevents_start(self):
        def change():
            self.fresh_memory()
            self.args += ['--port', '9001']
            self.write_plist()

        self.on_wait = change
        self.execute()
        self.assert_stopped(1)

    def test_battery_during_measurement_prevents_start(self):
        def change():
            self.fresh_memory()
            self.o.on_ac_power.return_value = False

        self.on_wait = change
        self.assertIn('battery', self.execute()['detail'])
        self.assert_stopped(1)

    def test_catalog_change_during_cleanup_prevents_start(self):
        self.on_purge = lambda: self.o.catalog[0].update(active=False)
        self.execute()
        self.assert_stopped(1)

    def test_manual_runtime_exception_is_not_automatic_eligibility(self):
        self.o.catalog[0]['required_provider_capabilities'] = ['apple_m5']
        self.o.live['hardware']['chip'] = 'Apple M5'
        self.assertIn('Runtime support', self.execute()['detail'])
        self.assert_stopped()

    def test_failed_durable_save_prevents_purge(self):
        args = self.admitted()
        original = self.o.save

        def fail():
            if (self.o.state.get('cacheRecovery') or {}).get('status') == 'running':
                raise OSError('fixture')
            original()

        with patch.object(self.o, 'save', side_effect=fail):
            with self.assertRaises(OSError):
                self.p.run(*args)
        self.assertEqual((self.purge_count, self.start_count), (0, 0))

    def test_reopen_blocks_intent_and_same_uuid_never_replays(self):
        def restart():
            self.o.automatic_control = OptimizerControl(self.o)

        self.on_purge = restart
        self.execute()
        reopened = self.o.automatic_control
        reopened.action(self.data)
        reopened.tick()
        self.assertEqual((self.purge_count, self.start_count), (1, 0))
        self.assertEqual(reopened.operation['status'], 'blocked')

    def test_per_uuid_attempt_survives_replaced_shared_marker(self):
        args = self.admitted()
        self.o.state['providerRequests'][-1]['cacheRecoveryAttempted'] = True
        self.o.state['cacheRecovery'] = {'at': time.time() - 601, 'requestId': str(uuid.uuid4())}
        self.assertIn('already attempted', self.execute(args)['detail'])
        self.assert_stopped()


if __name__ == '__main__':
    unittest.main()
