import json, pathlib, tempfile, threading, time, unittest, urllib.error, urllib.request
from unittest.mock import Mock
from collector import (
    APPLE_EPOCH,
    Collector,
    Handler,
    ThreadingHTTPServer,
    counter_rate,
    earnings_snapshot,
    monitor_snapshot,
    next_sample_delay,
    run_view,
)


class CollectorTests(unittest.TestCase):
    def test_counter_rate_and_restart(self):
        a = {'at': 100, 'session': 1, 'tokens': 1000}
        b = {'at': 103, 'session': 1, 'tokens': 1060}
        self.assertEqual(counter_rate(a, b, 104), 20)
        self.assertIsNone(counter_rate(a, {**b, 'session': 2}, 104))
        self.assertIsNone(counter_rate(a, {**b, 'tokens': 12}, 104))
        self.assertIsNone(counter_rate(a, b, 130))
        self.assertIsNone(counter_rate(a, a, 101))
        self.assertIsNone(counter_rate(None, b, 104))

    def test_microdollars_and_private_fields(self):
        result = earnings_snapshot(
            {
                'account_id': 'private-id',
                'available_balance_micro_usd': 1234567,
                'total_micro_usd': 2345678,
                'count': 2,
                'earnings': [
                    {
                        'id': 3,
                        'model': 'gemma',
                        'amount_micro_usd': 111,
                        'created_at': '2026-09-06T00:00:00Z',
                        'completion_tokens': 33,
                        'provider_key': 'private-key',
                    }
                ],
            },
            123,
        )
        self.assertEqual(result['balance'], 1.234567)
        self.assertEqual(result['entries'][0]['usd'], 0.000111)
        self.assertNotIn('private', json.dumps(result))

    def test_invalid_earnings_is_not_zero(self):
        with self.assertRaises(ValueError):
            earnings_snapshot({'earnings': []}, 123)

    def test_swift_dates(self):
        with tempfile.TemporaryDirectory() as root:
            path = pathlib.Path(root) / 'history.json'
            path.write_text(
                json.dumps(
                    {
                        'coverageStartedAt': 800000000,
                        'hours': [
                            {
                                'hour': 800002800,
                                'jobs': 3,
                                'microUSD': 654321,
                                'earningsByCategory': {'base_reward': {'microUSD': 100000}},
                            }
                        ],
                    }
                )
            )
            result = monitor_snapshot(path, time.time())
            self.assertEqual(result['coverageStartedAt'], 800000000 + APPLE_EPOCH)
            self.assertEqual(result['hours'][0]['at'], 800002800 + APPLE_EPOCH)
            self.assertEqual(result['hours'][0]['usd'], 0.654321)

    def test_model_category_counts_and_missing_history(self):
        with tempfile.TemporaryDirectory() as root:
            path = pathlib.Path(root) / 'history.json'
            categories = {
                'a': {'microUSD': 600000, 'entries': 3},
                'b': {'microUSD': 400000, 'entries': 8},
                'base_reward': {'microUSD': 100000, 'entries': 2},
                'old': {'microUSD': 0},
                'invalid': {'microUSD': 0, 'entries': -1},
                'boolean': {'microUSD': 0, 'entries': True},
            }
            path.write_text(
                json.dumps(
                    {
                        'hours': [
                            {
                                'hour': 800002800,
                                'jobs': 11,
                                'microUSD': 1100000,
                                'earningsByCategory': categories,
                            }
                        ]
                    }
                )
            )
            h = monitor_snapshot(path, time.time())['hours'][0]
            self.assertEqual(h['categoryJobs'], {'a': 3, 'b': 8, 'base_reward': 2})
            self.assertEqual(h['jobs'], 11)
            self.assertEqual(h['categories']['a'], 0.6)

    def test_missing_inputs_and_retained_earnings(self):
        with tempfile.TemporaryDirectory() as root:
            c = Collector(root)
            c.collect(500)
            self.assertFalse(c.snapshot['provider']['online'])
            self.assertIsNone(c.snapshot['provider']['tokensPerSecond'])
            self.assertEqual(c.snapshot['monitor']['status'], 'missing')
            c.earnings = {'status': 'ok', 'updatedAt': 490, 'balance': 2, 'error': None}
            c.earnings_error('Offline')
            self.assertEqual(c.earnings['balance'], 2)
            self.assertEqual(c.earnings['status'], 'stale')

    def test_stale_provider_is_not_live(self):
        with tempfile.TemporaryDirectory() as root:
            folder = pathlib.Path(root) / '.darkbloom'
            folder.mkdir()
            state = {
                'written_at': 400,
                'started_at': 300,
                'pid': 10,
                'inference_active': True,
                'stats': {'tokens_generated': 100, 'requests_served': 4},
            }
            (folder / 'daemon-state.json').write_text(json.dumps(state))
            c = Collector(root)
            c.collect(500)
            self.assertFalse(c.snapshot['provider']['active'])
            self.assertFalse(c.snapshot['provider']['online'])
            self.assertIsNone(c.snapshot['provider']['tokensPerSecond'])

    def test_corrupt_monitor_preserves_history(self):
        with tempfile.TemporaryDirectory() as root:
            folder = pathlib.Path(root) / 'Library/Application Support/Darkbloom Monitor'
            folder.mkdir(parents=True)
            path = folder / 'activity-history.json'
            path.write_text(
                json.dumps(
                    {
                        'coverageStartedAt': 800000000,
                        'hours': [{'hour': 800000000, 'microUSD': 1000000, 'jobs': 7}],
                    }
                )
            )
            c = Collector(root)
            c.collect(time.time())
            path.write_text('{')
            c.collect(time.time())
            self.assertEqual(c.snapshot['monitor']['status'], 'stale')
            self.assertEqual(c.snapshot['monitor']['hours'][0]['usd'], 1)


class SessionSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        (self.root / '.darkbloom').mkdir()
        self.path = self.root / '.darkbloom/daemon-state.json'
        self.c = Collector(self.root)
        self.c.account = 'account'
        self.c.sessions.probe = Mock(return_value=True)
        self.raw = {
            'pid': 10,
            'started_at': 900,
            'written_at': 1000,
            'attestation_public_key': 'device-key',
            'advertised_models': ['a'],
            'current_model': 'a',
            'inference_active': False,
            'warm_models': ['a', 'b'],
            'trust': {'status': 'online'},
            'stats': {'requests_served': 10, 'tokens_generated': 100},
        }

    def tearDown(self):
        self.c.close()
        self.c.history.close()
        self.tmp.cleanup()

    def collect(self, at, **changes):
        self.raw.update(written_at=at, **changes)
        self.path.write_text(json.dumps(self.raw))
        from model_readiness import session_key
        from model_combinations import selection_key

        self.c.optimizer.identity_ok = True
        self.c.optimizer.identity_at = at
        self.c.optimizer.identity_session = (self.raw['started_at'], self.raw['pid'])
        self.c.optimizer.warmup = {
            'session': session_key(self.raw),
            'model': selection_key(self.raw['advertised_models']),
            'status': 'ready',
            'verifiedAt': self.raw['started_at'],
        }
        self.c.collect(at)
        return self.c.snapshot['provider']

    def test_visible_counters_and_throughput_follow_model_session(self):
        a = self.collect(1000)
        self.assertEqual((a['sessionJobs'], a['sessionTokens'], a['uptime']), (0, 0, 0))
        b = self.collect(1003, stats={'requests_served': 15, 'tokens_generated': 160})
        self.assertEqual((b['sessionJobs'], b['sessionTokens'], b['tokensPerSecond']), (5, 60, 20))
        c = self.collect(
            1006,
            advertised_models=['b'],
            current_model='b',
            stats={'requests_served': 18, 'tokens_generated': 220},
        )
        self.assertNotEqual(c['session']['id'], a['session']['id'])
        self.assertEqual((c['sessionJobs'], c['sessionTokens']), (0, 0))
        self.assertIsNone(c['tokensPerSecond'])
        self.assertEqual(self.c.sessions.snapshot(1006)['recent'][1]['requests'], 5)

    def test_confirmed_stop_is_offline_even_if_state_file_is_recent(self):
        self.collect(1000)
        a = self.collect(1003, stats={'requests_served': 15, 'tokens_generated': 160})
        self.c.sessions.probe.return_value = False
        b = self.collect(
            1004, inference_active=True, stats={'requests_served': 99, 'tokens_generated': 999}
        )
        self.assertEqual(b['session']['id'], a['session']['id'])
        self.assertEqual(b['session']['status'], 'ended')
        self.assertFalse(b['online'])
        self.assertFalse(b['active'])
        self.assertIsNone(b['tokensPerSecond'])
        self.assertEqual((b['sessionJobs'], b['sessionTokens']), (5, 60))

    def test_same_model_restart_resets_visible_totals_and_history(self):
        a = self.collect(1000)
        b = self.collect(
            1004, pid=20, started_at=1003, stats={'requests_served': 2, 'tokens_generated': 25}
        )
        self.assertEqual((b['sessionJobs'], b['sessionTokens'], b['uptime']), (0, 0, 0))
        self.assertNotEqual(a['session']['id'], b['session']['id'])
        self.assertIsNone(b['tokensPerSecond'])


class RunViewTests(unittest.TestCase):
    def test_run_view_keeps_live_fields_and_only_the_current_trial_run(self):
        full = {
            'at': 1,
            'mode': 'demand',
            'status': 'watching',
            'detail': 'd',
            'selected': ['a'],
            'blockHours': 2,
            'controlVersion': 'v',
            'canManage': True,
            'busy': False,
            'currentModel': 'a',
            'models': [{'id': 'a', 'evidence': {'minutes': [0] * 5000}}],
            'events': [{'at': 1, 'detail': 'x'}],
            'demandAuto': {
                'enabled': True,
                'trial': {'runId': 'r2'},
                'runs': [{'id': 'r1'}, {'id': 'r2', 'decision': {}}],
                'history': [0] * 9000,
            },
        }
        view = run_view(full)
        self.assertEqual((view['models'], view['events']), ([], []))
        self.assertEqual(view['demandAuto']['runs'], [{'id': 'r2', 'decision': {}}])
        self.assertNotIn('history', view['demandAuto'])
        self.assertEqual(view['currentModel'], 'a')
        self.assertLess(len(json.dumps(view)), 1000)


class SampleScheduleTests(unittest.TestCase):
    def test_each_wait_lands_just_after_the_next_second(self):
        for now, work in [(1000.1, 0.05), (1000.1, 0.85), (1000.1, 1.3), (999.95, 0)]:
            done = now + work
            tick = done + next_sample_delay(done)
            self.assertAlmostEqual(tick % 1, 0.1, places=6)
            self.assertGreater(int(tick), int(now) if now % 1 >= 0.1 else int(now) - 1)
            self.assertLessEqual(next_sample_delay(done), 1)


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        (self.root / 'index.html').write_text('dashboard')
        Handler.collector = Collector(self.root)
        Handler.collector.collect(time.time())
        Handler.static_root = self.root
        Handler.dev = False
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True
        )
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def test_same_origin_read_and_no_secrets(self):
        with urllib.request.urlopen(self.base + '/api/snapshot') as r:
            data = r.read()
            self.assertEqual(r.headers['Cache-Control'], 'no-store')
            self.assertNotIn(b'auth_token', data)
            self.assertNotIn(b'api_key', data)

    def test_snapshot_omits_hours_only_for_the_current_revision(self):
        c = Handler.collector
        with urllib.request.urlopen(self.base + '/api/snapshot') as r:
            full = json.load(r)
        revision = full['monitor']['hoursRevision']
        self.assertIsInstance(full['monitor']['hours'], list)
        with urllib.request.urlopen(self.base + '/api/snapshot?hours=' + revision) as r:
            slim = json.load(r)
        self.assertNotIn('hours', slim['monitor'])
        self.assertEqual(slim['monitor']['hoursRevision'], revision)
        self.assertIn('hours', c.snapshot['monitor'])  # the shared snapshot itself is untouched
        with urllib.request.urlopen(self.base + '/api/snapshot?hours=0.0') as r:
            self.assertIn('hours', json.load(r)['monitor'])
        # Same list again keeps the revision; a changed list bumps it.
        c.collect(time.time())
        self.assertEqual(c.snapshot['monitor']['hoursRevision'], revision)
        c.hours_list = [{'at': 0}]
        c.collect(time.time())
        self.assertNotEqual(c.snapshot['monitor']['hoursRevision'], revision)

    def test_session_history_has_scoped_public_records_and_rejects_foreign_origin(self):
        c = Handler.collector
        now = time.time()
        c.sessions.probe = Mock(return_value=True)
        c.sessions.observe(
            'account',
            {
                'pid': 10,
                'started_at': now - 10,
                'written_at': now,
                'attestation_public_key': 'device-key',
                'advertised_models': ['a'],
                'stats': {'requests_served': 5, 'tokens_generated': 10},
            },
            now,
        )
        with urllib.request.urlopen(self.base + '/api/sessions') as r:
            self.assertEqual(r.headers['Cache-Control'], 'no-store')
            result = json.load(r)
            self.assertEqual(result['recent'][0]['id'], result['current']['id'])
            self.assertEqual(result['current']['requests'], 0)
            for private in (
                'device-key',
                '_baseline',
                '_process',
                '_signature',
                '"scope": "account"',
            ):
                self.assertNotIn(private, json.dumps(result))
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(
                urllib.request.Request(
                    self.base + '/api/sessions', headers={'Origin': 'https://example.com'}
                )
            )
        self.assertEqual(error.exception.code, 403)

    def test_foreign_origin_and_host_blocked(self):
        for headers in [
            {'Origin': 'https://example.com'},
            {'Host': 'example.com'},
            {'Sec-Fetch-Site': 'cross-site'},
        ]:
            with self.assertRaises(urllib.error.HTTPError) as result:
                urllib.request.urlopen(
                    urllib.request.Request(self.base + '/api/snapshot', headers=headers)
                )
            self.assertEqual(result.exception.code, 403)

    def test_traversal_blocked(self):
        with self.assertRaises(urllib.error.HTTPError) as result:
            urllib.request.urlopen(self.base + '/%2e%2e/collector.py')
        self.assertEqual(result.exception.code, 404)

    def test_no_mutation_endpoint(self):
        with self.assertRaises(urllib.error.HTTPError) as result:
            urllib.request.urlopen(
                urllib.request.Request(self.base + '/api/snapshot', method='POST', data=b'{}')
            )
        self.assertEqual(result.exception.code, 501)


if __name__ == '__main__':
    unittest.main()
