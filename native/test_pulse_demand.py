import pathlib
import tempfile
import threading
import unittest
from history import History
from optimizer_store import OptimizerStore
from pulse_demand import PulseDemand, MIN_TYPICAL_SAMPLES


class FakeNetwork:
    def __init__(self, capacity):
        self.capacity, self.calls = capacity, 0

    def snapshot(self, key=None):
        self.calls += 1
        return self.capacity if key == 'capacity' else {'capacity': self.capacity}


class PulseDemandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.h = History(pathlib.Path(self.tmp.name) / 'history.sqlite3')
        self.addCleanup(self.h.close)
        self.store = OptimizerStore(self.h)
        self.now = 1_790_000_000
        # A week of typical demand: 4 requests on 8 warm providers (pressure 0.5).
        self.h.db.executemany(
            'INSERT INTO opt_network VALUES(?,?,?,?,?,?)',
            [(self.now - i * 600, 'gemma', 3, 1, 8, 8) for i in range(1, 200)],
        )
        self.h.db.commit()
        self.capacity = {
            'status': 'ok',
            'updatedAt': self.now - 20,
            'data': {
                'models': [
                    {
                        'id': 'gemma',
                        'active_requests': 9,
                        'queued_requests': 3,
                        'warm_providers': 8,
                    },
                    {
                        'id': 'other',
                        'active_requests': 1,
                        'queued_requests': 0,
                        'warm_providers': 4,
                    },
                ]
            },
        }
        self.network = FakeNetwork(self.capacity)
        self.demand = PulseDemand(self.store, self.network)

    def settle(self):
        for t in list(threading.enumerate()):
            if t is not threading.current_thread() and t.daemon:
                t.join(timeout=5)

    def test_compares_live_pressure_with_the_models_typical_week(self):
        first = self.demand.snapshot(['gemma'], self.now)
        self.assertEqual((first['load'], first['warm'], first['pressure']), (12, 8, 1.5))
        self.assertIsNone(first['ratio'])  # the typical value is computed off the collector path
        self.settle()
        value = self.demand.snapshot(['gemma'], self.now)
        self.assertAlmostEqual(value['typicalPressure'], 0.5)
        self.assertAlmostEqual(value['ratio'], 3.0)
        self.assertGreaterEqual(value['typicalSamples'], MIN_TYPICAL_SAMPLES)

    def test_reads_capacity_at_most_every_15_seconds(self):
        for t in range(10):
            self.demand.snapshot(['gemma'], self.now + t)
        self.assertEqual(self.network.calls, 1)
        self.demand.snapshot(['gemma'], self.now + 15)
        self.assertEqual(self.network.calls, 2)

    def test_no_overlay_without_one_model_fresh_data_or_history(self):
        self.assertIsNone(self.demand.snapshot([], self.now))
        self.assertIsNone(self.demand.snapshot(['gemma', 'other'], self.now))
        self.assertIsNone(self.demand.snapshot(['missing'], self.now))
        stale = PulseDemand(self.store, FakeNetwork({**self.capacity, 'updatedAt': self.now - 600}))
        self.assertIsNone(stale.snapshot(['gemma'], self.now))
        bad = PulseDemand(
            self.store,
            FakeNetwork(
                {**self.capacity, 'data': {'models': [{'id': 'gemma', 'active_requests': None}]}}
            ),
        )
        self.assertIsNone(bad.snapshot(['gemma'], self.now))
        # Too little history: live values show, but no ratio against "usual".
        self.demand.snapshot(['other'], self.now)
        self.settle()
        value = self.demand.snapshot(['other'], self.now)
        self.assertEqual(value['pressure'], 0.25)
        self.assertIsNone(value['ratio'])


if __name__ == '__main__':
    unittest.main()
