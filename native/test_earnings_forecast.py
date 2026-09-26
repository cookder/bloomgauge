import contextlib
import copy
import concurrent.futures
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from earnings_forecast import build, complete_hours, presentation, LOOKBACK_SECONDS
import earnings_outlook

NOW = 20 * 86400 + 3600
CUTOFF = NOW - 120
SIGNAL = {'current': True, 'observedAt': NOW - 10, 'coverage': 1.0, 'status': 'normal'}


def minute(at, usd=0.001, seconds=60):
    return {
        'at': at,
        'seconds': seconds,
        'usd': usd,
        'paidJobs': 1,
        'requests': 1,
        'tokens': 10,
        'busy': 1,
    }


def recent(usd=0.001):
    return [minute(at, usd) for at in range(CUTOFF - 900, CUTOFF, 60)]


def history(usd=0.001):
    return [minute(NOW - day * 86400 + i * 60, usd) for day in (1, 2, 3, 4) for i in range(60)]


class ForecastTests(unittest.TestCase):
    def test_exact_recent_paid_window_predicts_one_fixed_hour(self):
        r = build({'minutes': recent()}, SIGNAL, NOW)
        self.assertEqual(r['state'], 'estimate')
        self.assertEqual(r['basis'], 'recent_paid_persistence')
        self.assertAlmostEqual(r['usd'], 0.06)
        self.assertEqual(r['usd'], r['usdPerHour'])
        self.assertEqual(r['targetEnd'], NOW + 3600)
        self.assertLessEqual(r['paidEvidenceThrough'], NOW - 120)
        self.assertFalse(r['validation']['prospectiveValidated'])

    def test_observed_idle_is_zero_but_missing_or_partial_is_unknown(self):
        self.assertEqual(build({'minutes': recent(0)}, SIGNAL, NOW)['usd'], 0)
        for rows in [recent()[:-1], [*recent()[:-1], minute(CUTOFF - 60, 0.001, 59.99)]]:
            r = build({'minutes': rows}, SIGNAL, NOW)
            self.assertIsNone(r['usd'])
            self.assertIn('incomplete_recent_paid_window', r['reasonCodes'])

    def test_signed_credit_adjustments_are_not_clamped(self):
        r = build({'minutes': recent(-0.001)}, SIGNAL, NOW)
        self.assertAlmostEqual(r['usd'], -0.06)

    def test_duplicate_minutes_do_not_manufacture_coverage(self):
        r = build({'minutes': recent() + recent()[:1]}, SIGNAL, NOW)
        self.assertIsNone(r['usd'])
        self.assertEqual(complete_hours({'minutes': history()[:60] + history()[:1]}, NOW), [])

    def test_unsettled_future_and_outside_lookback_inputs_cannot_leak(self):
        base = build({'minutes': recent()}, SIGNAL, NOW)
        noise = [
            minute(CUTOFF, 999),
            minute(NOW + 3600, 999),
            minute(NOW - LOOKBACK_SECONDS - 60, 999),
        ]
        r = build({'minutes': recent() + noise}, SIGNAL, NOW)
        self.assertEqual(r['usd'], base['usd'])
        self.assertEqual(r['support'], base['support'])

    def test_other_or_stale_current_model_cannot_use_recent_persistence(self):
        for signal in [
            {**SIGNAL, 'current': False},
            {**SIGNAL, 'observedAt': NOW - 90},
            {**SIGNAL, 'coverage': 0.79},
            {**SIGNAL, 'observedAt': NOW + 1},
        ]:
            self.assertIsNone(build({'minutes': recent()}, signal, NOW)['usd'])

    def test_history_uses_whole_hours_and_explicit_unadjusted_basis(self):
        r = build({'minutes': history() + [minute(CUTOFF - 60, 999)]}, {'current': False}, NOW)
        self.assertEqual(r['basis'], 'completed_hour_history')
        self.assertAlmostEqual(r['usd'], 0.06)
        self.assertEqual(r['support']['completedHours'], 4)
        self.assertFalse(r['support']['demandAdjusted'])
        self.assertEqual(r['uncertainty']['kind'], 'descriptive_historical_spread')
        self.assertIsNone(r['uncertainty']['nominalCoverage'])

    def test_incomplete_hours_are_not_scaled_to_full_hour(self):
        r = build({'minutes': history()[:-1]}, {}, NOW)
        self.assertIsNone(r['usd'])
        self.assertEqual(r['support']['completedHours'], 3)
        self.assertAlmostEqual(r['historicalFallback']['usdPerWarmHour'], 0.06)

    def test_many_hours_one_date_are_not_repeated_dates(self):
        rows = [minute(NOW - 86400 + i * 60) for i in range(240)]
        r = build({'minutes': rows}, {}, NOW)
        self.assertIsNone(r['usd'])
        self.assertIn('insufficient_dates', r['reasonCodes'])

    def test_old_history_not_relabelled_current_forecast(self):
        rows = [{**m, 'at': m['at'] - 8 * 86400} for m in history()]
        r = build({'minutes': rows}, {}, NOW)
        self.assertIsNone(r['usd'])
        self.assertIsNotNone(r['historicalFallback']['usdPerWarmHour'])
        self.assertIn('old_paid_history', r['reasonCodes'])

    def test_held_and_expired_preserve_original_target(self):
        packet = build({'minutes': recent()}, SIGNAL, NOW)
        held = presentation(packet, NOW + 600)
        self.assertEqual(held['state'], 'held')
        self.assertEqual(held['targetEnd'], NOW + 3600)
        expired = presentation(packet, NOW + 3600)
        self.assertEqual(expired['state'], 'expired')
        self.assertIsNone(expired['usd'])
        self.assertEqual(packet['state'], 'estimate')

    def test_report_clock_cache_does_not_patch_shared_optimizer_functions(self):
        import demand_baselines

        original = demand_baselines.conditional
        original_datetime = demand_baselines.datetime
        calculations = earnings_outlook._report_calculations()
        self.assertIs(demand_baselines.conditional, original)
        self.assertIs(demand_baselines.datetime, original_datetime)
        self.assertEqual(calculations.conditional({}, {}, {}, NOW), original({}, {}, {}, NOW))
        calculations.clear()


class Store:
    pass


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.evidence = Mock(return_value={'a': {'minutes': recent()}})

        @contextlib.contextmanager
        def view(store):
            yield SimpleNamespace(evidence=self.evidence)

        self.view_patch = patch.object(earnings_outlook, 'read_view', view)
        self.view_patch.start()
        self.paid_patch = patch.object(
            earnings_outlook,
            'read_paid_evidence',
            side_effect=lambda view, *args: view.evidence(*args),
        )
        self.paid_patch.start()
        self.hist = Mock(side_effect=self.history)
        self.hist_patch = patch.object(earnings_outlook, '_historical_report', self.hist)
        self.hist_patch.start()

    def tearDown(self):
        self.paid_patch.stop()
        self.hist_patch.stop()
        self.view_patch.stop()

    def history(self, store, account, device, start, end, now, signals):
        return {
            'at': now,
            'from': start,
            'to': min(end, now),
            'models': [{'model': 'a', 'serving': True, 'eligible': True}],
        }

    def get(self, now=NOW, days=7, account='owner', signals=None):
        return earnings_outlook.report(
            self.store,
            account,
            'mac',
            now - days * 86400,
            now,
            now,
            signals if signals is not None else {'a': SIGNAL},
        )

    def test_forecast_lookback_is_independent_of_historical_range(self):
        a = self.get(days=7)
        b = self.get(days=14)
        self.assertEqual(a['models'][0]['forecast'], b['models'][0]['forecast'])
        self.assertEqual(self.evidence.call_count, 1)
        self.assertEqual(self.evidence.call_args.args[2], max(0, NOW - LOOKBACK_SECONDS))
        self.assertEqual(self.hist.call_count, 2)

    def test_polling_cache_keeps_original_bounds_and_fixed_forecast(self):
        a = self.get()
        b = self.get(NOW + 30)
        self.assertEqual(self.hist.call_count, 1)
        self.assertEqual(self.evidence.call_count, 1)
        self.assertEqual(a['from'], b['from'])
        self.assertEqual(a['forecastAt'], b['forecastAt'])
        self.assertEqual(b['reportStatus'], 'cached')
        self.get(NOW + 121)
        self.assertEqual(self.hist.call_count, 2)
        self.assertEqual(self.evidence.call_count, 1)

    def test_cached_output_mutation_does_not_corrupt_packet(self):
        a = self.get()
        a['models'][0]['forecast']['usd'] = 999
        b = self.get(NOW + 10)
        self.assertAlmostEqual(b['models'][0]['forecast']['usd'], 0.06)

    def test_account_and_device_scope_are_not_reused(self):
        self.get()
        self.get(account='other')
        self.assertEqual(self.evidence.call_count, 2)
        self.assertEqual(self.hist.call_count, 2)

    def test_read_failure_holds_existing_packet_without_extending_end(self):
        a = self.get()
        self.hist.side_effect = sqlite3.OperationalError('fixture')
        self.evidence.side_effect = sqlite3.OperationalError('fixture')
        b = self.get(NOW + 301)
        self.assertEqual(b['reportStatus'], 'held')
        self.assertEqual(b['models'][0]['forecast']['state'], 'held')
        self.assertEqual(
            b['models'][0]['forecast']['targetEnd'], a['models'][0]['forecast']['targetEnd']
        )
        c = self.get(NOW + 3600)
        self.assertEqual(c['models'][0]['forecast']['state'], 'expired')
        self.assertIsNone(c['models'][0]['forecast']['usd'])

    def test_first_read_failure_is_not_a_zero_report(self):
        self.hist.side_effect = sqlite3.OperationalError('fixture')
        with self.assertRaises(sqlite3.OperationalError):
            self.get()

    def test_concurrent_requests_reuse_one_complete_computation(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.get(), range(8)))
        self.assertEqual(self.hist.call_count, 1)
        self.assertEqual(self.evidence.call_count, 1)
        self.assertEqual(len({r['forecastAt'] for r in results}), 1)

    def test_roster_and_serving_flags_reflect_current_signals(self):
        self.get()
        b = self.get(NOW + 30, signals={'a': {**SIGNAL, 'current': False}})
        self.assertFalse(b['models'][0]['serving'])
        self.assertIn('forecast', b['models'][0])


if __name__ == '__main__':
    unittest.main()
