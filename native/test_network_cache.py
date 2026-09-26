"""The saved network copy is throttled and leaves out the provider list."""

import threading
import unittest
from unittest.mock import patch

import network


class FakeHistory:
    def __init__(self):
        self.writes = []

    def cache(self, key, data=None):
        if data is not None:
            self.writes.append((key, data))
            return data
        return None


class NetworkCacheTests(unittest.TestCase):
    def setUp(self):
        self.h = FakeHistory()
        self.n = network.Network(self.h, threading.Event())
        self.n.state = {
            'capacity': {'status': 'ok', 'data': {'models': [{'id': 'gemma'}]}},
            'stats': {
                'status': 'ok',
                'data': {'providers': [{'id': i} for i in range(1000)], 'total': 5},
            },
        }

    def test_save_is_throttled_and_drops_providers(self):
        with patch.object(network.time, 'time', return_value=1000):
            self.n.save()
            self.n.save()
        self.assertEqual(len(self.h.writes), 1)
        saved = self.h.writes[0][1]
        self.assertNotIn('providers', saved['stats']['data'])
        self.assertEqual(saved['stats']['data']['total'], 5)
        self.assertEqual(len(self.n.state['stats']['data']['providers']), 1000)
        with patch.object(network.time, 'time', return_value=1000 + network.SAVE_SECONDS):
            self.n.save()
        self.assertEqual(len(self.h.writes), 2)

    def test_snapshot_of_one_key_is_a_copy(self):
        cap = self.n.snapshot('capacity')
        cap['data']['models'].append({'id': 'x'})
        self.assertEqual(len(self.n.state['capacity']['data']['models']), 1)
        self.assertEqual(self.n.snapshot('missing'), {})
        self.assertIn('stats', self.n.snapshot())


if __name__ == '__main__':
    unittest.main()
