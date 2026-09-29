"""Native update reservations, with synthetic state and no provider commands."""

import copy
import json
import threading
from types import SimpleNamespace
import unittest
import urllib.error
import uuid
from unittest.mock import Mock, patch

from update_guard import UpdateGuard, UpdateBlocked
from optimizer import Optimizer
import test_optimizer
import test_remote


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        self.o = SimpleNamespace(
            lock=threading.RLock(),
            command_lock=threading.Lock(),
            stop=threading.Event(),
            state={'mode': 'demand'},
            worker=None,
            warmup_worker=None,
        )
        self.guard = UpdateGuard(self.o, lambda: self.now)
        self.request_id = str(uuid.uuid4())

    def prepare(self):
        return self.guard.action({'action': 'prepare', 'requestId': self.request_id})

    def test_idle_reservation_commit_release_preserves_plan(self):
        original = copy.deepcopy(self.o.state)
        prepared = self.prepare()
        self.assertTrue(prepared['ready'])
        self.assertTrue(self.guard.active())
        committed = self.guard.action({'action': 'commit', 'lease': prepared['lease']})
        self.assertTrue(committed['ready'])
        self.assertEqual(committed['expiresInSeconds'], 30)
        self.assertEqual(
            self.guard.action({'action': 'release', 'lease': prepared['lease']}), {'released': True}
        )
        self.assertFalse(self.guard.active())
        self.assertEqual(self.o.state, original)

    def test_busy_operations_refuse_without_stopping_or_changing_state(self):
        for kind in ('pending', 'queued', 'switch-worker', 'warm-worker', 'command', 'stopping'):
            with self.subTest(kind=kind):
                self.setUp()
                if kind == 'pending':
                    self.o.state['pending'] = {'model': 'synthetic'}
                if kind == 'queued':
                    self.o.state['requestedModel'] = 'synthetic'
                if kind == 'switch-worker':
                    self.o.worker = Mock(is_alive=lambda: True)
                if kind == 'warm-worker':
                    self.o.warmup_worker = Mock(is_alive=lambda: True)
                if kind == 'command':
                    self.o.command_lock.acquire()
                if kind == 'stopping':
                    self.o.stop.set()
                before = copy.deepcopy(self.o.state)
                with self.assertRaises(UpdateBlocked):
                    self.prepare()
                self.assertFalse(self.guard.active())
                self.assertEqual(self.o.state, before)

    def test_lost_reply_cancellation_and_stale_lease_never_block_forever(self):
        prepared = self.prepare()
        self.now += 20
        repeated = self.prepare()
        self.assertEqual(repeated['lease'], prepared['lease'])
        self.assertEqual(repeated['expiresInSeconds'], 40)
        with self.assertRaises(UpdateBlocked):
            self.guard.action({'action': 'prepare', 'requestId': str(uuid.uuid4())})
        self.assertFalse(
            self.guard.action({'action': 'release', 'lease': str(uuid.uuid4())})['released']
        )
        self.now += 40
        self.assertFalse(self.guard.active())
        with self.assertRaises(UpdateBlocked):
            self.guard.action({'action': 'commit', 'lease': prepared['lease']})
        self.assertNotEqual(self.prepare()['lease'], prepared['lease'])

    def test_repeated_commit_does_not_extend_inhibition(self):
        lease = self.prepare()['lease']
        self.guard.action({'action': 'commit', 'lease': lease})
        self.now += 20
        self.assertEqual(
            self.guard.action({'action': 'commit', 'lease': lease})['expiresInSeconds'], 10
        )
        self.now += 10
        self.assertFalse(self.guard.active())

    def test_commit_rechecks_busy_state(self):
        lease = self.prepare()['lease']
        self.o.state['pending'] = {'model': 'unexpected'}
        with self.assertRaises(UpdateBlocked):
            self.guard.action({'action': 'commit', 'lease': lease})

    def test_payload_is_strict(self):
        for data in (
            None,
            [],
            {},
            {'action': 'prepare', 'requestId': 'x'},
            {'action': 'prepare', 'requestId': self.request_id, 'force': True},
            {'action': 'commit', 'lease': True},
            {'action': 'release', 'lease': '../x'},
        ):
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.guard.action(data)

    def automatic(self):
        return self.guard.action(
            {'action': 'prepare', 'requestId': self.request_id, 'automatic': True}
        )

    SOFT_HOLDS = (
        ('excursion', lambda o: o.state.update(manager={'excursion': {'target': 'big'}})),
        ('recovery', lambda o: o.state.update(manager={'recovery': {'attempts': 0}})),
        ('resume', lambda o: o.state.update(manager={'resume': {'id': 'r'}})),
        (
            'trial',
            lambda o: setattr(
                o, 'last_demand_decision', {'trial': {'current': True, 'status': 'running'}}
            ),
        ),
        (
            'settling-trial',
            lambda o: setattr(
                o, 'last_demand_decision', {'trial': {'current': True, 'status': 'settling'}}
            ),
        ),
    )

    def test_automatic_install_waits_for_trials_and_recovery_a_person_does_not(self):
        for name, apply in self.SOFT_HOLDS:
            with self.subTest(hold=name):
                self.setUp()
                apply(self.o)
                before = copy.deepcopy(self.o.state)
                with self.assertRaises(UpdateBlocked) as blocked:
                    self.automatic()
                self.assertFalse(self.guard.active())
                self.assertEqual(self.o.state, before)
                self.assertIn(blocked.exception.reason, ('excursion', 'manager-recovery', 'trial'))
                # Install and Relaunch chosen by a person is not held by these.
                self.assertTrue(self.prepare()['ready'])

    def test_automatic_install_ignores_finished_trials_and_idle_manager(self):
        self.o.state['manager'] = {'home': {'model': 'gemma'}, 'excursion': {'target': None}}
        self.o.last_demand_decision = {'trial': {'current': False, 'status': 'settling'}}
        # A finished run (not current) and an interrupted one hold nothing.
        lease = self.automatic()['lease']
        self.assertTrue(self.guard.action({'action': 'commit', 'lease': lease})['ready'])

    def test_automatic_commit_rechecks_soft_holds(self):
        lease = self.automatic()['lease']
        self.o.state['manager'] = {'excursion': {'target': 'big'}}
        with self.assertRaises(UpdateBlocked):
            self.guard.action({'action': 'commit', 'lease': lease})
        # A person's lease is not held back by an excursion.
        self.guard.action({'action': 'release', 'lease': lease})
        self.request_id = str(uuid.uuid4())
        lease = self.prepare()['lease']
        self.assertTrue(self.guard.action({'action': 'commit', 'lease': lease})['ready'])

    def test_quiet_probe_reservation_carries_through_to_termination(self):
        # Updates.swift keeps the probe's lease and reuses its requestId for the
        # termination admission: prepare is idempotent, so the same lease comes back
        # and no model work can start in between.
        probe = self.automatic()
        self.now += 1
        with self.assertRaises(ValueError):
            self.guard.require_available()
        again = self.automatic()
        self.assertEqual(again['lease'], probe['lease'])
        self.assertTrue(self.guard.action({'action': 'commit', 'lease': probe['lease']})['ready'])

    def test_automatic_flag_is_strict(self):
        lease = None
        for data in (
            {'action': 'prepare', 'requestId': self.request_id, 'automatic': False},
            {'action': 'prepare', 'requestId': self.request_id, 'automatic': 1},
            {'action': 'prepare', 'requestId': self.request_id, 'automatic': 'true'},
            {'action': 'commit', 'lease': str(uuid.uuid4()), 'automatic': True},
            {'action': 'release', 'lease': str(uuid.uuid4()), 'automatic': True},
        ):
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.guard.action(data)
        self.assertFalse(self.guard.active())
        # A retried prepare cannot switch between automatic and manual.
        lease = self.automatic()['lease']
        with self.assertRaises(ValueError):
            self.prepare()
        self.guard.action({'action': 'release', 'lease': lease})
        self.assertFalse(self.guard.automatic)

    def test_reservation_and_worker_admission_share_one_lock(self):
        entered, finished = threading.Event(), threading.Event()
        outcome = []

        def reserve():
            entered.set()
            try:
                self.prepare()
            except UpdateBlocked as error:
                outcome.append(error.reason)
            finished.set()

        with self.o.lock:
            worker = threading.Thread(target=reserve)
            worker.start()
            self.assertTrue(entered.wait(1))
            self.assertFalse(finished.is_set())
            self.o.state['pending'] = {'model': 'new'}
        self.assertTrue(finished.wait(2))
        worker.join(2)
        self.assertEqual(outcome, ['model-switch'])
        self.assertFalse(self.guard.active())


class AdmissionTests(unittest.TestCase):
    tearDown = test_optimizer.ControllerTests.tearDown
    manual_payload = test_optimizer.ControllerTests.manual_payload

    def setUp(self):
        test_optimizer.ControllerTests.setUp(self)
        self.o.tick_prewarm = Optimizer.tick_prewarm.__get__(self.o)

    def prepare(self):
        return self.o.update_guard.action({'action': 'prepare', 'requestId': str(uuid.uuid4())})

    def test_reserved_update_blocks_new_manual_and_automatic_work(self):
        lease = self.prepare()['lease']
        before = copy.deepcopy(self.o.state)
        with self.assertRaisesRegex(ValueError, 'preparing an update'):
            self.o.manual_action(self.manual_payload(), 'phone')
        with self.assertRaisesRegex(ValueError, 'preparing an update'):
            self.o.control_action({'action': 'restore'})
        self.o.tick(self.now)
        self.assertFalse(self.o.tick_prewarm(self.now))
        self.assertEqual(
            [
                c.args[0]
                for c in self.o.runner.call_args_list
                if c.args[0] != ['/usr/bin/sudo', '-n', '-ll', '/usr/sbin/purge']
            ],
            [],
        )
        self.assertEqual(self.o.state, before)
        self.o.update_guard.action({'action': 'release', 'lease': lease})
        self.o.manual_action(self.manual_payload(), 'phone')
        self.assertEqual(self.o.state['requestedModel'], 'b')

    def test_final_prewarm_admission_rechecks_reservation(self):
        self.o.raw.update(warm_models=[], advertised_models=['a'])
        self.o.warmup = {}
        self.o.served_warm = Mock(return_value=False)
        self.o.idle_since = self.now - 30

        def during_readiness(*args, **kwargs):
            self.prepare()
            return None

        self.o.prewarm_reason = Mock(side_effect=during_readiness)
        with patch('optimizer.local_request'), patch('optimizer.threading.Thread') as thread:
            self.assertFalse(self.o.tick_prewarm(self.now))
            thread.assert_not_called()
        self.o.runner.assert_not_called()

    def test_demand_decision_in_flight_cannot_dispatch_after_reservation(self):
        self.o.state['mode'] = 'demand'
        self.o.tracking = Mock(return_value={'counting': True})

        def decision(*args):
            self.prepare()
            return {'target': 'b', 'policy': {}, 'reason': 'fixture'}

        self.o.demand_decision = Mock(side_effect=decision)
        with patch('optimizer.threading.Thread') as thread:
            self.o.tick_demand(
                self.now, copy.deepcopy(self.o.state), self.live, self.o.raw, 'a', [], {}
            )
            thread.assert_not_called()
        self.assertFalse(self.o.state.get('pending'))
        self.o.runner.assert_not_called()

    def test_final_scheduled_admission_rechecks_reservation(self):
        budget = self.o.selection_budget

        def during_budget(*args):
            result = budget(*args)
            self.prepare()
            return result

        self.o.selection_budget = Mock(side_effect=during_budget)
        self.o.switch = Mock()
        self.o.tick(self.now)
        self.o.selection_budget.assert_called_once()
        self.o.switch.assert_not_called()
        self.assertFalse(self.o.state.get('pending'))
        self.o.runner.assert_not_called()


class UpdateHTTPTests(unittest.TestCase):
    setUp = test_remote.RemoteHTTPTests.setUp
    tearDown = test_remote.RemoteHTTPTests.tearDown
    read = test_remote.RemoteHTTPTests.read

    def request(self, server, headers=None, data=None):
        return self.read(
            server,
            '/api/update/native',
            headers,
            json.dumps(data or {'action': 'prepare', 'requestId': str(uuid.uuid4())}).encode(),
        )

    def test_native_secret_local_origin_and_action_are_all_required(self):
        collector = self.local.RequestHandlerClass.collector
        collector.reputation.native_token = 'synthetic-native-token'
        headers = {
            'Content-Type': 'application/json',
            'X-Bloom-Action': 'update',
            'X-Bloom-Native': 'synthetic-native-token',
        }
        for server, denied in (
            (self.local, {k: v for k, v in headers.items() if k != 'X-Bloom-Native'}),
            (self.local, {**headers, 'X-Bloom-Action': 'optimizer'}),
            (self.local, {**headers, 'Origin': 'https://foreign.example'}),
            (
                self.phone,
                {**headers, **self.headers, 'Origin': 'https://' + test_remote.HOST + ':8443'},
            ),
        ):
            with self.assertRaises(urllib.error.HTTPError) as result:
                self.request(server, denied)
            self.assertEqual(result.exception.code, 403)
            self.assertFalse(collector.optimizer.update_guard.active())
        with self.request(self.local, headers) as response:
            reply = json.load(response)
            self.assertTrue(reply['ready'])
        with self.request(
            self.local, headers, {'action': 'commit', 'lease': reply['lease']}
        ) as response:
            self.assertTrue(json.load(response)['ready'])
        with self.request(
            self.local, headers, {'action': 'release', 'lease': reply['lease']}
        ) as response:
            self.assertTrue(json.load(response)['released'])

    def test_busy_and_invalid_requests_fail_closed_preview_only_reserves(self):
        collector = self.local.RequestHandlerClass.collector
        collector.reputation.native_token = 'synthetic-native-token'
        headers = {
            'Content-Type': 'application/json',
            'X-Bloom-Action': 'update',
            'X-Bloom-Native': 'synthetic-native-token',
        }
        collector.optimizer.state['pending'] = {'model': 'fixture'}
        with self.assertRaises(urllib.error.HTTPError) as result:
            self.request(self.local, headers)
        self.assertEqual(result.exception.code, 409)
        self.assertEqual(json.load(result.exception)['reason'], 'model-switch')
        collector.optimizer.state.pop('pending')
        with self.assertRaises(urllib.error.HTTPError) as result:
            self.request(
                self.local,
                headers,
                {'action': 'prepare', 'requestId': str(uuid.uuid4()), 'force': True},
            )
        self.assertEqual(result.exception.code, 400)
        self.local.RequestHandlerClass.setup_preview = True
        before = copy.deepcopy(collector.optimizer.state)
        with self.request(self.local, headers) as response:
            self.assertTrue(json.load(response)['ready'])
        self.assertEqual(collector.optimizer.state, before)
        self.assertFalse(collector.optimizer.stop.is_set())


if __name__ == '__main__':
    unittest.main()
