"""Explicit On intent, idempotent requests, cancellation and cached controls."""

import copy
import threading
import time
import unittest
import uuid
from unittest.mock import Mock, patch
from optimizer_control import OptimizerControl, PLAN_FIELDS
from model_readiness import session_key
from model_combinations import selection_key
import test_optimizer as fixtures


class ControlTests(unittest.TestCase):
    setUp_fixture = fixtures.ControllerTests.setUp
    tearDown = fixtures.ControllerTests.tearDown

    def setUp(self):
        self.setUp_fixture()
        self.o.state.update(mode='observe', endsAt=None)
        self.o.save()
        self.control = self.o.automatic_control
        self.provider_status = 'running'
        self.o.provider_control.inspect = Mock(side_effect=self.provider)
        self.control.projection(time.time())
        self.o.snapshot.side_effect = AssertionError('Controls must not read analytics')
        self.o.store.summary = Mock(
            side_effect=AssertionError('History is deliberately unavailable')
        )

    def provider(self):
        model, options, environment = self.o.read_options()
        return {
            'status': self.provider_status,
            'version': self.o.control_version(),
            'controlVersion': self.o.control_version(),
            'model': model,
            'options': options,
            'environment': environment,
            'raw': copy.deepcopy(self.o.raw),
            'disabled': self.provider_status == 'stopped',
        }

    def request(self, enabled=True, **changes):
        view = self.control.snapshot()
        return {
            'action': 'set-automatic',
            'enabled': enabled,
            'requestId': str(uuid.uuid4()),
            'expectedControl': view['controlVersion'],
            **changes,
        }

    def enable(self, **changes):
        data = self.request(**changes)
        self.control.action(data)
        return data

    def test_cached_get_and_post_never_run_analytics_network_or_provider_checks(self):
        with patch.object(
            self.o.provider_control, 'inspect', side_effect=AssertionError('No inspection in HTTP')
        ):
            start = time.monotonic()
            for _ in range(100):
                self.control.snapshot()
            data = self.enable()
            result = self.control.snapshot()
            self.assertLess(time.monotonic() - start, 0.5)
        self.assertEqual(result['lastRequestId'], data['requestId'])
        self.assertEqual(result['automatic']['phase'], 'waiting')
        self.assertEqual(self.o.state['mode'], 'observe')
        self.o.store.summary.assert_not_called()
        self.o.snapshot.assert_not_called()
        self.o.runner.assert_not_called()
        self.net.fetch.assert_not_called()

    def test_warm_saved_plan_enables_without_changing_pool_policy_dates_or_history(self):
        before = copy.deepcopy(self.o.state)
        data = self.enable()
        self.control.tick()
        self.assertEqual(self.o.state['mode'], 'demand')
        for key in PLAN_FIELDS:
            self.assertEqual(self.o.state.get(key), before.get(key), key)
        result = self.control.snapshot()
        self.assertEqual(result['automatic']['phase'], 'active')
        self.assertEqual(result['actualMode'], 'demand')
        self.assertEqual(result['operation']['id'], data['requestId'])
        self.o.runner.assert_not_called()
        self.o.snapshot.assert_not_called()

    def test_manual_revokes_pending_on_and_old_post_never_replays(self):
        data = self.enable()
        self.control.action(self.request(False))
        self.control.tick()
        self.control.action(data)
        self.control.tick()
        self.assertEqual(self.o.state['mode'], 'observe')
        self.assertEqual(self.control.snapshot()['automatic']['mode'], 'manual')
        self.assertEqual(self.control.operation['status'], 'cancelled')
        self.o.runner.assert_not_called()

    def test_same_id_different_payload_and_stale_version_cannot_mutate(self):
        data = self.enable()
        with self.assertRaisesRegex(ValueError, 'already used'):
            self.control.action({**data, 'enabled': False})
        self.control.cancel('Cancelled fixture')
        with self.assertRaisesRegex(ValueError, 'status changed'):
            self.control.action(self.request(expectedControl='stale'))
        self.assertEqual(self.o.state['mode'], 'observe')

    def test_cold_readiness_waits_then_manual_prevents_late_enable(self):
        self.o.warmup = {}
        self.o.raw['warm_models'] = []
        self.o.read_state.return_value = copy.deepcopy(self.o.raw)
        self.enable()
        self.control.tick()
        self.assertEqual(self.control.snapshot()['automatic']['phase'], 'waiting')
        self.assertEqual(self.o.state['mode'], 'observe')
        self.control.action(self.request(False))
        self.o.raw['warm_models'] = ['a']
        self.o.read_state.return_value = copy.deepcopy(self.o.raw)
        self.o.warmup = {
            'session': session_key(self.o.raw),
            'model': 'a',
            'status': 'ready',
            'verifiedAt': self.now - 1,
        }
        self.control.tick()
        self.assertEqual(self.o.state['mode'], 'observe')

    def test_stopped_start_is_once_then_verified_warmth_resumes_same_plan(self):
        self.provider_status = 'stopped'
        self.o.live['provider']['online'] = False
        self.control.projection(time.time())
        before = copy.deepcopy(self.o.state)

        def start(data, source, automatic_id=None):
            self.assertTrue(self.control.intent_current(automatic_id))
            self.o.pause_internal('Explicit start', keep_automatic_intent=automatic_id)
            self.o.state['providerResult'] = {'id': data['requestId'], 'status': 'working'}

        self.o.provider_control.action = Mock(side_effect=start)
        data = self.enable(expectedProvider=self.control.snapshot()['providerVersion'])
        self.control.tick()
        self.control.tick()
        self.o.provider_control.action.assert_called_once()
        self.assertEqual(self.control.snapshot()['automatic']['phase'], 'starting')
        self.provider_status = 'running'
        self.o.live['provider']['online'] = True
        self.o.raw.update(pid=2, started_at=self.now - 3)
        self.o.read_state.return_value = copy.deepcopy(self.o.raw)
        self.o.identity_session = (self.o.raw['started_at'], self.o.raw['pid'])
        self.o.warmup = {
            'session': session_key(self.o.raw),
            'model': 'a',
            'status': 'ready',
            'verifiedAt': self.now - 1,
        }
        self.o.state['providerResult'] = {'id': data['requestId'], 'status': 'completed'}
        self.control.tick()
        self.control.action(data)
        self.control.tick()
        self.assertEqual(self.o.state['mode'], 'demand')
        for key in PLAN_FIELDS:
            self.assertEqual(self.o.state.get(key), before.get(key), key)
        self.o.provider_control.action.assert_called_once()

    def test_expiry_and_changes_revoke_intent_without_enable(self):
        for kind in ('expired', 'account', 'device', 'plan', 'launch', 'session'):
            with self.subTest(kind=kind):
                case = ControlTests()
                case.setUp()
                self.addCleanup(case.tearDown)
                case.enable()
                if kind == 'expired':
                    case.control.operation['expiresAt'] = time.time() - 1
                elif kind == 'account':
                    case.o.live['account'] = 'other'
                elif kind == 'device':
                    case.o.live['device'] = 'other'
                elif kind == 'plan':
                    case.o.state['models'] = ['a', 'c']
                elif kind == 'launch':
                    case.o.read_options.return_value = (
                        'a',
                        ['--local-endpoint', '--port', '9000'],
                        {},
                    )
                else:
                    case.o.raw['pid'] = 2
                case.control.tick()
                self.assertEqual(case.control.snapshot()['automatic']['phase'], 'blocked')
                self.assertEqual(case.o.state['mode'], 'observe')
                case.o.runner.assert_not_called()

    def queue_manual(self):
        data = {
            'action': 'switch',
            'model': 'b',
            'requestId': str(uuid.uuid4()),
            'expectedSession': session_key(self.o.raw),
        }
        self.o.manual_action(data)
        return data

    def test_real_manual_selection_then_cancel_never_revives_on(self):
        self.enable()
        data = self.queue_manual()
        self.assertEqual(self.control.operation['status'], 'cancelled')
        self.o.manual_action({'action': 'cancel', 'requestId': data['requestId']})
        self.control.tick()
        self.assertEqual(self.o.state['mode'], 'observe')
        self.assertIsNone(self.o.state.get('requestedModel'))
        self.assertEqual(
            [
                c.args[0]
                for c in self.o.runner.call_args_list
                if c.args[0] != ['/usr/bin/sudo', '-n', '-ll', '/usr/sbin/purge']
            ],
            [],
        )

    def test_manual_selection_during_readiness_prevents_final_enable(self):
        self.enable()
        original = self.o.demand_resume_context

        def admitted(*args):
            result = original(*args)
            self.queue_manual()
            return result

        self.o.demand_resume_context = admitted
        self.control.tick()
        self.assertEqual(self.control.operation['status'], 'cancelled')
        self.assertEqual(self.o.state['mode'], 'observe')
        self.assertEqual(self.o.state['requestedModel'], 'b')
        self.assertEqual(self.o.state['requestedKind'], 'manual')
        self.assertEqual(
            [
                c.args[0]
                for c in self.o.runner.call_args_list
                if c.args[0] != ['/usr/bin/sudo', '-n', '-ll', '/usr/sbin/purge']
            ],
            [],
        )

    def test_new_pending_command_or_lost_warm_proof_is_rechecked_at_commit(self):
        for change in ('pending', 'warm'):
            with self.subTest(change=change):
                case = ControlTests()
                case.setUp()
                self.addCleanup(case.tearDown)
                case.enable()
                original = case.o.demand_resume_context

                def admitted(*args):
                    result = original(*args)
                    if change == 'pending':
                        case.o.state['pending'] = {'kind': 'provider'}
                    else:
                        case.o.warmup = {}
                        case.o.raw['warm_models'] = []
                    return result

                case.o.demand_resume_context = admitted
                case.control.tick()
                self.assertEqual(case.o.state['mode'], 'observe')
                self.assertNotEqual(case.control.operation['status'], 'active')
                case.o.runner.assert_not_called()

    def test_stale_cache_still_allows_bound_manual_and_async_refresh(self):
        self.enable()
        with self.control.cache_lock:
            self.control.view['at'] = time.time() - 16
        self.control.action(self.request(False))
        self.assertEqual(self.control.operation['status'], 'cancelled')
        refresh = {
            'action': 'refresh',
            'requestId': str(uuid.uuid4()),
            'expectedControl': 'older-view',
        }
        self.control.action(refresh)
        self.assertEqual(self.o.next_identity, 0)
        self.assertEqual(self.o.next_discovery, 0)
        self.control.tick()
        self.assertEqual(self.o.state['mode'], 'observe')
        self.o.runner.assert_not_called()

    def test_old_manual_token_cannot_cancel_a_new_on(self):
        old_manual = self.request(False)
        self.enable()
        with self.assertRaisesRegex(ValueError, 'status changed'):
            self.control.action(old_manual)
        self.assertEqual(self.control.operation['status'], 'pending')

    def test_real_manual_inspection_does_not_hold_optimizer_lock(self):
        entered = threading.Event()
        release = threading.Event()
        original = self.o.provider_control.inspect

        def slow():
            entered.set()
            release.wait(2)
            return original()

        self.o.provider_control.inspect = slow
        worker = threading.Thread(target=self.queue_manual)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertTrue(self.o.lock.acquire(timeout=0.2))
            self.o.lock.release()
            self.control.snapshot()
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.o.state['requestedModel'], 'b')
        self.assertEqual(
            [
                c.args[0]
                for c in self.o.runner.call_args_list
                if c.args[0] != ['/usr/bin/sudo', '-n', '-ll', '/usr/sbin/purge']
            ],
            [],
        )

    def test_clock_change_cannot_extend_the_bounded_intent(self):
        self.enable()
        with patch('optimizer_control.time.monotonic', return_value=self.control.deadline + 1):
            self.control.tick()
        self.assertEqual(self.control.snapshot()['automatic']['phase'], 'blocked')
        self.assertEqual(self.o.state['mode'], 'observe')

    def test_restart_receipt_blocks_pending_intent_and_replay(self):
        data = self.enable()
        self.control = self.o.automatic_control = OptimizerControl(self.o)
        self.control.projection(time.time())
        self.assertEqual(self.control.snapshot()['automatic']['phase'], 'blocked')
        self.control.action(data)
        self.control.tick()
        self.assertEqual(self.o.state['mode'], 'observe')
        self.o.runner.assert_not_called()

    def test_first_plan_is_reviewed_once_and_saved_plan_rejects_replacement(self):
        self.o.state.update(startedAt=None, models=[], originalModel=None)
        self.control.projection(time.time())
        with self.assertRaisesRegex(ValueError, 'Review two'):
            self.enable()
        self.enable(models=['a', 'b'], demandPolicy={'targetUsdPerHour': 0.15})
        self.control.tick()
        self.assertEqual(self.o.state['mode'], 'demand')
        self.assertEqual(self.o.state['models'], ['a', 'b'])
        self.o.pause_automatic()
        self.control.projection(time.time())
        with self.assertRaisesRegex(ValueError, 'without replacing'):
            self.enable(models=['a', 'b'])
        self.o.snapshot.assert_not_called()

    def test_slow_service_and_readiness_do_not_hold_optimizer_lock_or_allow_late_enable(self):
        self.enable()
        entered = threading.Event()
        release = threading.Event()
        original = self.o.demand_resume_context

        def slow(*args):
            entered.set()
            release.wait(2)
            return original(*args)

        self.o.demand_resume_context = slow
        worker = threading.Thread(target=self.control.advance, args=(time.time(),))
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertTrue(self.o.lock.acquire(timeout=0.2))
            self.o.lock.release()
            self.assertEqual(self.control.snapshot()['automatic']['mode'], 'on')
            self.control.action(self.request(False))
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.o.state['mode'], 'observe')

    def test_unavailable_on_always_explains_identity_scope_or_completed_plan(self):
        cases = ('identity', 'model-scope', 'completed-plan', 'catalog', 'saved-pool')
        for code in cases:
            with self.subTest(code=code):
                case = ControlTests()
                case.setUp()
                self.addCleanup(case.tearDown)
                if code == 'identity':
                    case.o.live['account'] = ''
                elif code == 'model-scope':
                    case.o.read_options.return_value = (
                        selection_key(['a', 'b']),
                        ['--local-endpoint'],
                        {},
                    )
                elif code == 'completed-plan':
                    case.o.state['endsAt'] = case.now - 1
                elif code == 'catalog':
                    case.o.discovery_at = 0
                else:
                    case.o.state['models'] = ['b']
                view = case.control.projection(time.time())
                self.assertFalse(view['automatic']['canEnable'])
                self.assertEqual(view['automatic']['blocker']['code'], code)
                self.assertNotEqual(
                    view['automatic']['detail'], 'Manual mode. The current model keeps serving.'
                )
                if code == 'completed-plan':
                    self.assertFalse(view['firstPlan'])

    def test_failed_intent_persistence_dispatches_nothing(self):
        with patch.object(self.control, 'save', side_effect=OSError('fixture failure')):
            with self.assertRaises(OSError):
                self.enable()
        self.assertIsNone(self.control.operation)
        self.assertEqual(self.control.requests, [])
        self.o.runner.assert_not_called()

    def test_manual_save_failure_is_not_acknowledged_as_an_accepted_request(self):
        self.enable()
        self.control.tick()
        before = self.control.snapshot()['lastRequestId']
        data = self.request(False)
        with patch.object(self.o, 'save', side_effect=OSError('fixture disk failure')):
            with self.assertRaises(OSError):
                self.control.action(data)
        self.assertEqual(self.control.snapshot()['lastRequestId'], before)
        self.assertEqual(self.o.state['mode'], 'demand')

    def test_update_change_prevents_late_enable(self):
        self.enable()
        with patch.object(
            self.o.update_guard,
            'require_available',
            side_effect=ValueError('Fixture admission denied'),
        ):
            self.control.tick()
        self.assertEqual(self.o.state['mode'], 'observe')
        self.assertEqual(self.control.snapshot()['automatic']['phase'], 'blocked')
        self.o.runner.assert_not_called()

    def test_slow_service_projection_leaves_cached_get_and_optimizer_lock_available(self):
        entered = threading.Event()
        release = threading.Event()
        original = self.o.provider_control.inspect

        def slow():
            entered.set()
            release.wait(2)
            return original()

        self.o.provider_control.inspect = slow
        worker = threading.Thread(target=self.control.projection, args=(time.time(),))
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertTrue(self.o.lock.acquire(timeout=0.2))
            self.o.lock.release()
            before = time.monotonic()
            self.control.snapshot()
            self.assertLess(time.monotonic() - before, 0.1)
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())

    def test_legacy_response_analytics_does_not_hold_optimizer_lock(self):
        entered = threading.Event()
        release = threading.Event()

        def slow():
            entered.set()
            release.wait(2)
            return {}

        self.o.snapshot.side_effect = slow
        worker = threading.Thread(
            target=self.o.control_action,
            args=({'action': 'pause', 'expectedControl': self.o.control_version()},),
        )
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertTrue(self.o.lock.acquire(timeout=0.2))
            self.o.lock.release()
            self.assertEqual(self.control.snapshot()['automatic']['mode'], 'manual')
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())


if __name__ == '__main__':
    unittest.main()
