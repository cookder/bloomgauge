"""Pass B: trials are judged against what the incumbent normally earns, on pay after first work."""

import unittest

from history import History
from optimizer_store import OptimizerStore, CONTINUITY_SECONDS
from trial_economics import comparison, ordinary_review
from demand_optimizer import sustained_demand, LEARNING_PRESSURE, LEARNING_MIN_LOAD
from test_trial_economics import NOW, REGIME, goal, ordinary_case
from test_demand_optimizer import summary


class BenchmarkTests(unittest.TestCase):
    def anchor(self, pace, expected=None):
        return comparison(
            'incumbent', goal(pace), summary(pace), NOW, REGIME, 'earnings_target', 20, expected
        )

    def test_a_dip_does_not_lower_the_bar_below_normal_pay_at_this_demand(self):
        dip = self.anchor(0.0, expected=0.10)['comparison']
        self.assertAlmostEqual(dip['lower'], 0.095)
        self.assertEqual(dip['expectedRate'], 0.10)
        self.assertAlmostEqual(self.anchor(0.0)['comparison']['lower'], 0)

    def test_a_strong_current_pace_still_sets_the_bar(self):
        strong = self.anchor(0.15, expected=0.10)['comparison']
        self.assertAlmostEqual(strong['lower'], 0.1425)
        self.assertAlmostEqual(strong['upper'], 0.1575)

    def test_invalid_expectations_are_ignored(self):
        for bad in (None, -1, float('nan'), True):
            self.assertIsNone(self.anchor(0.05, expected=bad)['comparison']['expectedRate'])


class SteadyRateTests(unittest.TestCase):
    def test_keep_uses_pay_after_first_work_when_known(self):
        run, trial = ordinary_case(0.06)
        trial['clock']['usdPerHour'] = 0.03  # includes loading and the wait for traffic
        self.assertNotEqual(
            ordinary_review(run, trial, goal(0.07), {'fresh': True}, NOW)['status'], 'keep'
        )
        trial.update(steadyUsdPerHour=0.07, rampSeconds=240)
        result = ordinary_review(run, trial, goal(0.07), {'fresh': True}, NOW)
        self.assertEqual(result['status'], 'keep')
        self.assertEqual(result['rateBasis'], 'settled_inference_per_warm_hour_after_first_work')
        self.assertEqual(result['rampSeconds'], 240)


class ContinuityTests(unittest.TestCase):
    def test_late_readings_within_the_window_count_as_warm_time(self):
        h = History(':memory:')
        store = OptimizerStore(h)
        store.sample('a', 'd', 1_000_020, 1_000_020 + 30, 'm', 1, 10, False, True)
        store.sample(
            'a', 'd', 1_000_050, 1_000_050 + CONTINUITY_SECONDS + 1, 'm', 1, 10, False, True
        )
        seconds = h.db.execute('SELECT SUM(seconds) FROM opt_ready_minutes').fetchone()[0]
        self.assertEqual(seconds, 30)


class LearningDemandTests(unittest.TestCase):
    def samples(self, load, warm):
        cutoff = int(NOW // 30) * 30
        return [
            {'at': cutoff - 600 + i * 30, 'model': 'm', 'active': load, 'queued': 0, 'warm': warm}
            for i in range(20)
        ]

    def test_a_barely_used_model_is_not_worth_a_learning_run(self):
        # Sep 25: bonsai had ~5 requests across ~39 warm providers when a learning run started.
        self.assertFalse(
            sustained_demand(
                self.samples(5, 39),
                NOW,
                LEARNING_PRESSURE,
                LEARNING_MIN_LOAD,
                LEARNING_PRESSURE / 2,
            )['qualified']
        )
        self.assertFalse(
            sustained_demand(
                self.samples(8, 10),
                NOW,
                LEARNING_PRESSURE,
                LEARNING_MIN_LOAD,
                LEARNING_PRESSURE / 2,
            )['qualified']
        )

    def test_a_model_with_real_demand_can_be_measured(self):
        self.assertTrue(
            sustained_demand(
                self.samples(57, 90),
                NOW,
                LEARNING_PRESSURE,
                LEARNING_MIN_LOAD,
                LEARNING_PRESSURE / 2,
            )['qualified']
        )


if __name__ == '__main__':
    unittest.main()
