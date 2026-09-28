"""Automatic selection of a model with runtime requirements (Qwen3.8 needs apple_m5 + mlx_nax).

Roster proof (the coordinator lists it for the running session) or history proof: this
Mac's Darkbloom runtime reports the capabilities, the files check out, the device identity
is verified and this Mac has served the model before. Loading it must still pass the
post-start coordinator eligibility check; a failure there is a failed switch that restores.
"""

import copy, json, threading, time, unittest, uuid
from unittest.mock import Mock, patch

from demand_optimizer import policy
from model_combinations import selection_key
from optimizer import RUNTIME_PROOFS_KEY, session_key
from test_demand_optimizer import decision as legacy_upgrade
import test_optimizer

QWEN = 'EigenLabs/Qwen3.8-27B-4bit-mtp'
CAPS = ['apple_m5', 'mlx_nax']
OPTIONS = ['--local-endpoint', '--port', '8000']
PREFIX = 'Runtime support is not yet verified for automatic selection. '
NEVER = PREFIX + 'Needs one run on this Mac first: pick it from the manual model list to verify it.'
WAITING = PREFIX + 'Waiting to verify this Mac with the provider roster.'
HELD = 'the coordinator has not confirmed the selected model’s catalog or runtime eligibility'


class Background(threading.Event):
    """Optimizer.stop whose wait() runs what the collector and refresh loops do meanwhile."""

    def __init__(self, tick):
        super().__init__()
        self.tick = tick

    def wait(self, timeout=None):
        self.tick()
        time.sleep(0.01)
        return self.is_set()


class Fixture(unittest.TestCase):
    """Andrew's Mac: M5 Pro 48 GB serving model 'a'; Qwen3.8 downloaded, not advertised."""

    setUp_base = test_optimizer.ControllerTests.setUp
    tearDown = test_optimizer.ControllerTests.tearDown

    def setUp(self):
        self.setUp_base()
        self.o.catalog = [
            {'id': 'a', 'active': True, 'min_ram_gb': 24},
            {
                'id': QWEN,
                'active': True,
                'min_ram_gb': 36,
                'required_provider_capabilities': list(CAPS),
            },
        ]
        self.o.local = [
            {'id': 'a', 'estimated_memory_gb': 10, 'size_bytes': 1000, 'template_render_ok': True},
            {
                'id': QWEN,
                'estimated_memory_gb': 18.2,
                'size_bytes': 16293475586,
                'template_render_ok': True,
            },
        ]
        self.o.runner.return_value.stdout = json.dumps({'models': self.o.local})
        self.o.raw['runtime_capabilities'] = list(CAPS)  # Andrew's M5 Pro, Darkbloom 0.9.10
        self.raw = copy.deepcopy(self.o.raw)
        self.o.read_state.return_value = copy.deepcopy(self.raw)
        self.o.state['models'] = ['a', QWEN]
        self.accepted = False  # does the coordinator list what the Mac advertises?
        self.net.fetch.side_effect = self.fetch
        self.verify_identity()
        self.assertTrue(self.o.device_identity_ok and self.o.identity_ok)

    def fetch(self, path):
        if path == '/v1/models/catalog':
            return {'models': copy.deepcopy(self.o.catalog)}
        advertised = self.o.read_state()['advertised_models']
        return {
            'providers': [
                {
                    'se_public_key': 'public-test-key',
                    'provider_id': 'p',
                    'models': advertised if self.accepted or advertised == ['a'] else ['a'],
                    'status': 'online',
                    'trust_level': 'hardware',
                }
            ]
        }

    def verify_identity(self, now=None):
        """One pass of the background roster check (Optimizer.refresh)."""
        self.o.next_discovery = float('inf')
        self.o.next_identity = 0
        with patch('optimizer.time.monotonic', return_value=0):
            self.o.refresh(self.now if now is None else now)

    def served(self, minutes, model=QWEN, device=None, ago=3600, seconds=60, fresh=True):
        """`minutes` ready minutes this Mac served `model` alone, ending `ago` seconds back."""
        device = self.live['device'] if device is None else device
        end = int((self.now - ago) // 60) * 60
        with self.h.lock:
            self.h.db.executemany(
                'INSERT INTO opt_ready_minutes VALUES(?,?,?,?,?,?,?,?)',
                [
                    ('acct', device, end - 60 * i, model, seconds, 1, 30, 0)
                    for i in range(1, minutes + 1)
                ],
            )
            self.h.db.commit()
        if fresh:
            self.o.proof_minutes.clear()  # as if the cached lookup had expired

    def row(self, manual=False, model=QWEN):
        return next(
            r
            for r in self.o.candidates({}, {}, self.o.live, self.o.state, manual=manual)
            if r['id'] == model
        )

    def serve_qwen(self):
        """The Mac advertises Qwen and the coordinator lists it (e.g. after Verify & switch)."""
        self.o.raw.update(
            advertised_models=[QWEN], current_model=QWEN, warm_models=[QWEN], pid=5
        )
        self.o.read_state.return_value = copy.deepcopy(self.o.raw)
        self.o.live['provider']['model'] = QWEN
        self.accepted = True
        self.verify_identity()


class RuntimeProofTests(Fixture):
    def test_qwen_served_here_is_available_for_automatic_selection(self):
        self.served(10)
        row = self.row()
        self.assertEqual((row['available'], row['reason']), (True, None))
        self.assertEqual(row['runtimeProof'], 'history')
        self.assertFalse(row['requiresRuntimeVerification'])
        # Proven here, a manual pick needs no Verify & switch either.
        manual = self.row(manual=True)
        self.assertEqual(
            (manual['available'], manual['requiresRuntimeVerification'], manual['runtimeProof']),
            (True, False, 'history'),
        )
        self.assertIsNone(self.row(model='a')['runtimeProof'])  # no runtime requirements
        self.assertIsNotNone(self.o.selection_budget(QWEN, self.o.live, self.o.raw))
        # A catalog requirement the runtime also reports still matches (a superset).
        self.o.catalog[1]['required_provider_capabilities'] = CAPS + ['metal_4']
        self.o.raw['runtime_capabilities'] = CAPS + ['metal_4', 'other']
        self.assertEqual(self.row()['runtimeProof'], 'history')

    def test_short_old_partial_paired_or_another_macs_history_is_not_enough(self):
        self.served(9)
        self.served(20, ago=31 * 86400)
        self.served(20, ago=4 * 3600, seconds=45)
        self.served(20, device='another-mac')
        self.served(20, model=selection_key([QWEN, 'a']))
        row = self.row()
        self.assertEqual(
            (row['available'], row['reason'], row['runtimeProof']), (False, NEVER, None)
        )
        self.served(1, ago=0)
        self.assertEqual(self.row()['runtimeProof'], 'history')

    def test_missing_runtime_capabilities_say_which(self):
        self.served(30)
        for reported, missing in (
            (['apple_m5'], 'mlx_nax'),
            (['mlx_nax', 'apple_m4'], 'apple_m5'),
            ([], 'apple_m5, mlx_nax'),
            (None, 'apple_m5, mlx_nax'),
            ('apple_m5,mlx_nax', 'apple_m5, mlx_nax'),
        ):
            with self.subTest(reported=reported):
                self.o.raw['runtime_capabilities'] = reported
                if reported is None:
                    self.o.raw.pop('runtime_capabilities')
                row = self.row()
                self.assertFalse(row['available'])
                self.assertIsNone(row['runtimeProof'])
                self.assertEqual(
                    row['reason'],
                    PREFIX + 'This Mac’s Darkbloom runtime doesn’t report %s.' % missing,
                )

    def test_files_that_do_not_check_out_block_it(self):
        self.served(30)
        good = copy.deepcopy(self.o.local[1])
        for change in (
            {'template_render_ok': False},
            {'template_render_ok': None},
            {'template_render_ok': 'true'},
            {'size_bytes': 0},
            {'size_bytes': None},
            {'size_bytes': float('nan')},
        ):
            with self.subTest(change=change):
                self.o.local[1] = {**good, **change}
                row = self.row()
                self.assertFalse(row['available'])
                self.assertEqual(
                    row['reason'],
                    # A template Darkbloom reported broken holds every model, not only these.
                    'Its chat template failed Darkbloom’s check. Refresh models to recheck it.'
                    if change == {'template_render_ok': False}
                    else PREFIX + 'Refresh models to verify its downloaded files and template.',
                )

    def test_unverified_device_blocks_it(self):
        faults = ('roster down', 'identity', 'hardware', 'stale', 'future', 'daemon', 'history')
        for fault in faults:
            with self.subTest(fault=fault):
                self.tearDown()
                self.setUp()
                self.served(30)
                self.assertTrue(self.row()['available'])
                if fault == 'roster down':
                    self.net.fetch.side_effect = OSError('offline')
                    self.verify_identity()
                elif fault == 'identity':
                    self.o.device_identity_ok = False
                elif fault == 'hardware':
                    self.o.identity_hardware = False
                elif fault == 'stale':
                    self.o.identity_at = self.now - 181
                elif fault == 'future':
                    self.o.identity_at = self.now + 300
                elif fault == 'daemon':
                    self.o.identity_device = 'another-mac'  # the roster matched another key
                elif fault == 'history':
                    self.o.live['device'] = 'another-mac'  # history is kept under another id
                row = self.row()
                self.assertEqual((row['available'], row['runtimeProof']), (False, None))
                self.assertEqual(row['reason'], WAITING)

    def test_never_served_here_keeps_the_manual_verify_path(self):
        row = self.row()
        self.assertEqual(
            (row['available'], row['reason'], row['runtimeProof']), (False, NEVER, None)
        )
        self.assertIsNone(self.o.selection_budget(QWEN, self.o.live, self.o.raw))
        manual = self.row(manual=True)
        self.assertEqual(
            (manual['available'], manual['requiresRuntimeVerification'], manual['runtimeProof']),
            (True, True, None),
        )
        payload = {
            'action': 'switch',
            'model': QWEN,
            'expectedSession': session_key(self.o.raw),
            'requestId': str(uuid.uuid4()),
        }
        with self.assertRaisesRegex(ValueError, 'Verify & switch'):
            self.o.manual_action(payload, 'phone')
        self.o.manual_action({**payload, 'verifyRuntime': True}, 'phone')
        self.assertEqual(self.o.state['requestedModel'], QWEN)
        self.assertTrue(self.o.state['requestedVerifyRuntime'])
        self.assertEqual(self.o.state['mode'], 'observe')  # a manual attempt pauses automation

    def test_roster_proof_is_saved_and_outlives_history_rows(self):
        self.served(12)
        self.serve_qwen()
        self.assertEqual(self.row()['runtimeProof'], 'roster')
        saved = self.h.cache(RUNTIME_PROOFS_KEY)
        self.assertEqual(list(saved), [self.live['device']])
        self.assertAlmostEqual(saved[self.live['device']][QWEN], time.time(), delta=5)
        # Back on 'a'. Old rows are pruned; the saved proof alone still counts, also after
        # Bloomkeeper reopens and reads it back.
        self.o.raw = copy.deepcopy(self.raw)
        self.o.read_state.return_value = copy.deepcopy(self.raw)
        self.o.live['provider']['model'] = 'a'
        self.verify_identity()
        with self.h.lock:
            self.h.db.execute('DELETE FROM opt_ready_minutes')
            self.h.db.commit()
        self.o.proof_minutes.clear()
        self.o.runtime_proofs = None
        row = self.row()
        self.assertEqual((row['available'], row['runtimeProof']), (True, 'history'))
        # It is still only history proof: capabilities and files are rechecked.
        self.o.raw['runtime_capabilities'] = ['apple_m5']
        self.assertFalse(self.row()['available'])

    def test_no_proof_without_enough_ready_minutes_or_for_another_mac(self):
        self.served(3)
        self.serve_qwen()
        self.assertEqual(self.row()['runtimeProof'], 'roster')
        self.assertIsNone(self.h.cache(RUNTIME_PROOFS_KEY))
        for saved in (
            {'another-mac': {QWEN: self.now}},
            ['bad'],
            {self.live['device']: {QWEN: 'x'}},
        ):
            with self.subTest(saved=saved):
                self.o.raw = copy.deepcopy(self.raw)
                self.o.read_state.return_value = copy.deepcopy(self.raw)
                self.verify_identity()
                self.h.cache(RUNTIME_PROOFS_KEY, saved)
                self.o.runtime_proofs = None
                self.assertEqual(self.row()['reason'], NEVER)

    def test_history_lookup_is_cached_for_ten_minutes(self):
        calls = []
        lookup = self.o.store.served_minutes
        self.o.store.served_minutes = lambda *a: calls.append(a) or lookup(*a)
        self.assertFalse(self.row()['available'])
        self.served(10, fresh=False)
        for _ in range(5):
            self.assertFalse(self.row()['available'])
        self.assertEqual(len(calls), 1)
        later = self.now + 601
        self.o.identity_at = later
        with patch('optimizer.time.time', return_value=later):
            self.assertTrue(self.row()['available'])
        self.assertEqual(len(calls), 2)


class PostStartEligibilityTests(Fixture):
    """A history-proven automatic move still needs the coordinator after Qwen loads."""

    def setUp(self):
        super().setUp()
        self.served(30)
        self.daemon = copy.deepcopy(self.raw)
        self.o.read_state = Mock(side_effect=self.read)
        self.o.read_options.return_value = ('a', OPTIONS, {})
        self.commands, self.details = [], []
        self.o.command = Mock(side_effect=self.start)
        self.o.recovery_ready = Mock(side_effect=lambda *a, **k: self.read())
        self.o.stop = Background(self.background)
        real = self.o.verify_started
        self.o.verify_started = lambda target, previous, timeout=360, *a, **k: real(
            target, previous, 0.3, *a, **k
        )
        self.o.state.update(
            mode='demand',
            demandPolicy=policy(),
            expectedModel='a',
            lastSwitchAt=self.now - 4000,
            account='acct',
            device=self.live['device'],
        )
        legacy = legacy_upgrade()
        legacy.update(
            currentModel='a', policy=self.o.state['demandPolicy'], explorationTrigger=None
        )
        # evaluate(account, device, rows, current, raw, rules, now, ...): the legacy result.
        self.o.demand_auto.evaluate = Mock(
            side_effect=lambda *a, **k: {**copy.deepcopy(legacy), 'at': a[6]}
        )
        self.o.demand_auto.evidence = Mock(return_value=({}, {}))
        process = patch('manager.matching_process', return_value=True)
        process.start()
        self.addCleanup(process.stop)

    def read(self):
        return {**copy.deepcopy(self.daemon), 'written_at': time.time()}

    def start(self, target, options, environment):
        """`darkbloom start --model target`: a new session that loads and serves `target`."""
        self.commands.append(target)
        self.o.read_options.return_value = (target, OPTIONS, {})
        self.daemon.update(
            advertised_models=[target],
            current_model=target,
            warm_models=[target],
            started_at=time.time() - 1,
            pid=self.daemon['pid'] + 1,
            stats={'requests_served': 3, 'tokens_generated': 90},
        )

    def background(self):
        """The collector reads the new session; the refresh loop matches it to the roster."""
        raw = self.read()
        with self.o.lock:
            self.o.raw = raw
            self.details.append(self.o.detail)
        if self.o.identity_session != (raw['started_at'], raw['pid']):
            self.verify_identity(time.time())

    def move(self, kind):
        self.o.state['pending'] = {'model': QWEN, 'kind': 'demand', 'demandKind': kind}
        self.o.switch('a', QWEN, 'acct', self.live['device'])

    def test_manager_returns_home_to_a_history_proven_model(self):
        self.o.state['manager'] = {
            'home': {'model': QWEN, 'source': 'history', 'at': self.now - 90000}
        }
        d = self.o.demand_decision(time.time())
        self.assertEqual((d['target'], d['kind']), (QWEN, 'home'))
        self.assertEqual(d['reason'], 'Returning to home model %s.' % QWEN)
        self.assertEqual(self.row()['runtimeProof'], 'history')

    def test_coordinator_that_does_not_list_the_loaded_model_fails_the_switch_and_restores(self):
        self.o.state['manager'] = {
            'home': {'model': QWEN, 'source': 'history', 'at': self.now - 90000}
        }
        self.move('home')
        # The pre-dispatch recheck admitted it without roster proof; the command was sent.
        self.assertEqual(self.commands, [QWEN, 'a'])
        self.assertTrue(any(HELD in (d or '') for d in self.details), self.details)
        failure = self.o.state['lastSwitchFailure']
        self.assertEqual(
            (failure['model'], failure['stage'], failure['code'], failure['recovery']),
            (QWEN, 'verify', 'readiness-timeout', 'restored'),
        )
        self.assertEqual(self.o.state['lastSwitchResult']['outcome'], 'recovered')
        self.assertEqual(self.o.state['mode'], 'demand')  # the manager never pauses for this
        self.assertEqual(self.o.state['expectedModel'], 'a')
        self.assertIn('Restored and pre-warmed a. Automatic control continues.', self.o.detail)
        hold = self.o.state['manager']['homeRetry']
        self.assertEqual((hold['model'], hold['failures']), (QWEN, 1))
        self.assertEqual(self.daemon['advertised_models'], ['a'])
        self.assertIsNone(self.h.cache(RUNTIME_PROOFS_KEY))

    def test_failed_excursion_target_is_blocked_for_a_day(self):
        self.o.state['manager'] = {'home': {'model': 'a', 'source': 'history', 'at': 0}}
        move = {
            **legacy_upgrade(),
            'currentModel': 'a',
            'target': QWEN,
            'kind': 'excursion',
            'reason': 'Excursion to %s: fixture.' % QWEN,
            'policy': policy(),
            'manager': {'active': True, 'action': 'excursion', 'proposal': {'target': QWEN}},
        }
        self.o.demand_decision = Mock(side_effect=lambda now, *a, **k: {**move, 'at': now})
        self.move('excursion')
        self.assertEqual(self.commands, [QWEN, 'a'])
        self.assertEqual(self.o.state['lastSwitchFailure']['recovery'], 'restored')
        until = self.o.state['manager']['blocked'][QWEN]
        self.assertAlmostEqual(until - time.time(), 86400, delta=60)

    def test_coordinator_that_lists_it_completes_the_switch_and_saves_the_proof(self):
        self.o.state['manager'] = {
            'home': {'model': QWEN, 'source': 'history', 'at': self.now - 90000}
        }
        self.accepted = True
        self.move('home')
        self.assertEqual(self.commands, [QWEN])
        self.assertEqual(self.o.state['lastSwitchResult']['outcome'], 'switched')
        self.assertEqual(self.o.state['expectedModel'], QWEN)
        self.o.live['provider']['model'] = QWEN
        self.assertEqual(self.row()['runtimeProof'], 'roster')
        self.assertIn(QWEN, self.h.cache(RUNTIME_PROOFS_KEY)[self.live['device']])


if __name__ == '__main__':
    unittest.main()
