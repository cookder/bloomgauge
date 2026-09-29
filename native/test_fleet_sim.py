"""A simulated ten-Mac fleet: mixed models, pairs, switching, stale, unreachable,
re-installed and duplicate Macs, checked against My Macs' combined totals."""

import unittest

import test_machines

GEMMA, QWEN, PAIR = 'gemma', 'EigenLabs/Qwen3.8-27B-4bit-mtp', ['Qwen3.5-9B', 'gpt-oss-20b']

# name -> how that simulated Mac reports once connected
FLEET = {
    '1': {'models': [GEMMA], 'ratePerHour': 0.15},
    '2': {'models': [GEMMA], 'ratePerHour': 0.10, 'chip': 'M2 Max', 'memoryGB': 96},
    # An older BloomGauge: no model count, per-model pace or strategy.
    '3': {'models': [QWEN], 'ratePerHour': 0.30, 'chip': 'M3 Ultra', 'memoryGB': 256, 'old': True},
    # A pair's pace follows the credits each model earned, not an even split.
    '4': {
        'models': PAIR,
        'ratePerHour': 0.08,
        'modelRates': [
            {'model': 'Qwen3.5-9B', 'ratePerHour': 0.06},
            {'model': 'gpt-oss-20b', 'ratePerHour': 0.02},
        ],
    },
    '5': {'models': [], 'ratePerHour': None, 'ready': False, 'switching': True},
    '6': {'models': [GEMMA], 'ratePerHour': 0.20, 'stale': True},
    '7': {'models': [GEMMA], 'ratePerHour': 0.20, 'unreachable': True},
    '8': {'models': [GEMMA], 'ratePerHour': 0.20, 'reinstalled': True},
    '9': {'models': [GEMMA], 'ratePerHour': 0.15, 'device': '1'},  # same physical Mac as 1
}


class FleetSimulation(unittest.TestCase):
    tearDown = test_machines.MachineTests.tearDown
    report = test_machines.MachineTests.report
    connect = test_machines.MachineTests.connect

    def settled(self, hours=24):
        # Reads run at most six at a time; keep polling until every Mac has answered.
        for _ in range(5):
            self.c.machines.snapshot(hours)
            for f in list(self.c.machines.pending.values()):
                f.result(timeout=2)
        return self.c.machines.snapshot(hours)

    def setUp(self):
        test_machines.MachineTests.setUp(self)
        self.connected = False
        self.c.pulse_demand.snapshot = lambda models, now: (
            {'load': 40, 'warm': 20, 'pressure': 2.0, 'typicalPressure': 1.0, 'ratio': 2.0}
            if models == [GEMMA]
            else None
        )

        def fetch(url, hours):
            name = url.split('//')[1][0]
            spec = FLEET[name]
            if self.connected and spec.get('unreachable'):
                raise OSError('asleep')
            installation = ('f' if self.connected and spec.get('reinstalled') else name) * 32
            device = (spec.get('device') or name) * 64
            extra = {
                k: spec[k]
                for k in ('models', 'ratePerHour', 'chip', 'memoryGB', 'ready', 'switching')
                if k in spec
            }
            if spec.get('stale') and self.connected:
                extra['at'] = self.now - 600
            rate = spec.get('ratePerHour')
            extra['modelCount'] = len(spec['models'])
            extra['modelRates'] = spec.get('modelRates') or (
                [{'model': m, 'ratePerHour': rate} for m in spec['models']]
                if rate is not None
                else None
            )
            r = self.report(hours, installation=installation, device=device, **extra)
            if spec.get('old'):
                for key in ('modelCount', 'modelRates', 'strategy'):
                    del r[key]
            return r

        self.c.machines.fetcher = fetch
        for name in FLEET:
            self.connect(f'Mac {name}', f'https://{name}.test.ts.net:8443')
        self.connected = True

    def test_every_mac_gets_the_right_status(self):
        r = self.settled()
        self.assertEqual(len(r['machines']), 10)
        status = {m['name']: m['status'] for m in r['machines']}
        self.assertEqual(status['Mac 6'], 'stale')
        self.assertEqual(status['Mac 7'], 'unreachable')
        self.assertEqual(status['Mac 8'], 'identity-changed')
        self.assertEqual(status['Mac 9'], 'duplicate-device')
        self.assertTrue(all(status[f'Mac {n}'] == 'connected' for n in '12345'))

    def test_combined_pace_counts_each_live_mac_once(self):
        r = self.settled()
        # This Mac (.15) plus Macs 1-4; the switching Mac has no pace; stale, unreachable,
        # re-installed and duplicate Macs are left out.
        self.assertAlmostEqual(r['ratePerHour'], 0.15 + 0.15 + 0.10 + 0.30 + 0.08)
        self.assertEqual(r['rateMacs'], 5)
        self.assertAlmostEqual(sum(m['ratePerHour'] or 0 for m in r['models']), r['ratePerHour'])

    def test_model_rollup_splits_pairs_by_credits_and_attaches_demand(self):
        r = self.settled()
        models = {m['model']: m for m in r['models']}
        self.assertEqual(r['models'][0]['model'], GEMMA)
        # This Mac, 1, 2 and the stale 6 serve gemma; only live Macs add pace.
        self.assertEqual(len(models[GEMMA]['macs']), 4)
        self.assertAlmostEqual(models[GEMMA]['ratePerHour'], 0.15 + 0.15 + 0.10)
        self.assertEqual(models[GEMMA]['demand']['ratio'], 2.0)
        self.assertAlmostEqual(models['Qwen3.5-9B']['ratePerHour'], 0.06)
        self.assertAlmostEqual(models['gpt-oss-20b']['ratePerHour'], 0.02)
        # The older single-model Mac still adds its whole pace.
        self.assertAlmostEqual(models[QWEN]['ratePerHour'], 0.30)
        self.assertIsNone(models[QWEN]['demand'])

    def test_known_inference_only_counts_included_macs(self):
        r = self.settled()
        included = [m['name'] for m in r['machines'] if m['included']]
        self.assertEqual(
            sorted(included), ['Mac 1', 'Mac 2', 'Mac 3', 'Mac 4', 'Mac 5', 'This Mac']
        )
        self.assertAlmostEqual(r['knownInferenceUsd'], 0.1 * len(included))
        self.assertFalse(r['complete'])

    def test_a_demand_lookup_failure_never_breaks_the_view(self):
        def broken(models, now):
            raise RuntimeError('capacity unavailable')

        self.c.pulse_demand.snapshot = broken
        r = self.settled()
        self.assertTrue(all(m['demand'] is None for m in r['models']))


if __name__ == '__main__':
    unittest.main()
