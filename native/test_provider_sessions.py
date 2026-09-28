import copy, json, pathlib, tempfile, unittest
from datetime import datetime, timezone
from unittest.mock import Mock
from history import History
from provider_sessions import ProviderSessions, process_identity, BSDInfo
from reputation import Reputation
from test_reputation import provider


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name)
        (self.home / '.darkbloom').mkdir()
        self.h = History(':memory:')
        self.probe = Mock(return_value=True)
        self.s = ProviderSessions(self.h, self.home, self.probe)
        self.raw = {
            'pid': 10,
            'started_at': 900,
            'written_at': 1000,
            'attestation_public_key': 'this-mac',
            'process_identity': {'pid': 10, 'start_time_micros': 900000000},
            'advertised_models': ['a'],
            'current_model': 'a',
            'warm_models': ['a', 'b'],
            'stats': {'requests_served': 10, 'tokens_generated': 100},
        }

    def tearDown(self):
        self.h.close()
        self.tmp.cleanup()

    def observe(self, at=1000, account='account', **changes):
        self.raw.update(written_at=at, **changes)
        (self.home / '.darkbloom/daemon-state.json').write_text(json.dumps(self.raw))
        return self.s.observe(
            account,
            copy.deepcopy(self.raw),
            at,
            {'counting': True, 'verifiedAt': 900, 'detail': 'Verified ready fixture'},
        )

    def counts(self, requests, tokens):
        return {'requests_served': requests, 'tokens_generated': tokens}

    def test_initial_mid_process_adoption_does_not_assign_earlier_models_work(self):
        a = self.observe()
        self.assertEqual((a['requests'], a['tokens']), (0, 0))
        self.assertEqual(a['startedAt'], 1000)
        self.assertEqual(a['providerStartedAt'], 900)
        self.assertEqual(a['counterScope'], 'observed')
        b = self.observe(1003, stats=self.counts(13, 145))
        self.assertEqual((b['requests'], b['tokens']), (3, 45))
        self.assertEqual(b['id'], a['id'])

    def test_bloom_reopening_preserves_the_same_session_and_counters(self):
        a = self.observe()
        self.observe(1003, stats=self.counts(15, 160))
        self.s = ProviderSessions(self.h, self.home, self.probe)
        b = self.observe(1006, stats=self.counts(17, 180))
        self.assertEqual(b['id'], a['id'])
        self.assertEqual(b['requests'], 7)

    def test_confirmed_stop_freezes_and_marks_the_ended_session(self):
        self.observe()
        a = self.observe(1003, stats=self.counts(15, 160))
        self.probe.return_value = False
        b = self.observe(1004, stats=self.counts(999, 9999))
        self.assertEqual(b['status'], 'ended')
        self.assertEqual(b['requests'], 5)
        self.assertEqual(b['endedAt'], 1004)
        self.assertEqual(b['endReason'], 'service_stopped')
        self.assertEqual(self.observe(1005)['endedAt'], 1004)

    def test_restart_same_model_starts_a_new_session_with_startup_counters(self):
        a = self.observe()
        b = self.observe(
            1005,
            pid=20,
            started_at=1004,
            process_identity={'pid': 20, 'start_time_micros': 1004000000},
            stats=self.counts(2, 30),
        )
        self.assertNotEqual(a['id'], b['id'])
        self.assertEqual(b['requests'], 2)
        self.assertEqual(b['startedAt'], 1004)
        self.assertEqual(b['startReason'], 'service_restarted')
        self.assertEqual(self.s.snapshot(1005)['recent'][1]['status'], 'ended')

    def test_stop_and_resume_cannot_reuse_the_closed_session(self):
        a = self.observe()
        self.probe.return_value = False
        self.observe(1001)
        self.probe.return_value = True
        b = self.observe(1002)
        self.assertNotEqual(a['id'], b['id'])
        self.assertEqual(b['requests'], 0)

    def test_same_process_model_change_sets_a_new_counter_baseline(self):
        a = self.observe()
        self.observe(1003, stats=self.counts(15, 150))
        b = self.observe(
            1006, advertised_models=['b'], current_model='b', stats=self.counts(20, 220)
        )
        self.assertNotEqual(a['id'], b['id'])
        self.assertEqual((b['requests'], b['tokens']), (0, 0))
        self.assertEqual(b['startReason'], 'model_changed')
        self.assertEqual(self.observe(1009, stats=self.counts(23, 250))['requests'], 3)

    def test_pair_routing_and_order_do_not_change_the_session(self):
        a = self.observe(advertised_models=['a', 'b'])
        b = self.observe(
            1003, advertised_models=['b', 'a'], current_model='b', stats=self.counts(15, 150)
        )
        self.assertEqual(a['id'], b['id'])
        self.assertEqual(b['models'], ['a', 'b'])
        self.assertEqual(b['requests'], 5)
        self.assertNotEqual(self.observe(1006, advertised_models=['a'])['id'], a['id'])

    def test_stale_read_gap_or_permission_error_is_not_a_stop(self):
        a = self.observe()
        self.probe.return_value = None
        b = self.s.observe('account', self.raw, 1050)
        self.assertEqual(b['status'], 'stale')
        self.assertIsNone(b['endedAt'])
        self.probe.return_value = True
        b = self.observe(1051)
        self.assertEqual(a['id'], b['id'])
        self.assertEqual(b['status'], 'active')

    def test_snapshot_expires_without_new_collection_and_freezes_duration(self):
        a = self.observe()
        self.observe(1003, stats=self.counts(15, 150))
        b = self.s.snapshot(1020)['current']
        self.assertEqual(b['status'], 'stale')
        self.assertIsNone(b['endedAt'])
        self.assertEqual(b['durationSeconds'], 3)
        self.assertEqual(b['requests'], 5)
        self.assertEqual(b['id'], a['id'])

    def test_corrupt_or_missing_state_uses_last_known_process_to_check_stop(self):
        a = self.observe()
        b = self.s.observe('account', {'pid': 999, 'started_at': 1, 'written_at': 1001}, 1001)
        self.assertEqual(b['status'], 'stale')
        self.assertEqual(b['id'], a['id'])
        self.assertEqual(self.probe.call_args.args[0]['pid'], 10)
        self.probe.return_value = False
        self.assertEqual(self.s.observe('account', {}, 1002)['status'], 'ended')

    def test_old_timestamp_does_not_roll_back_the_session(self):
        a = self.observe()
        self.observe(1006, stats=self.counts(20, 200))
        old = {**self.raw, 'written_at': 1003, 'stats': self.counts(12, 110)}
        b = self.s.observe('account', old, 1006)
        self.assertEqual(b['id'], a['id'])
        self.assertEqual(b['requests'], 10)
        self.assertEqual(b['status'], 'stale')

    def test_older_snapshot_of_a_previous_model_cannot_create_a_session(self):
        self.observe()
        b = self.observe(1006, advertised_models=['b'], stats=self.counts(20, 200))
        old = {
            **self.raw,
            'written_at': 1003,
            'advertised_models': ['a'],
            'stats': self.counts(12, 110),
        }
        c = self.s.observe('account', old, 1007)
        self.assertEqual(c['id'], b['id'])
        self.assertEqual(c['models'], ['b'])
        self.assertEqual(c['status'], 'stale')
        self.assertEqual(len(self.s.snapshot(1007)['recent']), 2)

    def test_optional_native_metadata_disappearing_or_appearing_is_not_a_restart(self):
        a = self.observe()
        self.observe(1003, stats=self.counts(15, 150))
        self.raw.pop('process_identity')
        b = self.observe(1006, stats=self.counts(20, 200))
        self.assertEqual(b['id'], a['id'])
        self.assertEqual(b['requests'], 10)
        self.assertEqual(self.probe.call_args.args[0]['startMicros'], 900000000)
        self.h.close()
        self.h = History(':memory:')
        self.s = ProviderSessions(self.h, self.home, self.probe)
        a = self.observe(1007)
        b = self.observe(
            1009,
            process_identity={'pid': 10, 'start_time_micros': 900000000},
            stats=self.counts(23, 230),
        )
        self.assertEqual(b['id'], a['id'])
        self.assertEqual(b['requests'], 3)

    def test_counter_reset_in_same_process_begins_another_session(self):
        a = self.observe()
        self.observe(1003, stats=self.counts(20, 200))
        b = self.observe(1006, stats=self.counts(1, 5))
        self.assertNotEqual(b['id'], a['id'])
        self.assertEqual(b['startReason'], 'counter_reset')
        self.assertEqual((b['requests'], b['tokens']), (0, 0))

    def test_missing_metric_does_not_reset_other_counter_or_fabricate_coverage(self):
        self.observe(stats=self.counts(10, None))
        self.assertEqual(self.observe(1003, stats=self.counts(15, None))['requests'], 5)
        b = self.observe(1006, stats=self.counts(20, 100))
        self.assertEqual((b['requests'], b['tokens']), (10, 0))
        self.assertEqual(b['requestsSince'], 1000)
        self.assertEqual(b['tokensSince'], 1006)

    def test_missing_sample_does_not_hide_a_later_counter_reset(self):
        a = self.observe()
        self.observe(1003, stats=self.counts(25, 200))
        b = self.observe(1006, stats=self.counts(None, None))
        self.assertIsNone(b['requests'])
        b = self.observe(1009, stats=self.counts(3, 30))
        self.assertNotEqual(a['id'], b['id'])
        self.assertEqual(b['startReason'], 'counter_reset')

    def test_pid_reuse_native_identity_creates_a_new_session(self):
        a = self.observe()
        b = self.observe(
            1004, started_at=1003, process_identity={'pid': 10, 'start_time_micros': 1003000000}
        )
        self.assertNotEqual(a['id'], b['id'])

    def test_account_and_device_scopes_do_not_share_history(self):
        a = self.observe()
        b = self.observe(1003, account='second-account')
        self.assertNotEqual(a['id'], b['id'])
        self.assertEqual(len(self.s.snapshot(1003)['recent']), 1)
        c = self.observe(1006, account='account')
        self.assertNotEqual(c['id'], a['id'])
        self.assertEqual(len(self.s.snapshot(1006)['recent']), 2)
        serialized = json.dumps(self.s.snapshot(1006))
        for secret in ('this-mac', 'account', '_signature', '_process', 'startMicros'):
            self.assertNotIn(secret, serialized)

    def test_invalid_inputs_cannot_open_an_active_session(self):
        self.raw['advertised_models'] = []
        self.assertIsNone(self.observe())
        self.assertIsNone(self.s.snapshot(1000)['current'])
        self.assertIsNone(self.observe(account=''))


class SessionReputationTests(SessionTests):
    # Only reuse fixtures; inherited lifecycle tests are suppressed below.
    def setUp(self):
        super().setUp()
        self.observe()
        self.r = Reputation(self.h, self.home, 'secret', self.s, lambda now: 'connection')
        self.sequence = 0

    def ingest_rep(
        self, now, requested=None, record='connection', selected=None, total=100, score=0.9, **rep
    ):
        self.observe(now)
        p = provider(score=score)
        p.update(id=record, models=['a'] if selected is None else selected)
        p['reputation'].update(total_jobs=total, successful_jobs=total - 1, failed_jobs=1, **rep)
        self.sequence += 1
        return self.r.ingest(
            {
                'sequence': self.sequence,
                'status': 'ok',
                'requestedAt': now if requested is None else requested,
                'providers': [p],
            },
            now,
        )

    def test_fresh_baseline_keeps_official_totals_and_separate_session_deltas(self):
        a = self.ingest_rep(1001)
        self.assertEqual(a['data']['totalJobs'], 100)
        self.assertEqual(a['session']['reputation']['totalJobs'], 0)
        b = self.ingest_rep(1004, total=105, score=0.93)
        self.assertEqual(b['data']['totalJobs'], 105)
        self.assertEqual(b['session']['reputation']['totalJobs'], 5)
        self.assertAlmostEqual(b['session']['reputation']['scoreChange'], 3)
        self.assertEqual(b['session']['reputation']['since'], 1001)
        self.assertEqual(b['data']['observedSessionId'], b['session']['id'])
        for secret in ('connection', 'secret', 'this-mac', '_baseline', '_lastCounts'):
            self.assertNotIn('"' + secret + '"', json.dumps(b))

    def test_readings_without_a_score_still_observe_the_session(self):
        # Darkbloom 0.9.10 sends counts only (no composite score).
        a = self.ingest_rep(1001, score=None)
        self.assertEqual(a['data']['observedSessionId'], a['session']['id'])
        b = self.ingest_rep(1004, total=105, score=None)
        rep = b['session']['reputation']
        self.assertEqual((rep['totalJobs'], rep['successfulJobs']), (5, 5))
        self.assertIsNone(rep['scoreChange'])
        self.assertIsNone(rep['scoreNow'])
        self.assertEqual(rep['status'], 'observed')
        # A score that appears or disappears mid-session never breaks the counters.
        c = self.ingest_rep(1007, total=106, score=0.9)
        self.assertIsNone(c['session']['reputation']['scoreChange'])
        self.assertEqual(c['session']['reputation']['totalJobs'], 6)

    def test_late_old_session_response_never_baselines_the_new_model(self):
        self.ingest_rep(1001)
        self.observe(1004, advertised_models=['b'])
        r = self.ingest_rep(1005, requested=1003, selected=['b'])
        self.assertIsNone(r['session']['reputation'])
        self.assertIsNone(r['data']['observedSessionId'])
        self.assertEqual(r['data']['score'], 0.9)

    def test_wrong_connection_or_model_and_unverified_identity_cannot_baseline(self):
        for kwargs in (
            {'record': 'old-connection'},
            {'selected': ['b']},
            {'requested': 900},
            {'requested': 1100},
        ):
            self.assertIsNone(self.ingest_rep(1001, **kwargs)['session']['reputation'])
        self.r.identity_context = lambda now: None
        self.assertIsNone(self.ingest_rep(1002)['session']['reputation'])

    def test_raw_model_change_after_collect_rejects_in_flight_reputation(self):
        raw = {**self.raw, 'advertised_models': ['b'], 'written_at': 1001}
        (self.home / '.darkbloom/daemon-state.json').write_text(json.dumps(raw))
        data = {
            'score': 0.9,
            'providerStatus': 'online',
            **dict.fromkeys(
                (
                    'totalJobs',
                    'successfulJobs',
                    'failedJobs',
                    'uptimeSeconds',
                    'challengesPassed',
                    'challengesFailed',
                ),
                0,
            ),
        }
        self.assertFalse(
            self.s.reputation_observation(data, 1001, 'connection', ['a'], 'connection', 1001)
        )

    def test_counter_reset_stays_invalid_even_after_surpassing_original_baseline(self):
        self.ingest_rep(1001, total=100)
        b = self.ingest_rep(1004, total=10)
        self.assertEqual(b['session']['reputation']['status'], 'reset')
        b = self.ingest_rep(1007, total=120)
        self.assertEqual(b['session']['reputation']['status'], 'reset')
        self.assertIsNone(b['session']['reputation']['totalJobs'])

    def test_reopening_preserves_session_but_rebaselines_new_warm_interval(self):
        a = self.ingest_rep(1001)
        self.ingest_rep(1004, total=105)
        self.s = ProviderSessions(self.h, self.home, self.probe)
        self.observe(1006)
        self.r = Reputation(self.h, self.home, 'secret', self.s, lambda now: 'connection')
        b = self.ingest_rep(1007, total=107)
        self.assertEqual(a['session']['id'], b['session']['id'])
        self.assertEqual(b['session']['reputation']['totalJobs'], 0)
        self.assertEqual(b['session']['reputation']['baselineReason'], 'warm_resumed')

    def test_stale_or_ended_session_observations_are_clearly_marked(self):
        self.ingest_rep(1001)
        self.assertEqual(self.s.snapshot(1200)['current']['reputation']['status'], 'stale')
        self.probe.return_value = False
        self.observe(1201)
        self.assertEqual(self.s.snapshot(1201)['current']['reputation']['status'], 'recorded')

    def test_connection_reconnect_only_restarts_reputation_baseline(self):
        a = self.ingest_rep(1001)
        self.ingest_rep(1004, total=105)
        self.r.identity_context = lambda now: 'new-connection'
        b = self.ingest_rep(1007, record='new-connection', total=107)
        self.assertEqual(a['session']['id'], b['session']['id'])
        self.assertEqual(b['session']['reputation']['totalJobs'], 0)
        self.assertEqual(b['session']['reputation']['baselineReason'], 'connection_changed')

    def test_delayed_response_does_not_overwrite_a_newer_official_reading(self):
        self.ingest_rep(1001, total=100)
        a = self.ingest_rep(1007, total=105, score=0.93)
        b = self.ingest_rep(1009, requested=1004, total=102, score=0.91)
        self.assertEqual(b['data'], a['data'])
        self.assertEqual(b['session']['reputation']['asOf'], 1007)
        self.assertEqual(b['session']['reputation']['totalJobs'], 5)

    def test_a_counter_unknown_in_the_first_reading_baselines_when_it_is_known(self):
        def jobs(now, total, successful):
            self.observe(now)
            p = provider(score=None)
            p.update(id='connection', models=['a'])
            p['reputation'].update(
                total_jobs=total, successful_jobs=successful, failed_jobs=1
            )
            self.sequence += 1
            return self.r.ingest(
                {'sequence': self.sequence, 'status': 'ok', 'requestedAt': now, 'providers': [p]},
                now,
            )

        # First reading: parts above the total (merged counters), so only the total is unknown.
        a = jobs(1001, 100, 100)
        self.assertIsNone(a['data']['totalJobs'])
        self.assertIsNone(a['session']['reputation']['totalJobs'])
        self.assertEqual(a['session']['reputation']['uptimeSeconds'], 0)
        jobs(1004, 110, 109)
        c = jobs(1007, 120, 119)
        rep = c['session']['reputation']
        # Jobs restart together from the first complete reading, so they add up.
        self.assertEqual(
            (rep['totalJobs'], rep['successfulJobs'], rep['failedJobs']), (10, 10, 0)
        )
        self.assertEqual(rep['status'], 'observed')
        self.assertEqual(rep['since'], 1001)


# Lifecycle cases belong to SessionTests only, not the reputation fixture subclass.
for _name in list(SessionTests.__dict__):
    if _name.startswith('test_') and _name not in SessionReputationTests.__dict__:
        setattr(SessionReputationTests, _name, None)


class PartlyLoadedReputationTests(unittest.TestCase):
    """Jason (Sep 27): Darkbloom 0.9.10 loads a large model set on demand, so a
    3+ model session records reputation and concurrency with any model loaded."""

    tearDown, observe = SessionTests.tearDown, SessionTests.observe

    def setUp(self):
        SessionTests.setUp(self)
        self.raw.update(advertised_models=list('abcd'), warm_models=['a', 'c'])
        self.r = Reputation(self.h, self.home, 'secret', self.s, lambda now: 'connection')
        self.sequence = 0

    def ingest(self, now, models=None, **raw):
        session = self.observe(now, **raw)
        p = provider()
        p.update(
            id='connection',
            models=list('abcd') if models is None else models,
            online=True,
            last_heartbeat=datetime.fromtimestamp(now - 2, timezone.utc).isoformat(),
            pending_requests=1,
            max_concurrency=8,
        )
        p['reputation']['total_jobs'] = now - 900
        self.sequence += 1
        result = self.r.ingest(
            {'sequence': self.sequence, 'status': 'ok', 'requestedAt': now, 'providers': [p]}, now
        )
        return session, result

    def test_partly_loaded_set_records_reputation_and_concurrency(self):
        session, a = self.ingest(1001)
        self.assertEqual(a['data']['observedSessionId'], session['id'])
        self.assertEqual(a['data']['concurrency']['pending'], 1)
        _, b = self.ingest(1004, warm_models=['b'])  # Darkbloom swapped what's loaded
        self.assertEqual(b['session']['reputation']['status'], 'observed')
        self.assertIsNotNone(b['data']['concurrency'])

    def test_attribution_checks_still_apply(self):
        # Nothing loaded, another model list, another connection, or a pair missing one.
        self.assertIsNone(self.ingest(1001, warm_models=[])[1]['session']['reputation'])
        rejected = self.ingest(1004, models=list('abc'), warm_models=['a'])
        self.assertIsNone(rejected[1]['session']['reputation'])
        self.r.identity_context = lambda now: 'other-connection'
        self.assertIsNone(self.ingest(1007)[1]['data']['observedSessionId'])
        self.r.identity_context = lambda now: 'connection'
        pair = self.ingest(1010, models=list('ab'), advertised_models=list('ab'), warm_models=['a'])
        self.assertIsNone(pair[1]['session']['reputation'])
        self.assertIsNotNone(
            self.ingest(1013, models=list('ab'), warm_models=list('ab'))[1]['session']['reputation']
        )


if __name__ == '__main__':
    unittest.main()
