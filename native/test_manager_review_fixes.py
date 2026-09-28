"""Regressions from the beta 41 control-path review (Sep 27 2026): deferred moves and
reloads, resting pairs, battery-exempt cache recovery, the stall ladder, the watchdog's
early retry, Start's memory check, idle-timeout settings and the restore grace. Every
provider command is mocked.
"""

import copy
import time
import unittest
from unittest.mock import Mock, patch

import manager
import stall_recovery as sr
import test_demand_controller as controller
import test_manager as tm
import test_optimizer as fixtures
import test_provider_control as pc
import test_prewarm as warm_fixtures
from model_combinations import selection_key
from optimizer import DemandDeferred, NothingSent, Optimizer
from prewarm import WarmupDeferred
from test_demand_optimizer import decision as legacy_upgrade
from test_manager_control_path import NEMOTRON, idle_policy

PAIR = selection_key(['a', 'b'])


class ManagerDecisionHarness(tm.Harness):
    """The real demand tick and switch path, with the legacy evaluation stubbed."""

    def setUp(self):
        super().setUp()
        del self.o.tick_demand
        self.o.demand_auto.evaluate = Mock(side_effect=self.legacy)
        self.o.demand_auto.evidence = Mock(return_value=({}, {}))

    def legacy(self, account, device, rows, current, raw, rules, now, *args, **kwargs):
        d = legacy_upgrade()
        d.update(at=now, currentModel=current, policy=rules, target=None, kind=None)
        return d


# --- 1: a deferred return home is not retried every tick ------------------------------


class DeferredHomeTests(ManagerDecisionHarness):
    def setUp(self):
        super().setUp()
        # current is a (warm, counting); the saved home is b (a pin, so it stays home)
        self.o.state['manager']['home'] = manager.pin('b', self.now - 7200)
        self.o.state['lastSwitchAt'] = self.now - 4000
        self.o.purge_before_load = Mock(return_value=False)

    def test_a_deferred_return_home_waits_a_growing_time(self):
        self.o.verify_local_target = Mock(
            side_effect=DemandDeferred('The selected model’s local files could not be verified.')
        )
        dispatched = []
        for offset in range(0, 901, 15):
            before = self.o.verify_local_target.call_count
            self.at(offset)
            if self.o.verify_local_target.call_count > before:
                dispatched.append(offset)
        # 1, 2, 4 and 8 minutes apart (was every 15-s tick: 21 in the first 5 minutes)
        self.assertEqual(dispatched, [0, 60, 180, 420, 900])
        self.assertEqual(len(self.events('switch-deferred')), 5)
        self.assertEqual(self.o.state['manager']['deferred']['count'], 5)
        self.assertEqual(self.o.state['mode'], 'demand')
        # A move that goes through clears the wait.
        self.o.verify_local_target = Mock()
        self.at(1800)
        self.assertEqual([c.args[0] for c in self.o.command.call_args_list], ['b'])
        self.assertNotIn('deferred', self.o.state['manager'])
        self.assertNotIn('retryAt', self.o.state['manager'])

    def test_a_home_whose_template_failed_is_never_dispatched(self):
        self.o.verify_local_target = Mock()
        for row in self.o.local:
            if row['id'] == 'b':
                row['template_render_ok'] = False
        self.ticks(0, 300, 15)
        self.o.verify_local_target.assert_not_called()
        self.o.purge_before_load.assert_not_called()
        self.assertIn('chat template failed', self.o.detail)
        # The serving model is never marked unavailable for it.
        rows = self.o.candidates({}, {}, self.o.live, {})
        self.assertTrue(next(r for r in rows if r['id'] == 'a')['available'])


# --- 2 and 9: a reload that sent nothing is not an attempt ----------------------------


class DeferredReloadTests(tm.Harness):
    def test_a_deferred_watchdog_reload_is_not_a_failed_restore(self):
        self.o.perform_prewarm = Mock(
            side_effect=[
                WarmupDeferred('Waiting for fresh local readings.', code='readiness-changed'),
                True,
            ]
        )
        self.dark()
        self.ticks(0, 600)
        m = self.o.state['manager']
        self.assertEqual(self.loads(), ['a'])
        self.assertEqual(m['watchdog'].get('attempts', 0), 0)
        for key in ('homeRetry',):
            self.assertNotIn(key, m)
        self.assertNotIn('lastSwitchFailure', self.o.state)
        self.assertNotIn('lastSwitchResult', self.o.state)
        self.assertEqual(self.events('failed'), [])
        self.assertNotIn('never loaded after the switch', self.o.detail)
        self.at(630)  # the 60-second retry wait
        self.assertEqual(self.loads(), ['a'])
        self.at(660)
        # Retried as a reload (not escalated to a restart), and it worked.
        self.assertEqual((self.restores(), self.loads()), ([], ['a', 'a']))
        self.assertEqual(self.o.state['lastSwitchResult']['outcome'], 'recovered')

    def test_reloads_deferred_again_and_again_fall_back_to_a_restart(self):
        self.o.perform_prewarm = Mock(
            side_effect=WarmupDeferred('Waiting for supported models.', code='readiness-changed')
        )
        self.dark()
        self.ticks(0, 1200)
        self.assertEqual(len(self.loads()), manager.RELOAD_DEFERRALS)
        self.assertEqual(self.restores(), ['a'])
        self.assertEqual(self.events('failed'), [])

    def test_a_watchdog_reload_without_a_local_endpoint_is_a_restart(self):
        self.o.read_options.return_value = ('a', ['--port', '8000'], {})
        self.dark()
        self.ticks(0, 600)
        self.assertEqual((self.restores(), self.loads()), (['a'], []))

    def test_a_launchctl_failure_before_the_warm_up_is_not_an_idle_reload_attempt(self):
        # Optimizer.perform_prewarm: a failed launchctl read (None) before anything was sent.
        o = self.o
        o.service_disabled = Mock(return_value=None)
        with patch('optimizer.time.time', return_value=self.now):
            with self.assertRaises(NothingSent):
                Optimizer.perform_prewarm(
                    o, 'a', copy.deepcopy(o.raw), ['--local-endpoint', '--port', '8000']
                )
        o.service_disabled = Mock(return_value=False)
        self.o.perform_prewarm = Mock(side_effect=[NothingSent('Provider changed.'), True])
        self.o.raw.update(warm_models=[], stats={'requests_served': 5, 'tokens_generated': 50})
        self.at(0)
        m = self.o.state['manager']
        self.assertNotIn('idleReload', m)  # not the hourly attempt
        self.assertEqual(self.events('manager'), [])
        self.at(30)
        self.assertEqual(len(self.loads()), 1)
        self.at(60)
        self.assertEqual(len(self.loads()), 2)
        self.assertEqual(self.o.state['manager']['idleReload']['at'], self.now + 60)


# --- 3: resting pairs and Macs without the local endpoint -------------------------------


class RestingSelectionTests(tm.Harness):
    def rest(self):
        self.o.raw.update(warm_models=[], stats={'requests_served': 5, 'tokens_generated': 50})

    def test_an_idle_unloaded_pair_is_loaded_again(self):
        self.o.read_options.return_value = (PAIR, ['--local-endpoint', '--port', '8000'], {})
        self.o.state['expectedModel'] = PAIR
        self.o.state['manager']['home'] = {'model': PAIR, 'source': 'current', 'at': self.now}
        self.o.raw.update(advertised_models=['a', 'b'], current_model='a', warm_models=['a', 'b'])
        self.o.live['hardware']['memoryAvailableGB'] = 40
        self.at(0)
        self.rest()
        self.at(60)
        self.assertEqual((self.restores(), self.loads()), ([], [PAIR]))
        self.assertEqual(self.o.perform_prewarm.call_args.kwargs['move'], 'restore')
        self.assertEqual(self.o.state['manager']['idleReload']['model'], PAIR)

    def test_a_resting_model_without_a_local_endpoint_counts_as_dark(self):
        self.o.read_options.return_value = ('a', ['--port', '8000'], {})
        self.rest()
        self.ticks(0, 540)
        self.assertIsNotNone(self.o.manager.dark_since)
        self.assertEqual((self.restores(), self.loads()), ([], []))
        self.at(600)
        self.assertEqual((self.restores(), self.loads()), (['a'], []))

    def test_a_resting_model_the_user_chose_to_free_still_rests(self):
        self.o.read_options.return_value = ('a', ['--port', '8000'], {})
        idle_policy(self, '[backend]\nidle_timeout_mins = 30\n')
        self.rest()
        self.ticks(0, 900)
        self.assertIsNone(self.o.manager.dark_since)
        self.assertEqual((self.restores(), self.loads()), ([], []))


# --- 4: cache recovery for a battery-exempt move ----------------------------------------


class CacheRecoveryOnBatteryTests(unittest.TestCase):
    def setUp(self):
        self.f = warm_fixtures.WarmupControllerTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.o, self.raw = self.f.o, self.f.raw
        self.raw.update(
            advertised_models=[NEMOTRON],
            current_model=NEMOTRON,
            warm_models=[],
            capacity={'gpu_memory_active_gb': 0, 'gpu_memory_cache_gb': 0},
        )
        self.o.raw = copy.deepcopy(self.raw)
        self.o.local = [{'id': NEMOTRON, 'estimated_memory_gb': 12}]
        self.o.catalog = [{'id': NEMOTRON, 'active': True, 'min_ram_gb': 24}]
        self.o.read_options.return_value = (NEMOTRON, ['--local-endpoint'], {})
        self.o.warmup = {}
        # a cold model blocked by 24 GB of file cache, on battery
        self.o.live['hardware'].update(memoryAvailableGB=10, cachedFilesGB=24)
        self.o.on_ac_power = Mock(return_value=False)
        self.o.stop.wait.side_effect = self.collect

    def collect(self, seconds):
        self.f.now += seconds
        self.o.live['at'] = self.o.identity_at = self.f.now
        return False

    def test_a_restore_on_battery_clears_the_file_cache(self):
        self.assertTrue(self.o.cache_recovery_needed(self.raw))
        with patch('optimizer.clear_file_cache') as purge, patch('optimizer.prewarm'):
            self.o.verify_started(NEMOTRON, self.raw['started_at'] - 100, 360, move='restore')
        purge.assert_called_once()
        self.assertLess(self.f.now - warm_fixtures.NOW, 360)  # no wait for the deadline

    def test_a_voluntary_move_on_battery_still_waits(self):
        with patch('optimizer.clear_file_cache') as purge, patch('optimizer.prewarm'):
            self.o.verify_started(NEMOTRON, self.raw['started_at'] - 100, 60)
        purge.assert_not_called()


# --- 5: the stall ladder goes on when the restart gate vetoes a restart ----------------


class StallLadderTests(unittest.TestCase):
    def test_a_vetoed_restart_leads_to_the_escape_and_then_the_hold(self):
        import test_stall_recovery as stall

        low = stall.network(active=20, warm=100)
        attempts = [{'at': stall.T0 + 300, 'step': 'probe'}]
        r = stall.check(stall.T0 + 480, attempts, net=low)
        self.assertEqual(r['step'], 'escape')
        attempts.append({'at': stall.T0 + 480, 'step': 'escape'})
        r = stall.check(stall.T0 + 480 + sr.ESCAPE_UNMOVED_SECONDS, attempts, net=low)
        self.assertEqual(r['step'], 'hold')
        self.assertIn('check Darkbloom', r['reason'])
        # With no demand readings at all, the same.
        r = stall.check(stall.T0 + 480, attempts[:1], net={'x': []})
        self.assertEqual(r['step'], 'escape')


# --- 7: the user's Start uses provider.toml's reserve and is not a voluntary move ------


class StartMemoryTests(unittest.TestCase):
    setUp = pc.ProviderTests.setUp
    tearDown = pc.ProviderTests.tearDown
    write_plist = pc.ProviderTests.write_plist
    run_cli = pc.ProviderTests.run_cli
    payload = pc.ProviderTests.payload
    dispatch = pc.ProviderTests.dispatch
    provider_calls = pc.ProviderTests.provider_calls

    def stopped(self):
        self.process.return_value = False
        self.o.service_disabled.return_value = True
        self.o.live['provider']['online'] = False

    def test_start_uses_the_configured_memory_reserve(self):
        self.stopped()
        self.o.live['hardware']['memoryAvailableGB'] = 25  # 22.9 GB needed with the default
        toml = self.o.home / '.config/darkbloom/provider.toml'
        toml.parent.mkdir(parents=True, exist_ok=True)
        toml.write_text('[provider]\nmemory_reserve_gb = 12\n')  # 28.5 GB needed
        self.assertEqual(self.dispatch(self.payload('provider-start'))['status'], 'failed')
        self.assertEqual(self.provider_calls(), [])
        toml.write_text('[provider]\nmemory_reserve_gb = 4\n')
        self.assertEqual(self.dispatch(self.payload('provider-start'))['status'], 'completed')

    def test_a_pair_start_is_not_refused_for_knobs_that_hold_voluntary_moves(self):
        self.stopped()
        self.args += ['--model', 'b']
        self.o.plist_path.write_bytes(
            pc.plistlib.dumps(
                {
                    'ProgramArguments': [str(self.o.binary), 'start', *self.args],
                    'EnvironmentVariables': {'DARKBLOOM_MEM_CAP_FRACTION': '0.8'},
                }
            )
        )
        self.raw['advertised_models'] = ['a', 'b']
        self.o.local.append(
            {'id': 'b', 'estimated_memory_gb': 10, 'size_bytes': 1000, 'template_render_ok': True}
        )
        self.o.catalog.append({'id': 'b', 'active': True, 'min_ram_gb': 16})
        self.assertEqual(self.dispatch(self.payload('provider-start'))['status'], 'completed')


# --- 8: a pair restore or pick warms up despite knobs for voluntary moves --------------


class PairPrewarmConfigTests(tm.Harness):
    def test_pair_prewarm_uses_the_voluntary_check_only_for_voluntary_moves(self):
        self.o.combo_config_error = Mock(
            side_effect=lambda voluntary=True: 'knob' if voluntary else None
        )
        self.o.raw.update(advertised_models=['a', 'b'], warm_models=['a', 'b'])
        raw = copy.deepcopy(self.o.raw)
        with patch('optimizer.time.time', return_value=self.now):
            self.assertEqual(self.o.prewarm_reason(raw, self.now), 'knob')
            for move in ('restore', 'home', 'manual'):
                self.assertNotEqual(self.o.prewarm_reason(raw, self.now, move=move), 'knob')


# --- 10: the result text follows the manager's state when the switch ends -------------


class TurnedOffMidSwitchTests(ManagerDecisionHarness):
    def test_turning_the_manager_off_during_preflight_reads_as_paused(self):
        self.o.state['manager']['home'] = manager.pin('b', self.now - 7200)
        self.o.state['lastSwitchAt'] = self.now - 4000
        self.o.purge_before_load = Mock(return_value=False)
        self.o.verify_local_target = Mock(
            side_effect=lambda *a, **k: self.o.state.update(mode='observe')
        )
        self.at(0)
        self.o.command.assert_not_called()
        self.assertIn('paused', self.o.detail)
        self.assertNotIn('Automatic control continues', self.o.detail)


# --- 11: idle-timeout settings as Darkbloom reads them ---------------------------------


class IdleTimeoutSettingTests(tm.Harness):
    def test_the_config_path_falls_back_like_darkbloom(self):
        home, options = self.o.home, ['--local-endpoint']
        app = home / 'Library/Application Support/darkbloom/provider.toml'
        app.parent.mkdir(parents=True, exist_ok=True)
        app.write_text('[backend]\nenabled_models = [ "b" ]\nidle_timeout_mins = 30\n')
        self.assertEqual(manager.config_path(home, options), app)
        self.assertTrue(manager.idle_unload_chosen(home, options))
        self.assertEqual(manager.toml_selection(home, options), 'b')
        legacy = home / '.config/eigeninference/provider.toml'
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text('[backend]\nidle_timeout_mins = 0\n')
        self.assertEqual(manager.config_path(home, options), app)  # the earlier path wins
        idle_policy(self, '[backend]\nidle_timeout_mins = 60\n')  # ~/.config/darkbloom first
        self.assertFalse(manager.idle_unload_chosen(home, options))

    def test_provider_toml_wins_over_the_launch_agent_flag(self):
        home, options = self.o.home, ['--local-endpoint']
        idle_policy(self, '[backend]\nidle_timeout_mins = 60\n')
        self.assertFalse(manager.idle_unload_chosen(home, options + ['--idle-timeout', '20']))
        idle_policy(self, '[backend]\nidle_timeout_mins = 20\n')
        self.assertTrue(manager.idle_unload_chosen(home, options + ['--idle-timeout', '0']))
        # The flag counts only when the TOML doesn't set the key.
        idle_policy(self, '[backend]\nenabled_models = [ "a" ]\n')
        self.assertTrue(manager.idle_unload_chosen(home, options + ['--idle-timeout', '20']))
        self.assertFalse(manager.idle_unload_chosen(home, options + ['--idle-timeout', '60']))


# --- 12: the restore grace uses load time without the drain ---------------------------


class LoadTimeTests(unittest.TestCase):
    setUp = fixtures.ControllerTests.setUp
    tearDown = fixtures.ControllerTests.tearDown
    setup_demand = controller.DemandControllerTests.setup_demand

    def test_a_switch_records_its_load_time_without_the_drain(self):
        self.setup_demand()
        real = time.time
        offset = [0]

        def drain(*args):  # `darkbloom start` on 0.9.9+ returns after a 500-s drain
            offset[0] += 500

        def load(*args, **kwargs):
            offset[0] += 100
            return True

        self.o.command = Mock(side_effect=drain)
        self.o.verify_started = Mock(side_effect=load)
        self.o.state['pending'] = {'model': 'b', 'kind': 'demand'}
        with patch('optimizer.time.time', side_effect=lambda: real() + offset[0]):
            self.o.switch('a', 'b', 'acct', self.live['device'])
        self.o.command.assert_called_once()
        (at, seconds), = self.o.state['manager']['loadSeconds']['b']
        self.assertAlmostEqual(seconds, 100, delta=5)
        downtime = self.h.db.execute(
            "SELECT downtime FROM opt_events WHERE kind='switched' AND model='b'"
        ).fetchone()[0]
        self.assertGreaterEqual(downtime, 600)  # the event keeps the whole downtime


if __name__ == '__main__':
    unittest.main()
