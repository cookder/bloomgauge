import copy, json, pathlib, tempfile, threading, unittest
from unittest.mock import Mock, patch
import test_optimizer as controller_fixture
import test_prewarm as warm_fixture
from history import History
from optimizer import Optimizer, launch_options, session_key
from optimizer_store import OptimizerStore, device_id
from model_combinations import (
    members,
    selection_key,
    same_selection,
    pair_budget,
    pair_candidates,
    combination_config_error,
    configured_reserve_gb,
)
from optimizer import memory_budget
from prewarm import WarmupError


class CombinationPolicyTests(unittest.TestCase):
    def test_generated_provider_default_is_safe_for_solo_and_pairs(self):
        with tempfile.TemporaryDirectory() as root:
            p = pathlib.Path(root) / '.config/darkbloom/provider.toml'
            p.parent.mkdir(parents=True)
            for text in (
                'config_version = 3\n[provider]\nname = "fixture"\nmemory_reserve_gb = 4\nauto_update = true\n[backend]\nport = 8100\ncontinuous_batching = true\nenabled_models = []\nidle_timeout_mins = 60',
                'provider.memory_reserve_gb = 4 # generated default',
                '["provider"]\n"memory_reserve_gb" = 4',
            ):
                p.write_text(text)
                for pair in (True, False):
                    with self.subTest(text=text, pair=pair):
                        self.assertIsNone(combination_config_error(root, [], {}, require_pair=pair))
                self.assertEqual(p.read_text(), text)

    def test_memory_reserve_is_read_and_other_memory_settings_stop_automatic_moves(self):
        with tempfile.TemporaryDirectory() as root:
            p = pathlib.Path(root) / '.config/darkbloom/provider.toml'
            p.parent.mkdir(parents=True)
            for text, reserve in (
                ('[provider]\nmemory_reserve_gb = 8', 8),
                ('[provider]\nname = "x"\nmemory_reserve_gb = 0 # all of it', 0),
                ('provider.memory_reserve_gb = 12', 12),
                ('[backend]\nport = 8100', 4),
            ):
                p.write_text(text)
                self.assertEqual(configured_reserve_gb(root, []), reserve)
                for pair in (True, False):
                    with self.subTest(text=text, pair=pair):
                        self.assertIsNone(combination_config_error(root, [], {}, require_pair=pair))
            for text in (
                '[provider]\nmemory_reserve_gb = "4"',
                '[provider]\nmemory_reserve_gb = true',
                '[backend]\nmemory_reserve_gb = 4',
                '[other]\nprovider.memory_reserve_gb = 4',
                '[[provider]]\nmemory_reserve_gb = 4',
                '[provider]\nmemory_reserve_gb = 4\nkv_reserve_gb = 1',
                '[provider]\nmemory_reserve_gb = 4\nmemory_reserve_gb = 4',
                '[provider]\nmemory_reserve_gb = 4.5',
            ):
                p.write_text(text)
                for pair in (True, False):
                    with self.subTest(text=text, pair=pair):
                        self.assertIsNotNone(
                            combination_config_error(root, [], {}, require_pair=pair)
                        )
                        # Restores, the watchdog and the user's own picks don't stop.
                        self.assertIsNone(
                            combination_config_error(
                                root, [], {}, require_pair=pair, voluntary=False
                            )
                        )
                self.assertEqual(configured_reserve_gb(root, []), 4)

    def test_only_env_knobs_that_change_load_admission_stop_automatic_moves(self):
        with tempfile.TemporaryDirectory() as root:
            # What Darkbloom's installer copies into the launch agent (LaunchAgent.swift
            # passthroughEnvKeys) and what its docs suggest: none enter the load gate.
            for env in (
                {'DARKBLOOM_PREFIX_CACHE': '0'},
                {'DARKBLOOM_DRAIN_TIMEOUT_SECONDS': '900', 'DARKBLOOM_CBV2_PAGED_KV': '0'},
                {'DARKBLOOM_MLX_CACHE_LIMIT_GB': '4', 'DARKBLOOM_MLX_MEMORY_RESERVE_GB': '8'},
                {'DARKBLOOM_PREFIX_CACHE_MEMORY': '0', 'DARKBLOOM_KV_BACKEND_GUARD': '/tmp/g'},
                {'MLX_MEMORY_LIMIT': '1', 'METAL_DEVICE_WRAPPER_TYPE': '1'},
                {'DARKBLOOM_MEM_CAP_FRACTION': ''},
            ):
                with self.subTest(env=env):
                    self.assertIsNone(combination_config_error(root, [], env))
            for env in (
                {'DARKBLOOM_MEM_CAP_FRACTION': '0.8'},
                {'DARKBLOOM_ACTIVATION_RESERVE_GB': '8', 'DARKBLOOM_PREFIX_CACHE': '0'},
            ):
                with self.subTest(env=env):
                    self.assertIn(next(iter(env)), combination_config_error(root, [], env))
                    self.assertIsNone(combination_config_error(root, [], env, voluntary=False))

    def test_load_reserve_is_darkbloom_s_load_gate_reserve(self):
        # UnifiedMemoryCap.loadReserveBytes = max(memory_reserve_gb, physical - hard cap),
        # hard cap = min(90% of RAM, RAM - 2 GiB).
        for total, configured, reserve in (
            (48, 4, 4.8),
            (48, 8, 8),
            (128, 4, 12.8),
            (16, 4, 4),
            (16, 0, 2),
            (32, 1, 3.2),
        ):
            hardware = {'memoryTotalGB': total, 'memoryAvailableGB': total}
            with self.subTest(total=total, configured=configured):
                solo = memory_budget(hardware, {'memoryGB': 0}, 'm', 10, config_reserve=configured)
                self.assertAlmostEqual(solo['reserveGB'], reserve)
                self.assertAlmostEqual(solo['requiredGB'], 10 + reserve + 5.5 + 1)
                pair = pair_budget(
                    hardware, {'memoryGB': 0}, ['a', 'b'], [5, 5], config_reserve=configured
                )
                self.assertAlmostEqual(pair['reserveGB'], reserve)
        # Unchanged for the default reserve.
        self.assertAlmostEqual(
            memory_budget({'memoryTotalGB': 48, 'memoryAvailableGB': 20}, {'memoryGB': 0}, 'm', 10)[
                'reserveGB'
            ],
            4.8,
        )

    def test_exact_pair_identity_and_repeatable_cli_arguments(self):
        key = selection_key(['b', 'a'])
        self.assertEqual(members(key), ['a', 'b'])
        self.assertEqual(selection_key(['a', 'b']), key)
        self.assertEqual(selection_key(['a']), 'a')
        self.assertTrue(same_selection({'advertised_models': ['b', 'a']}, key))
        self.assertIsNone(selection_key(['a', 'a']))
        self.assertIsNone(selection_key(['a', 'b', 'c']))
        self.assertEqual(
            launch_options(
                {
                    'ProgramArguments': [
                        'binary',
                        'start',
                        '--model',
                        'b',
                        '--model',
                        'a',
                        '--local-endpoint',
                    ]
                }
            ),
            (key, ['--local-endpoint']),
        )

    def test_pair_budget_has_joint_weights_shared_activation_and_kv_for_each(self):
        h = {'memoryTotalGB': 48, 'memoryAvailableGB': 20}
        b = pair_budget(
            h, {'memoryGB': 14}, ['gpt-oss-20b', 'gemma-4-26b-qat-4bit'], [13.496, 17.444], 1
        )
        self.assertAlmostEqual(b['requiredGB'], 43.24)
        self.assertEqual(b['afterUnloadGB'], 35)
        rows = [
            {'id': 'a', 'name': 'A', 'memoryGB': 13, 'available': True},
            {'id': 'b', 'name': 'B', 'memoryGB': 17, 'available': True},
            {'id': 'c', 'name': 'C', 'memoryGB': 30, 'available': True},
        ]
        pairs = pair_candidates(rows, h, {'memoryGB': 14})
        self.assertTrue(pairs[0]['available'])
        self.assertFalse(pairs[0]['fitsNow'])
        self.assertTrue(any(not p['available'] for p in pairs))

    def test_custom_slot_and_memory_limits_do_not_silently_use_defaults(self):
        with tempfile.TemporaryDirectory() as root:
            p = pathlib.Path(root) / '.config/darkbloom/provider.toml'
            p.parent.mkdir(parents=True)
            for text in (
                '[backend]\nmax_model_slots = 1',
                '[backend]\nmax_model_slots = "two"',
                '[backend]\nmemory_reserve_gb = 9',
                '[backend]\n"max_model_slots" = 1',
                'backend.max_model_slots = 1',
                'backend = { max_model_slots = 1 }',
                'resources = { memory_reserve_gb = 40 }',
            ):
                p.write_text(text)
                self.assertIsNotNone(combination_config_error(root, [], {}))
            p.write_text(
                '[backend]\nmax_model_slots = 2\nstartup_preload = true\npreload_models = ["a"]'
            )
            self.assertIsNone(combination_config_error(root, [], {}))
            self.assertIsNone(combination_config_error(root, [], {'MLX_MEMORY_LIMIT': '1'}))
            self.assertIsNotNone(
                combination_config_error(root, [], {'DARKBLOOM_MEM_CAP_FRACTION': '0.8'})
            )
            self.assertIsNotNone(combination_config_error(root, ['--config', 'relative.toml'], {}))
            p.unlink()
            legacy = pathlib.Path(root) / 'Library/Application Support/darkbloom/provider.toml'
            legacy.parent.mkdir(parents=True)
            legacy.write_text('[backend]\nmax_model_slots = 1')
            self.assertIsNotNone(combination_config_error(root, [], {}))


class ReserveControllerTests(unittest.TestCase):
    """provider.toml memory_reserve_gb is Darkbloom's load reserve; Bloomkeeper budgets with it."""

    setUp = controller_fixture.ControllerTests.setUp
    tearDown = controller_fixture.ControllerTests.tearDown

    def test_load_budgets_use_the_configured_memory_reserve(self):
        config = pathlib.Path(self.tmp.name) / '.config/darkbloom/provider.toml'
        config.parent.mkdir(parents=True)
        config.write_text('[provider]\nmemory_reserve_gb = 8\n')
        budget = self.o.selection_budget('b', self.o.live, self.o.raw)
        self.assertEqual(budget['reserveGB'], 8)  # a 48 GB Mac: 10% alone would be 4.8
        self.assertEqual(budget['requiredGB'], 12 + 8 + 5.5 + 1)
        manual = {m['id']: m for m in self.o.manual_snapshot()['models']}
        self.assertEqual(manual['b']['loadBudget']['requiredGB'], 12 + 8 + 5.5 + 1)
        config.write_text('[provider]\nmemory_reserve_gb = 4\n')
        budget = self.o.selection_budget('b', self.o.live, self.o.raw)
        self.assertAlmostEqual(budget['reserveGB'], 4.8)

    def test_pair_restores_are_budgeted_even_with_knobs_that_stop_automatic_moves(self):
        pair = selection_key(['a', 'b'])
        self.o.read_options.return_value = (
            'a',
            ['--local-endpoint'],
            {'DARKBLOOM_MEM_CAP_FRACTION': '0.8'},
        )
        self.assertIsNotNone(self.o.selection_budget(pair, self.o.live, self.o.raw))
        # Voluntary pair tests (legacy combo mode) are still refused.
        self.assertIn('DARKBLOOM_MEM_CAP_FRACTION', self.o.combo_config_error())


class CombinationControllerTests(unittest.TestCase):
    def setUp(self):
        controller_fixture.ControllerTests.setUp(self)
        del self.o.snapshot
        self.clock = patch('optimizer.time.time', side_effect=lambda: self.now)
        self.clock.start()
        self.o.live['hardware']['memoryAvailableGB'] = 40
        self.o.state.update(startedAt=self.now - 100, endsAt=self.now + 100)

    def tearDown(self):
        self.clock.stop()
        controller_fixture.ControllerTests.tearDown(self)

    def queue(self):
        return self.o.control_action(
            {'action': 'schedule-combos', 'expectedControl': self.o.control_version()}, 'phone'
        )

    def fresh(self):
        self.o.live['at'] = self.now
        self.o.live['earnings']['updatedAt'] = self.now
        self.o.identity_at = self.now
        self.o.discovery_at = self.now
        self.o.raw['written_at'] = self.now
        self.o.read_state.return_value = copy.deepcopy(self.o.raw)
        self.net.snapshot.return_value['capacity']['updatedAt'] = self.now

    def test_queue_preserves_solo_test_and_survives_dashboard_restart(self):
        before = {
            k: copy.deepcopy(self.o.state[k])
            for k in ('mode', 'models', 'startedAt', 'endsAt', 'expectedModel', 'lastSwitchAt')
        }
        version = self.o.control_version()
        self.queue()
        self.assertEqual({k: self.o.state[k] for k in before}, before)
        self.assertEqual(self.o.state['comboPlan']['pairs'], [['a', 'b']])
        self.assertNotEqual(version, self.o.control_version())
        reopened = Optimizer(self.h, self.net, self.tmp.name, threading.Event(), Mock())
        self.assertEqual(reopened.state['comboPlan'], self.o.state['comboPlan'])
        self.o.runner.assert_not_called()

    def test_reopening_waits_for_initial_observation_without_losing_either_phase(self):
        self.queue()
        for mode, status in (('week', 'queued'), ('combo', 'running')):
            self.o.state['mode'] = mode
            self.o.state['comboPlan']['status'] = status
            self.o.save()
            reopened = Optimizer(self.h, self.net, self.tmp.name, threading.Event(), Mock())
            before = copy.deepcopy(reopened.state)
            reopened.tick(self.now)
            self.assertEqual(reopened.state, before)
            self.assertEqual(reopened.status, 'waiting')
            reopened.runner.assert_not_called()

    def test_temporarily_missing_identity_waits_but_a_changed_identity_pauses(self):
        self.queue()
        self.o.live['device'] = ''
        self.o.tick(self.now)
        self.assertEqual(self.o.state['comboPlan']['status'], 'queued')
        self.o.live['device'] = 'another-device'
        self.o.tick(self.now)
        self.assertEqual(self.o.state['mode'], 'observe')
        self.assertEqual(self.o.state['comboPlan']['status'], 'cancelled')

    def test_solo_deadline_transitions_once_to_its_queued_phase(self):
        self.queue()
        original_end = self.o.state['endsAt']
        self.now = original_end + 1
        self.fresh()
        self.o.tick(self.now)
        plan = self.o.state['comboPlan']
        self.assertEqual(self.o.state['mode'], 'combo')
        self.assertEqual(self.o.state['endsAt'], original_end)
        self.assertEqual(plan['endsAt'] - plan['startedAt'], 604800)
        self.assertEqual(plan['configurations'], ['a', selection_key(['a', 'b'])])
        self.o.runner.assert_not_called()

    def test_cancel_queued_phase_leaves_solo_test_running(self):
        self.queue()
        self.o.control_action(
            {'action': 'cancel-combos', 'expectedControl': self.o.control_version()}, 'phone'
        )
        self.assertEqual(self.o.state['mode'], 'week')
        self.assertEqual(self.o.state['comboPlan']['status'], 'cancelled')

    def test_pause_or_external_change_cancels_follow_on(self):
        self.queue()
        self.o.action({'action': 'pause'})
        self.assertEqual(self.o.state['comboPlan']['status'], 'cancelled')
        self.o.state['mode'] = 'week'
        self.queue()
        self.o.read_options.return_value = ('external', [], {})
        self.o.tick(self.now)
        self.assertEqual(self.o.state['comboPlan']['status'], 'cancelled')

    def test_stale_control_and_unknown_pair_cannot_schedule(self):
        version = self.o.control_version()
        self.queue()
        with self.assertRaises(ValueError):
            self.o.control_action({'action': 'schedule-combos', 'expectedControl': version})
        with self.assertRaises(ValueError):
            self.o.control_action(
                {
                    'action': 'schedule-combos',
                    'pairs': [['a', 'missing']],
                    'expectedControl': self.o.control_version(),
                }
            )

    def test_changed_parent_test_cannot_activate_queued_phase(self):
        self.queue()
        self.o.state['startedAt'] -= 100
        self.now = self.o.state['endsAt'] + 1
        self.o.tick(self.now)
        self.assertEqual(self.o.state['mode'], 'observe')
        self.assertEqual(self.o.state['comboPlan']['status'], 'cancelled')

    def test_pair_phase_completion_does_not_restart_the_provider(self):
        self.queue()
        self.now = self.o.state['endsAt'] + 1
        self.o.tick(self.now)
        self.fresh()
        self.o.tick(self.now)
        self.now = self.o.state['comboPlan']['endsAt'] + 1
        self.o.tick(self.now)
        self.assertEqual(self.o.state['mode'], 'observe')
        self.assertEqual(self.o.state['comboPlan']['status'], 'completed')
        self.o.runner.assert_not_called()

    def test_missing_hardware_at_deadline_waits_without_cancelling(self):
        self.queue()
        self.now = self.o.state['endsAt'] + 1
        self.fresh()
        self.o.live['hardware']['memoryAvailableGB'] = None
        self.o.tick(self.now)
        self.assertEqual(self.o.state['comboPlan']['status'], 'queued')
        self.assertEqual(self.o.state['mode'], 'week')
        self.assertIn('waiting', self.o.detail)

    def test_pair_command_is_two_arguments_not_an_encoded_model(self):
        self.o.command(selection_key(['b', 'a']), ['--local-endpoint'], {})
        args = self.o.runner.call_args.args[0]
        self.assertEqual(args[-4:], ['--model', 'a', '--model', 'b'])

    def test_delayed_load_failure_cancels_follow_on_in_either_phase(self):
        for phase in ('week', 'combo'):
            self.o.state['mode'] = 'week'
            self.queue()
            self.o.state['mode'] = phase
            self.o.state['comboPlan']['status'] = 'running' if phase == 'combo' else 'queued'
            self.o.state.update(lastSwitchAt=self.now - 10, rollbackModel='b')
            self.o.raw['last_model_load_error'] = {'model': 'a', 'at': self.now - 2}
            self.o.raw['warm_models'] = []
            self.o.tick(self.now)
            self.assertEqual(self.o.state['comboPlan']['status'], 'cancelled')
            self.assertEqual(self.o.state['mode'], 'observe')
            self.o.state['requestedModel'] = None
            self.o.raw.pop('last_model_load_error')

    def test_recovery_can_restore_the_previous_pair_without_changing_its_members(self):
        key = selection_key(['a', 'b'])
        self.o.raw.update(advertised_models=['a', 'b'], warm_models=['a', 'b'])
        self.o.warmup = {
            'session': session_key(self.o.raw),
            'model': key,
            'status': 'ready',
            'verifiedAt': self.now - 1,
        }
        self.o.read_state.return_value = copy.deepcopy(self.o.raw)
        self.o.read_options.return_value = (key, ['--local-endpoint'], {})
        self.o.recovery_ready.return_value = copy.deepcopy(self.o.raw)
        self.o.command = Mock()
        self.o.verify_started = Mock(side_effect=[False, True])
        self.o.switch(key, 'a', 'acct', self.o.live['device'])
        self.assertEqual([c.args[0] for c in self.o.command.call_args_list], ['a', key])
        self.assertEqual(self.o.state['mode'], 'observe')
        self.assertIn('Restored', self.o.detail)

    def test_fresh_memory_drop_prevents_the_actual_restart(self):
        self.o.live['hardware']['memoryAvailableGB'] = 0
        self.o.command = Mock()
        self.o.switch('a', selection_key(['a', 'b']), 'acct', self.o.live['device'])
        self.o.command.assert_not_called()

    def test_manual_switch_can_leave_a_pair_and_cancels_queued_automation(self):
        import uuid

        self.queue()
        key = selection_key(['a', 'b'])
        self.o.raw.update(advertised_models=['b', 'a'])
        self.o.read_state.return_value = copy.deepcopy(self.o.raw)
        self.o.read_options.return_value = (key, ['--local-endpoint'], {})
        self.o.state['expectedModel'] = key
        self.o.manual_action(
            {
                'action': 'switch',
                'model': 'a',
                'expectedSession': session_key(self.o.raw),
                'requestId': str(uuid.uuid4()),
            },
            'phone',
        )
        self.assertEqual(self.o.state['requestedModel'], 'a')
        self.assertEqual(self.o.state['mode'], 'observe')
        self.assertEqual(self.o.state['comboPlan']['status'], 'cancelled')


class PairWarmupTests(unittest.TestCase):
    def setUp(self):
        warm_fixture.WarmupControllerTests.setUp(self)
        self.key = selection_key(['a', 'b'])
        self.raw.update(advertised_models=['a', 'b'], warm_models=[])
        self.o.raw = copy.deepcopy(self.raw)
        self.o.read_options.return_value = (self.key, ['--local-endpoint'], {})
        self.o.local.append({'id': 'b', 'estimated_memory_gb': 10})
        self.o.catalog.append({'id': 'b', 'active': True, 'min_ram_gb': 24})
        self.o.live['hardware']['memoryAvailableGB'] = 46

    def advance(self, seconds):
        self.now += seconds
        self.raw['written_at'] = self.now
        self.o.raw = copy.deepcopy(self.raw)
        self.o.live['at'] = self.now
        self.o.identity_at = self.now
        self.o.discovery_at = self.now
        return False

    def tearDown(self):
        warm_fixture.WarmupControllerTests.tearDown(self)

    def test_both_models_decode_and_remain_loaded_together(self):
        def decode(home, raw, options, model, **kwargs):
            self.raw['warm_models'].append(model)

        with patch('optimizer.prewarm', side_effect=decode) as call:
            self.assertTrue(self.o.perform_prewarm(self.key, self.raw, ['--local-endpoint']))
        self.assertEqual([c.args[3] for c in call.call_args_list], ['a', 'b'])
        self.o.raw = copy.deepcopy(self.raw)
        self.assertEqual(self.o.warmup_snapshot()['status'], 'ready')
        self.o.runner.assert_not_called()

    def test_loading_second_model_cannot_evict_first_and_claim_pair_ready(self):
        def decode(home, raw, options, model, **kwargs):
            self.raw['warm_models'] = [model]

        with patch('optimizer.prewarm', side_effect=decode) as call:
            with self.assertRaisesRegex(WarmupError, 'loaded together'):
                self.o.perform_prewarm(self.key, self.raw, ['--local-endpoint'])
        self.assertEqual(call.call_count, 2)
        self.assertEqual(self.o.warmup['status'], 'failed')
        self.o.runner.assert_not_called()

    def test_aggregate_served_counters_do_not_prove_each_member_warm(self):
        self.raw.update(
            warm_models=['a', 'b'], stats={'requests_served': 100, 'tokens_generated': 1000}
        )
        self.assertFalse(self.o.served_warm(self.raw, self.now))

    def test_work_arriving_between_decodes_prevents_second_warmup(self):
        def decode(home, raw, options, model, **kwargs):
            self.raw['warm_models'].append(model)
            self.raw['inference_active'] = True

        with patch('optimizer.prewarm', side_effect=decode) as call:
            with self.assertRaisesRegex(WarmupError, 'idle capacity'):
                self.o.perform_prewarm(self.key, self.raw, ['--local-endpoint'])
        self.assertEqual(call.call_count, 1)
        self.o.runner.assert_not_called()

    def test_cold_cache_failure_can_reach_guarded_recovery_instead_of_failing_early(self):
        self.raw['last_model_load_error'] = {'model': 'a', 'at': self.raw['started_at'] + 1}
        self.o.cache_recovery_needed = Mock(return_value=True)
        self.o.prewarm_reason = Mock(return_value=None)
        self.o.perform_prewarm = Mock(return_value=True)
        self.assertTrue(self.o.verify_started(self.key, self.raw['started_at'] - 1, timeout=30))
        self.o.perform_prewarm.assert_called_once()

    def test_no_purge_to_squeeze_a_second_model_beside_a_healthy_model(self):
        self.raw['warm_models'] = ['a']
        self.o.live['hardware'].update(memoryAvailableGB=1, cachedFilesGB=30)
        self.assertFalse(self.o.cache_recovery_needed(self.raw))
        self.assertIn(
            'memory', self.o.prewarm_reason(self.raw, self.now, allow_cache_recovery=True)
        )


class PairEvidenceTests(unittest.TestCase):
    def test_pair_paid_work_uses_only_both_warm_minutes(self):
        h = History(':memory:')
        s = OptimizerStore(h)
        key = selection_key(['a', 'b'])
        start = 1788742800
        s.identity('device', 'provider')

        def credit(i, model, micro, at, provider='provider'):
            from datetime import datetime, timezone

            return {
                'id': i,
                'provider_id': provider,
                'created_at': datetime.fromtimestamp(at, timezone.utc).isoformat(),
                'model': model,
                'amount_micro_usd': micro,
            }

        s.credits(
            'account',
            [
                credit(1, 'a', 100000, start),
                credit(2, 'b', 200000, start + 10),
                credit(3, 'base_reward', 500000, start + 20),
                credit(4, 'a', 900000, start + 20, 'another-device'),
            ],
            start + 180,
        )
        for at in range(start, start + 120, 10):
            s.sample('account', 'device', at, at + 10, key, 1, 10, False, all_warm=at < start + 60)
        result = s.evidence('account', 'device', start, start + 240, start + 240)
        self.assertNotIn('a', result)
        self.assertNotIn('b', result)
        pair = result[key]
        self.assertAlmostEqual(pair['usd'], 0.3)
        self.assertEqual(pair['jobs'], 2)
        self.assertAlmostEqual(pair['hours'], 1 / 60)
        self.assertEqual(pair['bothWarmPercent'], 100)
        self.assertAlmostEqual(pair['usdPerHour'], 18)
        self.assertEqual(pair['perModel']['a']['jobs'], 1)
        self.assertEqual(pair['perModel']['b']['jobs'], 1)
        h.close()


if __name__ == '__main__':
    unittest.main()
