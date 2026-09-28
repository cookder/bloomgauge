"""Per-cell request rates from public /v1/stats snapshots: aggregates only, never provider ids."""

import copy
import json
import math
import os
import pathlib
import tempfile
import threading
import unittest
from datetime import datetime, timezone

import network
import network_evidence as ne
import retention
from history import History
from network_evidence import NetworkEvidence, hardware_cell
from optimizer_store import OptimizerStore

HOUR = 1_790_002_800  # an hour boundary
GEMMA, GPT = 'gemma-4-26b-qat-4bit', 'gpt-oss-20b'
PRICING = {
    'fallback_input_price': 50000,
    'fallback_output_price': 200000,
    'prices': [
        {'model': GEMMA, 'input_price': 42000, 'output_price': 220000},
        {'model': GPT, 'input_price': 18000, 'output_price': 90000},
    ],
}


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f') + '123Z'


def prov(pid, model=GEMMA, models=None, req=0, tok=0, status='online', chip=('M5', 'Pro', 48)):
    return {
        'id': pid,
        'status': status,
        'current_model': model,
        'models': [model] if models is None and model else models or [],
        'requests_served': req,
        'tokens_generated': tok,
        'chip_family': chip[0],
        'chip_tier': chip[1],
        'memory_gb': chip[2],
    }


def stats(t, providers, totals=(0, 0, 0)):
    return {
        'snapshot_at': iso(t),
        'providers': providers,
        'total_requests': totals[0],
        'total_prompt_tokens': totals[1],
        'total_completion_tokens': totals[2],
        'last_24h_requests': 1000,
        'last_24h_prompt_tokens': 2_500_000,
        'last_24h_completion_tokens': 300_000,
    }


def counts(req):
    """Providers p0..pn on dedicated gemma with the given request counters."""
    return [prov('p%d' % i, req=r, tok=r * 100) for i, r in enumerate(req)]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.h = History(pathlib.Path(self.tmp.name) / 'h.sqlite3')
        self.addCleanup(self.h.close)
        self.mine = set()
        self.hw = {}
        self.e = NetworkEvidence(self.h, lambda: self.mine, lambda: self.hw)
        self.e.set_pricing(PRICING)

    def rows(self):
        with self.h.lock:
            return [
                dict(zip(ne.COLUMNS, r))
                for r in self.h.db.execute('SELECT * FROM network_cell_rates ORDER BY cell,model')
            ]

    def put(self, hour, cell, model, dedicated=1, providers=2, mean=100.0, sd=10.0, obs=None, **kw):
        row = {
            'hour': hour,
            'cell': cell,
            'model': model,
            'dedicated': dedicated,
            'providers': providers,
            'windows': 12,
            'obs': obs or providers * 12,
            'req_h_median': mean,
            'req_h_mean': mean,
            'req_h_sd': sd,
            'p_zero': 0.0,
            'prompt_tokens': None,
            'completion_tokens': 100.0,
            **kw,
        }
        with self.h.lock:
            self.h.db.execute(
                'INSERT OR REPLACE INTO network_cell_rates VALUES(%s)' % ','.join('?' * 13),
                [row[c] for c in ne.COLUMNS],
            )
            self.h.db.commit()

    def network_row(self, hour, prompt=2000.0):
        self.put(hour, '*', '*', 0, 500, 100000.0, prompt_tokens=prompt, completion_tokens=150.0)


class WindowTests(Base):
    def test_dedicated_and_mixed_boxes_are_separate_groups(self):
        t0 = HOUR + 600
        a = [
            prov('d1', req=100, tok=1000),
            prov('d2', req=200, tok=2000),
            prov('m1', models=[GEMMA, GPT], req=50),
            prov('g1', GPT, req=0, chip=('M1', 'Max', 64)),
        ]
        b = copy.deepcopy(a)
        b[0].update(requests_served=150, tokens_generated=6000, status='serving')
        b[2]['requests_served'] = 75
        b[3]['requests_served'] = 10
        self.assertIsNone(self.e.observe(stats(t0, a), t0))
        w = self.e.observe(stats(t0 + 300, b), t0 + 300)
        self.assertAlmostEqual(w['minutes'], 5)
        g = w['groups'][('M5 Pro|48', GEMMA, True)]
        self.assertEqual((g['n'], g['median'], g['mean'], g['p_zero']), (2, 300, 300, 0.5))
        self.assertEqual(g['completion_tokens'], 100)
        self.assertEqual(w['groups'][('M5 Pro|48', GEMMA, False)]['mean'], 300)
        self.assertEqual(w['groups'][('M1 Max|64', GPT, True)]['mean'], 120)
        self.assertEqual(w['providers'], 4)

    def test_resets_missing_offline_idle_and_switched_providers_are_skipped(self):
        t0 = HOUR + 600
        a = [
            prov('reset', req=500),
            prov('offline', req=10),
            prov('switched', req=10),
            prov('readvertised', req=10),
            prov('idle', model='', models=[GEMMA], req=10),
            prov('gone', req=10),
            prov('ok', req=10),
        ]
        b = [
            prov('reset', req=20),
            prov('offline', req=20, status='untrusted'),
            prov('switched', GPT, req=20),
            prov('readvertised', models=[GEMMA, GPT], req=20),
            prov('idle', model='', models=[GEMMA], req=20),
            prov('new', req=500),
            prov('ok', req=20),
        ]
        self.e.observe(stats(t0, a), t0)
        w = self.e.observe(stats(t0 + 300, b), t0 + 300)
        self.assertEqual(w['providers'], 1)
        self.assertEqual(w['groups'][('M5 Pro|48', GEMMA, True)]['mean'], 120)

    def test_snapshots_are_diffed_every_five_minutes_within_four_to_35(self):
        t0 = HOUR + 60
        e = self.e
        e.observe(stats(t0, counts([0, 0])), t0)
        # Fetches in between are ignored without parsing a new baseline.
        self.assertIsNone(e.observe(stats(t0 + 60, counts([5, 5])), t0 + 60))
        self.assertEqual(e.prev_t, ne.epoch(iso(t0)))
        # The server's cached snapshot: wait for a newer one, even past the step.
        self.assertIsNone(e.observe(stats(t0, counts([5, 5])), t0 + 300))
        w = e.observe(stats(t0 + 360, counts([6, 6])), t0 + 360)
        self.assertAlmostEqual(w['groups'][('M5 Pro|48', GEMMA, True)]['mean'], 60)
        # A fetch 5 min later whose snapshot is only 3 min newer is too short...
        self.assertIsNone(e.observe(stats(t0 + 540, counts([7, 7])), t0 + 660))
        # ...but becomes the baseline for the next one.
        self.assertIsNotNone(e.observe(stats(t0 + 960, counts([14, 14])), t0 + 960))
        # A 40-minute gap (sleep) is dropped, and the next pair works again.
        self.assertIsNone(e.observe(stats(t0 + 3360, counts([90, 90])), t0 + 3360))
        w = e.observe(stats(t0 + 3660, counts([95, 95])), t0 + 3660)
        self.assertAlmostEqual(w['groups'][('M5 Pro|48', GEMMA, True)]['mean'], 60)


class HourlyTests(Base):
    def test_finished_hours_are_written_once_as_aggregates_without_ids(self):
        ids = ['secret-provider-%d' % i for i in range(3)]
        t = HOUR + 300
        req = [0, 0, 0]
        totals = [0, 0, 0]
        for step in range(15):  # windows end at HOUR+600 ... HOUR+4500
            providers = [prov(ids[0], req=req[0], tok=req[0] * 50), prov(ids[1], req=req[1])]
            providers.append(prov(ids[2], GPT, models=[GPT, GEMMA], req=req[2]))
            self.e.observe(stats(t, providers, totals), t)
            if t < HOUR + 3600:
                self.assertEqual(self.rows(), [])  # nothing written within the hour
            t += 300
            req[0] += 10
            req[2] += 5
            totals = [totals[0] + 1000, totals[1] + 2_000_000, totals[2] + 150_000]
        rows = {(r['cell'], r['model'], r['dedicated']): r for r in self.rows()}
        self.assertEqual({r['hour'] for r in rows.values()}, {HOUR})
        g = rows[('M5 Pro|48', GEMMA, 1)]
        self.assertEqual((g['providers'], g['windows'], g['obs']), (2, 10, 20))
        self.assertEqual((g['req_h_mean'], g['req_h_median'], g['p_zero']), (60, 60, 0.5))
        self.assertEqual(g['req_h_sd'], 60)
        self.assertEqual(g['completion_tokens'], 50)
        self.assertIsNone(g['prompt_tokens'])
        self.assertEqual(rows[('M5 Pro|48', GPT, 0)]['req_h_mean'], 60)
        net = rows[('*', '*', 0)]
        self.assertEqual(
            (net['req_h_mean'], net['prompt_tokens'], net['completion_tokens']), (12000, 2000, 150)
        )
        with self.h.lock:
            dump = '\n'.join(self.h.db.iterdump())
        self.assertNotIn('secret-provider', dump)
        self.assertIn('network_cell_rates', dump)
        # The unfinished hour is still used for estimates.
        rows = self.e._rows(1, HOUR + 3600 + 900)
        current = [r for r in rows if r['hour'] == HOUR + 3600 and r['model'] == GEMMA]
        self.assertEqual([(r['cell'], r['providers']) for r in current], [('M5 Pro|48', 2)])


class EstimateTests(Base):
    def test_cell_then_neighbour_then_mixed_then_none(self):
        now = HOUR + 1800
        self.network_row(HOUR)
        self.put(HOUR, 'M5 Pro|48', GEMMA, providers=5, mean=300)
        e = self.e.estimate(GEMMA, 'M5 Pro|48', now=now)
        self.assertEqual(
            (e['source'], e['dedicated_only'], e['providers'], e['req_h']), ('cell', True, 5, 300)
        )
        # Four boxes in the exact cell: pool with the same chip at >= memory, never less.
        self.put(HOUR, 'M4 Max|64', GEMMA, providers=4, mean=100)
        self.put(HOUR, 'M4 Max|128', GEMMA, providers=1, mean=600)
        self.put(HOUR, 'M4 Max|36', GEMMA, providers=9, mean=5000)
        self.put(HOUR, 'M4 Pro|64', GEMMA, providers=9, mean=5000)
        e = self.e.estimate(GEMMA, 'M4 Max|64', now=now)
        self.assertEqual((e['source'], e['providers'], e['req_h']), ('neighbour', 5, 200))
        # No dedicated boxes anywhere near: mixed boxes, flagged.
        self.put(HOUR, 'M1 Max|64', GPT, dedicated=0, providers=5, mean=80)
        e = self.e.estimate(GPT, 'M1 Max|64', now=now)
        self.assertEqual((e['source'], e['dedicated_only']), ('cell', False))
        e = self.e.estimate(GPT, 'M2 Ultra|192', now=now)
        self.assertEqual((e['source'], e['usd_per_h'], e['providers']), ('none', None, 0))
        # Evidence older than the window does not count.
        self.put(HOUR - 5 * 3600, 'M2 Ultra|192', GPT, providers=5)
        self.assertEqual(self.e.estimate(GPT, 'M2 Ultra|192', now=now)['source'], 'none')
        self.assertEqual(self.e.estimate(GPT, 'M2 Ultra|192', hours=6, now=now)['source'], 'cell')

    def test_fewer_than_five_providers_are_no_evidence(self):
        now = HOUR + 1800
        self.network_row(HOUR)
        for n in (2, 4):
            with self.subTest(providers=n):
                self.put(HOUR, 'M5 Max|48', GEMMA, providers=n, mean=300)
                e = self.e.estimate(GEMMA, 'M5 Max|48', now=now)
                self.assertEqual((e['source'], e['usd_per_h']), ('none', None))
        self.put(HOUR, 'M5 Max|48', GEMMA, providers=5, mean=300)
        self.assertEqual(self.e.estimate(GEMMA, 'M5 Max|48', now=now)['source'], 'cell')

    def test_interval_is_student_t_over_providers(self):
        now = HOUR + 1800
        self.network_row(HOUR)
        self.put(HOUR, 'M5 Pro|48', GEMMA, providers=5, mean=300, sd=60)
        e = self.e.estimate(GEMMA, 'M5 Pro|48', now=now)
        margin = 2.132 * 60 / 5**0.5  # t(0.95, df 4), not z = 1.645
        self.assertAlmostEqual(e['req_spread'], margin / 300)
        per = e['usd_per_request']
        self.assertAlmostEqual(e['low'], (300 - margin) * per * math.exp(-e['usd_error']))
        self.assertAlmostEqual(e['high'], (300 + margin) * per * math.exp(e['usd_error']))
        self.assertEqual(
            [ne.t90(df) for df in (0, 1, 4, 30)], [math.inf, 6.314, 2.132, 1.697]
        )
        self.assertAlmostEqual(ne.t90(60), 1.6706, places=4)  # exact 1.67065
        self.assertAlmostEqual(ne.t90(10**6), 1.6449, places=4)

    def test_pooling_hours_weights_observations_and_keeps_an_interval(self):
        now = HOUR + 1800
        self.network_row(HOUR)
        self.put(HOUR - 3600, 'M5 Pro|48', GEMMA, providers=5, mean=100, sd=0, obs=10)
        self.put(HOUR, 'M5 Pro|48', GEMMA, providers=3, mean=400, sd=0, obs=30)
        e = self.e.estimate(GEMMA, 'M5 Pro|48', now=now)
        self.assertEqual((e['providers'], e['windows']), (5, 24))
        self.assertAlmostEqual(e['req_h'], 325)
        self.assertAlmostEqual(e['req_h_sd'], (10 * 225**2 + 30 * 75**2) ** 0.5 / 40**0.5)
        self.assertLess(e['low'], e['usd_per_h'])
        self.assertGreater(e['high'], e['usd_per_h'])
        # Requests seen behind it (each observation a ~5-minute window): 10 x 100 + 30 x 400
        # req/h over 5 min. A thin home base is no base for the excursion counterfactual.
        self.assertAlmostEqual(e['requests'], (10 * 100 + 30 * 400) * 300 / 3600)
        self.assertEqual(self.e.estimate('nothing', 'M5 Pro|48', now=now)['requests'], 0)

    def test_one_sided_90_quantiles_for_lower_bounds(self):
        # The excursion ledger's kill switch and the home choice both use these (PLAN 6.9).
        self.assertEqual(
            [ne.t90_one_sided(df) for df in (0, 1, 2, 5, 30)], [math.inf, 3.078, 1.886, 1.476, 1.310]
        )
        self.assertAlmostEqual(ne.t90_one_sided(60), 1.2958, places=3)  # exact 1.29582
        self.assertAlmostEqual(ne.t90_one_sided(10**6), 1.2816, places=4)

    def test_unknown_cell_uses_this_macs_hardware(self):
        self.put(HOUR, 'M4 Max|128', GEMMA, providers=5)
        self.network_row(HOUR)
        self.assertEqual(self.e.estimate(GEMMA, now=HOUR + 60)['source'], 'none')
        self.hw = {'chip': 'Apple M4 Max', 'memoryTotalGB': 128.0}
        self.assertEqual(self.e.estimate(GEMMA, now=HOUR + 60)['source'], 'cell')


class DollarTests(Base):
    LIST = (2000 * 42000 + 100 * 220000) / 1e12  # gemma: list price x the network's tokens

    def setUp(self):
        super().setUp()
        self.h.cache('account', 'acct')
        OptimizerStore(self.h)  # opt_credits, workload_tokens
        self.mine = {'me-old-session', 'me'}
        self.now = HOUR + 1800
        self.network_row(HOUR, prompt=2000)
        self.put(HOUR, 'M5 Pro|48', GEMMA, providers=5, mean=300, completion_tokens=100)

    def credit(
        self, n, micro, model=GEMMA, prompt=None, account='acct', age=3600, start=0, provider='me'
    ):
        with self.h.lock:
            for i in range(start, start + n):
                self.h.db.execute(
                    'INSERT INTO opt_credits VALUES(?,?,?,?,?,?,?)',
                    (account, i, provider, self.now - age, model, micro, 10),
                )
                if prompt is not None:
                    self.h.db.execute(
                        'INSERT INTO workload_tokens VALUES(?,?,?,?)', (account, i, prompt, 10)
                    )
            self.h.db.commit()
        self.e.own_at = None

    def test_list_price_times_network_tokens_without_own_jobs(self):
        e = self.e.estimate(GEMMA, 'M5 Pro|48', now=self.now)
        self.assertEqual(e['usd_basis'], 'list')
        self.assertAlmostEqual(e['usd_per_request'], self.LIST)
        self.assertAlmostEqual(e['usd_per_h'], 300 * self.LIST)
        # Unknown models use the fallback price.
        self.put(HOUR, 'M5 Pro|48', 'new-model', providers=5, mean=10, completion_tokens=50)
        e = self.e.estimate('new-model', 'M5 Pro|48', now=self.now)
        self.assertAlmostEqual(e['usd_per_request'], (2000 * 50000 + 50 * 200000) / 1e12)

    def test_own_dollars_per_job_when_this_mac_has_enough_jobs(self):
        self.credit(49, 150)
        self.credit(100, 999_999, model='base_reward', start=1000)
        self.credit(100, 999_999, account='other', start=2000)
        self.credit(100, 999_999, age=8 * 86400, start=3000)
        self.credit(100, 999_999, start=4000, provider='another-mac')  # same account
        self.assertEqual(self.e.estimate(GEMMA, 'M5 Pro|48', now=self.now)['usd_basis'], 'list')
        self.credit(1, 150, start=49, provider='me-old-session')
        e = self.e.estimate(GEMMA, 'M5 Pro|48', now=self.now)
        self.assertEqual(e['usd_basis'], 'own')
        self.assertAlmostEqual(e['usd_per_request'], 150e-6)
        self.assertAlmostEqual(e['usd_per_h'], 300 * 150e-6)
        self.assertEqual(e['usd_error'], 0.17)
        forced = self.e.estimate(GEMMA, 'M5 Pro|48', now=self.now, basis='list')
        self.assertEqual(forced['usd_basis'], 'list')
        # Without this Mac's provider ids nothing is its own.
        self.mine = set()
        self.e.own_at = None
        self.assertEqual(self.e.estimate(GEMMA, 'M5 Pro|48', now=self.now)['usd_basis'], 'list')

    def test_own_prompt_tokens_never_replace_the_network_average(self):
        # Andrew's own gemma prompts (3,445/job) against the network's 2,557 inflated his
        # gpt-oss/gemma ratio x1.43: list pricing always uses the network's tokens.
        self.credit(60, 150, prompt=3000)
        e = self.e.estimate(GEMMA, 'M5 Pro|48', now=self.now, basis='list')
        self.assertEqual(e['usd_basis'], 'list')
        self.assertAlmostEqual(e['usd_per_request'], self.LIST)

    def test_list_price_error_is_calibrated_against_own_credits(self):
        # No own jobs on the model: the documented "up to ~2x".
        e = self.e.estimate(GEMMA, 'M5 Pro|48', now=self.now, basis='list')
        self.assertAlmostEqual(e['usd_error'], math.log(2))
        self.assertAlmostEqual(e['low'] / e['usd_per_h'], (1 - 2.132 * 10 / 5**0.5 / 300) / 2)
        # 50 own jobs at 1.5x list: |ln 1.5| plus the daily 17%; 49 are not enough.
        self.credit(49, round(self.LIST * 1.5 * 1e6))
        e = self.e.estimate(GEMMA, 'M5 Pro|48', now=self.now, basis='list')
        self.assertAlmostEqual(e['usd_error'], math.log(2))
        self.credit(1, round(self.LIST * 1.5 * 1e6), start=49)
        e = self.e.estimate(GEMMA, 'M5 Pro|48', now=self.now, basis='list')
        self.assertAlmostEqual(e['usd_error'], math.log(1.5) + 0.17, places=4)
        self.assertAlmostEqual(e['usd_per_request'], self.LIST)  # widened, never shifted
        # More jobs move the calibration with them.
        self.credit(950, round(self.LIST * 0.5 * 1e6), start=100)  # mean $/job ~0.55x list
        e = self.e.estimate(GEMMA, 'M5 Pro|48', now=self.now, basis='list')
        own = (50 * round(self.LIST * 1.5e6) + 950 * round(self.LIST * 0.5e6)) / 1000 / 1e6
        self.assertAlmostEqual(e['usd_error'], abs(math.log(own / self.LIST)) + 0.17, places=4)

    def test_uncalibrated_error_is_wider_outside_gemma_and_gpt_oss(self):
        """calibration-2026-09-28.md (b): Andrew's realized $/job per day against list x the
        network mix. gemma and gpt-oss: 90th percentile |ln| 0.74 over 37 model-days (ln 2).
        Other models: 1.48 over 18 model-days, 5 beyond x2 and 2 beyond x4 (ln 4)."""
        from network_evidence import uncalibrated_error

        for model in (GEMMA, GPT, 'gemma-4-26b-8bit'):
            self.assertAlmostEqual(uncalibrated_error(model), math.log(2))
        niche = ('qwen3.5-35b-a3b', 'Qwen3.5-9B', 'EigenLabs/Qwen3.8-27B-4bit-mtp', 'x', None)
        for model in niche:
            self.assertAlmostEqual(uncalibrated_error(model), math.log(4))
        self.put(HOUR, 'M5 Pro|48', 'qwen3.5-35b-a3b', providers=5, mean=10, completion_tokens=50)
        e = self.e.estimate('qwen3.5-35b-a3b', 'M5 Pro|48', now=self.now, basis='list')
        self.assertAlmostEqual(e['usd_error'], math.log(4))
        # Own jobs calibrate a niche model like any other: |ln(own / list)| + 0.17.
        self.credit(50, round(e['usd_per_request'] * 1.5 * 1e6), model='qwen3.5-35b-a3b')
        self.e.own_at = None
        e = self.e.estimate('qwen3.5-35b-a3b', 'M5 Pro|48', now=self.now, basis='list')
        self.assertAlmostEqual(e['usd_error'], math.log(1.5) + 0.17, places=3)

    def test_without_prices_there_is_no_dollar_figure(self):
        e = NetworkEvidence(self.h).estimate(GEMMA, 'M5 Pro|48', now=self.now)
        self.assertEqual((e['source'], e['usd_per_h'], e['req_h']), ('cell', None, 300))


class RelativeTests(Base):
    def setUp(self):
        super().setUp()
        self.now = HOUR + 1800
        self.network_row(HOUR)

    def test_ratio_in_the_same_cell_with_an_interval(self):
        self.put(HOUR, 'M5 Pro|48', GEMMA, providers=8, mean=300, completion_tokens=100)
        self.put(HOUR, 'M5 Pro|48', GPT, providers=5, mean=100, completion_tokens=600)
        r = self.e.relative(GPT, GEMMA, 'M5 Pro|48', now=self.now)
        a, b = r['estimate'], r['home_estimate']
        self.assertAlmostEqual(r['ratio'], a['usd_per_h'] / b['usd_per_h'])
        # Student-t per model (df 4 and 7) and each model's $/request error, in quadrature.
        spread = math.hypot(
            math.hypot(2.132 * 10 / 5**0.5 / 100, math.log(2)),
            math.hypot(1.895 * 10 / 8**0.5 / 300, math.log(2)),
        )
        self.assertAlmostEqual(r['low'], r['ratio'] * math.exp(-spread))
        self.assertAlmostEqual(r['high'], r['ratio'] * math.exp(spread))
        self.assertEqual((r['source'], r['dedicated_only']), ('cell', True))

    def test_both_models_use_the_first_level_where_both_have_evidence(self):
        self.put(HOUR, 'M5 Pro|48', GEMMA, providers=8, mean=300)
        self.put(HOUR, 'M5 Pro|48', GPT, providers=2, mean=100)
        self.put(HOUR, 'M5 Pro|64', GPT, providers=3, mean=300)
        self.put(HOUR, 'M5 Pro|64', GEMMA, providers=2, mean=600)
        r = self.e.relative(GPT, GEMMA, 'M5 Pro|48', now=self.now)
        self.assertEqual(r['source'], 'neighbour')
        self.assertEqual(r['home_estimate']['source'], 'neighbour')
        self.assertEqual(r['home_estimate']['providers'], 10)
        self.assertEqual((r['estimate']['providers'], r['estimate']['req_h']), (5, 220))

    def test_both_sides_of_a_ratio_use_one_pricing_basis(self):
        self.h.cache('account', 'acct')
        OptimizerStore(self.h)
        self.mine = {'me'}
        self.put(HOUR, 'M5 Pro|48', GEMMA, providers=8, mean=300, completion_tokens=100)
        self.put(HOUR, 'M5 Pro|48', GPT, providers=5, mean=300, completion_tokens=100)
        same = [{'model': m, 'input_price': 42000, 'output_price': 220000} for m in (GEMMA, GPT)]
        self.e.set_pricing({**PRICING, 'prices': same})

        def jobs(model, micro, first):
            with self.h.lock:
                self.h.db.executemany(
                    'INSERT INTO opt_credits VALUES(?,?,?,?,?,?,?)',
                    [('acct', first + i, 'me', self.now - 600, model, micro, 10) for i in range(60)],
                )
                self.h.db.commit()
            self.e.own_at = None

        jobs(GEMMA, 30, 0)  # this Mac's gemma jobs paid 30 micro-USD (list x mix: 106)
        r = self.e.relative(GPT, GEMMA, 'M5 Pro|48', now=self.now)  # default basis
        bases = (r['estimate']['usd_basis'], r['home_estimate']['usd_basis'])
        self.assertEqual(bases, ('list', 'list'))
        self.assertAlmostEqual(r['ratio'], 1.0)
        self.assertEqual(self.e.estimate(GEMMA, 'M5 Pro|48', now=self.now)['usd_basis'], 'own')
        jobs(GPT, 60, 100)  # own $/job for both: both sides use it
        r = self.e.relative(GPT, GEMMA, 'M5 Pro|48', now=self.now)
        bases = (r['estimate']['usd_basis'], r['home_estimate']['usd_basis'])
        self.assertEqual(bases, ('own', 'own'))
        self.assertAlmostEqual(r['ratio'], 2.0)
        r = self.e.relative(GPT, GEMMA, 'M5 Pro|48', now=self.now, basis='list')
        self.assertAlmostEqual(r['ratio'], 1.0)

    def test_missing_home_or_idle_model(self):
        self.put(HOUR, 'M5 Pro|48', GEMMA, providers=8, mean=300)
        self.assertIsNone(self.e.relative(GPT, GEMMA, 'M5 Pro|48', now=self.now)['ratio'])
        self.put(HOUR, 'M5 Pro|48', GPT, providers=5, mean=0, sd=0, p_zero=1.0)
        r = self.e.relative(GPT, GEMMA, 'M5 Pro|48', now=self.now)
        self.assertEqual((r['ratio'], r['low'], r['high']), (0, 0, 0))

    def test_table_lists_models_with_evidence_best_first(self):
        self.put(HOUR, 'M5 Pro|48', GEMMA, providers=8, mean=300)
        self.put(HOUR, 'M5 Pro|48', GPT, providers=5, mean=100)
        self.put(HOUR, 'M3 Ultra|512', 'big-model', providers=4, mean=100)
        t = self.e.table('M5 Pro|48', now=self.now)
        self.assertEqual([m['model'] for m in t['models']], [GEMMA, GPT])
        self.assertIsNone(t['self'])


class SelfTests(Base):
    def test_this_mac_is_found_ranked_and_left_out_of_the_cell(self):
        self.hw = {'chip': 'Apple M4', 'memoryTotalGB': 32.0}
        self.assertEqual(self.e.own_cell(), 'M4|32')
        self.mine = {'old-session', 'me'}
        t0 = HOUR + 600
        peers = [prov('a', req=0), prov('b', req=0), prov('c', req=0), prov('d', req=0)]
        self.e.observe(stats(t0, peers + [prov('me', req=0)]), t0)
        self.assertEqual(self.e.own_cell(), 'M5 Pro|48')
        for p, r in zip(peers, (10, 20, 40, 50)):
            p['requests_served'] = r
        w = self.e.observe(stats(t0 + 600, peers + [prov('me', req=30)]), t0 + 600)
        self.assertEqual(w['groups'][('M5 Pro|48', GEMMA, True)]['n'], 4)
        self.assertEqual(w['self']['percentile'], 0.5)
        s = self.e.self_percentile(now=t0 + 600)
        self.assertEqual(
            (s['model'], s['percentile'], s['peers'], s['req_h']), (GEMMA, 0.5, 4, 180)
        )
        self.assertIsNone(self.e.self_percentile(now=t0 + 5 * 3600))

    def test_failing_identity_lookup_is_harmless(self):
        def broken():
            raise RuntimeError('no identity yet')

        e = NetworkEvidence(self.h, broken, lambda: None)
        t0 = HOUR + 600
        e.observe(stats(t0, counts([0, 0])), t0)
        self.assertIsNotNone(e.observe(stats(t0 + 300, counts([1, 2])), t0 + 300))
        self.assertIsNone(e.own_cell())

    def test_hardware_cells(self):
        pro = {'chip': 'Apple M5 Pro', 'memoryTotalGB': 48.0}
        self.assertEqual(hardware_cell(pro), 'M5 Pro|48')
        big = {'chip': 'Apple M3 Ultra', 'memoryTotalGB': 511.9}
        self.assertEqual(hardware_cell(big), 'M3 Ultra|512')
        self.assertIsNone(hardware_cell({'chip': 'Intel', 'memoryTotalGB': 16}))
        self.assertIsNone(hardware_cell({'chip': 'Apple M4'}))
        self.assertEqual(ne.cell_of('M4', 'Base', 24), 'M4|24')


class RetentionTests(Base):
    def test_cell_rates_keep_30_days_and_other_tables_90(self):
        now = HOUR
        self.put(now - 31 * 86400, 'M5 Pro|48', GEMMA)
        self.put(now - 29 * 86400, 'M5 Pro|48', GEMMA)
        with self.h.lock:
            self.h.db.execute('INSERT INTO samples(at,tokens) VALUES(?,?)', (now - 31 * 86400, 1))
            self.h.db.commit()
        deleted = retention.prune(self.h, now, pause=0)
        self.assertEqual(deleted, {'network_cell_rates': 1})
        self.assertEqual([r['hour'] for r in self.rows()], [now - 29 * 86400])


class ListenerTests(unittest.TestCase):
    class FakeHistory:
        def cache(self, key, data=None):
            return data

    def test_listeners_run_after_each_fetch_and_their_errors_are_contained(self):
        stop = threading.Event()
        n = network.Network(self.FakeHistory(), stop)
        seen = []

        def broken(key, data, at):
            raise RuntimeError('boom')

        def record(key, data, at):
            seen.append((key, data))
            stop.set()

        n.listeners += [broken, record]
        n.fetch = lambda path: {'providers': []}
        with self.assertLogs('bloom.network', 'ERROR'):
            n.loop('stats', '/v1/stats', 0)
        self.assertEqual(seen, [('stats', {'providers': []})])
        self.assertEqual(n.state['stats']['status'], 'ok')

    def test_evidence_takes_stats_and_pricing(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = History(pathlib.Path(tmp) / 'h.sqlite3')
            e = NetworkEvidence(h)
            e.on_network('pricing', PRICING, HOUR)
            e.on_network('stats', stats(HOUR, counts([1])), HOUR)
            e.on_network('capacity', {'models': []}, HOUR)
            self.assertEqual(e.prices[GPT], (18000, 90000))
            self.assertEqual(e.prev_t, ne.epoch(iso(HOUR)))
            h.close()


@unittest.skipUnless(os.environ.get('BLOOM_STATS_SAMPLE'), 'set BLOOM_STATS_SAMPLE=a.json[,b.json]')
class RealSnapshotTests(Base):
    """A real /v1/stats payload (or a real pair about 5-35 min apart)."""

    def test_real_payload(self):
        paths = os.environ['BLOOM_STATS_SAMPLE'].split(',')
        a = json.load(open(paths[0]))
        if len(paths) > 1:
            b = json.load(open(paths[1]))
        else:
            b = copy.deepcopy(a)
            t = ne.epoch(a['snapshot_at']) + 300
            b['snapshot_at'] = iso(t)
            for i, p in enumerate(b['providers']):
                if p.get('current_model'):
                    p['requests_served'] += i % 7 * 10
                    p['tokens_generated'] += i % 7 * 1000
        t0, t1 = ne.epoch(a['snapshot_at']), ne.epoch(b['snapshot_at'])
        self.e.observe(a, t0)
        w = self.e.observe(b, t1)
        self.assertGreater(w['providers'], 300)
        self.assertGreater(len(w['groups']), 50)
        self.assertTrue(all(r >= 0 for g in w['groups'].values() for r in g['rates']))
        self.e.observe(dict(b, snapshot_at=iso(t1 + 3600)), t1 + 3600)  # flush the hour
        self.assertGreater(len(self.rows()), 50)
        ids = {p['id'] for p in a['providers']}
        with self.h.lock:
            dump = '\n'.join(self.h.db.iterdump())
        self.assertFalse(any(i in dump for i in ids))
        table = self.e.table('M5 Pro|48', hours=3, now=t1 + 60)
        self.assertTrue(table['models'])


if __name__ == '__main__':
    unittest.main()
