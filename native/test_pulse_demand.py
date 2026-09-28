import os
import pathlib
import tempfile
import threading
import time
import unittest
from datetime import datetime
from history import History
from optimizer_store import OptimizerStore
from pulse_demand import PulseDemand, TYPICAL_REFRESH_SECONDS

DAY = 86400


class FakeNetwork:
    def __init__(self, capacity):
        self.capacity, self.calls = capacity, 0

    def snapshot(self, key=None):
        self.calls += 1
        return self.capacity if key == 'capacity' else {'capacity': self.capacity}


class PulseDemandTests(unittest.TestCase):
    def setUp(self):
        self.old_tz = os.environ.get('TZ')
        os.environ['TZ'] = 'America/New_York'
        time.tzset()
        self.addCleanup(self.restore_tz)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.h = History(pathlib.Path(self.tmp.name) / 'history.sqlite3')
        self.addCleanup(self.h.close)
        self.store = OptimizerStore(self.h)
        # A Wednesday, 14:30 local.
        self.now = int(datetime.fromisoformat('2026-09-23T14:30:00-04:00').timestamp())
        # Usual for this time of day: 4 requests on 8 warm providers (pressure
        # 0.5) on the three previous Wednesdays, 13:30-15:30. One busy
        # Wednesday, busy mornings and a busy weekend raise the mean, not the median.
        for days, pressure_load in ((7, 4), (14, 4), (21, 4), (28, 40)):
            self.insert('gemma', self.now - days * DAY - 3600, 240, pressure_load)
        for days in range(1, 29):
            self.insert('gemma', self.now - days * DAY - 5 * 3600, 60, 40)
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

    def restore_tz(self):
        if self.old_tz is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = self.old_tz
        time.tzset()

    def insert(self, model, start, count, load, warm=8):
        self.h.db.executemany(
            'INSERT OR REPLACE INTO opt_network VALUES(?,?,?,?,?,?)',
            [(start + i * 30, model, load - 1, 1, warm, warm) for i in range(count)],
        )

    def settle(self):
        for t in list(threading.enumerate()):
            if t is not threading.current_thread() and t.daemon:
                t.join(timeout=5)

    def test_compares_live_pressure_with_the_usual_for_this_time_of_day(self):
        first = self.demand.snapshot(['gemma'], self.now)
        self.assertEqual((first['load'], first['warm'], first['pressure']), (12, 8, 1.5))
        self.assertIsNone(first['ratio'])  # the typical value is computed off the collector path
        self.settle()
        value = self.demand.snapshot(['gemma'], self.now)
        self.assertAlmostEqual(value['typicalPressure'], 0.5)
        self.assertAlmostEqual(value['ratio'], 3.0)
        self.assertEqual(value['typicalSamples'], 4 * 240)

    def test_no_ratio_without_three_days_at_this_time_of_day(self):
        # Plenty of history, but only at other hours or on weekends.
        for days in range(1, 29):
            self.insert('other', self.now - days * DAY - 6 * 3600, 240, 2)
        for days in (3, 4, 10, 11):  # Saturdays and Sundays, same clock time
            self.insert('other', self.now - days * DAY - 3600, 240, 2)
        self.h.db.commit()
        self.demand.snapshot(['other'], self.now)
        self.settle()
        value = self.demand.snapshot(['other'], self.now)
        self.assertEqual(value['pressure'], 0.25)
        self.assertIsNone(value['typicalPressure'])
        self.assertIsNone(value['ratio'])

    def test_recomputes_on_a_new_local_hour(self):
        first, later = self.now + 25 * 60, self.now + 31 * 60  # 14:55, then 15:01
        self.assertLess(later - first, TYPICAL_REFRESH_SECONDS)
        for t in (first, first + 60):
            with self.demand.lock:
                self.demand.typical_for('gemma', t)
            self.settle()
        self.assertEqual(self.demand.typical['gemma'][0], first)
        with self.demand.lock:
            self.demand.typical_for('gemma', later)
        self.settle()
        self.assertEqual(self.demand.typical['gemma'][0], later)

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
