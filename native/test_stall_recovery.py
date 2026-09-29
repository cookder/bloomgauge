"""Work that stops abruptly: detection, the recovery ladder and its controls."""

import copy
import io
import json
import unittest
import urllib.error
from types import SimpleNamespace
from unittest.mock import Mock, patch
import stall_recovery as sr
from stall_control import StallControl, self_route, api_key
from demand_optimizer import decide, policy, DemandOptimizer
from history import History
from optimizer_store import OptimizerStore
import test_demand_controller as controller_tests
from test_demand_targets import options
from test_demand_fallback import MODEL
from test_demand_optimizer import NOW as DECISION_NOW

T0 = 1_800_000_000  # work stops here (a whole minute)


def flow(model='a', minutes=30, jobs=10, end=T0):
    return [
        {'at': end - (minutes - i) * 60, 'model': model, 'seconds': 60, 'jobs': jobs}
        for i in range(minutes)
    ]


def silent(model='a', start=T0, minutes=60):
    return [
        {'at': start + i * 60, 'model': model, 'seconds': 60, 'jobs': 0} for i in range(minutes)
    ]


def network(model='a', start=T0 - 1800, end=T0 + 3600, active=100, warm=100, drop_at=None):
    return {
        model: [
            {
                'at': at,
                'active': active / 4 if drop_at and at >= drop_at else active,
                'queued': 0,
                'warm': warm,
            }
            for at in range(start, end, 30)
        ]
    }


def check(now, attempts=(), switches=(), minutes=None, net=None, session=None, probe=True):
    rows = minutes if minutes is not None else flow() + silent()
    return sr.assess(
        [m for m in rows if m['at'] + 60 <= now],
        net or network(),
        list(attempts),
        list(switches),
        session,
        now,
        probe,
    )


class AssessTests(unittest.TestCase):
    def test_short_gaps_are_routine(self):
        self.assertIsNone(check(T0 + 299)['step'])
        r = check(T0 + 300)
        self.assertEqual(r['step'], 'probe')
        self.assertTrue(r['demandHeld'])
        self.assertAlmostEqual(r['baselineJobsPerMinute'], 10)

    def test_missing_demand_readings_are_unknown_not_a_collapse(self):
        for net in ({'b': []}, {'a': []}, network(start=T0 - 1800, end=T0 - 600)):
            r = check(T0 + 300, net=net)
            self.assertIsNone(r['demandHeld'])
            self.assertEqual(r['step'], 'probe')

    def test_sleep_or_a_closed_app_is_not_silence(self):
        # Work stops, the Mac sleeps 25 minutes, then wakes with the model warm.
        woke = T0 + 25 * 60
        rows = flow() + silent(start=woke, minutes=10)
        self.assertIsNone(check(woke + 60, minutes=rows)['step'])
        self.assertEqual(check(woke + 300, minutes=rows)['step'], 'probe')
        attempts = [{'at': woke + 300, 'step': 'probe'}]
        # Restart needs 8 observed minutes, not 8 wall minutes since work stopped.
        self.assertIsNone(check(woke + 479, attempts, minutes=rows)['step'])
        self.assertEqual(check(woke + 480, attempts, minutes=rows)['step'], 'restart')

    def test_a_restart_stamped_after_its_own_switch_keeps_the_ladder(self):
        attempts = [{'at': T0 + 300, 'step': 'probe'}, {'at': T0 + 488, 'step': 'restart'}]
        # The switch began 8 s before the restart was recorded (slow CLI check).
        r = check(T0 + 488 + 400, attempts, switches=[T0 + 480], session=T0 + 540)
        self.assertNotEqual(r['status'], 'switching')
        self.assertEqual(r['step'], 'escape')

    def test_slow_or_patchy_work_is_never_a_stall(self):
        self.assertEqual(check(T0 + 1800, minutes=flow(jobs=3) + silent())['status'], 'quiet')
        patchy = [{**m, 'jobs': 30 if i % 3 == 0 else 0} for i, m in enumerate(flow())]
        self.assertEqual(check(T0 + 1800, minutes=patchy + silent())['status'], 'quiet')

    def test_ladder_waits_between_steps(self):
        attempts = [{'at': T0 + 300, 'step': 'probe'}]
        self.assertIsNone(check(T0 + 479, attempts)['step'])
        self.assertEqual(check(T0 + 480, attempts)['step'], 'restart')
        attempts.append({'at': T0 + 480, 'step': 'restart'})
        # Five minutes from the moment the new session started, not the request.
        self.assertIsNone(
            check(T0 + 480 + 300, attempts, switches=[T0 + 485], session=T0 + 540)['step']
        )
        self.assertEqual(
            check(T0 + 540 + 300, attempts, switches=[T0 + 485], session=T0 + 540)['step'], 'escape'
        )
        attempts.append({'at': T0 + 840, 'step': 'escape'})
        self.assertIsNone(check(T0 + 840 + 299, attempts)['step'])
        # With no switch yet, the optimizer gets 20 minutes to move (its own
        # confirmation takes 5), not 5.
        self.assertIsNone(check(T0 + 840 + 1199, attempts)['step'])
        r = check(T0 + 840 + 1200, attempts)
        self.assertEqual(r['step'], 'hold')
        self.assertIn('stopped trying', r['reason'])
        # No switch followed the escape, so the notice must not claim one.
        self.assertIn('a chance to switch models', r['reason'])
        # A switch after the escape: 5 minutes on the new model, then hold.
        moved = dict(switches=[T0 + 1000], session=T0 + 1010)
        self.assertIsNone(check(T0 + 1010 + 299, attempts, **moved)['step'])
        r = check(T0 + 1010 + 300, attempts, **moved)
        self.assertEqual(r['step'], 'hold')
        self.assertIn('a switch to another model', r['reason'])
        attempts.append({'at': T0 + 2040, 'step': 'hold'})
        self.assertIsNone(check(T0 + 2400, attempts)['step'])

    def test_demand_drop_goes_straight_to_another_model(self):
        r = check(T0 + 300, net=network(drop_at=T0))
        self.assertFalse(r['demandHeld'])
        self.assertEqual(r['step'], 'escape')

    def test_probe_job_does_not_end_the_episode_but_real_work_does(self):
        rows = flow() + silent()
        attempts = [{'at': T0 + 300, 'step': 'probe'}]
        rows[30 + 5]['jobs'] = 1
        self.assertEqual(check(T0 + 480, attempts, minutes=rows)['step'], 'restart')
        rows[30 + 6]['jobs'] = 4
        self.assertEqual(check(T0 + 480, attempts, minutes=rows)['status'], 'ok')

    def test_another_switch_owns_its_own_quiet_time(self):
        r = check(T0 + 600, switches=[T0 + 200])
        self.assertEqual(r['status'], 'switching')
        self.assertIsNone(r['step'])

    def test_restart_budget_and_missing_probe(self):
        self.assertEqual(
            check(T0 + 300, probe=False)['step'], None
        )  # a restart waits for 8 minutes
        self.assertEqual(check(T0 + 480, probe=False)['step'], 'restart')
        earlier = [{'at': T0 - 3000, 'step': 'restart'}]
        self.assertEqual(
            check(T0 + 480, earlier + [{'at': T0 + 300, 'step': 'probe'}])['step'], 'escape'
        )
        spaced = [{'at': T0 - 80000 + i * 4000, 'step': 'restart'} for i in range(3)]
        self.assertEqual(
            check(T0 + 480, spaced + [{'at': T0 + 300, 'step': 'probe'}])['step'], 'escape'
        )

    def test_hours_old_silence_is_ordinary_quiet(self):
        rows = flow() + silent(minutes=240)
        self.assertEqual(
            check(T0 + 4 * 3600, minutes=rows, net=network(end=T0 + 5 * 3600))['status'], 'quiet'
        )


class FakeStore:
    def __init__(self, h):
        self.h = h
        self.events = []

    def event(self, account, device, at, kind, model, detail, downtime=0):
        self.events.append((kind, model, detail))
        OptimizerStore.event(self, account, device, at, kind, model, detail, downtime)


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.h = History(':memory:')
        OptimizerStore(self.h)
        self.store = FakeStore(self.h)
        self.o = SimpleNamespace(
            store=self.store,
            home='/tmp',
            lock=__import__('threading').RLock(),
            detail='',
            last_demand_decision=None,
            tracking=lambda raw, now, cleared=True: {'counting': True},
            dispatch_stall_restart=Mock(return_value=True),
        )
        for m in flow() + silent(minutes=20):
            self.h.db.execute(
                'INSERT INTO opt_ready_minutes VALUES(?,?,?,?,?,?,?,?)',
                ('acct', 'mac', m['at'], m['model'], 60, m['jobs'], 0, 0),
            )
        for row in network(end=T0 + 1200)['a']:
            self.h.db.execute(
                'INSERT OR REPLACE INTO opt_network VALUES(?,?,?,?,?,?)',
                (row['at'], 'a', row['active'], 0, row['warm'], 100),
            )
        self.settings = {'mode': 'demand'}
        self.live = {'account': 'acct', 'device': 'mac'}
        self.raw = {'started_at': T0 - 9000, 'pid': 1}

    def tearDown(self):
        self.h.close()

    def control(self, key='secret-key'):
        self.route = Mock(
            return_value={'kind': 'self-route', 'ok': True, 'code': 'served', 'httpStatus': 200}
        )
        self.local = Mock()
        return StallControl(self.o, keychain=lambda: key, route=self.route, local=self.local)

    def test_probe_uses_self_route_when_a_key_is_stored(self):
        c = self.control()
        self.assertFalse(c.tick(T0 + 300, self.settings, self.live, self.raw, 'a', [], {}))
        for _ in range(50):
            if not c.nudging:
                break
            __import__('time').sleep(0.02)
        self.route.assert_called_once_with('a', 'secret-key')
        self.local.assert_not_called()
        kinds = [e[0] for e in self.store.events]
        self.assertEqual(kinds, ['stall-probe', 'stall-nudge'])
        self.assertNotIn('secret-key', json.dumps(self.store.events))
        self.assertIn('test request', self.o.detail)
        self.assertEqual(self.store.events[-1][2], 'Test request through Darkbloom was served.')

    def test_probe_falls_back_to_the_local_engine_without_a_key(self):
        c = self.control(key=None)
        c.tick(T0 + 300, self.settings, self.live, self.raw, 'a', ['--local-endpoint'], {})
        for _ in range(50):
            if not c.nudging:
                break
            __import__('time').sleep(0.02)
        self.route.assert_not_called()
        self.local.assert_called_once()
        self.assertEqual(
            self.store.events[-1][2],
            'Test request to the local engine (no API key stored) was served.',
        )
        self.assertEqual(c.snapshot(T0 + 301)['nudge']['kind'], 'local')

    def test_restart_is_dispatched_then_confirmed_once(self):
        c = self.control()
        self.store.event('acct', 'mac', T0 + 300, 'stall-probe', 'a', 'probe')
        self.assertTrue(c.tick(T0 + 480, self.settings, self.live, self.raw, 'a', [], {}))
        self.o.dispatch_stall_restart.assert_called_once()
        decision = c.confirm_restart('acct', 'mac', self.raw, 'a', policy(), T0 + 485)
        self.assertEqual(decision['kind'], 'recovery')
        self.assertEqual(decision['target'], 'a')
        self.assertIsNone(c.confirm_restart('acct', 'mac', self.raw, 'a', policy(), T0 + 486))
        self.assertIsNone(c.confirm_restart('acct', 'mac', self.raw, 'b', policy(), T0 + 487))

    def test_inactive_outside_demand_mode_or_during_a_trial(self):
        c = self.control()
        for settings, trial in (
            ({'mode': 'observe'}, None),
            ({'mode': 'demand', 'pending': {'model': 'b'}}, None),
            (self.settings, {'current': True, 'complete': False}),
        ):
            self.o.last_demand_decision = {'trial': trial} if trial else None
            self.assertFalse(c.tick(T0 + 600, settings, self.live, self.raw, 'a', [], {}))
            self.assertEqual(c.snapshot(T0 + 600)['status'], 'inactive')
        self.assertEqual(self.store.events, [])

    def test_escape_flag_and_hold_notice(self):
        self.settings['demandPolicy'] = {'managerStrategy': 0}  # the legacy escape
        c = self.control()
        for at, step in ((T0 + 300, 'probe'), (T0 + 480, 'restart')):
            self.store.event('acct', 'mac', at, 'stall-' + step, 'a', step)
        c.tick(T0 + 1100, self.settings, self.live, self.raw, 'a', [], {})
        self.assertTrue(c.escape_active(T0 + 1110))
        self.assertFalse(c.escape_active(T0 + 1200))  # stale status never keeps the escape open
        # No switch yet: the escape stays open while the optimizer confirms a move.
        c.tick(T0 + 1100 + 300, self.settings, self.live, self.raw, 'a', [], {})
        self.assertTrue(c.escape_active(T0 + 1400))
        c.tick(T0 + 1100 + 1200, self.settings, self.live, self.raw, 'a', [], {})
        self.assertFalse(c.escape_active(T0 + 2300))
        push = Mock()
        push.enqueue_notice.return_value = True
        c.send_pending('acct', 'mac', push, T0 + 2350)
        c.send_pending('acct', 'mac', push, T0 + 2360)
        push.enqueue_notice.assert_called_once()
        self.assertIn('stopped trying', push.enqueue_notice.call_args[0][3])


class RequestTests(unittest.TestCase):
    def opener(self, response=None, error=None):
        def open_(request, timeout):
            self.request = request
            if error:
                raise error
            return io.BytesIO(json.dumps(response).encode())

        return SimpleNamespace(open=open_)

    def test_self_route_request_and_reply_codes(self):
        result = self_route(
            'gemma', 'k', opener=self.opener({'choices': [{'message': {'content': 'hi'}}]})
        )
        self.assertEqual(
            result, {'kind': 'self-route', 'ok': True, 'httpStatus': 200, 'code': 'served'}
        )
        self.assertEqual(self.request.get_header('X-darkbloom-route'), 'self')
        self.assertEqual(json.loads(self.request.data)['max_tokens'], 5)
        body = io.BytesIO(b'{"error":{"code":"model_not_loaded","message":"stale"}}')
        error = urllib.error.HTTPError('u', 409, 'conflict', {}, body)
        self.assertEqual(
            self_route('gemma', 'k', opener=self.opener(error=error))['code'], 'model_not_loaded'
        )
        self.assertEqual(
            self_route('gemma', 'k', opener=self.opener(error=OSError()))['code'], 'unreachable'
        )
        self.assertEqual(
            self_route('gemma', 'k', opener=self.opener(error=__import__('socket').timeout()))[
                'code'
            ],
            'timeout',
        )
        wrapped = urllib.error.URLError(__import__('socket').timeout('timed out'))
        self.assertEqual(
            self_route('gemma', 'k', opener=self.opener(error=wrapped))['code'], 'timeout'
        )

    def test_default_opener_never_follows_redirects_or_uses_proxies(self):
        import stall_control

        built = []
        real = urllib.request.build_opener

        def capture(*handlers):
            built.extend(handlers)
            return self.opener(error=urllib.error.HTTPError('u', 302, 'found', {}, io.BytesIO(b'')))

        with unittest.mock.patch.object(stall_control.urllib.request, 'build_opener', capture):
            self.assertEqual(self_route('gemma', 'k')['code'], 'http-302')
        proxies = [h for h in built if isinstance(h, urllib.request.ProxyHandler)]
        self.assertEqual([h.proxies for h in proxies], [{}])
        redirect = next(h for h in built if isinstance(h, urllib.request.HTTPRedirectHandler))
        self.assertIsNone(
            redirect.redirect_request(None, None, 302, 'found', {}, 'https://elsewhere.example')
        )
        self.assertIs(urllib.request.build_opener, real)

    def test_keychain_lookup(self):
        ok = Mock(return_value=SimpleNamespace(returncode=0, stdout='abc123\n'))
        self.assertEqual(api_key(ok), 'abc123')
        self.assertEqual(ok.call_args[0][0][-2:], ['bloom-darkbloom-api-key', '-w'])
        self.assertIsNone(api_key(Mock(return_value=SimpleNamespace(returncode=44, stdout=''))))
        self.assertIsNone(api_key(Mock(side_effect=OSError())))


class DecisionTests(unittest.TestCase):
    def test_stall_escape_opens_the_idle_escape_and_ignores_lagged_pay(self):
        r = options(0.07)
        r[2]['selected'] = False
        r[-1]['selected'] = False
        args = (
            r,
            'a',
            {
                'a': {
                    'rate': 0.07,
                    'lower': 0.06,
                    'upper': 0.08,
                    'hours': 30,
                    'days': 4,
                    'blocks': 30,
                    'scope': 'weekday_time',
                    'forecastUsable': True,
                    'asOf': DECISION_NOW - 120,
                    'recent': True,
                }
            },
            [],
            [],
            policy(),
            DECISION_NOW,
        )
        held = decide(*args, activity={'fresh': True, 'idleSeconds': 0})
        self.assertIsNone(held['target'])
        self.assertFalse(held['stallEscape'])
        stalled = decide(*args, activity={'fresh': True, 'idleSeconds': 0}, stall_escape=True)
        self.assertEqual(stalled['target'], MODEL)
        self.assertTrue(stalled['escapeReady'])
        self.assertTrue(stalled['stallEscape'])
        self.assertIn('Work stopped abruptly', stalled['reason'])
        self.assertIsNone(
            decide(*args, activity={'fresh': False, 'idleSeconds': 0}, stall_escape=True)['target']
        )

    def test_recovery_run_counts_toward_the_daily_budget(self):
        h = History(':memory:')
        try:
            d = DemandOptimizer(h, OptimizerStore(h))
            decision = {
                'at': T0,
                'kind': 'recovery',
                'target': 'a',
                'policy': policy({'maxSwitchesPerDay': 2}),
                'reason': 'x',
                'stall': {},
            }
            run = d.begin_recovery('acct', 'mac', decision, T0)
            d.finish(run, 'acct', 'mac', T0 + 60, 'switched', 50, None)
            self.assertEqual(d.runs('acct', 'mac', T0 + 60)[0]['decision']['kind'], 'recovery')
            d.begin_recovery('acct', 'mac', {**decision, 'at': T0 + 100}, T0 + 100)
            with self.assertRaises(ValueError):
                d.begin_recovery('acct', 'mac', {**decision, 'at': T0 + 200}, T0 + 200)
        finally:
            h.close()


class SwitchTests(unittest.TestCase):
    setUp = controller_tests.DemandControllerTests.setUp
    tearDown = controller_tests.DemandControllerTests.tearDown
    setup_demand = controller_tests.DemandControllerTests.setup_demand
    advance = controller_tests.DemandControllerTests.advance

    def restart_result(self):
        return {
            'step': 'restart',
            'model': 'a',
            'status': 'stalled',
            'reason': 'Still no work while network demand held.',
            'silenceSeconds': 600,
            'baselineJobsPerMinute': 12,
            'demandHeld': True,
            'episodeStart': self.now - 600,
            'taken': ['probe'],
        }

    def test_tick_dispatches_a_same_model_restart_before_any_demand_decision(self):
        self.setup_demand()
        self.o.switch = Mock()
        self.o.stall.evaluate = Mock(return_value=self.restart_result())
        self.advance(0)
        self.o.switch.assert_called_once_with('a', 'a', 'acct', self.live['device'])
        self.assertEqual(self.o.state['pending']['demandKind'], 'stall-restart')
        self.o.demand_decision.assert_not_called()

    def test_guarded_restart_runs_on_the_same_model_without_a_purge(self):
        self.setup_demand()
        self.o.state['pending'] = {
            'model': 'a',
            'previous': 'a',
            'kind': 'demand',
            'demandKind': 'stall-restart',
        }
        self.o.stall.evaluate = Mock(return_value=self.restart_result())
        self.o.purge_before_load = Mock(return_value=False)
        self.o.command = Mock()
        self.o.verify_started = Mock(return_value=True)
        self.o.switch('a', 'a', 'acct', self.live['device'])
        self.o.command.assert_called_once_with('a', ['--local-endpoint', '--port', '8000'], {})
        self.o.purge_before_load.assert_not_called()
        self.assertEqual(self.o.state['mode'], 'demand')
        runs = self.o.demand_auto.runs('acct', self.live['device'], self.now + 60)
        self.assertEqual(runs[0]['decision']['kind'], 'recovery')
        with self.h.lock:
            kinds = [r[0] for r in self.h.db.execute('SELECT kind FROM opt_events ORDER BY id')]
            detail = self.h.db.execute(
                "SELECT detail FROM opt_events WHERE kind='switching'"
            ).fetchone()[0]
        self.assertEqual(kinds[:3], ['stall-restart', 'switching', 'switched'])
        self.assertIn('same model', detail)

    def test_restart_is_dropped_when_work_resumed(self):
        self.setup_demand()
        self.o.state['pending'] = {
            'model': 'a',
            'previous': 'a',
            'kind': 'demand',
            'demandKind': 'stall-restart',
        }
        self.o.stall.evaluate = Mock(
            return_value={**self.restart_result(), 'step': None, 'status': 'ok'}
        )
        self.o.command = Mock()
        self.o.switch('a', 'a', 'acct', self.live['device'])
        self.o.command.assert_not_called()
        self.assertEqual(self.o.state['mode'], 'demand')
        self.assertNotIn('pending', self.o.state)


if __name__ == '__main__':
    unittest.main()
