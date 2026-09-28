"""Manager control path (optimizer audit B6-B9, Sep 27 2026).

B6 an idle-unloaded model is loaded again, and a resting non-home model can be left;
B7 the freed-memory credit uses measured resident memory; B8 a readiness recheck just
before the local warm-up waits instead of failing; B9 returns home, restores and manual
picks are not held by rules meant for voluntary moves. Every provider command is mocked.
"""

import copy
import json
import subprocess
import unittest
from unittest.mock import Mock, patch

import manager
import test_manager as tm
import test_manual_timeout as timeout_fixtures
import test_prewarm as warm_fixtures
from demand_optimizer import policy
from demand_targets import GEMMA
from optimizer import DemandDeferred, Optimizer, launch_signature, memory_budget, session_key
from prewarm import WarmupDeferred
from test_demand_optimizer import decision as legacy_upgrade

NEMOTRON = 'nvidia-nemotron-3.5-lightning'


def served(test, model, minutes=10):
    """`model` served complete ready minutes on this Mac (Optimizer.served_here)."""
    with test.h.lock:
        test.h.db.executemany(
            'INSERT INTO opt_ready_minutes VALUES(?,?,?,?,?,?,?,?)',
            [
                ('acct', test.live['device'], int(test.now) - 3600 - 60 * i, model, 60, 1, 10, 30)
                for i in range(minutes)
            ],
        )
        test.h.db.commit()
    test.o.proof_minutes = {}


def idle_policy(test, text):
    """Write the harness Mac's provider.toml (Darkbloom's idle-memory setting lives there)."""
    toml = test.o.home / '.config/darkbloom/provider.toml'
    toml.parent.mkdir(parents=True, exist_ok=True)
    toml.write_text(text)


def run(test, kind, model, at, previous='a'):
    with test.h.lock:
        test.h.db.execute(
            """INSERT INTO demand_switch_runs
            (account,device,at,previous,model,payload,reserved_seconds,completed_at,result,downtime)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                'acct',
                test.live['device'],
                at,
                previous,
                model,
                json.dumps({'kind': kind}),
                120,
                at + 60,
                'switched',
                60,
            ),
        )
        test.h.db.commit()


# --- B6: idle unload -----------------------------------------------------------------


class IdleUnloadTests(tm.Harness):
    """Darkbloom's default idle timeout (60 min, provider.toml unset on Andrew's Mac) unloads a
    model that served; base rewards need a loaded model at each 5-minute settlement."""

    def rest(self):
        self.o.raw.update(warm_models=[], stats={'requests_served': 5, 'tokens_generated': 50})

    def test_idle_unloaded_home_is_loaded_again_at_once_quietly_and_at_most_hourly(self):
        switched_at = self.o.state['lastSwitchAt']
        self.rest()
        self.at(0)
        # Through the local endpoint (no restart), in the tick that noticed it.
        self.assertEqual((self.restores(), self.loads()), ([], ['a']))
        self.assertEqual(self.o.perform_prewarm.call_args.kwargs['move'], 'restore')
        self.assertIn(
            'Loaded a again after Darkbloom unloaded it while idle', self.events('manager')[-1]
        )
        # Not a switch or a restore: no notice, switch result or new switch time.
        self.assertEqual(self.events('manager-notice') + self.events('recovered'), [])
        self.assertNotIn('lastSwitchResult', self.o.state)
        self.assertEqual(self.o.state['lastSwitchAt'], switched_at)
        self.assertEqual(self.o.state['manager']['idleReload'], {'model': 'a', 'at': self.now})
        self.assertNotIn('pending', self.o.state)
        # Unloaded again within the hour (not Darkbloom's 60-minute default): no loop.
        self.ticks(60, 3540)
        self.assertEqual(self.loads(), ['a'])
        self.assertIsNone(self.o.manager.dark_since)  # resting is not dark
        self.at(3600)
        self.assertEqual(self.loads(), ['a', 'a'])
        self.assertEqual(self.o.state['mode'], 'demand')

    def test_an_idle_timeout_the_user_chose_is_left_alone(self):
        home, options = self.o.home, ['--local-endpoint']
        self.assertFalse(manager.idle_unload_chosen(home, options))  # Darkbloom's default
        for text, chosen in (
            ('[backend]\nidle_timeout_mins = 60\n', False),  # the default darkbloom start writes
            ('[backend]\nenabled_models = [ "a" ]\nidle_timeout_mins=30 # free when idle\n', True),
            ('backend.idle_timeout_mins = 45\n', True),
            ('[backend]\nidle_timeout_mins = "soon"\n', True),  # unreadable counts as chosen
            ('[backend]\nidle_timeout_mins = 0\n', False),  # always ready
            ('[provider]\nidle_timeout_mins = 60\n[backend]\npreload_models = []\n', False),
        ):
            with self.subTest(text=text):
                idle_policy(self, text)
                self.assertEqual(manager.idle_unload_chosen(home, options), chosen)
        self.assertTrue(manager.idle_unload_chosen(home, options + ['--idle-timeout', '20']))
        self.assertFalse(manager.idle_unload_chosen(home, options + ['--idle-timeout', '0']))
        self.assertFalse(manager.idle_unload_chosen(home, options + ['--idle-timeout', '60']))
        idle_policy(self, '[backend]\nidle_timeout_mins = 30\n')
        self.rest()
        self.ticks(0, 900)
        self.assertEqual((self.restores(), self.loads()), ([], []))
        self.assertNotIn('idleReload', self.o.state['manager'])

    def test_a_reload_that_cannot_run_now_is_not_sent(self):
        self.rest()
        self.o.read_options.return_value = ('a', ['--port', '8000'], {})  # no local endpoint
        self.at(0)
        self.o.read_options.return_value = ('a', ['--local-endpoint', '--port', '8000'], {})
        self.o.live['hardware']['memoryAvailableGB'] = 2  # the user opened a big app
        self.at(15)
        self.assertEqual((self.restores(), self.loads()), ([], []))
        self.o.live['hardware']['memoryAvailableGB'] = 24
        self.at(30)
        self.assertEqual(self.loads(), ['a'])

    def test_a_deferred_reload_is_retried_and_does_not_count_as_an_attempt(self):
        self.o.perform_prewarm = Mock(
            side_effect=[
                WarmupDeferred('Waiting for fresh local readings.', code='readiness-changed'),
                True,
            ]
        )
        self.rest()
        self.at(0)
        self.assertNotIn('idleReload', self.o.state['manager'])
        self.assertEqual(self.events('manager'), [])
        self.at(30)  # the 60-second retry wait
        self.assertEqual(len(self.loads()), 1)
        self.at(60)
        self.assertEqual(len(self.loads()), 2)
        self.assertEqual(self.o.state['manager']['idleReload']['at'], self.now + 60)


class RestingExcursionTests(tm.Harness):
    """An excursion target that Darkbloom unloaded while idle: max duration, early exit and
    the return home are no longer suppressed by the readiness gate."""

    def setUp(self):
        super().setUp()
        del self.o.tick_demand  # the real demand tick and switch path
        self.o.demand_auto.evaluate = Mock(side_effect=self.legacy)
        self.o.demand_auto.evidence = Mock(return_value=({}, {}))
        self.o.verify_local_target = Mock()

    def legacy(self, account, device, rows, current, raw, rules, now, *args, **kwargs):
        d = legacy_upgrade()
        d.update(at=now, currentModel=current, policy=rules, target=None, kind=None)
        return d

    def away(self, started):
        """Serving excursion target b since `started`; it served, then was unloaded."""
        self.o.read_options.return_value = ('b', ['--local-endpoint', '--port', '8000'], {})
        self.o.state['expectedModel'] = 'b'
        self.o.state['lastSwitchAt'] = self.now + started
        self.o.state['manager']['excursion'] = {
            'target': 'b',
            'from': 'a',
            'startedAt': self.now + started,
            'leftAt': self.now + started,
            'predictedUsdPerHour': 0.2,
            'reason': 'fixture',
            'maxMinutes': 240,
        }
        self.o.raw.update(
            advertised_models=['b'],
            current_model='b',
            warm_models=[],
            stats={'requests_served': 5, 'tokens_generated': 50},
        )

    def test_resting_excursion_target_goes_home_when_the_excursion_ends(self):
        idle_policy(self, '[backend]\nidle_timeout_mins = 30\n')  # chosen: no reload
        self.away(-241 * 60)
        self.assertFalse(self.o.tracking(self.o.raw, self.now)['counting'])
        self.at(0)
        self.assertEqual([c.args[0] for c in self.o.command.call_args_list], ['a'])
        self.assertEqual(self.o.verify_started.call_args.kwargs['move'], 'home')
        m = self.o.state['manager']
        self.assertNotIn('excursion', m)
        # Resting ends it (below); the max duration was due as well.
        self.assertEqual(m['lastExcursion']['endCode'], 'idle')
        self.assertEqual(self.o.state['expectedModel'], 'a')
        run = self.o.demand_auto.runs('acct', self.live['device'], self.now + 60)[0]
        self.assertEqual((run['result'], run['decision']['kind']), ('switched', 'home'))

    def test_resting_excursion_target_ends_the_excursion_instead_of_a_reload(self):
        """An idle-unloaded excursion target is the strongest sign to end it: no reload, the
        Mac goes home (the return home works from a resting model)."""
        self.away(-600)
        self.at(0)
        self.assertEqual((self.o.command.call_args_list, self.loads()), ([], []))
        self.assertEqual(self.o.state['manager']['excursion']['endCode'], 'idle')
        self.at(15)
        self.assertEqual(self.restores(), ['a'])
        self.assertEqual(self.loads(), [])
        m = self.o.state['manager']
        self.assertNotIn('excursion', m)
        self.assertEqual(m['lastExcursion']['endCode'], 'idle')
        self.assertIn('unloaded it after it sat idle', m['lastExcursion']['endReason'])

    def test_leaving_home_still_needs_a_counting_model(self):
        idle_policy(self, '[backend]\nidle_timeout_mins = 30\n')
        self.o.raw.update(warm_models=[], stats={'requests_served': 5, 'tokens_generated': 50})
        proposal = {'target': 'b', 'reason': 'fixture.', 'predictedUsdPerHour': 0.3}
        with patch('manager.manager_excursion', Mock(return_value=proposal)):
            for offset in range(0, 601, 60):
                self.at(offset)
        self.o.command.assert_not_called()
        # The user chose idle unloading: the status says so (test_bug_matrix ChosenIdleUnload).
        self.assertTrue(
            self.o.detail.startswith('Darkbloom unloaded a after it sat idle'), self.o.detail
        )


# --- B7: freed-memory credit ----------------------------------------------------------


class MemoryCreditTests(tm.Harness):
    def test_1850_qwen_switch_is_refused_on_measured_resident_memory(self):
        """Sep 27 18:50, 48 GB: free + inactive 20.64 GiB (vm_stat), gemma resident 14.76 GiB
        (its CLI estimate is 17.44, weights x 1.2). qwen3.5-35b needs 23.32 + 4.8 + 5.5 + 1
        = 34.62 GiB, 35.62 with the 1 GiB headroom. Measured: 35.40, refused. The padded
        estimate (38.08) admitted it, and such switches then failed after the restart."""
        self.o.local = [
            {'id': m, 'estimated_memory_gb': gb, 'size_bytes': 1, 'template_render_ok': True}
            for m, gb in ((GEMMA, 17.44), (tm.QWEN, 23.32))
        ]
        self.o.catalog = [{'id': m, 'active': True, 'min_ram_gb': 36} for m in (GEMMA, tm.QWEN)]
        self.o.raw.update(
            advertised_models=[GEMMA],
            warm_models=[GEMMA],
            capacity={
                'total_memory_gb': 48,
                'gpu_memory_active_gb': 14.76,
                'gpu_memory_cache_gb': 0,
            },
        )
        live = copy.deepcopy(self.o.live)
        live['provider']['memoryGB'] = 14.76
        live['hardware'].update(memoryTotalGB=48, memoryAvailableGB=20.64)
        budget = self.o.selection_budget(tm.QWEN, live, self.o.raw)
        self.assertAlmostEqual(budget['afterUnloadGB'], 35.40)
        self.assertAlmostEqual(budget['requiredGB'], 34.62)
        why = manager.admission({'available': True, 'loadBudget': budget}, policy())
        self.assertEqual(
            why,
            'waiting for enough free memory (35.6 GB needed, 35.4 GB available after unloading).',
        )

    def test_the_padded_estimate_counts_only_when_resident_memory_is_not_reported(self):
        hardware = {'memoryTotalGB': 48, 'memoryAvailableGB': 20}
        for resident, held, freed in (
            (14.76, 17.44, 14.76),
            (0, 17.44, 17.44 / 1.2),
            (0, float('nan'), 0),
        ):
            with self.subTest(resident=resident, held=held):
                budget = memory_budget(hardware, {'memoryGB': resident}, 'x', 10, 1, held)
                self.assertAlmostEqual(budget['afterUnloadGB'], 20 + freed + 1)
        budget = memory_budget(hardware, {'memoryGB': 40}, 'x', 10, 0, 17.44)
        self.assertEqual(budget['afterUnloadGB'], 48)  # never more than the Mac has

    def test_each_switch_logs_the_projection_against_the_new_session(self):
        self.o.verification_memory = (21.3, 2.5)
        with self.assertLogs('bloom.optimizer', 'INFO') as logs:
            self.o.log_memory_projection(tm.QWEN, {'afterUnloadGB': 35.4})
        self.assertIn('projected 35.40 GB free after the unload', logs.output[0])
        self.assertIn('saw 21.30 GB free with 2.50 GB loaded', logs.output[0])
        self.o.verification_memory = None
        with patch('optimizer.log') as log:
            self.o.log_memory_projection(tm.QWEN, {'afterUnloadGB': 35.4})
        log.info.assert_not_called()


# --- B8: pre-warm recheck -------------------------------------------------------------


class PrewarmRecheckTests(unittest.TestCase):
    """verify_started waits on any pre-warm reason; the recheck inside perform_prewarm used to
    turn the same reasons into a hard 'readiness-changed' failure."""

    def setUp(self):
        self.f = warm_fixtures.WarmupControllerTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.o, self.raw = self.f.o, self.f.raw
        self.raw.update(
            advertised_models=[NEMOTRON],
            current_model=NEMOTRON,
            warm_models=[],
            capacity={'gpu_memory_active_gb': 0.4},
        )
        self.o.raw = copy.deepcopy(self.raw)
        self.o.local = [{'id': NEMOTRON, 'estimated_memory_gb': 12}]
        self.o.catalog = [{'id': NEMOTRON, 'active': True, 'min_ram_gb': 24}]
        self.o.read_options.return_value = (NEMOTRON, ['--local-endpoint'], {})
        self.o.warmup = {}
        self.o.stop.wait.side_effect = self.collect
        self.previous = self.raw['started_at'] - 100
        self.started = self.f.now

    def collect(self, seconds):
        """Samples and roster checks keep arriving while verification waits."""
        self.f.now += seconds
        self.o.live['at'] = self.o.identity_at = self.f.now
        return False

    def decoded(self, *args, **kwargs):
        self.raw['warm_models'] = [NEMOTRON]

    def racing(self, change):
        real = self.o.perform_prewarm
        first = []

        def perform(*args, **kwargs):
            if not first:
                first.append(True)
                change()
            return real(*args, **kwargs)

        self.o.perform_prewarm = Mock(side_effect=perform)

    def test_sep25_0012_stale_reading_at_prewarm_is_not_a_failed_switch(self):
        """00:09:46 gemma -> nemotron; at 00:12 the switch 'failed' with "Waiting for fresh
        local readings." (the live snapshot was just over 10 s old as the local request was
        due), gemma was restored and switching paused for 7 h. At 07:11 the same switch
        succeeded in about a minute."""

        def stall():
            self.o.live['at'] = self.f.now - 10.5

        self.racing(stall)
        with patch('optimizer.prewarm', side_effect=self.decoded) as local:
            self.assertTrue(self.o.verify_started(NEMOTRON, self.previous, timeout=120))
        local.assert_called_once()
        self.assertEqual(self.o.perform_prewarm.call_count, 2)
        self.assertIsNone(self.o.verification_failure)
        self.assertEqual(self.o.warmup['status'], 'ready')
        self.assertLess(self.f.now - self.started, 60)
        self.assertEqual(self.o.verification_memory, (24, 0.4))  # for the memory log

    def test_memory_still_being_freed_waits_within_the_manager_memory_wait(self):
        def short():
            self.o.live['hardware']['memoryAvailableGB'] = 20  # 23.3 GB needed

        self.racing(short)
        freed = self.collect

        def collect(seconds):
            if self.f.now - self.started >= 60:
                self.o.live['hardware']['memoryAvailableGB'] = 30
            return freed(seconds)

        self.o.stop.wait.side_effect = collect
        with patch('optimizer.prewarm', side_effect=self.decoded) as local:
            self.assertTrue(
                self.o.verify_started(NEMOTRON, self.previous, timeout=360, memory_seconds=180)
            )
        local.assert_called_once()
        self.assertGreater(self.f.now - self.started, 60)

    def test_memory_short_past_the_manager_wait_fails(self):
        def short():
            self.o.live['hardware']['memoryAvailableGB'] = 20

        self.racing(short)
        with patch('optimizer.prewarm') as local:
            self.assertFalse(
                self.o.verify_started(NEMOTRON, self.previous, timeout=360, memory_seconds=180)
            )
        local.assert_not_called()
        self.assertEqual(self.o.verification_failure.code, 'readiness-timeout')
        self.assertIn('enough available memory', str(self.o.verification_failure))
        self.assertGreaterEqual(self.f.now - self.started, 180)
        self.assertLess(self.f.now - self.started, 220)

    def test_a_stale_recheck_is_deferred_not_failed(self):
        self.o.live['at'] = self.f.now - 11
        with self.assertRaises(WarmupDeferred) as caught:
            self.o.perform_prewarm(NEMOTRON, copy.deepcopy(self.raw), ['--local-endpoint'])
        self.assertEqual(caught.exception.code, 'readiness-changed')
        self.assertEqual(self.o.warmup['status'], 'waiting')

    def test_a_failed_recheck_before_cache_cleanup_waits_and_clears_nothing(self):
        self.raw['capacity'] = {'gpu_memory_active_gb': 0, 'gpu_memory_cache_gb': 0}
        self.o.raw = copy.deepcopy(self.raw)
        self.o.live['hardware'].update(memoryAvailableGB=10, cachedFilesGB=24)
        self.assertTrue(self.o.cache_recovery_needed(self.raw))
        self.o.live['at'] = self.f.now - 11
        with patch('optimizer.clear_file_cache') as purge:
            with self.assertRaises(WarmupDeferred) as caught:
                self.o.recover_file_cache(self.raw, NEMOTRON, ['--local-endpoint'])
        purge.assert_not_called()
        self.assertEqual(caught.exception.code, 'readiness-changed')
        self.assertNotIn('cacheRecovery', self.o.state)


# --- B9: returns home, restores and manual picks ----------------------------------------


class ReturnHomeGateTests(tm.Harness):
    decision = tm.ControllerTests.decision

    def setUp(self):
        super().setUp()
        del self.o.tick_demand  # the real demand tick
        self.o.tracking = Mock(return_value={'counting': True})
        self.o.switch = Mock()
        self.o.state['lastSwitchAt'] = self.now - 4000

    def decide(self, kind):
        value = (
            self.decision('b', 'home', 'Returning to home model b.', 'b')
            if kind == 'home'
            else self.decision('b', 'excursion', 'Excursion to b: fixture.', 'a')
        )
        self.o.demand_decision = Mock(
            side_effect=lambda now, *a, **k: {**copy.deepcopy(value), 'at': now, 'sourceAt': now}
        )

    def dispatched(self, kind, offset=0):
        self.decide(kind)
        self.o.switch.reset_mock()
        self.o.state.pop('demandProposal', None)
        self.at(offset)
        return self.o.switch.called

    def test_battery_holds_an_excursion_but_not_a_return_home(self):
        self.o.on_ac_power.return_value = False
        self.assertFalse(self.dispatched('excursion'))
        self.assertEqual(self.o.detail, 'Model switching waits while the Mac is on battery power.')
        self.assertTrue(self.dispatched('home'))
        self.assertEqual(self.o.state['pending']['demandKind'], 'home')

    def test_heat_line_holds_only_an_excursion_and_thermal_state_holds_both(self):
        self.o.live['hardware']['gpuTemp'] = 96
        self.assertFalse(self.dispatched('excursion'))
        self.assertIn('The Mac is hot', self.o.detail)
        self.assertTrue(self.dispatched('home'))
        self.o.state.pop('pending')
        self.o.live['hardware'].update(gpuTemp=60, thermal='Serious')
        self.assertFalse(self.dispatched('home'))
        self.assertIn('The Mac is hot', self.o.detail)

    def test_stale_earnings_and_network_feeds_hold_only_an_excursion(self):
        real = tm.Harness.at

        def stale(test, offset, **kwargs):
            test.o.live['earnings']['status'] = 'stale'
            test.net.snapshot.return_value['capacity']['status'] = 'stale'
            return real(test, offset, **kwargs)

        with patch.object(tm.Harness, 'at', stale):
            self.assertFalse(self.dispatched('excursion'))
            self.assertEqual(self.o.detail, 'Waiting for current earnings before changing models.')
            self.assertTrue(self.dispatched('home'))

    def test_overdue_catalog_holds_a_return_home_only_to_a_model_never_served_here(self):
        self.o.state['manager']['home'] = {'model': 'b', 'source': 'history', 'at': 0}
        self.o.discovery_error = (
            'Could not refresh locally available models and the network catalog.'
        )
        self.assertFalse(self.dispatched('home'))
        self.assertEqual(self.o.detail, 'Waiting for a fresh model catalog.')
        served(self, 'b')
        self.assertTrue(self.dispatched('home'))
        self.o.state.pop('pending')
        self.assertFalse(self.dispatched('excursion'))

    def test_a_return_home_skips_confirmation_and_minimum_run_but_an_excursion_waits(self):
        self.o.state['lastSwitchAt'] = self.now - 60  # 30-minute minimum run
        self.assertTrue(self.dispatched('home'))
        self.o.state.pop('pending')
        self.decide('excursion')
        self.o.switch.reset_mock()
        for offset in range(0, 1681, 60):
            self.at(offset)
        self.o.switch.assert_not_called()
        self.assertIn('Waiting for the 30-minute minimum run to finish.', self.o.detail)
        self.at(1740)
        self.o.switch.assert_called_once()
        self.assertEqual(self.o.state['pending']['demandKind'], 'excursion')

    def test_an_excursion_still_confirms_for_five_minutes(self):
        self.decide('excursion')
        for offset in range(0, 241, 60):
            self.at(offset)
        self.o.switch.assert_not_called()
        self.assertIn('Confirming for 5 minutes before switching.', self.o.detail)
        self.at(300)
        self.o.switch.assert_called_once()

    def test_unknown_power_source_is_not_battery(self):
        o = Optimizer(self.h, self.net, self.tmp.name, self.o.stop, Mock())
        for result, expected in (
            (subprocess.TimeoutExpired('pmset', 3), None),
            (OSError('pmset missing'), None),
            (Mock(stdout="Now drawing from 'AC Power'\n"), True),
            (Mock(stdout="Now drawing from 'Battery Power'\n"), False),
        ):
            with self.subTest(result=result):
                o.runner = (
                    Mock(side_effect=result)
                    if isinstance(result, Exception)
                    else Mock(return_value=result)
                )
                self.assertIs(o.on_ac_power(), expected)
        self.o.on_ac_power.return_value = None  # unknown: the excursion goes on confirming
        self.assertFalse(self.dispatched('excursion'))
        self.assertIn('Confirming for 5 minutes before switching.', self.o.detail)


class DailyLimitTests(tm.Harness):
    def test_only_excursion_starts_count_toward_the_daily_limit(self):
        for i, kind in enumerate(('explore', 'earnings', 'explore', 'home', 'home', 'recovery')):
            run(self, kind, 'b', self.now - 3600 * (i + 1))
        rules = policy({'maxSwitchesPerDay': 2})
        auto = self.o.demand_auto
        self.assertIsNone(auto.excursion_limit('acct', self.live['device'], rules, self.now))
        run(self, 'excursion', 'b', self.now - 7200)
        run(self, 'excursion', 'b', self.now - 90000)  # older than 24 h
        self.assertIsNone(auto.excursion_limit('acct', self.live['device'], rules, self.now))
        run(self, 'excursion', 'b', self.now - 600)
        self.assertEqual(
            auto.excursion_limit('acct', self.live['device'], rules, self.now),
            'The daily switch limit does not allow another excursion.',
        )
        d = {
            **legacy_upgrade(),
            'at': self.now,
            'currentModel': 'a',
            'target': 'b',
            'policy': rules,
        }
        with self.assertRaisesRegex(ValueError, 'daily switch limit'):
            auto.begin_manager('acct', self.live['device'], {**d, 'kind': 'excursion'}, self.now)
        self.assertTrue(
            auto.begin_manager('acct', self.live['device'], {**d, 'kind': 'home'}, self.now)
        )

    def test_a_refused_excursion_defers_before_cleanup_listing_or_catalog(self):
        self.o.state['demandPolicy'] = policy({'maxSwitchesPerDay': 1})
        run(self, 'excursion', 'c', self.now - 600)
        _, options, environment = self.o.read_options()
        self.o.state['pending'] = {
            'model': 'b',
            'previous': 'a',
            'at': self.now,
            'kind': 'demand',
            'demandKind': 'excursion',
            'session': session_key(self.o.raw),
            'launchSignature': launch_signature(options, environment),
        }
        self.o.purge_before_load = Mock()
        self.o.verify_local_target = Mock()
        self.o.demand_decision = Mock()
        self.o.switch('a', 'b', 'acct', self.live['device'])
        for step in (
            self.o.purge_before_load,
            self.o.verify_local_target,
            self.o.demand_decision,
            self.o.command,
        ):
            step.assert_not_called()
        self.assertIn('daily switch limit', self.events('switch-deferred')[-1])
        self.assertEqual(self.o.state['mode'], 'demand')
        self.assertNotIn('pending', self.o.state)


class RestoreGateTests(tm.Harness):
    def test_watchdog_restore_goes_ahead_on_battery_and_above_the_heat_line(self):
        self.o.on_ac_power.return_value = False
        self.o.live['hardware']['gpuTemp'] = 97
        self.dark()
        self.ticks(0, 600)
        self.assertEqual((self.restores(), self.loads()), ([], ['a']))
        self.assertEqual(self.o.perform_prewarm.call_args.kwargs['move'], 'restore')
        self.assertIn('Watchdog restored a', self.events('manager-notice')[0])

    def test_a_restart_restore_goes_ahead_on_battery(self):
        self.o.on_ac_power.return_value = False
        self.o.state['manager']['home']['model'] = 'b'
        self.dark()
        self.ticks(0, 600)
        self.assertEqual(self.restores(), ['b'])
        self.assertEqual(self.o.verify_started.call_args.kwargs['move'], 'restore')

    def test_thermal_state_still_holds_a_restore(self):
        self.o.live['hardware']['thermal'] = 'Critical'
        self.dark()
        self.ticks(0, 900)
        self.assertEqual((self.restores(), self.loads()), ([], []))
        self.assertIn('The Mac is hot', self.o.detail)

    def test_an_overdue_catalog_holds_a_restore_only_to_a_model_never_served_here(self):
        self.o.discovery_error = (
            'Could not refresh locally available models and the network catalog.'
        )
        self.dark()
        self.ticks(0, 660)
        self.assertEqual(self.loads(), [])
        self.assertIn('Waiting for a fresh model catalog', self.o.detail)
        served(self, 'a')
        self.at(720)
        self.assertEqual(self.loads(), ['a'])

    def test_known_local_target_needs_a_home_or_restore_move_and_history(self):
        now, live = self.now, self.o.live
        self.assertFalse(self.o.known_target('restore', 'a', live, now))
        served(self, 'a')
        self.assertTrue(self.o.known_target('restore', 'a', live, now))
        self.assertTrue(self.o.known_target('home', 'a', live, now))
        self.assertFalse(self.o.known_target('manual', 'a', live, now))
        self.assertFalse(self.o.known_target(None, 'a', live, now))
        self.assertFalse(self.o.known_target('home', 'b', live, now))

    def test_return_home_keeps_the_last_catalog_when_a_fetch_fails(self):
        served(self, 'b')
        self.net.fetch.side_effect = OSError('offline')
        catalog = copy.deepcopy(self.o.catalog)
        self.o.discovery_at = self.now - 700
        self.o.verify_local_target('b', ['--local-endpoint'], catalog_optional=True)
        self.assertEqual((self.o.catalog, self.o.discovery_at), (catalog, self.now - 700))
        with self.assertRaises(DemandDeferred):
            self.o.verify_local_target('b', ['--local-endpoint'])
        self.o.local[1]['template_render_ok'] = False  # files must still check out
        self.o.runner.return_value.stdout = json.dumps({'models': self.o.local})
        with self.assertRaises(DemandDeferred):
            self.o.verify_local_target('b', ['--local-endpoint'], catalog_optional=True)


class ManualPickTests(unittest.TestCase):
    setUp = timeout_fixtures.ManualTimeoutTests.setUp
    tearDown = timeout_fixtures.ManualTimeoutTests.tearDown
    manual_payload = timeout_fixtures.ManualTimeoutTests.manual_payload
    queue = timeout_fixtures.ManualTimeoutTests.queue
    arm = timeout_fixtures.ManualTimeoutTests.arm
    run_switch = timeout_fixtures.ManualTimeoutTests.run_switch

    def test_explicit_pick_goes_ahead_on_battery_and_above_the_heat_line(self):
        self.queue()
        self.o.on_ac_power.return_value = False
        self.o.live['hardware']['gpuTemp'] = 99
        self.o.switch = Mock()
        self.o.tick(self.now)
        self.o.worker.join(timeout=2)
        self.o.switch.assert_called_once_with('a', 'b', 'acct', self.live['device'])
        del self.o.switch
        self.arm()
        self.run_switch()
        self.o.command.assert_called_once()
        self.assertEqual(self.o.verify_started.call_args.kwargs['move'], 'manual')
        self.assertEqual(self.o.state['manualResult']['status'], 'completed')

    def test_thermal_state_still_holds_an_explicit_pick(self):
        self.queue()
        self.o.live['hardware']['thermal'] = 'Serious'
        self.o.switch = Mock()
        self.o.tick(self.now)
        self.o.switch.assert_not_called()
        self.assertIn('The Mac is hot', self.o.detail)


if __name__ == '__main__':
    unittest.main()
