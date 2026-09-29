import base64
import copy
import json
import io
import time
import urllib.error
import pathlib
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from web_push import (
    WebPush,
    PushTransport,
    PushResponse,
    DeliveryError,
    bounded_sender,
    digest,
    endpoint,
    libraries,
    send_webpush,
    validate_subscription,
    validate_contact,
)


CONTACT = 'mailto:operator@bloom-demo.com'


def b64(value):
    return base64.urlsafe_b64encode(value).decode().rstrip('=')


def subscription(target='https://web.push.apple.com/Qexample'):
    _, _, ec, serialization, _ = libraries()
    private = ec.generate_private_key(ec.SECP256R1())
    public = private.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return {'endpoint': target, 'keys': {'p256dh': b64(public), 'auth': b64(b'a' * 16)}}, private


class PushTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = pathlib.Path(self.tmp.name)
        self.sent, self.time = [], 1000
        self.subscription, self.recipient = subscription()

        def sender(sub, payload, private):
            self.sent.append((copy.deepcopy(sub), copy.deepcopy(payload)))
            return SimpleNamespace(status_code=201)

        self.push = WebPush(self.folder, sender=sender, clock=lambda: self.time)

    def tearDown(self):
        self.push.stop.set()
        if self.push.thread:
            self.push.thread.join(2)
        self.tmp.cleanup()

    def register(self):
        self.push.configure_contact(CONTACT)
        return self.push.subscribe('account', self.subscription)

    def flush(self):
        while not self.push.queue.empty():
            scope, payload, target = self.push.queue.get_nowait()
            self.push.deliver(scope, payload, target)
            self.push.queue.task_done()

    def test_constructor_and_unsubscribed_scans_do_not_create_state(self):
        self.assertFalse(self.push.path.exists())
        self.assertFalse(self.push.enqueue_switch('account', 'event', 'Previous model', 'Gemma'))
        self.assertFalse(self.push.path.exists())
        self.assertFalse(WebPush(None).status('account')['supported'])

    def test_spike_alerts_disabled_but_switch_reason_preserves_subscription(self):
        status = self.register()
        keys = self.push.state['privateKey'], self.push.state['publicKey']
        self.assertTrue(
            self.push.enqueue_switch('account', 'switch', 'Gemma', 'Qwen', 'paid_trial')
        )
        self.flush()
        payload = self.sent[0][1]
        self.assertEqual(payload['title'], 'BloomGauge · model switched')
        self.assertIn('Gemma → Qwen', payload['body'])
        self.assertIn('not yet proven', payload['body'])
        self.assertEqual(keys, (self.push.state['privateKey'], self.push.state['publicKey']))
        self.assertEqual(
            self.push.status('account')['categories'],
            {'demandSpikes': False, 'modelSwitches': True},
        )
        self.assertEqual(
            status['subscriptionId'], self.push.status('account')['subscriptions'][0]['id']
        )

    def test_queued_switch_survives_restart_without_repeating_success(self):
        self.register()
        self.push.enqueue_switch('account', 'switch', 'Gemma', 'Qwen', 'paid_trial')
        reopened = WebPush(self.folder, clock=lambda: self.time, sender=self.push.sender)
        reopened.resume_pending()
        scope, payload, target = reopened.queue.get_nowait()
        reopened.deliver(scope, payload, target)
        self.assertEqual(len(self.sent), 1)
        again = WebPush(self.folder, clock=lambda: self.time, sender=self.push.sender)
        again.resume_pending()
        self.assertTrue(again.queue.empty())
        self.assertFalse(again.enqueue_switch('account', 'switch', 'Gemma', 'Qwen'))

    def test_retry_only_unsent_device_and_expires_old_switches(self):
        self.register()
        other, _ = subscription('https://fcm.googleapis.com/wp/other')
        self.push.subscribe('account', other)
        attempts = []

        def sender(sub, *args):
            attempts.append(sub['endpoint'])
            return SimpleNamespace(
                status_code=201 if sub['endpoint'] == self.subscription['endpoint'] else 503
            )

        self.push.sender = sender
        self.push.enqueue_switch('account', 'switch', 'Gemma', 'Qwen')
        self.flush()
        self.assertEqual(len(attempts), 2)
        reopened = WebPush(
            self.folder,
            clock=lambda: self.time,
            sender=lambda sub, *args: (
                attempts.append(sub['endpoint']) or SimpleNamespace(status_code=201)
            ),
        )
        reopened.resume_pending()
        self.assertTrue(reopened.queue.empty())
        self.time += 301
        reopened.resume_pending()
        scope, payload, target = reopened.queue.get_nowait()
        reopened.deliver(scope, payload, target)
        self.assertEqual(
            attempts, [self.subscription['endpoint'], other['endpoint'], other['endpoint']]
        )
        self.push = WebPush(self.folder, clock=lambda: self.time, sender=self.push.sender)
        self.push.enqueue_switch('account', 'old', 'Qwen', 'Gemma')
        self.time += 901
        expired = WebPush(self.folder, clock=lambda: self.time, sender=self.push.sender)
        expired.resume_pending()
        self.assertTrue(expired.queue.empty())
        self.assertEqual(expired.state['pendingSwitches'], {})
        self.flush()  # An older in-memory queue item must also expire.
        self.assertEqual(len(attempts), 3)

    def test_legacy_keys_and_phone_registration_survive_contact_setup(self):
        before = self.push.subscribe('account', self.subscription)
        keys = self.push.state['privateKey'], self.push.state['publicKey']
        self.assertFalse(before['contactConfigured'])
        self.assertIn('sender contact', before['detail'])
        self.assertFalse(self.push.enqueue_switch('account', 'one', 'Previous model', 'Gemma'))
        with self.assertRaisesRegex(ValueError, 'sender contact'):
            self.push.test_notification('account', before['subscriptionId'])
        reopened = WebPush(self.folder, clock=lambda: self.time, sender=self.push.sender)
        reopened.configure_contact(CONTACT)
        self.assertEqual((reopened.state['privateKey'], reopened.state['publicKey']), keys)
        status = reopened.status('account')
        self.assertTrue(status['contactConfigured'])
        self.assertEqual(status['subscriptions'], before['subscriptions'])
        self.assertNotIn(CONTACT, json.dumps(status))
        self.assertTrue(reopened.enqueue_switch('account', 'one', 'Previous model', 'Gemma'))
        self.assertEqual(reopened.path.stat().st_mode & 0o777, 0o600)

    def test_sender_contact_rejects_local_placeholders_and_malformed_uris(self):
        self.assertEqual(validate_contact('operator@bloom-demo.com'), CONTACT)
        self.assertEqual(validate_contact(CONTACT), CONTACT)
        self.assertEqual(
            validate_contact('https://bloom-demo.com/contact'), 'https://bloom-demo.com/contact'
        )
        for contact in (
            None,
            [],
            '',
            'mailto:bloom@localhost.invalid',
            'mailto:bloom@localhost',
            'mailto:bloom@device.local',
            'mailto:bloom@sample.test',
            'operator@127.0.0.1',
            'mailto:operator@bloom-demo.com?subject=hi',
            'http://bloom-demo.com/contact',
            'https://localhost/contact',
            'https://1.1.1.1/contact',
            'https://[::1]/',
            'https://bloom-demo.com:8765/contact',
            'https://user:pass@bloom-demo.com/contact',
            'https://bloom-demo.com/#private',
            'operator@bloom-demo.com\n',
            'a@bad-.com',
        ):
            with self.subTest(contact=contact), self.assertRaises(ValueError):
                validate_contact(contact)

    def test_explicit_test_targets_one_owned_device_and_persists_cooldown(self):
        status = self.register()
        other, _ = subscription('https://fcm.googleapis.com/wp/same-owner-second-device')
        self.push.subscribe('account', other)
        foreign, _ = subscription('https://fcm.googleapis.com/wp/foreign-owner')
        foreign_id = self.push.subscribe('other', foreign)['subscriptionId']
        key = status['subscriptionId']
        for owner, target in (
            ('other', key),
            ('account', foreign_id),
            ('account', 'missing'),
            ('account', {}),
        ):
            with self.assertRaises(ValueError):
                self.push.test_notification(owner, target)
        queued = self.push.test_notification('account', key)
        self.assertTrue(queued['testQueued'])
        with self.assertRaisesRegex(ValueError, 'retry delay'):
            WebPush(self.folder, clock=lambda: self.time).test_notification('account', key)
        self.flush()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], self.subscription)
        self.assertEqual(self.sent[0][1]['title'], 'BloomGauge · notification test')
        self.assertNotIn(CONTACT, json.dumps(self.sent[0][1]))
        record = next(s for s in self.push.status('account')['subscriptions'] if s['id'] == key)
        self.assertEqual(record['lastTestResultAt'], self.time)
        self.assertIsNone(record['lastTestError'])
        self.time += 61
        self.push.test_notification('account', key)
        self.push.unsubscribe('account', self.subscription['endpoint'])
        self.flush()
        self.assertEqual(len(self.sent), 1)

    def test_test_failure_reports_authentication_reason_and_honors_backoff(self):
        key = self.register()['subscriptionId']

        def reject(*_):
            raise DeliveryError(403, '600', 'BadJwtToken')

        self.push.sender = reject
        self.push.test_notification('account', key)
        self.flush()
        record = self.push.status('account')['subscriptions'][0]
        self.assertEqual(record['lastStatusCode'], 403)
        self.assertEqual(record['lastReason'], 'BadJwtToken')
        self.assertIn('authentication', record['lastError'])
        self.assertIn('BadJwtToken', record['lastTestError'])
        self.assertIsNone(record['lastSentAt'])
        self.time += 61
        # Updating contact cannot erase a push service retry delay or keys.
        self.push.configure_contact('mailto:another@bloom-demo.com')
        with self.assertRaisesRegex(ValueError, 'retry delay'):
            self.push.test_notification('account', key)
        self.time += 600
        self.push.test_notification('account', key)

    def test_expired_phone_and_stopped_worker_cannot_accept_tests(self):
        key = self.register()['subscriptionId']
        self.push.state['subscriptions'][key]['status'] = 'expired'
        with self.assertRaisesRegex(ValueError, 'Enable notifications'):
            self.push.test_notification('account', key)
        self.push.state['subscriptions'][key]['status'] = 'subscribed'
        self.push.stop.set()
        with self.assertRaisesRegex(ValueError, 'busy'):
            self.push.test_notification('account', key)

    def test_keys_persist_privately_and_status_exposes_no_subscription_secrets(self):
        status = self.register()
        self.assertTrue(status['supported'])
        self.assertEqual(status['subscriptionCount'], 1)
        self.assertEqual(self.push.path.stat().st_mode & 0o777, 0o600)
        reopened = WebPush(self.folder).status('account')
        self.assertEqual(reopened['publicKey'], status['publicKey'])
        rendered = json.dumps(status)
        for private in (
            'PRIVATE KEY',
            self.subscription['endpoint'],
            self.subscription['keys']['auth'],
            '"account"',
        ):
            self.assertNotIn(private, rendered)
        self.assertEqual(self.push.status('other')['subscriptionCount'], 0)

    def test_invalid_or_overbroad_subscriptions_fail_before_key_creation(self):
        for change in (
            {'keys': {}},
            {'extra': 'value'},
            {'endpoint': 'http://web.push.apple.com/x'},
        ):
            with self.assertRaises(ValueError):
                self.push.subscribe('account', {**self.subscription, **change})
        self.assertFalse(self.push.path.exists())
        with self.assertRaises(ValueError):
            self.push.subscribe('', self.subscription)

    def test_only_supported_public_https_push_services_are_valid_endpoints(self):
        for target in (
            'https://web.push.apple.com/token',
            'https://eu.push.apple.com/token',
            'https://updates.push.services.mozilla.com/wpush/v2/token',
            'https://fcm.googleapis.com/wp/token',
        ):
            self.assertEqual(endpoint(target), target)
        for target in (
            'http://web.push.apple.com/token',
            'https://127.0.0.1/token',
            'https://[::1]/token',
            'https://localhost/token',
            'https://web.push.apple.com.evil.test/token',
            'https://evilpush.apple.com/token',
            'https://x.y.push.apple.com/token',
            'https://web.push.apple.com:8443/token',
            'https://user@web.push.apple.com/token',
            'https://web.push.apple.com/token#fragment',
            'https://web.push.apple.com/',
            'https://web.push.apple.com/\ntoken',
            'https://web.push.apple.com\\evil/token',
        ):
            with self.assertRaises(ValueError, msg=target):
                endpoint(target)

    def test_keys_must_be_real_p256_points_and_16_byte_auth_values(self):
        for keys in (
            {'auth': 'bad', 'p256dh': self.subscription['keys']['p256dh']},
            {'auth': b64(b'a' * 16), 'p256dh': b64(b'x' * 65)},
            {'auth': b64(b'a' * 15), 'p256dh': self.subscription['keys']['p256dh']},
        ):
            with self.assertRaises(ValueError):
                validate_subscription({**self.subscription, 'keys': keys})

    def test_delivery_is_account_scoped_generic_and_deduplicated_across_restart(self):
        self.register()
        other, _ = subscription('https://fcm.googleapis.com/wp/other')
        self.push.subscribe('other', other)
        self.assertTrue(self.push.enqueue_switch('account', 'one', 'Previous model', 'Gemma 4 26b'))
        self.assertFalse(
            self.push.enqueue_switch('account', 'one', 'Previous model', 'Gemma 4 26b')
        )
        self.flush()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0]['endpoint'], self.subscription['endpoint'])
        self.assertIn('Gemma 4 26b', self.sent[0][1]['body'])
        self.assertNotIn('url', self.sent[0][1])
        self.assertNotIn('account', json.dumps(self.sent[0][1]))
        self.assertFalse(
            WebPush(self.folder).enqueue_switch('account', 'one', 'Previous model', 'Gemma')
        )
        self.assertEqual(self.push.status('account')['lastSentAt'], self.time)

    def test_unsubscribe_and_account_reassignment_cancel_queued_targets(self):
        self.register()
        self.push.enqueue_switch('account', 'one', 'Previous model', 'Gemma')
        self.push.unsubscribe('other', self.subscription['endpoint'])
        self.assertEqual(self.push.status('account')['subscriptionCount'], 1)
        self.push.subscribe('other', self.subscription)
        self.flush()
        self.assertEqual(self.sent, [])
        self.push.unsubscribe('other', self.subscription['endpoint'])
        self.assertEqual(self.push.status('other')['subscriptionCount'], 0)

    def test_expired_subscription_is_disabled_and_has_an_actionable_status(self):
        self.register()
        self.push.sender = lambda *_: SimpleNamespace(status_code=410)
        self.push.enqueue_switch('account', 'one', 'Previous model', 'Gemma')
        self.flush()
        status = self.push.status('account')
        self.assertEqual(status['subscriptionCount'], 0)
        self.assertEqual(status['subscriptions'][0]['status'], 'expired')
        self.assertIn('Enable notifications again', status['lastError'])
        self.assertFalse(self.push.enqueue_switch('account', 'two', 'Previous model', 'Gemma'))

    def test_transient_failures_are_sanitized_and_respect_retry_after(self):
        self.register()

        class Failure(Exception):
            response = SimpleNamespace(status_code=429, headers={'Retry-After': '600'})

        def fail(*_):
            self.sent.append('attempt')
            raise Failure(self.subscription['endpoint'])

        self.push.sender = fail
        self.push.enqueue_switch('account', 'one', 'Previous model', 'Gemma')
        self.flush()
        self.assertEqual(len(self.sent), 1)
        self.push.enqueue_switch('account', 'two', 'Previous model', 'Gemma')
        self.flush()
        self.assertEqual(len(self.sent), 1)
        self.assertNotIn(self.subscription['endpoint'], json.dumps(self.push.status('account')))
        self.time += 601
        self.push.enqueue_switch('account', 'three', 'Previous model', 'Gemma')
        self.flush()
        self.assertEqual(len(self.sent), 2)

    def test_registration_renewal_preserves_delivery_expiry_and_backoff(self):
        self.register()
        record = self.push.state['subscriptions'][digest(self.subscription['endpoint'])]
        record.update(
            status='delayed',
            lastSentAt=100,
            lastAttemptAt=200,
            lastError='Delayed',
            retryAfter=3000,
        )
        renewed = self.push.subscribe('account', copy.deepcopy(self.subscription))
        self.assertEqual(renewed['lastSentAt'], 100)
        self.assertEqual(renewed['lastError'], 'Delayed')
        self.assertEqual(record['retryAfter'], 3000)
        record['status'] = 'expired'
        self.assertEqual(self.register()['subscriptionCount'], 0)

    def test_queue_is_bounded_and_background_worker_delivers(self):
        self.register()
        for i in range(32):
            self.assertTrue(self.push.enqueue_switch('account', str(i), 'Previous model', 'Gemma'))
        self.assertFalse(self.push.enqueue_switch('account', 'overflow', 'Previous model', 'Gemma'))
        self.push.start()
        self.push.queue.join()
        self.assertEqual(len(self.sent), 32)

    def test_corrupted_or_symlinked_key_file_is_not_silently_replaced(self):
        self.push.path.write_text('broken')
        self.assertFalse(self.push.status('account')['supported'])
        self.assertEqual(self.push.path.read_text(), 'broken')
        self.push.path.unlink()
        target = self.folder / 'other.json'
        target.write_text('private')
        self.push.path.symlink_to(target)
        self.assertFalse(self.push.status('account')['supported'])
        self.assertEqual(target.read_text(), 'private')

    def test_standard_webpush_encryption_and_vapid_roundtrip_without_network(self):
        self.register()
        captured = {}

        def transport(_self, url, **kwargs):
            captured.update(kwargs)
            return PushResponse(201, {})

        payload = {'title': 'BloomGauge', 'body': 'Gemma demand increased'}
        with patch.object(PushTransport, 'post', transport):
            result = send_webpush(
                self.subscription, payload, self.push.state['privateKey'], CONTACT
            )
        self.assertEqual(result.status_code, 201)
        self.assertEqual(captured['timeout'], 8)
        self.assertEqual(captured['headers']['content-encoding'], 'aes128gcm')
        self.assertTrue(any(key.lower() == 'authorization' for key in captured['headers']))
        authorization = next(
            value for key, value in captured['headers'].items() if key.lower() == 'authorization'
        )
        token = authorization.split('t=', 1)[1].split(',', 1)[0]
        claims = json.loads(base64.urlsafe_b64decode(token.split('.')[1] + '=='))
        self.assertEqual(claims['sub'], CONTACT)
        self.assertEqual(claims['aud'], 'https://web.push.apple.com')
        self.assertGreater(claims['exp'], time.time())
        self.assertLessEqual(claims['exp'], time.time() + 86400)
        self.assertNotIn(b'Gemma', captured['data'])
        import http_ece

        decoded = http_ece.decrypt(
            captured['data'], private_key=self.recipient, auth_secret=b'a' * 16, version='aes128gcm'
        )
        self.assertEqual(json.loads(decoded), payload)

    def test_transport_rejects_private_resolution_before_any_http_call(self):
        answer = [(socket_family, 1, 6, '', ('127.0.0.1', 443)) for socket_family in (2,)]
        with (
            patch('web_push.socket.getaddrinfo', return_value=answer),
            patch('web_push.urllib.request.build_opener') as opener,
        ):
            with self.assertRaises(ValueError):
                PushTransport().post(self.subscription['endpoint'], data=b'ciphertext')
            opener.assert_not_called()

    def test_subprocess_deadline_contains_secrets_in_stdin_only(self):
        self.register()
        with patch(
            'web_push.subprocess.run',
            return_value=SimpleNamespace(returncode=0, stdout='{"statusCode":201}'),
        ) as run:
            self.assertEqual(
                bounded_sender(
                    self.subscription,
                    {'title': 'BloomGauge'},
                    self.push.state['privateKey'],
                    CONTACT,
                ).status_code,
                201,
            )
        args, kwargs = run.call_args
        self.assertEqual(kwargs['timeout'], 15)
        self.assertNotIn(self.subscription['endpoint'], str(args))
        self.assertNotIn('PRIVATE KEY', str(args))
        self.assertNotIn(CONTACT, str(args))
        self.assertIn(self.subscription['endpoint'], kwargs['input'])
        with patch('web_push.subprocess.run', side_effect=subprocess.TimeoutExpired('worker', 15)):
            with self.assertRaises(DeliveryError):
                bounded_sender(
                    self.subscription,
                    {'title': 'BloomGauge'},
                    self.push.state['privateKey'],
                    CONTACT,
                )

    def test_only_bounded_allowlisted_error_reasons_survive_transport_and_worker(self):
        public = [(2, 1, 6, '', ('17.0.0.1', 443))]
        for reason, expected in (
            ('BadJwtToken', 'BadJwtToken'),
            (self.subscription['endpoint'], None),
            ([], None),
        ):
            error = urllib.error.HTTPError(
                self.subscription['endpoint'],
                403,
                'private details',
                {},
                io.BytesIO(json.dumps({'reason': reason}).encode()),
            )
            with (
                patch('web_push.socket.getaddrinfo', return_value=public),
                patch('web_push.urllib.request.build_opener') as opener,
            ):
                opener.return_value.open.side_effect = error
                result = PushTransport().post(self.subscription['endpoint'], data=b'encrypted')
                self.assertEqual(result.status_code, 403)
                self.assertEqual(result.service_reason, expected)
                self.assertEqual(result.text, '')
            with patch(
                'web_push.subprocess.run',
                return_value=SimpleNamespace(
                    returncode=0, stdout=json.dumps({'statusCode': 403, 'reason': reason})
                ),
            ):
                with self.assertRaises(DeliveryError) as failure:
                    bounded_sender(
                        self.subscription, {'title': 'BloomGauge'}, 'private-fixture', CONTACT
                    )
                    self.assertEqual(failure.exception.response.service_reason, expected)

    def test_isolated_delivery_worker_does_not_write_bytecode(self):
        worker = self.folder / 'worker.py'
        (self.folder / 'worker_fixture.py').write_text('value = 201\n')
        worker.write_text(
            'import json, pathlib, sys\n'
            'sys.path.insert(0, str(pathlib.Path(__file__).parent))\n'
            'import worker_fixture\n'
            'print(json.dumps({"statusCode": worker_fixture.value}))\n'
        )
        # -I ignores PYTHONDONTWRITEBYTECODE, so the child needs its own -B.
        with (
            patch('web_push.__file__', str(worker)),
            patch.dict('os.environ', {'PYTHONDONTWRITEBYTECODE': '1'}),
        ):
            self.assertEqual(bounded_sender({}, {}, 'fixture', CONTACT).status_code, 201)
        self.assertEqual(list(self.folder.rglob('*.pyc')), [])

    def test_transport_has_no_redirect_or_environment_proxy_support(self):
        from web_push import NoRedirect

        self.assertIsNone(
            NoRedirect().redirect_request(None, None, 302, None, None, 'https://localhost/')
        )
        response = SimpleNamespace(status=201, headers={})
        context = SimpleNamespace(__enter__=lambda _: response, __exit__=lambda *_: None)
        from unittest.mock import MagicMock

        context = MagicMock()
        context.__enter__.return_value = response
        public = [(2, 1, 6, '', ('17.0.0.1', 443))]
        with (
            patch('web_push.socket.getaddrinfo', return_value=public),
            patch('web_push.urllib.request.build_opener') as opener,
            patch('web_push.urllib.request.ProxyHandler') as proxy,
        ):
            opener.return_value.open.return_value = context
            self.assertEqual(
                PushTransport()
                .post(self.subscription['endpoint'], data=b'encrypted', timeout=999)
                .status_code,
                201,
            )
            proxy.assert_called_once_with({})
            self.assertEqual(opener.return_value.open.call_args.kwargs['timeout'], 8)


if __name__ == '__main__':
    unittest.main()
