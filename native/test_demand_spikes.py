import copy
import unittest
from unittest.mock import Mock

from demand_optimizer import decide, policy, DemandOptimizer
from demand_spikes import spike, review, demand_context, retention
from history import History
from optimizer_store import OptimizerStore
from test_demand_optimizer import NOW, candidate, summary
import test_demand_controller as controller_tests
from optimizer import Optimizer, session_key


def signal():
    s = candidate('nemotron')['signal']
    s.update(
        status='spike',
        load=276,
        pressure=3.4,
        baselineLoad=8.6,
        baselinePressure=0.174,
        baselineHours=20,
        baselineAsOf=NOW - 120,
        loadRatio=32,
        pressureRatio=19.6,
    )
    s['sustained'] = {
        'qualified': True,
        'seconds': 600,
        'samples': 19,
        'sourceAt': NOW - 30,
        'pressure': 3.35,
        'windows': [
            {'load': 264, 'pressure': 3.35, 'coverage': 1, 'qualified': True},
            {'load': 275, 'pressure': 3.4, 'coverage': 0.9, 'qualified': True},
        ],
    }
    cutoff = int(NOW // 30) * 30
    s['spikeContext'] = demand_context(
        [
            {'at': at, 'active': 270 if at >= cutoff - 600 else 8, 'queued': 0, 'warm': 80}
            for at in range(cutoff - 2400, cutoff, 30)
        ],
        NOW,
    )
    return s


def fixtures():
    a, b = candidate('gemma'), candidate('nemotron')
    b['signal'] = signal()
    a['signal'].update(status='normal', sustained=copy.deepcopy(b['signal']['sustained']))
    a['earningsTarget'] = {
        'ready': False,
        'status': 'productive',
        'reason': 'Preserving productive work.',
        'rate': 0.12,
        'fastRate': 0.11,
        'warmMinutes': 39,
        'usdPerHour': 0.12,
        'belowTarget': False,
        'livePaid': {'fresh': True, 'rate': 0.115, 'seconds': 300, 'asOf': NOW - 10},
    }
    return (
        [a, b],
        {'gemma': {**summary(0.12), 'recent': True}},
        {'fresh': True, 'idleSeconds': 0, 'busy': True},
    )


def choose(rows=None, rates=None, runs=None, trial=None, rules=None, activity=None):
    a, b, c = fixtures()
    return decide(
        rows or a,
        'gemma',
        rates or b,
        [],
        runs or [],
        rules or policy(),
        NOW,
        activity=activity or c,
        trial=trial,
    )


def experiment():
    d = choose()
    chosen = next(r for r in d['opportunities'] if r['model'] == 'nemotron')
    run = {
        'id': 42,
        'at': NOW - 1500,
        'model': 'nemotron',
        'previousModel': 'gemma',
        'result': 'switched',
        'downtime': 90,
        'decision': {
            'kind': 'explore',
            'explorationTrigger': 'demand_spike',
            'candidate': chosen,
            'spikeTrial': chosen['spikeTrial'],
        },
    }
    trial = {
        'runId': 42,
        'model': 'nemotron',
        'current': True,
        'status': 'productive',
        'complete': True,
        'settled': True,
        'usdPerHour': 0.04,
        'paidWarmSeconds': 1200,
        'trialMinutes': 20,
        'warmSeconds': 1200,
        'windowEnd': NOW - 180,
    }
    return run, trial


class SpikeAdmissionTests(unittest.TestCase):
    def test_prolonged_exceptional_demand_can_qualify_without_another_jump(self):
        s = signal()
        cutoff = int(NOW // 30) * 30
        s['spikeContext'] = demand_context(
            [
                {'at': at, 'active': 270, 'queued': 0, 'warm': 80}
                for at in range(cutoff - 2400, cutoff, 30)
            ],
            NOW,
        )
        v = spike(s, [], 'nemotron', NOW)
        self.assertTrue(v['qualified'])
        self.assertIn('six', v['reason'])

    def test_recent_plateau_is_not_exceptional_against_quiet_seasonal_baseline(self):
        s = signal()
        s['spikeContext']['recent'].update(load=200, pressure=2.5)
        self.assertFalse(spike(s, [], 'nemotron', NOW)['qualified'])

    def test_mean_spikes_cannot_hide_low_typical_demand_or_collapsed_tail(self):
        for area in ('windows', 'tails'):
            s = signal()
            s['spikeContext'][area][-1].update(load=20, pressure=0.2)
            with self.subTest(area=area):
                self.assertFalse(spike(s, [], 'nemotron', NOW)['qualified'])

    def test_missing_recent_context_or_stale_tail_never_admits_a_spike(self):
        for mutation in ('missing', 'gap', 'stale', 'future'):
            s = signal()
            if mutation == 'missing':
                s.pop('spikeContext')
            if mutation == 'gap':
                s['spikeContext']['recent']['qualified'] = False
            if mutation == 'stale':
                s['spikeContext']['sourceAt'] = NOW - 91
            if mutation == 'future':
                s['spikeContext']['sourceAt'] = NOW + 1
            with self.subTest(mutation=mutation):
                self.assertFalse(spike(s, [], 'nemotron', NOW)['qualified'])

    def test_observed_nemotron_regime_can_leave_productive_busy_gemma(self):
        d = choose()
        self.assertEqual(d['target'], 'nemotron')
        self.assertEqual(d['explorationTrigger'], 'demand_spike')
        self.assertEqual(d['kind'], 'explore')
        self.assertTrue(d['escapeReady'])
        row = next(r for r in d['opportunities'] if r['model'] == 'nemotron')
        self.assertIsNone(row['netGainUsd'])
        self.assertEqual(row['spikeTrial']['referenceRate'], 0.12)

    def test_sparse_old_zero_is_neutral_but_known_same_regime_is_not_retested(self):
        rows, rates, _ = fixtures()
        rates['nemotron'] = {**summary(0), 'forecastUsable': False}
        self.assertEqual(choose(rates=rates)['target'], 'nemotron')
        rates['nemotron'] = summary(0.04)
        self.assertIsNone(choose(rates=rates)['target'])

    def test_paid_upgrade_takes_priority(self):
        rows, rates, _ = fixtures()
        rows.append(candidate('better'))
        rates['better'] = summary(0.5)
        d = choose(rows, rates)
        self.assertEqual(d['target'], 'better')
        self.assertEqual(d['kind'], 'earnings')

    def test_normal_demand_does_not_undo_productive_protection(self):
        rows, _, _ = fixtures()
        rows[1]['signal'].update(status='normal', load=139, pressure=1.51)
        self.assertIsNone(choose(rows)['target'])

    def test_fresh_full_baseline_and_both_exceptional_windows_required(self):
        for key, value in [
            ('status', 'watching'),
            ('baselineHours', 1.9),
            ('baselineAsOf', NOW - 601),
            ('baselineAsOf', NOW + 1),
            ('baselineLoad', 0),
            ('baselinePressure', float('nan')),
        ]:
            s = signal()
            s[key] = value
            with self.subTest(key=key):
                self.assertFalse(spike(s, [], 'nemotron', NOW)['qualified'])
        for change in ({'pressure': 0.99}, {'load': 9}, {'coverage': 0.7}, {'qualified': False}):
            s = signal()
            s['sustained']['windows'][0].update(change)
            self.assertFalse(spike(s, [], 'nemotron', NOW)['qualified'])

    def test_one_old_spike_or_a_fading_plateau_does_not_qualify(self):
        for key in ('pressure', 'load'):
            s = signal()
            s['sustained']['windows'][1][key] = s['sustained']['windows'][0][key] * 0.7
            self.assertFalse(spike(s, [], 'nemotron', NOW)['qualified'])
        s = signal()
        s['sustained']['sourceAt'] = NOW - 91
        self.assertFalse(spike(s, [], 'nemotron', NOW)['qualified'])

    def test_resource_selection_and_readiness_guards_remain(self):
        for change in (
            {'available': False},
            {'selected': False},
            {'loadBudget': None},
            {'loadBudget': {'afterUnloadGB': 30.5, 'requiredGB': 30}},
        ):
            rows, _, _ = fixtures()
            rows[1].update(change)
            self.assertIsNone(choose(rows)['target'])
        self.assertIsNone(choose(activity={'fresh': False, 'idleSeconds': 0})['target'])
        rows, _, _ = fixtures()
        rows[0]['earningsTarget']['livePaid']['fresh'] = False
        self.assertIsNone(choose(rows)['target'])
        rows, _, _ = fixtures()
        rows[0]['earningsTarget']['warmMinutes'] = 10
        self.assertIsNone(choose(rows)['target'])

    def test_daily_trials_and_return_attempt_are_reserved(self):
        runs = [
            {
                'at': NOW - 3600 - i,
                'model': str(i),
                'downtime': 90,
                'decision': {'explorationTrigger': 'demand_spike'},
            }
            for i in range(3)
        ]
        self.assertIsNone(choose(runs=runs)['target'])
        self.assertIsNone(
            choose(
                runs=[{'at': NOW - 100, 'downtime': 90}], rules=policy({'maxSwitchesPerDay': 2})
            )['target']
        )
        self.assertEqual(
            choose(runs=[{**r, 'at': NOW - 90000} for r in runs])['target'], 'nemotron'
        )

    def test_same_plateau_is_not_repeated_but_materially_larger_demand_can_be(self):
        run, _ = experiment()
        self.assertIsNone(choose(runs=[run])['target'])
        rows, _, _ = fixtures()
        for w in rows[1]['signal']['sustained']['windows']:
            w['pressure'] *= 2.1
        self.assertEqual(choose(rows, runs=[run])['target'], 'nemotron')


class SpikeReviewTests(unittest.TestCase):
    def review(self, trial=None, rate=0.04, age=1500):
        r, t = experiment()
        t.update(trial or {})
        r['at'] = NOW - age
        return review([r], t, 'nemotron', {'fresh': True, 'rate': rate}, {'fresh': True}, NOW)

    def faded_case(self):
        r, t = experiment()
        t.update(complete=False, status='running', warmSeconds=420, warmStartedAt=NOW - 450)
        cutoff = int(NOW // 30) * 30
        context = demand_context(
            [
                {'at': at, 'active': 100, 'queued': 0, 'warm': 80}
                for at in range(cutoff - 240, cutoff, 30)
            ],
            NOW,
        )
        paid = {'fastRate': 0.02, 'asOf': NOW - 120, 'warmMinutes': 4}
        return r, t, context, paid

    def test_faded_spike_with_verified_low_pay_can_return_early_while_busy(self):
        r, t, c, p = self.faded_case()
        v = review(
            [r],
            t,
            'nemotron',
            {'fresh': True, 'rate': 0.02},
            {'fresh': True, 'busy': True},
            NOW,
            p,
            c,
        )
        self.assertEqual(v['status'], 'return')
        self.assertEqual(v['demand']['status'], 'faded')
        self.assertFalse(t['complete'])
        self.assertEqual(t['warmSeconds'], 420)
        rows, _, activity = fixtures()
        rows[1]['signal']['spikeContext'] = c
        rows[1]['earningsTarget'] = {
            **rows[0]['earningsTarget'],
            **p,
            'livePaid': {'fresh': True, 'rate': 0.02},
        }
        d = decide(
            rows,
            'nemotron',
            {},
            [],
            [r],
            policy(),
            NOW,
            last_switch=NOW - 450,
            activity=activity,
            trial=t,
        )
        self.assertEqual(d['target'], 'gemma')
        self.assertTrue(d['escapeReady'])
        self.assertEqual(d['explorationTrigger'], 'spike_return')

    def test_a_brief_dip_rebound_cold_or_missing_demand_cannot_end_trial(self):
        for change in ('rebound', 'gap', 'stale', 'before_warm', 'unknown', 'future'):
            r, t, c, p = self.faded_case()
            if change == 'rebound':
                c['tails'][-1].update(load=300, pressure=4)
            if change == 'gap':
                c['tails'][0]['qualified'] = False
            if change == 'stale':
                c['sourceAt'] = NOW - 91
            if change == 'before_warm':
                t['warmStartedAt'] = NOW - 120
            if change == 'unknown':
                c = None
            if change == 'future':
                c['sourceAt'] = NOW + 1
            v = review(
                [r], t, 'nemotron', {'fresh': True, 'rate': 0.02}, {'fresh': True}, NOW, p, c
            )
            with self.subTest(change=change):
                self.assertEqual(v['status'], 'measuring')

    def test_good_pay_or_insufficient_paid_evidence_overrides_network_fade(self):
        for change in (
            'fresh_recovery',
            'settled_recovery',
            'stale_paid',
            'partial_paid',
            'cold',
            'missing_live',
            'missing_activity',
        ):
            r, t, c, p = self.faded_case()
            live = {'fresh': True, 'rate': 0.02}
            activity = {'fresh': True}
            if change == 'fresh_recovery':
                live['rate'] = 0.12
            if change == 'settled_recovery':
                p['fastRate'] = 0.12
            if change == 'stale_paid':
                p['asOf'] = NOW - 241
            if change == 'partial_paid':
                p['warmMinutes'] = 3
            if change == 'cold':
                t['warmSeconds'] = 299
            if change == 'missing_live':
                live = {'fresh': False}
            if change == 'missing_activity':
                activity['fresh'] = False
            with self.subTest(change=change):
                self.assertEqual(
                    review([r], t, 'nemotron', live, activity, NOW, p, c)['status'], 'measuring'
                )

    def test_high_earnings_and_baseline_learning_are_protected(self):
        r, t, c, p = self.faded_case()
        p['highEarnings'] = {'active': True}
        self.assertEqual(
            review([r], t, 'nemotron', {'fresh': True, 'rate': 0.23}, {'fresh': True}, NOW, p, c)[
                'status'
            ],
            'keep',
        )
        p.pop('highEarnings')
        r['decision']['learningTrial'] = r['decision'].pop('spikeTrial')
        v = review([r], t, 'nemotron', {'fresh': True, 'rate': 0.02}, {'fresh': True}, NOW, p, c)
        self.assertEqual(v['status'], 'measuring')
        self.assertNotIn('demand', v)

    def test_waits_for_warm_measurement_and_credit_settlement(self):
        self.assertEqual(
            self.review({'complete': False, 'status': 'running'})['status'], 'measuring'
        )
        self.assertEqual(
            self.review({'complete': False, 'status': 'settling'})['status'], 'settling'
        )

    def test_returns_to_saved_incumbent_when_worse_and_keeps_competitive_paid_work(self):
        self.assertEqual(self.review()['status'], 'return')
        self.assertEqual(self.review({'usdPerHour': 0.13}, 0.13)['status'], 'keep')
        self.assertEqual(self.review({'usdPerHour': 0.119}, 0.119)['status'], 'keep')

    def test_partial_data_is_not_recorded_as_unpaid(self):
        d = self.review({'settled': False, 'paidWarmSeconds': 500, 'usdPerHour': 0.04})
        self.assertEqual(d['status'], 'return')
        self.assertIn('partial', d['reason'])

    def test_late_burst_gets_bounded_extension(self):
        self.assertEqual(self.review(rate=0.3)['status'], 'recovering')
        self.assertEqual(self.review({'windowEnd': NOW - 601}, rate=0.3)['status'], 'return')
        self.assertEqual(self.review(rate=0.3, age=2401)['status'], 'return')

    def test_paid_recovery_is_confirmed_before_returning_to_a_slower_incumbent(self):
        r, t = experiment()
        value = review(
            [r],
            t,
            'nemotron',
            {'fresh': True, 'rate': 0.4},
            {'fresh': True},
            NOW,
            {'fastRate': 0.3, 'asOf': NOW - 150},
        )
        self.assertEqual(value['status'], 'keep')
        value = review(
            [r],
            t,
            'nemotron',
            {'fresh': True, 'rate': 0.4},
            {'fresh': True},
            NOW,
            {'fastRate': 0.3, 'asOf': NOW - 250},
        )
        self.assertEqual(value['status'], 'recovering')

    def test_frozen_zero_trial_cannot_displace_fresh_productive_recovery(self):
        r, t = experiment()
        r['at'] = NOW - 3600
        t.update(usdPerHour=0, status='no_traffic')
        r['decision']['spikeTrial']['referenceRate'] = 0.15885
        paid = {'productiveFloor': 0.09, 'fastRate': 0.04, 'asOf': NOW - 120}
        value = review(
            [r], t, 'nemotron', {'fresh': True, 'rate': 0.090804}, {'fresh': True}, NOW, paid
        )
        self.assertEqual(value['status'], 'keep')
        self.assertIn('old incumbent', value['reason'])
        for rate in (0, 0.05, -0.01):
            with self.subTest(rate=rate):
                self.assertEqual(
                    review(
                        [r],
                        t,
                        'nemotron',
                        {'fresh': True, 'rate': rate},
                        {'fresh': True},
                        NOW,
                        paid,
                    )['status'],
                    'return',
                )
        self.assertEqual(
            review([r], t, 'nemotron', {'fresh': False, 'rate': 0.15}, {'fresh': True}, NOW, paid)[
                'status'
            ],
            'waiting',
        )
        self.assertEqual(
            review([r], t, 'nemotron', {'fresh': True, 'rate': 0.15}, {'fresh': False}, NOW, paid)[
                'status'
            ],
            'waiting',
        )
        t.update(complete=False, status='running', settled=False)
        self.assertEqual(
            review([r], t, 'nemotron', {'fresh': True, 'rate': 0.10}, {'fresh': True}, NOW, paid)[
                'status'
            ],
            'keep',
        )

    def test_expired_partial_trial_cannot_take_early_fade_path_over_paid_recovery(self):
        r, t, c, p = self.faded_case()
        r['at'] = NOW - 2500
        r['decision']['spikeTrial']['referenceRate'] = 0.15885
        p.update(productiveFloor=0.09, fastRate=0.091)
        value = review(
            [r], t, 'nemotron', {'fresh': True, 'rate': 0.090804}, {'fresh': True}, NOW, p, c
        )
        self.assertEqual(value['status'], 'keep')
        self.assertNotIn('early', value['reason'])

    def test_clock_limit_bounds_collection_gaps_but_freshness_is_still_required(self):
        self.assertEqual(
            self.review({'complete': False, 'status': 'running', 'settled': False}, age=2401)[
                'status'
            ],
            'return',
        )
        r, t = experiment()
        self.assertEqual(
            review([r], t, 'nemotron', {'fresh': False}, {'fresh': True}, NOW)['status'], 'waiting'
        )

    def test_session_changes_and_resolved_trials_do_not_restore_old_models(self):
        r, t = experiment()
        t['current'] = False
        self.assertIsNone(
            review([r], t, 'gemma', {'fresh': True, 'rate': 0.1}, {'fresh': True}, NOW)
        )
        t['current'] = True
        r['decision']['spikeResolution'] = {'status': 'keep'}
        self.assertIsNone(
            review([r], t, 'nemotron', {'fresh': True, 'rate': 0.1}, {'fresh': True}, NOW)
        )

    def test_real_decision_returns_without_idle_or_a_new_forecast(self):
        r, t = experiment()
        rows, _, activity = fixtures()
        rows[1]['earningsTarget'] = {
            **rows[0]['earningsTarget'],
            'livePaid': {'fresh': True, 'rate': 0.04},
            'rate': 0.04,
        }
        d = decide(
            rows,
            'nemotron',
            {'nemotron': summary(0.04)},
            [],
            [r],
            policy(),
            NOW,
            activity=activity,
            trial=t,
        )
        self.assertEqual(d['target'], 'gemma')
        self.assertEqual(d['explorationTrigger'], 'spike_return')
        self.assertTrue(d['escapeReady'])
        rows[0]['selected'] = False
        d = decide(
            rows,
            'nemotron',
            {'nemotron': summary(0.04)},
            [],
            [r],
            policy(),
            NOW,
            activity=activity,
            trial=t,
        )
        # A finished trial whose previous model can't return resumes normal checks instead of holding forever.
        self.assertIsNone(d['target'])
        self.assertIn('cannot currently return', d['reason'])
        self.assertEqual(d['spikeReview']['status'], 'keep')

    def test_running_spike_trial_cannot_be_preempted_by_another_experiment(self):
        r, t = experiment()
        t.update(complete=False, status='running')
        rows, _, activity = fixtures()
        rows[1]['earningsTarget'] = rows[0]['earningsTarget']
        d = decide(rows, 'nemotron', {}, [], [r], policy(), NOW, activity=activity, trial=t)
        self.assertIsNone(d['target'])
        self.assertEqual(d['spikeReview']['status'], 'measuring')


class SpikeStorageTests(unittest.TestCase):
    def test_restart_keeps_comparison_and_resolution_is_scoped_and_idempotent(self):
        h = History(':memory:')
        auto = DemandOptimizer(h, OptimizerStore(h))
        d = choose()
        run = auto.begin('a', 'd', d, NOW)
        auto.finish(run, 'a', 'd', NOW + 90, 'switched', 90, 'session')
        kept = {'runId': run, 'status': 'keep', 'at': NOW + 1500}
        auto.record_spike_review('wrong', 'd', kept)
        self.assertNotIn('spikeResolution', auto.runs('a', 'd', NOW + 2000)[0]['decision'])
        auto.record_spike_review('a', 'd', kept)
        auto.record_spike_review('a', 'd', {**kept, 'at': NOW + 1600})
        saved = DemandOptimizer(h, auto.store).runs('a', 'd', NOW + 2000)[0]['decision']
        self.assertEqual(saved['spikeResolution'], kept)
        self.assertEqual(saved['spikeTrial']['incumbent'], 'gemma')
        h.close()


class DemandContextTests(unittest.TestCase):
    def test_repeated_samples_future_invalid_capacity_and_missing_are_not_coverage(self):
        cutoff = int(NOW // 30) * 30
        row = {'at': cutoff - 30, 'active': 20, 'queued': 0, 'warm': 5}
        c = demand_context(
            [row] * 100 + [dict(row, at=NOW + 20), dict(row, at=cutoff - 60, warm=0)], NOW
        )
        self.assertTrue(c['fresh'])
        self.assertEqual(c['tails'][-1]['coverage'], 0.5)
        self.assertFalse(c['tails'][-1]['qualified'])
        self.assertIsNone(c['recent']['load'])

    def test_medians_resist_isolated_spikes_and_use_latest_sample_per_bucket(self):
        cutoff = int(NOW // 30) * 30
        rows = [
            {'at': at, 'active': 100, 'queued': 0, 'warm': 50}
            for at in range(cutoff - 2400, cutoff, 30)
        ]
        rows.append(dict(rows[-1], at=cutoff - 29, active=10000))
        c = demand_context(rows, NOW)
        self.assertEqual(c['tails'][-1]['load'], 100)
        self.assertEqual(c['windows'][-1]['pressure'], 2)
        self.assertEqual(c['recent']['coverage'], 1)


class SpikeControllerTests(unittest.TestCase):
    setUp = controller_tests.DemandControllerTests.setUp
    tearDown = controller_tests.DemandControllerTests.tearDown
    setup_demand = controller_tests.DemandControllerTests.setup_demand
    advance = controller_tests.DemandControllerTests.advance

    def test_background_spike_dispatch_does_not_wait_for_idle_or_browser(self):
        self.setup_demand()
        self.value.update(kind='explore', escapeReady=True, explorationTrigger='demand_spike')
        self.o.idle_since = None
        self.o.raw['inference_active'] = True
        self.o.switch = Mock()
        self.advance(0)
        self.o.switch.assert_called_once()

    def test_observe_never_dispatches_spike(self):
        self.setup_demand()
        self.value.update(kind='explore', escapeReady=True, explorationTrigger='demand_spike')
        self.o.state['mode'] = 'observe'
        self.o.switch = Mock()
        self.advance(0)
        self.o.switch.assert_not_called()

    def test_expired_spike_is_rechecked_before_command(self):
        self.setup_demand()
        self.o.state['pending'] = {'model': 'b', 'kind': 'demand', 'demandKind': 'explore'}
        self.value.update(kind='explore', escapeReady=False, explorationTrigger='demand_spike')
        self.o.command = Mock()
        self.o.switch('a', 'b', 'acct', self.live['device'])
        self.o.command.assert_not_called()

    def prepare_proof(self):
        self.setup_demand()
        self.o.state['pending'] = {'model': 'b', 'kind': 'demand'}
        self.o.live['pulse'] = {'status': 'paused'}
        self.proof = {
            'at': self.now,
            'target': 'b',
            'session': session_key(self.o.raw),
            'raw': copy.deepcopy(self.o.raw),
            'pulse': {'status': 'live', 'at': self.now},
            'providerSession': {'id': 67},
            'activity': {'fresh': True, 'observedAt': self.now, 'idleSeconds': 0},
        }
        self.o.demand_auto.evaluate = Mock(return_value=copy.deepcopy(self.value))

    def test_queuing_cannot_invalidate_its_own_just_verified_evidence(self):
        self.prepare_proof()
        Optimizer.demand_decision(self.o, self.now + 1, preflight=self.proof)
        call = self.o.demand_auto.evaluate.call_args.kwargs
        self.assertEqual(call['live']['pulse']['status'], 'live')
        self.assertEqual(call['live']['provider']['session'], {'id': 67})
        self.assertTrue(call['admission_activity']['fresh'])
        self.assertEqual(self.o.live['pulse']['status'], 'paused')
        Optimizer.demand_decision(self.o, self.now + 1)
        self.assertIsNone(self.o.demand_auto.evaluate.call_args.kwargs['admission_activity'])

    def test_expired_other_session_replaced_or_paused_proofs_are_not_reused(self):
        for mutation in (
            'expired',
            'session',
            'target',
            'paused',
            'manual',
            'counter_reset',
            'stale_activity',
        ):
            self.prepare_proof()
            if mutation == 'expired':
                self.proof['at'] -= 11
            if mutation == 'session':
                self.proof['session'] = 'other'
            if mutation == 'target':
                self.proof['target'] = 'other'
            if mutation == 'paused':
                self.o.state['mode'] = 'observe'
            if mutation == 'manual':
                self.o.state['requestedModel'] = 'c'
            if mutation == 'counter_reset':
                self.proof['raw']['stats']['requests_served'] += 100
            if mutation == 'stale_activity':
                self.proof['activity']['observedAt'] -= 16
            Optimizer.demand_decision(self.o, self.now, preflight=self.proof)
            with self.subTest(mutation=mutation):
                self.assertIsNone(
                    self.o.demand_auto.evaluate.call_args.kwargs['admission_activity']
                )


if __name__ == '__main__':
    unittest.main()
