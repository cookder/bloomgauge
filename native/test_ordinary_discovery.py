"""Comparative admission, honest terminal outcomes, retry information, display evidence."""

import copy, json, unittest
from test_demand_optimizer import NOW, candidate, summary
from test_trial_economics import goal, ordinary_case
import test_trial_economics as economics
import test_live_earnings as pulse_tests
from demand_targets import fresh_paid
from trial_economics import comparison, REVISION
from trial_economics import discovery_retry, RETRY_SECONDS, ordinary_review, sampling_budget
from demand_optimizer import decide, policy


def setup(rate=0.03, forecast=False):
    rows = [candidate('current'), candidate('better')]
    rows[0]['earningsTarget'] = goal(rate)
    rows[1]['signal'].update(
        status='normal',
        sustained={'qualified': True, 'pressure': 2, 'sourceAt': NOW - 30},
        regime={'providerVersion': 'test', 'model': 'better'},
    )
    return rows, {
        'current': {**summary(rate), 'asOf': NOW - 120, 'recent': True},
        'better': {**summary(0.2), 'asOf': NOW - 300, 'forecastUsable': forecast},
    }


def choose(rows, rates, runs=(), trial=None):
    return decide(
        rows,
        'current',
        rates,
        [],
        list(runs),
        policy(),
        NOW,
        NOW - 3600,
        {'fresh': True, 'idleSeconds': 1300},
        trial,
    )


def prior():
    r, t = ordinary_case()
    r['model'] = 'better'
    r['decision']['candidate'] = {'signal': {'load': 10, 'pressure': 2}}
    r['decision']['economicTrial']['regime'] = {'providerVersion': 'test', 'model': 'better'}
    r['decision']['outcome'] = {
        'complete': True,
        'settled': True,
        'windowEnd': NOW - 180,
        'competitive': {'outcome': 'uncertain'},
    }
    r['decision']['trialResolution'] = {'status': 'inconclusive', 'at': NOW - 60}
    return r


class AdmissionTests(unittest.TestCase):
    def test_idle_needs_comparator_even_when_shortfall_not_ready(self):
        rows, rates = setup()
        rows[0]['earningsTarget']['ready'] = False
        self.assertEqual(choose(rows, rates)['target'], 'better')
        rows[0]['earningsTarget']['livePaid'] = {'fresh': False, 'rate': None}
        d = choose(rows, rates)
        self.assertIsNone(d['target'])
        self.assertIn(
            'frozen incumbent',
            next(r for r in d['opportunities'] if r['model'] == 'better')['reason'],
        )

    def test_covered_zero_valid_but_unknown_or_negative_not_manufactured(self):
        for rate in (0, 0.000001):
            rows, rates = setup(rate)
            self.assertEqual(choose(rows, rates)['target'], 'better')
        rows, rates = setup()
        rows[0]['earningsTarget'].update(rate=None, ready=False)
        self.assertIsNone(choose(rows, rates)['target'])
        rows, rates = setup(-0.01)
        self.assertIsNone(choose(rows, rates)['target'])

    def test_known_weak_candidate_rejected_in_idle_and_shortfall(self):
        for ready in (True, False):
            rows, rates = setup(0.05, True)
            rows[0]['earningsTarget']['ready'] = ready
            rates['better'] = {**summary(0.02), 'asOf': NOW - 300}
            d = choose(rows, rates)
            self.assertIsNone(d['target'])
            self.assertIn(
                'Matched paid history',
                next(r for r in d['opportunities'] if r['model'] == 'better')['reason'],
            )

    def test_paid_upgrade_exempt_from_discovery_retry(self):
        rows, rates = setup(forecast=True)
        d = choose(rows, rates, [prior()])
        self.assertEqual((d['target'], d['kind']), ('better', 'earnings'))

    def test_sparse_candidate_not_excluded_by_old_zero(self):
        rows, rates = setup()
        rates['better']['rate'] = 0
        self.assertEqual(choose(rows, rates)['target'], 'better')

    def test_recent_uncertain_trial_held_even_with_new_comparator(self):
        rows, rates = setup()
        d = choose(rows, rates, [prior()])
        self.assertIsNone(d['target'])
        self.assertIn(
            'recent inconclusive',
            next(r for r in d['opportunities'] if r['model'] == 'better')['reason'],
        )


class RetryTests(unittest.TestCase):
    def signal(self):
        return setup()[0][1]['signal']

    def test_expiry_uses_fixed_resolution_time(self):
        r = prior()
        d = discovery_retry([r], 'better', self.signal(), NOW)
        self.assertTrue(d['held'])
        self.assertEqual(d['until'], NOW - 60 + RETRY_SECONDS)
        self.assertFalse(discovery_retry([r], 'better', self.signal(), d['until'])['held'])

    def test_legacy_records_neutral_and_unchanged(self):
        r = prior()
        r['decision'].pop('economicTrial')
        before = copy.deepcopy(r)
        self.assertFalse(discovery_retry([r], 'better', self.signal(), NOW)['held'])
        self.assertEqual(r, before)

    def test_escape_needs_fresh_sustained_material_change(self):
        r = prior()
        signal = self.signal()
        signal.update(load=20, pressure=4)
        self.assertFalse(discovery_retry([r], 'better', signal, NOW)['held'])
        for change in (
            {'observedAt': NOW - 91},
            {'coverage': 0.7},
            {'pressure': 3.99},
            {'sustained': {'qualified': False}},
        ):
            self.assertTrue(discovery_retry([r], 'better', {**signal, **change}, NOW)['held'])
        signal = self.signal()
        signal['regime']['providerVersion'] = 'new'
        self.assertFalse(discovery_retry([r], 'better', signal, NOW)['held'])
        signal['regime']['providerVersion'] = None
        self.assertTrue(discovery_retry([r], 'better', signal, NOW)['held'])

    def test_changed_latest_context_does_not_hide_older_matching_trial(self):
        old = prior()
        new = copy.deepcopy(old)
        new['id'] = 7
        new['at'] += 50
        new['decision']['economicTrial']['regime']['providerVersion'] = 'other'
        self.assertTrue(discovery_retry([new, old], 'better', self.signal(), NOW)['held'])

    def test_loss_held_recovered_keep_exempt(self):
        r = prior()
        r['decision'].pop('trialResolution')
        r['decision']['outcome']['competitive']['outcome'] = 'loss'
        self.assertTrue(discovery_retry([r], 'better', self.signal(), NOW)['held'])
        r['decision']['outcome']['competitive']['outcome'] = 'uncertain'
        r['decision']['trialResolution'] = {'status': 'keep', 'at': NOW - 60}
        self.assertFalse(discovery_retry([r], 'better', self.signal(), NOW)['held'])

    def success(self):
        r = prior()
        r['id'] = 8
        r['at'] += 50
        r['decision']['trialResolution'] = {'status': 'keep', 'at': NOW - 30}
        r['decision']['outcome'].update(
            clock={'qualified': True},
            competitive={
                'outcome': 'win',
                'revision': REVISION,
                'comparison': {'qualified': True},
                'regime': copy.deepcopy(self.signal()['regime']),
            },
        )
        return r

    def test_newer_comparable_success_supersedes_older_weak_sample(self):
        d = discovery_retry([prior(), self.success()], 'better', self.signal(), NOW)
        self.assertFalse(d['held'])
        self.assertEqual(d['priorRunId'], 8)

    def test_older_success_does_not_hide_newer_inconclusive_sample(self):
        good = self.success()
        good['at'] = prior()['at'] - 50
        self.assertTrue(discovery_retry([good, prior()], 'better', self.signal(), NOW)['held'])

    def test_success_must_be_recent_qualified_and_comparable(self):
        for fault in (
            'partial',
            'provider',
            'regime',
            'clock',
            'comparison',
            'load',
            'old',
            'unsustained',
        ):
            good = self.success()
            sig = self.signal()
            o = good['decision']['outcome']
            if fault == 'partial':
                o['settled'] = False
            if fault == 'provider':
                o['competitive']['regime']['providerVersion'] = 'other'
            if fault == 'regime':
                o['competitive']['regime']['loadBucket'] = 'other'
            if fault == 'clock':
                o['clock']['qualified'] = False
            if fault == 'comparison':
                o['competitive']['comparison']['qualified'] = False
            if fault == 'load':
                good['decision']['candidate']['signal']['load'] = 1000
            if fault == 'old':
                good['decision']['trialResolution']['at'] = NOW - RETRY_SECONDS
            if fault == 'unsustained':
                sig['sustained']['qualified'] = False
            self.assertTrue(discovery_retry([good, prior()], 'better', sig, NOW)['held'], fault)


class CoveredIdleTests(unittest.TestCase):
    setUp = pulse_tests.PulseTests.setUp
    tearDown = pulse_tests.PulseTests.tearDown
    ingest = pulse_tests.PulseTests.ingest
    snapshot = pulse_tests.PulseTests.snapshot

    def test_real_empty_credit_polls_produce_qualified_zero_comparator(self):
        now = pulse_tests.T + 340
        self.raw.update(advertised_models=['a'], started_at=pulse_tests.T, written_at=now)
        self.session.update(providerStartedAt=pulse_tests.T, lastSeenAt=now)
        for at in range(pulse_tests.T + 20, now + 1, 20):
            self.ingest([], at)
            packet = self.snapshot(at)
        paid = fresh_paid(packet, self.session, self.raw, 'a', now)
        self.assertTrue(paid['fresh'])
        self.assertEqual((paid['rate'], paid['seconds']), (0, 300))
        g = goal(0)
        g.update(asOf=now - 120, livePaid=paid)
        self.assertTrue(
            comparison('a', g, {'recent': True}, now, {}, 'ordinary', 20)['comparison']['qualified']
        )
        for broken in (
            {**packet, 'updatedAt': now - 46},
            {**packet, 'windows': {'300': {**packet['windows']['300'], 'seconds': 100}}},
        ):
            p = fresh_paid(broken, self.session, self.raw, 'a', now)
            self.assertFalse(p['fresh'])
            self.assertIsNone(p['rate'])


class TerminalTests(unittest.TestCase):
    def test_unknown_before_deadline_waits_then_closes_without_command(self):
        r, t = ordinary_case()
        a = r['decision']['ordinaryTrial']
        a['comparison']['qualified'] = False
        a['referenceRate'] = None
        a['deadline'] = NOW + 1
        self.assertEqual(
            ordinary_review(r, t, goal(0.02), {'fresh': True}, NOW)['status'], 'waiting'
        )
        result = ordinary_review(r, t, goal(0.02), {'fresh': True}, NOW + 1)
        self.assertEqual(result['status'], 'inconclusive')
        self.assertTrue(result['allowPaidAlternative'])
        self.assertNotIn('target', result)

    def test_missing_or_unsettled_clock_outcome_at_deadline_is_inconclusive(self):
        for patch in ({'settled': False}, {'clock': {'qualified': False}}):
            r, t = ordinary_case()
            r['decision']['ordinaryTrial']['deadline'] = NOW
            t.update(patch)
            self.assertEqual(
                ordinary_review(r, t, goal(0.02), {'fresh': True}, NOW)['status'], 'inconclusive'
            )
        r, t = ordinary_case()
        r['decision']['ordinaryTrial']['deadline'] = NOW
        self.assertEqual(
            ordinary_review(r, t, goal(0.02), {'fresh': False}, NOW)['status'], 'inconclusive'
        )

    def test_fresh_productive_recovery_still_kept(self):
        r, t = ordinary_case()
        r['decision']['ordinaryTrial']['comparison']['qualified'] = False
        r['decision']['ordinaryTrial']['deadline'] = NOW
        self.assertEqual(ordinary_review(r, t, goal(0.12), {'fresh': True}, NOW)['status'], 'keep')

    def test_persisted_terminal_does_not_reopen_when_warm_coverage_is_incomplete(self):
        r, t = ordinary_case()
        r['model'] = 'current'
        t.update(model='current', complete=False, settled=False, status='running')
        r['decision']['trialResolution'] = {'status': 'inconclusive', 'at': NOW - 1}
        rows, rates = setup(forecast=True)
        d = choose(rows, rates, [r], t)
        self.assertIsNone(d['spikeReview'])
        self.assertEqual((d['target'], d['kind']), ('better', 'earnings'))

    def test_terminal_scan_only_permits_paid_alternative(self):
        r, t = ordinary_case()
        r['model'] = 'current'
        t['model'] = 'current'
        r['decision']['ordinaryTrial']['comparison']['qualified'] = False
        r['decision']['ordinaryTrial']['deadline'] = NOW
        rows, rates = setup(forecast=True)
        d = choose(rows, rates, [r], t)
        self.assertEqual(d['spikeReview']['status'], 'inconclusive')
        self.assertEqual((d['target'], d['kind']), ('better', 'earnings'))
        rates['better']['forecastUsable'] = False
        self.assertIsNone(choose(rows, rates, [r], t)['target'])


class DisplayTests(unittest.TestCase):
    def test_memory_block_shown_without_relaxing_it(self):
        rows, rates = setup(forecast=True)
        rows[1]['loadBudget']['afterUnloadGB'] = 30
        d = choose(rows, rates)
        self.assertIsNone(d['target'])
        a = d['paidAlternative']
        self.assertEqual(a['model'], 'better')
        self.assertFalse(a['eligible'])
        self.assertIn('memory', a['reason'])
        self.assertEqual(a['at'], NOW)
        self.assertEqual(set(a), {'model', 'eligible', 'reason', 'at'})

    def test_eligible_describes_confirmation_not_dispatch(self):
        rows, rates = setup(forecast=True)
        a = choose(rows, rates)['paidAlternative']
        self.assertTrue(a['eligible'])
        self.assertIn('confirmation', a['reason'])

    def test_stale_historical_or_unknown_evidence_has_no_alternative(self):
        for fault in ('historical', 'stale', 'missing_current'):
            rows, rates = setup(forecast=True)
            if fault == 'historical':
                rates['better']['forecastUsable'] = False
            if fault == 'stale':
                rows[1]['signal']['observedAt'] = NOW - 91
            if fault == 'missing_current':
                rows[0]['earningsTarget']['livePaid']['fresh'] = False
            self.assertIsNone(choose(rows, rates)['paidAlternative'])


class AtomicTests(unittest.TestCase):
    setUp = economics.PersistenceTests.setUp
    tearDown = economics.PersistenceTests.tearDown
    draft = economics.PersistenceTests.draft

    def test_begin_rejects_missing_comparison_without_inserting(self):
        d = self.draft()
        d['earningsTarget']['livePaid']['fresh'] = False
        with self.assertRaisesRegex(ValueError, 'qualified frozen'):
            self.auto.begin('a', 'd', d, NOW)
        self.assertEqual(self.auto.runs('a', 'd', NOW), [])

    def test_retry_rechecks_concurrently_recorded_result(self):
        d = self.draft()
        r = prior()
        r['decision']['economicTrial']['regime']['providerVersion'] = '0.9.8'
        self.h.db.execute(
            'INSERT INTO demand_switch_runs(account,device,at,model,payload,result,completed_at,downtime) VALUES(?,?,?,?,?,?,?,?)',
            ('a', 'd', r['at'], 'better', json.dumps(r['decision']), 'switched', r['at'] + 60, 60),
        )
        self.h.db.commit()
        with self.assertRaisesRegex(ValueError, 'Recent trial evidence'):
            self.auto.begin('a', 'd', d, NOW)

    def test_readonly_decision_does_not_persist_terminal_preview(self):
        id = self.auto.begin('a', 'd', self.draft(), NOW)
        self.auto.finish(id, 'a', 'd', NOW + 60, 'switched', 60, 'session')
        r = self.auto.runs('a', 'd', NOW)[0]
        r['model'] = 'current'
        r['decision']['ordinaryTrial']['comparison']['qualified'] = False
        r['decision']['ordinaryTrial']['deadline'] = NOW
        _, trial = ordinary_case()
        trial.update(model='current', runId=id)
        before = self.h.db.total_changes
        rows, rates = setup(forecast=True)
        result = choose(rows, rates, [r], trial)
        self.assertEqual(result['spikeReview']['status'], 'inconclusive')
        self.assertEqual(self.h.db.total_changes, before)
        self.assertNotIn('trialResolution', self.auto.runs('a', 'd', NOW)[0]['decision'])

    def test_terminal_persists_once_and_ends_budget_occupancy(self):
        id = self.auto.begin('a', 'd', self.draft(), NOW)
        self.auto.finish(id, 'a', 'd', NOW + 60, 'switched', 60, 'session')
        terminal = {
            'runId': id,
            'ordinary': True,
            'status': 'inconclusive',
            'reason': 'missing comparison',
            'at': NOW + 2500,
        }
        self.auto.record_spike_review('a', 'd', terminal)
        self.auto.record_spike_review('a', 'd', {**terminal, 'at': NOW + 3000})
        r = self.auto.runs('a', 'd', NOW + 4000)[0]
        self.assertEqual(r['decision']['trialResolution'], terminal)
        self.assertAlmostEqual(sampling_budget([r], NOW + 4000)['minutesUsed'], 2500 / 60)


if __name__ == '__main__':
    unittest.main()
