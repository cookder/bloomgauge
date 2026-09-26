"""Full account-range credit exploration; pagination cannot change statistics."""

import unittest
from history import History


class CreditQueryTests(unittest.TestCase):
    def setUp(self):
        self.h = History(':memory:')
        self.rows = [
            ('one', 1, 100, 'a', 1000000, 10),
            ('one', 2, 200, 'b', 2000000, 20),
            ('one', 3, 200, 'a', 2000000, 5),
            ('one', 4, 300, 'base_reward', 3000000, 0),
            ('one', 5, 300, 'a', -500000, 0),
            ('one', 6, 400, 'a', 0, None),
            ('other', 99, 200, 'private-other-model', 999999999, 99999),
        ]
        self.h.db.executemany('INSERT INTO credits VALUES(?,?,?,?,?,?)', self.rows)

    def tearDown(self):
        self.h.close()

    def query(self, **args):
        return self.h.credits('one', 99, 400, args.pop('page', 1), args.pop('limit', 2), **args)

    def test_default_is_compatible_and_each_sort_applies_before_all_pages(self):
        expected = {
            'newest': [6, 5, 4, 3, 2, 1],
            'oldest': [1, 2, 3, 4, 5, 6],
            'amount-desc': [4, 3, 2, 1, 6, 5],
            'amount-asc': [5, 6, 1, 3, 2, 4],
            'tokens-desc': [2, 1, 3, 6, 5, 4],
        }
        self.assertEqual([x['id'] for x in self.query()['entries']], [6, 5])
        for order, ids in expected.items():
            with self.subTest(order=order):
                pages = [self.query(page=p, sort=order) for p in range(1, 4)]
                self.assertEqual([x['id'] for page in pages for x in page['entries']], ids)
                self.assertTrue(all(page['summary'] == pages[0]['summary'] for page in pages))
                self.assertTrue(
                    all(page['leaderboard'] == pages[0]['leaderboard'] for page in pages)
                )
                self.assertEqual(self.query(page=4, sort=order)['entries'], [])

    def test_summary_covers_filtered_range_once_and_keeps_base_separate(self):
        result = self.query(limit=1)
        self.assertEqual(result['scope'], 'account')
        self.assertEqual(
            result['summary'],
            {
                'count': 6,
                'totalUsd': 7.5,
                'inferenceUsd': 4.5,
                'baseRewardUsd': 3.0,
                'averageUsd': 1.25,
                'minUsd': -0.5,
                'maxUsd': 3.0,
                'outputTokens': 35,
            },
        )
        self.assertEqual(
            result['leaderboard'],
            [
                {'model': 'base_reward', 'count': 1, 'usd': 3.0, 'outputTokens': 0},
                {'model': 'a', 'count': 4, 'usd': 2.5, 'outputTokens': 15},
                {'model': 'b', 'count': 1, 'usd': 2.0, 'outputTokens': 20},
            ],
        )
        self.assertEqual(result['count'], 6)

    def test_category_exact_model_and_model_options_do_not_leak_other_account(self):
        result = self.query(category='inference', model='a', limit=1)
        self.assertEqual(result['count'], 4)
        self.assertEqual(result['summary']['totalUsd'], 2.5)
        self.assertEqual(result['summary']['baseRewardUsd'], 0)
        self.assertEqual(result['modelOptions'], ['a', 'b', 'base_reward'])
        self.assertEqual(len(result['leaderboard']), 1)
        self.assertNotIn('private-other-model', str(result))
        self.assertEqual(self.query(category='base_reward')['summary']['inferenceUsd'], 0)
        self.assertEqual(self.query(model="a' OR 1=1 --")['count'], 0)

    def test_empty_filtered_range_is_not_missing_account_history(self):
        result = self.query(category='base_reward', model='a')
        self.assertEqual(
            result['summary'],
            {
                'count': 0,
                'totalUsd': 0.0,
                'inferenceUsd': 0.0,
                'baseRewardUsd': 0.0,
                'averageUsd': None,
                'minUsd': None,
                'maxUsd': None,
                'outputTokens': 0,
            },
        )
        self.assertEqual(result['entries'], [])
        self.assertEqual(result['leaderboard'], [])
        self.assertEqual((result['coverageStart'], result['coverageEnd']), (100, 400))
        self.assertEqual(result['modelOptions'], ['a', 'b', 'base_reward'])
        result = self.h.credits('one', 201, 399, 1, 20)
        self.assertEqual(result['modelOptions'], ['a', 'base_reward'])
        self.assertEqual(result['count'], 2)
        self.assertEqual(self.h.credits('empty', 1, 2, 1, 20)['coverageStart'], None)

    def test_arguments_are_bounded_and_allowlisted(self):
        for args in (
            {'sort': 'usd; DROP TABLE credits'},
            {'category': 'hardware'},
            {'model': ''},
            {'model': 'a' * 513},
            {'model': 'a\n'},
            {'page': 0},
            {'page': 100001},
            {'limit': 0},
            {'limit': 251},
        ):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.query(**args)
        for start, end in ((float('nan'), 1), (0, float('inf')), (-1, 1), (2, 1)):
            with self.assertRaises(ValueError):
                self.h.credits('one', start, end, 1, 20)


if __name__ == '__main__':
    unittest.main()
