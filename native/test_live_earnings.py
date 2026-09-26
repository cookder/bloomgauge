import copy, json, pathlib, tempfile, unittest
from datetime import datetime, timezone
from unittest.mock import Mock
from collector import Collector, APPLE_EPOCH
from history import History
from live_earnings import EarningsPulse, credit_rows, retry_delay, supplement_monitor
from optimizer_store import OptimizerStore

T = 1788847200


def credit(i, amount=100, at=T + 100, model='a', provider='connection'):
    return {
        'id': i,
        'amount_micro_usd': amount,
        'created_at': datetime.fromtimestamp(at, timezone.utc).isoformat(),
        'model': model,
        'provider_id': provider,
        'completion_tokens': 10,
    }


def account(entries, identity='account'):
    return {
        'account_id': identity,
        'available_balance_micro_usd': 1234567,
        'total_micro_usd': 2345678,
        'count': len(entries),
        'earnings': entries,
    }


def monitor():
    return {
        '_account': 'account',
        '_recentEarningIDs': [5, 10],
        '_categorizedEarningIDs': [5, 10],
        'status': 'ok',
        'updatedAt': T + 100,
        'observedAt': T + 100,
        'coverageStartedAt': T,
        'gaps': 0,
        'hours': [
            {
                'at': T,
                'usd': 0.0005,
                'jobs': 2,
                'categories': {'a': 0.0005},
                'categoryJobs': {'a': 2},
            }
        ],
    }


class LiveCreditTests(unittest.TestCase):
    def test_credit_validation_and_integer_precision(self):
        rows = credit_rows([credit(1, 1), credit(2, -2), credit(1, 1)])
        self.assertEqual([r['microUsd'] for r in rows], [1, -2])
        for bad in (
            {'id': True},
            {'id': 2**54},
            {'amount_micro_usd': 0.1},
            {'completion_tokens': {}},
            {'completion_tokens': 2**80},
            {'model': ''},
        ):
            with self.assertRaises(ValueError):
                credit_rows([{**credit(1), **bad}])
        with self.assertRaises(ValueError):
            credit_rows([credit(1, 1), credit(1, 2)])

    def test_reconcile_new_money_once_and_monitor_catches_up(self):
        m = monitor()
        rows = credit_rows(
            [credit(10, 300), credit(11, 123, T + 110), credit(12, 250, T + 115, 'base_reward')]
        )
        a = supplement_monitor(m, rows, T + 140, T + 140)
        self.assertEqual(round(a['hours'][0]['usd'] * 1e6), 873)
        self.assertEqual(a['hours'][0]['jobs'], 3)
        self.assertEqual(a['hours'][0]['categoryJobs'], {'a': 3, 'base_reward': 1})
        self.assertEqual(m['hours'][0]['usd'], 0.0005)
        self.assertEqual(supplement_monitor(m, rows, T + 140, T + 141)['hours'], a['hours'])
        m.update(
            hours=a['hours'],
            observedAt=T + 140,
            updatedAt=T + 140,
            _recentEarningIDs=[5, 10, 11, 12],
            _categorizedEarningIDs=[5, 10, 11, 12],
        )
        self.assertEqual(supplement_monitor(m, rows, T + 160, T + 160)['hours'], a['hours'])
        self.assertFalse(any(k.startswith('_') for k in a))

    def test_late_lower_id_is_not_a_max_id_cursor_and_forgotten_ids_are_excluded(self):
        rows = credit_rows([credit(9, 123, T + 110), credit(1, 999, T + 20), credit(10, 300)])
        a = supplement_monitor(monitor(), rows, T + 140, T + 140)
        self.assertEqual(round(a['hours'][0]['usd'] * 1e6), 623)

    def test_total_and_category_id_sets_reconcile_independently(self):
        m = monitor()
        m['_recentEarningIDs'].append(11)
        m['hours'][0].update(usd=0.000623, jobs=3)
        a = supplement_monitor(
            m, credit_rows([credit(10, 300), credit(11, 123, T + 110)]), T + 140, T + 140
        )
        self.assertEqual(round(a['hours'][0]['usd'] * 1e6), 623)
        self.assertEqual(round(a['hours'][0]['categories']['a'] * 1e6), 623)
        self.assertEqual(a['hours'][0]['jobs'], 3)

    def test_unknown_category_job_denominator_stays_unknown_even_for_zero_money(self):
        m = monitor()
        m['hours'][0]['categories'] = {'a': 0}
        m['hours'][0].pop('categoryJobs')
        a = supplement_monitor(m, credit_rows([credit(11, 100, T + 110)]), T + 140, T + 140)
        self.assertNotIn('a', a['hours'][0]['categoryJobs'])

    def test_missing_ids_cannot_guess_a_double_count_prone_overlay(self):
        m = monitor()
        m.pop('_recentEarningIDs')
        a = supplement_monitor(m, credit_rows([credit(11)]), T + 140, T + 140)
        self.assertEqual(a['hours'], m['hours'])
        self.assertEqual(a['liveCredits']['status'], 'waiting')

    def test_gap_preserves_money_without_fabricating_complete_coverage(self):
        a = supplement_monitor(
            monitor(),
            credit_rows([credit(10, 300), credit(11, 100, T + 150)]),
            T + 180,
            T + 180,
            T + 160,
            T + 140,
        )
        self.assertEqual(round(a['hours'][0]['usd'] * 1e6), 600)
        self.assertEqual(a['status'], 'partial')
        self.assertEqual(a['gaps'], 1)
        self.assertEqual(a['coverageIntervals'][-1], {'start': T + 100, 'end': T + 140})

    def test_retry_after_seconds_dates_and_exponential_backoff(self):
        self.assertEqual(retry_delay(1), 40)
        self.assertGreaterEqual(retry_delay(9), 900)
        self.assertEqual(retry_delay(1, '7200'), 7200)
        header = datetime.fromtimestamp(T + 180, timezone.utc).strftime('%a, %d %b %Y %H:%M:%S GMT')
        self.assertEqual(retry_delay(1, header, T), 180)
        self.assertEqual(retry_delay(1, 'bad'), 40)
        self.assertEqual(retry_delay(1, 'nan'), 40)
        self.assertGreaterEqual(retry_delay(1, auth=True), 60)


class PulseTests(unittest.TestCase):
    def setUp(self):
        self.h = History(':memory:')
        self.store = OptimizerStore(self.h)
        self.projection = Mock()
        self.projection.evidence.return_value = []
        self.pulse = EarningsPulse(self.h, self.projection)
        self.raw = {'attestation_public_key': 'this-device'}
        self.session = {
            'id': 1,
            'models': ['a'],
            'startedAt': T,
            'status': 'active',
            'endedAt': None,
            'performance': {'status': 'counting', 'segmentStartedAt': T},
        }
        self.h.db.execute(
            'CREATE TABLE IF NOT EXISTS session_ready_intervals(session INTEGER,start REAL,end REAL,PRIMARY KEY(session,start))'
        )
        self.h.db.execute('INSERT INTO session_ready_intervals VALUES(?,?,?)', (1, T, T + 1000))
        self.earnings = {'status': 'ok', 'updatedAt': T + 120, 'sourceAsOf': T + 100}

    def tearDown(self):
        self.h.close()

    def ingest(self, entries, at=T + 120, as_of=None):
        end = (
            max(at - 20, max((r['at'] for r in credit_rows(entries) if r['at'] <= at), default=0))
            if as_of is None
            else as_of
        )
        new = self.h.save_credits('account', entries, end)
        self.store.credits('account', entries, end)
        self.pulse.ingest('account', credit_rows(entries), new, at)
        self.earnings.update(updatedAt=at, sourceAsOf=end)

    def snapshot(self, at=T + 120, connection='connection'):
        return self.pulse.snapshot('account', self.raw, self.session, self.earnings, connection, at)

    def test_first_sync_and_duplicate_polls_do_not_animate_backfill(self):
        self.ingest([credit(1, 100, T + 50)])
        self.assertEqual(self.snapshot()['events'], [])
        self.ingest([credit(1, 100, T + 50), credit(2, 123, T + 130)], T + 140)
        self.assertEqual([r['id'] for r in self.snapshot(T + 140)['events']], [2])
        self.ingest([credit(1, 100, T + 50), credit(2, 123, T + 130)], T + 160)
        self.assertEqual([r['id'] for r in self.snapshot(T + 160)['events']], [2])

    def test_source_timestamps_define_pace_not_arrival_burst(self):
        self.ingest([credit(1, 100, T + 20), credit(2, 200, T + 80)])
        a = self.snapshot()
        w = a['windows']['60']
        self.assertEqual(w['end'], T + 100)
        self.assertEqual(w['seconds'], 60)
        self.assertAlmostEqual(w['ratePerHour'], 0.0002 * 60)
        self.assertEqual(self.snapshot(T + 130)['windows']['60'], w)

    def test_uncovered_new_session_is_unavailable_not_zero(self):
        self.ingest([], T + 120)
        self.session['startedAt'] = T + 100
        w = self.snapshot()['windows']['60']
        self.assertEqual(w['seconds'], 0)
        self.assertIsNone(w['ratePerHour'])

    def test_observed_idle_reads_eventually_form_zero_pace(self):
        self.ingest([], T + 40)
        self.snapshot(T + 40)
        self.ingest([], T + 80)
        self.assertEqual(self.snapshot(T + 80)['windows']['60']['ratePerHour'], 0)

    def test_other_devices_models_rewards_and_old_sessions_are_excluded(self):
        self.ingest(
            [
                credit(1, 100, T + 80),
                credit(2, 999, T + 80, provider='other'),
                credit(3, 999, T + 80, model='b'),
                credit(4, 999, T + 80, model='base_reward'),
                credit(5, 999, T - 5),
            ]
        )
        a = self.snapshot()
        self.assertEqual(a['sessionMicroUsd'], 100)
        self.assertEqual(a['windows']['60']['microUsd'], 100)
        self.assertNotIn('this-device', json.dumps(a))
        self.assertNotIn('"connection"', json.dumps(a))

    def test_coordinator_reconnect_preserves_session_income_and_saved_mapping(self):
        self.ingest([credit(1, 100, T + 50)])
        self.assertEqual(self.snapshot()['sessionMicroUsd'], 100)
        self.ingest([credit(1, 100, T + 50), credit(2, 200, T + 130, provider='new')], T + 160)
        self.assertEqual(self.snapshot(T + 160, 'new')['sessionMicroUsd'], 300)
        self.pulse = EarningsPulse(self.h, self.projection)
        self.ingest([credit(2, 200, T + 130, provider='new')], T + 180)
        self.assertEqual(self.snapshot(T + 180, 'new')['sessionMicroUsd'], 300)

    def test_model_or_service_session_change_excludes_the_previous_session(self):
        self.ingest([credit(1, 100, T + 50)])
        self.snapshot()
        self.session.update(id=2, startedAt=T + 120, models=['b'])
        self.session['performance']['segmentStartedAt'] = T + 120
        self.h.db.execute(
            'INSERT INTO session_ready_intervals VALUES(?,?,?)', (2, T + 120, T + 180)
        )
        self.ingest([credit(1, 100, T + 50), credit(2, 200, T + 130, model='b')], T + 160)
        self.assertEqual(self.snapshot(T + 160)['sessionMicroUsd'], 200)
        self.assertEqual([x['id'] for x in self.snapshot(T + 160)['events']], [2])

    def test_saved_income_survives_expired_identity_and_missing_daemon(self):
        self.ingest([credit(1, 100, T + 50)])
        self.snapshot()
        self.session.update(status='ended', endedAt=T + 75)
        self.assertEqual(self.snapshot(connection=None)['sessionMicroUsd'], 100)
        self.raw = {}
        self.pulse = EarningsPulse(self.h, self.projection)
        a = self.snapshot(connection=None)
        self.assertEqual(a['sessionMicroUsd'], 100)
        self.assertEqual(a['events'], [])
        self.assertIsNone(a['windows']['60']['ratePerHour'])
        self.session['id'] = 2
        self.assertIsNone(self.snapshot(connection=None)['sessionMicroUsd'])

    def test_missing_identity_never_registers_or_borrows_another_device(self):
        self.ingest([credit(1, 100, T + 50)])
        self.snapshot()
        self.raw = {'attestation_public_key': 'other-device'}
        self.assertIsNone(self.snapshot(connection=None)['sessionMicroUsd'])
        self.raw = {}
        self.assertIsNone(self.snapshot()['windows']['60']['ratePerHour'])
        self.assertEqual(
            self.h.db.execute('SELECT COUNT(*) FROM pulse_connections').fetchone()[0], 1
        )

    def test_stale_offline_unmatched_dont_show_a_live_rate(self):
        self.ingest([credit(1, 100, T + 50)])
        self.snapshot()
        self.assertIsNone(self.snapshot(T + 180)['windows']['60']['ratePerHour'])
        self.assertIsNone(self.snapshot(connection=None)['windows']['60']['ratePerHour'])
        self.session.update(status='ended', endedAt=T + 75)
        self.ingest([credit(1, 100, T + 50), credit(2, 500, T + 80)], T + 140)
        a = self.snapshot(T + 140)
        self.assertEqual(a['sessionMicroUsd'], 100)
        self.assertEqual(a['events'], [])
        self.assertIsNone(a['windows']['60']['ratePerHour'])

    def test_baseline_uses_earlier_exact_model_runtime_and_idle(self):
        self.projection.evidence.return_value = [
            {'at': T - 3600, 'seconds': 1800, 'usd': 0.075},
            {'at': T - 1800, 'seconds': 1800, 'usd': 0},
            {'at': T + 1, 'seconds': 60, 'usd': 99},
        ]
        self.ingest([])
        a = self.snapshot()['baseline']
        self.assertEqual(a['ratePerHour'], 0.075)
        self.assertEqual(a['hours'], 1)

    def test_api_rollover_cannot_remove_overlaid_confirmed_money(self):
        m = monitor()
        self.ingest([credit(10, 300), credit(11, 123, T + 110)], T + 140, T + 120)
        a = self.pulse.monitor_snapshot(m, 'account', self.earnings, T + 140)
        self.ingest([credit(12, 200, T + 150)], T + 180, T + 160)
        b = self.pulse.monitor_snapshot(m, 'account', self.earnings, T + 180)
        self.assertEqual(round(a['hours'][0]['usd'] * 1e6), 623)
        self.assertEqual(round(b['hours'][0]['usd'] * 1e6), 823)
        self.assertEqual(b['status'], 'partial')

    def test_overlay_account_guard_and_internal_keys(self):
        self.ingest([credit(11, 200)])
        a = self.pulse.monitor_snapshot(monitor(), 'another', self.earnings, T + 120)
        self.assertEqual(a['hours'], [])
        self.assertFalse(any(k.startswith('_') for k in a))


class LiveCollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.c = Collector(self.root)
        self.c.sessions.probe = Mock(return_value=True)

    def tearDown(self):
        self.c.close()
        self.c.history.close()
        self.tmp.cleanup()

    def test_api_receipt_uses_request_start_for_cache_coverage(self):
        self.c.accept_earnings(account([]), T + 150, T + 100)
        self.assertEqual(self.c.earnings['sourceAsOf'], T + 80)
        self.assertEqual(self.c.earnings['updatedAt'], T + 150)

    def test_bad_credit_cannot_partially_write_the_ledger(self):
        for bad in ({'amount_micro_usd': 'bad'}, {'completion_tokens': 2**80}):
            raw = account([credit(1), {**credit(2), **bad}])
            with self.assertRaises(ValueError):
                self.c.accept_earnings(raw, T + 150)
            self.assertEqual(
                self.c.history.db.execute('SELECT COUNT(*) FROM credits').fetchone()[0], 0
            )

    def test_account_change_clears_old_monitor_even_when_file_missing(self):
        self.c.accept_earnings(account([credit(1)]), T + 120)
        self.c.monitor = monitor()
        self.c.accept_earnings(account([credit(2)], 'second-account'), T + 140)
        self.c.collect(T + 140)
        self.assertEqual(len(self.c.snapshot['monitor']['hours']), 1)
        self.assertAlmostEqual(self.c.snapshot['monitor']['hours'][0]['usd'], 0.0001)
        self.assertEqual(self.c.snapshot['monitor']['hours'][0]['jobs'], 1)
        self.assertEqual(self.c.snapshot['monitor']['source'], 'Bloomkeeper confirmed API ledger')
        self.assertNotIn('_account', self.c.snapshot['monitor'])

    def test_without_monitor_uses_exact_account_ledger_and_preserves_signed_credits(self):
        self.c.accept_earnings(
            account([credit(1, 100), credit(2, -20), credit(3, 70, model='base_reward')]), T + 140
        )
        self.c.collect(T + 140)
        m = self.c.snapshot['monitor']
        self.assertAlmostEqual(sum(h['usd'] for h in m['hours']), 0.00015)
        self.assertEqual(sum(h['jobs'] for h in m['hours']), 2)
        self.assertEqual(m['source'], 'Bloomkeeper confirmed API ledger')
        self.assertEqual(m['liveCredits']['added'], 0)
        self.c.collect(T + 141)
        self.assertAlmostEqual(sum(h['usd'] for h in self.c.snapshot['monitor']['hours']), 0.00015)

    def test_without_coverage_hourly_ledger_does_not_invent_observed_time(self):
        self.c.history.save_credits('account', [credit(1)], None)
        m = self.c.pulse.api_hourly_snapshot(
            'account', {'status': 'stale', 'updatedAt': T + 140}, T + 140
        )
        self.assertIsNone(m['observedAt'])
        self.assertEqual(m['status'], 'stale')
        self.assertEqual(m['gaps'], 1)
        self.assertGreaterEqual(m['coverageIntervals'][0]['end'], T + 140)

    def test_every_snapshot_earnings_input_uses_the_same_reconciled_ledger(self):
        folder = self.root / 'Library/Application Support/Darkbloom Monitor'
        folder.mkdir(parents=True)
        path = folder / 'activity-history.json'
        path.write_text(
            json.dumps(
                {
                    'accountID': 'account',
                    'coverageStartedAt': T - APPLE_EPOCH,
                    'lastIngestedAt': T + 100 - APPLE_EPOCH,
                    'recentEarningIDs': [5, 10],
                    'categorizedEarningIDs': [5, 10],
                    'hours': [
                        {
                            'hour': T - APPLE_EPOCH,
                            'jobs': 2,
                            'microUSD': 500,
                            'earningsByCategory': {'a': {'microUSD': 500, 'entries': 2}},
                        }
                    ],
                }
            )
        )
        provider = self.root / '.darkbloom'
        provider.mkdir()
        (provider / 'daemon-state.json').write_text(
            json.dumps(
                {
                    'pid': 10,
                    'started_at': T,
                    'written_at': T + 140,
                    'attestation_public_key': 'this-device',
                    'advertised_models': ['a'],
                    'current_model': 'a',
                    'stats': {'requests_served': 4, 'tokens_generated': 100},
                }
            )
        )
        self.c.accept_earnings(account([credit(10, 300), credit(11, 123, T + 110)]), T + 140)
        self.c.collect(T + 140)
        a = self.c.snapshot
        self.assertEqual(round(a['monitor']['hours'][0]['usd'] * 1e6), 623)
        self.assertEqual(round(a['forecast']['earnings']['actual'] * 1e6), 623)
        self.assertEqual(a['earnings']['revision'], 1)
        self.assertNotIn('_recentEarningIDs', a['monitor'])
        self.c.collect(T + 141)
        self.assertEqual(self.c.snapshot['forecast']['at'], T + 141)


if __name__ == '__main__':
    unittest.main()
