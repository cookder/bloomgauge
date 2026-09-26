"""Current-source cadence, bounded confirmation gaps and unchanged dispatch guards."""

import copy, unittest
from unittest.mock import Mock, patch
from optimizer import Optimizer
import test_optimizer_live as live_fixture
import test_demand_alerts as alerts
import test_demand_controller as ctl
from test_ordinary_discovery import setup, choose
from demand_confirmation import advance, pause, scope, view

T = 10000


class ProgressTests(unittest.TestCase):
    def start(self):
        return advance(None, 'b', T - 30, 's', T, 300)

    def test_distinct_five_minutes(self):
        p = self.start()
        for dt in range(30, 300, 30):
            p = advance(p, 'b', T + dt - 30, 's', T + dt, 300)
        self.assertEqual((p['seconds'], p['status']), (270, 'confirming'))
        p = advance(p, 'b', T + 270, 's', T + 300, 300)
        self.assertEqual((p['seconds'], p['status']), (300, 'ready'))

    def test_duplicates_do_not_count_or_extend(self):
        p = self.start()
        q = advance(p, 'b', T - 30, 's', T + 59, 300)
        self.assertEqual((q['seconds'], q['samples'], q['expiresAt']), (0, 1, T + 150))
        self.assertIsNone(advance(p, 'b', T - 30, 's', T + 90, 300))

    def test_pause_resume_does_not_credit_gap(self):
        p = advance(self.start(), 'b', T + 30, 's', T + 60, 300)
        p = pause(p, 's', T + 75, 'missing')
        p = advance(p, 'b', T + 90, 's', T + 120, 300)
        self.assertEqual((p['seconds'], p['samples']), (60, 3))
        p = advance(p, 'b', T + 120, 's', T + 150, 300)
        self.assertEqual(p['seconds'], 90)

    def test_paused_duplicate_stays_paused(self):
        p = pause(self.start(), 's', T + 10, 'missing')
        q = advance(p, 'b', T - 30, 's', T + 20, 300)
        self.assertEqual((q['status'], q['seconds'], q['expiresAt']), ('paused', 0, T + 150))

    def test_long_gap_and_unobserved_bridge(self):
        p = advance(self.start(), 'b', T + 30, 's', T + 60, 300)
        self.assertIsNone(pause(p, 's', T + 211, 'missing'))
        self.assertEqual(advance(p, 'b', T + 181, 's', T + 211, 300)['seconds'], 0)
        self.assertEqual(advance(self.start(), 'b', T + 90, 's', T + 120, 300)['seconds'], 0)

    def test_delayed_tick_cannot_bridge_expired_prior_source(self):
        for age in (20, 50):
            with self.subTest(age=age):
                p = advance(None, 'b', T - age, 's', T, 300)
                q = advance(p, 'b', T + 75 - age, 's', T + 75, 300)
                self.assertEqual((q['seconds'], q['samples']), (0, 2))
                q = advance(q, 'b', T + 105 - age, 's', T + 105, 300)
                self.assertEqual(q['seconds'], 30)

    def test_changed_target_session_legacy(self):
        p = advance(self.start(), 'b', T + 30, 's', T + 60, 300)
        for model, session in [('c', 's'), ('b', 'new')]:
            self.assertEqual(advance(p, model, T + 60, session, T + 90, 300)['seconds'], 0)
        p.pop('revision')
        self.assertEqual(advance(p, 'b', T + 60, 's', T + 90, 300)['seconds'], 0)

    def test_regression_future_clock_rewind(self):
        p = self.start()
        self.assertIsNone(advance(p, 'b', T - 31, 's', T + 1, 300))
        self.assertIsNone(advance(p, 'b', T + 1, 's', T, 300))
        self.assertIsNone(pause(p, 's', T - 1, 'clock'))

    def test_memory_accumulates_and_ready_is_not_dispatch(self):
        p = self.start()
        for dt in range(60, 301, 60):
            p = advance(p, 'b', T + dt - 30, 's', T + dt, 300, 'Needs memory.')
        self.assertEqual((p['seconds'], p['status'], p['reason']), (300, 'ready', 'Needs memory.'))

    def test_plan_policy_mode_intent_scope_and_readonly_expiry(self):
        s = {
            'account': 'a',
            'device': 'd',
            'mode': 'demand',
            'models': ['a', 'b'],
            'demandPolicy': {'x': 1},
        }
        raw = {'pid': 1, 'started_at': T, 'advertised_models': ['a']}
        original = scope(s, raw, 'intent')
        p = advance(None, 'b', T - 30, original, T, 300)
        for patch in (
            {'models': ['a', 'c']},
            {'demandPolicy': {'x': 2}},
            {'mode': 'observe'},
            {'account': 'other'},
        ):
            self.assertIsNone(view(p, scope({**s, **patch}, raw, 'intent'), T + 1))
        self.assertIsNone(view(p, scope(s, raw, 'new-intent'), T + 1))
        self.assertIsNone(view(p, original, T + 151))
        before = copy.deepcopy(p)
        self.assertEqual(view(p, original, T + 100)['status'], 'paused')
        self.assertEqual(p, before)


class CadenceTests(unittest.TestCase):
    setUp = alerts.DemandAlertTests.setUp
    tearDown = alerts.DemandAlertTests.tearDown
    samples = alerts.DemandAlertTests.samples
    baseline = alerts.DemandAlertTests.baseline
    recent = alerts.DemandAlertTests.recent
    scan = alerts.DemandAlertTests.scan

    def test_phase_offsets_do_not_advance_alerts_or_heavy_history(self):
        for phase in (0, 7, 19, 29):
            with self.subTest(phase=phase):
                self.tearDown()
                self.setUp()
                self.baseline()
                at = alerts.NOW + phase
                self.recent(at - 30, load=2)
                first = self.scan(at)
                old = first['models'][0]['observedAt']
                later = old + 91
                self.assertLess(later - at, 60)
                self.recent(later, load=2)
                self.assertEqual(
                    self.scanner.snapshot('account', 'device', later)['models'][0]['status'],
                    'stale',
                )
                saved = copy.deepcopy(self.scanner.cached)
                writes = self.h.db.total_changes
                calls = self.store.evidence.call_count
                row = self.scanner.current('account', 'device', later)['models'][0]
                self.assertEqual(row['status'], 'normal')
                self.assertGreater(row['observedAt'], old)
                self.assertLess(later - row['observedAt'], 90)
                self.assertEqual(self.scanner.cached, saved)
                self.assertEqual(self.h.db.total_changes, writes)
                self.assertEqual(self.store.evidence.call_count, calls)

    def test_duplicate_missing_future_foreign_scope(self):
        self.baseline()
        self.recent(load=2)
        self.scan()
        a = self.scanner.current('account', 'device', alerts.NOW)
        self.assertEqual(
            a['models'][0]['observedAt'],
            self.scanner.current('account', 'device', alerts.NOW + 10)['models'][0]['observedAt'],
        )
        self.samples(alerts.NOW + 1000, 1)
        self.assertEqual(
            self.scanner.current('account', 'device', alerts.NOW + 90)['models'][0]['status'],
            'stale',
        )
        self.assertEqual(self.scanner.current('foreign', 'device', alerts.NOW)['models'], [])


class EconomicsTests(unittest.TestCase):
    def test_memory_only_candidate_and_small_deficit(self):
        rows, rates = setup(forecast=True)
        b = rows[1]['loadBudget']
        b['afterUnloadGB'] = b['requiredGB'] + 0.98
        d = choose(rows, rates)
        r = next(r for r in d['opportunities'] if r['model'] == 'better')
        self.assertIsNone(d['target'])
        self.assertTrue(r['confirmationEligible'])
        self.assertIn('MB more memory', r['reason'])
        self.assertNotIn('0.0', r['reason'])

    def test_file_cache_the_switch_purge_frees_counts_toward_memory(self):
        rows, rates = setup(forecast=True)
        b = rows[1]['loadBudget']
        b['afterUnloadGB'] = b['requiredGB'] - 1.9
        reason = lambda: (
            next(r for r in choose(rows, rates)['opportunities'] if r['model'] == 'better')[
                'reason'
            ]
            or ''
        )
        self.assertIn('2.9 GB more memory', reason())
        b['reclaimableGB'] = 1.0
        self.assertIn('1.9 GB more memory', reason())
        b['reclaimableGB'] = 5.3
        self.assertNotIn('more memory', reason())
        del b['reclaimableGB']
        b['cleanupCouldFreeGB'] = 5.3
        self.assertIn('2.9 GB more memory', reason())
        self.assertIn('Enable cache cleanup', reason())
        b['cleanupCouldFreeGB'] = 2.0
        self.assertNotIn('Enable cache cleanup', reason())

    def test_loss_hidden_behind_memory_cannot_qualify(self):
        rows, rates = setup(forecast=True)
        rows[1]['loadBudget']['afterUnloadGB'] = 1
        rates['better']['lower'] = 0
        self.assertFalse(
            next(r for r in choose(rows, rates)['opportunities'] if r['model'] == 'better')[
                'confirmationEligible'
            ]
        )


class ControllerTests(unittest.TestCase):
    setUp = ctl.DemandControllerTests.setUp
    tearDown = ctl.DemandControllerTests.tearDown
    setup_demand = ctl.DemandControllerTests.setup_demand
    advance = ctl.DemandControllerTests.advance

    def test_memory_jitter_retains_confirmation_without_dispatch(self):
        self.setup_demand()
        self.o.switch = Mock()
        self.advance(0)
        self.advance(60)
        r = next(r for r in self.value['opportunities'] if r['model'] == self.value['target'])
        self.value['target'] = None
        r.update(confirmationEligible=True, reason='Needs 40 MB more memory.')
        for dt in (120, 180, 240, 300):
            r['signal']['observedAt'] = self.now + dt - 30
            self.advance(dt)
        self.assertEqual(self.o.state['demandProposal']['status'], 'ready')
        self.o.switch.assert_not_called()
        self.value.update(target=r['model'], kind='earnings')
        r['reason'] = None
        self.advance(360)
        self.o.switch.assert_called_once()

    def test_missing_pause_no_gap_credit_and_reversal_clears(self):
        self.setup_demand()
        self.o.switch = Mock()
        self.advance(0)
        self.advance(60)
        r = next(r for r in self.value['opportunities'] if r['model'] == self.value['target'])
        self.value['target'] = None
        r.update(confirmationMissing=True, reason='Waiting for fresh demand.')
        r['signal']['observedAt'] = self.source
        self.advance(90, False)
        self.assertEqual(self.o.state['demandProposal']['status'], 'paused')
        self.value['target'] = r['model']
        self.advance(120)
        self.assertEqual(self.o.state['demandProposal']['seconds'], 60)
        self.value['target'] = None
        r['confirmationMissing'] = False
        self.advance(150)
        self.assertNotIn('demandProposal', self.o.state)

    def ready_queue(self):
        self.setup_demand()
        for dt in (0, 60, 120, 180, 240):
            self.advance(dt)
        with patch('optimizer.threading.Thread'):
            self.advance(300)
        self.assertEqual(self.o.state['demandProposal']['status'], 'ready')
        self.assertEqual(self.o.state['pending']['model'], 'b')
        self.o.command = Mock()
        self.o.verify_started = Mock(return_value=True)

    def finish_guarded(self):
        with patch('optimizer.time.time', return_value=self.now + 300):
            self.o.switch('a', 'b', 'acct', self.live['device'])
        self.o.command.assert_not_called()
        self.o.verify_started.assert_not_called()
        self.assertEqual(self.o.demand_auto.runs('acct', self.live['device'], self.now + 301), [])

    def test_final_memory_guard_after_ready(self):
        self.ready_queue()
        self.o.selection_budget = Mock(
            side_effect=[
                {'afterUnloadGB': 40, 'requiredGB': 30},
                {'afterUnloadGB': 30.99, 'requiredGB': 30},
            ]
        )
        self.finish_guarded()

    def test_final_session_guard_after_ready(self):
        self.ready_queue()
        raw = copy.deepcopy(self.o.raw)
        self.o.read_state.side_effect = [raw, {**raw, 'pid': 2}]
        self.finish_guarded()

    def test_final_readiness_guard_after_ready(self):
        self.ready_queue()
        raw = copy.deepcopy(self.o.raw)
        self.o.read_state.side_effect = [raw, {**raw, 'warm_models': []}]
        self.finish_guarded()

    def test_full_confirmation_uses_post_evaluation_current_state_without_writes(self):
        self.setup_demand()
        self.o.switch = Mock()
        self.advance(0)
        before = copy.deepcopy(self.o.state)
        proposal = advance(
            before['demandProposal'],
            'b',
            self.now,
            self.o.confirmation_scope(self.o.state, self.o.raw),
            self.now + 30,
            300,
        )

        def evaluate(*args, **kwargs):
            self.o.state['demandProposal'] = copy.deepcopy(proposal)
            self.o.status = 'waiting'
            self.o.detail = 'Waiting for fresh evidence.'
            self.o.record_decision(self.now + 30)
            return copy.deepcopy(self.value)

        self.o.demand_auto.evaluate = Mock(side_effect=evaluate)
        with patch('optimizer.time.time', return_value=self.now + 30):
            result = Optimizer.demand_decision(self.o, self.now, before, self.o.live, self.o.raw)
        self.assertEqual(result['confirmation'], proposal)
        self.assertEqual(result['execution']['current']['phase'], 'waiting')
        self.o.demand_auto.evaluate = Mock(return_value=copy.deepcopy(self.value))
        saved = copy.deepcopy(self.o.state)
        writes = self.h.db.total_changes
        with patch('optimizer.time.time', return_value=self.now + 30):
            Optimizer.demand_decision(self.o, self.now + 30)
        self.assertEqual(self.o.state, saved)
        self.assertEqual(self.h.db.total_changes, writes)


class LiveProjectionTests(unittest.TestCase):
    def setUp(self):
        self.o = live_fixture.fixture()
        self.now = live_fixture.NOW
        self.d = live_fixture.decision()
        self.d.update(target='b', kind='earnings')
        self.record()
        p = None
        for dt in range(-300, 1, 60):
            p = advance(
                p,
                'b',
                self.now + dt - 30,
                scope(self.o.state, self.o.raw),
                self.now + dt,
                300,
                'Needs 40 MB more memory.',
            )
        self.o.state['demandProposal'] = p
        self.o.status = 'waiting'
        self.o.detail = p['reason']

    def record(self):
        self.o.live_projection.record(self.d, self.o.state, self.o.live, self.o.raw)

    def get(self, dt=0):
        return self.o.live_projection.snapshot('private-account', self.now + dt)

    def test_ready_waiting_retains_target_without_claiming_dispatch(self):
        v = self.get()
        self.assertEqual(
            (v['phase'], v['proposalTarget'], v['pendingTarget']), ('waiting', 'b', None)
        )
        self.assertEqual(v['confirmation']['status'], 'ready')

    def test_stale_comparison_pauses_without_adding_seconds_and_expires(self):
        v = self.get(100)
        self.assertFalse(v['fresh'])
        self.assertEqual(
            (v['confirmation']['status'], v['confirmation']['seconds']), ('paused', 300)
        )
        self.assertIsNone(self.get(151)['confirmation'])

    def test_changed_plan_mode_policy_session_intent_hide_retained(self):
        from types import SimpleNamespace

        state = copy.deepcopy(self.o.state)
        for change in (
            {'models': ['a', 'b', 'c']},
            {'demandPolicy': {**state['demandPolicy'], 'minRunMinutes': 60}},
            {'mode': 'observe'},
        ):
            self.o.state = {**state, **change}
            self.assertIsNone(self.get()['confirmation'])
        self.o.state = state
        self.o.raw['pid'] += 1
        self.assertIsNone(self.get()['confirmation'])
        self.o.raw['pid'] -= 1
        self.o.automatic_control = SimpleNamespace(operation={'id': 'new-intent'})
        self.assertIsNone(self.get()['confirmation'])

    def test_future_proposal_source_is_hidden(self):
        self.o.state['demandProposal']['sourceAt'] = self.now + 1
        self.assertIsNone(self.get()['confirmation'])


if __name__ == '__main__':
    unittest.main()
