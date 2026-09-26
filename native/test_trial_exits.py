"""Trial exits use known paid evidence without inheriting discovery-only gates."""

import unittest
from unittest.mock import Mock, patch
from demand_optimizer import decide, policy, sustained_demand, DemandOptimizer
from demand_spikes import review
from history import History
from optimizer_store import OptimizerStore
from test_demand_spikes import experiment, fixtures
from test_demand_optimizer import NOW, candidate, summary
import test_demand_controller as controller_fixtures


def positive(pressure=0.13):
    cutoff = int(NOW // 30) * 30
    samples = [
        {'at': at, 'active': pressure * 300, 'queued': 0, 'warm': 300}
        for at in range(cutoff - 600, cutoff, 30)
    ]
    return sustained_demand(samples, NOW), sustained_demand(samples, NOW, 0, 1, 1e-12)


def case():
    run, trial = experiment()
    trial.update(status='no_traffic', usdPerHour=0, requests=0, tokens=0)
    rows, _, activity = fixtures()
    normal, returning = positive()
    rows[0]['signal'].update(load=39, pressure=0.13, sustained=normal, returnDemand=returning)
    rows[1]['earningsTarget'] = {
        **rows[0]['earningsTarget'],
        'rate': 0,
        'fastRate': 0,
        'asOf': NOW - 120,
        'warmMinutes': 40,
        'livePaid': {'fresh': True, 'rate': 0},
    }
    activity.update(idleSeconds=1500)
    return run, trial, rows, activity


def choose(rows=None, trial=None, runs=None):
    run, t, r, a = case()
    return decide(
        rows or r,
        'nemotron',
        {'nemotron': {**summary(0), 'recent': True}, 'better': summary(0.5)},
        [],
        runs or [run],
        policy(),
        NOW,
        last_switch=NOW - 1500,
        activity=a,
        trial=trial or t,
    )


class TrialExitTests(unittest.TestCase):
    def test_known_paid_incumbent_returns_at_low_but_covered_positive_pressure(self):
        d = choose()
        self.assertEqual(d['target'], 'gemma')
        self.assertTrue(d['escapeReady'])
        self.assertEqual(d['explorationTrigger'], 'spike_return')
        self.assertEqual(d['kind'], 'explore')

    def test_missing_stale_and_zero_capacity_return_demand_stay_held(self):
        for change in ('stale', 'missing', 'no_demand', 'unselected', 'memory'):
            _, _, rows, _ = case()
            if change == 'stale':
                rows[0]['signal']['observedAt'] = NOW - 100
            if change == 'missing':
                rows[0]['signal']['returnDemand'] = {'qualified': False}
            if change == 'no_demand':
                rows[0]['signal'].update(load=0, pressure=0)
            if change == 'unselected':
                rows[0]['selected'] = False
            if change == 'memory':
                rows[0]['loadBudget'] = {'afterUnloadGB': 10, 'requiredGB': 30}
            with self.subTest(change=change):
                self.assertIsNone(choose(rows)['target'])
        cutoff = int(NOW // 30) * 30
        samples = [
            {'at': at, 'active': 39, 'queued': 0, 'warm': 0}
            for at in range(cutoff - 600, cutoff, 30)
        ]
        self.assertFalse(sustained_demand(samples, NOW, 0, 1, 1e-12)['qualified'])

    def test_return_positive_readings_must_cover_most_of_each_window(self):
        cutoff = int(NOW // 30) * 30
        samples = [
            {'at': at, 'active': 390 if at % 300 == 0 else 0, 'queued': 0, 'warm': 300}
            for at in range(cutoff - 600, cutoff, 30)
        ]
        self.assertFalse(sustained_demand(samples, NOW, 0, 1, 1e-12)['qualified'])

    def test_blocked_incumbent_does_not_hide_a_qualified_alternative(self):
        _, _, rows, _ = case()
        rows[0]['selected'] = False
        alternative = candidate('better')
        alternative['signal']['sustained'] = positive(2)[0]
        rows.append(alternative)
        d = choose(rows)
        self.assertEqual(d['target'], 'better')
        self.assertEqual(d['explorationTrigger'], 'trial_alternative')
        self.assertTrue(d['escapeReady'])
        self.assertIn('another qualified', d['reason'])

    def test_a_running_measurement_still_blocks_other_trials(self):
        _, trial, rows, _ = case()
        trial.update(complete=False, status='running', settled=False)
        rows.append(candidate('better'))
        self.assertIsNone(choose(rows, trial)['target'])

    def test_completed_trial_never_claims_early_or_partial_exit(self):
        run, t = experiment()
        paid = {'fastRate': 0, 'warmMinutes': 20, 'asOf': NOW - 120}
        from test_demand_spikes import SpikeReviewTests

        _, _, context, _ = SpikeReviewTests().faded_case()
        t['warmStartedAt'] = NOW - 1400
        d = review(
            [run], t, 'nemotron', {'fresh': True, 'rate': 0}, {'fresh': True}, NOW, paid, context
        )
        self.assertEqual(d['status'], 'return')
        self.assertNotIn('early', d['reason'])
        self.assertNotIn('partial', d['reason'])

    def test_recovery_of_paid_work_cancels_return(self):
        run, t, rows, a = case()
        t['usdPerHour'] = 0.2
        rows[1]['earningsTarget']['livePaid']['rate'] = 0.2
        d = decide(rows, 'nemotron', {}, [], [run], policy(), NOW, activity=a, trial=t)
        self.assertIsNone(d['target'])
        self.assertEqual(d['spikeReview']['status'], 'keep')

    def test_trial_admission_reserves_planned_exit_and_its_recovery(self):
        events = [
            {'at': NOW - 4000, 'kind': 'switched', 'model': 'nemotron', 'downtime': 20},
            {'at': NOW - 3500, 'kind': 'switched', 'model': 'gemma', 'downtime': 30},
        ]
        rows, rates, a = fixtures()
        rules = policy({'maxDowntimeMinutes': 15})
        for spent, expected in [(850, None), (830, 'nemotron')]:
            runs = [{'at': NOW - 2000, 'downtime': spent}]
            d = decide(rows, 'gemma', rates, events, runs, rules, NOW, activity=a)
            self.assertEqual(d['target'], expected)
            if expected:
                chosen = next(r for r in d['opportunities'] if r['model'] == expected)
                self.assertEqual(chosen['reservedSeconds'], 70)
                h = History(':memory:')
                auto = DemandOptimizer(h, OptimizerStore(h))
                saved = auto.begin('a', 'd', d, NOW)
                self.assertEqual(auto.runs('a', 'd', NOW)[0]['reservedSeconds'], 70)
                h.close()


class TrialDwellTests(unittest.TestCase):
    setUp = controller_fixtures.DemandControllerTests.setUp
    tearDown = controller_fixtures.DemandControllerTests.tearDown
    setup_demand = controller_fixtures.DemandControllerTests.setup_demand
    advance = controller_fixtures.DemandControllerTests.advance

    def test_completed_paid_upgrade_keeps_confirmation_but_not_extra_minimum_dwell(self):
        self.setup_demand()
        self.o.switch = Mock()
        self.o.state['lastSwitchAt'] = self.now
        self.value['completedTrialExit'] = True
        for offset in range(0, 241, 60):
            self.advance(offset)
        self.o.switch.assert_not_called()
        for offset in range(300, 481, 60):
            self.advance(offset)
        self.o.switch.assert_called_once()

    def test_lost_trial_exit_waiver_defers_before_command_and_consumes_no_attempt(self):
        self.setup_demand()
        self.o.state['lastSwitchAt'] = self.now - 1500
        self.value['completedTrialExit'] = True
        from demand_confirmation import advance

        proposal = None
        for offset in range(-300, 1, 60):
            proposal = advance(
                proposal,
                'b',
                self.source + offset,
                self.o.confirmation_scope(self.o.state, self.o.raw),
                self.now + offset,
                300,
            )
        self.o.state['demandProposal'] = proposal
        # Queue a genuinely confirmed early exit, then lose its paid-evidence
        # waiver before the worker's final evaluation. The target still wins.
        with patch('optimizer.threading.Thread'):
            self.advance(0)
        self.assertEqual(self.o.state['pending']['model'], 'b')
        self.value['completedTrialExit'] = False
        self.o.command = Mock()
        self.o.verify_started = Mock(return_value=True)
        with patch('optimizer.time.time', return_value=self.now):
            self.o.switch('a', 'b', 'acct', self.live['device'])
        self.o.command.assert_not_called()
        self.o.verify_started.assert_not_called()
        self.assertEqual(self.o.state['mode'], 'demand')
        self.assertNotIn('pending', self.o.state)
        self.assertIn('completed-trial exit', self.o.detail)
        self.assertEqual(self.o.demand_auto.runs('acct', self.live['device'], self.now + 1), [])

    def test_final_trial_exit_waiver_allows_early_command_when_still_supported(self):
        self.setup_demand()
        self.o.state['lastSwitchAt'] = self.now - 1500
        self.o.state['pending'] = {'model': 'b', 'kind': 'demand', 'demandKind': 'earnings'}
        self.value['completedTrialExit'] = True
        self.o.command = Mock()
        self.o.verify_started = Mock(return_value=True)
        with patch('optimizer.time.time', return_value=self.now):
            self.o.switch('a', 'b', 'acct', self.live['device'])
        self.o.command.assert_called_once()
        self.assertEqual(self.o.state['mode'], 'demand')

    def test_expired_minimum_run_does_not_need_a_trial_exit_waiver(self):
        self.setup_demand()
        self.o.state['lastSwitchAt'] = self.now - 1800
        self.o.state['pending'] = {'model': 'b', 'kind': 'demand', 'demandKind': 'earnings'}
        self.value['completedTrialExit'] = False
        self.o.command = Mock()
        self.o.verify_started = Mock(return_value=True)
        with patch('optimizer.time.time', return_value=self.now):
            self.o.switch('a', 'b', 'acct', self.live['device'])
        self.o.command.assert_called_once()
        self.assertEqual(self.o.state['mode'], 'demand')

    def test_same_model_option_edit_during_failure_prevents_restore(self):
        self.setup_demand()
        self.o.state['pending'] = {'model': 'b', 'kind': 'demand'}

        def verify(*args):
            self.o.read_options.return_value = ('b', ['--local-endpoint', '--port', '9000'], {})
            return False

        self.o.verify_started = Mock(side_effect=verify)
        self.o.command = Mock()
        self.o.switch('a', 'b', 'acct', self.live['device'])
        self.o.command.assert_called_once()
        self.assertEqual(self.o.state['mode'], 'observe')


if __name__ == '__main__':
    unittest.main()
