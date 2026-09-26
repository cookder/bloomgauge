"""Independent paid opportunities and bounded experiments with honest clock outcomes."""

import copy
import json
import unittest
from datetime import datetime
from history import History
from optimizer_store import OptimizerStore
from model_readiness import session_key
from demand_optimizer import DemandOptimizer, decide, policy, context_weight
from trial_economics import (
    REVISION,
    comparison,
    independent_paid,
    ordinary_review,
    clock_evidence,
    competitive,
    sampling_budget,
    session_span,
)
from test_demand_optimizer import NOW, candidate, summary

REGIME = {'providerVersion': '0.9.8', 'model': 'better'}


def goal(rate=0.03):
    return {
        'ready': True,
        'rate': rate,
        'fastRate': rate,
        'asOf': NOW - 120,
        'warmMinutes': 40,
        'usdPerHour': 0.12,
        'productiveFloor': 0.09,
        'livePaid': {'fresh': True, 'rate': rate},
        'highEarnings': {'active': False},
    }


def fixtures():
    rows = [candidate('current'), candidate('better')]
    rows[0]['earningsTarget'] = goal()
    rows[1]['signal'].update(
        status='normal',
        pressure=0.2,
        regime=REGIME,
        sustained={'qualified': False, 'pressure': 0.2},
    )
    rates = {
        'current': {**summary(0.03), 'asOf': NOW - 120, 'recent': True},
        'better': {**summary(0.2), 'asOf': NOW - 600},
    }
    return rows, rates


def choose(rows=None, rates=None, trial=None, runs=None):
    r, e = fixtures()
    return decide(
        rows or r,
        'current',
        rates or e,
        [],
        runs or [],
        policy(),
        NOW,
        NOW - 3600,
        {'fresh': True, 'idleSeconds': 0},
        trial,
    )


def ordinary_case(reference=0.06):
    anchor = comparison(
        'incumbent',
        {**goal(reference), 'asOf': NOW - 1920},
        summary(reference),
        NOW - 1800,
        REGIME,
        'earnings_target',
        20,
    )
    assert anchor['comparison']['qualified']
    anchor['sampling'] = True
    run = {
        'id': 4,
        'at': NOW - 1800,
        'model': 'better',
        'result': 'switched',
        'downtime': 60,
        'decision': {'ordinaryTrial': anchor, 'economicTrial': anchor},
    }
    trial = {
        'runId': 4,
        'model': 'better',
        'current': True,
        'complete': True,
        'settled': True,
        'status': 'productive',
        'trialMinutes': 20,
        'warmSeconds': 1200,
        'windowEnd': NOW - 180,
        'clock': {'qualified': True, 'usdPerHour': 0.02},
    }
    return run, trial


class IndependentPaidTests(unittest.TestCase):
    def test_shortfall_without_trial_uses_independent_earnings_path(self):
        d = choose()
        self.assertEqual(d['target'], 'better')
        self.assertEqual(d['kind'], 'earnings')
        self.assertFalse(d['escapeReady'])
        self.assertFalse(d['completedTrialExit'])

    def test_terminal_incomplete_trial_never_supplies_the_benchmark(self):
        trial = {
            'current': True,
            'complete': True,
            'settled': False,
            'status': 'insufficient_coverage',
            'usdPerHour': 999,
        }
        d = choose(trial=trial)
        self.assertEqual(d['kind'], 'earnings')
        self.assertEqual(d['target'], 'better')
        rows, rates = fixtures()
        rows[0]['earningsTarget']['asOf'] = NOW - 241
        self.assertIsNone(choose(rows, rates, trial)['target'])

    def test_active_or_settling_trial_retains_ownership(self):
        for status in ('running', 'settling'):
            with self.subTest(status=status):
                self.assertIsNone(
                    choose(
                        trial={
                            'current': True,
                            'complete': False,
                            'settled': False,
                            'status': status,
                            'trialMinutes': 20,
                        }
                    )['target']
                )

    def test_missing_stale_or_future_independent_inputs_stay_held(self):
        for field, value in [
            ('asOf', None),
            ('asOf', NOW + 1),
            ('asOf', NOW - 241),
            ('fastRate', None),
            ('warmMinutes', 3),
        ]:
            rows, rates = fixtures()
            rows[0]['earningsTarget'][field] = value
            with self.subTest(field=field, value=value):
                self.assertIsNone(choose(rows, rates)['target'])
        for field, value in [
            ('asOf', None),
            ('asOf', NOW + 1),
            ('asOf', NOW - 8 * 86400),
            ('forecastUsable', False),
        ]:
            rows, rates = fixtures()
            rates['better'][field] = value
            with self.subTest(candidate=field, value=value):
                self.assertIsNone(choose(rows, rates)['target'])

    def test_productive_recovery_and_safety_are_retained(self):
        rows, rates = fixtures()
        rows[0]['earningsTarget']['livePaid']['rate'] = 0.4
        self.assertIsNone(choose(rows, rates)['target'])
        rows, rates = fixtures()
        rows[1]['loadBudget']['afterUnloadGB'] = 30
        self.assertIsNone(choose(rows, rates)['target'])


class OrdinaryReviewTests(unittest.TestCase):
    def review(self, run=None, trial=None, paid=None, now=NOW):
        r, t = ordinary_case()
        return ordinary_review(run or r, trial or t, paid or goal(0.02), {'fresh': True}, now)

    def test_measure_then_guarded_return_against_saved_incumbent(self):
        r, t = ordinary_case()
        t.update(complete=False, settled=False, status='running')
        self.assertEqual(self.review(r, t)['status'], 'measuring')
        self.assertEqual(self.review()['status'], 'return')

    def test_expired_partial_trial_review_is_not_a_competitive_loss(self):
        r, t = ordinary_case()
        r['decision']['ordinaryTrial']['deadline'] = NOW - 1
        t.update(complete=False, settled=False, status='running', clock={})
        self.assertEqual(self.review(r, t)['status'], 'inconclusive')
        self.assertEqual(
            competitive(r['decision']['economicTrial'], {}, False)['outcome'], 'uncertain'
        )

    def test_fresh_and_settled_recovery_keeps_current_work(self):
        self.assertEqual(self.review(paid=goal(0.12))['status'], 'keep')
        g = goal(0.12)
        g['asOf'] = NOW - 500
        g['livePaid']['rate'] = 0.12
        self.assertNotEqual(self.review(paid=g)['status'], 'keep')

    def test_competitive_clock_outcome_not_warm_rate_drives_keep(self):
        r, t = ordinary_case()
        t['usdPerHour'] = 1
        t['clock']['usdPerHour'] = 0.02
        self.assertEqual(self.review(r, t, goal(0.04))['status'], 'return')
        t['clock']['usdPerHour'] = 0.061
        self.assertEqual(self.review(r, t, goal(0.061))['status'], 'keep')

    def test_missing_comparison_or_readings_names_hold(self):
        r, t = ordinary_case()
        r['decision']['ordinaryTrial']['comparison']['qualified'] = False
        result = self.review(r, t)
        self.assertEqual(result['status'], 'waiting')
        self.assertTrue(result['allowPaidAlternative'])
        g = goal()
        g['livePaid']['fresh'] = False
        self.assertEqual(self.review(paid=g)['status'], 'waiting')

    def test_saved_resolution_does_not_reopen(self):
        r, t = ordinary_case()
        r['decision']['trialResolution'] = {'status': 'keep', 'at': NOW - 100}
        self.assertIsNone(self.review(r, t))

    def test_return_requires_available_incumbent_or_independent_paid_alternative(self):
        r, t = ordinary_case()
        rows, rates = fixtures()
        rows[0]['id'] = 'incumbent'
        rows[0]['signal']['returnDemand'] = {'qualified': True}
        rows[0]['signal']['sustained'] = {'qualified': False}
        rows[1]['earningsTarget'] = goal(0.02)
        rates = {'better': summary(0.02)}
        d = decide(rows, 'better', rates, [], [r], policy(), NOW, NOW - 1800, {'fresh': True}, t)
        self.assertEqual(d['target'], 'incumbent')
        self.assertEqual(d['explorationTrigger'], 'ordinary_return')
        rows[0]['available'] = False
        self.assertIsNone(
            decide(rows, 'better', rates, [], [r], policy(), NOW, NOW - 1800, {'fresh': True}, t)[
                'target'
            ]
        )
        alternative = candidate('alternative')
        alternative['signal'].update(status='normal', pressure=0.1, sustained={'qualified': False})
        rows.append(alternative)
        rates['alternative'] = {**summary(0.3), 'asOf': NOW - 600}
        d = decide(rows, 'better', rates, [], [r], policy(), NOW, NOW - 1800, {'fresh': True}, t)
        self.assertEqual(d['target'], 'alternative')
        self.assertEqual(d['kind'], 'earnings')


class ClockEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.h = History(':memory:')
        self.store = OptimizerStore(self.h)
        self.raw = {
            'attestation_public_key': 'fixture',
            'pid': 9,
            'started_at': NOW + 30,
            'advertised_models': ['better'],
        }
        from optimizer_store import device_id

        self.device = device_id(self.raw)
        self.store.identity(self.device, 'p')
        self.run = {
            'id': 1,
            'at': NOW,
            'completedAt': NOW + 120,
            'model': 'better',
            'decision': {'providerSession': session_key(self.raw)},
        }
        self.h.db.execute(
            'CREATE TABLE provider_sessions(id INTEGER PRIMARY KEY,scope TEXT,data TEXT)'
        )
        self.session = {
            'models': ['better'],
            'providerStartedAt': NOW + 30,
            'startedAt': NOW + 30,
            'lastSeenAt': NOW + 1500,
            'endedAt': None,
            '_process': {'pid': 9},
        }
        self.save_session()
        self.h.db.execute('INSERT INTO opt_coverage VALUES(?,?,?)', ('a', NOW, NOW + 2000))
        self.credit(1, NOW + 150, 'better', 10000)
        self.credit(2, NOW + 151, 'better', -1000)
        self.credit(3, NOW + 155, 'base_reward', 999999)
        self.credit(4, NOW + 160, 'other', 999999)
        self.h.db.commit()

    def tearDown(self):
        self.h.close()

    def save_session(self):
        self.h.db.execute(
            'INSERT OR REPLACE INTO provider_sessions VALUES(1,?,?)',
            ('scope', json.dumps(self.session)),
        )

    def credit(self, id, at, model, value):
        self.h.db.execute(
            'INSERT INTO opt_credits VALUES(?,?,?,?,?,?,?)', ('a', id, 'p', at, model, value, 1)
        )

    def test_signed_actual_money_includes_startup_in_denominator_excludes_other_model_and_base(
        self,
    ):
        c = clock_evidence(self.store, 'a', self.device, self.run, NOW + 1200, NOW + 1500)
        self.assertTrue(c['qualified'])
        self.assertEqual(c['seconds'], 1200)
        self.assertAlmostEqual(c['usd'], 0.009)
        self.assertAlmostEqual(c['usdPerHour'], 0.027)
        self.assertTrue(c['paymentSeen'])

    def test_settlement_missing_coverage_and_other_device_stay_unknown(self):
        self.assertFalse(
            clock_evidence(self.store, 'a', self.device, self.run, NOW + 1450, NOW + 1500)[
                'qualified'
            ]
        )
        self.assertFalse(
            clock_evidence(self.store, 'a', 'foreign', self.run, NOW + 1200, NOW + 1500)[
                'qualified'
            ]
        )
        self.h.db.execute('UPDATE opt_coverage SET start=?', (NOW + 1,))
        c = clock_evidence(self.store, 'a', self.device, self.run, NOW + 1200, NOW + 1500)
        self.assertFalse(c['qualified'])
        self.assertIsNone(c['usdPerHour'])

    def test_manual_end_is_not_extended_to_next_automatic_run(self):
        self.session['endedAt'] = NOW + 300
        self.save_session()
        span = session_span(self.h, self.device, self.run, NOW + 5000)
        self.assertEqual(span['end'], NOW + 300)
        self.assertTrue(span['ended'])
        self.assertFalse(
            clock_evidence(self.store, 'a', self.device, self.run, NOW + 1200, NOW + 5000)[
                'qualified'
            ]
        )

    def test_positive_payment_is_factual_even_when_signed_net_is_negative(self):
        self.credit(5, NOW + 170, 'better', -20000)
        c = clock_evidence(self.store, 'a', self.device, self.run, NOW + 1200, NOW + 1500)
        self.assertLess(c['usd'], 0)
        self.assertTrue(c['paymentSeen'])


class CompetitiveAndBudgetTests(unittest.TestCase):
    def test_tiny_positive_payment_legacy_and_unmatched_evidence_are_neutral(self):
        r, t = ordinary_case()
        anchor = r['decision']['economicTrial']
        self.assertEqual(
            competitive(anchor, {'qualified': True, 'usdPerHour': 0.000003}, True)['outcome'],
            'loss',
        )
        self.assertEqual(
            competitive(None, {'qualified': True, 'usdPerHour': 0.3}, True)['outcome'], 'uncertain'
        )
        self.assertEqual(
            competitive(anchor, {'qualified': False, 'usdPerHour': 0.3}, True)['outcome'],
            'uncertain',
        )
        zero = comparison('incumbent', goal(0), summary(0), NOW, REGIME, 'idle', 20)
        self.assertEqual(
            competitive(zero, {'qualified': True, 'usdPerHour': 0.000003}, True)['outcome'],
            'uncertain',
        )

    def test_below_target_can_still_win_against_its_own_comparison(self):
        r, t = ordinary_case()
        anchor = r['decision']['economicTrial']
        self.assertEqual(
            competitive(anchor, {'qualified': True, 'usdPerHour': 0.08}, True)['outcome'], 'win'
        )

    def test_weight_needs_repeated_comparable_dates_and_matching_provider_regime(self):
        # Monday morning observations over two prior weekdays in the same week.
        now = datetime(2026, 9, 18, 12).timestamp()
        r, t = ordinary_case()
        anchor = r['decision']['economicTrial']
        outcome = {
            'complete': True,
            'settled': True,
            'status': 'productive',
            'pressure': 1,
            'load': 10,
            'competitive': competitive(anchor, {'qualified': True, 'usdPerHour': 0.001}, True),
        }
        runs = [
            {
                'at': now - d * 86400,
                'model': 'better',
                'decision': {'outcome': copy.deepcopy(outcome)},
            }
            for d in (1, 2, 3)
        ]
        self.assertLess(
            context_weight(runs, 'better', {'pressure': 1, 'load': 10, 'regime': REGIME}, now)[0], 1
        )
        self.assertEqual(
            context_weight(
                runs,
                'better',
                {'pressure': 1, 'load': 10, 'regime': {**REGIME, 'providerVersion': '0.9.9'}},
                now,
            ),
            (1, 0),
        )
        self.assertEqual(
            context_weight(runs, 'better', {'pressure': 4, 'load': 10, 'regime': REGIME}, now),
            (1, 0),
        )
        for r in runs:
            r['decision']['outcome'].pop('competitive')
        self.assertEqual(
            context_weight(runs, 'better', {'pressure': 1, 'load': 10, 'regime': REGIME}, now),
            (1, 0),
        )

    def test_budget_counts_elapsed_occupancy_not_just_switching_and_resolves_at_keep(self):
        r, t = ordinary_case()
        r['at'] = NOW - 7200
        r['decision']['trialResolution'] = {'at': NOW - 1800, 'status': 'keep'}
        b = sampling_budget([r], NOW, True)
        self.assertEqual(b['minutesUsed'], 90)
        self.assertFalse(b['qualified'])
        r['at'] = NOW - 90000
        r['decision']['trialResolution']['at'] = NOW - 85500
        self.assertEqual(sampling_budget([r], NOW)['minutesUsed'], 15)

    def test_a_later_switch_attempt_ends_the_trial_charge(self):
        r, t = ordinary_case()
        r['at'] = NOW - 36000
        r['decision']['outcome'] = {'occupancy': {'end': NOW, 'ended': True}}
        failed = {'at': NOW - 34920, 'model': 'b', 'result': 'failed', 'decision': {}}
        self.assertEqual(sampling_budget([r], NOW)['minutesUsed'], 600)
        self.assertEqual(sampling_budget([failed, r], NOW)['minutesUsed'], 18)

    def test_ended_session_stops_occupancy_and_return_does_not_consume_sampling(self):
        r, t = ordinary_case()
        r['decision']['outcome'] = {'occupancy': {'end': NOW - 1200, 'ended': True}}
        self.assertEqual(sampling_budget([r], NOW)['minutesUsed'], 10)
        r['decision']['economicTrial']['sampling'] = False
        self.assertEqual(sampling_budget([r], NOW)['minutesUsed'], 0)

    def test_budget_blocks_new_discovery_but_not_independent_paid_upgrade(self):
        r, t = ordinary_case()
        r['at'] = NOW - 7200
        r['decision']['trialResolution'] = {'at': NOW - 1200, 'status': 'keep'}
        d = choose(runs=[r])
        self.assertEqual(d['kind'], 'earnings')
        rows, rates = fixtures()
        rates['better']['forecastUsable'] = False
        rows[1]['signal']['sustained'] = {'qualified': True, 'pressure': 2}
        d = choose(rows, rates, runs=[r])
        self.assertIsNone(d['target'])
        self.assertIn(
            'learning time', next(x for x in d['opportunities'] if x['model'] == 'better')['reason']
        )


if __name__ == '__main__':
    unittest.main()


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.h = History(':memory:')
        self.store = OptimizerStore(self.h)
        self.auto = DemandOptimizer(self.h, self.store)

    def tearDown(self):
        self.h.close()

    def draft(self):
        rows, rates = fixtures()
        rates['better']['forecastUsable'] = False
        rows[1]['signal']['sustained'] = {'qualified': True, 'pressure': 2, 'sourceAt': NOW - 30}
        return choose(rows, rates)

    def test_begin_freezes_comparison_and_deadline_without_changing_controls(self):
        d = self.draft()
        id = self.auto.begin('a', 'd', d, NOW)
        r = self.auto.runs('a', 'd', NOW)[0]
        anchor = r['decision']['ordinaryTrial']
        self.assertTrue(anchor['comparison']['qualified'])
        self.assertEqual(anchor['referenceRate'], 0.03)
        self.assertEqual(anchor['deadline'], NOW + 2400)
        self.assertTrue(anchor['sampling'])
        d['earningsTarget']['rate'] = 999
        self.assertEqual(
            self.auto.runs('a', 'd', NOW)[0]['decision']['ordinaryTrial']['referenceRate'], 0.03
        )
        self.assertEqual(r['reservedSeconds'], 360)

    def test_final_budget_recheck_rejects_obsolete_preview(self):
        d = self.draft()
        r, t = ordinary_case()
        r['at'] = NOW - 7200
        r['decision']['trialResolution'] = {'at': NOW - 1200, 'status': 'keep'}
        self.h.db.execute(
            'INSERT INTO demand_switch_runs(account,device,at,model,payload,result,completed_at,downtime) VALUES(?,?,?,?,?,?,?,?)',
            ('a', 'd', r['at'], 'other', json.dumps(r['decision']), 'switched', r['at'] + 60, 60),
        )
        self.h.db.commit()
        with self.assertRaisesRegex(ValueError, 'elapsed sampling'):
            self.auto.begin('a', 'd', d, NOW)

    def test_resolution_persists_and_does_not_reopen(self):
        id = self.auto.begin('a', 'd', self.draft(), NOW)
        self.auto.finish(id, 'a', 'd', NOW + 60, 'switched', 60, 'session')
        review = {
            'runId': id,
            'ordinary': True,
            'status': 'keep',
            'reason': 'recovered',
            'at': NOW + 1500,
        }
        self.auto.record_spike_review('a', 'd', review)
        self.auto.record_spike_review('a', 'd', {**review, 'status': 'waiting', 'at': NOW + 1600})
        self.assertEqual(
            self.auto.runs('a', 'd', NOW + 1700)[0]['decision']['trialResolution'], review
        )

    def test_failed_return_does_not_claim_resolved_success(self):
        id = self.auto.begin('a', 'd', self.draft(), NOW)
        self.auto.finish(id, 'a', 'd', NOW + 60, 'switched', 60, 'session')
        d = choose()
        d['spikeReview'] = {
            'runId': id,
            'ordinary': True,
            'status': 'return',
            'incumbent': 'better',
            'at': NOW,
        }
        next_id = self.auto.begin('a', 'd', d, NOW)
        self.auto.finish(next_id, 'a', 'd', NOW + 120, 'failed', 120)
        self.assertNotIn(
            'trialResolution',
            next(r for r in self.auto.runs('a', 'd', NOW + 130) if r['id'] == id)['decision'],
        )
        next_id = self.auto.begin('a', 'd', d, NOW)
        self.auto.finish(next_id, 'a', 'd', NOW + 140, 'switched', 140, 'new-session')
        prior = next(r for r in self.auto.runs('a', 'd', NOW + 150) if r['id'] == id)
        self.assertEqual(prior['decision']['trialResolution']['status'], 'returned')

    def test_failed_attempt_charges_actual_completion_not_indefinite_occupancy(self):
        id = self.auto.begin('a', 'd', self.draft(), NOW)
        self.auto.finish(id, 'a', 'd', NOW + 60, 'failed', 60)
        budget = sampling_budget(self.auto.runs('a', 'd', NOW + 10000), NOW + 10000)
        self.assertEqual(budget['minutesUsed'], 1)

    def test_unknown_lifecycle_retains_forty_minute_reservation_and_is_labeled(self):
        r, t = ordinary_case()
        r['at'] = NOW - 4 * 3600
        b = sampling_budget([r], NOW)
        self.assertEqual(b['minutesUsed'], 40)
        self.assertEqual(b['unknownLifecycles'], 1)
