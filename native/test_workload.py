import json, unittest
from history import History
from optimizer_store import OptimizerStore
from model_combinations import selection_key
from workload import workload, token_count
from test_optimizer import AT, entry


class WorkloadTests(unittest.TestCase):
    def setUp(self):
        self.h = History(':memory:')
        self.s = OptimizerStore(self.h)
        self.s.identity('mac', 'p')

    def tearDown(self):
        self.h.close()

    def warm(self, at=AT, model='a', seconds=60):
        self.h.db.execute(
            'INSERT INTO opt_ready_minutes VALUES(?,?,?,?,?,?,?,?)',
            ('acct', 'mac', at, model, seconds, 0, 0, 0),
        )

    def save(self, entries):
        self.s.credits('acct', [entry(999999, AT - 1)] + entries, AT + 10000)

    def report(self, start=AT, end=AT + 7200, now=AT + 7400, model=None):
        return workload(self.s, 'acct', 'mac', start, end, now, model)

    def test_prompt_coverage_and_matched_token_denominator(self):
        self.warm()
        a = entry(1, amount=300)
        a.update(prompt_tokens=8, completion_tokens=12)
        b = entry(2, amount=900)
        b['completion_tokens'] = 100
        self.save([a, b])
        t = self.report()['totals']
        self.assertEqual(t['requests'], 2)
        self.assertEqual(t['meanPrompt'], 8)
        self.assertEqual(t['meanOutput'], 56)
        self.assertEqual(t['completeTokenSamples'], 1)
        self.assertEqual(t['usdPerMillionTokens'], 15)
        self.assertAlmostEqual(t['usdPerRequest'], 0.0006)

    def test_account_device_cold_partial_and_reward_exclusion(self):
        self.warm()
        self.warm(AT + 60, seconds=30)
        self.save(
            [
                entry(1),
                entry(2, provider='foreign'),
                entry(3, model='base_reward'),
                entry(4, AT + 70),
                entry(5, AT + 130),
            ]
        )
        self.s.credits('other', [entry(6)], AT + 10000)
        d = self.report()
        self.assertEqual(d['totals']['requests'], 1)
        self.assertEqual(d['excludedUnverifiedCredits'], 2)
        self.assertNotIn('foreign', json.dumps(d))
        self.assertNotIn('acct', json.dumps(d))

    def test_settlement_and_partial_range(self):
        self.warm()
        self.save([entry(1)])
        self.assertEqual(self.report(now=AT + 179)['totals']['requests'], 0)
        self.assertEqual(self.report(now=AT + 180)['totals']['requests'], 1)
        self.assertEqual(self.report(start=AT + 1)['totals']['requests'], 0)

    def test_deduplication_and_sparse_repeated_poll_preserves_prompt(self):
        self.warm()
        a = entry(1)
        a['prompt_tokens'] = 25
        self.save([a])
        self.save([entry(1)])
        self.assertEqual(self.report()['totals']['requests'], 1)
        self.assertEqual(self.report()['totals']['meanPrompt'], 25)

    def test_zero_and_invalid_missing_values(self):
        self.warm()
        a = entry(1)
        a.update(prompt_tokens=True, completion_tokens=0)
        self.save([a])
        t = self.report()['totals']
        self.assertEqual(t['meanOutput'], 0)
        self.assertIsNone(t['meanPrompt'])
        self.assertIsNone(t['usdPerMillionTokens'])
        for v in [True, -1, 1.2, '5', float('nan'), 2**54]:
            self.assertIsNone(token_count(v))

    def test_legacy_output_no_invented_prompts(self):
        self.warm()
        self.save([entry(1)])
        self.h.db.execute('DELETE FROM workload_tokens')
        t = self.report()['totals']
        self.assertEqual(t['meanOutput'], 12)
        self.assertIsNone(t['meanPrompt'])

    def test_pair_attribution_no_double_count(self):
        self.warm(model=selection_key(['a', 'b']))
        self.save([entry(1), entry(2, model='b', amount=2000000)])
        d = self.report()
        self.assertEqual(d['totals']['confirmedUSD'], 3)
        self.assertEqual(d['totals']['warmHours'], 1 / 60)
        self.assertEqual(self.report(model='b')['totals']['confirmedUSD'], 2)

    def test_signed_adjustments_not_requests(self):
        self.warm()
        self.save([entry(1, amount=100), entry(2, amount=-30)])
        t = self.report()['totals']
        self.assertEqual(t['requests'], 1)
        self.assertEqual(t['adjustments'], 1)
        self.assertAlmostEqual(t['confirmedUSD'], 0.00007)
        self.assertAlmostEqual(t['usdPerRequest'], 0.0001)

    def test_missing_buckets_stay_blank(self):
        self.warm()
        self.warm(AT + 900)
        self.save([entry(1), entry(2, AT + 920)])
        self.assertTrue(any(r['confirmedUSD'] is None for r in self.report()['samples']))

    def test_burst_and_size_shift_require_exposure(self):
        for at in range(AT, AT + 3900, 60):
            self.warm(at)
        entries = [entry(i + 1, AT + i * 60 + 10, amount=100) for i in range(60)]
        for i in range(25):
            a = entry(100 + i, AT + 3600 + i * 10, amount=10)
            a['completion_tokens'] = 1
            entries.append(a)
        self.save(entries)
        d = self.report()
        self.assertEqual({a['kind'] for a in d['unusual']}, {'Request burst', 'Output size shift'})
        self.assertEqual(d['unusual'][0]['ratio'], 5)
        self.h.db.execute('DELETE FROM opt_ready_minutes WHERE at>=? AND at<?', (AT, AT + 2400))
        self.assertEqual(self.report()['unusual'], [])

    def test_all_history_bounded_and_absent_model(self):
        self.warm()
        self.save([entry(1)])
        self.assertLessEqual(
            len(self.report(end=AT + 86400 * 400, now=AT + 86400 * 400)['samples']), 182
        )
        self.assertEqual(self.report(model='absent')['totals']['requests'], 0)

    def test_invalid_ranges(self):
        for start, end in [(-1, 20), (20, 10), (0, float('inf')), (float('nan'), 20)]:
            with self.assertRaises(ValueError):
                self.report(start=start, end=end)
