import copy
import json
import unittest
from history import History
from optimizer_store import OptimizerStore
from decision_journal import DecisionJournal
from demand_optimizer import DemandOptimizer
from model_pricing import quote, comparison, enrich_workload
from opportunity_lab import (
    OpportunityLab,
    factors,
    recent_mix,
    coverage,
    outcome_inputs,
    observed_outcome,
)
from demand_detail import detail
from workload import aggregate
from test_optimizer import AT, entry


def feed(at=AT):
    return {
        'status': 'ok',
        'updatedAt': at,
        'data': {
            'prices': [
                {'model': 'a', 'input_price': 42000, 'output_price': 220000},
                {'model': 'b', 'input_price': 80000, 'output_price': 130000},
            ]
        },
    }


def pressure(at=AT, n=30, warm=100):
    return [
        {'at': at - i * 30, 'active': 200, 'queued': 50, 'warm': warm} for i in reversed(range(n))
    ]


class PriceTests(unittest.TestCase):
    def test_price_units_and_no_fallback_or_alias(self):
        p = quote(feed(), 'a', AT)
        self.assertEqual(p['inputUSDPerMillion'], 0.042)
        self.assertEqual(p['outputUSDPerMillion'], 0.22)
        self.assertTrue(p['fresh'])
        for model in ('A', 'a-qat-4bit', 'unknown'):
            self.assertIsNone(quote(feed(), model, AT)['inputUSDPerMillion'])

    def test_missing_components_zero_future_and_stale(self):
        for bad in (None, True, -1, float('nan'), '42000'):
            d = feed()
            d['data']['prices'][0]['input_price'] = bad
            self.assertFalse(quote(d, 'a', AT)['fresh'])
            self.assertIsNone(quote(d, 'a', AT)['inputUSDPerMillion'])
        d = feed()
        d['data']['prices'][0]['input_price'] = 0
        self.assertEqual(quote(d, 'a', AT)['inputUSDPerMillion'], 0)
        self.assertFalse(quote(feed(), 'a', AT + 1801)['fresh'])
        self.assertFalse(quote(feed(), 'a', AT - 1)['fresh'])
        d = feed()
        d['status'] = 'stale'
        self.assertFalse(quote(d, 'a', AT)['fresh'])

    def test_same_job_mix_not_unmatched_means(self):
        stats = aggregate(
            [
                {'at': AT, 'micro_usd': 100, 'prompt': 900, 'output': 100},
                {'at': AT, 'micro_usd': 9000, 'prompt': None, 'output': 99000},
                {'at': AT, 'micro_usd': -10, 'prompt': 10000, 'output': 10000},
            ]
        )
        p = comparison(stats, quote(feed(), 'a', AT))
        self.assertEqual(p['inputFraction'], 0.9)
        self.assertAlmostEqual(p['blendUSDPerMillion'], 0.0598)
        self.assertEqual(p['realizedUSDPerMillion'], 0.1)
        self.assertEqual(p['completeJobs'], 1)
        self.assertEqual(p['totalJobs'], 2)

    def test_assumed_and_measured_scores_are_indices(self):
        p = quote(feed(), 'a', AT)
        a = factors(pressure(), p, {}, AT)
        self.assertAlmostEqual(a['score'], 2 * (0.042 * 0.85 + 0.22 * 0.15))
        self.assertEqual(a['pressure'], 2)  # Queued work is deliberately excluded.
        mix = {
            'completeTokenSamples': 50,
            'requests': 60,
            'measuredMinutes': 30,
            'lastMeasuredAt': AT - 200,
            'measuredPromptTokens': 9500,
            'measuredOutputTokens': 500,
        }
        b = factors(pressure(), p, mix, AT)
        self.assertEqual(b['mixBasis'], 'measured')
        self.assertAlmostEqual(b['score'], 2 * (0.042 * 0.95 + 0.22 * 0.05))
        for field, value in [
            ('completeTokenSamples', 49),
            ('measuredMinutes', 29),
            ('lastMeasuredAt', AT - 86401),
        ]:
            self.assertEqual(
                factors(pressure(), p, {**mix, field: value}, AT)['mixBasis'], 'assumed'
            )

    def test_thin_repeated_no_supply_and_stale_demand_not_zero_scores(self):
        p = quote(feed(), 'a', AT)
        for rows in (pressure(n=23), pressure(n=1) * 30, pressure(warm=0), pressure(AT - 100), []):
            self.assertIsNone(factors(rows, p, {}, AT)['score'])
        self.assertIsNotNone(factors(pressure(n=24), p, {}, AT)['score'])
        rows = pressure()
        for r in rows:
            r['active'] = 0
        self.assertEqual(factors(rows, p, {}, AT)['score'], 0)


class LabTests(unittest.TestCase):
    def setUp(self):
        self.h = History(':memory:')
        self.s = OptimizerStore(self.h)
        self.s.identity('mac', 'p')
        DecisionJournal(self.h)
        DemandOptimizer(self.h, self.s)
        self.lab = OpportunityLab(self.s)

    def tearDown(self):
        self.h.close()

    def warm(self, start=AT, minutes=60, model='a', seconds=60):
        for at in range(start, start + minutes * 60, 60):
            self.h.db.execute(
                'INSERT OR REPLACE INTO opt_ready_minutes VALUES(?,?,?,?,?,?,?,?)',
                ('acct', 'mac', at, model, seconds, 2, 10, 30),
            )

    def network(self, now=AT):
        for m in ('a', 'b'):
            self.h.db.executemany(
                'INSERT OR REPLACE INTO opt_network VALUES(?,?,?,?,?,?)',
                [(r['at'], m, r['active'], r['queued'], r['warm'], 100) for r in pressure(now)],
            )
        self.h.db.commit()
        return {'capacity': {'status': 'ok', 'updatedAt': now}, 'pricing': feed(now)}

    def pay(self, rows, now=AT + 7200):
        self.s.credits('acct', [entry(999999, AT - 4000, amount=0)] + rows, now)

    def test_record_is_prospective_idempotent_scoped_and_read_only(self):
        network = self.network()
        self.lab.record('acct', 'mac', AT, network, ['a'], True, 'demand', ['a', 'b'])
        self.lab.record('acct', 'mac', AT + 10, network, ['b'], True, 'demand', ['b'])
        d = self.lab.report('acct', 'mac', AT - 86400, AT + 1, AT + 1)
        self.assertEqual(d['observations'], 1)
        self.assertEqual(d['coverageStart'], AT)
        self.assertEqual(d['latest']['selected'], ['a'])
        self.assertEqual(d['latest']['leader'], 'b')
        self.assertEqual(d['latest']['version'], 'price-pressure-v1')
        self.assertTrue(d['passive'])
        self.assertTrue(d['fresh'])
        self.assertEqual(
            self.h.db.execute('SELECT COUNT(*) FROM demand_switch_runs').fetchone()[0], 0
        )
        for account, device in [('other', 'mac'), ('acct', 'other')]:
            self.assertIsNone(self.lab.report(account, device, AT - 1, AT + 1, AT + 1)['latest'])
        self.assertNotIn('acct', json.dumps(d))
        self.assertNotIn('"mac"', json.dumps(d))

    def test_prices_frozen_no_repricing_older_observations(self):
        n = self.network()
        self.lab.record('acct', 'mac', AT, n, ['a'], True, 'observe', ['a'])
        old = self.lab.report('acct', 'mac', AT, AT + 1, AT + 1)['latest']['models']
        n['pricing']['data']['prices'][0]['input_price'] = 999999
        self.assertEqual(
            self.lab.report('acct', 'mac', AT, AT + 1, AT + 1)['latest']['models'], old
        )

    def test_mix_filters_account_identity_solo_warm_coverage_and_time(self):
        self.warm(minutes=1)
        self.warm(AT + 60, 1, seconds=30)
        self.warm(AT + 120, 1, model='@combo:["a","b"]')
        rows = []
        for i, at, provider in [
            (1, AT + 10, 'p'),
            (2, AT + 20, 'other'),
            (3, AT + 70, 'p'),
            (4, AT + 130, 'p'),
            (5, AT + 400, 'p'),
        ]:
            r = entry(i, at, provider=provider, amount=100)
            r.update(prompt_tokens=900, completion_tokens=100)
            rows.append(r)
        self.pay(rows)
        mix = recent_mix(self.s, 'acct', 'mac', AT + 1000)
        self.assertEqual(mix['a']['completeTokenSamples'], 1)
        self.assertEqual(mix['a']['measuredMinutes'], 1)
        self.assertEqual(recent_mix(self.s, 'other', 'mac', AT + 1000), {})
        self.assertEqual(recent_mix(self.s, 'acct', 'mac', AT + 120), {})

    def test_clock_outcome_keeps_quiet_and_signed_pay_excludes_other_devices_base(self):
        self.warm(minutes=15)
        self.pay(
            [
                entry(1, AT + 10, amount=40000),
                entry(2, AT + 100, amount=-10000),
                entry(3, AT + 20, amount=999999, provider='other'),
                entry(4, AT + 50, model='base_reward'),
            ]
        )
        inputs = outcome_inputs(self.s, 'acct', 'mac', AT, AT + 3600)
        o = observed_outcome(inputs, 'a', AT, 1800, AT + 4000)
        self.assertAlmostEqual(o['inferenceUSD'], 0.03)
        self.assertAlmostEqual(o['usdPerClockHour'], 0.06)
        self.assertAlmostEqual(o['warmUSDPerHour'], 0.12)
        self.assertFalse(o['comparable'])
        self.assertEqual(o['warmMinutes'], 15)
        self.assertIsNone(observed_outcome(inputs, 'a', AT, 1800, AT + 1900)['usdPerClockHour'])

    def test_missing_credit_coverage_is_not_zero_and_changed_model_not_comparable(self):
        self.warm(minutes=15)
        self.warm(AT + 900, 15, model='b')
        self.pay([entry(1, AT + 20, amount=4000)])
        inputs = outcome_inputs(self.s, 'acct', 'mac', AT, AT + 1800)
        o = observed_outcome(inputs, 'a', AT, 1800, AT + 4000)
        self.assertTrue(o['selectionChanged'])
        self.assertFalse(o['comparable'])
        inputs['intervals'] = [(AT, AT + 900)]
        o = observed_outcome(inputs, 'a', AT, 1800, AT + 4000)
        self.assertEqual(o['status'], 'partial')
        self.assertIsNone(o['usdPerClockHour'])
        self.assertEqual(o['coveredPercent'], 50)

    def test_gap_and_unchosen_earnings_not_fabricated(self):
        n = self.network()
        self.lab.record('acct', 'mac', AT, n, ['a'], True, 'demand', ['a', 'b'])
        n = self.network(AT + 240)
        self.lab.record('acct', 'mac', AT + 240, n, ['a'], True, 'demand', ['a', 'b'])
        r = self.lab.report('acct', 'mac', AT, AT + 300, AT + 300)
        self.assertTrue(any(not x['scores'] for x in r['history']))
        self.assertEqual(len(r['evaluations']), 1)
        self.assertEqual(r['evaluations'][0]['model'], 'a')
        self.assertEqual(r['evaluations'][0]['leader'], 'b')
        self.assertFalse(self.lab.report('acct', 'mac', AT, AT + 1000, AT + 1000)['fresh'])

    def test_details_smoothing_gaps_time_alignment_and_local_zero(self):
        self.network(AT + 870)
        self.warm(minutes=15)
        self.pay([])
        d = detail(self.s, 'acct', 'mac', 'a', AT, AT + 1800, AT + 2000)
        self.assertTrue(any(p['active'] == 200 for p in d['samples']))
        self.assertTrue(any(p['active'] is None for p in d['samples']))
        self.assertEqual(
            next(p['usdPerWarmHour'] for p in d['localSamples'] if p['usdPerWarmHour'] is not None),
            0,
        )
        self.assertTrue(any(p['usdPerWarmHour'] is None for p in d['localSamples']))
        self.assertTrue(
            all(
                p['usdPerWarmHour'] is None
                for p in detail(self.s, 'acct', 'other', 'a', AT, AT + 1800, AT + 2000)[
                    'localSamples'
                ]
            )
        )
        self.assertEqual(d['localBucketSeconds'], 300)

    def test_bad_ranges(self):
        for start, end in [(AT, AT), (float('nan'), AT), (0, float('inf')), (-1, AT), (True, AT)]:
            with self.assertRaises(ValueError):
                self.lab.report('acct', 'mac', start, end, AT + 1)
            with self.assertRaises(ValueError):
                detail(self.s, 'acct', 'mac', 'a', start, end, AT + 1)

    def test_coverage_union(self):
        self.assertEqual(coverage([(0, 30), (20, 70), (70, 100)], 0, 100), 1)
        self.assertEqual(coverage([(0, 30), (40, 100)], 0, 100), 0.9)


if __name__ == '__main__':
    unittest.main()
