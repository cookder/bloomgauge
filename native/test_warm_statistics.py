import copy, json, pathlib, tempfile, threading, unittest
from unittest.mock import Mock
from history import History
from optimizer import Optimizer
from optimizer_store import OptimizerStore, device_id
from model_readiness import readiness, session_key
from model_projection import ModelProjection
from provider_sessions import ProviderSessions
from live_earnings import EarningsPulse, credit_rows
from forecast import forecast

T = 1788847200


def raw(at=T, jobs=10, tokens=100, **changes):
    return {
        'pid': 10,
        'started_at': T - 100,
        'written_at': at,
        'attestation_public_key': 'device',
        'advertised_models': ['a'],
        'current_model': 'a',
        'warm_models': ['a'],
        'trust': {'status': 'online'},
        'inference_active': False,
        'stats': {'requests_served': jobs, 'tokens_generated': tokens},
        **changes,
    }


def proof(state, at=T - 1):
    from model_combinations import selection_key

    return {
        'session': session_key(state),
        'model': selection_key(state['advertised_models']),
        'status': 'ready',
        'verifiedAt': at,
    }


class WarmStatisticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name)
        self.h = History(':memory:')
        self.s = OptimizerStore(self.h)
        self.sessions = ProviderSessions(self.h, self.home, Mock(return_value=True))

    def tearDown(self):
        self.h.close()
        self.tmp.cleanup()

    def observe(self, state, ready=True, at=None):
        at = state['written_at'] if at is None else at
        gate = (
            readiness(state, proof(state), at, True)
            if ready
            else {'counting': False, 'detail': 'Loading'}
        )
        return self.sessions.observe('account', state, at, gate)

    def test_fresh_matching_decode_and_all_models_are_required_but_idle_is_ready(self):
        r = raw()
        p = proof(r)
        self.assertTrue(readiness(r, p, T, True)['counting'])
        for state, pr, now, identity, pending in [
            (raw(warm_models=[]), p, T, True, False),
            (r, {**p, 'status': 'waiting'}, T, True, False),
            (r, {**p, 'status': 'failed'}, T, True, False),
            (r, {**p, 'status': 'warming'}, T, True, False),
            (r, {**p, 'session': 'other'}, T, True, False),
            (r, p, T, False, False),
            (r, p, T, True, True),
            (r, p, T + 16, True, False),
            (r, {**p, 'verifiedAt': T + 1}, T, True, False),
            (raw(trust={'status': 'offline'}), p, T, True, False),
        ]:
            self.assertFalse(readiness(state, pr, now, identity, pending)['counting'])
        pair = raw(advertised_models=['a', 'b'], warm_models=['a', 'b'])
        self.assertTrue(
            readiness(
                {**pair, 'advertised_models': ['b', 'a'], 'current_model': 'b'},
                proof(pair),
                T,
                True,
            )['counting']
        )
        self.assertFalse(
            readiness({**pair, 'warm_models': ['a']}, proof(pair), T, True)['counting']
        )

    def test_model_counters_pause_without_restarting_session_and_do_not_catch_up_warmups(self):
        first = self.observe(raw(), False)
        self.observe(raw(T + 3, 20, 300), False)
        warm = self.observe(raw(T + 6, 21, 301))
        self.assertEqual(warm['performance']['seconds'], 0)
        self.assertEqual(warm['performance']['requests'], 0)
        a = self.observe(raw(T + 9, 23, 321))
        self.assertEqual(
            (a['performance']['seconds'], a['performance']['requests'], a['performance']['tokens']),
            (3, 2, 20),
        )
        idle = self.observe(raw(T + 12, 23, 321))
        self.assertEqual(idle['performance']['seconds'], 6)
        self.observe(raw(T + 15, 30, 500), False)
        self.observe(raw(T + 18, 31, 501))
        end = self.observe(raw(T + 21, 34, 531))
        self.assertEqual(end['id'], first['id'])
        self.assertEqual(
            (
                end['performance']['seconds'],
                end['performance']['requests'],
                end['performance']['tokens'],
            ),
            (9, 5, 50),
        )
        intervals = [
            tuple(r)
            for r in self.h.db.execute(
                'SELECT start,end FROM session_ready_intervals ORDER BY start'
            )
        ]
        self.assertEqual(intervals, [(T + 6, T + 12), (T + 18, T + 21)])

    def test_missing_stale_and_reopen_gaps_never_backfill(self):
        first = self.observe(raw())
        self.observe(raw(T + 3, 11, 110))
        self.observe({}, False, at=T + 4)
        self.observe(raw(T + 6, 20, 200))
        self.observe(raw(T + 9, 21, 210))
        a = self.observe(raw(T + 30, 30, 300))
        self.assertEqual(a['performance']['seconds'], 6)
        self.sessions = ProviderSessions(self.h, self.home, Mock(return_value=True))
        a = self.observe(raw(T + 33, 40, 400))
        self.assertEqual(a['id'], first['id'])
        self.assertEqual(a['performance']['seconds'], 6)
        self.assertEqual(a['performance']['requests'], 2)

    def test_cold_idle_detection_still_allows_prewarmer_to_observe_safe_idle(self):
        o = Optimizer(self.h, Mock(), self.home, threading.Event(), Mock())
        o.identity_ok = True
        o.identity_at = T
        o.identity_session = (T - 100, 10)

        def sample(state):
            o.observe('account', state, {'at': state['written_at'], 'provider': {'online': True}})

        o.store.sample = Mock()
        sample(raw())
        sample(raw(T + 3))
        self.assertEqual(o.idle_since, T + 3)
        o.store.sample.assert_not_called()
        o.warmup = proof(raw(), T + 4)
        sample(raw(T + 6, 11, 101))
        sample(raw(T + 9, 13, 121))
        o.store.sample.assert_called_once()
        self.assertEqual(o.store.sample.call_args.args[2:7], (T + 6, T + 9, 'a', 2, 20))
        sample(raw(T + 12, 20, 200, warm_models=[]))
        sample(raw(T + 15, 21, 201))
        sample(raw(T + 18, 22, 211))
        self.assertEqual(o.store.sample.call_count, 2)
        o.runner.assert_not_called()

    def test_missing_counters_are_unknown_or_partial_independently(self):
        first = self.observe(raw(tokens=None))
        self.assertIsNone(first['performance']['tokens'])
        self.observe(raw(T + 3, 11, None))
        self.observe(raw(T + 6, 12, 100))
        a = self.observe(raw(T + 9, 13, 110))['performance']
        self.assertEqual((a['requests'], a['tokens'], a['seconds']), (3, 10, 9))
        self.assertFalse(a['requestsPartial'])
        self.assertTrue(a['tokensPartial'])
        self.observe(raw(T + 12, None, 120))
        self.observe(raw(T + 15, 30, 130))
        a = self.observe(raw(T + 18, 31, 140))['performance']
        self.assertEqual(a['requests'], 4)
        self.assertTrue(a['requestsPartial'])

    def test_legacy_and_partial_warm_minutes_cannot_enter_performance_evidence(self):
        self.h.db.execute(
            'INSERT INTO opt_minutes VALUES(?,?,?,?,?,?,?,?)',
            ('account', 'device', T, 'a', 60, 5, 50, 0),
        )
        self.h.db.execute('INSERT INTO opt_coverage VALUES(?,?,?)', ('account', T, T + 1000))
        self.assertEqual(self.s.evidence('account', 'device', T, T + 1000, T + 1000), {})
        for at in range(T, T + 55, 5):
            self.s.sample('account', 'device', at, at + 5, 'a', 0, 0, False, True)
        self.assertEqual(self.s.evidence('account', 'device', T, T + 1000, T + 1000), {})
        self.s.sample('account', 'device', T + 55, T + 60, 'a', 0, 0, False, False)
        self.assertEqual(self.s.evidence('account', 'device', T, T + 1000, T + 1000), {})
        for at in range(T + 60, T + 120, 5):
            self.s.sample('account', 'device', at, at + 5, 'a', 0, 0, False, True)
        result = self.s.evidence('account', 'device', T, T + 1000, T + 1000)['a']
        self.assertEqual(result['hours'], 1 / 60)
        self.assertEqual(result['usdPerHour'], 0)

    def test_pulse_clips_money_and_time_together_and_keeps_all_confirmed_income_when_cold(self):
        from test_live_earnings import credit

        p = EarningsPulse(self.h, Mock(evidence=Mock(return_value=[])))
        entries = [credit(1, 100, T + 10), credit(2, 999, T + 30), credit(3, 200, T + 50)]
        new = self.h.save_credits('account', entries, T + 60)
        self.s.credits('account', entries, T + 60)
        # Coverage before the first returned credit was already observed.
        self.h.cache('credit-coverage:account', {'start': T, 'end': T + 60})
        p.ingest('account', credit_rows(entries), new, T + 80)
        session = {
            'id': 1,
            'models': ['a'],
            'startedAt': T,
            'status': 'active',
            'endedAt': None,
            'performance': {'status': 'counting', 'segmentStartedAt': T + 40},
        }
        self.h.db.executemany(
            'INSERT INTO session_ready_intervals VALUES(?,?,?)',
            [(1, T, T + 20), (1, T + 40, T + 60)],
        )
        earnings = {'status': 'ok', 'updatedAt': T + 80, 'sourceAsOf': T + 60}
        a = p.snapshot('account', raw(T + 80), session, earnings, 'connection', T + 80)
        self.assertEqual(a['windows']['60']['seconds'], 40)
        self.assertEqual(a['windows']['60']['microUsd'], 300)
        self.assertAlmostEqual(a['windows']['60']['ratePerHour'], 0.0003 * 90)
        self.assertEqual(a['sessionMicroUsd'], 1299)
        session['performance']['status'] = 'paused'
        b = p.snapshot('account', raw(T + 81), session, earnings, 'connection', T + 81)
        self.assertEqual(b['status'], 'paused')
        self.assertIsNone(b['windows']['60']['ratePerHour'])
        self.assertEqual(b['events'], [])
        self.assertEqual(b['sessionMicroUsd'], 1299)
        # A newly delivered credit from the excluded cold gap stays real money
        # but cannot animate as current warm work after readiness resumes.
        session['performance']['status'] = 'counting'
        entries.append(credit(4, 500, T + 30))
        new = self.h.save_credits('account', entries, T + 60)
        self.s.credits('account', entries, T + 60)
        p.ingest('account', credit_rows(entries), new, T + 90)
        earnings['updatedAt'] = T + 90
        c = p.snapshot('account', raw(T + 90), session, earnings, 'connection', T + 90)
        self.assertEqual(c['events'], [])
        self.assertEqual(c['sessionMicroUsd'], 1799)

    def test_cold_models_suppress_cached_projections_without_removing_confirmed_earnings(self):
        from test_model_projection import minutes

        p = ModelProjection(self.s)
        p.cached = minutes()
        p.cache_key = ('account', device_id(raw()), ('a',))
        p.cache_at = T
        monitor = {
            'status': 'ok',
            'observedAt': T,
            'hours': [{'at': T, 'usd': 0.5, 'jobs': 3, 'categories': {}}],
        }
        provider = {'online': True, 'tracking': {'counting': False}}
        self.assertEqual(
            p.estimate('account', raw(), provider, monitor, {'status': 'ok', 'updatedAt': T}, T)[
                'status'
            ],
            'unavailable',
        )
        f = forecast(T, monitor, provider, [{'at': T - 60, 'rate': 10, 'n': 60}], [])
        self.assertEqual(f['earnings']['actual'], 0.5)
        self.assertEqual(f['earnings']['status'], 'paused')
        self.assertEqual(f['throughput']['status'], 'paused')

    def test_reputation_rebaselines_after_cold_interval_and_preserves_official_counter_scope(self):
        (self.home / '.darkbloom').mkdir()

        def observe(at, ready=True):
            r = raw(at)
            (self.home / '.darkbloom/daemon-state.json').write_text(json.dumps(r))
            return self.observe(r, ready)

        def rep(at, total, requested=None):
            return self.sessions.reputation_observation(
                {'score': 0.9, 'providerStatus': 'online', 'totalJobs': total},
                at if requested is None else requested,
                'connection',
                ['a'],
                'connection',
                at,
            )

        observe(T)
        self.assertTrue(rep(T, 100))
        observe(T + 3)
        rep(T + 3, 105)
        self.assertEqual(self.sessions.snapshot(T + 3)['current']['reputation']['totalJobs'], 5)
        observe(T + 6, False)
        self.assertFalse(rep(T + 6, 110))
        observe(T + 9)
        self.assertFalse(rep(T + 9, 120, T + 7))
        self.assertTrue(rep(T + 9, 120))
        result = self.sessions.snapshot(T + 9)['current']['reputation']
        self.assertEqual(result['totalJobs'], 0)
        self.assertEqual(result['baselineReason'], 'warm_resumed')


if __name__ == '__main__':
    unittest.main()
