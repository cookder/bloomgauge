"""Timed data gathering: larger learning allowances, unchanged protections."""

import unittest
from unittest.mock import Mock

import data_gathering
import test_optimizer as fixtures
from demand_optimizer import DemandOptimizer
from history import History
from optimizer_store import OptimizerStore
from baseline_learning import opportunity
from demand_optimizer import decide, policy
from test_baseline_learning import options
from test_demand_optimizer import NOW
from test_demand_targets import goal
from trial_economics import sampling_budget

ACTIVE = {'active': True, 'endsAt': NOW + 3600}


def learning(model, at):
    return {
        'model': model,
        'at': at,
        'downtime': 30,
        'decision': {'explorationTrigger': 'baseline_learning'},
    }


def choose(rate=0.1, runs=None, gathering=None, activity=None, rules=None):
    rules = rules or policy({'protectUsdPerHour': 0.09})
    rows = options()
    rows[0]['earningsTarget'] = goal(rate, rules=rules)
    if gathering and gathering.get('active'):
        rules = data_gathering.relaxed(rules)
    return decide(
        rows,
        'a',
        {},
        [],
        runs or [],
        rules,
        NOW,
        NOW - 7200,
        activity or {'fresh': True, 'idleSeconds': 0},
        None,
        gathering,
    )


class WindowTests(unittest.TestCase):
    def test_only_offered_durations_start(self):
        for seconds in data_gathering.DURATIONS:
            saved = data_gathering.start(seconds, NOW)
            self.assertTrue(data_gathering.status(saved, NOW)['active'])
        for seconds in (0, 3600, 86400.0, '86400', True, None):
            with self.assertRaises(ValueError):
                data_gathering.start(seconds, NOW)

    def test_window_ends_on_time_and_malformed_state_is_off(self):
        saved = data_gathering.start(86400, NOW)
        self.assertEqual(data_gathering.status(saved, NOW + 86399)['remainingSeconds'], 1)
        self.assertFalse(data_gathering.status(saved, NOW + 86400)['active'])
        self.assertFalse(data_gathering.status(saved, NOW - 1)['active'])
        for bad in (
            None,
            [],
            {},
            {**saved, 'endsAt': NOW + 10**9},
            {**saved, 'seconds': 60},
            {**saved, 'startedAt': float('nan')},
        ):
            self.assertFalse(data_gathering.status(bad, NOW)['active'])

    def test_relaxed_rules_still_validate_and_never_lower_user_limits(self):
        relaxed = policy(data_gathering.relaxed(policy()))
        self.assertEqual((relaxed['maxSwitchesPerDay'], relaxed['maxDowntimeMinutes']), (24, 60))
        self.assertEqual(relaxed['baselineLearningEnabled'], 1)
        self.assertEqual(
            data_gathering.relaxed(policy({'maxDowntimeMinutes': 60}))['maxDowntimeMinutes'], 60
        )


class AllowanceTests(unittest.TestCase):
    evidence = {'needsSamples': True, 'reason': 'Missing samples'}
    signal = {'learningDemand': {'qualified': True}}

    def qualified(self, runs, gathering=None):
        limits = data_gathering.learning_limits(gathering)
        return opportunity(self.evidence, self.signal, runs, 'b', NOW, 1, limits)['qualified']

    def test_learning_time_not_a_run_count_is_the_limit(self):
        self.assertTrue(self.qualified([learning('x', NOW - 300), learning('y', NOW - 200)]))
        self.assertTrue(
            self.qualified([learning(m, NOW - 300 - i) for i, m in enumerate('xyzuvwpq')], ACTIVE)
        )
        self.assertNotIn(
            'trialLimit',
            opportunity(
                self.evidence, self.signal, [], 'b', NOW, 1, data_gathering.learning_limits(None)
            ),
        )

    def test_same_model_again_after_four_hours_with_or_without_boost(self):
        for gathering in (None, ACTIVE):
            with self.subTest(gathering=gathering):
                self.assertTrue(self.qualified([learning('b', NOW - 5 * 3600)], gathering))
                self.assertFalse(self.qualified([learning('b', NOW - 3 * 3600)], gathering))

    def test_boost_raises_learning_time_to_at_least_three_hours(self):
        for minutes, boosted in ((0, 180), (120, 180), (300, 300)):
            rules = policy({'learningMinutesPerDay': minutes})
            self.assertEqual(
                data_gathering.learning_limits(None, rules)['samplingMinutes'], minutes
            )
            self.assertEqual(
                data_gathering.learning_limits(ACTIVE, rules)['samplingMinutes'], boosted
            )

    def test_sampling_allowance_grows_only_while_gathering(self):
        run = {
            'at': NOW - 6000,
            'decision': {'economicTrial': {'sampling': True}, 'trialResolution': {'at': NOW - 600}},
        }
        self.assertFalse(sampling_budget([run], NOW, True)['qualified'])
        self.assertTrue(
            sampling_budget([run], NOW, True, data_gathering.SAMPLING_MINUTES)['qualified']
        )


class DecisionTests(unittest.TestCase):
    def test_boost_starts_learning_without_waiting_for_shortfall_evidence(self):
        # Only 25 minutes of this session: too early for an ordinary learning run.
        rules = policy()

        def early(gathering=None):
            rows = options()
            rows[0]['earningsTarget'] = goal(0.1, since=NOW - 1500, rules=rules)
            r = data_gathering.relaxed(rules) if gathering and gathering.get('active') else rules
            return decide(
                rows,
                'a',
                {},
                [],
                [],
                r,
                NOW,
                NOW - 1500,
                {'fresh': True, 'idleSeconds': 0},
                None,
                gathering,
            )

        self.assertIsNone(early()['target'])
        d = early(ACTIVE)
        self.assertEqual((d['target'], d['explorationTrigger']), ('b', 'baseline_learning'))
        self.assertTrue(d['escapeReady'])
        self.assertIn('Learning boost', d['reason'])
        self.assertNotIn('trialLimit', d['baselineLearning'])
        self.assertEqual(d['limits']['sampling']['minutesLimit'], 180)

    def test_high_earnings_hold_still_wins(self):
        self.assertIsNone(choose(rate=0.21, gathering=ACTIVE)['target'])

    def test_stale_activity_or_inactive_window_does_not_gather(self):
        self.assertIsNone(
            choose(gathering=ACTIVE, activity={'fresh': False, 'idleSeconds': 0})['target']
        )
        self.assertIsNone(choose(gathering={'active': False})['target'])

    def test_memory_and_demand_guards_still_apply(self):
        rows = options()
        rows[0]['earningsTarget'] = goal(0.1)
        rows[1]['loadBudget'] = {'afterUnloadGB': 20, 'requiredGB': 30}
        d = decide(
            rows,
            'a',
            {},
            [],
            [],
            data_gathering.relaxed(policy()),
            NOW,
            NOW - 7200,
            {'fresh': True, 'idleSeconds': 0},
            None,
            ACTIVE,
        )
        self.assertIsNone(d['target'])
        rows = options()
        rows[0]['earningsTarget'] = goal(0.1)
        rows[1]['signal']['learningDemand'] = {'qualified': False}
        d = decide(
            rows,
            'a',
            {},
            [],
            [],
            data_gathering.relaxed(policy()),
            NOW,
            NOW - 7200,
            {'fresh': True, 'idleSeconds': 0},
            None,
            ACTIVE,
        )
        self.assertIsNone(d['target'])

    def test_gathering_can_turn_learning_on_but_not_bypass_used_learning_time(self):
        self.assertEqual(
            choose(gathering=ACTIVE, rules=policy({'baselineLearningEnabled': 0}))['target'], 'b'
        )
        below = policy()  # $0.20 protect level; the fixture pays $0.10
        self.assertEqual(
            choose(
                gathering=ACTIVE,
                rules=below,
                runs=[learning(m, NOW - 300 - i) for i, m in enumerate('xyzuvwpq')],
            )['target'],
            'b',
        )
        # 170 of the boost's 180 minutes are used; a run needs 40 free.
        used = {
            **learning('x', NOW - 10260),
            'result': 'switched',
            'decision': {
                'explorationTrigger': 'baseline_learning',
                'economicTrial': {'sampling': True},
                'trialResolution': {'at': NOW - 60},
            },
        }
        d = choose(gathering=ACTIVE, rules=below, runs=[used])
        self.assertIsNone(d['target'])
        self.assertTrue(
            d['reason'].startswith(
                "Paid pace is below your protect level. Today's learning time is used up (170 of 180 minutes"
            ),
            d['reason'],
        )
        # It frees up once the run is 140 minutes inside the rolling day, on the 5-minute grid.
        self.assertEqual(
            d['limits']['sampling']['availableAt'], -(-(NOW + 86400 - 8460) // 300) * 300
        )
        self.assertIn('The next learning run can start around', d['reason'])


class ControlTests(unittest.TestCase):
    setUp = fixtures.ControllerTests.setUp
    tearDown = fixtures.ControllerTests.tearDown

    def send(self, seconds):
        # Learning boost belongs to legacy demand following; the manager refuses it.
        self.o.state['demandPolicy'] = policy({'managerStrategy': 0})
        self.o.snapshot = Mock(return_value={})
        return self.o.set_data_gathering(
            {
                'action': 'data-gathering',
                'seconds': seconds,
                'expectedControl': self.o.control_version(),
            },
            'mac',
        )

    def test_start_and_stop_record_state_and_an_event_without_a_provider_command(self):
        version = self.o.control_version()
        self.send(259200)
        saved = self.o.state['dataGathering']
        self.assertEqual(saved['endsAt'] - saved['startedAt'], 259200)
        self.assertNotEqual(version, self.o.control_version())
        self.assertEqual(self.h.cache('optimizer-settings')['dataGathering'], saved)
        self.send(0)
        self.assertNotIn('dataGathering', self.o.state)
        kinds = [r[0] for r in self.h.db.execute('SELECT kind FROM opt_events ORDER BY id')]
        self.assertEqual(kinds, ['data-gathering', 'data-gathering'])
        self.o.runner.assert_not_called()

    def test_rejects_other_durations_stale_control_and_extra_fields(self):
        for seconds in (3600, '86400', None):
            with self.assertRaises(ValueError):
                self.send(seconds)
        with self.assertRaises(ValueError):
            self.send(0)
        with self.assertRaises(ValueError):
            self.o.set_data_gathering(
                {'action': 'data-gathering', 'seconds': 86400, 'expectedControl': 'old'}, 'mac'
            )
        with self.assertRaises(ValueError):
            self.o.set_data_gathering(
                {
                    'action': 'data-gathering',
                    'seconds': 86400,
                    'mode': 'demand',
                    'expectedControl': self.o.control_version(),
                },
                'mac',
            )
        self.assertNotIn('dataGathering', self.o.state)


class BeginTests(unittest.TestCase):
    def test_switch_refused_if_window_ended_after_the_decision(self):
        h = History(':memory:')
        self.addCleanup(h.close)
        auto = DemandOptimizer(h, OptimizerStore(h))
        chosen = {
            'model': 'b',
            'eligible': True,
            'outboundSeconds': 60,
            'returnSeconds': 60,
            'learningTrial': {'incumbent': 'a', 'referenceRate': 0.1},
        }
        decision = {
            'at': NOW,
            'target': 'b',
            'kind': 'explore',
            'escapeReady': True,
            'opportunities': [chosen],
            'baseline': None,
            'planningMinutes': 60,
            'currentModel': 'a',
            'policy': policy(),
            'explorationTrigger': 'baseline_learning',
            'dataGathering': {'active': True, 'endsAt': NOW + 5},
        }
        with self.assertRaisesRegex(ValueError, 'Learning boost ended'):
            auto.begin('acct', 'device', decision, NOW + 10)


if __name__ == '__main__':
    unittest.main()
