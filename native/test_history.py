import tempfile, pathlib, unittest
from history import History, epoch


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.h = History(':memory:')

    def tearDown(self):
        self.h.close()

    def test_variable_precision_dates(self):
        self.assertEqual(epoch('2026-09-06T04:05:58.03306Z'), epoch('2026-09-06T04:05:58.033060Z'))

    def test_one_second_fahrenheit(self):
        for i in [100, 101]:
            self.h.save_sample(i, {'tokensPerSecond': 12}, {'cpuTemp': 50, 'gpuTemp': 60})
        d = self.h.chart(99, 102)
        self.assertEqual(d['bucketSeconds'], 1)
        self.assertEqual(len(d['samples']), 2)
        self.assertEqual(d['samples'][0]['cpuTempF'], 122)
        self.assertEqual(d['samples'][0]['gpuTempF'], 140)

    def test_custom_range_bounded_output(self):
        for i in range(4000):
            self.h.save_sample(i + 100, {'tokensPerSecond': i}, {})
        self.assertLessEqual(len(self.h.chart(100, 5000)['samples']), 601)
        self.assertEqual(self.h.chart(2000, 2020)['count'], 21)

    def test_gaps(self):
        for i in [100, 101, 140]:
            self.h.save_sample(i, {'tokensPerSecond': 1}, {})
        d = self.h.chart(90, 150)
        self.assertEqual(len(d['samples']), 4)
        self.assertIsNone(d['samples'][2]['tokensPerSecond'])

    def test_credit_pages_dedupe_and_accounts(self):
        rows = [
            {
                'id': i,
                'created_at': '2026-09-06T04:05:58.03306Z',
                'model': 'gemma',
                'amount_micro_usd': i,
                'completion_tokens': i,
            }
            for i in range(250)
        ]
        self.h.save_credits('a', rows)
        self.h.save_credits('a', rows)
        self.h.save_credits('b', rows[:10])
        a = self.h.credits('a', 0, 9999999999, 1, 100)
        b = self.h.credits('a', 0, 9999999999, 2, 100)
        self.assertEqual(a['count'], 250)
        self.assertEqual(len(a['entries']), 100)
        self.assertFalse({x['id'] for x in a['entries']} & {x['id'] for x in b['entries']})
        self.assertEqual(self.h.credits('b', 0, 9999999999, 1, 100)['count'], 10)

    def test_reopen(self):
        with tempfile.TemporaryDirectory() as folder:
            p = pathlib.Path(folder) / 'db'
            a = History(p)
            a.save_sample(123, {'tokensPerSecond': 5}, {})
            a.close()
            b = History(p)
            self.assertEqual(b.chart(1, 500)['samples'][0]['tokensPerSecond'], 5)
            b.close()

    def test_network_rates(self):
        d = {
            'bucket_seconds': 60,
            'time_series': [
                {
                    'timestamp': '2026-09-06T04:00:00Z',
                    'requests': 120,
                    'prompt_tokens': 600,
                    'completion_tokens': 300,
                }
            ],
        }
        self.h.save_network_series(d)
        self.h.save_network_series(d)
        r = self.h.chart(0, 9999999999, True)
        self.assertEqual(r['samples'][0]['requestsPerMinute'], 120)
        self.assertEqual(r['samples'][0]['tokensPerSecond'], 5)
        self.assertEqual(r['count'], 1)

    def test_finer_network_data_does_not_double_count(self):
        coarse = {
            'bucket_seconds': 3600,
            'time_series': [
                {
                    'timestamp': '2026-09-06T04:00:00Z',
                    'requests': 3600,
                    'prompt_tokens': 0,
                    'completion_tokens': 3600,
                }
            ],
        }
        fine = {
            'bucket_seconds': 60,
            'time_series': [
                {
                    'timestamp': '2026-09-06T04:30:00Z',
                    'requests': 60,
                    'prompt_tokens': 0,
                    'completion_tokens': 60,
                }
            ],
        }
        self.h.save_network_series(coarse)
        self.h.save_network_series(fine)
        d = self.h.chart(0, 9999999999, True)
        self.assertEqual(d['count'], 1)
        self.assertEqual(d['samples'][0]['requestsPerMinute'], 60)

    def test_monitor_hours_survive_source_rotation(self):
        def sample(at):
            return {
                'hours': [{'at': at, 'usd': 1, 'jobs': 2, 'categories': {}}],
                'coverageStartedAt': at,
                'gaps': 0,
                'updatedAt': at,
                'status': 'ok',
            }

        self.h.save_monitor('a', sample(100))
        d = self.h.save_monitor('a', sample(200))
        self.assertEqual(len(d['hours']), 2)
        self.assertEqual(d['coverageStartedAt'], 100)
        self.assertIsNone(self.h.cache('monitor:b'))


if __name__ == '__main__':
    unittest.main()
