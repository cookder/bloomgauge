import math
import unittest
from history import History
from model_demand import model_demand
from optimizer_store import OptimizerStore

AT = 1788742800


class ModelDemandTests(unittest.TestCase):
    def setUp(self):
        self.h = History(':memory:')
        self.store = OptimizerStore(self.h)

    def tearDown(self):
        self.h.close()

    def sample(self, at, model='a', active=4, queued=0, warm=2):
        self.store.network(
            at,
            [
                {
                    'id': model,
                    'active_requests': active,
                    'queued_requests': queued,
                    'warm_providers': warm,
                    'routable_providers': warm + 1,
                }
            ],
        )

    def period(self, start, end, **kwargs):
        for at in range(start, end, 30):
            self.sample(at, **kwargs)

    def read(self, start=AT, end=AT + 1200, now=AT + 1200):
        return model_demand(self.h, start, end, now)

    def test_rank_share_and_trend_use_actual_model_gauges(self):
        self.period(AT, AT + 600, active=4, queued=2)
        self.period(AT + 600, AT + 1200, active=10, queued=2)
        self.period(AT, AT + 1200, model='b', active=3)
        a, b = self.read()['models']
        self.assertEqual(a['id'], 'a')
        self.assertEqual(a['averageLoad'], 9)
        self.assertEqual(a['peakLoad'], 12)
        self.assertEqual(a['averageQueued'], 2)
        self.assertEqual(a['sharePercent'], 75)
        self.assertEqual(a['trendPercent'], 100)
        self.assertEqual(a['trendState'], 'up')
        self.assertEqual(b['trendPercent'], 0)
        self.assertEqual(b['trendState'], 'steady')
        self.assertEqual(a['coveragePercent'], 100)
        self.assertEqual(a['chart'][0]['share'], 100 * 6 / 9)

    def test_outages_and_missing_models_are_not_zero_demand(self):
        self.period(AT, AT + 1200, model='b')
        self.period(AT + 600, AT + 1200, model='a', active=20)
        a = self.read()['models'][0]
        self.assertEqual(a['averageLoad'], 20)
        self.assertEqual(a['coveragePercent'], 50)
        self.assertIsNone(a['firstHalfAverage'])
        self.assertEqual(a['trendState'], 'insufficient')
        self.assertIsNone(a['chart'][0]['load'])
        self.assertEqual(a['chart'][-1]['load'], 20)
        self.assertIsNone(a['trendPercent'])

    def test_frequent_burst_samples_cannot_replace_period_coverage(self):
        for at in range(AT, AT + 40):
            self.sample(at, active=1)
        for at in range(AT + 600, AT + 640):
            self.sample(at, active=5)
        a = self.read()['models'][0]
        self.assertGreater(a['samples'], 40)
        self.assertLess(a['firstHalfCoverage'], 20)
        self.assertEqual(a['trendState'], 'insufficient')

    def test_zero_baseline_and_zero_warm_supply_are_explicit(self):
        self.period(AT, AT + 600, active=0, warm=0)
        self.period(AT + 600, AT + 1200, active=2, warm=0)
        a = self.read()['models'][0]
        self.assertEqual(a['trendState'], 'new')
        self.assertIsNone(a['trendPercent'])
        self.assertIsNone(a['averagePressure'])
        self.assertEqual(a['noWarmSamples'], 20)
        self.assertIsNone(a['chart'][0]['share'])
        self.assertIsNone(a['chart'][-1]['pressure'])

    def test_zero_demand_has_no_share_and_short_ranges_have_no_trend(self):
        self.period(AT, AT + 300, active=0)
        a = self.read(end=AT + 300)['models'][0]
        self.assertIsNone(a['sharePercent'])
        self.assertEqual(a['trendState'], 'insufficient')
        self.assertEqual(a['averagePressure'], 0)

    def test_all_history_compares_recorded_lifetime_and_future_is_clipped(self):
        self.period(AT, AT + 600, active=4)
        self.period(AT + 600, AT + 1200, active=2)
        result = self.read(start=0, end=AT + 86400)
        self.assertEqual(result['comparison']['start'], AT)
        self.assertEqual(result['to'], AT + 1200)
        self.assertEqual(result['models'][0]['trendPercent'], -50)
        self.assertEqual(result['models'][0]['trendState'], 'down')
        self.assertEqual(self.read(start=AT - 86400)['models'][0]['trendState'], 'insufficient')

    def test_bounds_are_half_open_and_coverage_is_global(self):
        self.sample(AT - 30, active=900)
        self.sample(AT, active=2)
        self.sample(AT + 1200, active=800)
        result = self.read()
        self.assertEqual(result['models'][0]['samples'], 1)
        self.assertEqual(result['models'][0]['averageLoad'], 2)
        self.assertEqual(result['coverageStart'], AT - 30)
        self.assertEqual(result['coverageEnd'], AT + 1200)
        self.assertEqual(self.read(start=AT + 2000, end=AT + 2100, now=AT + 2200)['models'], [])

    def test_long_histories_have_bounded_aligned_chart_buckets_and_gaps(self):
        for model in ('a', 'b'):
            self.period(AT, AT + 1800, model=model)
            self.period(AT + 86400 * 365, AT + 86400 * 365 + 1800, model=model)
        end = AT + 86400 * 365 + 1800
        result = self.read(start=0, end=end, now=end)
        a, b = result['models']
        self.assertLessEqual(len(a['chart']), 361)
        self.assertEqual([p['at'] for p in a['chart']], [p['at'] for p in b['chart']])
        self.assertIsNone(a['chart'][100]['load'])
        self.assertGreater(a['chart'][0]['load'], 0)
        self.assertEqual(a['trendState'], 'insufficient')

    def test_model_identifiers_remain_distinct_and_repeated_polls_deduplicate(self):
        for model in ('gemma-4-26b', 'gemma-4-26b-qat-4bit', 'EigenLabs/Qwen3.8-27B-4bit-mtp'):
            self.sample(AT, model=model)
            self.sample(AT, model=model)
        self.assertEqual(self.read()['count'], 3)
        self.assertEqual(len(self.read()['models']), 3)

    def test_empty_and_invalid_ranges(self):
        self.assertEqual(self.read()['models'], [])
        for start, end in ((-1, AT), (AT, AT), (math.nan, AT), (0, math.inf)):
            with self.assertRaises(ValueError):
                self.read(start, end)


if __name__ == '__main__':
    unittest.main()
