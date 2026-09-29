"""Customer regression: 3+ models, independent API ledger, exact Mac attribution."""

import copy, json, pathlib, ssl, tempfile, unittest, urllib.error
from unittest.mock import Mock, patch
from collector import Collector
from optimizer import roster_identity, launch_options
from provider_reporting import ProviderReporting, ReportingIdentity, observed_models
from model_combinations import selection_key
from test_live_earnings import T, credit, account


def daemon(at=T):
    return {
        'pid': 123,
        'started_at': T - 100,
        'written_at': at,
        'attestation_public_key': 'fixture-key',
        'current_model': 'a',
        'advertised_models': ['a', 'b', 'c'],
        'warm_models': ['a', 'b', 'c'],
        'trust': {'status': 'online'},
        'inference_active': False,
        'stats': {'requests_served': 10, 'tokens_generated': 100},
        'slots': [{'model': m, 'kv_backend': 'paged'} for m in 'abc'],
    }


def roster(raw):
    return [
        {
            'se_public_key': raw['attestation_public_key'],
            'provider_id': 'this-mac',
            'models': raw['advertised_models'],
            'status': 'online',
            'trust_level': 'hardware',
        }
    ]


class ReportingReadinessTests(unittest.TestCase):
    def setUp(self):
        self.r = ProviderReporting()
        self.raw = daemon()

    def observe(self, delta=0, **changes):
        self.raw.update(written_at=T + delta, **changes)
        return self.r.observe('account', self.raw, T + delta, True)

    def warm(self):
        self.assertFalse(self.observe()['counting'])
        result = self.observe(3, stats={'requests_served': 11, 'tokens_generated': 160})
        self.assertTrue(result['counting'])
        return result

    def test_observed_output_then_idle_counts_only_forward_from_proof(self):
        proof = self.warm()
        self.assertEqual(proof['verifiedAt'], T + 3)
        self.assertEqual(self.observe(6)['verifiedAt'], T + 3)
        self.assertEqual(self.observe(6)['verifiedAt'], T + 3)
        self.assertEqual(proof['scope'], 'aggregate')

    def test_strict_model_set_validation(self):
        for bad in (
            None,
            [],
            ['a', 'a'],
            [' a'],
            [''],
            ['x' * 513],
            [1],
            ['a', None],
            list(map(str, range(65))),
        ):
            self.assertEqual(observed_models(bad), [])
        self.assertEqual(observed_models(['c', 'a', 'b']), ['a', 'b', 'c'])

    def test_cold_failed_slot_unknown_trust_and_invalid_counters_pause(self):
        # A partly loaded set keeps counting (test_load_or_unload_...); nothing loaded pauses.
        for changes in (
            {'warm_models': []},
            {'slots': []},
            {'slots': [{'model': m, 'kv_backend': None if m == 'b' else 'paged'} for m in 'abc']},
            {
                'slots': [
                    {
                        'model': m,
                        'kv_backend': 'paged',
                        'load_error': 'failed' if m == 'b' else None,
                    }
                    for m in 'abc'
                ]
            },
            {'slots': [None]},
            {'trust': None},
            {'trust': {'status': 'offline'}},
            {'stats': {'requests_served': True, 'tokens_generated': 160}},
            {'stats': {'requests_served': -1, 'tokens_generated': 160}},
            {'pid': False},
            {'started_at': T + 99},
        ):
            with self.subTest(changes=changes):
                self.setUp()
                self.warm()
                self.assertFalse(self.observe(6, **changes)['counting'])
                self.assertIsNone(self.r.verified_at)

    def test_legacy_missing_slots_can_verify_actual_serving(self):
        self.raw.pop('slots')
        self.warm()

    def test_load_or_unload_in_the_same_process_keeps_counting_in_a_new_segment(self):
        # Darkbloom 0.9.10 loads models on demand and unloads idle ones (Jason, Sep 27).
        self.warm()
        with self.assertLogs('bloom.reporting', 'INFO') as logs:
            unloaded = self.observe(6, warm_models=['a', 'b'])
            self.assertTrue(unloaded['counting'])
            self.assertEqual(unloaded['verifiedAt'], T + 6)  # new segment: sets never mix
            self.assertEqual(unloaded['loaded'], ['a', 'b'])
            self.assertIn('2 of 3 models loaded now (a, b)', unloaded['detail'])
            self.assertEqual(self.observe(9)['verifiedAt'], T + 6)
            loaded = self.observe(12, warm_models=['a', 'b', 'c'])
            self.assertTrue(loaded['counting'])
            self.assertEqual(loaded['verifiedAt'], T + 12)
        self.assertEqual(len(logs.records), 2)
        self.assertIn('change 1 in this provider process', logs.output[0])
        self.assertIn('2 of 3 offered loaded (a, b)', logs.output[0])
        self.assertIn('change 2 in this provider process', logs.output[1])
        # A change across a gap still needs fresh output.
        self.assertFalse(self.observe(40, warm_models=['a'])['counting'])
        # So does loading again after nothing was loaded: the Mac went cold.
        self.setUp()
        self.warm()
        self.assertFalse(self.observe(6, warm_models=[])['counting'])
        self.assertFalse(self.observe(9, warm_models=['a'])['counting'])
        self.assertTrue(
            self.observe(12, stats={'requests_served': 12, 'tokens_generated': 200})['counting']
        )

    def test_only_models_the_network_routes_here_count_as_loaded(self):
        # The roster row lists a and b: c gets no network work on this Mac.
        def observe(delta, **changes):
            self.raw.update(written_at=T + delta, **changes)
            return self.r.observe('account', self.raw, T + delta, True, False, 'mac', ('a', 'b'))

        self.assertIn(
            'none of the 2 models the network sends this Mac work for are loaded yet',
            observe(0, warm_models=['c'])['detail'],
        )
        observe(3, warm_models=['a', 'c'])
        counting = observe(6, stats={'requests_served': 11, 'tokens_generated': 160})
        self.assertTrue(counting['counting'])
        self.assertEqual(counting['loaded'], ['a'])
        self.assertIn('1 of 2 models loaded now (a)', counting['detail'])
        self.assertIn('work for 2 of the 3 models your provider offers', counting['detail'])
        # c loading or unloading changes nothing that counts: same segment.
        self.assertEqual(observe(9, warm_models=['a'])['verifiedAt'], T + 6)

    def test_read_only_proof_cannot_bridge_process_selection_account_or_device(self):
        for changes in (
            {'pid': 124},
            {'started_at': T - 90},
            {'attestation_public_key': 'other'},
            {
                'advertised_models': ['a', 'b', 'd'],
                'warm_models': ['a', 'b', 'd'],
                'slots': [{'model': m, 'kv_backend': 'paged'} for m in 'abd'],
            },
        ):
            self.setUp()
            self.warm()
            self.assertFalse(self.observe(6, **changes)['counting'])
        self.setUp()
        self.warm()
        self.assertFalse(self.r.observe('other', self.raw, T + 3, True)['counting'])

    def test_stale_pending_identity_gaps_and_counter_reset_require_new_output(self):
        for kw in (
            {'now': T + 20},
            {'now': T - 6},
            {'now': T + 3, 'pending': True},
            {'now': T + 3, 'identity_verified': False},
        ):
            self.setUp()
            self.warm()
            args = {'now': T + 3, 'identity_verified': True, **kw}
            self.assertFalse(self.r.observe('account', self.raw, **args)['counting'])
        self.setUp()
        self.warm()
        self.assertFalse(self.observe(30)['counting'])
        self.setUp()
        self.warm()
        self.assertFalse(
            self.observe(6, stats={'requests_served': 0, 'tokens_generated': 0})['counting']
        )
        self.setUp()
        self.warm()
        self.assertFalse(
            self.observe(3, stats={'requests_served': 12, 'tokens_generated': 170})['counting']
        )

    def test_roster_reporting_does_not_grant_control_eligibility(self):
        proof = roster_identity(self.raw, roster(self.raw))
        self.assertTrue(proof['reportingEligible'])
        self.assertFalse(proof['servingEligible'])
        self.assertIsNone(selection_key(self.raw['advertised_models']))
        with self.assertRaises(ValueError):
            launch_options(
                {
                    'ProgramArguments': [
                        'darkbloom',
                        'start',
                        '--model',
                        'a',
                        '--model',
                        'b',
                        '--model',
                        'c',
                    ]
                }
            )
        for values in (None, [], ['a', 'b', 'x'], ['a', 'b', 'b']):
            rows = roster(self.raw)
            rows[0]['models'] = values
            self.assertFalse(roster_identity(self.raw, rows)['reportingEligible'])
        with self.assertRaises(ValueError):
            roster_identity(self.raw, roster(self.raw) * 2)


class MultiModelCollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.c = Collector(self.root)
        self.c.account = 'account'
        self.c.sessions.probe = Mock(return_value=True)
        self.raw = daemon()
        self.rows = roster(self.raw)
        self.path = self.root / '.darkbloom/daemon-state.json'
        self.path.parent.mkdir()
        self.c.optimizer.runner = Mock(
            side_effect=AssertionError('No provider command is permitted in reporting')
        )
        self.c.optimizer.next_discovery = T + 1e6
        self.c.network.fetch = Mock(
            side_effect=lambda path: (
                {'providers': self.rows} if path == '/v1/providers/attestation' else {}
            )
        )
        self.c.network.snapshot = Mock(return_value={})
        self.entries = []

    def tearDown(self):
        self.c.optimizer.runner.assert_not_called()
        self.c.close()
        self.c.history.close()
        self.tmp.cleanup()

    def step(self, delta, refresh=False, **changes):
        self.raw.update(written_at=T + delta, **changes)
        self.path.write_text(json.dumps(self.raw))
        if refresh:
            self.c.optimizer.next_identity = 0
            with patch('optimizer.time.monotonic', return_value=0):
                self.c.optimizer.refresh(T + delta)
        self.c.collect(T + delta)
        return self.c.snapshot

    def paid(self, delta, entries):
        self.entries += entries
        self.c.accept_earnings(account(self.entries), T + delta)

    def run_ready(self):
        self.paid(0, [])
        self.step(0, True)
        for delta in range(3, 64, 3):
            if delta == 30:
                self.paid(
                    30,
                    [
                        credit(1, 100, T + 10, 'a', 'this-mac'),
                        credit(2, 200, T + 20, 'b', 'this-mac'),
                    ],
                )
            if delta == 60:
                self.paid(
                    60,
                    [
                        credit(3, 300, T + 35, 'c', 'this-mac'),
                        credit(4, 900000, T + 35, 'a', 'other-mac'),
                        credit(5, 900000, T + 35, 'base_reward', 'this-mac'),
                        credit(6, 900000, T + 35, 'unselected', 'this-mac'),
                    ],
                )
            self.step(
                delta, stats={'requests_served': 10 + delta, 'tokens_generated': 100 + delta * 20}
            )
        return self.c.snapshot

    def test_three_model_pulse_without_monitor_aggregates_only_this_macs_selected_inference(self):
        result = self.run_ready()
        self.assertFalse((self.root / 'Library/Application Support/Darkbloom Monitor').exists())
        self.assertEqual(result['monitor']['source'], 'BloomGauge confirmed API ledger')
        self.assertEqual(result['pulse']['status'], 'live')
        self.assertEqual(result['pulse']['sessionMicroUsd'], 600)
        # The API ledger starts at its earliest returned credit (T+10).
        # The initial boundary credit is retained in session earnings, but a
        # rate uses the open-left, closed-right covered interval (T+10,T+40].
        self.assertEqual(result['pulse']['windows']['60']['seconds'], 30)
        self.assertEqual(result['pulse']['windows']['60']['microUsd'], 500)
        self.assertAlmostEqual(result['pulse']['windows']['60']['ratePerHour'], 0.0005 * 3600 / 30)
        self.assertEqual(result['provider']['sessionJobs'], 60)
        self.assertEqual(result['provider']['sessionTokens'], 1200)
        self.assertEqual(result['provider']['tokensPerSecond'], 20)
        self.assertEqual(result['traffic']['status'], 'live')
        self.assertEqual(result['pulse']['reporting']['models'], list('abc'))
        self.assertFalse(self.c.optimizer.tracking(self.raw, T + 63)['counting'])
        self.assertEqual(
            self.c.history.db.execute('SELECT COUNT(*) FROM opt_ready_minutes').fetchone()[0], 0
        )
        self.assertEqual(self.c.optimizer.state['mode'], 'observe')
        with patch('optimizer.time.time', return_value=T + 63):
            opt = self.c.optimizer.snapshot()
        self.assertEqual(opt['currentModels'], list('abc'))
        self.assertEqual(opt['reporting']['sessionId'], result['provider']['session']['id'])
        self.assertIsNone(opt['demandAuto']['target'])
        # The manager holds one model or a pair; three are left to Darkbloom.
        self.assertIn('one serving model, or a pair', opt['demandAuto']['controlError'])

    def test_four_model_collection_and_cold_model_gap_do_not_bridge(self):
        self.raw['advertised_models'] = list('abcd')
        self.raw['warm_models'] = list('abcd')
        self.raw['slots'] = [{'model': m, 'kv_backend': 'paged'} for m in 'abcd']
        self.rows = roster(self.raw)
        a = self.run_ready()
        self.assertEqual(len(a['pulse']['models']), 4)
        proof = self.c.optimizer.reporting_roster.proof
        # Darkbloom unloading d starts a new segment. The roster row doesn't depend on
        # loaded models, so the proof stays and counting carries on without a new fetch.
        b = self.step(66, warm_models=list('abc'))
        self.assertEqual(b['pulse']['status'], 'live')
        self.assertTrue(b['pulse']['reporting']['counting'])
        self.assertIn('3 of 4 models loaded now (a, b, c)', b['pulse']['detail'])
        self.assertIs(self.c.optimizer.reporting_roster.proof, proof)
        self.assertFalse(self.c.optimizer.reporting_recheck_requested)
        c = self.step(69, warm_models=list('abcd'))
        self.assertEqual(c['pulse']['status'], 'live')
        # Segments never bridge a change: the ready time between samples straddling it
        # belongs to neither loaded set.
        self.assertEqual(c['provider']['session']['performance']['segmentStartedAt'], T + 69)

        def intervals():
            return [
                tuple(r)
                for r in self.c.history.db.execute(
                    'SELECT start,end FROM session_ready_intervals WHERE session=? ORDER BY start',
                    (c['provider']['session']['id'],),
                )
            ]

        self.assertEqual(intervals(), [(T + 3, T + 63)])
        self.assertEqual(self.step(72)['pulse']['status'], 'live')
        self.assertEqual(intervals(), [(T + 3, T + 63), (T + 69, T + 72)])

    def test_large_model_set_counts_with_only_some_models_loaded(self):
        # Customer case (Sep 27): 11 models offered, only a few loaded at a time.
        offered = [f'm{i:02d}' for i in range(11)]
        self.raw['advertised_models'] = offered
        self.raw['warm_models'] = ['m00', 'm03']
        self.raw['current_model'] = 'm00'
        self.raw['slots'] = [{'model': m, 'kv_backend': 'paged'} for m in ('m00', 'm03')]
        self.rows = roster(self.raw)
        self.paid(0, [])
        self.step(0, True)
        for delta in range(3, 64, 3):
            snap = self.step(
                delta, stats={'requests_served': 10 + delta, 'tokens_generated': 100 + delta * 20}
            )
        reporting = snap['pulse']['reporting']
        self.assertTrue(reporting['counting'], snap['pulse']['detail'])
        self.assertEqual(len(reporting['models']), 11)
        self.assertIn('2 of 11 models loaded now (m00, m03)', reporting['detail'])
        # Another model loading is a different set: a fresh segment, without a pause.
        loaded = ('m00', 'm03', 'm07')
        more = self.step(
            66,
            warm_models=list(loaded),
            slots=[{'model': m, 'kv_backend': 'paged'} for m in loaded],
        )
        self.assertTrue(more['pulse']['reporting']['counting'], more['pulse']['detail'])
        self.assertIn('3 of 11 models loaded now (m00, m03, m07)', more['pulse']['detail'])
        self.assertEqual(more['provider']['session']['performance']['segmentStartedAt'], T + 66)

    def test_roster_that_leaves_out_offered_models_matches_the_models_it_routes(self):
        # Customer case (Sep 27): 11 offered. The coordinator's row lists only the models
        # it routes to this Mac: none outside its catalog, and no catalog model this Mac
        # can't serve (weight hash, hardware or runtime capability, App Attest pending).
        offered = [f'm{i:02d}' for i in range(11)]
        self.raw['advertised_models'] = offered
        rows = roster(self.raw)
        for listed in (offered[:10], offered[:6], offered[1:10], [offered[4]]):
            rows[0]['models'] = listed
            proof = roster_identity(self.raw, rows)
            self.assertTrue(proof['reportingEligible'], listed)
            self.assertEqual(proof['models'], listed)
            self.assertFalse(proof['servingEligible'])
        # An extra, empty, duplicated or malformed row still fails closed.
        for listed in (offered + ['x'], [], offered[:5] * 2, [offered[0], None], None):
            rows[0]['models'] = listed
            self.assertFalse(roster_identity(self.raw, rows)['reportingEligible'], listed)

    def test_collector_counts_when_roster_omits_a_model_outside_the_catalog(self):
        offered = [f'm{i:02d}' for i in range(11)]
        self.c.optimizer.catalog = [{'id': m} for m in offered[:10]]
        self.raw['advertised_models'] = offered
        self.raw['warm_models'] = ['m00', 'm03']
        self.raw['current_model'] = 'm00'
        self.raw['slots'] = [{'model': m, 'kv_backend': 'paged'} for m in ('m00', 'm03')]
        self.rows = roster(self.raw)
        self.rows[0]['models'] = offered[:10]
        self.paid(0, [])
        self.step(0, True)
        for delta in range(3, 64, 3):
            snap = self.step(
                delta, stats={'requests_served': 10 + delta, 'tokens_generated': 100 + delta * 20}
            )
        self.assertTrue(snap['pulse']['reporting']['counting'], snap['pulse']['detail'])

    def test_large_model_set_with_nothing_loaded_says_so(self):
        self.raw['advertised_models'] = [f'm{i:02d}' for i in range(11)]
        self.raw['warm_models'] = []
        self.raw['slots'] = []
        self.rows = roster(self.raw)
        self.paid(0, [])
        snap = self.step(0, True)
        self.assertFalse(snap['pulse']['reporting']['counting'])
        self.assertIn('none of the 11 models your provider offers are loaded yet', snap['pulse']['detail'])

    def test_wrong_identity_stale_roster_stopped_process_and_account_never_earn(self):
        self.run_ready()
        self.assertIsNone(
            self.c.optimizer.reporting_identity(
                {**self.raw, 'attestation_public_key': 'wrong'}, T + 63
            )
        )
        self.assertIsNone(self.c.optimizer.reporting_identity(self.raw, T - 1))
        self.step(63, True)
        self.c.optimizer.reporting_roster.proof['at'] = T - 200
        self.assertEqual(self.step(66)['pulse']['status'], 'unmatched')
        self.step(66, True)
        self.c.sessions.probe.return_value = False
        self.assertEqual(self.step(69)['pulse']['status'], 'offline')
        self.assertIsNone(self.c.provider_reporting.verified_at)
        self.c.sessions.probe.return_value = True
        self.c.account = 'different-account'
        b = self.step(72)
        self.assertEqual(b['pulse']['status'], 'unmatched')
        self.assertIsNone(b['pulse']['windows']['60']['ratePerHour'])

    def test_selection_change_requires_roster_rematch_and_never_reuses_old_rates(self):
        a = self.run_ready()
        b = self.step(
            66,
            advertised_models=list('abd'),
            warm_models=list('abd'),
            slots=[{'model': m, 'kv_backend': 'paged'} for m in 'abd'],
        )
        self.assertEqual(b['pulse']['status'], 'unmatched')
        self.assertNotEqual(a['provider']['session']['id'], b['provider']['session']['id'])
        self.assertIsNone(b['pulse']['windows']['60']['ratePerHour'])
        self.rows = roster(self.raw)
        self.assertEqual(self.step(69, True)['pulse']['status'], 'paused')

    def test_ambiguous_roster_does_not_fall_back_to_account_totals(self):
        self.rows *= 2
        self.paid(0, [credit(1, 999, T - 1, 'a', 'this-mac')])
        a = self.step(0, True)
        self.assertEqual(a['pulse']['status'], 'unmatched')
        self.assertIsNone(a['pulse']['sessionMicroUsd'])
        self.assertIsNone(self.c.optimizer.reporting_roster.proof)

    def fresh_ready_case(self):
        case = MultiModelCollectorTests()
        case.setUp()
        self.addCleanup(case.tearDown)
        case.run_ready()
        return case

    def test_transport_timeout_preserves_warm_reporting_but_revokes_all_control_gates(self):
        before = self.run_ready()
        proof = copy.deepcopy(self.c.optimizer.reporting_roster.proof)
        self.c.network.fetch.side_effect = urllib.error.URLError('synthetic timeout')
        failed = self.step(66, True)
        self.assertEqual(failed['pulse']['status'], 'live')
        self.assertEqual(failed['traffic']['status'], 'live')
        self.assertEqual(
            failed['provider']['tracking']['verifiedAt'],
            before['provider']['tracking']['verifiedAt'],
        )
        self.assertEqual(failed['pulse']['sessionMicroUsd'], 600)
        self.assertEqual(self.c.optimizer.reporting_roster.proof, proof)
        opt = self.c.optimizer
        self.assertFalse(opt.identity_ok)
        self.assertFalse(opt.device_identity_ok)
        self.assertFalse(opt.identity_hardware)
        self.assertEqual(opt.eligible_models, [])
        self.assertIsNone(opt.identity_provider)
        self.assertFalse(opt.tracking(self.raw, T + 66)['counting'])
        self.assertEqual(
            self.c.history.db.execute('SELECT COUNT(*) FROM opt_ready_minutes').fetchone()[0], 0
        )
        with patch('optimizer.time.time', return_value=T + 66):
            view = opt.snapshot()
        self.assertIsNone(view['demandAuto']['target'])
        self.assertIn('one serving model, or a pair', view['demandAuto']['controlError'])
        self.assertEqual(opt.state['mode'], 'observe')
        self.c.network.fetch.side_effect = lambda path: {'providers': self.rows}
        recovered = self.step(69, True)
        self.assertEqual(recovered['pulse']['status'], 'live')
        self.assertEqual(
            recovered['provider']['tracking']['verifiedAt'],
            before['provider']['tracking']['verifiedAt'],
        )
        self.assertEqual(recovered['pulse']['sessionMicroUsd'], 600)

    def test_repeated_network_errors_never_renew_original_roster_expiry_and_recovery_needs_new_output(
        self,
    ):
        self.run_ready()
        original = self.c.optimizer.reporting_roster.proof['at']
        self.c.network.fetch.side_effect = urllib.error.URLError('synthetic timeout')
        for delta in range(66, 183, 3):
            if delta % 30 == 0:
                self.paid(delta, [])
            result = self.step(delta, refresh=delta in (66, 120, 177, 180))
            self.assertEqual(
                result['pulse']['status'], 'live' if delta < 180 else 'unmatched', delta
            )
            if delta < 180:
                self.assertEqual(self.c.optimizer.reporting_roster.proof['at'], original)
        self.assertIsNone(self.c.optimizer.reporting_roster.proof)
        self.c.network.fetch.side_effect = lambda path: {'providers': self.rows}
        self.assertEqual(self.step(183, True)['pulse']['status'], 'paused')
        self.assertEqual(
            self.step(186, stats={'requests_served': 90, 'tokens_generated': 2000})['pulse'][
                'status'
            ],
            'live',
        )
        self.assertEqual(
            self.c.snapshot['provider']['session']['performance']['segmentStartedAt'], T + 186
        )

    def test_authoritative_invalid_rosters_revoke_and_cannot_reappear_on_later_timeout(self):
        cases = [
            None,
            [],
            roster(daemon()) * 2,
            {},
            [None],
            roster(daemon()) + [None],
            roster(daemon()) + [{**roster(daemon())[0], 'se_public_key': 'different-device'}],
            [{**roster(daemon())[0], 'models': ['a', 'b', 'c', 'x']}],
            [{**roster(daemon())[0], 'models': []}],
            [{**roster(daemon())[0], 'models': ['a', 'b', 'b']}],
            [{**roster(daemon())[0], 'models': None}],
            [{**roster(daemon())[0], 'provider_id': None}],
            [{**roster(daemon())[0], 'status': 'offline'}],
            [{**roster(daemon())[0], 'trust_level': 'software'}],
        ]
        for rows in cases:
            with self.subTest(rows=rows):
                case = self.fresh_ready_case()
                case.rows = rows
                result = case.step(66, True)
                self.assertEqual(result['pulse']['status'], 'unmatched')
                self.assertIsNone(case.c.optimizer.reporting_roster.proof)
                case.c.network.fetch.side_effect = urllib.error.URLError('synthetic timeout')
                self.assertEqual(case.step(69, True)['pulse']['status'], 'unmatched')

    def test_malformed_http_tls_and_local_read_errors_do_not_use_transport_grace(self):
        failures = [
            ValueError('bad JSON'),
            KeyError('providers'),
            urllib.error.HTTPError('https://fixture.invalid', 503, 'unavailable', {}, None),
            urllib.error.URLError(ssl.SSLCertVerificationError('untrusted certificate')),
        ]
        for error in failures:
            with self.subTest(error=type(error).__name__):
                case = self.fresh_ready_case()
                case.c.network.fetch.side_effect = error
                self.assertEqual(case.step(66, True)['pulse']['status'], 'unmatched')
                self.assertIsNone(case.c.optimizer.reporting_roster.proof)
        with patch.object(self.c.optimizer, 'read_state', side_effect=OSError('unreadable')):
            self.c.optimizer.refresh(T)
        self.assertIsNone(self.c.optimizer.reporting_roster.proof)

    def test_local_readiness_or_scope_gap_revokes_even_while_roster_network_is_down(self):
        # Loaded models and slots are not identity: see the loaded-set test below.
        changes = [
            {'trust': {'status': 'offline'}},
            {'trust': None},
            {'stats': {'requests_served': -1, 'tokens_generated': 200}},
            {'pid': 124},
            {'started_at': T - 90},
            {'attestation_public_key': 'other'},
            {'advertised_models': ['a', 'b']},
            {'advertised_models': ['a', 'b', 'd']},
        ]
        for change in changes:
            with self.subTest(change=change):
                case = self.fresh_ready_case()
                saved = copy.deepcopy(case.raw)
                case.c.network.fetch.side_effect = urllib.error.URLError('synthetic timeout')
                result = case.step(66, True, **change)
                self.assertNotEqual(result['pulse']['status'], 'live')
                self.assertIsNone(case.c.optimizer.reporting_roster.proof)
                case.raw = saved
                self.assertNotEqual(case.step(69, True)['pulse']['status'], 'live')
        case = self.fresh_ready_case()
        case.c.collect(T + 90)
        self.assertIsNone(case.c.optimizer.reporting_roster.proof)
        case = self.fresh_ready_case()
        case.c.sessions.probe.return_value = False
        case.step(66)
        self.assertIsNone(case.c.optimizer.reporting_roster.proof)
        case = self.fresh_ready_case()
        data = account([])
        data['account_id'] = 'new-account'
        case.c.accept_earnings(data, T + 66)
        self.assertIsNone(case.c.optimizer.reporting_roster.proof)

    def test_loaded_models_and_slots_are_readiness_not_roster_identity(self):
        # Darkbloom loading or unloading models, or a slot problem, never revokes the
        # roster proof; statistics start a new segment or pause on their own.
        for change, status in (({'warm_models': ['a', 'b']}, 'live'), ({'slots': []}, 'paused')):
            with self.subTest(change=change):
                case = self.fresh_ready_case()
                proof = case.c.optimizer.reporting_roster.proof
                case.c.network.fetch.side_effect = urllib.error.URLError('synthetic timeout')
                self.assertEqual(case.step(66, True, **change)['pulse']['status'], status)
                self.assertIs(case.c.optimizer.reporting_roster.proof, proof)
                self.assertFalse(case.c.optimizer.reporting_recheck_requested)

    def test_transport_grace_never_creates_an_initial_proof_or_counts_before_output(self):
        self.paid(0, [])
        self.c.network.fetch.side_effect = TimeoutError('timeout')
        self.assertEqual(self.step(0, True)['pulse']['status'], 'unmatched')
        self.assertIsNone(self.c.optimizer.reporting_roster.proof)
        self.c.network.fetch.side_effect = lambda path: {'providers': self.rows}
        self.assertEqual(self.step(3, True)['pulse']['status'], 'paused')
        self.c.network.fetch.side_effect = ConnectionError('disconnected')
        self.assertEqual(self.step(6, True)['pulse']['status'], 'paused')
        self.assertEqual(
            self.step(9, stats={'requests_served': 11, 'tokens_generated': 160})['pulse']['status'],
            'live',
        )

    def test_solo_and_pair_control_identity_still_fail_closed_on_transport_error(self):
        for models in (['a'], ['a', 'b']):
            with self.subTest(models=models):
                case = MultiModelCollectorTests()
                case.setUp()
                self.addCleanup(case.tearDown)
                case.raw['advertised_models'] = models
                case.rows = roster(case.raw)
                case.paid(0, [])
                case.step(0, True)
                self.assertTrue(case.c.optimizer.identity_ok)
                case.c.network.fetch.side_effect = urllib.error.URLError('synthetic timeout')
                case.step(3, True)
                self.assertFalse(case.c.optimizer.identity_ok)
                self.assertFalse(case.c.optimizer.device_identity_ok)
                self.assertIsNone(case.c.optimizer.reporting_roster.proof)

    def test_discovery_elapsed_clock_matches_fresh_daemon_without_real_cli(self):
        self.paid(0, [])
        self.raw['written_at'] = T + 6
        self.path.write_text(json.dumps(self.raw))
        opt = self.c.optimizer
        opt.next_discovery = 0
        # Deliberately mocked model discovery; no CLI/provider process executes.
        with (
            patch.object(opt, 'runner', return_value=Mock(stdout='{"models":[]}')),
            patch.object(
                self.c.network,
                'fetch',
                side_effect=lambda path: (
                    {'models': []} if path.endswith('catalog') else {'providers': self.rows}
                ),
            ),
            patch('optimizer.time.monotonic', side_effect=[100, 106, 106]),
        ):
            opt.refresh(T)
        self.assertEqual(opt.reporting_roster.proof['at'], T + 6)
        self.assertEqual(opt.reporting_identity(self.raw, T + 6), 'this-mac')

    def test_identity_or_account_change_during_roster_fetch_cannot_confirm_reporting(self):
        for change in ('process', 'account'):
            with self.subTest(change=change):
                case = self.fresh_ready_case()

                def fetch(path):
                    if change == 'process':
                        case.path.write_text(json.dumps({**case.raw, 'pid': 999}))
                    else:
                        case.c.history.cache('account', 'changed')
                    return {'providers': case.rows}

                case.c.network.fetch.side_effect = fetch
                case.c.optimizer.next_identity = 0
                with patch('optimizer.time.monotonic', return_value=0):
                    case.c.optimizer.refresh(T + 63)
                self.assertIsNone(case.c.optimizer.reporting_roster.proof)

    def test_authoritative_provider_mapping_change_requires_new_serving_proof(self):
        self.run_ready()
        self.rows[0]['provider_id'] = 'replacement-provider'
        self.assertEqual(self.step(66, True)['pulse']['status'], 'paused')
        self.assertEqual(self.step(69)['pulse']['status'], 'paused')
        result = self.step(72, stats={'requests_served': 90, 'tokens_generated': 2000})
        self.assertEqual(result['pulse']['status'], 'live')
        self.assertEqual(result['provider']['session']['performance']['segmentStartedAt'], T + 72)

    def test_refresh_finishing_after_collector_clock_keeps_existing_warm_output_proof(self):
        before = self.run_ready()
        self.raw['written_at'] = T + 66
        self.path.write_text(json.dumps(self.raw))
        self.c.optimizer.next_identity = 0
        with patch('optimizer.time.monotonic', side_effect=[100, 100.01, 100.02]):
            self.c.optimizer.refresh(T + 66)
        self.c.collect(T + 66)
        self.assertEqual(self.c.snapshot['pulse']['status'], 'live')
        self.assertEqual(
            self.c.snapshot['provider']['tracking']['verifiedAt'],
            before['provider']['tracking']['verifiedAt'],
        )
        self.assertEqual(self.step(69)['pulse']['status'], 'live')
        self.assertEqual(self.c.optimizer.reporting_roster.previous_proof['at'], T)


class ReportingIdentityTests(unittest.TestCase):
    def test_exact_expiry_and_old_collector_sample_cannot_renew_or_erase_newer_proof(self):
        proof = ReportingIdentity()
        raw = daemon(T + 10)
        proof.confirm('account', raw, T + 10, 'this-mac', T + 10)
        self.assertIsNone(proof.match('account', raw, T + 9.99))
        self.assertEqual(proof.proof['at'], T + 10)
        raw['written_at'] = T + 189.99
        self.assertEqual(proof.match('account', raw, T + 189.99), 'this-mac')
        raw['written_at'] = T + 190
        self.assertIsNone(proof.match('account', raw, T + 190))
        self.assertIsNone(proof.proof)

    def test_older_samples_cannot_erase_newer_scope_or_borrow_an_expired_previous_proof(self):
        proof = ReportingIdentity()
        raw = daemon(T)
        proof.confirm('account', raw, T, 'this-mac', T)
        latest = {**raw, 'written_at': T + 180.1}
        proof.confirm('account', latest, T + 180.1, 'this-mac', T + 180.1)
        self.assertIsNone(proof.match('account', {**raw, 'written_at': T + 180}, T + 180))
        self.assertEqual(proof.proof['at'], T + 180.1)
        changed = {**latest, 'pid': 999, 'written_at': T + 181}
        proof.confirm('account', changed, T + 181, 'this-mac', T + 181)
        self.assertIsNone(proof.match('account', latest, T + 180.5))
        self.assertEqual(proof.match('account', changed, T + 181), 'this-mac')
        self.assertIsNone(proof.previous_proof)


if __name__ == '__main__':
    unittest.main()
