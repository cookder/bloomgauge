import email.message
import json
import pathlib
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import Mock, patch

from cache_permission import cache_permission
from collector import Collector, Handler, ThreadingHTTPServer
from remote import Remote, consent_url, identity, serve_slot

HOST = 'bloom.example.ts.net'
OWNER = 'owner@example.com'
TARGET = 'http://127.0.0.1:8766'
STATUS = {
    'BackendState': 'Running',
    'Self': {'DNSName': HOST + '.', 'UserID': 42},
    'User': {'42': {'LoginName': OWNER}},
}


def serve_for(target):
    return {
        'TCP': {'8443': {'HTTPS': True}},
        'Web': {HOST + ':8443': {'Handlers': {'/': {'Proxy': target}}}},
    }


# Phone access as set up before the secret prefix (1.36.53 and earlier).
SERVE = serve_for(TARGET)


class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / 'remote-access.json'
        self.calls, self.serve = [], {}
        self.status = STATUS
        self.failure = False

        def runner(args):
            self.calls.append(args)
            if self.failure:
                return 1, '', 'disconnected'
            if args == ['status', '--json']:
                return 0, json.dumps(self.status), ''
            if args == ['serve', 'status', '--json']:
                return 0, json.dumps(self.serve), ''
            if args[-1] == 'off':
                self.serve = {}
                return 0, '', ''
            if args[-1].startswith(TARGET + '/'):
                self.serve = serve_for(args[-1])
                return 0, '', ''
            raise AssertionError(args)

        self.remote = Remote(self.path, runner=runner, binary=lambda: '/fake/tailscale')

    def tearDown(self):
        self.tmp.cleanup()

    def headers(self, owner=OWNER):
        h = email.message.Message()
        h['Host'] = HOST + ':8443'
        h['X-Forwarded-Proto'] = 'https'
        if owner:
            h['Tailscale-User-Login'] = owner
        return h

    def test_explicit_enable_persists_and_only_owner_can_read(self):
        self.assertFalse(self.remote.authorize(self.headers()))
        state = self.remote.action('enable')
        self.assertEqual(state['url'], 'https://' + HOST + ':8443')
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertTrue(self.remote.authorize(self.headers()))
        self.assertFalse(self.remote.authorize(self.headers('friend@example.com')))
        self.assertFalse(
            self.remote.authorize(self.headers(None))
        )  # Funnel and tagged nodes lack identity.
        h = self.headers()
        h['Tailscale-User-Login'] = OWNER
        self.assertFalse(self.remote.authorize(h))
        self.assertFalse(any('funnel' in call for call in self.calls))

    def test_serve_target_carries_a_private_secret(self):
        self.remote.action('enable')
        target = self.serve['Web'][HOST + ':8443']['Handlers']['/']['Proxy']
        secret = target.removeprefix(TARGET + '/')
        self.assertRegex(secret, r'^[A-Za-z0-9_-]{32}$')
        self.assertEqual(json.loads(self.path.read_text())['secret'], secret)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(secret, json.dumps(self.remote.snapshot()))
        self.assertNotIn(secret, json.dumps(self.remote.snapshot(remote=True)))
        self.assertEqual(self.remote.phone_path(f'/{secret}/api/snapshot?x=1'), '/api/snapshot?x=1')
        self.assertEqual(self.remote.phone_path(f'/{secret}/'), '/')
        for path in ('/', '/api/snapshot', f'/{secret}', f'/{secret}x/', f'/x{secret}/', '', None):
            self.assertIsNone(self.remote.phone_path(path))
        # A restart keeps the secret, so Serve stays pointed at the listener.
        restarted = Remote(self.path, runner=self.remote.runner, binary=self.remote.binary)
        self.assertEqual(restarted.target, target)
        self.assertEqual(restarted.refresh()['status'], 'enabled')

    def test_disable_and_enable_again_uses_a_new_secret(self):
        self.remote.action('enable')
        first = self.remote.target
        self.remote.action('disable')
        self.assertEqual(self.serve, {})
        self.assertNotIn('secret', json.loads(self.path.read_text()))
        self.remote.action('enable')
        self.assertNotEqual(self.remote.target, first)
        self.assertEqual(self.remote.refresh()['status'], 'enabled')

    def test_phone_access_from_before_the_secret_moves_to_it(self):
        self.path.write_text(json.dumps({'host': HOST, 'owner': OWNER, 'enabled': True}))
        self.serve = SERVE
        remote = Remote(self.path, runner=self.remote.runner, binary=self.remote.binary)
        saved = json.loads(self.path.read_text())
        self.assertEqual(remote.target, TARGET + '/' + saved['secret'])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(remote.authorize(self.headers()))
        self.assertEqual(remote.refresh()['status'], 'enabled')
        self.assertEqual(self.serve, serve_for(remote.target))
        self.assertTrue(remote.authorize(self.headers()))
        self.assertIn(['serve', '--bg', '--https=8443', '--yes', remote.target], self.calls)

    def test_failed_move_is_retried_after_a_minute_not_every_scan(self):
        self.path.write_text(json.dumps({'host': HOST, 'owner': OWNER, 'enabled': True}))
        self.serve = SERVE
        runner = self.remote.runner

        def refusing(args):
            if '--bg' in args:
                self.calls.append(args)
                return 1, '', 'busy'
            return runner(args)

        remote = Remote(self.path, runner=refusing, binary=self.remote.binary)
        for _ in range(3):
            self.assertEqual(remote.refresh()['status'], 'off')
        self.assertFalse(remote.authorize(self.headers()))
        self.assertEqual(sum('--bg' in call for call in self.calls), 1)
        remote.repointed -= 61
        remote.runner = runner
        self.assertEqual(remote.refresh()['status'], 'enabled')

    def test_older_slots_move_only_for_the_same_account_and_listener(self):
        self.path.write_text(
            json.dumps({'host': HOST, 'owner': 'old@example.com', 'enabled': True})
        )
        self.serve = SERVE
        remote = Remote(self.path, runner=self.remote.runner, binary=self.remote.binary)
        self.assertEqual(remote.refresh()['status'], 'changed')
        self.assertEqual(self.serve, SERVE)
        self.assertFalse(any('--bg' in call for call in self.calls))
        old = TARGET + '/' + 'a' * 32
        self.assertEqual(serve_slot(serve_for(old), HOST, remote.target, TARGET), 'stale')
        self.assertEqual(serve_slot(serve_for(TARGET), HOST, remote.target, TARGET), 'stale')
        self.assertEqual(serve_slot(serve_for(old), HOST, remote.target), 'conflict')
        for other in (
            TARGET + '/other',
            TARGET + '/' + 'a' * 32 + '/x',
            'http://127.0.0.1:8767',
            'http://127.0.0.1:87660',
            'http://localhost:8766',
        ):
            self.assertEqual(serve_slot(serve_for(other), HOST, remote.target, TARGET), 'conflict')
        self.assertEqual(
            serve_slot(
                {**serve_for(TARGET), 'AllowFunnel': {HOST + ':8443': True}},
                HOST,
                remote.target,
                TARGET,
            ),
            'conflict',
        )
        two = serve_for(TARGET)
        two['Web'][HOST + ':8443']['Handlers']['/api'] = {'Proxy': 'http://127.0.0.1:3000'}
        self.assertEqual(serve_slot(two, HOST, remote.target, TARGET), 'conflict')

    def test_disable_also_removes_an_older_slot(self):
        self.path.write_text(json.dumps({'host': HOST, 'owner': OWNER, 'enabled': True}))
        self.serve = SERVE
        remote = Remote(self.path, runner=self.remote.runner, binary=self.remote.binary)
        remote.action('disable')
        self.assertEqual(self.serve, {})

    def test_other_services_and_public_funnel_are_never_overwritten(self):
        for config in [
            {**SERVE, 'AllowFunnel': {HOST + ':8443': True}},
            {'TCP': {'8443': {'TCPForward': 'localhost:8888'}}},
            {'Foreground': {'session': SERVE}},
            {'Web': {HOST + ':8443': {'Handlers': {'/': {'Proxy': 'http://127.0.0.1:3000'}}}}},
        ]:
            self.serve = config
            with self.assertRaises(ValueError):
                self.remote.action('enable')
            self.assertFalse(any('--bg' in call for call in self.calls))

    def test_different_port_configuration_is_preserved(self):
        self.assertEqual(serve_slot({'TCP': {'443': {'HTTPS': True}}}, HOST, TARGET), 'empty')

    def test_account_change_and_stale_identity_revoke_access(self):
        self.remote.action('enable')
        self.remote.checked = time.monotonic() - 60
        self.assertFalse(self.remote.authorize(self.headers()))
        self.remote.refresh()
        self.status = {**STATUS, 'User': {'42': {'LoginName': 'new@example.com'}}}
        self.assertEqual(self.remote.refresh()['status'], 'changed')
        self.assertFalse(self.remote.authorize(self.headers('new@example.com')))
        self.assertFalse(self.remote.authorize(self.headers()))

    def test_disable_revokes_even_when_tailscale_is_unreachable(self):
        self.remote.action('enable')
        self.failure = True
        self.remote.action('disable')
        self.assertFalse(self.remote.authorize(self.headers()))
        self.assertEqual(json.loads(self.path.read_text()), {'enabled': False})

    def test_restart_revalidates_saved_settings_before_trusting(self):
        self.remote.action('enable')
        restarted = Remote(self.path, runner=self.remote.runner, binary=self.remote.binary)
        self.assertNotEqual(
            restarted.snapshot()['statusInstance'], self.remote.snapshot()['statusInstance']
        )
        self.assertEqual(restarted.snapshot()['statusRevision'], 0)
        self.assertFalse(restarted.authorize(self.headers()))
        restarted.refresh()
        self.assertGreater(restarted.snapshot()['statusRevision'], 0)
        self.assertTrue(restarted.authorize(self.headers()))
        self.serve = {**SERVE, 'AllowFunnel': {HOST + ':8443': True}}
        restarted.refresh()
        self.assertFalse(restarted.authorize(self.headers()))

    def test_https_onboarding_timeout_has_actionable_verified_link(self):
        base_runner = self.remote.runner

        def runner(args):
            if '--bg' in args:
                return 124, 'Enable here: https://login.tailscale.com/f/serve?node=123\n', ''
            return base_runner(args)

        self.remote.runner = runner
        state = self.remote.action('enable')
        self.assertEqual(state['status'], 'needs_https')
        self.assertEqual(state['authURL'], 'https://login.tailscale.com/f/serve?node=123')
        self.assertFalse(self.remote.authorize(self.headers()))
        self.assertIsNone(consent_url('https://login.tailscale.com.evil.example/foo'))
        self.assertIsNone(
            identity({**STATUS, 'Self': {'DNSName': 'foreign.example', 'UserID': 42}})
        )

    def test_pending_action_is_visible_without_reporting_cached_state_as_completed(self):
        baseline = self.remote.refresh()
        entered, release = threading.Event(), threading.Event()
        original = self.remote.runner
        result, failures = [], []

        def runner(args):
            if '--bg' in args:
                entered.set()
                if not release.wait(3):
                    raise AssertionError('test did not release synthetic operation')
            return original(args)

        self.remote.runner = runner

        def change():
            try:
                result.append(self.remote.action('enable'))
            except Exception as error:
                failures.append(error)

        thread = threading.Thread(target=change)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            pending = self.remote.snapshot()
            self.assertTrue(pending['operationPending'])
            self.assertEqual(pending['operationRevision'], baseline['operationRevision'])
            self.assertEqual(pending['status'], 'off')
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        finished = result[0]
        self.assertFalse(finished['operationPending'])
        self.assertEqual(finished['operationRevision'], baseline['operationRevision'] + 1)
        self.assertGreater(finished['statusRevision'], baseline['statusRevision'])
        self.assertEqual(finished['status'], 'enabled')

    def test_queued_actions_and_failures_clear_pending_and_advance_revision(self):
        failures = []

        def invalid_action():
            try:
                self.remote.action('invalid')
            except ValueError:
                failures.append('expected')

        with self.remote.operations:
            threads = [threading.Thread(target=invalid_action) for _ in range(2)]
            for thread in threads:
                thread.start()
            deadline = time.monotonic() + 2
            while self.remote.pending_operations < 2 and time.monotonic() < deadline:
                time.sleep(0.001)
            self.assertEqual(self.remote.pending_operations, 2)
            self.assertTrue(self.remote.snapshot()['operationPending'])
            self.assertEqual(self.remote.snapshot()['operationRevision'], 0)
        for thread in threads:
            thread.join(2)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(failures, ['expected', 'expected'])
        self.assertFalse(self.remote.snapshot()['operationPending'])
        self.assertEqual(self.remote.snapshot()['operationRevision'], 2)

    def test_status_scans_advance_status_revision_without_counting_as_actions(self):
        initial = self.remote.snapshot()
        first = self.remote.refresh()
        self.failure = True
        second = self.remote.refresh()
        self.assertGreater(first['statusRevision'], initial['statusRevision'])
        self.assertGreater(second['statusRevision'], first['statusRevision'])
        self.assertEqual(second['operationRevision'], 0)
        self.assertFalse(second['operationPending'])


class RemoteHTTPBase(unittest.TestCase):
    """Servers and helpers only. Other modules subclass this, not RemoteHTTPTests,
    so they don't re-run every phone test."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name).resolve()
        (self.root / 'index.html').write_text('private dashboard')
        # Never ask this Mac's real Tailscale or sudo: no Tailscale, and purge needs approval.
        self.remote = Remote(self.root / 'remote.json', binary=lambda: None)
        permission = patch(
            'manual_selection.cache_permission',
            lambda runner: cache_permission(Mock(return_value=Mock(returncode=1, stdout=''))),
        )
        permission.start()
        self.addCleanup(permission.stop)
        self.remote.trusted = {'host': HOST, 'owner': OWNER}
        self.remote.checked = time.monotonic()

        class LocalHandler(Handler):
            pass

        LocalHandler.collector = Collector(self.root)
        LocalHandler.collector.collect(time.time())
        LocalHandler.static_root = self.root
        LocalHandler.remote = self.remote
        LocalHandler.dev = False

        class PhoneHandler(LocalHandler):
            remote_view = True

        self.local = ThreadingHTTPServer(('127.0.0.1', 0), LocalHandler)
        self.phone = ThreadingHTTPServer(('127.0.0.1', 0), PhoneHandler)
        for server in (self.local, self.phone):
            threading.Thread(
                target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True
            ).start()
        self.headers = {
            'Host': HOST + ':8443',
            'Tailscale-User-Login': OWNER,
            'X-Forwarded-Proto': 'https',
        }

    def tearDown(self):
        for server in (self.local, self.phone):
            server.shutdown()
            server.server_close()
        self.tmp.cleanup()

    def read(self, server, path, headers=None, data=None):
        if server is self.phone:
            # Tailscale Serve adds the phone secret to every path.
            path = f'/{self.remote.secret}' + path
        return urllib.request.urlopen(
            urllib.request.Request(
                f'http://127.0.0.1:{server.server_port}' + path, headers=headers or {}, data=data
            )
        )

    def denied(self, server, path, headers=None, data=None):
        with self.assertRaises(urllib.error.HTTPError) as result:
            self.read(server, path, headers, data)
        self.assertEqual(result.exception.code, 403)


class RemoteHTTPTests(RemoteHTTPBase):
    def test_pulse_history_on_local_and_authenticated_phone(self):
        collector = self.local.RequestHandlerClass.collector
        with collector.lock:
            collector.account = 'one'
            collector.snapshot['pulse'] = {'sessionId': 4}
            collector.pulse.record_rate(
                'one',
                {
                    'at': 100,
                    'sessionId': 4,
                    'status': 'live',
                    'windows': {'60': {'ratePerHour': 0.15}},
                },
            )
        for server, headers in ((self.local, {}), (self.phone, self.headers)):
            with self.read(
                server, '/api/pulse-history?session=4&from=0&to=200', headers
            ) as response:
                self.assertEqual(json.load(response)['samples'][0]['rate60'], 0.15)
            for query in ('session=5', 'from=NaN', 'from=200&to=100'):
                with self.assertRaises(urllib.error.HTTPError) as result:
                    self.read(server, '/api/pulse-history?' + query, headers)
                self.assertEqual(result.exception.code, 400)
        self.denied(
            self.phone, '/api/pulse-history', {**self.headers, 'Origin': 'https://foreign.example'}
        )

    def test_phone_listener_needs_the_secret_that_serve_adds(self):
        url = f'http://127.0.0.1:{self.phone.server_port}'
        for path in ('/api/snapshot', '/', f'/{"a" * 32}/api/snapshot', f'/{self.remote.secret}'):
            with self.assertRaises(urllib.error.HTTPError) as result:
                urllib.request.urlopen(urllib.request.Request(url + path, headers=self.headers))
            self.assertEqual(result.exception.code, 404)
            self.assertNotIn(self.remote.secret.encode(), result.exception.read())
        with self.read(self.phone, '/api/snapshot', self.headers) as response:
            self.assertIn('provider', json.load(response))
        with self.read(self.phone, '/api/remote', self.headers) as response:
            self.assertNotIn(self.remote.secret.encode(), response.read())
        # The Mac's own listener is unaffected, and never accepts the phone's paths.
        with self.read(self.local, '/api/snapshot') as response:
            self.assertIn('provider', json.load(response))
        with self.read(self.local, '/api/remote') as response:
            self.assertNotIn(self.remote.secret.encode(), response.read())

    def test_all_remote_routes_require_the_owner_identity(self):
        self.denied(self.phone, '/api/pulse-history', {'Host': HOST + ':8443'})
        for path in (
            '/',
            '/api/snapshot',
            '/api/sessions',
            '/api/remote',
            '/api/history',
            '/api/reputation',
            '/api/network/models',
            '/api/optimizer',
            '/api/model-control',
            '/api/optimizer/history?model=test',
            '/api/model-history',
            '/api/workload',
        ):
            self.denied(self.phone, path)
            self.denied(
                self.phone, path, {**self.headers, 'Tailscale-User-Login': 'other@example.com'}
            )
        with self.read(self.phone, '/api/snapshot', self.headers) as response:
            self.assertIn('provider', json.load(response))
        with self.read(self.phone, '/api/sessions', self.headers) as response:
            self.assertEqual(json.load(response)['recent'], [])

    def test_model_demand_is_readable_on_phone_and_validates_ranges(self):
        for server, headers in ((self.local, {}), (self.phone, self.headers)):
            with self.read(server, '/api/network/models?from=0', headers) as response:
                result = json.load(response)
                self.assertEqual(result['models'], [])
                self.assertIn('comparison', result)
            for query in ('from=nan', 'from=-1', 'from=20&to=10', 'to=inf'):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    self.read(server, '/api/network/models?' + query, headers)
                self.assertEqual(error.exception.code, 400)

    def test_passive_model_history_on_phone_needs_no_running_test_and_validates_input(self):
        c = self.local.RequestHandlerClass.collector
        c.optimizer.live.update(account='private-account', device='private-device')
        c.history.db.execute(
            'INSERT INTO opt_minutes VALUES(?,?,?,?,?,?,?,?)',
            ('private-account', 'private-device', 600, 'old-model', 60, 1, 10, 0),
        )
        c.history.db.commit()
        for server, headers in ((self.local, {}), (self.phone, self.headers)):
            with self.read(
                server, '/api/model-history?from=0&model=old-model', headers
            ) as response:
                result = json.load(response)
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
                self.assertEqual(result['switchingMode'], 'observe')
                self.assertFalse(result['historyResetsOnTestStart'])
                self.assertEqual(result['models'][0]['earlierHours'], 1 / 60)
                self.assertNotIn('private-device', json.dumps(result))
                self.assertNotIn('private-account', json.dumps(result))
            for query in ('from=nan', 'from=-1', 'from=20&to=10', 'to=inf', 'model=' + 'x' * 513):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    self.read(server, '/api/model-history?' + query, headers)
                self.assertEqual(error.exception.code, 400)

    def test_workload_is_owner_scoped_and_validates_ranges(self):
        for server, headers in ((self.local, {}), (self.phone, self.headers)):
            with self.read(server, '/api/workload?from=0', headers) as response:
                result = json.load(response)
                self.assertEqual(result['totals']['requests'], 0)
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
            for query in ('from=nan', 'from=-1', 'from=20&to=10', 'to=inf', 'model=' + 'x' * 513):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    self.read(server, '/api/workload?' + query, headers)
                self.assertEqual(error.exception.code, 400)

    def test_reputation_ingest_requires_native_secret_and_phone_is_read_only(self):
        self.local.RequestHandlerClass.collector.reputation.native_token = 'native-only'
        data = b'{"sequence":1,"status":"connecting"}'
        action = {'Content-Type': 'application/json', 'X-Bloom-Native': 'native-only'}
        self.denied(
            self.local, '/api/reputation/native', {'Content-Type': 'application/json'}, data
        )
        self.denied(
            self.local,
            '/api/reputation/native',
            {**action, 'Origin': 'https://foreign.example'},
            data,
        )
        self.denied(self.phone, '/api/reputation/native', {**self.headers, **action}, data)
        with self.read(self.local, '/api/reputation/native', action, data) as response:
            self.assertEqual(json.load(response)['status'], 'connecting')
        with self.read(self.phone, '/api/reputation', self.headers) as response:
            result = json.load(response)
            self.assertFalse(result['nativeAvailable'])
            self.assertNotIn('native-only', json.dumps(result))

    def test_remote_and_csrf_requests_cannot_change_settings(self):
        payload = b'{"action":"disable"}'
        action = {'Content-Type': 'application/json', 'X-Bloom-Action': 'remote-access'}
        self.denied(self.phone, '/api/remote', {**self.headers, **action}, payload)
        self.denied(self.local, '/api/remote', {'Content-Type': 'application/json'}, payload)
        self.denied(
            self.local, '/api/remote', {**action, 'Origin': 'https://foreign.example'}, payload
        )
        self.denied(self.local, '/api/remote', {**self.headers, **action}, payload)
        with self.read(self.local, '/api/remote', action, payload) as response:
            self.assertFalse(json.load(response)['enabled'])

    def test_optimizer_is_available_on_phone_and_rejects_csrf(self):
        optimizer = self.local.RequestHandlerClass.collector.optimizer
        payload = json.dumps(
            {'action': 'pause', 'expectedControl': optimizer.control_version()}
        ).encode()
        action = {'Content-Type': 'application/json', 'X-Bloom-Action': 'optimizer'}
        self.denied(self.phone, '/api/optimizer', {**self.headers, **action}, payload)
        self.denied(self.local, '/api/optimizer', {'Content-Type': 'application/json'}, payload)
        self.denied(
            self.local, '/api/optimizer', {**action, 'Origin': 'https://foreign.example'}, payload
        )
        self.denied(
            self.local, '/api/optimizer', {**action, 'Sec-Fetch-Site': 'cross-site'}, payload
        )
        with self.read(self.phone, '/api/optimizer', self.headers) as response:
            value = json.load(response)
            self.assertTrue(value['canManage'])
            self.assertTrue(value['remote'])
        phone = {**self.headers, **action, 'Origin': 'https://' + HOST + ':8443'}
        with self.read(self.phone, '/api/optimizer', phone, payload) as response:
            value = json.load(response)
            self.assertEqual(value['mode'], 'observe')
            self.assertTrue(value['remote'])
        payload = json.dumps(
            {'action': 'pause', 'expectedControl': optimizer.control_version()}
        ).encode()
        with self.read(self.local, '/api/optimizer', action, payload) as response:
            self.assertEqual(json.load(response)['mode'], 'observe')

    def test_optimizer_phone_commands_require_owner_https_origin_and_valid_bodies(self):
        optimizer = self.local.RequestHandlerClass.collector.optimizer
        from unittest.mock import Mock

        optimizer.control_action = Mock(return_value={'mode': 'week'})
        payload = b'{"action":"start","mode":"week","models":["a","b"],"blockHours":2,"expectedControl":"latest"}'
        headers = {
            **self.headers,
            'Content-Type': 'application/json',
            'X-Bloom-Action': 'optimizer',
            'Origin': 'https://' + HOST + ':8443',
        }
        for changes in (
            {'Origin': 'https://foreign.example'},
            {'Origin': 'null'},
            {'Tailscale-User-Login': 'other@example.com'},
            {'X-Forwarded-Proto': 'http'},
            {'Sec-Fetch-Site': 'cross-site'},
            {'X-Bloom-Action': 'manual-model'},
        ):
            self.denied(self.phone, '/api/optimizer', {**headers, **changes}, payload)
        for key in ('Origin', 'Tailscale-User-Login', 'X-Bloom-Action'):
            self.denied(
                self.phone,
                '/api/optimizer',
                {k: v for k, v in headers.items() if k != key},
                payload,
            )
        for body in (b'[]', b'null', b'not json', b' ' * 4097):
            with self.assertRaises(urllib.error.HTTPError) as e:
                self.read(self.phone, '/api/optimizer', headers, body)
            self.assertEqual(e.exception.code, 400)
        optimizer.control_action.assert_not_called()
        with self.read(self.phone, '/api/optimizer', headers, payload) as response:
            self.assertEqual(json.load(response)['mode'], 'week')
        optimizer.control_action.assert_called_once_with(json.loads(payload), 'phone')

    def test_optimizer_rejects_stale_views_and_unrelated_actions(self):
        headers = {
            **self.headers,
            'Content-Type': 'application/json',
            'X-Bloom-Action': 'optimizer',
            'Origin': 'https://' + HOST + ':8443',
        }
        for body in (
            {'action': 'pause'},
            {'action': 'pause', 'expectedControl': 'stale'},
            {'action': 'disable'},
            {'action': 'switch', 'model': 'a'},
        ):
            with self.assertRaises(urllib.error.HTTPError) as e:
                self.read(self.phone, '/api/optimizer', headers, json.dumps(body).encode())
            self.assertEqual(e.exception.code, 400)

    def test_manual_control_is_available_to_local_mac_and_authenticated_owner_phone(self):
        from unittest.mock import Mock

        optimizer = self.local.RequestHandlerClass.collector.optimizer
        optimizer.manual_action = Mock(
            return_value={'queuedModel': 'model-b', 'requestId': 'request', 'canCancel': True}
        )
        payload = b'{"action":"switch","model":"model-b"}'
        action = {'Content-Type': 'application/json', 'X-Bloom-Action': 'manual-model'}
        with self.read(self.local, '/api/model-control', action, payload) as response:
            self.assertTrue(json.load(response)['canCancel'])
        optimizer.manual_action.assert_called_with({'action': 'switch', 'model': 'model-b'}, 'mac')
        phone = {**self.headers, **action, 'Origin': 'https://' + HOST + ':8443'}
        with self.read(self.phone, '/api/model-control', phone, payload) as response:
            self.assertEqual(json.load(response)['queuedModel'], 'model-b')
        optimizer.manual_action.assert_called_with(
            {'action': 'switch', 'model': 'model-b'}, 'phone'
        )
        with self.read(self.phone, '/api/model-control', self.headers) as response:
            self.assertIn('models', json.load(response))

    def test_phone_manual_control_rejects_wrong_identity_cross_site_and_missing_origin(self):
        from unittest.mock import Mock

        optimizer = self.local.RequestHandlerClass.collector.optimizer
        optimizer.manual_action = Mock()
        payload = b'{"action":"switch","model":"b"}'
        headers = {
            **self.headers,
            'Content-Type': 'application/json',
            'X-Bloom-Action': 'manual-model',
            'Origin': 'https://' + HOST + ':8443',
        }
        for changes in (
            {'Origin': 'https://foreign.example'},
            {'Origin': 'null'},
            {'Tailscale-User-Login': 'other@example.com'},
            {'X-Forwarded-Proto': 'http'},
            {'Sec-Fetch-Site': 'cross-site'},
            {'X-Bloom-Action': 'optimizer'},
        ):
            self.denied(self.phone, '/api/model-control', {**headers, **changes}, payload)
        self.denied(
            self.phone,
            '/api/model-control',
            {k: v for k, v in headers.items() if k != 'Origin'},
            payload,
        )
        self.denied(
            self.phone,
            '/api/model-control',
            {k: v for k, v in headers.items() if k != 'Tailscale-User-Login'},
            payload,
        )
        self.denied(self.local, '/api/model-control', {'Content-Type': 'application/json'}, payload)
        self.denied(
            self.local,
            '/api/model-control',
            {
                'Content-Type': 'application/json',
                'X-Bloom-Action': 'manual-model',
                'Origin': 'https://foreign.example',
            },
            payload,
        )
        optimizer.manual_action.assert_not_called()

    def test_provider_actions_use_same_owner_origin_guards_and_source(self):
        from unittest.mock import Mock

        optimizer = self.local.RequestHandlerClass.collector.optimizer
        optimizer.manual_action = Mock(return_value={'accepted': True})
        headers = {
            **self.headers,
            'Content-Type': 'application/json',
            'X-Bloom-Action': 'manual-model',
            'Origin': 'https://' + HOST + ':8443',
        }
        for action in ('provider-start', 'provider-stop', 'provider-endpoint'):
            payload = json.dumps(
                {'action': action, 'requestId': 'fixture', 'expectedProvider': 'fixture'}
            ).encode()
            for change in (
                {'Origin': 'null'},
                {'Tailscale-User-Login': 'foreign@example.com'},
                {'X-Bloom-Action': 'optimizer'},
            ):
                self.denied(self.phone, '/api/model-control', {**headers, **change}, payload)
            with self.read(self.phone, '/api/model-control', headers, payload) as response:
                self.assertTrue(json.load(response)['accepted'])
            optimizer.manual_action.assert_called_with(json.loads(payload), 'phone')

    def test_manual_endpoint_rejects_unrelated_actions_and_bad_bodies(self):
        headers = {
            **self.headers,
            'Content-Type': 'application/json',
            'X-Bloom-Action': 'manual-model',
            'Origin': 'https://' + HOST + ':8443',
        }
        for payload in (
            b'[]',
            b'{"action":"start","mode":"week"}',
            b'{"action":"disable"}',
            b'{"action":"switch","model":"a"}',
            b'not json',
            b' ' * 2049,
        ):
            with self.assertRaises(urllib.error.HTTPError) as e:
                self.read(self.phone, '/api/model-control', headers, payload)
            self.assertEqual(e.exception.code, 400)
        self.denied(self.phone, '/api/optimizer', headers, b'{"action":"pause"}')

    def test_remote_origin_checks_allow_opening_links_but_block_cross_site_api_reads(self):
        self.denied(
            self.phone, '/api/snapshot', {**self.headers, 'Origin': 'https://foreign.example'}
        )
        self.denied(self.phone, '/api/snapshot', {**self.headers, 'Sec-Fetch-Site': 'cross-site'})
        with self.read(
            self.phone,
            '/',
            {**self.headers, 'Sec-Fetch-Site': 'cross-site', 'Sec-Fetch-Mode': 'navigate'},
        ) as response:
            self.assertEqual(response.read(), b'private dashboard')
        with self.read(self.phone, '/api/remote', self.headers) as response:
            self.assertFalse(json.load(response)['canManage'])


if __name__ == '__main__':
    unittest.main()
