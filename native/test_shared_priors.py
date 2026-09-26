"""Shared starting curves and opt-in pay summaries (optimizer redesign Phase 3)."""

import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock

import demand_curves as curves
import pay_sharing as sharing
import shared_priors
import test_support_reports as base
from test_demand_curves import NOW, history, signal

PRIOR = {'qwen-big': {'level': 0.30, 'exponent': 0.6, 'range': (0.7, 1.3)}}


class SharedPriorFileTests(unittest.TestCase):
    def test_bundled_file_parses(self):
        loaded = shared_priors.load()
        self.assertTrue(loaded)
        for curve in loaded.values():
            self.assertTrue(0 < curve['level'] < 5 and 0 <= curve['exponent'] <= 2)

    def test_malformed_entries_and_files_are_ignored(self):
        good = {'level': 0.1, 'exponent': 0.5, 'range': [0.8, 1.2]}
        data = {
            'schema': 1,
            'models': {
                'ok': good,
                'nan': {**good, 'level': float('nan')},
                'huge': {**good, 'level': 50},
                'bad-range': {**good, 'range': [2, 1]},
                '': good,
                'no-range': {**good, 'range': None},
            },
        }
        self.assertEqual(set(shared_priors.parse(data)), {'ok', 'no-range'})
        self.assertEqual(shared_priors.parse({'schema': 2, 'models': {'ok': good}}), {})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'p.json'
            path.write_text('{not json')
            self.assertEqual(shared_priors.load(path), {})


class HardwareMatchTests(unittest.TestCase):
    CURVE = {'level': 0.1, 'exponent': 0.5, 'range': [0.8, 1.2]}

    def test_own_class_curve_wins_and_other_hardware_is_flagged(self):
        data = {
            'schema': 1,
            'models': {
                'gemma': {**self.CURVE, 'hardware': ['M5 Pro|33-64', 'M1 Max|33-64']},
                'qwen': {**self.CURVE, 'hardware': ['M5 Pro|33-64']},
            },
            'byHardware': {'M1 Max|33-64': {'gemma': {**self.CURVE, 'level': 0.2}}},
        }
        m1 = shared_priors.parse(data, 'M1 Max|33-64')
        self.assertEqual((m1['gemma']['level'], m1['gemma']['sameHardware']), (0.2, True))
        self.assertFalse(m1['qwen']['sameHardware'])
        m3 = shared_priors.parse(data, 'M3 Ultra|over-128')
        self.assertEqual(m3['gemma']['level'], 0.1)
        self.assertFalse(m3['gemma']['sameHardware'])
        self.assertTrue(shared_priors.parse(data)['qwen']['sameHardware'])

    def test_other_hardware_widens_a_never_run_range(self):
        same = {'qwen-big': {**PRIOR['qwen-big'], 'sameHardware': True}}
        other = {'qwen-big': {**PRIOR['qwen-big'], 'sameHardware': False}}
        a = curves.build({}, {}, {'qwen-big': signal(2, 1)}, None, NOW, same)['qwen-big']
        b = curves.build({}, {}, {'qwen-big': signal(2, 1)}, None, NOW, other)['qwen-big']
        self.assertAlmostEqual(a['usdPerHour'], b['usdPerHour'])
        self.assertLess(b['lower'], a['lower'])
        self.assertGreater(b['upper'], a['upper'])

    def test_build_priors_merges_summaries_by_hardware(self):
        import build_priors

        base = {
            'gemma': {
                'level': 0.16,
                'exponent': 0.6,
                'range': [0.8, 1.2],
                'hours': 200,
                'periods': 400,
                'macs': 1,
                'hardware': ['M5 Pro|33-64'],
            }
        }
        other = [
            {
                'gemma': {
                    'level': 0.06,
                    'exponent': 0.6,
                    'range': [0.7, 1.3],
                    'hours': 50,
                    'periods': 100,
                    'macs': 1,
                    'hardware': ['M1 Max|33-64'],
                }
            }
        ]
        merged = build_priors.merge(base, other)['gemma']
        self.assertAlmostEqual(merged['level'], 0.16)  # warm-hour-weighted median
        self.assertEqual(
            (merged['macs'], merged['hardware']), (2, ['M1 Max|33-64', 'M5 Pro|33-64'])
        )
        classes = build_priors.by_hardware(base, other)
        self.assertEqual(classes['M1 Max|33-64']['gemma']['level'], 0.06)
        self.assertEqual(classes['M5 Pro|33-64']['gemma']['level'], 0.16)

    def test_opt_in_summaries_cannot_steer_or_invent_curves(self):
        import build_priors

        base = {
            'gemma': {
                'level': 0.16,
                'exponent': 0.6,
                'range': [0.8, 1.2],
                'hours': 30,
                'periods': 60,
                'macs': 1,
                'hardware': ['M5 Pro|33-64'],
            }
        }
        curve = lambda level, hours=150, **k: {
            'level': level,
            'exponent': 0.6,
            'range': None,
            'hours': hours,
            'periods': 10,
            'hardware': ['M1|up-to-16'],
            **k,
        }
        # Two senders claiming huge pay and 150 h each: capped at a day each, and the median holds.
        loud = [{'gemma': curve(4.0)}, {'gemma': curve(4.5)}, {'gemma': curve(0.15, 20)}]
        cleaned, dropped = build_priors.clean(
            loud + [{'gemma': curve(float('nan'))}, {'made-up': curve(0.2)}, {'gemma': curve(9.0)}],
            {'gemma'},
        )
        self.assertEqual(sorted(dropped), ['gemma', 'gemma', 'made-up'])
        self.assertTrue(all(c['hours'] <= 24 for s in cleaned for c in s.values()))
        self.assertLess(build_priors.merge(base, cleaned[:1] + cleaned[2:3])['gemma']['level'], 0.2)
        # A model only opt-ins report needs three Macs.
        solo = build_priors.clean([{'qwen': curve(0.3)}, {'qwen': curve(0.3)}], {'qwen'})[0]
        self.assertNotIn('qwen', build_priors.merge({}, solo))
        self.assertIn(
            'qwen',
            build_priors.merge({}, solo + build_priors.clean([{'qwen': curve(0.3)}], {'qwen'})[0]),
        )
        self.assertIn(
            'No model moved',
            build_priors.compare(
                {'gemma': {'level': 0.16, 'exponent': 0.6}},
                {'gemma': {'level': 0.17, 'exponent': 0.6}},
            ),
        )


class SharedCurveTests(unittest.TestCase):
    def test_new_mac_with_no_history_still_predicts_from_shared_curves(self):
        self.assertEqual(curves.build({}, {}, {'qwen-big': signal(2, 1)}, None, NOW), {})
        row = curves.build(
            {}, {}, {'qwen-big': signal(2, 1), 'other': signal(1, 1)}, None, NOW, PRIOR
        )
        self.assertEqual(row['qwen-big']['basis'], 'shared')
        # Not serving: this Mac would be one more warm provider, so pressure 2/(1+1).
        self.assertAlmostEqual(row['qwen-big']['usdPerHour'], 0.30 * (1.01**0.6), places=6)
        self.assertLess(row['qwen-big']['lower'], row['qwen-big']['usdPerHour'] * 0.7 * 0.85 + 1e-9)
        self.assertEqual(row['other']['basis'], 'prior')

    def test_shared_curve_pulls_a_thin_model_and_own_data_wins_with_hours(self):
        start = NOW - 6 * 86400
        busy, net = history([0.5, 1.0, 1.5], lambda p: 0.05 * p, start, days=6)
        thin, thin_net = history([1.0], lambda p: 0.05, start, days=1)
        evidence = {'gemma': busy, 'qwen-big': thin}
        network = {'gemma': net, 'qwen-big': thin_net}
        signals = {'gemma': signal(1, 1), 'qwen-big': signal(2, 1)}
        plain = curves.build(evidence, network, signals, 'gemma', NOW)['qwen-big']['usdPerHour']
        pulled = curves.build(evidence, network, signals, 'gemma', NOW, PRIOR)['qwen-big'][
            'usdPerHour'
        ]
        self.assertGreater(pulled, plain)
        lots, lots_net = history([1.0], lambda p: 0.05, start, days=6)
        evidence['qwen-big'], network['qwen-big'] = lots, lots_net
        heavy = curves.build(evidence, network, signals, 'gemma', NOW, PRIOR)['qwen-big'][
            'usdPerHour'
        ]
        self.assertLess(heavy, pulled)

    def test_summary_holds_only_the_curve(self):
        busy, net = history([0.5, 1.0, 1.5], lambda p: 0.05 * p, NOW - 6 * 86400, days=6)
        out = curves.summary(
            {'gemma': busy, 'thin': {'minutes': busy['minutes'][:30]}},
            {'gemma': net, 'thin': net},
            NOW,
        )
        self.assertEqual(set(out), {'gemma'})
        self.assertEqual(set(out['gemma']), {'level', 'exponent', 'range', 'hours', 'periods'})


class FakeHistory:
    def __init__(self):
        self.data = {}

    def cache(self, key, data=None):
        if data is not None:
            self.data[key] = json.loads(json.dumps(data))
            return data
        return self.data.get(key)


class PaySharingTests(unittest.TestCase):
    def setUp(self):
        self.clock = 1790000000
        self.calls = []
        self.code = {'POST': 200, 'DELETE': 204}
        usage = Mock(version='1.36.37')
        usage.metadata.return_value = {'chipFamily': 'M5 Pro', 'memoryBand': '33-64', 'osMajor': 26}
        self.collector = Mock(history=FakeHistory(), usage=usage)
        self.share = sharing.PaySharing(
            self.collector,
            transport=self.transport,
            now=lambda: self.clock,
            curves_source=lambda now: {
                'gemma': {
                    'level': 0.16,
                    'exponent': 0.6,
                    'range': [0.8, 1.2],
                    'hours': 200,
                    'periods': 400,
                }
            },
        )

    def transport(self, method, url, body, headers, timeout):
        self.calls.append((method, json.loads(body), headers['Authorization']))
        return self.code[method]

    def test_off_by_default_then_opt_in_sends_only_curves_and_hardware_class(self):
        self.assertFalse(self.share.status()['enabled'])
        self.share.tick()
        self.assertEqual(self.calls, [])
        self.assertTrue(self.share.action({'enabled': True})['enabled'])
        method, body, auth = self.calls[0]
        self.assertEqual(method, 'POST')
        self.assertEqual(
            set(body), {'schema', 'id', 'appVersion', 'chipFamily', 'memoryBand', 'models'}
        )
        self.assertEqual(
            (body['chipFamily'], body['memoryBand'], body['appVersion']),
            ('M5 Pro', '33-64', '1.36.37'),
        )
        self.assertNotIn('account', json.dumps(body))
        self.assertRegex(auth, r'^Bearer [0-9a-f]{64}$')

    def test_weekly_refresh_and_six_hour_retry(self):
        self.share.action({'enabled': True})
        self.clock += 6 * 86400
        self.share.tick()
        self.assertEqual(len(self.calls), 1)
        self.clock += 86400
        self.code['POST'] = 503
        self.share.tick()
        self.share.tick()
        self.assertEqual(len(self.calls), 2)
        self.clock += 6 * 3600
        self.code['POST'] = 200
        self.share.tick()
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.calls[2][1]['id'], self.calls[0][1]['id'])

    def test_turning_off_deletes_first_and_failures_change_nothing(self):
        self.code['POST'] = 503
        with self.assertRaises(sharing.SharingError):
            self.share.action({'enabled': True})
        self.assertFalse(self.share.status()['enabled'])
        self.code['POST'] = 200
        self.share.action({'enabled': True})
        self.code['DELETE'] = 500
        with self.assertRaises(sharing.SharingError):
            self.share.action({'enabled': False})
        self.assertTrue(self.share.status()['enabled'])
        self.code['DELETE'] = 204
        self.assertFalse(self.share.action({'enabled': False})['enabled'])
        self.assertEqual(self.calls[-1][1], {'schema': 1, 'id': self.calls[1][1]['id']})

    def test_phone_and_bad_input_are_refused(self):
        with self.assertRaises(sharing.SharingError):
            self.share.action({'enabled': True}, remote=True)
        for bad in ({'enabled': 'yes'}, {}, {'enabled': True, 'x': 1}):
            with self.assertRaises(ValueError):
                self.share.action(bad)
        self.assertEqual(self.calls, [])


class PaySharingHTTPTests(unittest.TestCase):
    setUp = base.SupportHTTPTests.setUp
    tearDown = base.SupportHTTPTests.tearDown

    def test_status_and_opt_in_over_http(self):
        self.collector.pay_sharing.network_enabled = True
        self.collector.pay_sharing.transport = lambda *a: 200
        url = f'http://127.0.0.1:{self.local.server_port}/api/pay-sharing'
        with urllib.request.urlopen(url, timeout=3) as r:
            self.assertFalse(json.load(r)['enabled'])
        request = urllib.request.Request(
            url,
            data=b'{"enabled":true}',
            headers={'Content-Type': 'application/json', 'X-Bloom-Action': 'pay-sharing'},
        )
        with urllib.request.urlopen(request, timeout=10) as r:
            self.assertTrue(json.load(r)['enabled'])
        bad = urllib.request.Request(
            url, data=b'{"enabled":true}', headers={'Content-Type': 'application/json'}
        )
        with self.assertRaises(urllib.error.HTTPError) as result:
            urllib.request.urlopen(bad, timeout=3)
        self.assertEqual(result.exception.code, 403)


if __name__ == '__main__':
    unittest.main()
