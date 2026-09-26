"""File-cache cleanup before each switch; never invokes a real CLI or sudo."""

import unittest
from unittest.mock import Mock

from cache_recovery import CacheRecoveryError
import test_optimizer as fixtures
import test_switch_failure_details as details


class SwitchPurgeTests(unittest.TestCase):
    tearDown = fixtures.ControllerTests.tearDown
    start_target = details.SwitchFailureDetailsTests.start_target
    switch = details.SwitchFailureDetailsTests.switch

    def setUp(self):
        details.SwitchFailureDetailsTests.setUp(self)
        self.order = []
        self.o.command.side_effect = lambda *args: (
            self.order.append('start'),
            self.start_target(*args),
        )
        self.o.verify_started = Mock(return_value=True)
        self.o.stop = Mock(**{'wait.return_value': False, 'is_set.return_value': False})
        self.o.manual_selection.permission_status = Mock(return_value={'status': 'ready'})
        self.purge.side_effect = self.cleared

    def cleared(self, runner):
        self.order.append('purge')
        self.o.live['hardware'].update(memoryAvailableGB=24, at=self.now + 1)

    def test_purges_once_before_starting_the_next_model(self):
        self.switch()
        self.assertEqual(self.order, ['purge', 'start'])
        self.assertEqual(self.o.state['switchPurge']['status'], 'cleared')
        self.assertEqual(self.o.state['switchPurge']['model'], 'b')
        self.assertNotIn('lastSwitchFailure', self.o.state)

    def test_cache_blocked_memory_is_rechecked_after_purge(self):
        # 10 GB available + 10 GB resident is short of the 23.3 GB 'b' needs,
        # until the purge turns cached file pages back into available memory.
        self.o.live['hardware']['memoryAvailableGB'] = 10
        self.switch()
        self.assertEqual(self.order, ['purge', 'start'])

    def test_still_short_after_purge_keeps_the_current_model(self):
        self.o.live['hardware']['memoryAvailableGB'] = 10
        self.purge.side_effect = lambda runner: (
            self.order.append('purge'),
            self.o.live['hardware'].update(at=self.now + 1),
        )
        self.switch()
        self.assertEqual(self.order, ['purge'])
        self.o.command.assert_not_called()

    def test_purge_credit_needs_permission_fresh_readings_and_no_recent_shortfall(self):
        self.o.live['hardware'].update(cachedFilesGB=5.3, at=self.now)
        self.assertEqual(self.o.purge_credit('b', self.o.live, self.now), 5.3)
        self.assertEqual(self.o.purge_credit('b', self.o.live, self.now + 30), 0)
        self.o.state['purgeShortfall'] = {'model': 'b', 'at': self.now - 600}
        self.assertEqual(self.o.purge_credit('b', self.o.live, self.now), 0)
        self.assertEqual(self.o.purge_credit('c', self.o.live, self.now), 5.3)
        self.o.state['purgeShortfall']['at'] = self.now - 3601
        self.assertEqual(self.o.purge_credit('b', self.o.live, self.now), 5.3)
        self.o.manual_selection.permission_status.return_value = {'status': 'required'}
        self.assertEqual(self.o.purge_credit('b', self.o.live, self.now), 0)
        self.assertEqual(
            self.o.purge_credit('b', self.o.live, self.now, require_permission=False), 5.3
        )

    def test_demand_switch_still_short_after_purge_stops_the_credit(self):
        self.o.state.update(mode='demand', pending={'kind': 'demand', 'model': 'b'})
        self.o.live['hardware']['memoryAvailableGB'] = 10
        self.purge.side_effect = lambda runner: (
            self.order.append('purge'),
            self.o.live['hardware'].update(at=self.now + 1),
        )
        self.switch()
        self.assertEqual(self.order, ['purge'])
        self.o.command.assert_not_called()
        self.assertEqual(self.o.state['purgeShortfall'], {'model': 'b', 'at': self.now})

    def test_without_permission_switches_without_purge(self):
        self.o.manual_selection.permission_status.return_value = {'status': 'missing'}
        self.switch()
        self.purge.assert_not_called()
        self.assertEqual(self.order, ['start'])
        self.assertNotIn('switchPurge', self.o.state)

    def test_failed_purge_is_recorded_and_never_blocks_the_switch(self):
        self.purge.side_effect = CacheRecoveryError('needs authorization', code='cache-permission')
        self.switch()
        self.assertEqual(self.order, ['start'])
        self.assertEqual(self.o.state['switchPurge']['status'], 'failed')
        self.assertIsNone(self.o.manual_selection.permission)

    def test_recent_purge_is_not_repeated(self):
        self.o.state['switchPurge'] = {'at': self.now - 30, 'status': 'cleared'}
        self.switch()
        self.purge.assert_not_called()
        self.assertEqual(self.order, ['start'])

    def test_provider_restart_during_purge_sends_no_start(self):
        def restarted(runner):
            self.order.append('purge')
            self.o.read_state.side_effect = lambda: {**self.raw, 'pid': 99, 'written_at': self.now}

        self.purge.side_effect = restarted
        self.switch()
        self.o.command.assert_not_called()

    def test_missing_fresh_reading_still_uses_latest_and_is_labeled(self):
        self.purge.side_effect = lambda runner: self.order.append('purge')
        self.switch()
        self.assertEqual(self.order, ['purge', 'start'])
        self.assertIn('no fresh memory reading', self.o.state['switchPurge']['detail'])


if __name__ == '__main__':
    unittest.main()
