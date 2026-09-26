import copy, io, json, pathlib, tempfile, threading, unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from history import History
from optimizer import Optimizer, ExternalChange, session_key, launch_signature
from optimizer_store import device_id
from prewarm import prewarm, local_request, WarmupError, WarmupDeferred, NoRedirect

NOW = 1788841846.0


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name)
        self.path = self.home / '.darkbloom/local.json'
        self.path.parent.mkdir()
        self.info = {
            'base_url': 'http://127.0.0.1:8000/v1',
            'pid': 123,
            'port': 8000,
            'api_key': 'test-local-token',
        }
        self.write()
        self.raw = {'pid': 123}
        self.options = ['--local-endpoint']

    def tearDown(self):
        self.tmp.cleanup()

    def write(self):
        self.path.write_text(json.dumps(self.info))
        self.path.chmod(0o600)

    def client(self, value):
        c = Mock()
        c.open.return_value = io.BytesIO(json.dumps(value).encode())
        return c

    def test_warmup_is_one_authenticated_local_token(self):
        c = self.client(
            {
                'model': 'a',
                'choices': [{'message': {'reasoning_content': 'x'}}],
                'usage': {'completion_tokens': 1},
            }
        )
        prewarm(self.home, self.raw, self.options, 'a', opener=c)
        req = c.open.call_args.args[0]
        body = json.loads(req.data)
        self.assertEqual(req.full_url, 'http://127.0.0.1:8000/v1/chat/completions')
        self.assertEqual(req.get_header('Authorization'), 'Bearer test-local-token')
        self.assertEqual(body['max_tokens'], 1)
        self.assertEqual(body['temperature'], 0)
        self.assertFalse(body['stream'])

    def test_discovery_cannot_send_credentials_to_foreign_host_port_pid_or_path(self):
        original = copy.deepcopy(self.info)
        for change in (
            {'base_url': 'https://example.com/v1'},
            {'pid': 124},
            {'base_url': 'http://127.0.0.1:8001/v1'},
            {'base_url': 'http://127.0.0.1:8000/v1?token=x'},
            {'base_url': 'http://user@127.0.0.1:8000/v1'},
            {'base_url': 'http://127.0.0.1:8000/elsewhere'},
            {'api_key': 'bad\ntoken'},
        ):
            with self.subTest(change=change):
                self.info = {**original, **change}
                self.write()
                with self.assertRaises(WarmupError):
                    local_request(self.home, self.raw, self.options)

    def test_no_endpoint_or_no_auth_or_private_bind_is_rejected(self):
        for options in (
            [],
            ['--local-endpoint', '--no-auth'],
            ['--local-endpoint', '--bind', '100.64.0.1'],
        ):
            with self.assertRaises(WarmupError):
                local_request(self.home, self.raw, options)

    def test_insecure_discovery_permissions_and_symlink_are_rejected(self):
        self.path.chmod(0o644)
        with self.assertRaises(WarmupError):
            local_request(self.home, self.raw, self.options)
        self.path.unlink()
        self.path.symlink_to(self.home / 'missing')
        with self.assertRaises(WarmupError):
            local_request(self.home, self.raw, self.options)

    def test_redirects_are_never_followed(self):
        with self.assertRaises(WarmupError):
            NoRedirect().redirect_request(None, None, 302, '', {}, 'https://example.com')

    def test_non_decode_responses_cannot_claim_ready(self):
        for value in (
            {'model': 'other', 'choices': [1], 'usage': {'completion_tokens': 1}},
            {'model': 'a', 'choices': [1], 'usage': {'completion_tokens': 0}},
            {'model': 'a', 'choices': [], 'usage': {'completion_tokens': 1}},
        ):
            with self.assertRaises(WarmupError):
                prewarm(self.home, self.raw, self.options, 'a', opener=self.client(value))

    def test_capacity_errors_are_redacted_and_do_not_purge(self):
        c = Mock()
        c.open.side_effect = HTTPError('http://secret', 503, 'sensitive upstream text', {}, None)
        with self.assertRaises(WarmupDeferred) as caught:
            prewarm(self.home, self.raw, self.options, 'a', opener=c)
        self.assertEqual(caught.exception.code, 'warmup-capacity')
        self.assertNotIn('secret', str(caught.exception))
        self.assertNotIn('sensitive', str(caught.exception))


class WarmupControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.h = History(':memory:')
        self.now = NOW
        self.net = Mock()
        self.net.snapshot.return_value = {}
        self.net.snapshot.side_effect = lambda key=None, m=self.net.snapshot: (
            m.return_value if key is None else (m.return_value.get(key) or {})
        )
        self.o = Optimizer(self.h, self.net, self.tmp.name, threading.Event(), Mock())
        self.raw = {
            'attestation_public_key': 'test-device',
            'pid': 123,
            'started_at': NOW - 300,
            'written_at': NOW,
            'advertised_models': ['a'],
            'current_model': 'a',
            'warm_models': ['a'],
            'inference_active': False,
            'stats': {'requests_served': 0, 'tokens_generated': 0},
            'trust': {'status': 'online'},
            'capacity': {},
        }
        self.o.raw = copy.deepcopy(self.raw)
        self.o.live = {
            'at': NOW,
            'account': 'test',
            'device': device_id(self.raw),
            'provider': {'online': True, 'model': 'a', 'memoryGB': 12},
            'hardware': {
                'chip': 'Apple M5 Pro',
                'memoryTotalGB': 48,
                'memoryUsedGB': 24,
                'memoryAvailableGB': 24,
                'cpuTemp': 50,
                'gpuTemp': 60,
                'thermal': 'Nominal',
            },
        }
        self.o.identity_ok = True
        self.o.identity_at = NOW
        self.o.identity_session = (self.raw['started_at'], 123)
        self.o.discovery_at = NOW
        self.o.local = [{'id': 'a', 'estimated_memory_gb': 12}]
        self.o.catalog = [{'id': 'a', 'active': True, 'min_ram_gb': 24}]
        self.o.on_ac_power = Mock(return_value=True)
        self.o.service_disabled = Mock(return_value=False)
        self.o.read_options = Mock(return_value=('a', ['--local-endpoint'], {}))
        self.o.read_state = Mock(side_effect=lambda: {**self.raw, 'written_at': self.now})
        self.o.idle_since = NOW - 30
        self.clock = patch('optimizer.time.time', side_effect=lambda: self.now)
        self.clock.start()
        self.wait = patch.object(self.o.stop, 'wait', side_effect=self.advance)
        self.wait.start()

    def advance(self, seconds):
        self.now += seconds
        return False

    def tearDown(self):
        self.clock.stop()
        self.wait.stop()
        self.h.close()
        self.tmp.cleanup()

    def test_response_and_fresh_loaded_model_are_both_required(self):
        self.raw['warm_models'] = []
        with patch('optimizer.prewarm'):
            with self.assertRaisesRegex(WarmupError, 'not confirmed'):
                self.o.perform_prewarm('a', self.raw, ['--local-endpoint'])
        self.assertEqual(self.o.warmup['status'], 'failed')
        self.raw['warm_models'] = ['a']
        self.o.live['at'] = self.now
        with patch('optimizer.prewarm') as call:
            self.assertTrue(self.o.perform_prewarm('a', self.raw, ['--local-endpoint']))
        self.o.raw['written_at'] = self.now
        call.assert_called_once()
        self.assertEqual(self.o.warmup_snapshot()['status'], 'ready')

    def test_endpoint_failure_is_not_success(self):
        with patch('optimizer.prewarm', side_effect=WarmupError('Insufficient memory')):
            with self.assertRaises(WarmupError):
                self.o.perform_prewarm('a', self.raw, ['--local-endpoint'])
        self.assertEqual(self.o.warmup['status'], 'failed')
        self.o.runner.assert_not_called()

    def test_changed_session_cannot_be_warmed(self):
        self.raw['started_at'] += 1
        with patch('optimizer.prewarm') as call:
            with self.assertRaises(ExternalChange):
                self.o.perform_prewarm('a', self.o.raw, ['--local-endpoint'])
        call.assert_not_called()

    def test_model_change_during_decode_does_not_get_ready_or_restart(self):
        def change(*args, **kwargs):
            self.raw['advertised_models'] = ['b']

        with patch('optimizer.prewarm', side_effect=change):
            with self.assertRaises(ExternalChange):
                self.o.perform_prewarm('a', copy.deepcopy(self.raw), ['--local-endpoint'])
        self.assertEqual(self.o.warmup['status'], 'failed')
        self.o.runner.assert_not_called()

    def test_rechecks_idle_state_before_request(self):
        original = copy.deepcopy(self.raw)
        self.raw['inference_active'] = True
        with patch('optimizer.prewarm') as call:
            with self.assertRaisesRegex(WarmupError, 'idle'):
                self.o.perform_prewarm('a', original, ['--local-endpoint'])
        call.assert_not_called()

    def test_memory_does_not_double_count_a_hypothetical_unload(self):
        self.raw['warm_models'] = []
        self.o.live['hardware']['memoryAvailableGB'] = 15
        self.assertIn('memory', self.o.prewarm_reason(self.raw, NOW))
        self.raw['warm_models'] = ['a']
        self.assertIsNone(self.o.prewarm_reason(self.raw, NOW))

    def test_power_thermal_identity_and_stale_readings_hold_warmup(self):
        self.o.on_ac_power.return_value = False
        self.assertIn('battery', self.o.prewarm_reason(self.raw, NOW))
        self.o.on_ac_power.return_value = True
        self.o.live['hardware']['gpuTemp'] = 98
        self.assertIn('hot', self.o.prewarm_reason(self.raw, NOW))
        self.o.live['hardware']['gpuTemp'] = 60
        self.o.identity_session = ('old', 123)
        self.assertIn('session', self.o.prewarm_reason(self.raw, NOW))
        self.o.identity_session = (self.raw['started_at'], 123)
        self.o.live['at'] = NOW - 30
        self.assertIn('fresh', self.o.prewarm_reason(self.raw, NOW))

    def test_external_restart_in_observe_mode_gets_one_warmup(self):
        self.assertEqual(self.o.state['mode'], 'observe')
        with patch('optimizer.local_request'), patch('optimizer.prewarm') as call:
            self.assertTrue(self.o.tick_prewarm(NOW))
            self.o.warmup_worker.join(2)
            self.assertFalse(self.o.tick_prewarm(NOW + 30))
            call.assert_called_once()
        self.assertEqual(self.o.state['mode'], 'observe')
        self.o.runner.assert_not_called()

    def test_successful_warmup_does_not_fight_later_idle_unload(self):
        self.o.warmup = {'session': session_key(self.raw), 'model': 'a', 'status': 'ready'}
        self.o.raw['warm_models'] = []
        self.assertFalse(self.o.tick_prewarm(NOW))
        self.assertEqual(self.o.warmup_snapshot()['status'], 'cold')

    def test_failed_external_warmup_is_bounded(self):
        self.o.warmup = {
            'session': session_key(self.raw),
            'model': 'a',
            'status': 'failed',
            'attempts': 2,
        }
        self.assertFalse(self.o.tick_prewarm(NOW))
        self.o.runner.assert_not_called()

    def test_already_served_output_proves_warm_without_competing_with_paid_work(self):
        self.raw['stats'] = {'requests_served': 1, 'tokens_generated': 10}
        self.raw['inference_active'] = True
        self.o.raw = copy.deepcopy(self.raw)
        self.assertFalse(self.o.tick_prewarm(NOW))
        self.assertEqual(self.o.warmup_snapshot()['status'], 'ready')
        self.o.runner.assert_not_called()

    def test_serving_counters_need_loaded_status_and_current_session(self):
        self.raw['stats'] = {'requests_served': 1, 'tokens_generated': 10}
        self.raw['warm_models'] = []
        self.assertFalse(self.o.served_warm(self.raw, NOW))
        self.raw['warm_models'] = ['a']
        self.raw['started_at'] += 1
        self.assertFalse(self.o.served_warm(self.raw, NOW))

    def test_managed_switch_does_not_accept_online_without_warmup(self):
        self.o.prewarm_reason = Mock(return_value=None)
        self.o.perform_prewarm = Mock(side_effect=WarmupError('cold'))
        self.assertFalse(self.o.verify_started('a', NOW - 1000, timeout=30))
        self.o.perform_prewarm.assert_called_once()

    def test_managed_switch_verifies_decode_before_success(self):
        self.o.prewarm_reason = Mock(return_value=None)
        self.o.perform_prewarm = Mock(return_value=True)
        self.assertTrue(self.o.verify_started('a', NOW - 1000, timeout=30))
        self.o.perform_prewarm.assert_called_once()

    def test_prior_session_and_explicit_stop_are_not_verified(self):
        self.o.perform_prewarm = Mock()
        self.assertFalse(self.o.verify_started('a', self.raw['started_at'], timeout=6))
        self.o.perform_prewarm.assert_not_called()
        self.o.service_disabled.return_value = True
        with self.assertRaises(ExternalChange):
            self.o.verify_started('a', NOW - 1000, timeout=6)

    def test_busy_recovery_is_never_forced(self):
        self.raw['inference_active'] = True
        self.assertIsNone(self.o.recovery_ready('b', 'a', device_id(self.raw), timeout=14))
        self.o.runner.assert_not_called()

    def test_recovery_requires_continuous_idle_and_same_device(self):
        self.assertIsNotNone(self.o.recovery_ready('b', 'a', device_id(self.raw), timeout=16))
        self.assertGreaterEqual(self.now - NOW, 12)
        self.assertIsNone(self.o.recovery_ready('b', 'a', 'other', timeout=16))

    def test_launch_options_changed_during_start_are_not_verified_or_warmed(self):
        expected = launch_signature(['--local-endpoint'], {})
        self.o.read_options.return_value = ('a', ['--local-endpoint', '--port', '9000'], {})
        self.o.perform_prewarm = Mock()
        with self.assertRaises(ExternalChange):
            self.o.verify_started('a', NOW - 1000, timeout=6, expected_launch=expected)
        self.o.perform_prewarm.assert_not_called()

    def test_environment_change_prevents_failed_switch_restoration(self):
        expected = launch_signature(['--local-endpoint'], {})
        self.o.read_options.return_value = ('a', ['--local-endpoint'], {'DB_MAX_CONCURRENCY': '2'})
        self.assertIsNone(
            self.o.recovery_ready(
                'b', 'a', device_id(self.raw), timeout=16, expected_launch=expected
            )
        )
        self.o.runner.assert_not_called()

    def test_unchanged_launch_settings_allow_idle_recovery(self):
        expected = launch_signature(['--local-endpoint'], {})
        self.assertIsNotNone(
            self.o.recovery_ready(
                'b', 'a', device_id(self.raw), timeout=16, expected_launch=expected
            )
        )

    def test_local_warmup_time_is_not_recorded_as_paid_work(self):
        self.o.warmup = {'status': 'warming'}
        self.o.store.sample = Mock()
        self.o.observe('test', self.raw, self.o.live)
        self.o.store.sample.assert_not_called()
        self.assertIsNone(self.o.idle_since)


if __name__ == '__main__':
    unittest.main()
