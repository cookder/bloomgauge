"""Display provenance, source freshness and execution-state precedence."""

import copy
import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import optimizer_live
from demand_optimizer import policy
from optimizer_store import device_id
from optimizer_control import PLAN_FIELDS, digest
from model_readiness import session_key

NOW = 1790132000


def fixture(now=NOW):
    raw = {
        'attestation_public_key': 'synthetic-key',
        'pid': 41,
        'started_at': now - 3600,
        'written_at': now,
        'advertised_models': ['a'],
        'current_model': 'a',
        'warm_models': ['a'],
    }
    live = {
        'account': 'private-account',
        'device': device_id(raw),
        'at': now,
        'provider': {'online': True},
    }
    state = {
        'account': live['account'],
        'device': live['device'],
        'mode': 'demand',
        'models': ['a', 'b', 'c', 'd'],
        'startedAt': now - 86400,
        'lastSwitchAt': now - 600,
        'expectedModel': 'a',
        'demandPolicy': policy(),
    }
    o = SimpleNamespace(
        state=state,
        live=live,
        raw=raw,
        lock=threading.RLock(),
        status='optimizing',
        detail='Keeping the current model.',
        update_guard=None,
        stop=threading.Event(),
    )
    o.warmup = {'status': 'ready', 'model': 'a', 'session': session_key(raw)}
    o.live_projection = optimizer_live.OptimizerLive(o)
    return o


def decision(now=NOW):
    baseline = {
        'rate': 0.10,
        'lower': 0.08,
        'upper': 0.15,
        'recentGuardRate': 0.12,
        'liveGuardRate': 0.15,
        'asOf': now - 120,
        'forecastUsable': True,
        'historyReason': 'Matched paid history.',
    }
    rows = [
        {
            'model': model,
            'selected': True,
            'eligible': True,
            'reason': None,
            'kind': 'earnings',
            'estimate': {**baseline, 'rate': 0.3},
            'netGainUsd': net,
            'signal': {'load': 10, 'observedAt': now - 20},
            'sustained': {'sourceAt': now - 30},
        }
        for model, net in [('c', 0.20), ('a', None), ('b', 0.30), ('d', 0.01)]
    ]
    return {
        'at': now,
        'currentModel': 'a',
        'policy': policy(),
        'target': 'd',
        'kind': 'explore',
        'sourceAt': now - 30,
        'baseline': baseline,
        'opportunities': rows,
        'reason': 'Historical comparison reason.',
        'planningMinutes': 60,
        'limits': {'nextRunAt': now + 1200},
        'trial': None,
    }


class OptimizerLiveTests(unittest.TestCase):
    def setUp(self):
        self.o = fixture()
        self.cache = self.o.live_projection
        self.d = decision()
        self.record()

    def record(self):
        self.cache.record(
            self.d,
            copy.deepcopy(self.o.state),
            copy.deepcopy(self.o.live),
            copy.deepcopy(self.o.raw),
        )

    def view(self, now=NOW, account='private-account'):
        return self.cache.snapshot(account, now)

    def test_actual_wait_beats_fresh_historical_productive_hold(self):
        self.o.status = 'waiting'
        self.o.detail = 'Waiting for fresh local readings.'
        self.d['reason'] = 'Preserving productive paid work.'
        self.record()
        result = self.view()
        self.assertEqual(result['phase'], 'waiting')
        self.assertEqual(result['reason'], self.o.detail)
        self.assertTrue(result['fresh'])  # Observation freshness is not command readiness.
        self.assertIsNone(result['proposalTarget'])

    def test_authoritative_return_target_wins_over_row_order_and_larger_net(self):
        self.d['explorationTrigger'] = 'baseline_return'
        self.d['opportunities'][0]['selectionReason'] = 'baseline_learning'
        self.record()
        result = self.view()
        self.assertEqual(result['comparisonTarget'], 'd')
        self.assertEqual([r['model'] for r in result['candidates']], ['d', 'b', 'c'])
        self.assertEqual(result['candidates'][0]['selectionReason'], 'baseline_return')
        self.assertIsNone(result['candidates'][2]['selectionReason'])
        self.assertIsNone(result['pendingTarget'])
        self.assertIsNone(result['proposalTarget'])

    def test_planning_horizon_and_current_paid_guard_values_are_copied(self):
        self.o.state['demandPolicy']['planningMinutes'] = 120
        self.d['policy'] = policy(self.o.state['demandPolicy'])
        self.d['planningMinutes'] = 120
        self.record()
        result = self.view()
        paid = result['currentPaidBasis']
        self.assertEqual(result['planningMinutes'], 120)
        self.assertEqual(paid['meanUsdPerWarmHour'], 0.10)
        self.assertEqual(paid['upperUsdPerWarmHour'], 0.15)
        self.assertEqual(paid['recentGuardUsdPerHour'], 0.12)
        self.assertEqual(paid['liveGuardUsdPerHour'], 0.15)
        self.assertEqual(paid['basis'], 'matched_history')
        self.assertEqual(paid['asOf'], NOW - 120)
        self.assertEqual(result['earliestEligibleAt'], NOW + 1200)
        self.assertNotIn('nextSwitchAt', result)
        self.assertNotIn('readiness', result)

    def test_held_positive_history_and_missing_net_do_not_rank_as_paid_alternatives(self):
        self.d['target'] = None
        for row in self.d['opportunities']:
            if row['model'] == 'b':
                row.update(eligible=False, reason='Not enough memory.')
            if row['model'] == 'c':
                row.update(netGainUsd=None)
            if row['model'] == 'd':
                row.update(netGainUsd=0)
        self.record()
        rows = self.view()['candidates']
        self.assertEqual(
            [(r['model'], r['group']) for r in rows],
            [('d', 'paid_alternative'), ('b', 'held'), ('c', 'unknown')],
        )
        self.assertEqual(rows[1]['firstBlocker'], 'Not enough memory.')
        self.assertEqual(rows[1]['meanUsdPerWarmHour'], 0.3)
        self.assertIsNone(rows[2]['netGainUsd'])
        self.assertEqual(rows[0]['netGainUsd'], 0)

    def test_saved_pool_only_is_bounded_and_keeps_authoritative_target(self):
        self.o.state['models'] += ['m' + str(i) for i in range(40)]
        self.d['opportunities'].append(
            {**self.d['opportunities'][0], 'model': 'not-selected', 'netGainUsd': 999}
        )
        self.record()
        rows = self.view()['candidates']
        self.assertEqual(len(rows), 16)
        self.assertEqual(rows[0]['model'], 'd')
        self.assertNotIn('not-selected', [r['model'] for r in rows])
        self.assertNotIn('a', [r['model'] for r in rows])

    def test_each_identity_session_selection_mode_pool_and_policy_change_clears_old_comparison(
        self,
    ):
        mutations = [
            lambda o: o.live.update(account='new-account'),
            lambda o: o.state.update(account='new-account'),
            lambda o: o.live.update(device='new-device'),
            lambda o: o.raw.update(attestation_public_key='new-key'),
            lambda o: o.raw.update(pid=99),
            lambda o: o.raw.update(started_at=NOW - 50),
            lambda o: o.raw.update(advertised_models=['b']),
            lambda o: o.state.update(mode='observe'),
            lambda o: o.state.update(models=['a', 'b']),
            lambda o: o.state['demandPolicy'].update(planningMinutes=120),
            lambda o: o.state.update(startedAt=NOW),
            lambda o: o.state.update(lastSwitchAt=NOW),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                self.setUp()
                mutate(self.o)
                result = self.view()
                self.assertFalse(result['fresh'])
                self.assertEqual(result['candidates'], [])
                self.assertIsNone(result['currentPaidBasis'])
                self.assertIsNone(result['comparisonTarget'])
                self.assertIsNone(self.cache.cached)

    def test_collector_account_change_or_logout_revokes_cache_even_with_old_live_identity(self):
        for account in ('new-account', '', None):
            self.setUp()
            result = self.view(account=account)
            self.assertFalse(result['fresh'])
            self.assertEqual(result['candidates'], [])
            self.assertIsNone(self.cache.cached)

    def test_slow_comparison_cannot_publish_after_its_original_binding_changes(self):
        inputs = copy.deepcopy((self.o.state, self.o.live, self.o.raw))
        self.o.raw['pid'] += 1
        self.cache.record(self.d, *inputs)
        self.assertIsNone(self.cache.cached)
        self.assertEqual(self.view()['currentModel'], 'a')

    def test_invalid_or_mismatched_decision_cannot_populate_cache(self):
        for changes in (
            {'currentModel': 'b'},
            {'policy': {}},
            {'target': 'outside'},
            {'at': float('nan')},
        ):
            self.setUp()
            self.d.update(changes)
            self.record()
            self.assertIsNone(self.cache.cached)

    def test_stale_comparison_hides_historical_values_but_retains_origin_and_known_mode(self):
        self.o.live['at'] = self.o.raw['written_at'] = NOW + 91
        result = self.view(NOW + 91)
        self.assertFalse(result['fresh'])
        self.assertEqual(result['phase'], 'waiting')
        self.assertEqual(result['lastComparisonAt'], NOW)
        self.assertEqual(result['mode'], 'demand')
        self.assertEqual(result['currentModel'], 'a')
        self.assertEqual(result['candidates'], [])
        for field in (
            'currentPaidBasis',
            'comparisonTarget',
            'planningMinutes',
            'earliestEligibleAt',
        ):
            self.assertIsNone(result[field])

    def test_wall_clock_rollback_cannot_revive_monotonically_expired_cache(self):
        with patch.object(
            optimizer_live.time,
            'monotonic',
            return_value=self.cache.cached['createdMonotonic'] + 91,
        ):
            self.assertFalse(self.view()['fresh'])
            self.assertEqual(self.view()['candidates'], [])

    def test_source_and_local_age_are_checked_separately_from_poll_time(self):
        for field, value in [('sourceAt', NOW - 91), ('sourceAt', NOW + 1)]:
            self.setUp()
            self.d[field] = value
            self.record()
            self.assertFalse(self.view()['fresh'])
        for bucket, field, value in [
            ('live', 'at', NOW - 10),
            ('raw', 'written_at', NOW - 15),
            ('raw', 'written_at', NOW + 5),
        ]:
            self.setUp()
            getattr(self.o, bucket)[field] = value
            self.assertFalse(self.view()['fresh'])
        self.setUp()
        self.o.live['at'] = self.o.raw['written_at'] = NOW + 30
        result = self.view(NOW + 30)
        self.assertTrue(result['fresh'])
        self.assertEqual(result['sourceAt'], NOW - 30)
        self.assertEqual(result['lastComparisonAt'], NOW)

    def test_fresh_scan_timestamp_does_not_replace_missing_current_network_observation(self):
        self.d['scanAt'] = NOW
        next(r for r in self.d['opportunities'] if r['model'] == 'a')['signal']['observedAt'] = None
        self.record()
        self.assertFalse(self.view()['fresh'])

    def test_target_row_staleness_invalidates_the_whole_comparison(self):
        next(r for r in self.d['opportunities'] if r['model'] == 'd')['signal']['observedAt'] = (
            NOW - 91
        )
        self.record()
        result = self.view()
        self.assertFalse(result['fresh'])
        self.assertIsNone(result['comparisonTarget'])
        self.assertEqual(result['candidates'], [])

    def test_old_individual_candidate_is_unknown_without_hiding_other_fresh_rows(self):
        next(r for r in self.d['opportunities'] if r['model'] == 'c')['signal']['observedAt'] = (
            NOW - 91
        )
        self.record()
        result = self.view()
        row = next(r for r in result['candidates'] if r['model'] == 'c')
        self.assertTrue(result['fresh'])
        self.assertEqual(row['group'], 'unknown')
        self.assertFalse(row['eligible'])
        self.assertIsNone(row['netGainUsd'])
        self.assertIsNone(row['meanUsdPerWarmHour'])
        self.assertIsNone(row['load'])

    def test_pending_switch_is_actual_even_if_comparison_expired_or_mode_is_manual(self):
        self.o.state.update(mode='observe', pending={'model': 'b', 'kind': 'demand'})
        self.o.detail = 'Rechecking the confirmed opportunity.'
        result = self.view(NOW + 100)
        self.assertEqual(result['phase'], 'switching')
        self.assertEqual(result['pendingTarget'], 'b')
        self.assertFalse(result['fresh'])
        self.assertIsNone(result['comparisonTarget'])

    def test_confirmation_copies_observed_progress_without_countdown_or_prediction(self):
        self.o.state['demandProposal'] = {
            'model': 'd',
            'checkedAt': NOW,
            'sourceAt': NOW - 30,
            'seconds': 120,
        }
        self.o.detail = 'Confirming d using fresh demand and paid-work evidence.'
        first = self.view()
        self.o.live['at'] = self.o.raw['written_at'] = NOW + 30
        second = self.view(NOW + 30)
        self.assertEqual(first['phase'], 'confirming')
        self.assertEqual(second['confirmation']['seconds'], 120)
        self.assertEqual(
            second['confirmation']['requiredSeconds'], self.d['policy']['confirmationMinutes'] * 60
        )
        self.assertEqual(second['proposalTarget'], 'd')
        self.assertIsNone(second['pendingTarget'])

    def test_minimum_run_gate_is_waiting_and_does_not_claim_a_scheduled_switch(self):
        self.o.state['demandProposal'] = {
            'model': 'd',
            'checkedAt': NOW,
            'sourceAt': NOW - 30,
            'seconds': 600,
        }
        self.o.detail = 'Keeping the current model until its minimum run is complete.'
        result = self.view()
        self.assertEqual(result['phase'], 'waiting')
        self.assertIsNone(result['confirmation'])
        self.assertEqual(result['earliestEligibleAt'], NOW + 1200)

    def test_measurement_requires_current_bound_running_or_settling_trial(self):
        for status in ('running', 'settling'):
            self.d['trial'] = {
                'model': 'a',
                'current': True,
                'status': status,
                'warmSeconds': 240,
                'trialMinutes': 20,
            }
            self.record()
            result = self.view()
            self.assertEqual(result['phase'], 'measuring')
            self.assertEqual(result['measurement']['warmSeconds'], 240)
        for changes in (
            {'current': False},
            {'model': 'b'},
            {'status': 'productive'},
            {'status': 'insufficient_coverage'},
        ):
            self.d['trial'].update(changes)
            self.record()
            self.assertIsNone(self.view()['measurement'])

    def test_off_waiting_pending_and_update_holds_precede_measurement(self):
        for hold in ('off', 'waiting', 'pending', 'update', 'offline'):
            self.setUp()
            self.d['trial'] = {
                'model': 'a',
                'current': True,
                'status': 'running',
                'warmSeconds': 240,
                'trialMinutes': 20,
            }
            self.record()
            if hold == 'off':
                self.o.state['mode'] = 'observe'
            elif hold == 'waiting':
                self.o.status = 'waiting'
                self.o.detail = (
                    'The Mac is hot. Holding the current model until temperatures settle.'
                )
            elif hold == 'pending':
                self.o.state['pending'] = {'model': 'b'}
            elif hold == 'update':
                self.o.update_guard = SimpleNamespace(
                    lease='synthetic', deadline=time.monotonic() + 60
                )
            elif hold == 'offline':
                self.o.live['provider']['online'] = False
            result = self.view()
            self.assertIsNone(result['measurement'])
            self.assertNotEqual(result['phase'], 'measuring')

    def test_cold_or_unverified_model_cannot_claim_measurement_from_cached_trial(self):
        for cold in (True, False):
            self.setUp()
            self.d['trial'] = {
                'model': 'a',
                'current': True,
                'status': 'running',
                'warmSeconds': 240,
                'trialMinutes': 20,
            }
            self.record()
            if cold:
                self.o.raw['warm_models'] = []
            else:
                self.o.warmup['session'] = 'another-session'
            result = self.view()
            self.assertEqual(result['phase'], 'waiting')
            self.assertIsNone(result['measurement'])

    def test_readiness_wait_text_is_not_relabelled_as_measuring(self):
        self.o.detail = 'Waiting for verified warm readiness before following demand.'
        self.assertEqual(self.view()['phase'], 'waiting')

    def pending_on(self):
        self.o.state['mode'] = 'observe'
        op = {
            'id': 'synthetic-on',
            'status': 'pending',
            'at': NOW,
            'expiresAt': NOW + 600,
            'account': self.o.live['account'],
            'device': self.o.live['device'],
            'plan': digest({key: self.o.state.get(key) for key in PLAN_FIELDS}),
            'session': session_key(self.o.raw),
            'detail': 'Checking readiness before turning On.',
            'startRequested': False,
        }
        self.o.automatic_control = SimpleNamespace(
            operation=op, requests=[{'id': op['id']}], deadline=time.monotonic() + 600
        )
        return op

    def test_saved_manual_mode_with_accepted_on_intent_is_waiting(self):
        for status in ('pending', 'starting', 'waiting'):
            self.setUp()
            op = self.pending_on()
            op['status'] = status
            result = self.view()
            self.assertEqual(result['mode'], 'observe')
            self.assertEqual(result['phase'], 'waiting')
            self.assertEqual(result['reason'], op['detail'])
            self.assertFalse(result['fresh'])
            self.assertEqual(result['candidates'], [])
            self.assertIsNone(result['measurement'])

    def test_expired_cancelled_replaced_or_unbound_on_intent_does_not_claim_readiness(self):
        changes = [
            lambda o: o.automatic_control.operation.update(status='cancelled'),
            lambda o: o.automatic_control.operation.update(expiresAt=NOW),
            lambda o: setattr(o.automatic_control, 'deadline', time.monotonic() - 1),
            lambda o: setattr(o.automatic_control, 'requests', []),
            lambda o: o.automatic_control.operation.update(id='not-receipted'),
            lambda o: o.state.update(models=['a', 'b']),
            lambda o: o.live.update(account='another'),
            lambda o: o.raw.update(pid=99),
            lambda o: o.stop.set(),
        ]
        for change in changes:
            self.setUp()
            self.pending_on()
            change(self.o)
            self.assertEqual(self.view()['phase'], 'off')

    def test_real_accepted_on_remains_waiting_without_dispatch_or_replaying_request(self):
        import test_optimizer_control

        case = test_optimizer_control.ControlTests()
        case.setUp()
        try:
            case.enable()
            with patch.object(
                case.o.provider_control,
                'inspect',
                side_effect=AssertionError('No read-time inspection'),
            ):
                result = case.o.live_projection.snapshot('acct')
            self.assertEqual(result['mode'], 'observe')
            self.assertEqual(result['phase'], 'waiting')
            self.assertEqual(result['reason'], case.control.operation['detail'])
            case.o.runner.assert_not_called()
            self.assertEqual(len(case.control.requests), 1)
        finally:
            case.tearDown()

    def test_nonfinite_numbers_are_unknown_but_zero_and_signed_values_survive(self):
        self.d['baseline'].update(rate=0, lower=0, upper=0, liveGuardRate=0, recentGuardRate=0)
        row = next(r for r in self.d['opportunities'] if r['model'] == 'b')
        row.update(netGainUsd=-0.01)
        row['estimate'].update(rate=float('nan'), lower=True, upper=float('inf'))
        row['signal']['load'] = 0
        self.record()
        result = self.view()
        self.assertEqual(result['currentPaidBasis']['meanUsdPerWarmHour'], 0)
        row = next(r for r in result['candidates'] if r['model'] == 'b')
        self.assertEqual(row['netGainUsd'], -0.01)
        self.assertEqual(row['load'], 0)
        for field in ('meanUsdPerWarmHour', 'lowerUsdPerWarmHour', 'upperUsdPerWarmHour'):
            self.assertIsNone(row[field])
        json.dumps(result, allow_nan=False)

    def test_get_is_bounded_read_only_and_does_not_touch_slow_surfaces(self):
        for name in (
            'snapshot',
            'demand_decision',
            'read_state',
            'read_options',
            'tracking',
            'save',
            'runner',
        ):
            setattr(
                self.o, name, Mock(side_effect=AssertionError('Forbidden display call: ' + name))
            )
        before = copy.deepcopy((self.o.state, self.o.live, self.o.raw, self.d))
        start = time.monotonic()
        for _ in range(100):
            result = self.view()
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertEqual(before, (self.o.state, self.o.live, self.o.raw, self.d))
        encoded = json.dumps(result)
        self.assertNotIn('private-account', encoded)
        self.assertNotIn(self.o.live['device'], encoded)
        self.assertNotIn('synthetic-key', encoded)
        result['candidates'][0]['meanUsdPerWarmHour'] = 999
        self.assertNotEqual(self.view()['candidates'][0]['meanUsdPerWarmHour'], 999)

    def test_display_record_failure_is_contained(self):
        with patch.object(self.cache, '_record', side_effect=RuntimeError('Bad display data')):
            self.record()
        self.assertIsNone(self.cache.cached)
        self.assertEqual(self.o.status, 'optimizing')

    def test_background_tick_publishes_without_recomputing_or_mutating_decision(self):
        import test_optimizer

        fixture_case = test_optimizer.ControllerTests()
        fixture_case.setUp()
        try:
            o = fixture_case.o
            now = fixture_case.now
            o.state['mode'] = 'demand'
            o.state['models'] = ['a', 'b', 'c', 'd']
            value = decision(now)
            value['target'] = None
            value['sourceAt'] = None
            value['reason'] = 'Keeping the current model.'
            o.demand_decision = Mock(return_value=value)
            before = copy.deepcopy(value)
            o.tick_demand(
                now,
                copy.deepcopy(o.state),
                copy.deepcopy(o.live),
                copy.deepcopy(o.raw),
                'a',
                [],
                {},
            )
            self.assertTrue(o.live_projection.snapshot('acct', now)['fresh'])
            self.assertEqual(value, before)
            o.demand_decision.assert_called_once()
            o.runner.assert_not_called()
        finally:
            fixture_case.tearDown()


if __name__ == '__main__':
    unittest.main()
