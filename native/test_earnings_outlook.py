import json
import unittest
from unittest.mock import patch
from test_demand_baselines import PairingTests, sample, evidence, network, NOW, SIGNAL
from earnings_outlook import report, band_evidence
from demand_baselines import conditional
import test_remote


class OutlookPairingTests(PairingTests):
    def test_all_models_keep_their_own_paid_yield_and_bands(self):
        for model, amount in [('a', 2500), ('b', 500)]:
            self.warm(model=model)
            self.net(self.at + 5, model=model)
            self.net(self.at + 35, model=model)
            self.paid(model=model, amount=amount)
        r = report(
            self.s,
            'owner',
            'mac',
            0,
            NOW,
            NOW,
            {'a': {**SIGNAL, 'current': True}, 'b': SIGNAL, 'new': SIGNAL},
        )
        rows = {m['model']: m for m in r['models']}
        self.assertAlmostEqual(rows['a']['bands'][4]['usdPerHour'], 0.15)
        self.assertAlmostEqual(rows['b']['bands'][4]['usdPerHour'], 0.03)
        self.assertEqual(rows['new']['current']['hours'], 0)
        self.assertIsNone(rows['new']['bands'][4]['usdPerHour'])
        self.assertTrue(rows['a']['serving'])
        self.assertFalse(rows['b']['serving'])
        e = self.s.evidence('owner', 'mac', 0, NOW, NOW)
        self.assertEqual(
            rows['a']['current'],
            conditional(
                e['a'], network([sample(self.at, usd=0.0025)]), {**SIGNAL, 'current': True}, NOW
            ),
        )

    def test_report_has_no_writes_and_scopes_identity(self):
        self.warm()
        self.net(self.at + 5)
        self.net(self.at + 35)
        self.paid()
        n = self.h.db.total_changes
        r = report(self.s, 'other', 'mac', 0, NOW, NOW, {'a': SIGNAL})
        self.assertEqual(r['models'][0]['totalWarmHours'], 0)
        self.assertIsNone(r['models'][0]['current']['usdPerHour'])
        self.assertEqual(n, self.h.db.total_changes)
        for start, end in ((-1, NOW), (0, float('nan')), (NOW, NOW + 100), (NOW + 100, NOW + 200)):
            with self.assertRaises(ValueError):
                report(self.s, 'owner', 'mac', start, end, NOW)


class BandEvidenceTests(unittest.TestCase):
    def many(self):
        return [
            sample(NOW - day * 86400 - 2 * 3600 + i * 60, usd=0.15 / 60, pressure=0.75)
            for day in (1, 2, 3)
            for i in range(120)
        ]

    def get(self, rows, low=0.5, high=1):
        return band_evidence(evidence(rows), rows, low, high, NOW)

    def test_same_model_quiet_history_does_not_dilute_busy_band(self):
        busy = self.many()
        quiet = [sample(m['at'] - 8 * 3600, usd=0, pressure=0.1) for m in busy]
        r = self.get(busy + quiet)
        self.assertEqual(r['quality'], 'repeated')
        self.assertAlmostEqual(r['usdPerHour'], 0.15)
        self.assertAlmostEqual(r['hours'], 6)
        self.assertEqual(self.get(busy + quiet, 0, 0.25)['usdPerHour'], 0)

    def test_boundary_and_missing_bands_do_not_invent_zero(self):
        rows = [sample(NOW - 600, pressure=0.5)]
        self.assertIsNone(self.get(rows, 0.25, 0.5)['usdPerHour'])
        self.assertEqual(self.get(rows)['quality'], 'limited')
        self.assertEqual(self.get([], 8, None)['quality'], 'none')

    def test_thin_jackpot_cannot_inflate_supported_band(self):
        r = self.get(self.many() + [sample(NOW - 1000, usd=100, pressure=0.75)])
        self.assertEqual(r['quality'], 'repeated')
        self.assertAlmostEqual(r['usdPerHour'], 0.15)
        self.assertAlmostEqual(r['excludedThinHours'], 1 / 60)

    def test_many_minutes_on_one_date_or_old_runs_are_not_repeated_current_evidence(self):
        rows = [sample(NOW - 36000 + i * 60, pressure=0.75) for i in range(300)]
        self.assertEqual(self.get(rows)['quality'], 'limited')
        old = [{**m, 'at': m['at'] - 20 * 86400} for m in self.many()]
        self.assertEqual(self.get(old)['quality'], 'older')

    def test_network_holes_cannot_manufacture_supported_periods(self):
        rows = self.many()
        joined = [m for i, m in enumerate(rows) if i % 2]
        r = band_evidence(evidence(rows), joined, 0.5, 1, NOW)
        self.assertEqual(r['quality'], 'limited')
        self.assertEqual(r['blocks'], 0)

    def test_signed_adjustments_survive(self):
        rows = [sample(NOW - 600, usd=-0.01, pressure=0.75)]
        self.assertAlmostEqual(self.get(rows)['usdPerHour'], -0.6)


class OutlookHTTPTests(test_remote.RemoteHTTPBase):
    def test_report_uses_current_account_and_phone_owner(self):
        c = self.local.RequestHandlerClass.collector
        c.account = 'current'
        c.optimizer.live = {'account': 'current', 'device': 'mac'}
        with patch('earnings_outlook.report', return_value={'scope': 'fixture'}) as build:
            for server, headers in ((self.local, {}), (self.phone, self.headers)):
                with self.read(
                    server,
                    '/api/optimizer/earnings-outlook?account=foreign&device=foreign',
                    headers,
                ) as r:
                    self.assertEqual(json.load(r)['scope'], 'fixture')
                self.assertEqual(build.call_args.args[1:3], ('current', 'mac'))
            c.optimizer.live = {'account': 'previous', 'device': 'mac'}
            with self.read(self.local, '/api/optimizer/earnings-outlook') as r:
                r.read()
            self.assertEqual(build.call_args.args[1:3], ('', ''))
        self.denied(self.phone, '/api/optimizer/earnings-outlook', {})
        self.denied(
            self.phone,
            '/api/optimizer/earnings-outlook',
            {**self.headers, 'Origin': 'https://foreign.example'},
        )


if __name__ == '__main__':
    unittest.main()
