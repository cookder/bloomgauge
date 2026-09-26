"""Thin history, genuine idle detection and persisted, settled trial outcomes."""

import unittest
from history import History
from optimizer_store import OptimizerStore, device_id
from model_readiness import session_key
from demand_optimizer import (
    DemandOptimizer,
    decide,
    estimate,
    policy,
    sustained_demand,
    context_weight,
)
from test_demand_optimizer import NOW, candidate, summary, minutes


def observations(pressure=1, now=NOW):
    return [
        {'at': now - 600 + i * 30, 'active': pressure * 10, 'queued': 0, 'warm': 10}
        for i in range(20)
    ]


def exploring(idle=1200, evidence=None, extra=None, runs=None, activity=None, trial=None):
    signals = sustained_demand(observations(), NOW)
    current = candidate('gemma')
    # Ordinary comparative discovery now needs an explicit covered zero benchmark.
    current['earningsTarget'] = {
        'ready': False,
        'rate': 0,
        'fastRate': 0,
        'asOf': NOW - 120,
        'warmMinutes': 30,
        'usdPerHour': 0.12,
        'productiveFloor': 0.09,
        'livePaid': {'fresh': True, 'rate': 0},
        'highEarnings': {'active': False},
    }
    row = {
        **candidate('nemotron'),
        'evidence': evidence or {},
        'signal': {**candidate('nemotron')['signal'], 'sustained': signals},
    }
    return decide(
        [current, row] + (extra or []),
        'gemma',
        {'gemma': summary(0.15), 'nemotron': {**summary(0), 'forecastUsable': False}},
        [],
        runs or [],
        policy(),
        NOW,
        NOW - 1200,
        activity or {'fresh': True, 'idleSeconds': idle, 'idleSince': NOW - idle},
        trial,
    )


class TrialDecisionTests(unittest.TestCase):
    def test_zero_history_is_uncertainty_not_disqualification(self):
        for evidence in (
            {},
            {'hours': 1, 'jobs': 0, 'eligible': False},
            {'hours': 1, 'jobs': 1, 'usd': 0.001},
        ):
            d = exploring(evidence=evidence)
            self.assertEqual(d['target'], 'nemotron')
            self.assertTrue(d['escapeReady'])
            self.assertEqual(d['kind'], 'explore')
            row = next(r for r in d['opportunities'] if r['model'] == 'nemotron')
            self.assertIsNone(row['netGainUsd'])
            self.assertIsNone(row['switchCostUsd'])

    def test_ten_minutes_scans_twenty_minutes_allows_escape(self):
        self.assertIsNone(exploring(599)['target'])
        self.assertEqual(exploring(600)['target'], 'nemotron')
        self.assertFalse(exploring(1199)['escapeReady'])
        self.assertTrue(exploring(1200)['escapeReady'])
        self.assertFalse(exploring(activity={'fresh': False, 'idleSeconds': 99999})['escapeReady'])

    def test_old_lucrative_rate_cannot_outvote_sustained_pressure_in_trial(self):
        old = {
            **candidate('old-winner'),
            'signal': {
                **candidate('old-winner')['signal'],
                'pressure': 0.05,
                'sustained': sustained_demand(observations(0.05), NOW),
            },
        }
        self.assertEqual(exploring(extra=[old])['target'], 'nemotron')

    def test_single_spike_duplicate_missing_cold_or_future_samples_do_not_qualify(self):
        for samples in (
            [observations()[0]] * 20,
            observations()[0:12],
            [{**r, 'warm': 0} for r in observations()],
            observations(0)[:-1] + [{'at': NOW - 30, 'active': 1e6, 'queued': 0, 'warm': 1}],
            observations(now=NOW + 1200),
        ):
            self.assertFalse(sustained_demand(samples, NOW)['qualified'])
        self.assertTrue(sustained_demand(observations(), NOW)['qualified'])
        self.assertFalse(sustained_demand(observations(), NOW + 91)['qualified'])

    def test_learning_network_baseline_still_allows_absolute_pressure_trial(self):
        extra = {
            **candidate('fresh-model'),
            'signal': {
                **candidate('fresh-model')['signal'],
                'status': 'learning',
                'pressureRatio': None,
                'sustained': sustained_demand(observations(3), NOW),
            },
        }
        self.assertEqual(exploring(extra=[extra])['target'], 'fresh-model')

    def test_trials_obey_attempt_and_downtime_limits(self):
        self.assertIsNone(
            exploring(
                runs=[{'at': NOW - i, 'downtime': 60} for i in range(policy()['maxSwitchesPerDay'])]
            )['target']
        )
        self.assertIsNone(exploring(runs=[{'at': NOW - 30, 'downtime': 1700}])['target'])

    def test_cooldown_is_bounded_and_sparse_zeros_do_not_change_rank(self):
        run = {
            'at': NOW - 3600,
            'model': 'nemotron',
            'downtime': 60,
            'decision': {
                'outcome': {
                    'complete': True,
                    'pressure': 1,
                    'status': 'no_traffic',
                    'cooldownUntil': NOW + 3600,
                }
            },
        }
        self.assertIsNone(exploring(runs=[run])['target'])
        run['decision']['outcome']['cooldownUntil'] = NOW - 1
        self.assertEqual(exploring(runs=[run])['target'], 'nemotron')
        self.assertEqual(context_weight([run], 'nemotron', {'pressure': 1}, NOW)[0], 1)

    def test_trial_gets_warm_window_then_reassesses_unpaid_work(self):
        trial = {'current': True, 'status': 'running', 'trialMinutes': 20, 'complete': False}
        self.assertIsNone(exploring(trial=trial)['target'])
        trial.update(status='no_paid_work', complete=True, settled=True)
        self.assertTrue(exploring(idle=120, trial=trial)['escapeReady'])
        self.assertEqual(exploring(idle=0, trial=trial)['target'], 'nemotron')
        self.assertIsNone(
            exploring(idle=0, trial=trial, activity={'fresh': False, 'idleSeconds': 0})['target']
        )

    def test_failed_trial_escape_needs_current_known_zero_pay(self):
        from demand_targets import target_status

        trial = {
            'current': True,
            'status': 'no_traffic',
            'complete': True,
            'settled': True,
            'trialMinutes': 20,
        }
        other = {
            **candidate('other'),
            'signal': {
                **candidate('other')['signal'],
                'sustained': sustained_demand(observations(), NOW),
            },
        }

        def choose(paid, chosen_trial=trial, recent=None):
            current = {
                **candidate('gemma'),
                'earningsTarget': target_status({}, None, NOW, policy(), live_paid=paid),
            }
            current['earningsTarget'].update(rate=0, fastRate=0, warmMinutes=30, asOf=NOW - 120)
            return decide(
                [current, other],
                'gemma',
                {
                    'gemma': {**summary(0.08), 'recentGuardRate': recent},
                    'other': {**summary(0), 'forecastUsable': False},
                },
                [],
                [],
                policy(),
                NOW,
                NOW - 3600,
                {'fresh': True, 'idleSeconds': 0, 'busy': True},
                chosen_trial,
            )

        for paid in (
            None,
            {},
            {'fresh': False, 'rate': 0},
            {'fresh': True, 'rate': None},
            {'fresh': True, 'rate': float('nan')},
            {'fresh': True, 'rate': True},
            {'fresh': True, 'rate': 0.001},
            {'fresh': True, 'rate': -0.01},
        ):
            with self.subTest(paid=paid):
                self.assertIsNone(choose(paid)['target'])
        admitted = choose({'fresh': True, 'rate': 0})
        self.assertEqual(admitted['target'], 'other')
        self.assertTrue(admitted['escapeReady'])
        self.assertEqual(admitted['explorationTrigger'], 'failed_trial')
        for changed in ({'settled': False}, {'complete': False}, {'current': False}):
            self.assertIsNone(choose({'fresh': True, 'rate': 0}, {**trial, **changed})['target'])
        self.assertIsNone(choose({'fresh': True, 'rate': 0}, recent=0.001)['target'])

    def test_broad_or_short_history_is_not_a_forecast(self):
        for hours, days in ((1, 1), (19, 2), (60, 5)):
            e = estimate(
                {'hours': hours, 'days': days, 'jobs': 1000, 'minutes': minutes(0.5, NOW - 86400)},
                {},
                {'pressure': 0.05, 'load': 1},
                NOW,
            )
            self.assertFalse(e['forecastUsable'])
            self.assertAlmostEqual(e['rate'], 0.5)

    def test_repeated_paid_days_with_matching_pressure_support_forecast(self):
        rows = sum([minutes(0.15, NOW - d * 86400 - 3600, 480) for d in (7, 14, 21)], [])
        net = {r['at']: {'pressure': 1, 'load': 10, 'warm': 10} for r in rows}
        data = {'hours': 24, 'days': 3, 'jobs': 500, 'minutes': rows}
        self.assertTrue(estimate(data, net, {'pressure': 1, 'load': 10}, NOW)['forecastUsable'])
        self.assertFalse(estimate(data, net, {'pressure': 0.05, 'load': 1}, NOW)['forecastUsable'])

    def test_sparse_old_current_rate_cannot_make_a_forecast_switch_look_profitable(self):
        d = decide(
            [candidate('a'), candidate('b')],
            'a',
            {'a': {**summary(0), 'forecastUsable': False}, 'b': summary(0.5)},
            [],
            [],
            policy(),
            NOW,
        )
        self.assertIsNone(d['target'])
        d = decide(
            [candidate('a'), candidate('b')],
            'a',
            {'a': {**summary(0), 'forecastUsable': False, 'recentGuardRate': 0}, 'b': summary(0.5)},
            [],
            [],
            policy(),
            NOW,
        )
        self.assertEqual(d['target'], 'b')


class TrialObservationTests(unittest.TestCase):
    def setUp(self):
        self.h = History(':memory:')
        self.store = OptimizerStore(self.h)
        self.auto = DemandOptimizer(self.h, self.store)
        self.t = self.auto.trials
        self.raw = {
            'pid': 12,
            'started_at': NOW - 600,
            'advertised_models': ['nemotron'],
            'warm_models': ['nemotron'],
            'attestation_public_key': 'test-device',
            'inference_active': False,
            'written_at': NOW,
            'stats': {'requests_served': 1, 'tokens_generated': 4},
        }
        self.device = device_id(self.raw)
        self.store.identity(self.device, 'provider')

    def tearDown(self):
        self.h.close()

    def record(self, at, ready=True):
        self.raw['written_at'] = at
        self.t.observe('a', self.device, self.raw, at, ready, NOW - 1)

    def activity(self, at=NOW):
        return self.t.activity('a', self.device, self.raw, at)

    def warm(self, start, end):
        for at in range(start, end + 1, 3):
            self.record(at)
        with self.h.lock:
            self.h.db.executemany(
                'INSERT OR REPLACE INTO opt_ready_minutes VALUES(?,?,?,?,?,?,?,?)',
                [('a', self.device, m, 'nemotron', 60, 0, 0, 0) for m in range(start, end, 60)],
            )
            self.h.db.execute(
                'INSERT OR REPLACE INTO opt_coverage VALUES(?,?,?)', ('a', start, end)
            )
            self.h.db.commit()

    def trial_run(self):
        d = exploring()
        run_id = self.auto.begin('a', self.device, d, NOW)
        self.auto.finish(run_id, 'a', self.device, NOW, 'switched', 0, session_key(self.raw))
        return self.auto.runs('a', self.device, NOW)[0]

    def test_fresh_continuous_idle_and_duplicate_source(self):
        self.warm(int(NOW), int(NOW + 1200))
        self.assertEqual(self.activity(NOW + 1200)['idleSeconds'], 1200)
        self.record(NOW + 1200)
        self.assertEqual(self.activity(NOW + 1200)['idleSeconds'], 1200)
        self.assertFalse(self.activity(NOW + 1220)['fresh'])

    def test_work_and_changed_counters_cannot_look_idle(self):
        self.record(NOW)
        self.record(NOW + 3)
        self.raw['stats']['tokens_generated'] += 1
        self.assertFalse(self.activity(NOW + 3)['fresh'])
        self.record(NOW + 6)
        self.assertEqual(self.activity(NOW + 6)['idleSeconds'], 0)
        self.record(NOW + 9)
        self.assertEqual(self.activity(NOW + 9)['idleSeconds'], 3)
        self.raw['inference_active'] = True
        self.record(NOW + 12)
        self.assertEqual(self.activity(NOW + 12)['idleSeconds'], 0)

    def test_long_gaps_cold_new_session_account_and_restart_never_bridge(self):
        self.record(NOW)
        self.record(NOW + 3)
        self.record(NOW + 45)
        self.assertEqual(self.activity(NOW + 45)['idleSeconds'], 0)
        self.record(NOW + 48, False)
        self.record(NOW + 51)
        self.assertEqual(self.activity(NOW + 51)['idleSeconds'], 0)
        self.raw['pid'] = 20
        self.record(NOW + 54)
        self.assertEqual(self.activity(NOW + 54)['idleSeconds'], 0)
        self.record(NOW + 57)
        self.assertFalse(self.t.activity('other', self.device, self.raw, NOW + 57)['fresh'])
        self.assertFalse(
            DemandOptimizer(self.h, self.store).trials.activity(
                'a', self.device, self.raw, NOW + 57
            )['fresh']
        )

    def test_a_late_reading_within_the_same_session_stays_continuous(self):
        # Readings are ~15 s apart but can run late; counters are cumulative, so no work hides in the gap.
        self.record(NOW)
        self.record(NOW + 27)
        self.assertEqual(self.activity(NOW + 27)['idleSeconds'], 27)

    def test_begin_rechecks_idle_window(self):
        with self.assertRaisesRegex(ValueError, 'window'):
            self.auto.begin('a', self.device, exploring(600), NOW)

    def test_twenty_warm_minutes_wait_for_settlement_and_persist_zero(self):
        run = self.trial_run()
        self.warm(int(NOW), int(NOW + 1200))
        self.assertEqual(
            self.t.outcome('a', self.device, run, self.raw, NOW + 1200)['status'], 'settling'
        )
        o = self.t.outcome('a', self.device, run, self.raw, NOW + 1320)
        self.assertEqual(o['status'], 'no_traffic')
        self.assertEqual(o['usd'], 0)
        self.t.review('a', self.device, [run], self.raw, NOW + 1320)
        saved = DemandOptimizer(self.h, self.store).runs('a', self.device, NOW + 1400)[0]
        self.assertEqual(saved['decision']['outcome']['warmSeconds'], 1200)
        self.assertGreater(saved['decision']['outcome']['cooldownUntil'], NOW + 1320)

    def test_late_paid_work_corrects_saved_zero(self):
        run = self.trial_run()
        self.warm(int(NOW), int(NOW + 1200))
        self.t.review('a', self.device, [run], self.raw, NOW + 1320)
        with self.h.lock:
            self.h.db.execute(
                'INSERT INTO opt_credits VALUES(?,?,?,?,?,?,?)',
                ('a', 1, 'provider', NOW + 10, 'nemotron', 10000, 5),
            )
            self.h.db.commit()
        self.t.review(
            'a', self.device, self.auto.runs('a', self.device, NOW + 1500), self.raw, NOW + 1500
        )
        o = self.auto.runs('a', self.device, NOW + 1500)[0]['decision']['outcome']
        self.assertEqual(o['status'], 'productive')
        self.assertEqual(o['usd'], 0.01)
        self.assertIsNone(o['cooldownUntil'])

    def test_first_traffic_warm_time_and_scope(self):
        run = self.trial_run()
        self.record(NOW)
        self.record(NOW + 3)
        self.raw['stats']['tokens_generated'] += 10
        self.record(NOW + 6)
        o = self.t.outcome('a', self.device, run, self.raw, NOW + 6)
        self.assertEqual(o['firstTrafficSeconds'], 6)
        self.assertEqual(o['tokens'], 10)
        self.assertEqual(
            self.t.outcome('other', self.device, run, self.raw, NOW + 6)['warmSeconds'], 0
        )
        self.raw['pid'] = 25
        self.assertEqual(
            self.t.outcome('a', self.device, run, self.raw, NOW + 9)['status'], 'interrupted'
        )

    def test_partial_trial_closes_without_zero_inference_and_late_credits_correct_it(self):
        run = self.trial_run()
        self.warm(int(NOW), int(NOW + 1200))
        self.h.db.execute(
            'DELETE FROM opt_ready_minutes WHERE at>=? AND at<?', (NOW + 300, NOW + 600)
        )
        self.h.db.commit()
        self.assertEqual(
            self.t.outcome('a', self.device, run, self.raw, NOW + 1499)['status'], 'settling'
        )
        o = self.t.outcome('a', self.device, run, self.raw, NOW + 1500)
        self.assertEqual(o['status'], 'insufficient_coverage')
        self.assertTrue(o['complete'])
        self.assertTrue(o['partial'])
        self.assertFalse(o['settled'])
        self.assertEqual(o['coveragePercent'], 75)
        self.assertIsNone(o['cooldownUntil'])
        self.warm(int(NOW + 300), int(NOW + 600))
        self.h.db.execute(
            'INSERT INTO opt_credits VALUES(?,?,?,?,?,?,?)',
            ('a', 99, 'provider', NOW + 400, 'nemotron', 5000, 10),
        )
        self.h.db.commit()
        o = self.t.outcome('a', self.device, run, self.raw, NOW + 1600)
        self.assertEqual(o['status'], 'productive')
        self.assertAlmostEqual(o['usd'], 0.005)
        self.assertFalse(o['partial'])

    def test_missing_coverage_never_becomes_negative_context_weight(self):
        runs = [
            {
                'at': NOW - d * 86400,
                'model': 'x',
                'decision': {
                    'outcome': {'complete': True, 'pressure': 1, 'status': 'insufficient_coverage'}
                },
            }
            for d in (1, 2, 3)
        ]
        self.assertEqual(context_weight(runs, 'x', {'pressure': 1}, NOW), (1, 0))

    def test_live_counters_can_advance_during_history_read_in_either_order(self):
        self.record(NOW)
        self.record(NOW + 3)
        import copy

        old = copy.deepcopy(self.raw)
        self.raw['stats']['tokens_generated'] += 10
        self.raw['written_at'] = NOW + 6
        a = self.activity(NOW + 6)
        self.assertTrue(a['fresh'])
        self.assertTrue(a['busy'])
        self.assertEqual(a['idleSeconds'], 0)
        self.record(NOW + 6)
        a = self.t.activity('a', self.device, old, NOW + 6)
        self.assertTrue(a['fresh'])
        self.assertEqual(a['idleSeconds'], 0)
        self.raw['stats']['tokens_generated'] = 0
        self.raw['written_at'] = NOW + 9
        self.assertFalse(self.activity(NOW + 9)['fresh'])

    def test_idle_interval_crossing_switch_completion_still_completes_trial(self):
        run = self.trial_run()
        run['completedAt'] = NOW + 1
        self.warm(int(NOW), int(NOW + 1203))
        o = self.t.outcome('a', self.device, run, self.raw, NOW + 1330)
        self.assertEqual(o['warmSeconds'], 1200)
        self.assertEqual(o['warmStartedAt'], NOW + 1)
        self.assertEqual(o['status'], 'no_traffic')

    def test_work_segment_crossing_completion_is_not_allocated_to_trial(self):
        run = self.trial_run()
        run['completedAt'] = NOW + 1
        self.record(NOW)
        self.raw['stats']['requests_served'] += 1
        self.record(NOW + 3)
        self.record(NOW + 6)
        o = self.t.outcome('a', self.device, run, self.raw, NOW + 6)
        self.assertEqual(o['warmSeconds'], 3)
        self.assertEqual(o['requests'], 0)

    def test_advancing_idle_interval_does_not_erase_completed_trial(self):
        run = self.trial_run()
        self.warm(int(NOW), int(NOW + 1323))
        o = self.t.outcome('a', self.device, run, self.raw, NOW + 1320)
        self.assertEqual(o['warmSeconds'], 1200)
        self.assertEqual(o['windowEnd'], NOW + 1200)
        self.assertEqual(o['status'], 'no_traffic')
        self.assertTrue(o['settled'])

    def test_advancing_idle_interval_does_not_borrow_future_warm_time(self):
        run = self.trial_run()
        self.warm(int(NOW), int(NOW + 1323))
        o = self.t.outcome('a', self.device, run, self.raw, NOW + 1197)
        self.assertEqual(o['warmSeconds'], 1197)
        self.assertEqual(o['windowEnd'], NOW + 1197)
        self.assertEqual(o['status'], 'running')
        self.assertFalse(o['complete'])

    def test_work_segment_crossing_decision_time_is_not_prorated(self):
        run = self.trial_run()
        self.record(NOW)
        self.record(NOW + 3)
        self.raw['stats']['requests_served'] += 1
        self.record(NOW + 6)
        o = self.t.outcome('a', self.device, run, self.raw, NOW + 5)
        self.assertEqual(o['warmSeconds'], 3)
        self.assertEqual(o['requests'], 0)
        self.assertIsNone(o['firstTrafficSeconds'])

    def test_idle_interval_starting_at_decision_time_is_not_counted(self):
        run = self.trial_run()
        self.record(NOW + 3)
        self.record(NOW + 6)
        o = self.t.outcome('a', self.device, run, self.raw, NOW + 3)
        self.assertEqual(o['warmSeconds'], 0)
        self.assertFalse(o['complete'])


if __name__ == '__main__':
    unittest.main()
