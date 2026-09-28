"""Demand automation must obey the real controller's final switch boundaries."""

import copy
import json
import threading
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
from optimizer import Optimizer, DemandDeferred, session_key
import test_optimizer
from test_demand_optimizer import decision, candidate
from demand_optimizer import policy


class DemandControllerTests(unittest.TestCase):
    setUp = test_optimizer.ControllerTests.setUp
    tearDown = test_optimizer.ControllerTests.tearDown

    def setup_demand(self):
        # These cover legacy demand following; test_manager.py covers the manager strategy.
        self.o.state.update(
            mode='demand',
            demandPolicy=policy(
                {'minRunMinutes': 30, 'confirmationMinutes': 5, 'managerStrategy': 0}
            ),
        )
        self.o.raw.update(warm_models=['a'], trust={'status': 'online', 'trust_level': 'hardware'})
        self.o.warmup = {
            'session': session_key(self.o.raw),
            'model': 'a',
            'status': 'ready',
            'verifiedAt': self.now - 1,
        }
        self.o.read_state.return_value = copy.deepcopy(self.o.raw)
        self.o.tracking = Mock(return_value={'counting': True})
        self.o.idle_since = self.now - 30
        self.o.verify_local_target = Mock()
        self.value = decision()
        self.value.update(at=self.now, sourceAt=self.now - 30, policy=self.o.state['demandPolicy'])
        self.o.demand_decision = Mock(
            side_effect=lambda now, *args, **kwargs: {
                **copy.deepcopy(self.value),
                'at': now,
                'sourceAt': self.source,
            }
        )
        self.source = self.now - 30

    def advance(self, offset, source=True):
        now = self.now + offset
        self.o.live['at'] = now
        self.o.live['earnings']['updatedAt'] = now
        self.o.identity_at = now
        self.o.discovery_at = now
        self.o.raw['written_at'] = now
        self.o.read_state.return_value = copy.deepcopy(self.o.raw)
        self.net.snapshot.return_value['capacity']['updatedAt'] = now
        if source:
            self.source = now - 30
        with patch('optimizer.time.time', return_value=now):
            self.o.tick(now)
        if self.o.worker:
            self.o.worker.join(2)

    def test_distinct_confirmation_dispatches_one_guarded_automatic_switch(self):
        self.setup_demand()
        self.o.switch = Mock()
        for offset in range(0, 300, 60):
            self.advance(offset)
        self.o.switch.assert_not_called()
        self.advance(300)
        self.o.switch.assert_called_once_with('a', 'b', 'acct', self.live['device'])
        self.assertEqual(self.o.state['pending']['kind'], 'demand')
        self.assertFalse(self.o.state['pending']['afterIdleTimeout'])
        self.advance(315)
        self.o.switch.assert_called_once()

    def test_duplicate_source_and_collection_gaps_do_not_count_as_confirmation(self):
        self.setup_demand()
        self.o.switch = Mock()
        for offset in range(0, 600, 15):
            self.advance(offset, False)
        self.o.switch.assert_not_called()
        self.assertLess((self.o.state.get('demandProposal') or {}).get('seconds', 0), 300)
        self.o.state.pop('demandProposal', None)
        for offset in (600, 660, 960, 1020, 1080):
            self.advance(offset)
        self.o.switch.assert_not_called()

    def test_unready_stale_or_reversed_opportunity_clears_proposal(self):
        self.setup_demand()
        self.o.switch = Mock()
        self.advance(0)
        self.o.tracking.return_value = {'counting': False}
        self.advance(60)
        self.assertNotIn('demandProposal', self.o.state)
        self.o.tracking.return_value = {'counting': True}
        self.advance(120)
        self.o.live['earnings']['status'] = 'stale'
        self.advance(180)
        self.assertEqual(self.o.state['demandProposal']['status'], 'paused')
        self.o.switch.assert_not_called()
        self.o.live['earnings']['status'] = 'ok'
        self.value['target'] = None
        self.advance(240)
        self.assertNotIn('demandProposal', self.o.state)

    def test_minimum_run_finishes_then_busy_work_does_not_hold(self):
        self.setup_demand()
        self.o.switch = Mock()
        self.o.state['lastSwitchAt'] = self.now
        for offset in range(0, 361, 60):
            self.advance(offset)
        self.o.switch.assert_not_called()
        self.assertIn('minimum', self.o.detail)
        self.o.state['lastSwitchAt'] = self.now - 4000
        self.o.idle_since = None
        for offset in range(420, 1201, 60):
            self.advance(offset)
        self.o.switch.assert_called_once()
        self.assertEqual(self.o.state['mode'], 'demand')

    def test_busy_final_recheck_dispatches_qualified_demand_switch(self):
        self.setup_demand()
        self.o.state['pending'] = {'model': 'b', 'kind': 'demand'}
        self.o.read_state.return_value['inference_active'] = True
        self.o.command = Mock()
        self.o.verify_started = Mock(return_value=True)
        self.o.switch('a', 'b', 'acct', self.live['device'])
        self.o.command.assert_called_once()
        self.assertEqual(self.o.state['mode'], 'demand')
        self.assertNotIn('pending', self.o.state)
        self.assertTrue(
            self.o.demand_auto.runs('acct', self.live['device'], self.now + 60)[0]['decision'][
                'activeAtDispatch'
            ]
        )

    def test_changed_economic_opportunity_is_rechecked_before_command(self):
        self.setup_demand()
        self.o.state['pending'] = {'model': 'b', 'kind': 'demand'}
        self.value['target'] = None
        self.o.command = Mock()
        self.o.switch('a', 'b', 'acct', self.live['device'])
        self.o.command.assert_not_called()
        self.assertEqual(self.o.state['mode'], 'demand')
        self.assertIn('opportunity changed', self.o.detail)

    def test_live_paid_snapshot_reaches_the_real_demand_evaluator(self):
        self.setup_demand()
        self.o.live['pulse'] = {
            'sessionId': 65,
            'status': 'live',
            'windows': {'300': {'ratePerHour': 0.113}},
        }
        self.o.demand_auto.evaluate = Mock(return_value=copy.deepcopy(self.value))
        Optimizer.demand_decision(self.o, self.now)
        self.assertEqual(
            self.o.demand_auto.evaluate.call_args.kwargs['live']['pulse'], self.o.live['pulse']
        )

    def test_paid_alternative_reports_global_control_blocker(self):
        self.setup_demand()
        self.value['paidAlternative'] = {
            'model': 'b',
            'eligible': True,
            'reason': 'Awaiting confirmation',
            'at': self.now,
        }
        self.o.demand_auto.evaluate = Mock(return_value=copy.deepcopy(self.value))
        self.o.read_options = Mock(side_effect=ValueError('unreadable'))
        result = Optimizer.demand_decision(self.o, self.now)
        self.assertIsNone(result['target'])
        self.assertFalse(result['paidAlternative']['eligible'])
        self.assertEqual(result['paidAlternative']['reason'], result['controlError'])

    def test_memory_reserve_is_budgeted_and_unknown_memory_settings_stop_moves(self):
        self.setup_demand()
        p = self.o.home / '.config/darkbloom/provider.toml'
        p.parent.mkdir(parents=True, exist_ok=True)
        self.o.demand_auto.evaluate = Mock(return_value=copy.deepcopy(self.value))
        for reserve in (4, 12):
            p.write_text('[provider]\nmemory_reserve_gb = %d' % reserve)
            self.o.demand_auto.evaluate.return_value = copy.deepcopy(self.value)
            result = Optimizer.demand_decision(self.o, self.now)
            self.assertIsNone(result['controlError'])
            self.assertEqual(result['target'], self.value['target'])
        # Darkbloom's load reserve: memory_reserve_gb, at least 10% of this 48 GB Mac.
        self.assertEqual(self.o.selection_budget('b', self.o.live, self.o.raw)['reserveGB'], 12)
        p.write_text('[provider]\nmemory_reserve_gb = 4\nkv_reserve_gb = 1')
        self.o.demand_auto.evaluate.return_value = copy.deepcopy(self.value)
        result = Optimizer.demand_decision(self.o, self.now)
        self.assertIsNone(result['target'])
        self.assertIn('kv_reserve_gb', result['controlError'])
        self.assertEqual(result['reason'], result['controlError'])

    def test_dispatch_preserves_trigger_and_paid_evidence_for_audit(self):
        self.setup_demand()
        self.o.state['pending'] = {'model': 'b', 'kind': 'demand'}
        self.value.update(
            reason='Supported paid improvement',
            explorationTrigger=None,
            earningsTarget={
                'livePaid': {'fresh': True, 'rate': 0.113, 'seconds': 300, 'asOf': self.now - 5}
            },
        )
        self.o.command = Mock()
        self.o.verify_started = Mock(return_value=True)
        self.o.switch('a', 'b', 'acct', self.live['device'])
        saved = self.o.demand_auto.runs('acct', self.live['device'], self.now + 60)[0]['decision']
        self.assertEqual(saved['reason'], self.value['reason'])
        self.assertEqual(saved['earningsTarget'], self.value['earningsTarget'])

    def test_work_arriving_during_slow_preflight_does_not_cancel_command(self):
        self.setup_demand()
        self.o.state['pending'] = {'model': 'b', 'kind': 'demand'}
        self.o.read_state.side_effect = [
            copy.deepcopy(self.o.raw),
            {**self.o.raw, 'stats': {'requests_served': 11, 'tokens_generated': 30}},
        ]
        self.o.command = Mock()
        self.o.verify_started = Mock(return_value=True)
        self.o.switch('a', 'b', 'acct', self.live['device'])
        self.o.command.assert_called_once()
        self.assertEqual(self.o.state['mode'], 'demand')
        self.assertTrue(
            self.o.demand_auto.runs('acct', self.live['device'], self.now + 60)[0]['decision'][
                'workAdvancedDuringPreflight'
            ]
        )

    def test_success_records_real_attempt_and_verifies_warmup(self):
        self.setup_demand()
        self.o.state['pending'] = {'model': 'b', 'kind': 'demand'}
        self.o.command = Mock()
        self.o.verify_started = Mock(return_value=True)
        self.o.switch('a', 'b', 'acct', self.live['device'])
        self.o.verify_local_target.assert_called_once()
        self.o.command.assert_called_once_with('b', ['--local-endpoint', '--port', '8000'], {})
        self.o.verify_started.assert_called_once()
        self.assertEqual(self.o.state['mode'], 'demand')
        self.assertEqual(self.o.state['expectedModel'], 'b')
        rows = self.o.demand_auto.runs('acct', self.live['device'], self.now + 60)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['result'], 'switched')

    def test_failed_load_uses_guarded_recovery_then_pauses(self):
        self.setup_demand()
        self.o.state['pending'] = {'model': 'b', 'kind': 'demand'}
        self.o.command = Mock()
        self.o.verify_started = Mock(side_effect=[False, True])
        self.o.switch('a', 'b', 'acct', self.live['device'])
        self.assertEqual([c.args[0] for c in self.o.command.call_args_list], ['b', 'a'])
        self.assertEqual(self.o.state['mode'], 'observe')
        self.assertEqual(
            self.o.demand_auto.runs('acct', self.live['device'], self.now + 60)[0]['result'],
            'recovered',
        )

    def test_serving_preflight_time_is_not_counted_as_switch_downtime(self):
        self.setup_demand()
        self.o.state['pending'] = {'model': 'b', 'kind': 'demand'}
        clock = [self.now]

        def evaluate(now, *args, **kwargs):
            clock[0] += 45
            self.o.live['at'] = clock[0]
            self.o.live['earnings']['updatedAt'] = clock[0]
            self.o.read_state.return_value['written_at'] = clock[0]
            return {**copy.deepcopy(self.value), 'at': clock[0]}

        def command(*args):
            clock[0] += 4

        def verified(*args):
            clock[0] += 20
            return True

        self.o.demand_decision = Mock(side_effect=evaluate)
        self.o.command = Mock(side_effect=command)
        self.o.verify_started = Mock(side_effect=verified)
        with patch('optimizer.time.time', side_effect=lambda: clock[0]):
            self.o.switch('a', 'b', 'acct', self.live['device'])
        self.o.command.assert_called_once()
        run = self.o.demand_auto.runs('acct', self.live['device'], clock[0])[0]
        self.assertEqual(run['downtime'], 24)

    def test_policy_upgrade_preserves_active_plan_and_custom_values(self):
        from demand_optimizer import PREVIOUS_DEFAULTS

        self.setup_demand()
        self.o.state.update(demandPolicy={**policy(), **PREVIOUS_DEFAULTS, 'maxSwitchesPerDay': 6})
        self.o.state.pop('demandPolicyRevision', None)
        self.o.save()
        saved = copy.deepcopy(self.o.state)
        reopened = Optimizer(self.h, self.net, self.tmp.name, threading.Event(), Mock())
        for key in (
            'mode',
            'models',
            'startedAt',
            'endsAt',
            'lastSwitchAt',
            'originalModel',
            'account',
            'device',
        ):
            self.assertEqual(reopened.state.get(key), saved.get(key))
        self.assertEqual(reopened.state['demandPolicy']['trialCooldownMinutes'], 30)
        self.assertEqual(reopened.state['demandPolicy']['maxSwitchesPerDay'], 6)
        reopened.runner.assert_not_called()

    def test_user_pause_before_final_command_wins(self):
        self.setup_demand()
        self.o.state.update(mode='observe', pending={'model': 'b', 'kind': 'demand'})
        self.o.command = Mock()
        self.o.switch('a', 'b', 'acct', self.live['device'])
        self.o.command.assert_not_called()
        self.assertEqual(self.o.state['mode'], 'observe')

    def test_extra_margin_is_rechecked_after_slow_preflight(self):
        self.setup_demand()
        self.o.state['pending'] = {'model': 'b', 'kind': 'demand'}
        self.o.selection_budget = Mock(
            side_effect=[
                {'afterUnloadGB': 40, 'requiredGB': 30},
                {'afterUnloadGB': 30.5, 'requiredGB': 30},
            ]
        )
        self.o.command = Mock()
        self.o.switch('a', 'b', 'acct', self.live['device'])
        self.o.command.assert_not_called()
        self.assertEqual(self.o.state['mode'], 'demand')

    def test_phone_start_validates_policy_and_stale_controls_and_preserves_history(self):
        self.o.state['mode'] = 'observe'
        self.o.snapshot.return_value.update(controlError=None, identityVerified=True)
        old = self.o.control_version()
        with self.assertRaises(ValueError):
            self.o.control_action(
                {
                    'action': 'start',
                    'mode': 'demand',
                    'models': ['a', 'b'],
                    'demandPolicy': {'memoryHeadroomGB': 0},
                    'expectedControl': old,
                },
                'phone',
            )
        self.o.control_action(
            {
                'action': 'start',
                'mode': 'demand',
                'models': ['a', 'b'],
                'demandPolicy': {'minRunMinutes': 30},
                'expectedControl': old,
            },
            'phone',
        )
        self.assertEqual(self.o.state['mode'], 'demand')
        self.assertEqual(self.o.state['demandPolicy']['minRunMinutes'], 30)
        self.o.runner.assert_not_called()
        with self.assertRaises(ValueError):
            self.o.control_action({'action': 'pause', 'expectedControl': old}, 'phone')
        self.o.control_action(
            {'action': 'pause', 'expectedControl': self.o.control_version()}, 'phone'
        )
        self.assertEqual(self.o.state['mode'], 'observe')

    def test_restart_never_reuses_old_confirmation(self):
        self.setup_demand()
        self.advance(0)
        self.o.save()
        reopened = Optimizer(self.h, self.net, self.tmp.name, threading.Event(), Mock())
        self.assertEqual(reopened.state['mode'], 'demand')
        self.assertNotIn('demandProposal', reopened.state)

    def test_interrupted_attempt_pauses_even_if_pending_state_was_not_saved(self):
        self.setup_demand()
        self.o.save()
        d = decision()
        d['at'] = self.now
        self.o.demand_auto.begin('acct', self.live['device'], d, self.now)
        reopened = Optimizer(self.h, self.net, self.tmp.name, threading.Event(), Mock())
        self.assertEqual(reopened.state['mode'], 'observe')
        row = reopened.demand_auto.runs('acct', self.live['device'], self.now + 60)[0]
        self.assertEqual(row['result'], 'interrupted')
        self.assertIsNone(row['downtime'])
        self.assertEqual(row['reservedSeconds'], 240)

    def test_shutdown_during_preflight_cannot_start_a_model(self):
        self.setup_demand()
        self.o.state['pending'] = {'model': 'b', 'kind': 'demand'}
        self.o.command = Mock()

        def stop_during_read(*args, **kwargs):
            self.o.stop.set()
            return {**self.value, 'at': self.now}

        self.o.demand_decision.side_effect = stop_during_read
        self.o.switch('a', 'b', 'acct', self.live['device'])
        self.o.command.assert_not_called()
        self.assertEqual(self.o.state['mode'], 'observe')

    def test_disk_verification_requires_real_download_and_valid_template(self):
        self.o.runner.return_value = SimpleNamespace(
            stdout=json.dumps(
                {
                    'models': [
                        {
                            'id': 'b',
                            'size_bytes': 123,
                            'template_render_ok': True,
                            'estimated_memory_gb': 12,
                        }
                    ]
                }
            )
        )
        self.o.verify_local_target('b', [])
        self.assertEqual(self.o.local[0]['id'], 'b')
        for row in (
            {'id': 'b', 'size_bytes': 0, 'template_render_ok': True},
            {'id': 'b', 'size_bytes': 123, 'template_render_ok': False},
            {'id': 'c', 'size_bytes': 123, 'template_render_ok': True},
        ):
            self.o.runner.return_value = SimpleNamespace(stdout=json.dumps({'models': [row]}))
            with self.assertRaises(DemandDeferred):
                self.o.verify_local_target('b', [])

    def test_idle_escape_uses_recorded_demand_without_another_confirmation_or_minimum_run(self):
        self.setup_demand()
        self.o.switch = Mock()
        self.o.state['lastSwitchAt'] = self.now - 1200
        self.value.update(
            kind='explore', escapeReady=True, activity={'fresh': True, 'idleSeconds': 1200}
        )
        self.advance(0)
        self.o.switch.assert_called_once()
        self.assertEqual(self.o.state['pending']['demandKind'], 'explore')
        self.assertFalse(self.o.state['pending']['afterIdleTimeout'])

    def test_quiet_observation_threshold_is_not_followed_by_an_idle_command_wait(self):
        self.setup_demand()
        self.o.switch = Mock()
        self.value.update(
            kind='explore', escapeReady=False, activity={'fresh': True, 'idleSeconds': 1190}
        )
        self.advance(0)
        self.o.switch.assert_not_called()
        self.value['escapeReady'] = True
        self.o.idle_since = None
        self.advance(15)
        self.o.switch.assert_called_once()

    def test_trial_idle_eligibility_is_rechecked_before_command(self):
        self.setup_demand()
        self.o.state['pending'] = {'model': 'b', 'kind': 'demand', 'demandKind': 'explore'}
        self.value.update(kind='explore', escapeReady=False)
        self.o.command = Mock()
        self.o.switch('a', 'b', 'acct', self.live['device'])
        self.o.command.assert_not_called()
        self.assertEqual(self.o.state['mode'], 'demand')

    def test_earnings_shortfall_dispatches_while_busy_without_any_idle_wait(self):
        self.setup_demand()
        self.o.switch = Mock()
        self.o.idle_since = None
        self.value.update(
            kind='explore',
            escapeReady=True,
            explorationTrigger='earnings_target',
            activity={'fresh': True, 'idleSeconds': 0},
        )
        self.advance(0)
        self.o.switch.assert_called_once()
        self.assertEqual(self.o.next_switch, self.now)
        self.advance(15)
        self.o.switch.assert_called_once()
        self.assertFalse(self.o.state['pending']['afterIdleTimeout'])

    def test_background_worker_can_dispatch_without_any_browser_or_http_request(self):
        self.setup_demand()
        dispatched = threading.Event()
        self.o.switch = Mock(side_effect=lambda *args: dispatched.set())
        self.o.refresh = Mock()  # fixture data; never contact a real provider
        self.o.idle_since = None
        self.o.raw['inference_active'] = True
        self.value.update(
            kind='explore',
            escapeReady=True,
            explorationTrigger='earnings_target',
            activity={'fresh': True, 'idleSeconds': 0},
        )
        try:
            self.o.start()
            self.assertTrue(dispatched.wait(3), self.o.detail)
            self.o.switch.assert_called_once_with('a', 'b', 'acct', self.live['device'])
            self.assertFalse(self.o.state['pending']['afterIdleTimeout'])
        finally:
            self.o.stop.set()
            for worker in (self.o.refresh_worker, self.o.control_worker, self.o.worker):
                if worker:
                    worker.join(timeout=3)

    def test_reset_missing_and_stale_final_counters_still_hold_busy_switch(self):
        for changed in (
            {'stats': {'requests_served': 0, 'tokens_generated': 0}},
            {'stats': {}},
            {'written_at': self.now - 40},
            {'inference_active': None},
            {'pid': 999},
        ):
            self.setup_demand()
            self.o.state['pending'] = {'model': 'b', 'kind': 'demand'}
            self.o.command = Mock()
            self.o.read_state.side_effect = [
                copy.deepcopy(self.o.raw),
                {**self.o.raw, 'inference_active': True, **changed},
            ]
            self.o.switch('a', 'b', 'acct', self.live['device'])
            self.o.command.assert_not_called()
            self.o.read_state.side_effect = None

    def test_lost_decode_proof_or_cold_final_model_cannot_switch(self):
        self.setup_demand()
        self.o.state['pending'] = {'model': 'b', 'kind': 'demand'}
        self.o.command = Mock()
        self.o.warmup = {}
        self.o.switch('a', 'b', 'acct', self.live['device'])
        self.o.command.assert_not_called()
        self.setup_demand()
        self.o.state['pending'] = {'model': 'b', 'kind': 'demand'}
        self.o.command = Mock()
        self.o.read_state.side_effect = [
            copy.deepcopy(self.o.raw),
            {**self.o.raw, 'warm_models': []},
        ]
        self.o.switch('a', 'b', 'acct', self.live['device'])
        self.o.command.assert_not_called()

    def test_real_observe_collects_idle_only_after_verified_ready_samples(self):
        self.setup_demand()
        self.o.tracking = Mock(return_value={'counting': True, 'verifiedAt': self.now - 1})
        for offset in (0, 3, 6):
            raw = {**self.o.raw, 'written_at': self.now + offset}
            self.o.observe('acct', raw, {**self.o.live, 'at': self.now + offset})
        a = self.o.demand_auto.trials.activity(
            'acct', self.live['device'], self.o.raw, self.now + 6
        )
        self.assertEqual(a['idleSeconds'], 6)
        self.o.tracking.return_value = {'counting': False}
        self.o.observe(
            'acct', {**self.o.raw, 'written_at': self.now + 9}, {**self.o.live, 'at': self.now + 9}
        )
        self.assertFalse(
            self.o.demand_auto.trials.activity(
                'acct', self.live['device'], self.o.raw, self.now + 9
            )['fresh']
        )


if __name__ == '__main__':
    unittest.main()
