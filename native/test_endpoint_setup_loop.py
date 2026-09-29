"""Endpoint setup is not an outside change (1.36.60 beta 41 report, M1 Ultra, Darkbloom 0.9.10).

Every Prepare or Start that set up pre-warming ended with "Provider settings changed during the
switch": BloomGauge compared a digest of the argv it passed with the launch agent that
`darkbloom start` rebuilds from its own options, which always adds `--port`/`--bind` to
`--local-endpoint`. The On card kept asking for the same setup. Synthetic providers only; the
fake `darkbloom start` below writes what Darkbloom 0.9.10 writes. Never a real CLI."""

import copy
import plistlib
import subprocess
import time
import unittest
import uuid
from unittest.mock import Mock

import manager
from optimizer import launch_signature, session_key
from provider_control import endpoint_issue
import test_manual_selection
import test_provider_control

COORDINATOR = 'wss://api.darkbloom.dev/ws/provider'
SEVEN = ['a', 'b', 'c', 'd', 'e', 'f', 'g']


def darkbloom_launch(binary, args):
    """The launch agent Darkbloom 0.9.10's `start` saves: LaunchAgent.serviceProgramArguments
    rebuilds it from the parsed options instead of keeping the argv it was given."""
    coordinator, config, models = COORDINATOR, None, []
    endpoint, port, bind, no_auth = False, '8000', '127.0.0.1', False
    i = 0
    while i < len(args):
        flag = args[i]
        if flag in ('--coordinator-url', '--config', '-c', '--model', '--port', '--bind'):
            value = args[i + 1]
            if flag == '--coordinator-url':
                coordinator = value
            elif flag in ('--config', '-c'):
                config = value
            elif flag == '--model':
                models.append(value)
            elif flag == '--port':
                port = value
            else:
                bind = value
            i += 2
            continue
        if flag == '--idle-timeout':  # saved to provider.toml, not the launch agent
            i += 2
            continue
        endpoint |= flag == '--local-endpoint'
        no_auth |= flag == '--no-auth'
        i += 1
    argv = [str(binary), 'start', '--foreground', '--coordinator-url', coordinator]
    if config:
        argv += ['--config', config]
    for model in models:
        argv += ['--model', model]
    if endpoint:
        argv += ['--local-endpoint', '--port', port, '--bind', bind]
        if no_auth:
            argv.append('--no-auth')
    return argv, models


class EndpointSetupLoopTests(unittest.TestCase):
    tearDown = test_provider_control.ProviderTests.tearDown
    provider_calls = test_provider_control.ProviderTests.provider_calls
    payload = test_manual_selection.SelectionTests.payload
    admit = test_manual_selection.SelectionTests.admit
    run_worker = test_manual_selection.SelectionTests.run_worker
    row = test_manual_selection.SelectionTests.row
    verified = test_manual_selection.SelectionTests.verified

    def setUp(self):
        test_manual_selection.SelectionTests.setUp(self)
        # As `darkbloom start` (no --local-endpoint) left Jason's Mac.
        self.args = ['--foreground', '--coordinator-url', COORDINATOR, '--model', 'a']
        self.env = {}
        self.write_plist()
        self.write_toml(['a'])
        self.start_error = None
        del self.o.verify_started  # the real verification, not the fixture's
        self.o.served_warm = Mock(return_value=True)
        self.o.prewarm_reason = Mock(return_value='Waiting for idle capacity.')
        self.o.recovery_ready = Mock(return_value=None)

    def write_plist(self):
        plist = {'ProgramArguments': [str(self.o.binary), 'start', *self.args]}
        env = getattr(self, 'env', {'SAVED_OPTION': 'kept'})
        if env:
            plist['EnvironmentVariables'] = env
        self.o.plist_path.write_bytes(plistlib.dumps(plist))

    def write_toml(self, models, multiline=False):
        path = self.o.home / '.config/darkbloom/provider.toml'
        path.parent.mkdir(parents=True, exist_ok=True)
        if multiline:  # TOMLKit wraps long arrays
            value = '[\n' + ''.join("    '%s',\n" % m for m in models) + ']'
        else:
            value = '[ %s ]' % ', '.join("'%s'" % m for m in models)
        path.write_text(
            'config_version = 3\n\n[backend]\nenabled_models = %s\nstartup_preload = true\n' % value
        )

    def run_cli(self, args, **kw):
        if args[1] == 'start' and self.start_error:
            raise self.start_error
        result = test_manual_selection.SelectionTests.run_cli(self, args, **kw)
        if args[1] == 'start':
            argv, models = darkbloom_launch(self.o.binary, args[2:])
            self.args = argv[2:]
            self.env = {}  # only Darkbloom's passthrough variables are kept
            self.write_plist()
            self.write_toml(models)  # ProviderModelSelection stages enabled_models
        return result

    def plist_args(self):
        return plistlib.loads(self.o.plist_path.read_bytes())['ProgramArguments'][2:]

    def arm(self, target, previous='a'):
        v = self.p.inspect()
        self.o.state['pending'] = {
            'kind': 'manual',
            'model': target,
            'previous': previous,
            'requestId': self.o.state['requestId'],
            'requestedAt': self.o.state['requestedAt'],
            'session': session_key(self.raw),
            'selectionAction': True,
            'verifyRuntime': self.o.state['requestedVerifyRuntime'],
            'launchSignature': launch_signature(v['options'], v['environment']),
        }

    def prepare(self, target='a'):
        self.admit(self.payload(model=target))
        self.assertEqual(self.o.state['manualResult']['status'], 'queued')
        self.arm(target)
        self.o.switch('a', target, 'acct', self.o.live['device'])
        return self.o.state['manualResult']

    def blocked_on(self, detail):
        self.o.automatic_control.operation = {
            'id': str(uuid.uuid4()),
            'status': 'blocked',
            'detail': detail,
            'blocker': {'code': 'readiness', 'action': 'configure'},
        }

    def card(self):
        return self.o.automatic_control.projection(time.time())['automatic']

    # Jason's case: Prepare on the serving model, Darkbloom adds --port/--bind.

    def test_prepare_sets_up_pre_warming_without_a_false_outside_change(self):
        setup = endpoint_issue([])
        self.assertEqual(endpoint_issue(self.p.inspect()['options']), setup)
        self.blocked_on(setup)  # On asked for the setup earlier
        self.assertEqual(self.card()['detail'], setup)
        result = self.prepare('a')
        self.assertEqual(result['status'], 'completed', result['detail'])
        self.assertIn(
            '--local-endpoint', self.provider_calls()[-1], 'BloomGauge asked for the endpoint'
        )
        self.assertEqual(
            self.plist_args()[-5:], ['--local-endpoint', '--port', '8000', '--bind', '127.0.0.1']
        )
        self.assertIsNone(endpoint_issue(self.p.inspect()['options']))
        # The On card no longer asks for the setup that is now done.
        card = self.card()
        self.assertNotEqual(card['phase'], 'blocked')
        self.assertNotEqual(card['detail'], setup)
        self.assertEqual(len(self.provider_calls()), 1)

    def test_switch_with_setup_to_another_model_is_verified(self):
        result = self.prepare('b')
        self.assertEqual(result['status'], 'completed', result['detail'])
        self.assertEqual(self.o.state['expectedModel'], 'b')
        self.assertNotIn('lastSwitchFailure', self.o.state)

    def test_darkbloom_normalizing_old_launch_settings_is_not_an_outside_change(self):
        # An agent from an older Darkbloom: no --bind, --idle-timeout in argv, a variable
        # outside Darkbloom's passthrough list. `start` rewrites all three.
        self.args = ['--model', 'a', '--local-endpoint', '--port', '8000', '--idle-timeout', '0']
        self.env = {'SAVED_OPTION': 'kept'}
        self.write_plist()
        result = self.prepare('b')
        self.assertEqual(result['status'], 'completed', result['detail'])
        self.assertEqual(self.provider_calls()[-1].count('--local-endpoint'), 1)

    def test_start_with_setup_from_stopped_is_verified(self):
        self.process.return_value = False
        self.o.service_disabled.return_value = True
        self.o.live['provider']['online'] = False
        self.o.identity_ok = False
        self.o.idle_since = None
        self.raw['written_at'] = self.now - 600
        self.o.raw = copy.deepcopy(self.raw)
        self.assertTrue(self.row()['canStart'])
        self.admit()
        result = self.run_worker()['selectionResult']
        self.assertEqual(result['status'], 'completed', result['detail'])
        self.assertIn('--local-endpoint', self.plist_args())

    # Real outside changes are still caught.

    def outside_change(self, change):
        self.o.served_warm = Mock(return_value=False)

        def wait(seconds):
            self.args = change(self.plist_args())
            self.write_plist()
            return False

        self.o.stop.wait.side_effect = wait
        return self.prepare('b')

    def test_user_editing_the_endpoint_during_verification_is_an_outside_change(self):
        result = self.outside_change(
            lambda args: [('9000' if v == '8000' else v) for v in args]
        )
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(result['detail'].startswith('Provider settings changed during the switch.'))

    def test_user_picking_another_model_during_verification_is_an_outside_change(self):
        def darkbloom_start_c(args):  # `darkbloom start --model c` in Terminal
            self.write_toml(['c'])
            return [('c' if v == 'b' else v) for v in args]

        result = self.outside_change(darkbloom_start_c)
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(result['detail'].startswith('Provider settings changed during the switch.'))

    def test_darkbloom_dropping_the_requested_endpoint_is_not_accepted(self):
        run = self.run_cli

        def ignore_endpoint(args, **kw):
            return run([a for a in args if a != '--local-endpoint'], **kw)

        self.o.runner.side_effect = ignore_endpoint
        result = self.prepare('b')
        self.assertEqual(result['status'], 'failed')
        self.assertIn('settings changed', result['detail'])

    # Darkbloom set to serve three or more models.

    def test_three_or_more_enabled_models_get_a_plain_message(self):
        notice = (
            'Darkbloom is set to serve 7 models. Run `darkbloom start` in Terminal and pick '
            'one model, or use BloomGauge’s model controls.'
        )
        self.args = self.args + ['--local-endpoint', '--port', '8000', '--bind', '127.0.0.1']
        self.write_plist()
        self.write_toml(SEVEN, multiline=True)
        self.assertIsNone(manager.toml_selection(self.o.home, []))
        # Darkbloom's launchd child follows enabled_models, not --model.
        self.raw['advertised_models'] = list(SEVEN)
        self.raw['version'] = '0.9.10'
        self.o.raw = copy.deepcopy(self.raw)
        card = self.card()
        self.assertEqual((card['phase'], card['detail']), ('blocked', notice))
        # The model controls the notice points to take the pick (test_bug_matrix
        # ThreeModelTests: it replaces the list through `darkbloom start`).
        self.assertTrue(self.row()['canSwitch'], self.row()['switchReason'])
        # A Darkbloom without a graceful drain would cut off accepted work: Terminal only.
        self.raw['version'] = '0.9.8'
        self.o.raw = copy.deepcopy(self.raw)
        self.assertEqual(
            self.row()['switchReason'],
            'Darkbloom serves 7 models. Run `darkbloom start` in Terminal and pick one model.',
        )

    def test_picker_start_with_several_models_is_named_not_set_up_again(self):
        # `darkbloom start`'s picker saves every pick as --model and in provider.toml.
        self.args = ['--foreground', '--coordinator-url', COORDINATOR]
        for model in SEVEN[:4]:
            self.args += ['--model', model]
        self.write_plist()
        self.write_toml(SEVEN[:4])
        card = self.card()
        self.assertEqual(card['phase'], 'blocked')
        self.assertTrue(card['detail'].startswith('Darkbloom is set to serve 4 models.'))

    def test_model_controls_still_replace_a_saved_multi_model_set(self):
        # Serving the launch agent's model, but the next restart would serve the list.
        self.write_toml(SEVEN)
        card = self.card()
        self.assertTrue(card['detail'].startswith('Darkbloom is set to serve 7 models.'))
        self.assertNotEqual(card['phase'], 'blocked')
        self.assertTrue(self.row()['canSwitch'])
        result = self.prepare('b')
        self.assertEqual(result['status'], 'completed', result['detail'])
        self.assertFalse(self.card()['detail'].startswith('Darkbloom is set to serve'))

    def test_failed_start_that_left_several_models_says_so(self):
        self.write_toml(SEVEN)
        self.start_error = subprocess.CalledProcessError(1, ['darkbloom'])
        result = self.prepare('b')
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(
            result['detail'].startswith('Darkbloom is set to serve 7 models.'), result['detail']
        )

    # Two failures with the same cause: the cause and the manual fix, not the same request.

    def test_repeated_setup_failure_shows_cause_and_manual_fix(self):
        setup = endpoint_issue([])
        self.blocked_on(setup)
        self.start_error = subprocess.CalledProcessError(1, ['darkbloom'])
        first = self.prepare('a')
        self.assertEqual(first['status'], 'failed')
        self.assertNotIn('darkbloom start --local-endpoint', first['detail'])
        self.assertEqual(self.card()['detail'], setup)
        second = self.prepare('a')
        self.assertEqual(second['status'], 'failed')
        fix = '`darkbloom start --local-endpoint --model a`'
        for text in (second['detail'], self.card()['detail']):
            self.assertIn('Darkbloom’s start command failed.', text)
            self.assertIn(fix, text)
            self.assertNotIn('use Prepare, Start or Switch', text)
        self.assertIn(fix, self.o.wait_reason(self.o.live, time.time()))
        # Setting it up (here by hand) clears the notice.
        self.start_error = None
        self.args = self.args + ['--local-endpoint', '--port', '8000', '--bind', '127.0.0.1']
        self.write_plist()
        self.assertNotEqual(self.card()['phase'], 'blocked')

    def test_different_causes_do_not_count_as_repeats(self):
        self.start_error = subprocess.CalledProcessError(1, ['darkbloom'])
        self.prepare('a')
        self.start_error = subprocess.TimeoutExpired(['darkbloom'], 60)
        second = self.prepare('a')
        self.assertNotIn('darkbloom start --local-endpoint', second['detail'])


if __name__ == '__main__':
    unittest.main()
