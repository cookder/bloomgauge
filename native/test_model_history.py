import json
import tempfile
import threading
import unittest
from unittest.mock import Mock
from history import History
from model_combinations import selection_key
from model_history import model_history
from model_readiness import session_key
from optimizer import Optimizer
from optimizer_store import OptimizerStore, device_id
from test_optimizer import AT, entry


class ModelHistoryTests(unittest.TestCase):
    def setUp(self):
        self.h = History(':memory:')
        self.s = OptimizerStore(self.h)
        self.s.identity('mac', 'p')

    def tearDown(self):
        self.h.close()

    def minute(self, at=AT, model='a', old=False, account='acct', device='mac', seconds=60):
        table = 'opt_minutes' if old else 'opt_ready_minutes'
        self.h.db.execute(
            f'INSERT INTO {table} VALUES(?,?,?,?,?,?,?,?)',
            (account, device, at, model, seconds, 1, 20, 0),
        )

    def report(self, start=AT, end=AT + 7200, selected='a'):
        return model_history(self.s, 'acct', 'mac', start, end, AT + 7400, selected)

    def row(self, report, model='a'):
        return next(r for r in report['models'] if r['id'] == model)

    def test_old_time_is_visible_but_cannot_become_warm_or_optimization_evidence(self):
        self.minute(old=True)
        self.s.credits('acct', [entry(1)], AT + 7200)
        r = self.row(self.report())
        self.assertEqual(r['earlierHours'], 1 / 60)
        self.assertEqual(r['warmHours'], 0)
        self.assertEqual(r['credits']['usd'], 1)
        self.assertEqual(r['evidence'], {})
        self.assertIsNone(r['unitEarnings']['usdPerRequest'])
        self.assertEqual(self.s.evidence('acct', 'mac', AT, AT + 7200, AT + 7400), {})
        self.assertFalse(self.report()['historyResetsOnTestStart'])

    def test_idle_warm_time_and_earnings_survive_beside_legacy_time(self):
        self.minute(old=True)
        self.minute(AT + 60)
        self.minute(AT + 120)
        self.s.credits('acct', [entry(1, AT - 1), entry(2, AT + 70)], AT + 7200)
        r = self.row(self.report())
        self.assertEqual(r['earlierHours'], 1 / 60)
        self.assertEqual(r['warmHours'], 2 / 60)
        self.assertEqual(r['evidence']['usdPerHour'], 30)
        self.assertEqual(r['credits']['usd'], 1)

    def test_same_minute_old_and_new_records_are_not_added_twice(self):
        self.minute(old=True)
        self.minute(seconds=30)
        r = self.row(self.report())
        self.assertEqual(r['earlierHours'], 0)
        self.assertEqual(r['warmHours'], 1 / 120)

    def test_only_this_device_account_and_inference_credits_are_included(self):
        self.minute(old=True)
        self.minute(model='foreign', old=True, account='other')
        self.minute(model='foreign2', device='other')
        self.s.identity('mac', 'replacement-provider')
        self.s.credits(
            'acct',
            [
                entry(1),
                entry(2, provider='other-mac'),
                entry(3, model='base_reward'),
                entry(4, provider='replacement-provider'),
            ],
            AT + 7200,
        )
        self.s.credits('other', [entry(1)], AT + 7200)
        report = self.report()
        self.assertEqual([r['id'] for r in report['models']], ['a'])
        self.assertEqual(report['totals']['inferenceUSD'], 2)
        self.assertEqual(report['totals']['paidJobs'], 2)
        text = json.dumps(report)
        self.assertNotIn('replacement-provider', text)
        self.assertNotIn('acct', text)

    def test_yields_use_matching_credited_work_not_local_counters(self):
        self.minute()
        self.s.credits(
            'acct',
            [
                entry(0, AT - 1),
                {**entry(1, amount=100), 'completion_tokens': 10, 'prompt_tokens': 10},
                {**entry(2, amount=900), 'completion_tokens': 90},
                {**entry(3, amount=9000), 'completion_tokens': None},
                entry(4, provider='foreign'),
                entry(5, model='base_reward'),
            ],
            AT + 7200,
        )
        # Identity replacement can legitimately map two provider IDs to this Mac.
        self.s.identity('other', 'p')
        self.s.credits('other', [entry(6)], AT + 7200)
        r = self.row(self.report())
        u = r['unitEarnings']
        self.assertEqual(u['requests'], 3)
        self.assertAlmostEqual(u['usdPerRequest'], 0.01 / 3)
        self.assertEqual(u['outputSamples'], 2)
        self.assertEqual(u['completeTokenSamples'], 1)
        self.assertEqual(u['usdPerMillionOutput'], 10)  # $0.001 / 100, not $0.01 / local tokens.
        self.assertEqual(u['usdPerMillionTokens'], 5)  # Only the $0.0001 job with BOTH counts.
        self.assertEqual(r['evidence']['tokens'], 20)  # Local throughput stays a separate measure.
        self.assertEqual(self.report()['samples'][0]['usdPerMillionOutput'], 10)

    def test_yields_keep_zero_jobs_adjustments_and_unknown_counts_distinct(self):
        self.minute()
        self.s.credits(
            'acct',
            [
                entry(0, AT - 1),
                {**entry(1, amount=0), 'completion_tokens': 10, 'prompt_tokens': 0},
                {**entry(2, amount=-100), 'completion_tokens': 100000},
                {**entry(3, amount=100), 'completion_tokens': 0, 'prompt_tokens': 0},
            ],
            AT + 7200,
        )
        r = self.row(self.report())
        u = r['unitEarnings']
        self.assertEqual(u['requests'], 2)
        self.assertEqual(u['adjustments'], 1)
        self.assertAlmostEqual(u['usdPerRequest'], 0.00005)
        self.assertEqual(u['usdPerMillionOutput'], 10)
        self.assertEqual(r['credits']['usd'], 0)  # Signed ledger is unchanged.
        self.h.db.execute(
            'UPDATE workload_tokens SET output_tokens=NULL,prompt_tokens=NULL WHERE id IN (1,3)'
        )
        u = self.row(self.report())['unitEarnings']
        self.assertIsNone(u['usdPerMillionOutput'])
        self.assertEqual(u['outputSamples'], 0)

    def test_yields_reuse_explicit_legacy_output_without_inventing_prompt_counts(self):
        self.minute()
        self.s.credits('acct', [entry(0, AT - 1), entry(1, amount=12)], AT + 7200)
        self.h.db.execute('DELETE FROM workload_tokens')
        u = self.row(self.report())['unitEarnings']
        self.assertEqual(u['usdPerMillionOutput'], 1)
        self.assertIsNone(u['usdPerMillionTokens'])
        self.h.db.execute('UPDATE opt_credits SET tokens=0')
        self.assertIsNone(self.row(self.report())['unitEarnings']['usdPerMillionOutput'])

    def test_yields_require_complete_covered_settled_minutes_in_exact_range(self):
        self.minute()
        self.minute(AT + 60, seconds=30)
        self.minute(AT + 120)
        self.s.credits(
            'acct', [entry(0, AT - 1), entry(1), entry(2, AT + 70), entry(3, AT + 130)], AT + 7200
        )
        self.assertEqual(self.row(self.report(start=AT + 1))['unitEarnings']['requests'], 1)
        report = model_history(self.s, 'acct', 'mac', AT, AT + 7200, AT + 180, 'a')
        self.assertEqual(self.row(report)['unitEarnings']['requests'], 1)
        self.h.db.execute('DELETE FROM opt_coverage')
        u = self.row(self.report())['unitEarnings']
        self.assertIsNone(u['usdPerRequest'])
        self.assertEqual(u['requests'], 0)

    def test_pair_yields_are_joint_and_never_contaminate_solo_comparisons(self):
        pair = selection_key(['a', 'b'])
        self.minute()
        self.minute(AT + 60, model=pair)
        self.s.credits(
            'acct',
            [
                entry(0, AT - 1),
                entry(1, amount=100),
                entry(2, AT + 70, amount=1000),
                entry(3, AT + 80, model='b', amount=3000),
            ],
            AT + 7200,
        )
        report = self.report(selected=pair)
        self.assertAlmostEqual(self.row(report)['unitEarnings']['usdPerRequest'], 0.0001)
        self.assertIsNone(self.row(report, 'b')['unitEarnings']['usdPerRequest'])
        self.assertAlmostEqual(self.row(report, pair)['unitEarnings']['usdPerRequest'], 0.002)
        self.assertEqual(self.row(report, pair)['unitEarnings']['requests'], 2)
        self.assertAlmostEqual(report['samples'][0]['usdPerRequest'], 0.002)

    def test_yield_trends_use_weighted_buckets_and_null_gaps(self):
        for at in [AT, AT + 3600, AT + 10800]:
            self.minute(at)
        self.s.credits(
            'acct',
            [
                entry(0, AT - 1),
                entry(1, amount=100),
                entry(2, AT + 3610, amount=1000),
                entry(3, AT + 3620, amount=3000),
                entry(4, AT + 10810, amount=0),
            ],
            AT + 15000,
        )
        before = self.h.db.total_changes
        report = model_history(self.s, 'acct', 'mac', AT, AT + 14400, AT + 15000, 'a')
        self.assertEqual(self.h.db.total_changes, before)
        self.assertEqual([p['usdPerRequest'] for p in report['samples']], [0.0001, 0.002, None, 0])
        self.assertAlmostEqual(self.row(report)['unitEarnings']['usdPerRequest'], 0.0041 / 4)

    def test_similar_demand_filters_hourly_rate_yields_and_trend_without_erasing_quiet_hours(self):
        now = AT + 7400
        for i in range(30):
            self.minute(AT + i * 60)
            load = 1 if i < 20 else 100
            for delta in (5, 35):
                self.h.db.execute(
                    'INSERT INTO opt_network VALUES(?,?,?,?,?,?)',
                    (AT + i * 60 + delta, 'a', load, 0, 10, 10),
                )
        self.s.credits(
            'acct',
            [
                entry(0, AT - 1),
                {**entry(1, AT + 1210, amount=10000), 'completion_tokens': 100},
                {**entry(2, AT + 1250, amount=10000), 'completion_tokens': 100},
            ],
            AT + 7200,
        )
        signal = {
            'status': 'normal',
            'coverage': 1,
            'observedAt': now - 30,
            'pressure': 10,
            'active': 100,
            'load': 100,
            'warmProviders': 10,
        }
        report = model_history(self.s, 'acct', 'mac', AT, AT + 7200, now, 'a', {'a': signal})
        r = self.row(report)
        d = r['demandComparison']
        self.assertAlmostEqual(r['evidence']['usdPerHour'], 0.04)
        self.assertAlmostEqual(d['usdPerHour'], 0.12)
        self.assertAlmostEqual(d['otherDemandHours'], 20 / 60)
        self.assertFalse(d['forecastUsable'])
        self.assertEqual(d['minutes'], 10)
        self.assertEqual(r['credits']['usd'], 0.02)
        self.assertAlmostEqual(r['warmHours'], 0.5)
        self.assertEqual(r['demandUnitEarnings']['requests'], 2)
        self.assertEqual(r['demandUnitEarnings']['usdPerMillionOutput'], 100)
        self.assertAlmostEqual(report['samples'][0]['matchedUsdPerWarmHour'], 0.12)
        self.assertAlmostEqual(report['samples'][0]['usdPerWarmHour'], 0.04)
        stale = model_history(
            self.s,
            'acct',
            'mac',
            AT,
            AT + 7200,
            now,
            'a',
            {'a': {**signal, 'observedAt': now - 100}},
        )
        self.assertIsNone(self.row(stale)['demandComparison']['usdPerHour'])
        self.assertIsNone(self.row(stale)['demandUnitEarnings']['usdPerRequest'])
        self.assertEqual(self.row(stale)['credits'], r['credits'])

    def test_paid_credits_do_not_reconstruct_unobserved_hours(self):
        self.s.credits('acct', [entry(1), entry(2, AT + 3600)], AT + 7200)
        r = self.row(self.report())
        self.assertEqual(r['warmHours'] + r['earlierHours'], 0)
        self.assertEqual(r['evidence'], {})
        self.assertEqual(r['credits']['jobs'], 2)

    def test_date_boundaries_clip_time_but_never_prorate_money(self):
        self.minute(old=True)
        self.s.credits('acct', [entry(1, AT + 10), entry(2, AT + 50)], AT + 7200)
        r = self.row(self.report(start=AT + 30, end=AT + 60))
        self.assertEqual(r['earlierHours'], 1 / 120)
        self.assertEqual(r['credits']['usd'], 1)
        self.assertEqual(r['evidence'], {})

    def test_partial_warm_minute_is_visible_but_not_a_settled_hourly_rate(self):
        self.minute(seconds=30)
        self.s.credits('acct', [entry(1, AT - 1), entry(2)], AT + 7200)
        r = self.row(self.report())
        self.assertEqual(r['warmHours'], 1 / 120)
        self.assertEqual(r['evidence'], {})

    def test_missing_coverage_leaves_gaps_in_earnings_trend(self):
        self.minute(AT, old=True)
        self.minute(AT + 7200, old=True)
        self.s.credits('acct', [entry(1)], AT + 60)
        self.s.credits('acct', [entry(2, AT + 7220)], AT + 7260)
        report = self.report(end=AT + 7300)
        self.assertTrue(
            any(p['confirmedUSD'] is None and p['earlierHours'] is None for p in report['samples'])
        )
        self.assertAlmostEqual(sum(p['confirmedUSD'] or 0 for p in report['samples']), 2)

    def test_signed_adjustments_and_old_model_outside_catalog_remain_visible(self):
        self.s.credits(
            'acct',
            [entry(1, model='retired', amount=-500), entry(2, model='retired', amount=1000)],
            AT + 7200,
        )
        report = self.report(selected='retired')
        self.assertAlmostEqual(self.row(report, 'retired')['credits']['usd'], 0.0005)
        self.assertAlmostEqual(sum(p['confirmedUSD'] or 0 for p in report['samples']), 0.0005)

    def test_pair_time_is_not_assigned_to_its_solo_members(self):
        pair = selection_key(['a', 'b'])
        self.minute(model=pair)
        self.s.credits('acct', [entry(1, AT - 1), entry(2), entry(3, model='b')], AT + 7200)
        report = self.report(selected=pair)
        self.assertEqual(self.row(report, pair)['warmHours'], 1 / 60)
        self.assertEqual(self.row(report)['warmHours'], 0)
        self.assertIsNone(self.row(report, pair)['credits'])
        self.assertEqual(self.row(report, pair)['evidence']['usd'], 2)
        self.assertEqual(report['totals']['inferenceUSD'], 2)
        self.assertTrue(all(p['confirmedUSD'] is None for p in report['samples']))

    def test_long_history_buckets_are_bounded(self):
        self.minute(old=True)
        self.minute(AT + 86400 * 400, old=True)
        report = model_history(self.s, 'acct', 'mac', 0, AT + 86400 * 401, AT + 86400 * 402, 'a')
        self.assertLessEqual(len(report['samples']), 242)

    def test_future_or_empty_ranges_have_no_invented_records(self):
        self.assertEqual(self.report(start=AT + 7500, end=AT + 8000)['models'], [])
        for start, end in ((-1, AT), (AT, AT), (float('nan'), AT), (0, float('inf'))):
            with self.assertRaises(ValueError):
                self.report(start, end)


class PassiveLifecycleTests(unittest.TestCase):
    def test_observe_mode_collects_then_two_tests_preserve_all_saved_evidence(self):
        with tempfile.TemporaryDirectory() as home:
            h = History(':memory:')
            o = Optimizer(h, Mock(), home, threading.Event(), Mock())
            raw = {
                'attestation_public_key': 'key',
                'pid': 123,
                'started_at': AT - 60,
                'written_at': AT,
                'advertised_models': ['a'],
                'warm_models': ['a'],
                'trust': {'status': 'online'},
                'inference_active': False,
                'stats': {'requests_served': 1, 'tokens_generated': 1},
            }
            dev = device_id(raw)
            o.identity_ok = True
            o.identity_at = AT
            o.identity_session = (AT - 60, 123)
            o.warmup = {
                'session': session_key(raw),
                'model': 'a',
                'status': 'ready',
                'verifiedAt': AT - 1,
            }
            for i in range(21):
                o.observe(
                    'acct',
                    {**raw, 'written_at': AT + i * 3},
                    {
                        'at': AT + i * 3,
                        'provider': {'online': True},
                        'hardware': {},
                        'earnings': {},
                    },
                )
            self.assertEqual(o.state['mode'], 'observe')
            self.assertEqual(
                h.db.execute('SELECT SUM(seconds) FROM opt_ready_minutes').fetchone()[0], 60
            )
            o.store.identity(dev, 'p')
            o.store.credits('acct', [entry(1, AT - 1), entry(2)], AT + 3600)
            h.db.execute(
                'INSERT INTO opt_minutes VALUES(?,?,?,?,?,?,?,?)',
                ('acct', dev, AT - 60, 'a', 60, 1, 1, 0),
            )
            before = model_history(o.store, 'acct', dev, 0, AT + 3600, AT + 4000)
            o.snapshot = Mock(
                return_value={
                    'controlError': None,
                    'identityVerified': True,
                    'models': [{'id': 'a', 'available': True}, {'id': 'b', 'available': True}],
                }
            )
            o.read_options = Mock(return_value=('a', [], {}))
            o.discovery_at = __import__('time').time()
            for _ in range(2):
                o.action({'action': 'start', 'mode': 'week', 'models': ['a', 'b'], 'blockHours': 4})
                o.action({'action': 'pause'})
            after = model_history(o.store, 'acct', dev, 0, AT + 3600, AT + 4000)
            self.assertEqual(before, after)
            o.runner.assert_not_called()
            h.close()


if __name__ == '__main__':
    unittest.main()
