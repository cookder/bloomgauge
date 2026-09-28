import copy, json, plistlib, tempfile, threading, time, unittest, uuid
from unittest.mock import Mock, patch
from history import History
from optimizer import Optimizer, launch_options
from optimizer_store import device_id
from provider_control import endpoint_issue


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.h = History(':memory:')
        self.net = Mock()
        self.stop = threading.Event()
        self.o = Optimizer(self.h, self.net, self.tmp.name, self.stop, Mock())
        self.p = self.o.provider_control
        self.o.plist_path.parent.mkdir(parents=True)
        self.args = ['--model', 'a', '--local-endpoint', '--idle-timeout', '3600']
        self.write_plist()
        self.now = time.time()
        self.raw = {
            'pid': 123,
            'started_at': self.now - 100,
            'written_at': self.now,
            'advertised_models': ['a'],
            'attestation_public_key': 'fixture',
            'inference_active': False,
            'stats': {'requests_served': 10, 'tokens_generated': 50},
            'trust': {'status': 'online', 'trust_level': 'hardware'},
        }
        self.o.raw = copy.deepcopy(self.raw)
        self.o.read_state = Mock(side_effect=lambda: copy.deepcopy(self.raw))
        self.o.service_disabled = Mock(return_value=False)
        self.process = patch('provider_control.matching_process', return_value=True).start()
        self.addCleanup(patch.stopall)
        self.o.live = {
            'at': self.now,
            'account': 'acct',
            'device': device_id(self.raw),
            'provider': {'online': True, 'memoryGB': 10},
            'hardware': {
                'at': self.now,
                'memoryTotalGB': 64,
                'memoryUsedGB': 20,
                'memoryAvailableGB': 44,
                'cpuTemp': 50,
                'gpuTemp': 50,
                'thermal': 'Nominal',
            },
        }
        self.o.identity_ok = True
        self.o.identity_at = self.now
        self.o.identity_session = (self.raw['started_at'], self.raw['pid'])
        self.o.discovery_at = self.now
        self.o.on_ac_power = Mock(return_value=True)
        self.o.idle_since = self.now - 20
        self.o.local = [
            {'id': 'a', 'estimated_memory_gb': 10, 'size_bytes': 1000, 'template_render_ok': True}
        ]
        self.o.catalog = [{'id': 'a', 'active': True, 'min_ram_gb': 16}]
        self.net.fetch.return_value = {'models': self.o.catalog}
        self.o.stop = Mock()
        self.o.stop.is_set.return_value = False
        self.o.stop.wait.return_value = False
        self.o.runner.side_effect = self.run_cli

    def tearDown(self):
        if self.o.worker:
            self.o.worker.join(2)
        self.h.close()
        self.tmp.cleanup()

    def write_plist(self):
        self.o.plist_path.write_bytes(
            plistlib.dumps(
                {
                    'ProgramArguments': [str(self.o.binary), 'start', *self.args],
                    'EnvironmentVariables': {'SAVED_OPTION': 'kept'},
                }
            )
        )

    def run_cli(self, args, **kw):
        if args[1:3] == ['models', 'list']:
            return Mock(stdout=json.dumps({'models': self.o.local}))
        if args[1] == 'stop':
            self.process.return_value = False
            self.o.service_disabled.return_value = True
        if args[1] == 'start':
            self.raw.update(pid=124, started_at=self.now + 1, written_at=time.time())
            self.process.return_value = True
            self.o.service_disabled.return_value = False
            self.args = args[2:]
            self.write_plist()
        return Mock(returncode=0)

    def payload(self, kind):
        return {
            'action': kind,
            'requestId': str(uuid.uuid4()),
            'expectedProvider': self.p.snapshot()['version'],
        }

    def dispatch(self, data, source='phone'):
        self.p.action(data, source)
        if self.o.worker:
            self.o.worker.join(2)
        return self.o.state['providerResult']

    def provider_calls(self):
        return [
            c.args[0]
            for c in self.o.runner.call_args_list
            if len(c.args[0]) > 1 and c.args[0][1] in ('start', 'stop')
        ]

    def test_phone_stop_is_explicit_verified_and_pauses_automation(self):
        self.o.state['mode'] = 'demand'
        data = self.payload('provider-stop')
        result = self.dispatch(data)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(self.o.state['mode'], 'observe')
        self.assertEqual(len(self.provider_calls()), 1)
        self.p.action(data, 'phone')
        self.assertEqual(len(self.provider_calls()), 1)
        self.assertEqual(self.p.snapshot()['status'], 'stopped')

    def test_start_saved_model_and_environment_in_background(self):
        self.process.return_value = False
        self.o.service_disabled.return_value = True
        self.o.live['provider']['online'] = False
        result = self.dispatch(self.payload('provider-start'))
        self.assertEqual(result['status'], 'completed')
        cmd = self.provider_calls()[0]
        self.assertIn('--model', cmd)
        self.assertIn('--idle-timeout', cmd)
        self.assertNotIn('--foreground', cmd)
        self.assertNotEqual(self.o.warmup.get('status'), 'ready')

    def test_start_memory_and_power_checks(self):
        self.process.return_value = False
        self.o.service_disabled.return_value = True
        self.o.live['hardware']['memoryAvailableGB'] = 1
        result = self.dispatch(self.payload('provider-start'))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.provider_calls(), [])

    def test_pair_start_requires_each_weight_and_pair_headroom(self):
        self.process.return_value = False
        self.o.service_disabled.return_value = True
        self.args += ['--model', 'b']
        self.write_plist()
        self.raw['advertised_models'] = ['a', 'b']
        self.o.local.append(
            {'id': 'b', 'estimated_memory_gb': 0, 'size_bytes': 1000, 'template_render_ok': True}
        )
        self.assertEqual(self.dispatch(self.payload('provider-start'))['status'], 'failed')
        self.o.local[-1]['estimated_memory_gb'] = 10
        self.o.live['hardware']['memoryAvailableGB'] = 33
        self.assertEqual(self.dispatch(self.payload('provider-start'))['status'], 'failed')
        self.assertEqual(self.provider_calls(), [])

    def test_battery_start_stays_stopped(self):
        self.process.return_value = False
        self.o.service_disabled.return_value = True
        self.o.on_ac_power.return_value = False
        self.assertEqual(self.dispatch(self.payload('provider-start'))['status'], 'failed')
        self.assertEqual(self.provider_calls(), [])

    def test_no_commands_from_snapshot(self):
        self.p.snapshot()
        self.p.snapshot()
        self.assertEqual(self.provider_calls(), [])

    def test_missing_endpoint_guidance_matches_current_model_controls(self):
        self.args.remove('--local-endpoint')
        self.write_plist()
        value = self.p.snapshot()
        self.assertEqual(value['endpoint'], 'setup')
        self.assertIn('Optimizer → Overview on the Mac', value['endpointDetail'])
        self.assertIn('Prepare, Start or Switch', value['endpointDetail'])
        self.assertNotIn('Controller', value['endpointDetail'])
        self.assertNotIn('Enable pre-warming', value['endpointDetail'])
        self.assertTrue(value['canEnableEndpoint'])
        self.assertEqual(self.provider_calls(), [])

    def test_stopped_and_unavailable_guidance_points_to_overview(self):
        self.process.return_value = False
        self.assertIn('Optimizer → Overview', self.p.snapshot()['detail'])
        with patch.object(self.p, 'inspect', side_effect=ValueError('Fixture')):
            self.assertIn('Optimizer → Overview', self.p.snapshot()['detail'])
        self.assertEqual(self.provider_calls(), [])

    def test_configuration_review_is_specific_and_never_rewrites_the_file(self):
        p = self.o.home / '.config/darkbloom/provider.toml'
        p.parent.mkdir(parents=True, exist_ok=True)
        # memory_reserve_gb is read into Bloomkeeper's load budgets; an unknown memory
        # setting only stops automatic moves, and the card says so.
        for text, blocked in (
            ('[provider]\nmemory_reserve_gb = 4', False),
            ('[provider]\nmemory_reserve_gb = 12', False),
            ('[provider]\nmemory_reserve_gb = 4\nkv_reserve_gb = 1', True),
        ):
            p.write_text(text)
            value = self.p.snapshot()
            self.assertEqual(bool(value['configurationIssue']), blocked)
            if blocked:
                self.assertIn('kv_reserve_gb', value['configurationIssue'])
                self.assertIn('restores and your own picks still work', value['configurationIssue'])
            self.assertEqual(p.read_text(), text)
        self.assertEqual(self.provider_calls(), [])

    def test_snapshot_distinguishes_work_in_progress_from_running(self):
        self.assertFalse(self.p.snapshot()['operationPending'])
        self.o.state['pending'] = {'model': 'a'}
        value = self.p.snapshot()
        self.assertEqual(value['status'], 'running')
        self.assertTrue(value['operationPending'])
        self.assertFalse(value['canStart'])
        self.assertFalse(value['canStop'])

    def test_stale_control_and_reused_id_rejected(self):
        data = self.payload('provider-stop')
        self.args += ['--port', '8001']
        self.write_plist()
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.p.action(data, 'phone')
        data = self.payload('provider-stop')
        self.dispatch(data)
        with self.assertRaisesRegex(ValueError, 'different'):
            self.p.action({**data, 'action': 'provider-start'}, 'phone')

    def test_endpoint_setup_local_only_and_authenticated(self):
        self.args.remove('--local-endpoint')
        self.write_plist()
        data = self.payload('provider-endpoint')
        with self.assertRaisesRegex(ValueError, 'on the Mac'):
            self.p.action(data, 'phone')
        with patch(
            'provider_control.local_request',
            return_value=('http://127.0.0.1:8000/v1/chat/completions', 'private'),
        ):
            result = self.dispatch(data, 'mac')
        self.assertEqual(result['status'], 'completed')
        self.assertIn('--local-endpoint', self.provider_calls()[0])
        self.assertNotIn('--no-auth', self.provider_calls()[0])

    def test_endpoint_setup_never_interrupts_busy_work(self):
        self.args.remove('--local-endpoint')
        self.write_plist()
        self.raw['inference_active'] = True
        with self.assertRaisesRegex(ValueError, 'idle'):
            self.p.action(self.payload('provider-endpoint'), 'mac')
        self.assertEqual(self.provider_calls(), [])

    def test_endpoint_setup_can_explicitly_start_a_stopped_provider(self):
        self.args.remove('--local-endpoint')
        self.write_plist()
        self.process.return_value = False
        self.o.service_disabled.return_value = True
        self.o.live['provider']['online'] = False
        self.o.idle_since = None
        self.o.identity_ok = False
        self.assertTrue(self.p.snapshot()['canEnableEndpoint'])
        with patch(
            'provider_control.local_request',
            return_value=('http://127.0.0.1:8000/v1/chat/completions', 'private'),
        ):
            result = self.dispatch(self.payload('provider-endpoint'), 'mac')
        self.assertEqual(result['status'], 'completed')
        self.assertIn('--local-endpoint', self.provider_calls()[0])

    def test_discovery_error_is_not_claimed_ready(self):
        self.assertEqual(self.p.snapshot()['endpoint'], 'waiting')

    def test_no_auth_or_remote_bind_not_rewritten(self):
        for flag in (['--no-auth'], ['--bind', '0.0.0.0']):
            self.args = ['--model', 'a', *flag]
            self.write_plist()
            self.assertFalse(self.p.snapshot()['canEnableEndpoint'])
            self.assertTrue(endpoint_issue(flag))

    def test_worker_and_update_admission(self):
        data = self.payload('provider-stop')
        for field, value in [('pending', {'model': 'a'}), ('requestedModel', 'b')]:
            self.o.state[field] = value
            with self.assertRaises(ValueError):
                self.p.action(data, 'phone')
            self.o.state.pop(field)
        self.o.update_guard.action({'action': 'prepare', 'requestId': str(uuid.uuid4())})
        with self.assertRaisesRegex(ValueError, 'update'):
            self.p.action(data, 'phone')
        self.assertEqual(self.provider_calls(), [])

    def test_strict_payload_and_no_shell_arguments(self):
        for extra in ({'command': 'purge'}, {'model': 'other'}, {'source': 'mac'}):
            with self.assertRaises(ValueError):
                self.p.action({**self.payload('provider-stop'), **extra}, 'phone')
        self.assertEqual(self.provider_calls(), [])

    def test_external_change_after_admission_sends_nothing(self):
        captured = []

        class Deferred:
            def __init__(s, **kw):
                captured.append(kw)

            def start(s):
                pass

            def is_alive(s):
                return False

            def join(s, *a):
                pass

        with patch('provider_control.threading.Thread', Deferred):
            self.p.action(self.payload('provider-stop'), 'phone')
        self.raw['pid'] = 999
        captured[0]['target'](*captured[0]['args'])
        self.assertEqual(self.provider_calls(), [])
        self.assertEqual(self.o.state['providerResult']['status'], 'failed')

    def test_auto_selection_parse_is_not_a_shell_or_local_only_escape(self):
        self.assertEqual(
            launch_options(
                {'ProgramArguments': ['darkbloom', 'start', '--local-endpoint']}, allow_auto=True
            ),
            (None, ['--local-endpoint']),
        )
        for args in (['--model', 'a', '--model', 'a'], ['anything'], ['--port=8000'], ['--']):
            with self.assertRaises(ValueError):
                launch_options({'ProgramArguments': ['darkbloom', 'start', *args]}, allow_auto=True)
        # An unknown flag is kept verbatim, never read as the local endpoint.
        parsed = launch_options({'ProgramArguments': ['darkbloom', 'start', '--local']}, True)
        self.assertEqual(parsed, (None, ['--local']))
        self.assertIn('Prepare', endpoint_issue(parsed[1]))

    def test_auto_select_launch_uses_actual_advertised_selection(self):
        self.args = ['--local-endpoint']
        self.write_plist()
        self.assertEqual(self.o.read_options()[0], 'a')
        self.raw['advertised_models'] = []
        with self.assertRaisesRegex(ValueError, 'select a serving'):
            self.o.read_options()

    def test_automatic_switch_waits_for_endpoint_setup_before_restart(self):
        self.args = ['--model', 'a']
        self.write_plist()
        self.assertIn('pre-warming', self.o.wait_reason(self.o.live, time.time()))
        self.assertEqual(self.provider_calls(), [])

    def test_stale_alive_provider_is_not_reported_stopped(self):
        self.raw['written_at'] = self.now - 60
        self.o.service_disabled.return_value = True
        self.assertEqual(self.p.snapshot()['status'], 'unknown')
        self.assertFalse(self.p.snapshot()['canStart'])


if __name__ == '__main__':
    unittest.main()
