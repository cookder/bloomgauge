"""Field reports (Sep 25-28): the optimizer ended up Off with no failure recorded, and legacy
test modes paused on any failed switch (report labels: test_report_labels). The regression
cases failed before their fixes; the plain update cases guard what already worked."""

import copy
import threading
import unittest
from unittest.mock import Mock

import manager
import test_manager as manager_fixture
import test_optimizer as fixtures
from demand_optimizer import POLICY, POLICY_REVISION, policy, repaired_policy
from optimizer import Optimizer
from optimizer_store import device_id

# What the Optimizer keeps only in memory; after a restart the collector and the refresh loop
# fill these in again. The restarted instance takes the fixture's so it sees the same Mac.
IN_MEMORY = (
    'live',
    'raw',
    'read_options',
    'read_state',
    'identity_ok',
    'identity_at',
    'identity_session',
    'discovery_at',
    'on_ac_power',
    'service_disabled',
    'tick_prewarm',
    'recovery_ready',
    'snapshot',
    'local',
    'catalog',
    'warmup',
    'command',
    'verify_started',
    'perform_prewarm',
    'tick_demand',
)


class PolicyRepairTests(unittest.TestCase):
    def test_policy_from_another_build_is_repaired_not_rejected(self):
        saved = {
            **manager_fixture.SAVED,
            'futureKey': 1,  # a key a newer (or older) build saved
            'maxSwitchesPerDay': 100,  # above the range
            'memoryHeadroomGB': 3.3,  # off its 0.5 GB step
            'fallbackEnabled': True,  # not a number
            'confirmationMinutes': 45,  # longer than the run window below
            'minRunMinutes': 30,
        }
        with self.assertRaises(ValueError):
            policy(saved)
        result, repaired = repaired_policy(saved, 2)
        self.assertEqual(policy(result), result)
        self.assertEqual(
            repaired,
            [
                'confirmationMinutes',
                'fallbackEnabled',
                'futureKey',
                'maxSwitchesPerDay',
                'memoryHeadroomGB',
            ],
        )
        self.assertNotIn('futureKey', result)
        self.assertEqual(
            (result['maxSwitchesPerDay'], result['memoryHeadroomGB']),
            (48, 3.5),
        )
        self.assertEqual((result['minRunMinutes'], result['confirmationMinutes']), (30, 30))
        self.assertEqual(result['fallbackEnabled'], POLICY['fallbackEnabled'])
        # Every other saved choice survives, and the manager keys are filled in.
        for key in ('maxDowntimeMinutes', 'trialMinutes', 'protectUsdPerHour'):
            self.assertEqual(result[key], manager_fixture.SAVED[key])
        self.assertEqual((result['managerStrategy'], result['managerExcursions']), (1, 1))

    def test_valid_policies_are_untouched_and_only_unreadable_ones_raise(self):
        self.assertEqual(repaired_policy(None), (policy(), []))
        saved = {**policy(), 'managerStrategy': 0}
        self.assertEqual(repaired_policy(saved, POLICY_REVISION), (saved, []))
        for bad in ('corrupt', [1], 3):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                repaired_policy(bad)


class Restart(manager_fixture.Harness):
    """Manager on, home model a serving and ready (test_manager.Harness)."""

    def restart(self, saved):
        """Quit and reopen Bloomkeeper (an app update) with `saved` as the stored settings."""
        self.h.cache('optimizer-settings', saved)
        old = self.o
        self.o = Optimizer(self.h, self.net, self.tmp.name, threading.Event(), old.runner)
        for name in IN_MEMORY:
            setattr(self.o, name, getattr(old, name))
        return self.o

    def saved(self, **changes):
        state = copy.deepcopy(self.o.state)
        state.update(changes)
        return state

    def paused(self):
        return self.events('paused')

    def assert_manager_runs(self, offsets=(0, 15, 30, 45)):
        self.o.tick_demand.reset_mock()
        for offset in offsets:
            self.at(offset)
            self.assertEqual(self.o.state['mode'], 'demand')
            self.assertTrue(manager.active(self.o.state))
        # Every tick got past the identity and settings checks to the manager's decision.
        self.assertEqual(self.o.tick_demand.call_count, len(offsets))
        self.assertEqual(self.paused(), [])
        self.assertEqual(self.restores(), [])


class AppUpdateTests(Restart):
    """Bug 1 (a, c) and bug 2: updating the app while the manager is on keeps it on."""

    def test_update_from_the_manager_build_keeps_the_manager_on(self):
        self.restart(self.saved(demandPolicyRevision=POLICY_REVISION))
        self.assert_manager_runs()
        self.assertEqual(self.o.state['manager']['home']['model'], 'a')

    def test_update_from_a_pre_manager_demand_plan_turns_the_manager_on(self):
        state = self.saved(
            demandPolicy=copy.deepcopy(manager_fixture.SAVED), demandPolicyRevision=2
        )
        state.pop('manager')
        self.restart(state)
        self.assertEqual(
            self.o.state['demandPolicy'],
            {**manager_fixture.SAVED, 'managerStrategy': 1, 'managerExcursions': 1},
        )
        self.assert_manager_runs()

    def test_policy_saved_by_another_build_keeps_the_manager_on_and_says_so(self):
        rules = {**manager_fixture.SAVED, 'futureKey': 1, 'maxSwitchesPerDay': 100}
        self.restart(self.saved(demandPolicy=rules, demandPolicyRevision=2))
        self.assertEqual(self.o.state['demandPolicy']['maxSwitchesPerDay'], 48)
        self.assertNotIn('futureKey', self.o.state['demandPolicy'])
        self.assertIn('nearest allowed values', self.events('manager-notice')[-1])
        self.assert_manager_runs()

    def test_pre_manager_legacy_runs_are_taken_over_by_the_manager(self):
        # Bug 2: a week test, historical optimization or pair test saved by 1.36.59 has a
        # policy that now upgrades to the Manager; those modes would still pause on a failed
        # switch, so the Manager takes them over instead of leaving them hidden and running.
        for mode in ('week', 'optimize', 'combo'):
            with self.subTest(mode=mode):
                state = self.saved(
                    mode=mode,
                    models=['a', 'b'],
                    endsAt=self.now + 86400,
                    demandPolicy=copy.deepcopy(manager_fixture.SAVED),
                    demandPolicyRevision=2,
                    comboPlan={'status': 'running', 'pairs': [['a', 'b']]},
                )
                state.pop('manager', None)
                self.restart(state)
                self.assertEqual(self.o.state['mode'], 'demand')
                self.assertEqual(self.o.state['comboPlan']['status'], 'cancelled')
                self.assertIn('Manager', self.events('manager-notice')[-1])
                self.assert_manager_runs()

    def test_legacy_strategy_runs_are_left_alone(self):
        legacy = {**policy(), 'managerStrategy': 0}
        self.restart(self.saved(mode='week', demandPolicy=legacy, endsAt=self.now + 86400))
        self.assertEqual(self.o.state['mode'], 'week')
        self.assertFalse(manager.active(self.o.state))

    def test_unreadable_settings_turn_it_off_and_say_why(self):
        self.restart(self.saved(demandPolicy='corrupt'))
        self.assertEqual(self.o.state['mode'], 'observe')
        self.assertEqual(self.o.state['demandPolicy'], policy())
        [reason] = self.paused()
        self.assertIn('could not read its saved optimizer settings', reason)
        self.assertEqual(self.o.detail, reason)
        # The Off card reads this event (lib/optimizer-manager.ts lastPause) for this Mac.
        events = self.o.store.summary(
            self.o.state['account'], self.o.state['device'], 0, self.now, self.now
        )[2]
        self.assertIn(reason, [e['detail'] for e in events if e['kind'] == 'paused'])

    def test_repaired_legacy_rules_turn_it_off_and_say_why(self):
        legacy = {**manager_fixture.SAVED, 'managerStrategy': 0, 'maxSwitchesPerDay': 100}
        self.restart(self.saved(demandPolicy=legacy))
        self.assertEqual(self.o.state['mode'], 'observe')
        self.assertEqual(self.o.state['demandPolicy']['maxSwitchesPerDay'], 48)
        self.assertEqual(self.o.state['demandPolicy']['trialMinutes'], 15)  # kept
        [reason] = self.paused()
        self.assertIn('nearest allowed values', reason)

    def test_update_during_a_darkbloom_update_that_rotated_the_device_key(self):
        # Both updates at once: the app reopens, and Darkbloom came back with a new key.
        self.restart(self.saved())
        self.rotate()
        for offset in (0, 15):
            self.at(offset)
            self.assertEqual(self.o.state['mode'], 'demand')
        self.o.device_identity_ok, self.o.identity_device = True, self.new_device
        self.at(30)
        self.assertEqual(self.o.state['device'], self.new_device)
        self.assert_manager_runs((45, 60))

    def rotate(self):
        self.o.raw['attestation_public_key'] = 'rotated-public-key'
        self.new_device = device_id(self.o.raw)
        self.o.live['device'] = self.new_device
        self.o.read_state.return_value = copy.deepcopy(self.o.raw)


class DeviceKeyTests(Restart):
    """Bug 1 (b): the per-tick account/device check."""

    rotate = AppUpdateTests.rotate

    def test_new_device_key_holds_without_pausing_then_continues(self):
        old = self.o.state['device']
        self.rotate()
        self.o.next_identity = 999
        for offset in (0, 15, 30):
            self.at(offset)
            self.assertEqual(self.o.state['mode'], 'demand')
            self.assertEqual(self.o.state['device'], old)
            self.assertEqual(self.o.status, 'waiting')
            self.assertIn('new device key', self.o.detail)
        self.assertEqual(self.o.next_identity, 0)  # re-verified at the next refresh
        self.assertEqual(self.restores(), [])  # nothing is sent while held
        self.o.tick_demand.assert_not_called()
        [held] = self.events('manager-notice')
        self.assertIn('Automatic control stays on', held)
        # The roster lists the new key for this account: adopt it and carry on.
        self.o.device_identity_ok, self.o.identity_device = True, self.new_device
        self.at(45)
        self.assertEqual(self.o.state['device'], self.new_device)
        self.assertIn('matched it to your account again', self.events('manager-notice')[-1])
        self.assert_manager_runs((60, 75))

    def test_a_roster_match_for_another_key_or_a_stale_one_does_not_adopt(self):
        self.rotate()
        self.o.device_identity_ok, self.o.identity_device = True, 'some-other-device'
        self.at(0)
        self.assertNotEqual(self.o.state['device'], self.new_device)
        self.o.identity_device = self.new_device
        self.o.live['at'] = self.now + 400
        self.o.identity_at = self.now  # 400 s old
        self.o.tick(self.now + 400)
        self.assertNotEqual(self.o.state['device'], self.new_device)
        self.assertEqual(self.o.state['mode'], 'demand')

    def test_another_account_turns_it_off_and_says_why(self):
        device = self.o.live['device']
        self.o.live['account'] = 'another-account'
        self.at(0)
        self.assertEqual(self.o.state['mode'], 'observe')
        [row] = self.h.db.execute(
            "SELECT account, device, detail FROM opt_events WHERE kind='paused'"
        ).fetchall()
        # Written for the account now signed in, which is what the Off card reads.
        self.assertEqual((row[0], row[1]), ('another-account', device))
        self.assertIn('different Darkbloom account', row[2])

    def test_legacy_strategy_still_pauses_on_a_new_device_key(self):
        self.o.state['demandPolicy'] = {**policy(), 'managerStrategy': 0}
        self.rotate()
        self.at(0)
        self.assertEqual(self.o.state['mode'], 'observe')
        self.assertEqual(len(self.paused()), 1)


class LegacyStartTests(unittest.TestCase):
    setUp = fixtures.ControllerTests.setUp
    tearDown = fixtures.ControllerTests.tearDown

    def test_start_refuses_legacy_tests_under_the_manager(self):
        # Bug 2: hidden in the UI under the Manager; the backend refuses them too.
        self.o.state['mode'] = 'observe'
        self.o.snapshot.return_value.update(controlError=None, identityVerified=True)
        for mode in ('week', 'optimize'):
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, 'legacy strategy'):
                self.o.action({'action': 'start', 'mode': mode, 'models': ['a', 'b']})
            self.assertEqual(self.o.state['mode'], 'observe')
        legacy = {**policy(), 'managerStrategy': 0}
        self.o.action(
            {'action': 'start', 'mode': 'optimize', 'models': ['a', 'b'], 'demandPolicy': legacy}
        )
        self.assertEqual(self.o.state['mode'], 'optimize')
        self.assertFalse(manager.enabled(self.o.state['demandPolicy']))


if __name__ == '__main__':
    unittest.main()
