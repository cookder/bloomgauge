import copy
import unittest
from unittest.mock import Mock

from demand_targets import high_earnings, target_status, fresh_paid
from demand_optimizer import decide, policy
from demand_spikes import review
from model_combinations import selection_key
from optimizer import session_key
from test_demand_spikes import fixtures, experiment
from test_demand_optimizer import NOW, summary
from test_productive_switching import live
import test_demand_controller as controllers


def paid(rate=0.25):
    return {'fresh': True, 'rate': rate, 'seconds': 300, 'asOf': NOW - 5}


def evidence(rates=(0.25, 0.25, 0.25)):
    end = int((NOW - 120) // 60) * 60
    return {
        'minutes': [
            {'at': end - 900 + i * 60, 'seconds': 60, 'usd': rates[i // 5] / 60} for i in range(15)
        ]
    }


class HighEarningsTests(unittest.TestCase):
    def test_three_paid_windows_hold_at_threshold_without_rounding_surprises(self):
        for rate in (0.2, 0.21, 0.5):
            value = high_earnings(evidence((rate,) * 3), NOW - 3600, NOW, paid(rate))
            self.assertTrue(value['active'])
            self.assertEqual(value['coveredMinutes'], 15)

    def test_one_burst_or_recent_decline_does_not_mean_consistently_high(self):
        for rates in ((0.1, 0.1, 0.5), (0.25, 0.19, 0.25), (0.25, 0.25, 0.199)):
            self.assertFalse(high_earnings(evidence(rates), NOW - 3600, NOW, paid(0.25))['active'])
        self.assertFalse(high_earnings(evidence(), NOW - 3600, NOW, paid(0.15))['active'])

    def test_unknown_live_tail_does_not_cancel_supported_hold(self):
        self.assertTrue(high_earnings(evidence(), NOW - 3600, NOW, {'fresh': False})['active'])

    def test_gaps_duplicates_cold_and_previous_sessions_do_not_invent_coverage(self):
        e = evidence()
        e['minutes'] = e['minutes'][2:]
        self.assertFalse(high_earnings(e, NOW - 3600, NOW, paid())['active'])
        e['minutes'] += copy.deepcopy(e['minutes'])
        self.assertFalse(high_earnings(e, NOW - 3600, NOW, paid())['active'])
        e = evidence()
        e['minutes'][0]['seconds'] = 30
        e['minutes'][1]['seconds'] = 30
        self.assertFalse(high_earnings(e, NOW - 3600, NOW, paid())['active'])
        self.assertFalse(high_earnings(evidence(), NOW - 500, NOW, paid())['active'])
        self.assertFalse(high_earnings(evidence(), None, NOW, paid())['active'])

    def test_signed_adjustments_count_against_high_earnings(self):
        e = evidence()
        e['minutes'][-1]['usd'] = -0.01
        self.assertFalse(high_earnings(e, NOW - 3600, NOW, paid())['active'])

    def test_one_missing_minute_per_window_remains_explicitly_covered(self):
        e = evidence()
        e['minutes'] = [m for i, m in enumerate(e['minutes']) if i % 5 != 0]
        v = high_earnings(e, NOW - 3600, NOW, paid())
        self.assertTrue(v['active'])
        self.assertEqual(v['coveredMinutes'], 12)

    def decide(self, rate=0.25, forecast=None):
        rows, rates, activity = fixtures()
        rows[0]['earningsTarget'] = target_status(
            evidence((rate,) * 3), NOW - 3600, NOW, policy(), live_paid=paid(rate)
        )
        # Existing measured baseline is separate from the short high-rate hold.
        rates['gemma'] = {**summary(rate), 'recent': True}
        if forecast:
            rates['nemotron'] = summary(forecast)
        return decide(rows, 'gemma', rates, [], [], policy(), NOW, activity=activity)

    def test_exceptional_spike_cannot_displace_high_earner(self):
        d = self.decide()
        self.assertIsNone(d['target'])
        self.assertIn('$0.20', d['reason'])
        self.assertIsNone(d['explorationTrigger'])
        row = next(r for r in d['opportunities'] if r['model'] == 'nemotron')
        self.assertFalse(row['eligible'])
        self.assertTrue(row['spike']['qualified'])
        self.assertIn('protect level', row['reason'])

    def test_strong_paid_upgrade_remains_available(self):
        d = self.decide(forecast=0.6)
        self.assertEqual(d['target'], 'nemotron')
        self.assertEqual(d['kind'], 'earnings')

    def test_sub_twenty_cent_productive_model_still_allows_spike_trial(self):
        rows, rates, activity = fixtures()
        rows[0]['earningsTarget']['highEarnings'] = high_earnings(
            evidence((0.15,) * 3), NOW - 3600, NOW, paid(0.15)
        )
        d = decide(rows, 'gemma', rates, [], [], policy(), NOW, activity=activity)
        self.assertEqual(d['explorationTrigger'], 'demand_spike')

    def test_completed_high_paid_trial_stays_even_if_saved_incumbent_was_higher(self):
        r, t = experiment()
        r['decision']['spikeTrial']['referenceRate'] = 0.8
        t.update(usdPerHour=0.25)
        goal = {'highEarnings': high_earnings(evidence(), NOW - 3600, NOW, paid())}
        v = review([r], t, 'nemotron', paid(), {'fresh': True}, NOW, goal)
        self.assertEqual(v['status'], 'keep')
        self.assertIn('$0.20', v['reason'])

    def test_pair_paid_proof_requires_the_exact_ready_pair(self):
        p, s, r = live(0.25)
        p['models'] = ['b', 'a']
        s['models'] = ['a', 'b']
        r['advertised_models'] = ['b', 'a']
        pair = selection_key(['a', 'b'])
        self.assertTrue(fresh_paid(p, s, r, pair, NOW)['fresh'])
        p['models'] = ['a']
        self.assertFalse(fresh_paid(p, s, r, pair, NOW)['fresh'])


class ScheduledHoldTests(unittest.TestCase):
    setUp = controllers.DemandControllerTests.setUp
    tearDown = controllers.DemandControllerTests.tearDown
    setup_demand = controllers.DemandControllerTests.setup_demand
    advance = controllers.DemandControllerTests.advance

    def test_week_and_pair_rotations_pause_without_resetting_the_plan(self):
        for mode in ('week', 'combo'):
            self.setup_demand()
            self.o.state.update(mode=mode, startedAt=self.now - 86400, endsAt=self.now + 86400)
            self.o.scheduled_trial_hold = Mock(
                return_value={'active': True, 'reason': 'High earnings hold'}
            )
            self.o.switch = Mock()
            before = copy.deepcopy(self.o.state)
            self.advance(0)
            self.o.switch.assert_not_called()
            self.assertEqual(self.o.state, before)
            self.assertIn('High earnings', self.o.detail)

    def test_queued_scheduled_rotation_rechecks_hold_before_command(self):
        self.setup_demand()
        self.o.state.update(
            mode='week', pending={'model': 'b', 'kind': 'automatic', 'automaticMode': 'week'}
        )
        self.o.scheduled_trial_hold = Mock(
            return_value={'active': True, 'reason': 'High earnings hold'}
        )
        self.o.command = Mock()
        self.o.switch('a', 'b', 'acct', self.live['device'])
        self.o.command.assert_not_called()
        self.assertEqual(self.o.state['mode'], 'week')
        self.assertIn('High earnings', self.o.detail)

    def test_manual_choice_bypasses_automatic_trial_hold(self):
        self.setup_demand()
        self.o.state.update(
            mode='observe',
            requestedModel='b',
            requestedKind='manual',
            pending={'model': 'b', 'kind': 'manual', 'session': session_key(self.o.raw)},
        )
        self.o.scheduled_trial_hold = Mock(
            return_value={'active': True, 'reason': 'High earnings hold'}
        )
        self.o.command = Mock()
        self.o.verify_started = Mock(return_value=True)
        self.o.switch('a', 'b', 'acct', self.live['device'])
        self.o.scheduled_trial_hold.assert_not_called()
        self.o.command.assert_called_once()


if __name__ == '__main__':
    unittest.main()
