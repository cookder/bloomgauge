"""Use the real provider-control admission/worker with a mocked provider CLI."""

import copy
import threading
import time
import unittest
import uuid
from unittest.mock import Mock, patch
from model_readiness import session_key
from optimizer_control import PLAN_FIELDS
import test_provider_control as fixtures


class ControlStartTests(unittest.TestCase):
    write_plist = fixtures.ProviderTests.write_plist
    run_cli = fixtures.ProviderTests.run_cli
    tearDown = fixtures.ProviderTests.tearDown

    def setUp(self):
        fixtures.ProviderTests.setUp(self)
        self.control = self.o.automatic_control
        self.process.return_value = False
        self.o.service_disabled.return_value = True
        self.o.live['provider']['online'] = False
        self.o.live['earnings'] = {'status': 'ok', 'updatedAt': self.now}
        self.net.snapshot.return_value = {
            'capacity': {'status': 'ok', 'updatedAt': self.now, 'data': {'models': []}}
        }
        self.net.snapshot.side_effect = lambda key=None, m=self.net.snapshot: (
            m.return_value if key is None else (m.return_value.get(key) or {})
        )
        self.o.local.append(
            {'id': 'b', 'estimated_memory_gb': 12, 'size_bytes': 1000, 'template_render_ok': True}
        )
        self.o.catalog.append({'id': 'b', 'active': True, 'min_ram_gb': 16})
        self.o.state.update(
            mode='observe',
            models=['a', 'b'],
            startedAt=self.now - 3600,
            endsAt=None,
            expectedModel='a',
            originalModel='a',
            account='acct',
            device=self.o.live['device'],
        )
        self.o.save()
        self.control.projection(time.time())
        self.data = {
            'action': 'set-automatic',
            'enabled': True,
            'requestId': str(uuid.uuid4()),
            'expectedControl': self.control.snapshot()['controlVersion'],
            'expectedProvider': self.control.snapshot()['providerVersion'],
        }
        self.o.store.summary = Mock(side_effect=AssertionError('No history for On'))

    def test_real_admission_and_mock_cli_start_once_then_warm_resume(self):
        before = copy.deepcopy(self.o.state)
        self.control.action(self.data)
        self.control.advance(time.time())
        self.o.worker.join(2)
        self.assertEqual(self.o.state['providerResult']['status'], 'completed')
        self.o.raw = copy.deepcopy(self.raw)
        now = time.time()
        self.o.raw['warm_models'] = ['a']
        self.raw['warm_models'] = ['a']
        self.o.raw['started_at'] = min(self.o.raw['started_at'], now - 0.1)
        self.raw['started_at'] = self.o.raw['started_at']
        # One clock read. The mock CLI stamped written_at in the worker; readiness needs a sample
        # written after the pre-warm, which a >=50 ms stall since then would otherwise reverse.
        self.o.raw['written_at'] = self.raw['written_at'] = now
        self.o.live['at'] = now
        self.o.live['provider']['online'] = True
        self.o.identity_session = (self.raw['started_at'], self.raw['pid'])
        self.o.identity_at = now
        self.o.warmup = {
            'session': session_key(self.raw),
            'model': 'a',
            'status': 'ready',
            'verifiedAt': now - 0.05,
        }
        with patch('optimizer.matching_process', return_value=True):
            self.control.tick()
        self.assertEqual(self.o.state['mode'], 'demand')
        for key in PLAN_FIELDS:
            self.assertEqual(self.o.state.get(key), before.get(key), key)
        self.control.action(self.data)
        calls = [c.args[0] for c in self.o.runner.call_args_list if c.args[0][1] == 'start']
        self.assertEqual(len(calls), 1)
        self.o.store.summary.assert_not_called()

    def test_manual_between_provider_admission_and_dispatch_prevents_cli_start(self):
        self.control.action(self.data)
        worker = Mock()
        worker.is_alive.return_value = False
        with patch('provider_control.threading.Thread', return_value=worker) as factory:
            self.control.advance(time.time())
        args = factory.call_args.kwargs['args']
        manual = {
            'action': 'set-automatic',
            'enabled': False,
            'requestId': str(uuid.uuid4()),
            'expectedControl': self.control.snapshot()['controlVersion'],
        }
        self.control.action(manual)
        self.p.run(*args)
        self.assertEqual(self.o.state['mode'], 'observe')
        self.assertEqual(self.control.operation['status'], 'cancelled')
        calls = [c.args[0] for c in self.o.runner.call_args_list if c.args[0][1] == 'start']
        self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main()
