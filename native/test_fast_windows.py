"""Real calendar-minute fixtures; no live services, writes, or provider commands."""

import unittest
from demand_targets import target_status
from demand_optimizer import estimate, policy

NOW = 1790096400.0


def evidence(missing=(), first_rate=None):
    end = NOW - 120
    rows = [{'at': NOW - 3600 + i * 60, 'seconds': 60, 'usd': 0.03 / 60} for i in range(58)]
    rows = [r for r in rows if r['at'] not in {end - offset for offset in missing}]
    if first_rate is not None:
        for r in rows:
            if r['at'] == end - 300:
                r['usd'] = first_rate / 60
    return {'minutes': rows, 'hours': len(rows) / 60, 'jobs': 100, 'days': 1}


class FastWindowTests(unittest.TestCase):
    def test_one_missing_minute_retains_four_known_minutes_for_all_offsets(self):
        e = evidence((120,))
        for offset in range(60):
            g = target_status(e, NOW - 7200, NOW + offset, policy())
            with self.subTest(offset=offset):
                self.assertAlmostEqual(g['fastRate'], 0.03)

    def test_same_five_completed_minutes_have_same_rate_between_boundaries(self):
        e = evidence(first_rate=0.18)
        a = target_status(e, NOW - 7200, NOW, policy())['fastRate']
        for offset in (1, 15, 30, 59):
            self.assertAlmostEqual(
                target_status(e, NOW - 7200, NOW + offset, policy())['fastRate'], a
            )

    def test_estimate_protects_recovered_pay_with_one_missing_minute(self):
        e = evidence((120,))
        for offset in (0, 1, 15, 30, 59):
            with self.subTest(offset=offset):
                self.assertAlmostEqual(
                    estimate(e, {}, {}, NOW + offset, NOW - 7200).get('recentGuardRate'), 0.03
                )

    def test_two_missing_minutes_remain_unknown(self):
        e = evidence((120, 180))
        for offset in (0, 15, 59):
            self.assertIsNone(target_status(e, NOW - 7200, NOW + offset, policy())['fastRate'])

    def test_unsettled_future_money_is_excluded(self):
        e = evidence()
        e['minutes'] += [{'at': NOW - 60, 'seconds': 60, 'usd': 100}]
        self.assertAlmostEqual(target_status(e, NOW - 7200, NOW + 30, policy())['fastRate'], 0.03)


if __name__ == '__main__':
    unittest.main()
