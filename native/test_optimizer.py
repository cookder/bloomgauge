import copy, json, pathlib, plistlib, tempfile, threading, time, unittest, uuid
from datetime import datetime, timezone
from unittest.mock import Mock, patch
from history import History
from optimizer import (
    Optimizer,
    ExternalChange,
    launch_options,
    planned_model,
    best_candidate,
    memory_budget,
    session_key,
    activity_counters,
    same_activity,
    launch_signature,
    graceful_drain,
)
from optimizer_store import OptimizerStore, device_id
from demand_optimizer import policy

AT = 1788739200  # A fixed, minute-aligned instant.


def entry(i, at=AT + 20, model='a', provider='p', amount=1000000):
    return {
        'id': i,
        'created_at': datetime.fromtimestamp(at, timezone.utc).isoformat(),
        'model': model,
        'provider_id': provider,
        'amount_micro_usd': amount,
        'completion_tokens': 12,
    }


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.h = History(':memory:')
        self.s = OptimizerStore(self.h)
        self.s.identity('mac', 'p')

    def tearDown(self):
        self.h.close()

    def full_minute(self, at=AT, model='a'):
        for offset in range(0, 60, 3):
            self.s.sample('acct', 'mac', at + offset, at + offset + 3, model, 1, 20, False, True)

    def evidence(self):
        return self.s.evidence('acct', 'mac', AT, AT + 3600, AT + 4000)

    def test_only_this_mac_account_and_inference_count(self):
        self.full_minute()
        self.s.credits(
            'acct',
            [
                entry(1),
                entry(2, provider='another-mac'),
                entry(3, model='base_reward'),
                entry(4, at=AT - 1),
            ],
            AT + 180,
        )
        self.s.credits('other-account', [entry(5)], AT + 180)
        e = self.evidence()['a']
        self.assertEqual(e['usd'], 1)
        self.assertEqual(e['jobs'], 1)
        self.assertEqual(e['hours'], 1 / 60)
        self.assertEqual(e['usdPerHour'], 60)
        self.assertEqual(e['busyPercent'], 0)

    def test_idle_minutes_remain_in_denominator(self):
        self.full_minute()
        self.full_minute(AT + 60)
        self.s.credits('acct', [entry(1, at=AT - 1), entry(2)], AT + 180)
        self.assertEqual(self.evidence()['a']['usdPerHour'], 30)

    def test_old_credits_do_not_invent_run_time(self):
        self.s.credits('acct', [entry(1)], AT + 180)
        self.assertEqual(self.evidence(), {})

    def test_gaps_partial_minutes_and_settlement_lag(self):
        self.s.sample('acct', 'mac', AT, AT + 600, 'a', 100, 1000, True, True)
        self.assertEqual(self.evidence(), {})
        self.s.sample('acct', 'mac', AT, AT + 10, 'a', 1, 20, True, True)
        self.s.credits('acct', [entry(1, at=AT - 1)], AT + 180)
        self.assertEqual(self.evidence(), {})
        self.full_minute(AT + 120)
        self.assertEqual(self.s.evidence('acct', 'mac', AT, AT + 1000, AT + 250), {})

    def test_first_credit_mid_minute_does_not_claim_earlier_coverage(self):
        self.full_minute()
        self.s.credits('acct', [entry(1)], AT + 180)
        self.assertEqual(self.evidence(), {})

    def test_poll_overlap_deduplicates_and_disjoint_polls_keep_gap(self):
        self.s.credits('acct', [entry(1, at=AT - 1)], AT + 60)
        self.s.credits('acct', [entry(1, at=AT - 1), entry(2, at=AT + 30)], AT + 120)
        self.s.credits('acct', [entry(3, at=AT + 600)], AT + 660)
        with self.h.lock:
            self.assertEqual(
                self.h.db.execute('SELECT COUNT(*) FROM opt_coverage').fetchone()[0], 2
            )
            self.assertEqual(self.h.db.execute('SELECT COUNT(*) FROM opt_credits').fetchone()[0], 3)

    def test_switch_time_is_separate_from_warm_model_rate(self):
        self.full_minute()
        self.s.credits('acct', [entry(1, at=AT - 1), entry(2)], AT + 180)
        self.s.event('acct', 'mac', AT + 100, 'switched', 'a', 'Reconnected', 60)
        self.assertEqual(self.evidence()['a']['usdPerHour'], 60)
        self.assertEqual(self.evidence()['a']['switchMinutes'], 1)

    def test_device_rollover_ids_are_deduped(self):
        self.s.identity('mac', 'p')
        self.s.identity('mac', 'new-p')
        self.full_minute()
        self.s.credits('acct', [entry(1, at=AT - 1), entry(2, provider='new-p')], AT + 180)
        self.assertEqual(self.evidence()['a']['usd'], 1)

    def test_network_history_is_per_model_and_keeps_missing_data(self):
        self.s.network(
            AT,
            [
                {
                    'id': 'a',
                    'active_requests': 5,
                    'queued_requests': 3,
                    'warm_providers': 4,
                    'routable_providers': 8,
                }
            ],
        )
        self.s.network(
            AT + 600,
            [
                {
                    'id': 'a',
                    'active_requests': 0,
                    'queued_requests': 0,
                    'warm_providers': 0,
                    'routable_providers': 2,
                }
            ],
        )
        self.s.network(
            AT,
            [
                {
                    'id': 'bad',
                    'active_requests': float('nan'),
                    'queued_requests': 0,
                    'warm_providers': 0,
                    'routable_providers': 2,
                }
            ],
        )
        chart = self.s.chart('acct', 'mac', 'a', AT, AT + 1000, AT + 2000)
        self.assertEqual(chart['samples'][0]['pressure'], 2)
        self.assertIsNone(chart['samples'][1]['pressure'])
        self.assertIsNone(chart['samples'][0]['usdPerHour'])

    def test_six_hours_across_days_allows_confirmed_zero_traffic_baseline(self):
        for at in list(range(AT, AT + 14400, 60)) + list(range(AT + 86400, AT + 93600, 60)):
            self.full_minute(at)
        self.s.credits('acct', [entry(1, at=AT - 1)], AT + 100000)
        e = self.s.evidence('acct', 'mac', AT, AT + 100000, AT + 101000)['a']
        self.assertTrue(e['tested'])
        self.assertFalse(e['eligible'])
        self.assertEqual(e['usd'], 0)


class PolicyTests(unittest.TestCase):
    def test_cached_memory_is_not_double_counted_in_load_budget(self):
        h = {'memoryTotalGB': 48, 'memoryAvailableGB': 3, 'cachedFilesGB': 30}
        b = memory_budget(h, {'memoryGB': 10}, 'a', 20, 2)
        self.assertEqual(b['afterUnloadGB'], 15)
        self.assertAlmostEqual(b['requiredGB'], 31.3)
        self.assertLess(b['afterUnloadGB'], b['requiredGB'])
        self.assertIsNone(
            memory_budget({**h, 'memoryAvailableGB': None}, {'memoryGB': 10}, 'a', 20)
        )

    def test_gpt_load_headroom_matches_smaller_measured_activation_floor(self):
        h = {'memoryTotalGB': 48, 'memoryAvailableGB': 24}
        a = memory_budget(h, {'memoryGB': 14}, 'a', 13.5)
        gpt = memory_budget(h, {'memoryGB': 14}, 'gpt-oss-20b', 13.5)
        self.assertEqual(a['requiredGB'] - gpt['requiredGB'], 2)

    def test_week_rotation_covers_each_time_slot_for_six_models(self):
        models = list('abcdef')
        slots = {m: set() for m in models}
        for day in range(7):
            counts = {m: 0 for m in models}
            for slot in range(12):
                m, end = planned_model(models, AT, AT + (day * 12 + slot) * 7200, 2)
                slots[m].add(slot)
                counts[m] += 1
            self.assertTrue(all(n == 2 for n in counts.values()))
        self.assertTrue(all(len(s) == 12 for s in slots.values()))

    def test_preserves_endpoint_and_idle_arguments(self):
        p = {
            'ProgramArguments': [
                '/bin/darkbloom',
                'start',
                '--foreground',
                '--coordinator-url',
                'wss://api.darkbloom.dev/ws/provider',
                '--model',
                'a',
                '--idle-timeout',
                '0',
                '--local-endpoint',
                '--port',
                '8000',
                '--bind',
                '127.0.0.1',
            ]
        }
        model, args = launch_options(p)
        self.assertEqual(model, 'a')
        self.assertIn('--local-endpoint', args)
        self.assertIn('0', args)
        self.assertNotIn('--foreground', args)
        self.assertNotIn('--model', args)

    def test_duplicate_and_unsupported_model_sets_are_rejected(self):
        for a in [
            ['--model', 'a', '--model', 'b', '--model', 'c'],
            ['--model', 'a', '--model', 'a'],
            ['--model', 'a', 'stray'],
            ['--model'],
        ]:
            with self.assertRaises(ValueError):
                launch_options({'ProgramArguments': ['binary', *a]})

    def row(self, model, rate, ready=True):
        return {
            'id': model,
            'selected': True,
            'available': True,
            'evidence': {'score': rate, 'eligible': ready, 'tested': ready},
            'demand': {
                'pressure': 1,
                'recentPressure': 1,
                'recentLoad': 5,
                'recentSamples': 50,
                'recentSpan': 1470,
            },
        }

    def test_network_popularity_cannot_override_device_earnings(self):
        a = self.row('a', 0.2)
        b = self.row('b', 0.1)
        b['demand'].update(pressure=1000, recentPressure=1000)
        self.assertIsNone(best_candidate([a, b], 'a')[0])

    def test_missing_or_zero_recent_network_demand_blocks_a_candidate(self):
        for change in [{'recentSamples': 1}, {'recentSpan': 100}, {'recentLoad': 0}]:
            b = self.row('b', 1)
            b['demand'].update(change)
            self.assertIsNone(best_candidate([self.row('a', 0.1), b], 'a')[0])

    def test_demand_adjustment_is_bounded(self):
        a = self.row('a', 0.2)
        b = self.row('b', 0.1)
        b['demand']['recentPressure'] = 100000
        self.assertIsNone(best_candidate([a, b], 'a')[0])

    def test_improvement_and_evidence_required(self):
        for b in [self.row('b', 0.21), self.row('b', 0.4, False)]:
            self.assertIsNone(best_candidate([self.row('a', 0.2), b], 'a')[0])
        self.assertEqual(best_candidate([self.row('a', 0.2), self.row('b', 0.3)], 'a')[0], 'b')

    def test_quiet_model_can_be_replaced_once_its_idle_time_is_measured(self):
        a = self.row('a', 0)
        a['evidence']['eligible'] = False
        self.assertEqual(best_candidate([a, self.row('b', 0.1)], 'a')[0], 'b')


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.h = History(':memory:')
        self.net = Mock()
        self.now = time.time()
        self.net.snapshot.return_value = {
            'capacity': {'status': 'ok', 'updatedAt': self.now, 'data': {'models': []}}
        }
        self.net.snapshot.side_effect = lambda key=None, m=self.net.snapshot: (
            m.return_value if key is None else (m.return_value.get(key) or {})
        )
        self.o = Optimizer(self.h, self.net, self.tmp.name, threading.Event(), Mock())
        self.raw = {
            'attestation_public_key': 'public-test-key',
            'written_at': self.now,
            'started_at': self.now - 10000,
            'pid': 1,
            'advertised_models': ['a'],
            'inference_active': False,
            'stats': {'requests_served': 10, 'tokens_generated': 20},
        }
        self.live = {
            'at': self.now,
            'account': 'acct',
            'device': device_id(self.raw),
            'provider': {'online': True, 'model': 'a', 'memoryGB': 10},
            'hardware': {
                'chip': 'Apple M5 Pro',
                'memoryTotalGB': 48,
                'memoryUsedGB': 24,
                'memoryAvailableGB': 24,
                'cpuTemp': 55,
                'gpuTemp': 65,
                'thermal': 'Nominal',
            },
            'earnings': {'status': 'ok', 'updatedAt': self.now},
        }
        self.o.live = copy.deepcopy(self.live)
        self.o.raw = copy.deepcopy(self.raw)
        self.o.read_options = Mock(return_value=('a', ['--local-endpoint', '--port', '8000'], {}))
        self.o.read_state = Mock(return_value=copy.deepcopy(self.raw))
        self.o.identity_ok = True
        self.o.identity_at = self.now
        self.o.discovery_at = self.now
        self.o.on_ac_power = Mock(return_value=True)
        self.o.state.update(
            mode='week',
            models=['a', 'b'],
            startedAt=self.now - 7300,
            endsAt=self.now + 604800,
            expectedModel='a',
            lastSwitchAt=self.now - 7300,
            account='acct',
            device=self.live['device'],
            blockHours=2,
        )
        self.o.service_disabled = Mock(return_value=False)
        self.o.tick_prewarm = Mock(return_value=False)
        self.o.recovery_ready = Mock(return_value=copy.deepcopy(self.raw))
        self.o.identity_session = (self.raw['started_at'], self.raw['pid'])
        self.o.snapshot = Mock(
            return_value={
                'models': [
                    {'id': 'a', 'available': True, 'memoryGB': 10},
                    {'id': 'b', 'available': True, 'memoryGB': 12},
                ]
            }
        )
        self.o.local = [
            {'id': 'a', 'estimated_memory_gb': 10},
            {'id': 'b', 'estimated_memory_gb': 12},
        ]
        self.o.catalog = [{'id': m, 'active': True, 'min_ram_gb': 24} for m in ('a', 'b')]
        self.o.raw.update(
            current_model='a',
            warm_models=['a'],
            trust={'status': 'online', 'trust_level': 'hardware'},
        )
        self.live['hardware']['at'] = self.now
        self.o.live['hardware']['at'] = self.now
        self.raw = copy.deepcopy(self.o.raw)
        self.o.read_state.return_value = copy.deepcopy(self.raw)
        self.o.warmup = {
            'session': session_key(self.raw),
            'model': 'a',
            'status': 'ready',
            'verifiedAt': self.now - 1,
        }
        self.o.recovery_ready.return_value = copy.deepcopy(self.raw)
        self.o.local = [{**m, 'size_bytes': 1000, 'template_render_ok': True} for m in self.o.local]
        self.o.runner.return_value.stdout = json.dumps({'models': self.o.local})
        self.net.fetch.side_effect = lambda path: (
            {'models': self.o.catalog}
            if path == '/v1/models/catalog'
            else {
                'providers': [
                    {
                        'se_public_key': 'public-test-key',
                        'provider_id': 'p',
                        'models': ['a'],
                        'status': 'online',
                        'trust_level': 'hardware',
                    }
                ]
            }
        )
        process_patch = patch('optimizer.matching_process', return_value=True)
        process_patch.start()
        self.addCleanup(process_patch.stop)

    def tearDown(self):
        self.h.close()
        self.tmp.cleanup()

    def test_phone_can_start_and_pause_test_without_immediate_provider_restart(self):
        self.o.state.update(mode='observe', demandPolicy={**policy(), 'managerStrategy': 0})
        self.o.snapshot.return_value.update(controlError=None, identityVerified=True)
        payload = {
            'action': 'start',
            'mode': 'week',
            'models': ['a', 'b'],
            'blockHours': 2,
            'expectedControl': self.o.control_version(),
        }
        self.o.control_action(payload, 'phone')
        self.assertEqual(self.o.state['mode'], 'week')
        self.assertEqual(self.o.state['models'], ['a', 'b'])
        self.assertEqual(self.o.state['endsAt'] - self.o.state['startedAt'], 604800)
        self.assertNotEqual(payload['expectedControl'], self.o.control_version())
        with self.assertRaises(ValueError):
            self.o.control_action(payload, 'phone')
        self.o.control_action(
            {'action': 'pause', 'expectedControl': self.o.control_version()}, 'phone'
        )
        self.assertEqual(self.o.state['mode'], 'observe')
        self.o.runner.assert_not_called()

    def test_stale_control_cannot_overwrite_another_plan_or_manual_choice(self):
        version = self.o.control_version()
        for change in (
            {'models': ['a', 'c']},
            {'requestedModel': 'b', 'requestedKind': 'manual'},
            {'pending': {'model': 'b'}},
            {'mode': 'observe'},
        ):
            state = copy.deepcopy(self.o.state)
            self.o.state.update(change)
            with self.assertRaises(ValueError):
                self.o.control_action({'action': 'pause', 'expectedControl': version}, 'phone')
            self.o.state = state
        self.o.raw['pid'] = 2
        with self.assertRaises(ValueError):
            self.o.control_action({'action': 'pause', 'expectedControl': version}, 'phone')
        with self.assertRaises(ValueError):
            self.o.control_action({'action': 'disable'}, 'phone')
        self.o.runner.assert_not_called()

    def test_start_rechecks_provider_session_even_before_next_live_poll(self):
        self.o.state['mode'] = 'observe'
        self.o.read_state.return_value = {**self.raw, 'pid': 2}
        with self.assertRaisesRegex(ValueError, 'provider changed'):
            self.o.control_action(
                {
                    'action': 'start',
                    'mode': 'week',
                    'models': ['a', 'b'],
                    'blockHours': 2,
                    'expectedControl': self.o.control_version(),
                },
                'phone',
            )
        self.assertEqual(self.o.state['mode'], 'observe')
        self.o.runner.assert_not_called()

    def test_manual_change_pauses(self):
        self.o.read_options.return_value = ('manual', [], {})
        self.o.tick(self.now)
        self.assertEqual(self.o.state['mode'], 'observe')
        self.o.runner.assert_not_called()

    def test_schedule_dispatches_one_switch_after_minimum_run_and_idle(self):
        self.o.idle_since = self.now - 30
        self.o.switch = Mock()
        self.o.tick(self.now)
        self.o.worker.join(timeout=2)
        self.o.switch.assert_called_once_with('a', 'b', 'acct', self.live['device'])
        self.assertEqual(self.o.state['pending']['model'], 'b')
        self.o.tick(self.now + 1)
        self.o.switch.assert_called_once()

    def test_start_validates_candidates_and_saves_plan(self):
        # Seven-day tests are a legacy strategy (test_start_refuses_legacy_tests_under_the_manager).
        self.o.state.update(mode='observe', demandPolicy={**policy(), 'managerStrategy': 0})
        self.o.snapshot.return_value.update(controlError=None, identityVerified=True)
        for payload in [
            dict(mode='week', models=['a', 'bad']),
            dict(mode='week', models=['a', 'a']),
            dict(mode='week', models=['b']),
            dict(mode='bad', models=['a', 'b']),
            dict(mode='week', models=['a', 'b'], blockHours=True),
        ]:
            with self.assertRaises(ValueError):
                self.o.action({'action': 'start', **payload})
            self.assertEqual(self.o.state['mode'], 'observe')
        self.o.action({'action': 'start', 'mode': 'week', 'models': ['b', 'a'], 'blockHours': 2})
        self.assertEqual(self.o.state['models'], ['a', 'b'])
        self.assertEqual(self.o.state['endsAt'] - self.o.state['startedAt'], 604800)
        loaded = Optimizer(self.h, self.net, self.tmp.name, threading.Event(), Mock())
        self.assertEqual(loaded.state['mode'], 'week')
        self.assertEqual(loaded.state['models'], ['a', 'b'])

    def test_offline_and_stale_feeds_never_restart(self):
        self.o.live['provider']['online'] = False
        self.o.tick(self.now)
        self.assertIn('offline', self.o.detail)
        self.o.runner.assert_not_called()
        self.o.live = copy.deepcopy(self.live)
        self.net.snapshot.return_value = {'capacity': {'status': 'stale', 'updatedAt': self.now}}
        self.o.tick(self.now)
        self.assertIn('stale', self.o.detail)
        self.o.runner.assert_not_called()

    def test_minimum_run_holds_but_busy_automatic_rotation_does_not(self):
        self.o.switch = Mock()
        self.o.idle_since = None
        self.o.raw['inference_active'] = True
        self.o.state['lastSwitchAt'] = self.now - 10
        self.o.tick(self.now)
        self.assertIn('minimum', self.o.detail)
        self.o.switch.assert_not_called()
        self.o.state['lastSwitchAt'] = self.now - 7300
        self.o.tick(self.now)
        self.o.worker.join(2)
        self.o.switch.assert_called_once()
        self.assertEqual(self.o.state['pending']['kind'], 'automatic')

    def test_busy_week_switch_retains_plan_and_verifies_new_model(self):
        self.o.state['pending'] = {'model': 'b', 'kind': 'automatic', 'automaticMode': 'week'}
        self.o.read_state.return_value['inference_active'] = True
        self.o.command = Mock()
        self.o.verify_started = Mock(return_value=True)
        self.o.switch('a', 'b', 'acct', self.live['device'])
        self.o.command.assert_called_once()
        self.o.verify_started.assert_called_once()
        self.assertEqual(self.o.state['mode'], 'week')
        self.assertEqual(self.o.state['expectedModel'], 'b')

    def test_paused_automatic_rotation_cannot_dispatch_while_busy(self):
        self.o.state.update(
            mode='observe', pending={'model': 'b', 'kind': 'automatic', 'automaticMode': 'week'}
        )
        self.o.read_state.return_value['inference_active'] = True
        self.o.command = Mock()
        self.o.switch('a', 'b', 'acct', self.live['device'])
        self.o.command.assert_not_called()

    def test_week_finishes_without_unrequested_restart(self):
        self.o.state['endsAt'] = self.now - 1
        self.o.tick(self.now)
        self.assertEqual(self.o.state['mode'], 'observe')
        self.o.runner.assert_not_called()

    def test_battery_heat_and_memory_hold(self):
        self.o.on_ac_power.return_value = False
        self.o.tick(self.now)
        self.assertIn('battery', self.o.detail)
        self.o.on_ac_power.return_value = True
        self.o.live['hardware']['gpuTemp'] = 96
        self.o.tick(self.now)
        self.assertIn('hot', self.o.detail)
        self.o.live['hardware']['gpuTemp'] = 65
        self.o.live['hardware']['memoryAvailableGB'] = 1
        self.o.tick(self.now)
        self.assertIn('memory', self.o.detail)

    def test_delayed_model_load_failure_pauses_and_queues_restore(self):
        self.o.state.update(rollbackModel='b', lastSwitchAt=self.now - 60)
        self.o.raw['last_model_load_error'] = {
            'model': 'a',
            'at': self.now - 5,
            'message': 'Insufficient memory',
        }
        self.o.raw['warm_models'] = []
        self.o.tick(self.now)
        self.assertEqual(self.o.state['mode'], 'observe')
        self.assertEqual(self.o.state['requestedModel'], 'b')
        self.o.runner.assert_not_called()

    def test_model_switch_uses_cli_and_preserves_flags(self):
        self.o.state['pending'] = {'model': 'b'}
        self.o.command = Mock()
        self.o.verify_started = Mock(return_value=True)
        self.o.switch('a', 'b', 'acct', self.live['device'])
        self.o.command.assert_called_once_with('b', ['--local-endpoint', '--port', '8000'], {})
        self.assertEqual(self.o.state['expectedModel'], 'b')
        self.assertNotIn('pending', self.o.state)

    def test_failed_switch_restores_previous_model_and_pauses(self):
        self.o.command = Mock()
        self.o.verify_started = Mock(side_effect=[False, True])
        self.o.switch('a', 'b', 'acct', self.live['device'])
        self.assertEqual([c.args[0] for c in self.o.command.call_args_list], ['b', 'a'])
        self.assertEqual(self.o.state['mode'], 'observe')
        self.assertIn('Restored', self.o.detail)

    def test_manual_stop_during_switch_is_not_undone(self):
        self.o.command = Mock()
        self.o.verify_started = Mock(side_effect=ExternalChange())
        self.o.switch('a', 'b', 'acct', self.live['device'])
        self.o.command.assert_called_once()
        self.assertEqual(self.o.state['mode'], 'observe')
        self.assertIn('manual choice', self.o.detail)
        self.assertIn('launch settings changed during the switch', self.o.detail)

    def test_provider_change_names_the_check_that_fired(self):
        # Jason, Sep 27: 23 different checks raise ExternalChange; the result said which one.
        self.o.command = Mock()
        self.o.verify_started = Mock(side_effect=ExternalChange('Provider changed during warm-up.'))
        self.o.switch('a', 'b', 'acct', self.live['device'])
        self.assertTrue(self.o.detail.startswith('Provider changed during warm-up.'))
        self.assertNotIn('launch settings changed during the switch', self.o.detail)
        self.assertIn('manual choice', self.o.detail)

    def test_busy_final_recheck_sends_no_restart(self):
        self.o.read_state.return_value = {**self.raw, 'inference_active': True}
        self.o.command = Mock()
        self.o.switch('a', 'b', 'acct', self.live['device'])
        self.o.command.assert_not_called()
        self.assertEqual(self.o.state['mode'], 'observe')

    def test_start_gets_the_whole_drain_deadline_on_draining_providers(self):
        self.o.command('b', ['--local-endpoint'], {})
        self.assertEqual(self.o.runner.call_args.kwargs['timeout'], 60)
        self.o.raw['version'] = '0.9.9'
        self.o.command('b', ['--local-endpoint'], {})
        call = self.o.runner.call_args
        self.assertEqual(call.kwargs['timeout'], 660)
        self.assertEqual(call.args[0][1:], ['start', '--local-endpoint', '--model', 'b'])
        self.assertTrue(graceful_drain({'version': '0.10.0'}))
        for version in ('0.9.8', '', None, 'beta', 99):
            self.assertFalse(graceful_drain({'version': version}))

    def test_draining_provider_switches_manually_without_an_idle_wait(self):
        self.o.manual_action(self.manual_payload())
        self.o.switch = Mock()
        self.o.tick(self.now)
        self.assertIn('idle', self.o.detail)
        self.o.switch.assert_not_called()
        self.o.raw['version'] = '0.9.9'
        self.o.tick(self.now)
        self.o.worker.join(timeout=2)
        self.o.switch.assert_called_once_with('a', 'b', 'acct', self.live['device'])

    def test_draining_provider_restarts_with_work_in_flight(self):
        busy = {**self.raw, 'version': '0.9.9', 'inference_active': True}
        self.o.raw = copy.deepcopy(busy)
        self.o.read_state.return_value = copy.deepcopy(busy)
        self.o.command = Mock()
        self.o.verify_started = Mock(return_value=True)
        self.o.manual_action(self.manual_payload())
        self.o.switch('a', 'b', 'acct', self.live['device'])
        self.o.command.assert_called_once()
        self.assertEqual(self.o.state['manualResult']['status'], 'completed')
        (detail,) = self.h.db.execute(
            "SELECT detail FROM opt_events WHERE kind='switching' ORDER BY id DESC"
        ).fetchone()
        self.assertIn('finishes accepted requests first', detail)

    def drained(self, **changes):
        return {
            **self.raw,
            'version': '0.9.9',
            'warm_models': [],
            'inference_active': False,
            'lifecycle': {'outcome': 'drained', 'remaining': 0, 'coordinator_acknowledged': True},
            **changes,
        }

    def failed_start(self, **changes):
        return {
            'model': 'b',
            'stage': 'start',
            'code': 'startup-timeout',
            'recovery': 'blocked',
            'recoveryCode': 'idle-not-verified',
            'at': self.now - 300,
            **changes,
        }

    def test_provider_left_drained_by_a_failed_start_is_restarted_once(self):
        self.o.state.update(mode='observe', lastSwitchFailure=self.failed_start())
        self.o.raw = self.drained()
        self.o.read_state.return_value = self.drained()
        self.o.command = Mock()
        self.o.tick(self.now)
        self.o.tick(self.now + 119)
        self.o.command.assert_not_called()
        self.o.tick(self.now + 120)
        self.o.command.assert_called_once_with('a', ['--local-endpoint', '--port', '8000'], {})
        self.assertEqual(self.o.state['lastSwitchFailure']['recovery'], 'restored')
        self.assertEqual(self.o.state['lastSwitchFailure']['recoveryCode'], 'restored')
        self.assertEqual(self.o.state['mode'], 'observe')
        self.assertIn('Automatic switching stays paused', self.o.detail)
        (kind,) = self.h.db.execute('SELECT kind FROM opt_events ORDER BY id DESC').fetchone()
        self.assertEqual(kind, 'recovered')
        self.o.tick(self.now + 400)
        self.o.command.assert_called_once()

    def test_failed_drained_restart_is_reported_and_not_repeated(self):
        self.o.state.update(mode='observe', lastSwitchFailure=self.failed_start())
        self.o.raw = self.drained()
        self.o.read_state.return_value = self.drained()
        self.o.command = Mock(side_effect=RuntimeError('start failed'))
        for dt in (0, 120, 240, 480):
            self.o.tick(self.now + dt)
        self.o.command.assert_called_once()
        self.assertEqual(self.o.state['lastSwitchFailure']['recovery'], 'blocked')
        self.assertIn('darkbloom restart', self.o.detail)
        (kind,) = self.h.db.execute('SELECT kind FROM opt_events ORDER BY id DESC').fetchone()
        self.assertEqual(kind, 'failed')

    def test_drained_provider_needs_a_recent_unrestored_start_failure(self):
        self.o.state['mode'] = 'observe'
        self.o.raw = self.drained()
        self.o.read_state.return_value = self.drained()
        self.o.command = Mock()
        for failure in (
            None,
            self.failed_start(recovery='restored'),
            self.failed_start(at=self.now - 7 * 3600),
            self.failed_start(stage='verify'),
        ):
            self.o.state['lastSwitchFailure'] = failure
            self.o.drained_since = None
            for dt in (0, 200):
                self.o.tick(self.now + dt)
        self.o.command.assert_not_called()

    def test_draining_busy_or_serving_provider_is_not_restarted(self):
        self.o.state.update(mode='observe', lastSwitchFailure=self.failed_start())
        self.o.command = Mock()
        for raw in (
            self.drained(inference_active=True),
            self.drained(lifecycle={'outcome': 'draining', 'remaining': 1}),
            self.drained(warm_models=['a']),
            {**self.raw, 'version': '0.9.9'},
        ):
            self.o.raw = copy.deepcopy(raw)
            self.o.read_state.return_value = copy.deepcopy(raw)
            self.o.drained_since = None
            for dt in (0, 200):
                self.o.tick(self.now + dt)
        # Readings can change between the tick and the restart: recheck them.
        self.o.raw = self.drained()
        self.o.read_state.return_value = self.drained(inference_active=True)
        self.o.drained_since = None
        for dt in (0, 200):
            self.o.tick(self.now + dt)
        self.o.command.assert_not_called()

    def test_observation_breaks_on_reset_gap_and_multiple_models(self):
        self.raw.update(warm_models=['a'], trust={'status': 'online'})
        self.o.warmup = {
            'session': session_key(self.raw),
            'model': 'a',
            'status': 'ready',
            'verifiedAt': self.now - 1,
        }
        h = self.live['hardware']
        snap = {
            'at': self.now,
            'provider': self.live['provider'],
            'hardware': h,
            'earnings': self.live['earnings'],
        }
        self.o.store.sample = Mock()
        self.o.observe('acct', self.raw, snap)
        raw = {
            **self.raw,
            'written_at': self.now + 3,
            'stats': {'requests_served': 11, 'tokens_generated': 50},
        }
        self.o.observe('acct', raw, {**snap, 'at': self.now + 3})
        self.o.store.sample.assert_called_once()
        self.assertEqual(self.o.store.sample.call_args.args[5:7], (1, 30))
        raw = {**raw, 'written_at': self.now + 6, 'started_at': self.now + 5}
        self.o.observe('acct', raw, {**snap, 'at': self.now + 6})
        self.assertEqual(self.o.store.sample.call_count, 1)
        raw = {**raw, 'advertised_models': ['a', 'b'], 'written_at': self.now + 9}
        self.o.observe('acct', raw, {**snap, 'at': self.now + 9})
        self.assertEqual(self.o.store.sample.call_count, 1)

    def test_interrupted_switch_does_not_resume_automatically(self):
        # Legacy strategy: under the Manager an interrupted switch is recovered (test_manager).
        self.o.state['demandPolicy'] = {**policy(), 'managerStrategy': 0}
        self.o.state['pending'] = {'model': 'b'}
        self.o.save()
        other = Optimizer(self.h, self.net, self.tmp.name, threading.Event(), Mock())
        self.assertEqual(other.state['mode'], 'observe')
        other.runner.assert_not_called()

    def test_account_change_pauses(self):
        self.o.live['account'] = 'different'
        self.o.tick(self.now)
        self.assertEqual(self.o.state['mode'], 'observe')

    def manual_payload(self, model='b'):
        self.o.local = [{'id': m, 'estimated_memory_gb': 12} for m in ('a', 'b')]
        self.o.catalog = [{'id': m, 'active': True, 'min_ram_gb': 24} for m in ('a', 'b')]
        return {
            'action': 'switch',
            'model': model,
            'expectedSession': session_key(self.raw),
            'requestId': str(uuid.uuid4()),
        }

    def test_manual_selection_pauses_automation_and_queues_without_immediate_restart(self):
        payload = self.manual_payload()
        result = self.o.manual_action(payload, 'phone')
        self.assertEqual(self.o.state['mode'], 'observe')
        self.assertEqual(result['queuedModel'], 'b')
        self.assertTrue(result['canCancel'])
        self.assertEqual(result['requestId'], payload['requestId'])
        self.assertEqual(
            [
                c.args[0]
                for c in self.o.runner.call_args_list
                if c.args[0] != ['/usr/bin/sudo', '-n', '-ll', '/usr/sbin/purge']
            ],
            [],
        )
        self.assertIn(
            'phone',
            self.h.db.execute(
                "SELECT detail FROM opt_events WHERE kind='manual-requested'"
            ).fetchone()[0],
        )

    def test_retried_manual_request_is_idempotent_and_different_payload_rejected(self):
        p = self.manual_payload()
        self.o.manual_action(p)
        self.o.manual_action(p)
        self.assertEqual(
            self.h.db.execute(
                "SELECT COUNT(*) FROM opt_events WHERE kind='manual-requested'"
            ).fetchone()[0],
            1,
        )
        with self.assertRaises(ValueError):
            self.o.manual_action({**p, 'model': 'a'})
        self.o.manual_action({'action': 'cancel', 'requestId': p['requestId']})
        self.o.manual_action(p)
        self.assertIsNone(self.o.state['requestedModel'])
        self.assertEqual(
            [
                c.args[0]
                for c in self.o.runner.call_args_list
                if c.args[0] != ['/usr/bin/sudo', '-n', '-ll', '/usr/sbin/purge']
            ],
            [],
        )

    def test_manual_current_model_is_noop_and_preserves_automation(self):
        p = self.manual_payload('a')
        r = self.o.manual_action(p)
        self.assertEqual(r['lastResult']['status'], 'unchanged')
        self.assertEqual(self.o.state['mode'], 'week')
        self.assertFalse(r['switching'])
        self.assertEqual(
            [
                c.args[0]
                for c in self.o.runner.call_args_list
                if c.args[0] != ['/usr/bin/sudo', '-n', '-ll', '/usr/sbin/purge']
            ],
            [],
        )

    def test_manual_switch_rejects_stale_session_invalid_model_and_stopped_service(self):
        p = self.manual_payload()
        for changed in (
            {'expectedSession': 'old'},
            {'model': '../../bad --model c'},
            {'requestId': 'invalid'},
        ):
            with self.assertRaises(ValueError):
                self.o.manual_action({**p, **changed})
        self.o.service_disabled.return_value = True
        with self.assertRaises(ValueError):
            self.o.manual_action(p)
        self.assertEqual(self.o.state['mode'], 'week')
        self.assertEqual(
            [
                c.args[0]
                for c in self.o.runner.call_args_list
                if c.args[0] != ['/usr/bin/sudo', '-n', '-ll', '/usr/sbin/purge']
            ],
            [],
        )

    def test_manual_switch_does_not_depend_on_earnings_or_demand_ranking(self):
        p = self.manual_payload()
        self.o.manual_action(p)
        self.o.live['earnings']['status'] = 'stale'
        self.net.snapshot.return_value = {'capacity': {'status': 'stale'}}
        self.o.idle_since = self.now - 30
        self.o.state['lastSwitchAt'] = self.now - 1
        self.o.switch = Mock()
        self.o.tick(self.now)
        self.o.worker.join(timeout=2)
        self.o.switch.assert_called_once_with('a', 'b', 'acct', self.live['device'])

    def test_manual_queue_preserves_idle_heat_and_memory_gates(self):
        # Battery power and the 95 °C line don't hold an explicit pick; thermal state does.
        self.o.manual_action(self.manual_payload())
        self.o.switch = Mock()
        self.o.tick(self.now)
        self.assertIn('idle', self.o.detail)
        self.o.idle_since = self.now - 30
        self.o.live['hardware']['thermal'] = 'Serious'
        self.o.tick(self.now)
        self.assertIn('hot', self.o.detail)
        self.o.live['hardware']['thermal'] = 'Nominal'
        self.o.live['hardware']['memoryAvailableGB'] = 1
        self.o.tick(self.now)
        self.assertIn('memory', self.o.detail)
        self.o.switch.assert_not_called()
        self.assertEqual(self.o.state['requestedModel'], 'b')

    def test_manual_cancel_is_scoped_to_request_and_cannot_interrupt_restart(self):
        p = self.manual_payload()
        self.o.manual_action(p)
        with self.assertRaises(ValueError):
            self.o.manual_action({'action': 'cancel', 'requestId': str(uuid.uuid4())})
        self.o.state['pending'] = {'model': 'b'}
        with self.assertRaises(ValueError):
            self.o.manual_action({'action': 'cancel', 'requestId': p['requestId']})
        self.o.state.pop('pending')
        r = self.o.manual_action({'action': 'cancel', 'requestId': p['requestId']})
        self.assertFalse(r['canCancel'])
        self.assertEqual(r['lastResult']['status'], 'cancelled')
        self.assertEqual(
            [
                c.args[0]
                for c in self.o.runner.call_args_list
                if c.args[0] != ['/usr/bin/sudo', '-n', '-ll', '/usr/sbin/purge']
            ],
            [],
        )

    def test_conflicting_actions_cannot_override_queued_manual_selection(self):
        p = self.manual_payload()
        self.o.manual_action(p)
        with self.assertRaises(ValueError):
            self.o.manual_action({**p, 'requestId': str(uuid.uuid4()), 'model': 'a'})
        with self.assertRaises(ValueError):
            self.o.action({'action': 'start', 'mode': 'week', 'models': ['a', 'b']})
        with self.assertRaises(ValueError):
            self.o.action({'action': 'restore'})
        self.assertEqual(self.o.state['requestedModel'], 'b')

    def test_manual_completion_and_synchronous_failure_have_reported_outcomes(self):
        p = self.manual_payload()
        self.o.manual_action(p)
        self.o.command = Mock()
        self.o.verify_started = Mock(return_value=True)
        self.o.switch('a', 'b', 'acct', self.live['device'])
        self.assertEqual(self.o.state['manualResult']['status'], 'completed')
        self.assertEqual(self.o.state['manualResult']['id'], p['requestId'])
        self.assertIsNone(self.o.state['requestedKind'])
        p = self.manual_payload()
        self.o.manual_action(p)
        self.o.verify_started = Mock(side_effect=[False, True])
        self.o.switch('a', 'b', 'acct', self.live['device'])
        self.assertEqual(self.o.state['manualResult']['status'], 'recovered')
        self.assertEqual(self.o.state['mode'], 'observe')

    def test_delayed_manual_load_failure_queues_recovery_even_in_observe_mode(self):
        request_id = str(uuid.uuid4())
        self.o.state.update(
            mode='observe',
            rollbackModel='b',
            lastSwitchAt=self.now - 60,
            manualResult={'id': request_id, 'status': 'completed', 'model': 'a'},
        )
        self.o.raw['last_model_load_error'] = {'model': 'a', 'at': self.now - 5}
        self.o.raw['warm_models'] = []
        self.o.tick(self.now)
        self.assertEqual(self.o.state['requestedModel'], 'b')
        self.assertEqual(self.o.state['requestedKind'], 'manual-recovery')
        self.assertEqual(self.o.state['manualResult']['status'], 'recovering')
        self.assertEqual(self.o.state['requestId'], request_id)
        self.o.runner.assert_not_called()

    def test_restart_cancels_queued_manual_request_and_preserves_deduplication(self):
        p = self.manual_payload()
        self.o.manual_action(p)
        other = Optimizer(self.h, self.net, self.tmp.name, threading.Event(), Mock())
        self.assertIsNone(other.state['requestedModel'])
        self.assertEqual(other.state['manualResult']['status'], 'interrupted')
        self.assertEqual(other.state['manualRequests'][0]['id'], p['requestId'])
        other.runner.assert_not_called()

    def test_busy_race_keeps_manual_selection_and_cancel_available(self):
        p = self.manual_payload()
        self.o.manual_action(p)
        queued_at = self.o.state['requestedAt']
        self.o.state['pending'] = {'model': 'b'}
        self.o.command = Mock()
        self.o.read_state.return_value = {**self.raw, 'inference_active': True}
        self.o.switch('a', 'b', 'acct', self.live['device'])
        self.o.command.assert_not_called()
        self.assertEqual(self.o.state['requestedModel'], 'b')
        self.assertEqual(self.o.state['requestId'], p['requestId'])
        self.assertEqual(self.o.state['requestedAt'], queued_at)
        self.assertEqual(self.o.state['manualResult']['status'], 'queued')
        self.assertNotIn('pending', self.o.state)
        self.assertIsNone(self.o.idle_since)
        self.assertTrue(self.o.manual_snapshot()['canCancel'])
        self.o.manual_action({'action': 'cancel', 'requestId': p['requestId']})
        self.assertIsNone(self.o.state['requestedModel'])

    def test_counter_race_retries_then_completes_with_same_request(self):
        p = self.manual_payload()
        self.o.manual_action(p)
        self.o.command = Mock()
        self.o.verify_started = Mock(return_value=True)
        changed = {**self.raw, 'stats': {'requests_served': 11, 'tokens_generated': 30}}
        self.o.read_state.return_value = changed
        self.o.switch('a', 'b', 'acct', self.live['device'])
        self.o.command.assert_not_called()
        self.assertEqual(self.o.state['manualResult']['status'], 'queued')
        self.o.raw = copy.deepcopy(changed)
        self.o.switch('a', 'b', 'acct', self.live['device'])
        self.o.command.assert_called_once()
        self.assertEqual(self.o.state['manualResult']['status'], 'completed')
        self.assertEqual(self.o.state['manualResult']['id'], p['requestId'])

    def test_queued_session_or_launch_change_is_terminal(self):
        for change in ('session', 'options', 'environment'):
            with self.subTest(change=change):
                self.o.raw = copy.deepcopy(self.raw)
                self.o.read_options.return_value = ('a', ['--local-endpoint', '--port', '8000'], {})
                self.o.state.update(
                    expectedModel='a', requestedModel=None, requestedKind=None, requestId=None
                )
                self.o.manual_action(self.manual_payload())
                if change == 'session':
                    self.o.raw['pid'] = 2
                elif change == 'options':
                    self.o.read_options.return_value = (
                        'a',
                        ['--local-endpoint', '--port', '8001'],
                        {},
                    )
                else:
                    self.o.read_options.return_value = (
                        'a',
                        ['--local-endpoint', '--port', '8000'],
                        {'DARKBLOOM_TEST': 'changed'},
                    )
                self.o.tick(self.now)
                self.assertIsNone(self.o.state['requestedModel'])
                self.assertEqual(self.o.state['manualResult']['status'], 'cancelled')
                self.assertIn('session or launch settings changed', self.o.detail)
        self.assertEqual(
            [
                c.args[0]
                for c in self.o.runner.call_args_list
                if c.args[0] != ['/usr/bin/sudo', '-n', '-ll', '/usr/sbin/purge']
            ],
            [],
        )

    def test_final_launch_change_is_not_treated_as_busy_retry(self):
        self.o.manual_action(self.manual_payload())
        self.o.command = Mock()
        self.o.state['pending'] = {
            'model': 'b',
            'session': session_key(self.raw),
            'launchSignature': launch_signature(['--port', '8001'], {}),
        }
        self.o.switch('a', 'b', 'acct', self.live['device'])
        self.o.command.assert_not_called()
        self.assertIsNone(self.o.state['requestedModel'])
        self.assertEqual(self.o.state['manualResult']['status'], 'failed')

    def test_counter_reset_or_account_change_is_not_a_work_only_retry(self):
        for change in ('reset', 'account'):
            with self.subTest(change=change):
                self.o.read_state.return_value = copy.deepcopy(self.raw)
                self.o.live = copy.deepcopy(self.live)
                self.o.manual_action(self.manual_payload())
                self.o.command = Mock()
                if change == 'reset':
                    self.o.read_state.return_value['stats'] = {
                        'requests_served': 0,
                        'tokens_generated': 0,
                    }
                else:
                    self.o.live['account'] = 'another-account'
                self.o.switch('a', 'b', 'acct', self.live['device'])
                self.o.command.assert_not_called()
                self.assertIsNone(self.o.state['requestedModel'])
                self.assertEqual(self.o.state['manualResult']['status'], 'failed')

    def test_diagnostic_stat_changes_do_not_falsely_block_idle_switch(self):
        self.o.manual_action(self.manual_payload())
        self.o.command = Mock()
        self.o.verify_started = Mock(return_value=True)
        self.o.read_state.return_value = {
            **self.raw,
            'stats': {**self.raw['stats'], 'usage_gaps': 42},
        }
        self.o.switch('a', 'b', 'acct', self.live['device'])
        self.o.command.assert_called_once()
        self.assertEqual(self.o.state['manualResult']['status'], 'completed')
        for bad in (
            {},
            {'requests_served': False, 'tokens_generated': 20},
            {'requests_served': -1, 'tokens_generated': 20},
            {'requests_served': 10, 'tokens_generated': float('nan')},
            {'requests_served': 10, 'tokens_generated': 1.5},
        ):
            self.assertIsNone(activity_counters({'stats': bad}))
            self.assertFalse(same_activity({'stats': bad}, {'stats': bad}))

    def test_fast_pending_tick_does_not_query_history(self):
        self.assertEqual(self.o.control_interval(), 15)
        self.o.manual_action(self.manual_payload())
        self.assertEqual(self.o.control_interval(), 1)
        self.o.idle_since = self.now - 12
        self.o.switch = Mock()
        self.o.snapshot.reset_mock()
        self.o.tick(self.now)
        self.o.worker.join(timeout=2)
        self.o.switch.assert_called_once()
        self.o.snapshot.assert_not_called()
        self.assertEqual(self.o.control_interval(), 15)

    def test_slow_network_refresh_cannot_hold_the_control_tick(self):
        refresh_entered = threading.Event()
        release_refresh = threading.Event()
        tick_ran = threading.Event()

        def slow_refresh(_):
            refresh_entered.set()
            release_refresh.wait(2)

        self.o.refresh = slow_refresh
        self.o.tick = lambda _: tick_ran.set()
        try:
            self.o.start()
            self.assertTrue(refresh_entered.wait(1))
            self.assertTrue(tick_ran.wait(1))
            self.assertFalse(release_refresh.is_set())
            workers = (self.o.refresh_worker, self.o.control_worker)
            self.o.start()
            self.assertEqual(workers, (self.o.refresh_worker, self.o.control_worker))
        finally:
            self.o.stop.set()
            release_refresh.set()
            self.o.refresh_worker.join(timeout=2)
            self.o.control_worker.join(timeout=2)
            self.assertFalse(self.o.refresh_worker.is_alive())
            self.assertFalse(self.o.control_worker.is_alive())

    def test_replaced_same_target_request_cannot_dispatch_the_old_request(self):
        self.o.manual_action(self.manual_payload())
        self.o.idle_since = self.now - 30
        self.o.switch = Mock()
        budget = self.o.selection_budget

        def replaced_request(*args):
            self.o.state['requestId'] = str(uuid.uuid4())
            return budget(*args)

        self.o.selection_budget = replaced_request
        self.o.tick(self.now)
        self.o.switch.assert_not_called()
        self.assertNotIn('pending', self.o.state)

    def test_queue_progress_and_cache_event_are_scoped_and_stale_safe(self):
        self.o.manual_action(self.manual_payload())
        self.o.state['requestedAt'] = self.now - 125
        self.o.idle_since = self.now - 5
        self.o.state['cacheRecovery'] = {
            'session': 'earlier-private-session',
            'at': self.now - 3600,
            'model': 'b',
            'status': 'cleared',
            'detail': 'Cache cleared.',
        }
        with patch('optimizer.time.time', return_value=self.now):
            r = self.o.manual_snapshot()
        self.assertEqual(r['queue']['ageSeconds'], 125)
        self.assertEqual(r['queue']['idleSeconds'], 5)
        self.assertEqual(r['queue']['activity'], 'idle')
        self.assertFalse(r['cacheCleanup']['lastAttempt']['currentSession'])
        self.assertNotIn('session', r['cacheCleanup']['lastAttempt'])
        self.o.state['cacheRecovery']['session'] = session_key(self.raw)
        self.o.raw['written_at'] = self.now - 20
        with patch('optimizer.time.time', return_value=self.now):
            r = self.o.manual_snapshot()
        self.assertEqual(r['queue']['idleSeconds'], 0)
        self.assertEqual(r['queue']['activity'], 'unknown')
        self.assertTrue(r['cacheCleanup']['lastAttempt']['currentSession'])


if __name__ == '__main__':
    unittest.main()
