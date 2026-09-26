import unittest
from datetime import datetime, timezone
from history import History
from optimizer_store import OptimizerStore, device_id
from model_projection import ModelProjection, projection
from model_combinations import selection_key

NOW = 1788825600


def minutes(rate=1, start=NOW - 7200, count=60):
    return [{'at': start + i * 60, 'seconds': 60, 'usd': rate / 60} for i in range(count)]


class ProjectionTests(unittest.TestCase):
    def test_linear_pace_preserves_anchor_and_integrates_exact_horizon(self):
        f = projection(minutes(2), ['a'], NOW, NOW - 17)
        self.assertEqual(f['status'], 'ready')
        self.assertEqual(f['points'][0], {'at': NOW - 17, 'additional': 0})
        self.assertEqual(f['points'][-1]['at'], NOW + 86400)
        self.assertAlmostEqual(f['points'][-1]['additional'], 2 * (24 + 17 / 3600))
        self.assertAlmostEqual(f['ratePerHour'], 2)

    def test_short_history_does_not_create_a_forecast_and_idle_is_observed(self):
        self.assertEqual(projection(minutes(count=29), ['a'], NOW, NOW)['status'], 'learning')
        f = projection(minutes(0), ['a'], NOW, NOW)
        self.assertEqual(f['status'], 'ready')
        self.assertTrue(all(p['additional'] == 0 for p in f['points']))

    def test_recent_history_and_momentum_change_rate(self):
        slow = minutes(0.1, NOW - 7 * 86400)
        steady = projection(slow + minutes(0.1, NOW - 3600), ['a'], NOW, NOW)
        busy = projection(slow + minutes(1, NOW - 3600), ['a'], NOW, NOW)
        self.assertGreater(busy['ratePerHour'], steady['ratePerHour'])
        self.assertLessEqual(busy['ratePerHour'], 1)

    def test_comparable_time_slots_affect_future_slope(self):
        # Each local four-hour slot has evidence across a prior full weekday.
        from optimizer_store import context

        rows = minutes(0.1, NOW - 86400, 1440)
        target = context(NOW + 6 * 3600)
        for r in rows:
            if context(r['at']) == target:
                r['usd'] = 2 / 60
        f = projection(rows, ['a'], NOW, NOW)
        slopes = [
            (b['additional'] - a['additional']) / (b['at'] - a['at'])
            for a, b in zip(f['points'], f['points'][1:])
        ]
        self.assertGreater(max(slopes), min(slopes))


class ModelScopeTests(unittest.TestCase):
    def setUp(self):
        self.h = History(':memory:')
        self.s = OptimizerStore(self.h)
        self.p = ModelProjection(self.s)
        self.raw = {
            'attestation_public_key': 'key',
            'advertised_models': ['a'],
            'pid': 1,
            'started_at': NOW - 10000,
            'written_at': NOW,
        }
        self.device = device_id(self.raw)
        self.s.identity(self.device, 'provider')
        self.h.db.execute('INSERT INTO opt_coverage VALUES(?,?,?)', ('acct', NOW - 86400, NOW))

    def tearDown(self):
        self.h.close()

    def credit(self, ident, at, model, amount, provider='provider', account='acct'):
        self.h.db.execute(
            'INSERT INTO opt_credits VALUES(?,?,?,?,?,?,?)',
            (account, ident, provider, at, model, amount, 10),
        )

    def test_single_model_reuses_settled_device_history_including_idle(self):
        for r in minutes():
            self.h.db.execute(
                'INSERT INTO opt_ready_minutes VALUES(?,?,?,?,?,?,?,?)',
                ('acct', self.device, r['at'], 'a', 60, 1, 10, 0),
            )
        self.credit(1, NOW - 7190, 'a', 1000000)
        self.credit(2, NOW - 7190, 'b', 9000000)
        self.credit(3, NOW - 7190, 'base_reward', 9000000)
        self.credit(4, NOW - 7190, 'a', 9000000, provider='other')
        self.credit(5, NOW - 7190, 'a', 9000000, account='other')
        rows = self.p.evidence('acct', self.device, ['a'], NOW)
        self.assertEqual(len(rows), 60)
        self.assertEqual(sum(r['usd'] for r in rows), 1)
        self.assertEqual(self.p.evidence('acct', self.device, ['b'], NOW), [])

    def test_multi_model_requires_joint_runtime_and_preserves_zero_work(self):
        self.assertEqual(self.p.evidence('acct', self.device, ['a', 'b'], NOW), [])
        for offset in range(0, 3600, 3):
            self.s.sample(
                'acct',
                self.device,
                NOW - 7200 + offset,
                NOW - 7197 + offset,
                selection_key(['b', 'a']),
                0,
                0,
                False,
                True,
            )
        self.credit(1, NOW - 7190, 'a', 1000000)
        self.credit(2, NOW - 7190, 'b', 2000000)
        self.credit(3, NOW - 7190, 'c', 9000000)
        self.credit(4, NOW - 7190, 'a', 9000000, provider='other')
        rows = self.p.evidence('acct', self.device, ['a', 'b'], NOW)
        self.assertEqual(len(rows), 60)
        self.assertEqual(sum(r['usd'] for r in rows), 3)
        self.assertEqual(self.p.evidence('acct', self.device, ['a', 'c'], NOW), [])

    def test_pair_evidence_comes_only_from_verified_ready_samples(self):
        self.assertEqual(self.p.evidence('acct', self.device, ['a', 'b'], NOW), [])
        for offset in range(0, 180, 3):
            self.s.sample(
                'acct',
                self.device,
                NOW - 180 + offset,
                NOW - 177 + offset,
                selection_key(['a', 'b']),
                0,
                0,
                False,
                True,
            )
        self.assertEqual(len(self.p.evidence('acct', self.device, ['a', 'b'], NOW)), 1)

    def test_offline_stale_and_changed_models_drop_cached_projection(self):
        monitor = {'status': 'ok', 'observedAt': NOW}
        earnings = {'status': 'ok', 'updatedAt': NOW}
        self.p.cached = minutes()
        self.p.cache_key = ('acct', self.device, ('a',))
        self.p.cache_at = NOW

        def estimate(raw=None, online=True, m=monitor, e=earnings):
            return self.p.estimate(
                'acct',
                raw or self.raw,
                {'online': online, 'tracking': {'counting': True}},
                m,
                e,
                NOW,
            )

        self.assertEqual(estimate()['status'], 'ready')
        self.assertEqual(estimate(online=False)['status'], 'unavailable')
        self.assertEqual(estimate(m={**monitor, 'observedAt': NOW - 181})['status'], 'unavailable')
        self.assertEqual(estimate(e={**earnings, 'status': 'stale'})['status'], 'unavailable')
        self.assertEqual(
            estimate(raw={**self.raw, 'advertised_models': ['b']})['status'], 'learning'
        )

    def test_projection_and_pulse_baseline_share_one_read_per_minute(self):
        calls = []
        original = self.s.evidence
        self.s.evidence = lambda *a: calls.append(a) or original(*a)
        self.p.evidence('acct', self.device, ['a'], NOW, shared=True)
        self.p.evidence('acct', self.device, ['a', 'b'], NOW + 30, shared=True)
        self.assertEqual(len(calls), 1)
        self.p.evidence('acct', self.device, ['a'], NOW + 60, shared=True)
        self.p.evidence('acct', self.device, ['a'], NOW + 60)
        self.assertEqual(len(calls), 3)


if __name__ == '__main__':
    unittest.main()
