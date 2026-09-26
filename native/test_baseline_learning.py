import unittest
from history import History
from optimizer_store import OptimizerStore
from baseline_learning import baselines, measured_jobs, opportunity
from demand_optimizer import decide, policy, sustained_demand, DemandOptimizer
from demand_spikes import review
from test_demand_optimizer import candidate, NOW, summary
from test_demand_targets import goal
from test_optimizer import AT, entry
from model_readiness import session_key


def options():
    a, b = candidate('a'), candidate('b')
    a['earningsTarget'] = goal(0.04)
    b['paymentBaseline'] = {
        'needsSamples': True,
        'reason': 'Missing prompt samples',
        'priority': 3,
        'completeTokenSamples': 0,
        'jobMinutes': 0,
        'fresh': False,
    }
    b['signal'].update(
        status='normal',
        pressure=0.1,
        sustained={'qualified': False, 'sourceAt': NOW - 30},
        learningDemand={'qualified': True, 'sourceAt': NOW - 30, 'samples': 20},
    )
    return [a, b]


def choose(rows=None, runs=None, rules=None, trial=None, estimates=None):
    return decide(
        rows or options(),
        'a',
        estimates or {},
        [],
        runs or [],
        rules or policy(),
        NOW,
        NOW - 7200,
        {'fresh': True, 'idleSeconds': 0},
        trial,
    )


class LearningPolicyTests(unittest.TestCase):
    def test_low_paid_busy_work_can_fill_a_baseline_gap(self):
        d = choose()
        self.assertEqual(d['target'], 'b')
        self.assertTrue(d['escapeReady'])
        self.assertEqual(d['explorationTrigger'], 'baseline_learning')
        self.assertIn('50 complete', d['reason'])

    def test_good_paid_work_and_high_hold_are_preserved(self):
        protect = policy({'protectUsdPerHour': 0.09})
        for rate in (0.1, 0.21):
            rows = options()
            rows[0]['earningsTarget'] = goal(rate, rules=protect)
            self.assertIsNone(choose(rows, rules=protect)['target'])

    def test_disabled_or_complete_baseline_no_weak_demand_trial(self):
        self.assertIsNone(choose(rules=policy({'baselineLearningEnabled': 0}))['target'])
        rows = options()
        rows[1]['paymentBaseline']['needsSamples'] = False
        self.assertIsNone(choose(rows)['target'])

    def test_no_repeat_within_four_hours_and_reserved_return(self):
        trial = lambda model, at: {
            'model': model,
            'at': at,
            'downtime': 30,
            'decision': {'explorationTrigger': 'baseline_learning'},
        }
        for runs in (
            [trial('b', NOW - 200)],
            [trial('b', NOW - 4 * 3600 + 60)],
            [{'at': NOW - i, 'downtime': 1} for i in range(policy()['maxSwitchesPerDay'] - 1)],
        ):
            self.assertIsNone(choose(runs=runs)['target'])
        # Learning time, not a count of earlier runs, limits how many happen.
        self.assertEqual(
            choose(
                runs=[trial('x', NOW - 300), trial('y', NOW - 200), trial('b', NOW - 4 * 3600 - 60)]
            )['target'],
            'b',
        )

    def test_resource_freshness_selection_and_running_trial_guards(self):
        for change in (
            {'selected': False},
            {'available': False},
            {'loadBudget': None},
            {'loadBudget': {'afterUnloadGB': 20, 'requiredGB': 30}},
        ):
            rows = options()
            rows[1].update(change)
            self.assertIsNone(choose(rows)['target'])
        rows = options()
        rows[1]['signal']['observedAt'] = NOW - 91
        self.assertIsNone(choose(rows)['target'])
        self.assertIsNone(
            choose(trial={'current': True, 'status': 'running', 'trialMinutes': 20})['target']
        )

    def test_repeated_comparable_paid_upgrade_wins_over_learning(self):
        rows = options()
        c = candidate('c')
        c['signal']['sustained'] = {'qualified': True}
        rows.append(c)
        self.assertEqual(
            choose(
                rows,
                estimates={
                    'a': {**summary(0.04), 'recent': True, 'asOf': NOW - 120},
                    'c': {**summary(0.3), 'asOf': NOW - 300},
                },
            )['target'],
            'c',
        )
        rows = options()
        rows[1]['signal']['sustained'] = {'qualified': True}
        d = choose(
            rows,
            estimates={
                'a': {**summary(0.04), 'recent': True, 'asOf': NOW - 120},
                'b': {**summary(0.3), 'asOf': NOW - 300},
            },
        )
        self.assertEqual(d['target'], 'b')
        self.assertNotEqual(d['explorationTrigger'], 'baseline_learning')
        self.assertIsNone(next(r for r in d['opportunities'] if r['model'] == 'b')['learningTrial'])

    def test_two_covered_windows_need_actual_nonzero_demand(self):
        samples = [{'at': NOW - i * 30, 'active': 4, 'queued': 0, 'warm': 50} for i in range(1, 21)]
        self.assertTrue(sustained_demand(samples, NOW, 0.05, 3, 0.025)['qualified'])
        self.assertFalse(sustained_demand(samples, NOW)['qualified'])
        for rows in (
            samples[:6],
            [{**r, 'active': 0} for r in samples],
            [{**r, 'warm': 0} for r in samples],
        ):
            self.assertFalse(sustained_demand(rows, NOW, 0.05, 3, 0.025)['qualified'])

    def test_clock_expired_partial_learning_does_not_trap_other_opportunity_checks(self):
        rows = options()
        rows[1]['selected'] = False
        rows[0]['earningsTarget']['livePaid'] = {'fresh': True, 'rate': 0.02}
        run = {
            'id': 1,
            'model': 'a',
            'at': NOW - 2401,
            'downtime': 20,
            'decision': {'learningTrial': {'incumbent': 'b', 'referenceRate': 0.05}},
        }
        trial = {
            'runId': 1,
            'model': 'a',
            'current': True,
            'complete': False,
            'settled': False,
            'status': 'running',
            'warmSeconds': 600,
            'paidWarmSeconds': 300,
            'trialMinutes': 20,
        }
        result = choose(rows, runs=[run], trial=trial)
        self.assertIsNone(result['target'])
        self.assertEqual(result['spikeReview']['status'], 'keep')
        self.assertIn('Resume ordinary', result['spikeReview']['reason'])


class LearningEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.h = History(':memory:')
        self.store = OptimizerStore(self.h)
        self.store.identity('mac', 'p')
        self.h.db.execute('INSERT INTO opt_coverage VALUES(?,?,?)', ('acct', AT, AT + 7200))

    def tearDown(self):
        self.h.close()

    def add(self, model='a', device='mac', seconds=60):
        self.h.db.execute(
            'INSERT OR IGNORE INTO opt_ready_minutes VALUES(?,?,?,?,?,?,?,?)',
            ('acct', device, AT, model, seconds, 0, 0, 0),
        )

    def test_complete_exact_solo_settled_scope_only(self):
        self.add()
        self.add('b', seconds=30)
        self.add('c', device='other')
        data = []
        for i, (model, provider) in enumerate(
            [('a', 'p'), ('a', 'other'), ('b', 'p'), ('c', 'p'), ('base_reward', 'p')]
        ):
            r = entry(i + 1, AT + 10, model=model, provider=provider, amount=300)
            r.update(prompt_tokens=10, completion_tokens=20)
            data.append(r)
        self.store.credits('acct', data, AT + 7200)
        good = measured_jobs(self.store, 'acct', 'mac', AT, AT + 3600, AT + 7400)
        self.assertEqual(len(good), 1)
        self.assertEqual(good[0]['model'], 'a')
        self.assertEqual(measured_jobs(self.store, 'other', 'mac', AT, AT + 3600, AT + 7400), [])
        self.assertEqual(measured_jobs(self.store, 'acct', 'mac', AT, AT + 3600, AT + 179), [])
        self.assertEqual(measured_jobs(self.store, 'acct', 'mac', AT + 1, AT + 3600, AT + 7400), [])

    def test_no_legacy_or_empty_coverage_invention(self):
        self.add()
        self.store.credits('acct', [entry(1, AT + 10)], AT + 7200)
        self.assertEqual(measured_jobs(self.store, 'acct', 'mac', AT, AT + 3600, AT + 7400), [])
        b = baselines(self.store, 'acct', 'mac', ['a'], AT + 7400)['a']
        self.assertTrue(b['needsSamples'])
        self.assertIsNone(b['usdPerMillionTokens'])

    def test_committed_trial_allowance_rechecked(self):
        auto = DemandOptimizer(self.h, self.store)
        d = choose()
        first = auto.begin('acct', 'mac', d, NOW)
        auto.finish(first, 'acct', 'mac', NOW + 1, 'failed', 1)
        with self.assertRaisesRegex(ValueError, 'baseline-learning'):
            auto.begin('acct', 'mac', d, NOW + 2)

    def test_short_completed_learning_trial_can_keep_or_return(self):
        trial = {
            'current': True,
            'model': 'b',
            'runId': 1,
            'complete': True,
            'settled': True,
            'usdPerHour': 0.02,
            'warmSeconds': 660,
            'paidWarmSeconds': 600,
            'trialMinutes': 20,
            'windowEnd': NOW - 300,
        }
        run = {
            'id': 1,
            'at': NOW - 1500,
            'model': 'b',
            'decision': {'learningTrial': {'incumbent': 'a', 'referenceRate': 0.05}},
        }
        d = review([run], trial, 'b', {'fresh': True, 'rate': 0.02}, {'fresh': True}, NOW)
        self.assertEqual(d['status'], 'return')
        self.assertTrue(d['learning'])
        trial['usdPerHour'] = 0.06
        self.assertEqual(
            review([run], trial, 'b', {'fresh': True, 'rate': 0.06}, {'fresh': True}, NOW)[
                'status'
            ],
            'keep',
        )

    def test_sample_goal_ends_trial_without_waiting_twenty_minutes(self):
        auto = DemandOptimizer(self.h, self.store)
        raw = {'pid': 123, 'started_at': AT - 1, 'advertised_models': ['a']}
        session = session_key(raw)
        for i in range(20):
            self.h.db.execute(
                'INSERT INTO opt_ready_minutes VALUES(?,?,?,?,?,?,?,?)',
                ('acct', 'mac', AT + i * 60, 'a', 60, 5, 100, 1),
            )
            self.h.db.execute(
                'INSERT INTO demand_observed VALUES(?,?,?,?,?,?,?,?,?,?)',
                (
                    'acct',
                    'mac',
                    session,
                    'a',
                    AT + i * 60,
                    AT + (i + 1) * 60,
                    5,
                    100,
                    1,
                    AT + i * 60 + 1,
                ),
            )
        entries = []
        for i in range(50):
            r = entry(i + 1, AT + (i // 5) * 60 + 5 + i % 5, amount=100)
            r.update(prompt_tokens=10, completion_tokens=20)
            entries.append(r)
        self.store.credits('acct', entries, AT + 2000)
        run = {
            'id': 1,
            'model': 'a',
            'at': AT - 10,
            'completedAt': AT,
            'result': 'switched',
            'decision': {
                'kind': 'explore',
                'trialMinutes': 20,
                'providerSession': session,
                'learningTrial': {
                    'incumbent': 'b',
                    'referenceRate': 0.03,
                    'sampleGoal': 50,
                    'jobMinutesGoal': 10,
                },
            },
        }
        result = auto.trials.outcome('acct', 'mac', run, raw, AT + 1800)
        self.assertEqual(result['warmSeconds'], 600)
        self.assertEqual(result['learning']['completeTokenSamples'], 50)
        self.assertTrue(result['complete'])
        self.assertTrue(result['learning']['targetReached'])
        run['decision']['outcome'] = result
        self.assertEqual(
            auto.trials.outcome('acct', 'mac', run, raw, AT + 3600)['windowEnd'],
            result['windowEnd'],
        )


if __name__ == '__main__':
    unittest.main()
