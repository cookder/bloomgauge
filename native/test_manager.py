"""Manager strategy: hold home, no blind trials, recover instead of pausing, watchdog."""

import copy
import threading
import unittest
import uuid
from unittest.mock import Mock, patch

import manager
from demand_fallback import MODEL as GPT_OSS
from demand_optimizer import POLICY_REVISION, PREVIOUS_DEFAULTS, policy, upgraded_policy
from demand_targets import GEMMA
from model_combinations import pair_candidates, selection_key
from optimizer import Optimizer, session_key
from prewarm import WarmupError
import test_demand_controller as controller
import test_optimizer as fixtures
import test_stall_recovery as stall_fixtures
from test_demand_fallback import choose as legacy_trial, rows as trial_rows
from test_demand_optimizer import NOW, decision as legacy_upgrade

# Andrew's saved revision-2 policy (optimizer-settings, Sep 27), before managerStrategy existed.
SAVED = {
    'minRunMinutes': 30,
    'confirmationMinutes': 5,
    'improvementPercent': 20,
    'planningMinutes': 60,
    'minimumNetUsd': 0.02,
    'maxSwitchesPerDay': 24,
    'maxDowntimeMinutes': 60,
    'memoryHeadroomGB': 1,
    'idleEscapeMinutes': 20,
    'trialMinutes': 15,
    'trialCooldownMinutes': 30,
    'fallbackEnabled': 1,
    'targetUsdPerHour': 0.2,
    'baselineLearningEnabled': 1,
    'protectUsdPerHour': 0.12,
    'learningMinutesPerDay': 180,
}


def minutes(rate, hours, end=NOW):
    count = int(hours * 60)
    return [{'at': end - (count - i) * 60, 'seconds': 60, 'usd': rate / 60} for i in range(count)]


def row(model, available=True, after=40, required=30):
    return {
        'id': model,
        'available': available,
        'selected': True,
        'reason': None if available else 'Not active in the network catalog',
        'loadBudget': {'afterUnloadGB': after, 'requiredGB': required},
    }


def context(current, home='a', rows=None, blocked=None, source='history'):
    return {
        'current': current,
        'home': {'model': home, 'source': source, 'at': NOW - 90000, 'usdPerHour': 0.108}
        if home
        else None,
        'rows': {r['id']: r for r in rows or [row('a'), row('b'), row(GPT_OSS), row('new')]},
        'rules': policy(),
        'blocked': blocked or {},
    }


def managed(legacy, current='a', home='a', state=None, **kwargs):
    state = state or {'mode': 'demand', 'demandPolicy': policy()}
    return manager.decide(legacy, state, context(current, home, **kwargs), NOW)


def holds(test, d, home='a'):
    test.assertIsNone(d['target'])
    test.assertIsNone(d['kind'])
    test.assertIsNone(d['explorationTrigger'])
    test.assertFalse(d['escapeReady'])
    test.assertIsNone(d['paidAlternative'])
    test.assertFalse(any(r['confirmationEligible'] for r in d['opportunities']))
    test.assertTrue(d['reason'].startswith('Holding home model ' + home), d['reason'])
    test.assertEqual(d['manager']['action'], 'hold')


class PolicyMigrationTests(unittest.TestCase):
    def test_saved_revision_two_policy_loads_as_manager_and_keeps_every_choice(self):
        result = upgraded_policy(copy.deepcopy(SAVED), 2)
        self.assertEqual(POLICY_REVISION, 3)
        self.assertEqual(result, {**SAVED, 'managerStrategy': 1, 'managerExcursions': 1})
        self.assertEqual(manager.strategy(result), 'manager')

    def test_only_revision_one_defaults_are_upgraded(self):
        old = {**policy(), **PREVIOUS_DEFAULTS}
        old.pop('managerStrategy')
        self.assertEqual(upgraded_policy(old, 0)['minRunMinutes'], 30)
        # A revision-2 user who chose 60 minutes keeps it through the revision-3 bump.
        self.assertEqual(upgraded_policy(old, 2)['minRunMinutes'], 60)
        self.assertEqual(upgraded_policy({**SAVED, 'managerStrategy': 0}, 3)['managerStrategy'], 0)

    def test_strategy_accepts_only_zero_or_one(self):
        self.assertEqual(manager.strategy(policy({'managerStrategy': 0})), 'legacy')
        for bad in (2, True, 'manager', 0.5, None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                policy({'managerStrategy': bad})

    def test_reopened_demand_plan_stays_on_and_unknown_keys_are_dropped(self):
        f = fixtures.ControllerTests('setUp')
        f.setUp()
        self.addCleanup(f.tearDown)
        f.o.state.update(mode='demand', demandPolicy=copy.deepcopy(SAVED), demandPolicyRevision=2)
        f.o.save()
        reopened = Optimizer(f.h, f.net, f.tmp.name, threading.Event(), Mock())
        self.assertEqual(reopened.state['mode'], 'demand')
        self.assertEqual(
            reopened.state['demandPolicy'], {**SAVED, 'managerStrategy': 1, 'managerExcursions': 1}
        )
        self.assertEqual(reopened.state['demandPolicyRevision'], 3)
        # A key this build doesn't know (another build saved it) no longer turns the manager
        # off after an update: it is dropped and every known choice kept (test_field_reports).
        f.o.state['demandPolicy'] = {**SAVED, 'futureKey': 1}
        f.o.save()
        reopened = Optimizer(f.h, f.net, f.tmp.name, threading.Event(), Mock())
        self.assertEqual(reopened.state['mode'], 'demand')
        self.assertEqual(
            reopened.state['demandPolicy'], {**SAVED, 'managerStrategy': 1, 'managerExcursions': 1}
        )


class HomeTests(unittest.TestCase):
    def home(self, earned, saved=None, current='x', allowed=None, runs=(), last_switch=0):
        return manager.choose_home(
            {'home': saved} if saved else {},
            earned,
            lambda: list(runs),
            current,
            set(allowed or earned) | {current},
            NOW,
            last_switch,
        )

    @staticmethod
    def days(rates, hours=4, end=NOW):
        """`hours` ready hours on each of consecutive days (the first ending at `end`)."""
        return [m for i, r in enumerate(rates) for m in minutes(r, hours, end - i * 86400)]

    def test_best_lower_bound_over_three_days_in_thirty(self):
        earned = {
            GEMMA: {'minutes': minutes(0.108, 120)},  # five days
            'qwen': {'minutes': minutes(0.30, 20)},  # better, but on one day only
            GPT_OSS: {'minutes': minutes(0.02, 200)},
            'old': {'minutes': self.days([0.5] * 3, 4, NOW - 31 * 86400)},  # outside 30 days
            selection_key([GEMMA, 'b']): {'minutes': minutes(0.9, 50)},  # combos never
        }
        home = self.home(earned)
        self.assertEqual(home['model'], GEMMA)
        self.assertEqual(home['source'], 'history')
        self.assertAlmostEqual(home['usdPerHour'], 0.108)
        self.assertEqual(home['days'], 6)  # 120 h to noon cross six local calendar days
        self.assertAlmostEqual(home['hours'], 120, delta=0.1)

    def test_saved_history_home_needs_a_day_old_home_and_a_challenger_lower_bound(self):
        earned = {GEMMA: {'minutes': self.days([0.10] * 5)}}
        earned['b'] = {'minutes': self.days([0.20, 0.05, 0.14])}  # mean 0.13, too noisy
        fresh = {'model': GEMMA, 'source': 'history', 'at': NOW - 3600}
        old = {**fresh, 'at': NOW - 2 * 86400}
        self.assertEqual(self.home(earned, fresh)['model'], GEMMA)
        self.assertEqual(self.home(earned, old)['model'], GEMMA)  # lower bound 0.048 < 0.10
        earned['b'] = {'minutes': self.days([0.2, 0.18, 0.22])}  # lower bound 0.178
        self.assertEqual(self.home(earned, fresh)['model'], GEMMA)  # held for a day
        self.assertEqual(self.home(earned, old)['model'], 'b')

    def test_pin_wins_and_current_is_the_last_resort_but_never_a_fallback_pick(self):
        earned = {GEMMA: {'minutes': minutes(0.108, 120)}}
        pin = manager.pin('b', NOW)
        self.assertEqual(self.home(earned, pin)['model'], 'b')
        self.assertEqual(self.home({}, current='b'), {'model': 'b', 'source': 'current', 'at': NOW})
        fallback = {
            'result': 'switched',
            'model': GPT_OSS,
            'completedAt': NOW - 600,
            'decision': {'kind': 'explore', 'candidate': {'selectionReason': 'fallback'}},
        }
        self.assertIsNone(self.home({}, current=GPT_OSS, runs=[fallback], last_switch=NOW - 600))
        manual_later = {**fallback, 'completedAt': NOW - 7200}
        self.assertEqual(
            self.home({}, current=GPT_OSS, runs=[manual_later], last_switch=NOW - 600)['model'],
            GPT_OSS,
        )
        saved = {'model': 'b', 'source': 'current', 'at': NOW - 60}
        self.assertEqual(self.home({}, saved, current='c', allowed={'b'})['model'], 'b')  # sticky
        # ...while it is allowed: deselected or unavailable, it is dropped.
        self.assertEqual(self.home({}, saved, current='c')['model'], 'c')


class DecisionTests(unittest.TestCase):
    """Replays of legacy decisions that used to move the Mac off a good model."""

    def test_noise_that_used_to_start_trials_holds_home(self):
        without_new = trial_rows()
        without_new[2]['selected'] = False
        cases = {
            'idle escape': legacy_trial(idle=1200),
            'gpt-oss fallback': legacy_trial(without_new, idle=1200),
            'paid upgrade': legacy_upgrade(),
            'failed trial': legacy_trial(failed=True),
        }
        for trigger in ('earnings_target', 'baseline_learning', 'demand_spike', 'failed_trial'):
            cases[trigger] = {
                **legacy_upgrade(),
                'kind': 'explore',
                'explorationTrigger': trigger,
                'escapeReady': True,
            }
        for name, legacy in cases.items():
            with self.subTest(name):
                self.assertIsNotNone(legacy['target'], name)
                d = managed(legacy)
                holds(self, d)
                self.assertIn('$0.108 per ready hour', d['reason'])
                self.assertIsNone(legacy.get('manager'))  # the input is not mutated

    def test_off_home_returns_home_unless_memory_retry_or_failed_pin_hold_it(self):
        d = managed(legacy_upgrade(), current='b')
        self.assertEqual((d['target'], d['kind']), ('a', 'home'))
        self.assertEqual(d['reason'], 'Returning to home model a.')
        self.assertEqual(d['sourceAt'], NOW)
        short = managed(legacy_upgrade(), current='b', rows=[row('a', after=30.5), row('b')])
        self.assertIsNone(short['target'])
        self.assertIn(
            'Waiting to return to home model a: waiting for enough free memory', short['reason']
        )
        state = {
            'mode': 'demand',
            'demandPolicy': policy(),
            'manager': {'homeRetry': {'model': 'a', 'until': NOW + 60, 'failures': 1}},
        }
        self.assertIsNone(managed(legacy_upgrade(), current='b', state=state)['target'])
        state['manager'] = {'retryAt': NOW + 60}
        self.assertIsNone(managed(legacy_upgrade(), current='b', state=state)['target'])
        pinned = context('b', 'a', source='manual')
        pinned['home']['failedAt'] = NOW - 5
        d = manager.decide(legacy_upgrade(), {'mode': 'demand'}, pinned, NOW)
        self.assertIsNone(d['target'])
        self.assertIn('Your pick a could not be restored', d['reason'])

    def test_pin_is_held_and_never_offered_an_excursion(self):
        proposal = Mock(return_value={'target': 'b', 'reason': 'x', 'predictedUsdPerHour': 1})
        with patch('manager.manager_excursion', proposal):
            d = managed(legacy_upgrade(), current='a', source='manual')
        self.assertIsNone(d['target'])
        self.assertEqual(
            d['reason'], 'Holding your pick a. BloomGauge will not switch away from it.'
        )
        self.assertTrue(d['manager']['pinned'])
        proposal.assert_not_called()

    def test_excursion_hook_contract(self):
        good = {'target': 'b', 'reason': 'public demand for b doubled.', 'predictedUsdPerHour': 0.2}
        with patch('manager.manager_excursion', Mock(return_value=good)) as hook:
            d = managed(legacy_upgrade())
        state, now, ctx = hook.call_args.args
        self.assertEqual((now, ctx['current'], ctx['home']['model']), (NOW, 'a', 'a'))
        self.assertEqual((d['target'], d['kind']), ('b', 'excursion'))
        self.assertEqual(d['reason'], 'Excursion to b: public demand for b doubled.')
        self.assertEqual(d['manager']['proposal'], good)
        for bad, kwargs in (
            ({**good, 'target': 'a'}, {}),
            ({**good, 'predictedUsdPerHour': None}, {}),
            ({**good, 'target': selection_key(['a', 'b'])}, {}),
            (good, {'blocked': {'b': NOW + 60}}),
            (good, {'rows': [row('a'), row('b', available=False)]}),
        ):
            with self.subTest(bad=bad, kwargs=kwargs):
                with patch('manager.manager_excursion', Mock(return_value=bad)):
                    holds(self, managed(legacy_upgrade(), **kwargs))

    def test_active_excursion_holds_until_it_ends_then_returns_home(self):
        excursion = {
            'target': 'b',
            'from': 'a',
            'startedAt': NOW - 600,
            'reason': 'demand',
            'maxMinutes': 60,
        }
        state = {'mode': 'demand', 'demandPolicy': policy(), 'manager': {'excursion': excursion}}
        d = managed(legacy_upgrade(), current='b', state=state)
        self.assertIsNone(d['target'])
        self.assertEqual(d['reason'], 'Excursion to b: demand')
        excursion['startedAt'] = NOW - 3600
        d = managed(legacy_upgrade(), current='b', state=state)
        self.assertEqual(
            (d['target'], d['kind'], d['manager']['action']), ('a', 'home', 'end-excursion')
        )
        excursion['startedAt'] = NOW - 600
        manager.end_excursion(state, 'realized pay fell below half the prediction', NOW)
        d = managed(legacy_upgrade(), current='b', state=state)
        self.assertEqual(d['target'], 'a')
        self.assertIn('realized pay fell below half the prediction', d['reason'])
        with patch('manager.manager_excursion_end', Mock(return_value='evidence faded')):
            state['manager']['excursion'].pop('endReason')
            self.assertEqual(managed(legacy_upgrade(), current='b', state=state)['target'], 'a')

    def test_control_errors_and_missing_home_hold_the_current_model(self):
        d = managed({**legacy_upgrade(), 'controlError': 'Demand following compares solo models.'})
        self.assertIsNone(d['target'])
        self.assertEqual(d['reason'], 'Demand following compares solo models.')
        d = managed(legacy_upgrade(), home=None)
        self.assertIsNone(d['target'])
        self.assertIn('No home model yet', d['reason'])


class CombinationTests(unittest.TestCase):
    hardware = {'memoryTotalGB': 128, 'memoryAvailableGB': 100}

    def test_gemma_is_never_paired_under_the_manager(self):
        rows = [
            {'id': m, 'name': m, 'available': True, 'memoryGB': 10}
            for m in (GEMMA, 'b', 'c')
        ]
        pairs = pair_candidates(
            rows, self.hardware, {'memoryGB': 0}, 0, None, manager.SOLO_MODELS
        )
        pairs = {r['id']: r for r in pairs}
        self.assertFalse(pairs[selection_key([GEMMA, 'b'])]['available'])
        self.assertIn('Served alone only', pairs[selection_key([GEMMA, 'c'])]['reason'])
        self.assertTrue(pairs[selection_key(['b', 'c'])]['available'])
        legacy = pair_candidates(rows, self.hardware, {'memoryGB': 0})
        self.assertTrue(all(r['available'] for r in legacy))


class Harness(unittest.TestCase):
    setUp_base = fixtures.ControllerTests.setUp
    tearDown = fixtures.ControllerTests.tearDown
    manual_payload = fixtures.ControllerTests.manual_payload

    def setUp(self):
        self.setUp_base()
        self.o.state.update(mode='demand', demandPolicy=policy(), expectedModel='a')
        self.o.state['manager'] = {
            'home': {'model': 'a', 'source': 'history', 'at': self.now - 90000}
        }
        process = patch('manager.matching_process', return_value=True)
        process.start()
        self.addCleanup(process.stop)
        self.o.tick_demand = Mock()
        self.o.command = Mock()
        self.o.verify_started = Mock(return_value=True)
        self.o.perform_prewarm = Mock(return_value=True)

    def dark(self, session=0, **changes):
        """A provider session started at `session` that advertises a model but never loaded it."""
        self.o.raw.update(
            warm_models=[],
            started_at=self.now + session,
            pid=2,
            stats={'requests_served': 0, 'tokens_generated': 0},
            **changes,
        )

    def at(self, offset, online=True, written=True):
        now = self.now + offset
        self.o.live.update(at=now)
        self.o.live['provider']['online'] = online
        self.o.live['hardware']['at'] = now
        self.o.live['earnings']['updatedAt'] = now
        self.o.identity_at = now
        self.o.discovery_at = now
        if written:  # a stopped provider stops writing daemon-state
            self.o.raw['written_at'] = now
        self.o.read_state.return_value = copy.deepcopy(self.o.raw)
        self.net.snapshot.return_value['capacity']['updatedAt'] = now
        with patch('optimizer.time.time', return_value=now), patch(
            'manager.time.time', return_value=now
        ):
            self.o.tick(now)
            if self.o.worker:
                self.o.worker.join(2)
        return now

    def ticks(self, start, end, step=60):
        # The control loop ticks every 15 s; a longer gap is a wake from sleep.
        for offset in range(start, end + 1, step):
            self.at(offset)

    def events(self, kind):
        return [
            r[0]
            for r in self.h.db.execute(
                'SELECT detail FROM opt_events WHERE kind=? ORDER BY id', (kind,)
            )
        ]

    def restores(self):
        return [c.args[0] for c in self.o.command.call_args_list]

    def loads(self):
        return [c.args[0] for c in self.o.perform_prewarm.call_args_list]


class WatchdogTests(Harness):
    def test_cold_home_is_force_loaded_after_the_window_and_automation_stays_on(self):
        self.dark()
        self.ticks(0, 540)
        self.assertEqual((self.restores(), self.loads()), ([], []))
        self.assertIn('a has not been ready since', self.o.detail)
        self.assertIn('nothing is loaded and no work has arrived', self.o.detail)
        self.assertIn('BloomGauge restores a', self.o.detail)
        self.at(600)
        # The right model was selected but cold: load it through the local endpoint, no restart.
        self.assertEqual((self.restores(), self.loads()), ([], ['a']))
        self.assertEqual(self.o.state['mode'], 'demand')
        self.assertEqual(self.o.state['lastSwitchResult']['outcome'], 'recovered')
        self.assertNotIn('pending', self.o.state)
        self.assertIn('Watchdog restored a after', self.events('manager-notice')[0])

    def test_restart_restores_another_home_without_force(self):
        self.o.state['manager']['home']['model'] = 'b'
        self.dark()
        self.ticks(0, 600)
        self.assertEqual((self.restores(), self.loads()), (['b'], []))
        self.assertEqual(self.o.verify_started.call_args.args[4], manager.MEMORY_WAIT_SECONDS)
        self.assertEqual(self.o.state['expectedModel'], 'b')
        self.assertIn('--force is never used', self.events('switching')[0])

    def test_window_covers_a_drain_and_three_median_loads(self):
        self.assertEqual(self.o.manager.window({'version': '0.9.8'}), 600)
        self.assertEqual(self.o.manager.window({'version': '0.9.9'}), 600 + 3 * 90)
        for downtime in (60, 80, 100):
            self.o.store.event(
                'acct', self.live['device'], self.now, 'switched', 'a', 'ok', downtime
            )
        self.o.manager.load_cache = (0, 0)
        self.assertEqual(self.o.manager.window({'version': '0.9.9'}), 600 + 3 * 80)

    def test_never_fires_during_a_099_drain_offline_provider_or_idle_unload(self):
        self.dark(version='0.9.9', lifecycle={'outcome': 'draining', 'remaining': 1})
        self.ticks(0, 3000)
        self.assertEqual(self.restores(), [])
        self.assertIn('draining', self.o.detail)
        self.o.raw['lifecycle'] = {'outcome': 'serving', 'remaining': 0}
        for offset in range(3060, 5000, 60):
            self.at(offset, online=False)
        self.assertEqual(self.restores(), [])
        # A session that served and was then unloaded by the idle timeout the user chose rests
        # (with Darkbloom's default the manager loads it again: test_manager_control_path).
        toml = self.o.home / '.config/darkbloom/provider.toml'
        toml.parent.mkdir(parents=True, exist_ok=True)
        toml.write_text('[backend]\nidle_timeout_mins = 30\n')
        self.o.raw['warm_models'] = ['a']
        self.at(5040)
        self.o.raw.update(warm_models=[], stats={'requests_served': 5, 'tokens_generated': 50})
        self.ticks(5100, 7000)
        self.assertEqual((self.restores(), self.loads()), ([], []))

    def test_load_then_restart_then_notifies_and_backs_off(self):
        self.o.perform_prewarm = Mock(side_effect=WarmupError('no', code='readiness-timeout'))
        self.o.verify_started = Mock(return_value=False)
        self.dark()
        self.ticks(0, 600)
        self.assertEqual((self.restores(), self.loads()), ([], ['a']))
        self.ticks(660, 720)  # the retry follows the short grace after the failed load
        self.assertEqual(self.restores(), [])
        self.at(780)
        self.assertEqual((self.restores(), self.loads()), (['a'], ['a']))
        wd = self.o.state['manager']['watchdog']
        self.assertEqual(wd['attempts'], 0)
        self.assertEqual(wd['backoff'], 3600)
        self.assertIn('two restores did not work', self.events('manager-notice')[0])
        self.ticks(840, 1500)
        self.assertEqual(len(self.restores() + self.loads()), 2)
        self.assertIn('tries again at', self.o.detail)
        self.assertEqual(self.o.state['mode'], 'demand')

    def test_work_in_flight_or_short_memory_waits_without_a_command(self):
        self.dark(inference_active=None)
        self.ticks(0, 660)
        self.assertEqual(self.restores(), [])
        self.assertIn('Work is in flight', self.o.detail)
        self.o.raw['inference_active'] = False
        self.o.live['hardware']['memoryAvailableGB'] = 2
        self.o.live['provider']['memoryGB'] = 0
        self.at(720)
        self.assertEqual(self.restores(), [])
        self.assertIn('Not enough free memory to restore a', self.o.detail)

    def test_sep28_idle_unloaded_home_short_only_on_file_cache_is_restored(self):
        """Sep 28 21:20-22:10: gemma idle-unloaded, 'Not enough free memory to restore' for 50 min.

        Darkbloom's idle unload leaves the weights in file cache; the same-model restore must
        count what the purge frees (like a switch does) and let the reload clear it.
        """
        self.o.manual_selection.permission_status = Mock(return_value={'status': 'ready'})
        self.dark()
        self.o.live['provider']['memoryGB'] = 0
        budget = self.o.selection_budget('a', self.o.live, self.o.raw)
        short = budget['requiredGB'] - 1.1
        self.o.live['hardware'].update(memoryAvailableGB=short, cachedFilesGB=9.6)
        self.ticks(0, 660)
        self.assertEqual(self.loads(), ['a'])
        self.assertNotIn('Not enough free memory', self.o.detail)

    def test_same_model_restart_purges_a_cold_model(self):
        self.o.manual_selection.permission_status = Mock(return_value={'status': 'ready'})
        self.o.purge_before_load = Mock(return_value=False)
        self.o.perform_prewarm = Mock(side_effect=WarmupError('no', code='readiness-timeout'))
        self.o.verify_started = Mock(return_value=False)
        self.dark()
        self.ticks(0, 780)
        self.assertEqual((self.restores(), self.loads()), (['a'], ['a']))
        self.assertEqual([c.args[0] for c in self.o.purge_before_load.call_args_list], ['a'])

    def test_a_cleanup_that_frees_too_little_holds_the_credit_instead_of_repeating(self):
        self.o.manual_selection.permission_status = Mock(return_value={'status': 'ready'})
        self.o.purge_before_load = Mock(return_value=True)  # ran, but the cache stays
        self.o.perform_prewarm = Mock(side_effect=WarmupError('no', code='readiness-timeout'))
        self.dark()
        self.o.live['provider']['memoryGB'] = 0
        budget = self.o.selection_budget('a', self.o.live, self.o.raw)
        self.o.live['hardware'].update(
            memoryAvailableGB=budget['requiredGB'] - 1.1, cachedFilesGB=9.6
        )
        self.ticks(0, 3000)
        self.assertEqual(self.restores(), [])
        self.assertEqual(self.o.purge_before_load.call_count, 1)
        self.assertEqual(self.o.state['purgeShortfall']['model'], 'a')
        self.assertIn('Not enough free memory to restore a', self.o.detail)

    def test_residual_mlx_memory_after_an_unload_is_not_a_load(self):
        raw = {'warm_models': [], 'capacity': {'gpu_memory_active_gb': 5.088746547698975e-06}}
        self.assertIsNone(manager.provider_busy(raw))
        raw['capacity']['gpu_memory_active_gb'] = 3.2  # a load allocating its weights
        self.assertEqual(manager.provider_busy(raw), 'loading')

    def test_warm_current_model_gets_no_purge_credit(self):
        self.o.manual_selection.permission_status = Mock(return_value={'status': 'ready'})
        self.o.purge_credit = Mock(return_value=50)
        self.o.raw['warm_models'] = ['a']
        self.o.manager.dispatch(
            self.now, 'a', 'watchdog', self.o.state, self.o.live, self.o.raw, 'a', 'x'
        )
        if self.o.worker:
            self.o.worker.join(2)
        self.o.purge_credit.assert_not_called()

    def test_sep9_manual_pick_that_loads_late_is_kept(self):
        """Pick at 13:00:59, 'failed' at 13:12, ready at 13:24: the pick must be kept."""
        failed_at = self.now
        self.o.state['manager'] = {'home': {**manager.pin('b', failed_at - 660), 'failures': 1}}
        self.o.state['lastSwitchResult'] = {'at': failed_at, 'outcome': 'failed'}
        self.o.read_options.return_value = ('b', ['--local-endpoint', '--port', '8000'], {})
        self.dark(-600, version='0.9.9', advertised_models=['b'], current_model='b')
        self.o.manager.dark_since = failed_at - 900  # dark before and during the switch
        self.o.perform_prewarm = Mock(side_effect=WarmupError('still loading', code='x'))
        for offset in range(0, 721, 60):
            self.at(offset)
        # Never reverted: at most the pick itself is force-loaded (no restart, no other model).
        self.assertEqual(self.restores(), [])
        self.assertEqual(set(self.loads()), {'b'})
        self.assertIn('restores b', self.o.detail)
        self.o.raw['warm_models'] = ['b']
        self.at(780)
        self.assertEqual(self.restores(), [])
        self.assertEqual(self.o.state['expectedModel'], 'b')
        home = self.o.state['manager']['home']
        self.assertEqual((home['model'], home['source'], home['failures']), ('b', 'manual', 0))
        self.assertEqual(self.o.state['mode'], 'demand')

    def test_failed_pin_falls_back_to_last_good_and_stops_reverting(self):
        self.o.state['manager'] = {
            'home': manager.pin('b', self.now - 7200),
            'lastGood': {'model': 'a', 'at': self.now - 3600},
        }
        self.o.verify_started = Mock(side_effect=[False, True])
        self.dark()
        self.ticks(0, 840)
        self.assertEqual(self.restores(), ['b', 'a'])  # the pin once, then the last good model
        home = self.o.state['manager']['home']
        self.assertEqual(home['model'], 'b')
        self.assertTrue(home['failedAt'])
        self.assertIn('Your pick b could not be restored', self.events('manager-notice')[0])


class RecoveryTests(unittest.TestCase):
    """A failed automatic move restores instead of pausing (auto-restore)."""

    setUp_base = controller.DemandControllerTests.setUp
    tearDown = controller.DemandControllerTests.tearDown
    setup_demand = controller.DemandControllerTests.setup_demand
    at = Harness.at
    events = Harness.events

    def setUp(self):
        self.setUp_base()
        self.setup_demand()
        rules = policy({'minRunMinutes': 30, 'confirmationMinutes': 5})
        self.o.state.update(demandPolicy=rules, expectedModel='a')
        self.o.state['manager'] = {
            'home': {'model': 'b', 'source': 'history', 'at': self.now - 90000}
        }
        self.value.update(
            target='b',
            kind='home',
            reason='Returning to home model b.',
            policy=rules,
            explorationTrigger=None,
            manager={
                'active': True,
                'action': 'return-home',
                'home': self.o.state['manager']['home'],
            },
        )
        self.o.state['pending'] = {'model': 'b', 'kind': 'demand', 'demandKind': 'home'}
        self.o.command = Mock()
        process = patch('manager.matching_process', return_value=True)
        process.start()
        self.addCleanup(process.stop)

    def switch(self):
        self.o.switch('a', 'b', 'acct', self.live['device'])

    def cold(self, model='b', **changes):
        """`darkbloom start --model b` rewrote the plist and restarted; b never loaded."""
        self.o.read_options.return_value = (model, ['--local-endpoint', '--port', '8000'], {})
        self.o.state['expectedModel'] = model
        self.o.raw.update(
            advertised_models=[model],
            current_model=model,
            warm_models=[],
            started_at=self.now - 30,
            pid=2,
            stats={'requests_served': 0, 'tokens_generated': 0},
            **changes,
        )

    def restores(self):
        return [c.args[0] for c in self.o.command.call_args_list]

    def test_restored_by_the_switch_keeps_automation_on_and_holds_home_back(self):
        self.o.verify_started = Mock(side_effect=[False, True])
        self.switch()
        self.assertEqual(self.restores(), ['b', 'a'])
        self.assertEqual(self.o.state['mode'], 'demand')
        self.assertIn('Automatic control continues', self.o.detail)
        run = self.o.demand_auto.runs('acct', self.live['device'], self.now + 60)[0]
        self.assertEqual((run['result'], run['decision']['kind']), ('recovered', 'home'))
        hold = self.o.state['manager']['homeRetry']
        self.assertEqual((hold['model'], hold['failures']), ('b', 1))
        self.assertGreater(hold['until'], self.now + 3000)
        self.assertIn('Restored a after the switch to b failed', self.events('manager-notice')[0])

    def test_auto_chosen_target_is_blocked_for_a_day_but_home_is_retried(self):
        self.o.state['manager'] = {'home': {'model': 'a', 'source': 'history', 'at': 0}}
        self.value.update(kind='excursion')
        self.o.state['pending']['demandKind'] = 'excursion'
        self.o.verify_started = Mock(side_effect=[False, True])
        self.switch()
        until = self.o.state['manager']['blocked']['b']
        self.assertAlmostEqual(until - self.now, 86400, delta=60)

    def test_unverified_switch_waits_for_the_target_then_restores_previous(self):
        self.o.verify_started = Mock(side_effect=[False, True])
        self.o.recovery_ready = Mock(return_value=None)  # no safe idle window during the switch
        self.switch()
        rec = self.o.state['manager']['recovery']
        self.assertEqual((rec['failedTarget'], rec['previous'], rec['attempts']), ('b', 'a', 0))
        self.assertEqual(self.o.state['mode'], 'demand')
        self.assertEqual(self.restores(), ['b'])
        self.o.tick_demand = Mock()
        self.cold()
        self.at(60)
        self.assertEqual(self.restores(), ['b'])
        self.assertIn('b never loaded after the switch at', self.o.detail)
        self.assertIn('whether it loads, then restoring a', self.o.detail)
        self.at(250)
        self.assertEqual(self.restores(), ['b', 'a'])
        self.assertNotIn('recovery', self.o.state['manager'])
        self.assertEqual(self.o.state['lastSwitchResult']['outcome'], 'recovered')
        self.assertIn('Restored a after the switch to b failed', self.events('manager-notice')[0])

    def test_target_that_becomes_ready_anyway_is_kept(self):
        self.o.verify_started = Mock(return_value=False)
        self.o.recovery_ready = Mock(return_value=None)
        self.switch()
        self.o.tick_demand = Mock()
        self.o.read_options.return_value = ('b', ['--local-endpoint', '--port', '8000'], {})
        self.o.raw.update(advertised_models=['b'], current_model='b', warm_models=['b'])
        self.at(120)
        self.at(135)
        self.assertEqual(self.restores(), ['b'])
        self.assertEqual(self.o.state['expectedModel'], 'b')
        self.assertNotIn('recovery', self.o.state['manager'])
        self.assertNotEqual(self.o.state['manager']['home'].get('source'), 'external')
        self.assertIn('b became ready after a delayed load', self.events('manager')[-1])

    def test_recovery_never_restarts_a_draining_provider(self):
        self.o.verify_started = Mock(return_value=False)
        self.o.recovery_ready = Mock(return_value=None)
        self.switch()
        self.o.tick_demand = Mock()
        self.cold(lifecycle={'outcome': 'draining', 'remaining': 2})
        for offset in range(60, 1500, 60):
            self.at(offset)
        self.assertEqual(self.restores(), ['b'])
        self.o.raw['lifecycle'] = {'outcome': 'serving', 'remaining': 0}
        self.at(1560)
        self.assertEqual(self.restores(), ['b', 'a'])

    def test_two_failed_restores_hand_over_to_the_watchdog_and_automation_stays_on(self):
        self.o.verify_started = Mock(return_value=False)
        self.o.recovery_ready = Mock(return_value=None)
        self.switch()
        self.o.tick_demand = Mock()
        self.cold()
        for offset in (200, 300, 420, 520):
            self.at(offset)
        self.assertEqual(self.restores(), ['b', 'a', 'a'])
        self.assertEqual(self.o.state['mode'], 'demand')  # never Off
        self.assertEqual(self.events('paused'), [])
        m = self.o.state['manager']
        self.assertNotIn('recovery', m)
        self.assertAlmostEqual(m['watchdog']['nextAt'] - (self.now + 520), 1800, delta=5)
        self.assertIn('did not work twice', self.events('manager-notice')[-1])
        self.assertIn('Automatic control stays on', self.events('manager-notice')[-1])
        self.assertIn('tries again at', self.o.detail)

    def test_legacy_strategy_still_pauses(self):
        self.o.state['demandPolicy'] = policy({'managerStrategy': 0})
        self.value['policy'] = self.o.state['demandPolicy']
        self.o.verify_started = Mock(side_effect=[False, True])
        self.switch()
        self.assertEqual(self.o.state['mode'], 'observe')


class ControllerTests(Harness):
    def decision(self, target=None, kind=None, reason='Holding home model a.', home='a'):
        legacy = legacy_upgrade()
        legacy.update(
            at=self.now,
            currentModel='a',
            target=target,
            kind=kind,
            reason=reason,
            policy=self.o.state['demandPolicy'],
            explorationTrigger=None,
            manager={
                'active': True,
                'home': {'model': home, 'source': 'history', 'at': self.now - 90000},
            },
        )
        return legacy

    def test_demand_status_says_what_the_manager_is_doing(self):
        legacy = legacy_trial(idle=1200)
        legacy['currentModel'] = 'a'
        self.o.demand_auto.evaluate = Mock(return_value=legacy)
        earned = {
            'a': {'minutes': minutes(0.108, 72, self.now)},
            'b': {'minutes': minutes(0.02, 72, self.now)},
        }
        self.o.demand_auto.evidence = Mock(return_value=(earned, {}))
        self.o.state['manager'] = {}
        d = Optimizer.demand_decision(self.o, self.now)
        self.assertIsNone(d['target'])
        self.assertEqual(
            d['reason'],
            'Holding home model a (best paid on this Mac: $0.108 per ready hour over the last 30 days).',
        )
        self.assertEqual(d['manager']['home']['model'], 'a')
        self.assertEqual(d['manager']['strategy'], 'manager')
        self.assertTrue(d['manager']['active'])
        self.assertFalse(d['dataGathering']['active'])
        self.o.state['demandPolicy'] = policy({'managerStrategy': 0})
        self.o.demand_auto.evaluate.return_value = legacy_trial(idle=1200)
        self.assertEqual(Optimizer.demand_decision(self.o, self.now)['target'], 'new')

    def test_home_return_dispatches_a_recorded_manager_move_without_confirming(self):
        self.o.state['lastSwitchAt'] = self.now - 4000
        self.o.idle_since = self.now - 30
        value = self.decision('b', 'home', 'Returning to home model b.', 'b')
        self.o.demand_decision = Mock(
            side_effect=lambda now, *a, **k: {**copy.deepcopy(value), 'at': now, 'sourceAt': now}
        )
        del self.o.tick_demand
        self.o.tracking = Mock(return_value={'counting': True})
        self.o.switch = Mock()
        # A return home uses no demand evidence: no 5-minute confirmation.
        self.at(0)
        self.o.switch.assert_called_once_with('a', 'b', 'acct', self.live['device'])
        self.assertEqual(self.o.state['manager']['home']['model'], 'b')  # persisted
        self.assertEqual(self.o.state['pending']['demandKind'], 'home')
        self.assertNotIn('demandProposal', self.o.state)

    def test_completed_manager_move_is_budgeted_and_announced(self):
        value = self.decision('b', 'home', 'Returning to home model b.', 'b')
        self.o.demand_decision = Mock(
            side_effect=lambda now, *a, **k: {**copy.deepcopy(value), 'at': now}
        )
        self.o.verify_local_target = Mock()
        self.o.tracking = Mock(return_value={'counting': True})
        self.o.state['pending'] = {'model': 'b', 'kind': 'demand', 'demandKind': 'home'}
        self.o.switch('a', 'b', 'acct', self.live['device'])
        self.assertEqual(self.o.state['expectedModel'], 'b')
        run = self.o.demand_auto.runs('acct', self.live['device'], self.now + 60)[0]
        self.assertEqual((run['result'], run['decision']['kind']), ('switched', 'home'))
        alert = self.h.db.execute('SELECT reason FROM model_switch_alerts').fetchone()
        self.assertEqual(alert[0], 'home_return')
        # Only excursion starts count toward the daily limit: another return home is not capped.
        rules = policy({'maxSwitchesPerDay': 1})
        self.assertTrue(
            self.o.demand_auto.begin_manager(
                'acct',
                self.live['device'],
                {**value, 'policy': rules, 'at': self.now + 60},
                self.now + 60,
            )
        )

    def test_manual_pick_becomes_the_pin_and_automatic_control_resumes(self):
        p = self.manual_payload('b')
        self.o.manual_action(p)
        self.assertEqual(self.o.state['mode'], 'observe')
        self.assertEqual(self.o.state['manager']['resume']['id'], p['requestId'])
        self.o.switch('a', 'b', 'acct', self.live['device'])
        self.assertEqual(self.o.state['manualResult']['status'], 'completed')
        self.assertEqual(self.o.state['mode'], 'demand')
        home = self.o.state['manager']['home']
        self.assertEqual((home['model'], home['source']), ('b', 'manual'))
        raw = {**self.o.raw, 'advertised_models': ['b']}
        rows = [row('a'), row('b')]
        d = self.o.manager.decide(
            {**legacy_upgrade(), 'currentModel': 'b', 'policy': policy()},
            self.o.state,
            self.o.live,
            raw,
            rows,
            self.now,
        )
        self.assertIsNone(d['target'])
        self.assertTrue(d['reason'].startswith('Holding your pick b'))
        self.o.pause_automatic()  # Off releases the pin
        self.assertNotIn('home', self.o.state['manager'])

    def test_manual_pick_of_the_serving_model_pins_it_without_pausing(self):
        p = self.manual_payload('a')
        self.o.manual_action(p)
        self.assertEqual(self.o.state['mode'], 'demand')
        self.assertEqual(self.o.state['manager']['home']['source'], 'manual')

    def test_pick_interrupted_by_an_app_restart_is_kept_and_automation_resumes(self):
        p = self.manual_payload('b')
        self.o.manual_action(p)
        reopened = Optimizer(self.h, self.net, self.tmp.name, threading.Event(), Mock())
        self.assertEqual(reopened.state['manualResult']['status'], 'interrupted')
        self.assertEqual(reopened.state['mode'], 'demand')
        self.assertEqual(reopened.state['manager']['home']['model'], 'b')
        self.assertNotIn('resume', reopened.state['manager'])

    def test_cancelled_pick_resumes_without_a_pin(self):
        p = self.manual_payload('b')
        self.o.manual_action(p)
        self.o.manual_action({'action': 'cancel', 'requestId': p['requestId']})
        self.assertEqual(self.o.state['mode'], 'demand')
        self.assertEqual(self.o.state['manager']['home']['source'], 'history')

    def test_outside_change_is_kept_as_a_pin_and_never_pauses(self):
        self.o.read_options.return_value = ('b', [], {})
        self.o.raw.update(advertised_models=['b'], warm_models=['b'])
        self.at(0)
        self.assertEqual(self.o.state['mode'], 'demand')
        self.assertEqual(self.o.state['expectedModel'], 'b')
        home = self.o.state['manager']['home']
        self.assertEqual((home['model'], home['source']), ('b', 'external'))
        self.o.command.assert_not_called()

    def test_diverging_or_unreadable_launch_settings_hold_without_pausing(self):
        self.o.read_options.return_value = ('b', [], {})  # plist says b, daemon serves a
        self.at(0)
        self.assertEqual((self.o.state['mode'], self.o.state['expectedModel']), ('demand', 'a'))
        self.assertIn('Holding until they agree', self.o.detail)
        self.o.read_options.side_effect = ValueError('unsupported')
        self.at(15)
        self.assertEqual(self.o.state['mode'], 'demand')
        self.assertIn('The manager holds and rechecks', self.o.detail)
        self.o.command.assert_not_called()

    def test_reopening_during_a_move_keeps_automation_on(self):
        self.o.state['pending'] = {'model': 'b', 'previous': 'a', 'kind': 'demand'}
        self.o.save()
        reopened = Optimizer(self.h, self.net, self.tmp.name, threading.Event(), Mock())
        self.assertEqual(reopened.state['mode'], 'demand')
        self.assertEqual(reopened.state['manager']['recovery']['failedTarget'], 'b')
        self.o.state.pop('pending')
        self.o.save()
        d = {**self.decision('b', 'home'), 'manager': {}}
        self.o.demand_auto.begin_manager('acct', self.live['device'], d, self.now)
        reopened = Optimizer(self.h, self.net, self.tmp.name, threading.Event(), Mock())
        self.assertEqual(reopened.state['mode'], 'demand')
        row_ = reopened.demand_auto.runs('acct', self.live['device'], self.now + 60)[0]
        self.assertEqual(row_['result'], 'interrupted')

    def test_learning_boost_is_refused_under_the_manager(self):
        with self.assertRaisesRegex(ValueError, 'Learning boost is off'):
            self.o.set_data_gathering(
                {
                    'action': 'data-gathering',
                    'seconds': 86400,
                    'expectedControl': self.o.control_version(),
                },
                'mac',
            )

    def test_gemma_pairs_are_not_offered_for_combination_tests(self):
        self.o.catalog = [{'id': m, 'active': True, 'min_ram_gb': 24} for m in ('a', GEMMA)]
        self.o.local = [
            {'id': m, 'estimated_memory_gb': 10, 'size_bytes': 1, 'template_render_ok': True}
            for m in ('a', GEMMA)
        ]
        self.o.state['models'] = ['a', GEMMA]
        live = {**self.o.live, 'hardware': {**self.o.live['hardware'], 'memoryTotalGB': 128}}
        candidates = self.o.candidates({}, {}, live, self.o.state)
        snap = self.o.combo_snapshot(self.o.state, live, self.o.raw, candidates, {})
        self.assertFalse(snap['candidates'][0]['available'])
        legacy = {**self.o.state, 'demandPolicy': policy({'managerStrategy': 0})}
        snap = self.o.combo_snapshot(legacy, live, self.o.raw, candidates, {})
        self.assertIsNone(snap['candidates'][0]['reason'])


class StallTests(unittest.TestCase):
    setUp = stall_fixtures.ControlTests.setUp
    tearDown = stall_fixtures.ControlTests.tearDown
    control = stall_fixtures.ControlTests.control

    def test_manager_stops_the_ladder_instead_of_escaping_and_pushes_notices(self):
        c = self.control()
        for at, step in ((stall_fixtures.T0 + 300, 'probe'), (stall_fixtures.T0 + 480, 'restart')):
            self.store.event('acct', 'mac', at, 'stall-' + step, 'a', step)
        c.tick(stall_fixtures.T0 + 1100, self.settings, self.live, self.raw, 'a', [], {})
        self.assertFalse(c.escape_active(stall_fixtures.T0 + 1110))
        self.assertEqual(self.store.events[-1][0], 'stall-hold')
        self.assertIn('Holding the home model', self.store.events[-1][2])
        self.store.event(
            'acct', 'mac', stall_fixtures.T0 + 1101, 'manager-notice', 'a', 'Watchdog restored a.'
        )
        push = Mock()
        push.enqueue_notice.return_value = True
        c.send_pending('acct', 'mac', push, stall_fixtures.T0 + 1200)
        titles = [call.args[2] for call in push.enqueue_notice.call_args_list]
        self.assertEqual(
            titles, ['BloomGauge · no work arriving', 'BloomGauge · model recovery']
        )


if __name__ == '__main__':
    unittest.main()


QWEN = 'qwen3.5-35b-a3b'
OPTIONS = ['--coordinator-url', 'wss://api.darkbloom.dev/ws/provider', '--local-endpoint']
MEMORY_WAIT = (
    'Waiting for enough available memory to pre-warm. Cache recovery only runs for a cold '
    'model blocked by file cache.'
)


class AndrewSep27Tests(Harness):
    """Andrew's M5 Pro 48 GB, provider 0.9.10, Sep 27 2026.

    Gemma earned until 15:38. At 15:38:55 the legacy optimizer made a learning-boost trial
    switch to qwen3.5-35b-a3b; at 15:40:01 it failed ("Waiting for enough available memory
    to pre-warm ... Safe restoration could not be verified ... Automatic switching is
    paused"). The provider then advertised qwen but never loaded it (0.9.10 loads on
    demand; a cold slot gets no work): no ready minutes, no earnings for 1.5 h, and turning
    On was refused until the model was Warm. At 17:12 a manual pick of gemma drained,
    restarted qwen and left no provider running; at 17:15 Andrew started gemma by hand.
    """

    def setUp(self):
        super().setUp()
        rows = ((GEMMA, 15.6), (QWEN, 20.9))
        self.o.local = [
            {'id': m, 'estimated_memory_gb': gb, 'size_bytes': 1, 'template_render_ok': True}
            for m, gb in rows
        ]
        self.o.catalog = [{'id': m, 'active': True, 'min_ram_gb': 36} for m, _ in rows]
        self.o.read_options.return_value = (GEMMA, OPTIONS, {})
        self.o.raw.update(
            advertised_models=[GEMMA],
            current_model=GEMMA,
            warm_models=[GEMMA],
            version='0.9.10',
            lifecycle={'outcome': 'serving', 'remaining': 0},
            capacity={'total_memory_gb': 48, 'gpu_memory_active_gb': 17, 'gpu_memory_cache_gb': 0},
            stats={'requests_served': 7946, 'tokens_generated': 900000},
        )
        self.o.live['provider'].update(model=GEMMA, memoryGB=17)
        self.o.live['hardware'].update(memoryTotalGB=48, memoryAvailableGB=20)
        self.o.state.update(expectedModel=GEMMA, models=[GEMMA, QWEN])
        self.o.state['manager'] = {
            'home': {'model': GEMMA, 'source': 'history', 'at': self.now - 90000}
        }
        self.o.demand_auto.evidence = Mock(
            return_value=(
                {
                    GEMMA: {'minutes': minutes(0.1066, 205, self.now)},
                    QWEN: {'minutes': minutes(0.2, 3, self.now)},
                },
                {},
            )
        )
        self.o.verify_local_target = Mock()
        self.o.recovery_identity = Mock(return_value=False)  # 15:40: restore "not verified"
        self.commands = []
        self.o.command = Mock(side_effect=self.started)
        self.o.verify_started = Mock(side_effect=self.verified)
        self.o.read_state.return_value = copy.deepcopy(self.o.raw)
        self.o.warmup = {
            'session': session_key(self.o.raw),
            'model': GEMMA,
            'status': 'ready',
            'verifiedAt': self.now - 1,
        }

    def started(self, target, options, environment):
        """`darkbloom start --model target` (never --force): it restarts with that model."""
        self.commands.append((target, self.o.detail))
        self.o.read_options.return_value = (target, OPTIONS, {})
        self.o.raw.update(
            advertised_models=[target],
            current_model=target,
            warm_models=[],
            started_at=self.o.raw['written_at'] - 1,
            pid=self.o.raw['pid'] + 1,
            stats={'requests_served': 0, 'tokens_generated': 0},
        )
        self.o.raw['capacity']['gpu_memory_active_gb'] = 0

    def verified(self, target, *args, **kwargs):
        if target == QWEN:  # 48 GB with gemma's file cache resident: qwen never loads
            self.o.verification_failure = WarmupError(MEMORY_WAIT, code='readiness-changed')
            return False
        self.o.raw['warm_models'] = [target]  # prewarm through the local endpoint
        self.o.raw['capacity']['gpu_memory_active_gb'] = 17
        return True

    def cold_qwen(self, since):
        self.o.read_options.return_value = (QWEN, OPTIONS, {})
        self.o.raw.update(
            advertised_models=[QWEN],
            current_model=QWEN,
            warm_models=[],
            started_at=self.now + since,
            pid=7,
            stats={'requests_served': 0, 'tokens_generated': 0},
        )
        self.o.raw['capacity']['gpu_memory_active_gb'] = 0
        self.o.identity_session = (self.o.raw['started_at'], self.o.raw['pid'])
        self.o.read_state.return_value = copy.deepcopy(self.o.raw)

    def test_1538_learning_trial_to_qwen_is_never_taken_by_the_manager(self):
        import data_gathering

        self.o.state['dataGathering'] = data_gathering.start(86400, self.now)  # learning boost
        legacy = legacy_upgrade()
        legacy.update(
            at=self.now,
            currentModel=GEMMA,
            target=QWEN,
            kind='explore',
            escapeReady=True,
            explorationTrigger='baseline_learning',
            reason='Learning boost: collect 50 complete paid token samples across 10 paid '
            'minutes, up to 15 warm minutes, then compare with the previous model.',
            policy=data_gathering.relaxed(policy()),
        )
        self.o.demand_auto.evaluate = Mock(return_value=copy.deepcopy(legacy))
        d = Optimizer.demand_decision(self.o, self.now)
        self.assertFalse(self.o.demand_auto.evaluate.call_args.kwargs['gathering']['active'])
        self.assertIsNone(d['target'])
        self.assertTrue(d['reason'].startswith('Holding home model ' + GEMMA), d['reason'])
        # A legacy move already queued when the manager takes over is rechecked and dropped.
        self.o.state['pending'] = {'model': QWEN, 'kind': 'demand', 'demandKind': 'explore'}
        self.o.switch(GEMMA, QWEN, 'acct', self.live['device'])
        self.assertEqual(self.commands, [])
        self.assertEqual(self.o.state['mode'], 'demand')
        self.assertTrue(self.events('switch-deferred'))

    def test_failed_switch_that_never_loads_is_restored_to_gemma_within_ten_minutes(self):
        # The same failure on a manager move (an excursion is the only way off home).
        move = {
            **legacy_upgrade(),
            'at': self.now,
            'currentModel': GEMMA,
            'target': QWEN,
            'kind': 'excursion',
            'reason': 'Excursion to qwen3.5-35b-a3b: fixture.',
            'policy': policy(),
            'manager': {'active': True, 'action': 'excursion', 'proposal': {'target': QWEN}},
        }
        self.o.demand_decision = Mock(side_effect=lambda now, *a, **k: {**move, 'at': now})
        self.o.state['pending'] = {'model': QWEN, 'kind': 'demand', 'demandKind': 'excursion'}
        self.o.switch(GEMMA, QWEN, 'acct', self.live['device'])  # 15:38:55 .. 15:40:01
        self.assertEqual([c[0] for c in self.commands], [QWEN])
        self.assertEqual(self.o.state['mode'], 'demand')  # never Off / "Manual"
        failed = self.events('failed')[-1]
        self.assertIn(MEMORY_WAIT, failed)
        self.assertNotIn('Automatic switching is paused', failed)
        rec = self.o.state['manager']['recovery']
        self.assertEqual((rec['failedTarget'], rec['previous']), (QWEN, GEMMA))
        self.assertGreater(self.o.state['manager']['blocked'][QWEN], self.now + 86000)
        self.assertEqual(self.o.state['expectedModel'], QWEN)
        restored_at = None
        for offset in range(15, 601, 15):
            self.at(offset)
            if offset == 60:
                self.assertIn(QWEN + ' never loaded after the switch at', self.o.detail)
                self.assertIn('(waiting for enough available memory to pre-warm)', self.o.detail)
                self.assertIn('then restoring ' + GEMMA, self.o.detail)
            if restored_at is None and len(self.commands) == 2:
                restored_at = offset
        self.assertEqual([c[0] for c in self.commands], [QWEN, GEMMA])
        self.assertLessEqual(restored_at, 300)
        self.assertIn('Restoring ' + GEMMA, self.commands[1][1])
        # Force-loaded and verified warm, with the pre-warm memory wait bounded.
        self.assertEqual(self.o.verify_started.call_args.args[0], GEMMA)
        self.assertEqual(self.o.verify_started.call_args.args[4], manager.MEMORY_WAIT_SECONDS)
        self.assertEqual(self.o.state['mode'], 'demand')
        self.assertEqual(self.o.state['expectedModel'], GEMMA)
        self.assertTrue(manager.loaded(self.o.raw, self.now + 600)[0])  # earning again
        self.assertNotIn('recovery', self.o.state['manager'])
        self.assertIn(
            'Restored %s after the switch to %s failed' % (GEMMA, QWEN),
            self.events('manager-notice')[0],
        )

    def live_state(self):
        """17:11: qwen advertised and cold since 15:39; Off after the legacy failure."""
        failed = self.now - 5400
        self.cold_qwen(-5450)
        self.o.state.update(
            mode='observe',
            expectedModel=GEMMA,
            startedAt=self.now - 86400,
            endsAt=None,
            lastSwitchResult={'at': failed, 'outcome': 'failed'},
            lastSwitchFailure={
                'model': QWEN,
                'stage': 'verify',
                'code': 'readiness-changed',
                'recovery': 'blocked',
                'recoveryCode': 'resource-or-identity',
                'at': failed,
                'elapsedSeconds': 62,
            },
        )
        self.o.store.event(
            'acct',
            self.live['device'],
            failed,
            'failed',
            QWEN,
            MEMORY_WAIT + ' Safe restoration could not be verified; no forced recovery restart '
            'was sent. Automatic switching is paused. Review Help & feedback on the Mac.',
        )
        self.h.db.execute(
            'INSERT INTO opt_ready_minutes VALUES(?,?,?,?,?,?,?,?)',
            ('acct', self.live['device'], int(failed - 120), GEMMA, 60, 3, 90, 60),
        )
        self.o.live['hardware']['memoryAvailableGB'] = 40.8  # 85% free
        self.o.live['provider']['memoryGB'] = 0
        self.o.raw['written_at'] = self.now
        self.o.read_state.return_value = copy.deepcopy(self.o.raw)
        self.o.identity_ok = False  # the cold model is not serving-eligible on the roster
        self.o.device_identity_ok = True
        self.o.identity_session = (self.o.raw['started_at'], self.o.raw['pid'])
        self.o.manager.observed_since = self.now - 3600

    def test_turning_on_with_a_cold_model_is_admitted_and_restores_gemma_at_once(self):
        self.live_state()
        saved = copy.deepcopy(self.o.state)
        with self.assertRaisesRegex(ValueError, 'Warm and ready before resuming'):
            self.o.identity_ok = True
            self.o.demand_resume_context(
                {**saved, 'demandPolicy': policy({'managerStrategy': 0})}, QWEN
            )
        self.o.identity_ok = False
        self.o.demand_resume_context(saved, QWEN)  # the manager's On is not refused
        self.o.resume_demand(
            {'action': 'resume-demand', 'expectedControl': self.o.control_version(), 'currentModel': QWEN},
            'mac',
            return_snapshot=False,
        )
        self.assertEqual((self.o.state['mode'], self.o.state['expectedModel']), ('demand', QWEN))
        self.at(0)
        self.assertEqual([c[0] for c in self.commands], [GEMMA])
        detail = self.commands[0][1]
        self.assertIn(QWEN + ' never loaded after the switch at', detail)
        self.assertIn('waiting for enough available memory to pre-warm', detail)
        self.assertIn('Restoring ' + GEMMA + ' and verifying it is warm', detail)
        self.assertEqual(self.o.state['mode'], 'demand')
        self.assertEqual(self.o.state['lastSwitchResult']['outcome'], 'recovered')
        self.assertIn('Watchdog restored ' + GEMMA, self.events('manager-notice')[0])

    def test_turning_on_on_battery_is_admitted_and_restores_gemma(self):
        # Sep 28 22:50: On sat at "Model switching waits while the Mac is on battery power".
        self.live_state()
        self.o.on_ac_power = Mock(return_value=False)
        saved = copy.deepcopy(self.o.state)
        with self.assertRaisesRegex(ValueError, 'battery'):
            self.o.identity_ok = True
            self.o.demand_resume_context(
                {**saved, 'demandPolicy': policy({'managerStrategy': 0})}, QWEN
            )
        self.o.identity_ok = False
        self.o.demand_resume_context(saved, QWEN)
        self.o.resume_demand(
            {'action': 'resume-demand', 'expectedControl': self.o.control_version(), 'currentModel': QWEN},
            'mac',
            return_snapshot=False,
        )
        self.assertEqual(self.o.state['mode'], 'demand')
        self.at(0)
        self.assertEqual([c[0] for c in self.commands], [GEMMA])

    def test_manager_turns_on_with_only_the_serving_model_available(self):
        # Sep 27 18:17: 0.9.10 listed only the enabled model, so no alternative was available.
        self.live_state()
        saved = copy.deepcopy(self.o.state)
        rows = [
            {'id': QWEN, 'available': True, 'selected': True},
            {'id': GEMMA, 'available': False, 'selected': True, 'reason': 'Not available'},
        ]
        self.o.candidates = lambda *args, **kwargs: rows
        self.assertEqual(self.o.demand_resume_context(saved, QWEN)[3], {QWEN})

    def test_status_view_carries_the_concrete_reason(self):
        self.live_state()
        self.o.state['mode'] = 'demand'
        self.o.state['expectedModel'] = QWEN
        self.o.manager.observed_since = self.now  # just launched: readings settle first
        self.at(0)
        self.assertEqual(self.commands, [])
        view = self.o.manager.watchdog_view(self.o.state, self.o.raw, self.now)
        self.assertIn(QWEN + ' never loaded after the switch at', view['reason'])
        self.assertIn('BloomGauge restores ' + GEMMA, view['reason'])
        self.assertEqual(view['darkSince'], self.now - 5400)

    def test_1712_manual_pick_that_leaves_no_provider_running_is_started_again(self):
        self.cold_qwen(-600)
        self.o.state['expectedModel'] = QWEN
        self.o.read_state.return_value = copy.deepcopy(self.o.raw)
        self.o.raw['written_at'] = self.now
        self.o.manual_action(
            {
                'action': 'switch',
                'model': GEMMA,
                'expectedSession': session_key(self.o.raw),
                'requestId': str(uuid.uuid4()),
            }
        )
        self.assertEqual(self.o.state['manager']['resume']['model'], GEMMA)

        def stops(target, options, environment):
            # 0.9.10 drained, restarted qwen (provider.toml still enabled qwen), then exited.
            self.commands.append((target, self.o.detail))
            self.o.read_options.return_value = (QWEN, OPTIONS, {})
            self.o.raw['written_at'] = self.now - 60
            self.o.read_state.return_value = copy.deepcopy(self.o.raw)
            self.o.live['hardware']['memoryAvailableGB'] = 40.8  # nothing is loaded now
            self.o.command.side_effect = self.started

        from optimizer import ExternalChange

        self.o.command.side_effect = stops
        self.o.verify_started = Mock(side_effect=ExternalChange('Provider settings changed.'))
        with patch('manager.matching_process', return_value=False):
            self.o.switch(QWEN, GEMMA, 'acct', self.live['device'])
        self.assertEqual(self.o.state['mode'], 'demand')
        home = self.o.state['manager']['home']
        self.assertEqual((home['model'], home['source']), (GEMMA, 'manual'))
        self.assertIn(
            'Darkbloom stopped after the switch to ' + GEMMA,
            self.o.state['manualResult']['detail'],
        )
        self.o.verify_started = Mock(side_effect=self.verified)
        with patch('manager.matching_process', return_value=False):
            for offset in (15, 30, 60):
                self.at(offset, online=False, written=False)
            self.assertEqual(len(self.commands), 1)
            self.assertIn('Starting ' + GEMMA + ' at', self.o.detail)
            self.at(105, online=False, written=False)
        self.assertEqual([c[0] for c in self.commands], [GEMMA, GEMMA])
        self.assertIn('Started %s again' % GEMMA, self.events('manager-notice')[-1])
        self.assertEqual(self.o.state['mode'], 'demand')

    def test_a_switch_that_lands_in_startup_preload_is_not_a_failed_switch(self):
        """0.9.10 preloads its models at every start and, until it registers, rewrites
        daemon-state only every 30 s: mid-preload it is starting, not stopped."""
        from optimizer import ExternalChange

        for preload, stopped in (([GEMMA], False), ([], True)):
            with self.subTest(preload=preload):
                self.cold_qwen(-600)
                self.o.state['expectedModel'] = QWEN
                self.o.state.pop('lastSwitchFailure', None)
                self.o.raw['written_at'] = self.now
                self.o.read_state.return_value = copy.deepcopy(self.o.raw)
                self.o.manual_action(
                    {
                        'action': 'switch',
                        'model': GEMMA,
                        'expectedSession': session_key(self.o.raw),
                        'requestId': str(uuid.uuid4()),
                    }
                )

                def preloading(target, options, environment, preload=preload):
                    self.started(target, options, environment)
                    self.o.raw.pop('trust', None)  # not registered yet
                    self.o.raw['startup_preload_pending_models'] = preload
                    self.o.raw['written_at'] = self.now - 25  # the last 30 s liveness write
                    self.o.read_state.return_value = copy.deepcopy(self.o.raw)

                self.o.command.side_effect = preloading
                self.o.verify_started = Mock(
                    side_effect=ExternalChange('Provider settings changed.')
                )
                with patch('manager.time.time', return_value=self.now):
                    self.o.switch(QWEN, GEMMA, 'acct', self.live['device'])
                detail = self.o.state['manualResult']['detail']
                self.assertEqual(
                    'Darkbloom stopped after the switch to ' + GEMMA in detail, stopped
                )
                self.assertEqual('lastSwitchFailure' in self.o.state, stopped)

    def test_a_stop_bloomkeeper_did_not_cause_is_respected(self):
        self.o.manager.last_command = {'at': self.now - 3600, 'target': GEMMA}
        with patch('manager.matching_process', return_value=False):
            for offset in range(0, 600, 60):
                self.at(offset, online=False, written=False)
        self.assertEqual(self.commands, [])

    def test_1715_gemma_started_by_hand_is_adopted_on_the_next_tick(self):
        self.o.state['expectedModel'] = QWEN
        self.o.state['manager']['home'] = manager.pin(GEMMA, self.now - 200)  # the 17:12 pick
        self.o.manager.last_command = {'at': self.now - 150, 'target': GEMMA}
        self.o.raw.update(started_at=self.now - 30, pid=9)  # gemma loaded and advertised
        self.at(0)
        self.assertEqual(self.o.state['expectedModel'], GEMMA)
        self.assertEqual(self.o.state['mode'], 'demand')
        self.at(15)
        self.assertEqual(self.commands, [])
        home = self.o.state['manager']['home']
        self.assertEqual((home['model'], home['source'], home['failures']), (GEMMA, 'manual', 0))
        self.o.state['manager']['home'] = {'model': GEMMA, 'source': 'history', 'at': 0}
        self.o.state['expectedModel'] = QWEN
        self.at(30)
        self.assertEqual(self.o.state['manager']['home']['source'], 'external')

    def test_launch_agent_and_provider_toml_disagree(self):
        import plistlib

        self.o.read_options = Optimizer.read_options.__get__(self.o)
        self.o.plist_path.parent.mkdir(parents=True, exist_ok=True)
        self.o.plist_path.write_bytes(
            plistlib.dumps(
                {'ProgramArguments': [str(self.o.binary), 'start', '--model', QWEN, *OPTIONS]}
            )
        )
        toml = self.o.home / '.config/darkbloom/provider.toml'
        toml.parent.mkdir(parents=True, exist_ok=True)
        toml.write_text(
            "[backend]\nenabled_models = [ '%s' ]\npreload_models = [ '%s' ]\n" % (GEMMA, GEMMA)
        )
        self.assertEqual(manager.toml_selection(self.o.home, OPTIONS), GEMMA)
        self.o.read_state.return_value = {**self.o.raw, 'advertised_models': [GEMMA]}
        self.assertEqual(self.o.read_options()[0], GEMMA)  # what 0.9.10 actually runs
        self.assertEqual(self.o.read_options(plist_only=True)[0], QWEN)
        self.o.read_state.return_value = {**self.o.raw, 'advertised_models': [QWEN]}
        self.assertEqual(self.o.read_options()[0], QWEN)

    def test_admission_counts_memory_held_by_the_loaded_model(self):
        self.o.raw.update(advertised_models=[QWEN], warm_models=[QWEN])
        self.o.raw['capacity']['gpu_memory_active_gb'] = 0  # not reported by the provider
        live = {
            **self.o.live,
            'provider': {**self.o.live['provider'], 'memoryGB': 0},
            'hardware': {**self.o.live['hardware'], 'memoryAvailableGB': 12},
        }
        budget = self.o.selection_budget(GEMMA, live, self.o.raw)
        # The CLI estimate pads weights by 1.2; only the weights are freed.
        self.assertAlmostEqual(budget['afterUnloadGB'], 12 + 20.9 / 1.2)
        self.assertGreater(budget['afterUnloadGB'], budget['requiredGB'])
        live['provider']['memoryGB'] = 25  # reported: never counted twice
        budget = self.o.selection_budget(GEMMA, live, self.o.raw)
        self.assertAlmostEqual(budget['afterUnloadGB'], 37)


class PairHoldTests(unittest.TestCase):
    """The manager holds any pair without gemma as its home; gemma is served alone."""

    def test_which_selections_the_manager_can_hold_and_pool_membership(self):
        pair = selection_key(['a', 'b'])
        self.assertIsNone(manager.hold_error('a'))
        self.assertIsNone(manager.hold_error(pair))
        self.assertIn('Gemma is served alone', manager.hold_error(selection_key([GEMMA, 'a'])))
        self.assertIsNotNone(manager.hold_error(None))
        self.assertTrue(manager.in_pool(pair, ['a', 'b', 'c']))
        self.assertTrue(manager.in_pool(pair, [pair]))
        self.assertFalse(manager.in_pool(pair, ['a', 'c']))
        self.assertTrue(manager.in_pool('a', ['a']))
        self.assertFalse(manager.in_pool(None, ['a']))

    def test_a_pair_without_history_becomes_home_and_is_held(self):
        pair = selection_key(['a', 'b'])
        home = manager.choose_home({}, {}, lambda: [], pair, {'a', 'b'}, NOW)
        self.assertEqual(home, {'model': pair, 'source': 'current', 'at': NOW})
        gemma_pair = selection_key([GEMMA, 'a'])
        self.assertIsNone(manager.choose_home({}, {}, lambda: [], gemma_pair, {'a'}, NOW))
        d = managed(legacy_upgrade(), current=pair, home=pair, source='current')
        self.assertIsNone(d['target'])
        self.assertEqual(d['manager']['action'], 'hold')
        self.assertEqual(d['reason'], 'Holding home model a + b.')


class PairRestoreTests(Harness):
    def test_a_failed_move_away_from_a_pair_restores_the_pair(self):
        pair = selection_key(['a', 'b'])
        for previous, restored in ((pair, pair), (selection_key([GEMMA, 'a']), None)):
            with self.subTest(previous=previous):
                m = {
                    'recovery': {
                        'failedTarget': 'c',
                        'previous': previous,
                        'at': self.now - 600,
                        'attempts': 0,
                    }
                }
                control = self.o.manager
                control.dispatch = Mock(return_value=True)
                settings = {**self.o.state, 'manager': m}
                control.recover(self.now, settings, self.o.live, self.o.raw, 'c', m)
                self.assertEqual(control.dispatch.call_args.args[1], restored)

    def test_an_outside_change_to_a_pair_without_gemma_is_kept_as_the_pick(self):
        pair = selection_key(['a', 'b'])
        self.o.raw.update(advertised_models=['a', 'b'], warm_models=['a', 'b'])
        control = self.o.manager
        self.assertTrue(control.external(self.now, self.o.state, self.o.live, self.o.raw, pair))
        home = self.o.state['manager']['home']
        self.assertEqual((home['model'], home['source']), (pair, 'external'))
        self.assertEqual(self.o.state['expectedModel'], pair)
        # A gemma pair is served as it is, never adopted as the pick.
        gemma_pair = selection_key([GEMMA, 'a'])
        self.o.raw.update(advertised_models=[GEMMA, 'a'])
        self.o.state['manager']['home'] = {'model': 'a', 'source': 'history', 'at': self.now}
        control.external(self.now, self.o.state, self.o.live, self.o.raw, gemma_pair)
        self.assertEqual(self.o.state['manager']['home']['model'], 'a')


class TurnOnTests(unittest.TestCase):
    """The On control (OptimizerControl) admits a cold model under the manager."""

    def setUp(self):
        import test_optimizer_control

        self.case = test_optimizer_control.ControlTests('setUp')
        self.case.setUp()
        self.addCleanup(self.case.tearDown)

    def test_on_with_a_cold_model_is_not_refused(self):
        case = self.case
        case.o.warmup = {}
        case.o.raw['warm_models'] = []
        case.o.read_state.return_value = copy.deepcopy(case.o.raw)
        case.enable()
        case.control.tick()
        self.assertEqual(case.o.state['mode'], 'demand')
        self.assertEqual(case.control.operation['status'], 'active')
