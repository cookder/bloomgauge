"""Customer Outlook integration with temporary history and synthetic evidence only."""

import copy
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from collector import Collector
from earnings_outlook import report
from earnings_forecast import REFRESH_SECONDS
from test_earnings_forecast import NOW, SIGNAL, recent, minute


class CustomerForecastJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = NOW
        self.evidence = {'a': {'minutes': recent()}}
        self.edition_patch = patch('collector.personal_edition', return_value=False)
        self.edition_patch.start()
        self.paid_patch = patch(
            'earnings_forecast_journal.read_paid_evidence', side_effect=lambda *args: self.evidence
        )
        self.paid_patch.start()
        self.collectors = []

    def collector(self, name='customer', preview=False):
        home = self.root / name / 'empty-home'
        home.mkdir(parents=True, exist_ok=True)
        options = {'forecast_enabled': False} if preview else {}
        c = Collector(
            home=home,
            data_path=home.parent / 'history.sqlite',
            usage_network_enabled=False,
            discovery_enabled=False,
            support_network_enabled=False,
            **options,
        )
        c.optimizer.runner = Mock(side_effect=AssertionError('No provider or permission command'))
        self.collectors.append(c)
        return c

    def dispose(self, c):
        c.optimizer.runner.assert_not_called()
        c.close()
        c.history.close()
        self.collectors.remove(c)

    def tearDown(self):
        for c in self.collectors[:]:
            self.dispose(c)
        self.paid_patch.stop()
        self.edition_patch.stop()
        self.temp.cleanup()

    def record(self, c, account='owner', device='mac', now=NOW):
        c.earnings_forecast.record(account, device, now, {'a': SIGNAL})

    def test_customer_default_persists_immutable_forecast_across_reopen(self):
        c = self.collector()
        self.assertTrue(c.earnings_forecast.enabled)
        self.assertFalse(c.predictive_lab.enabled)
        self.record(c)
        packet = c.earnings_forecast.latest('owner', 'mac', NOW)
        self.assertIsNotNone(packet)
        self.dispose(c)
        c = self.collector()
        self.evidence = {'a': {'minutes': recent(99)}}
        self.record(c, now=NOW + 30)
        self.assertEqual(c.earnings_forecast.latest('owner', 'mac', NOW + 30), packet)
        self.assertEqual(
            c.earnings_forecast.evaluation('owner', 'mac', NOW + 30)['recordedPackets'], 1
        )

    def test_customer_account_device_and_future_scope(self):
        c = self.collector()
        self.record(c)
        for account, device in [('other', 'mac'), ('owner', 'other')]:
            self.assertIsNone(c.earnings_forecast.latest(account, device, NOW))
            result = c.earnings_forecast.evaluation(account, device, NOW + 3720)
            self.assertEqual(result['recordedPackets'], 0)
            self.assertEqual(result['models'], [])
        self.assertIsNone(c.earnings_forecast.latest('owner', 'mac', NOW - 1))

    def test_preview_creates_no_journal_and_cannot_read_normal_history(self):
        normal = self.collector()
        self.record(normal)
        preview = self.collector('setup-preview', preview=True)
        self.assertFalse(preview.earnings_forecast.enabled)
        self.assertFalse(preview.predictive_lab.enabled)
        self.record(preview)
        self.assertIsNone(preview.earnings_forecast.latest('owner', 'mac', NOW))
        self.assertEqual(preview.threads, [])
        self.assertIsNone(
            preview.history.db.execute(
                "SELECT name FROM sqlite_master WHERE name='earnings_forecast_observations'"
            ).fetchone()
        )

    def test_report_truthfully_transitions_from_waiting_to_persisted(self):
        c = self.collector()
        before = report(c.optimizer.store, 'owner', 'mac', 0, NOW, NOW, {'a': SIGNAL})
        self.assertEqual(before['forecastPersistence'], 'awaiting_collector')
        self.record(c)
        after = report(c.optimizer.store, 'owner', 'mac', 0, NOW, NOW, {'a': SIGNAL})
        self.assertEqual(after['forecastPersistence'], 'versioned_local_journal')
        # Evaluation retains the existing bounded refresh cadence.
        after = report(
            c.optimizer.store,
            'owner',
            'mac',
            0,
            NOW + REFRESH_SECONDS,
            NOW + REFRESH_SECONDS,
            {'a': SIGNAL},
        )
        self.assertEqual(after['forecastEvaluation']['recordedPackets'], 1)
        self.assertEqual(after['forecastEvaluation']['models'][0]['pendingWindows'], 1)

    def test_preview_entrypoint_disables_journal_network_and_background_workers(self):
        import collector as module

        args = SimpleNamespace(
            data=self.root / 'preview/history.sqlite',
            static=self.root / 'web',
            port=0,
            remote_port=0,
            setup_preview=True,
            dev=False,
        )
        fields = ('collector', 'static_root', 'dev', 'setup_preview', 'remote')
        previous = {name: getattr(module.Handler, name) for name in fields}
        try:
            with (
                patch.object(module, 'parse_args', return_value=args),
                patch.object(module, 'Collector') as factory,
                patch.object(module, 'ThreadingHTTPServer') as servers,
                patch.object(module, 'Remote') as remote,
                patch.object(module.threading, 'Thread'),
                patch.object(module.signal, 'signal'),
                patch('sys.stdout', new_callable=io.StringIO),
            ):
                servers.return_value.server_port = 12345
                module.main()
                options = factory.call_args.kwargs
                self.assertEqual(options['home'], args.data.parent / 'empty-home')
                for key in (
                    'forecast_enabled',
                    'usage_network_enabled',
                    'discovery_enabled',
                    'support_network_enabled',
                ):
                    self.assertIs(options[key], False)
                factory.return_value.start.assert_not_called()
                factory.return_value.collect.assert_called_once()
                remote.return_value.start.assert_not_called()
        finally:
            for name, value in previous.items():
                setattr(module.Handler, name, value)

    def test_journal_never_changes_controls_or_saved_settings(self):
        c = self.collector()
        controls_before = copy.deepcopy(c.optimizer.state)
        cache_before = list(
            c.history.db.execute('SELECT key,data FROM cache ORDER BY key').fetchall()
        )
        self.record(c)
        c.earnings_forecast.latest('owner', 'mac', NOW)
        c.earnings_forecast.evaluation('owner', 'mac', NOW)
        report(c.optimizer.store, 'owner', 'mac', 0, NOW, NOW, {'a': SIGNAL})
        self.assertEqual(c.optimizer.state, controls_before)
        self.assertEqual(
            list(c.history.db.execute('SELECT key,data FROM cache ORDER BY key').fetchall()),
            cache_before,
        )

    def test_real_customer_store_scores_covered_zero_and_censors_missing_minutes(self):
        c = self.collector()
        self.evidence = {'a': {'minutes': recent(0)}}
        self.record(c)
        self.evidence = {'a': {'minutes': [minute(t, 0) for t in range(NOW, NOW + 3600, 60)]}}
        result = c.earnings_forecast.evaluation('owner', 'mac', NOW + 3720)['models'][0]
        self.assertEqual(result['windows'], 1)
        self.assertEqual(result['maeUSDPerHour'], 0)
        c.earnings_forecast.score_cache.clear()
        self.evidence['a']['minutes'].pop()
        result = c.earnings_forecast.evaluation('owner', 'mac', NOW + 3720)['models'][0]
        self.assertEqual(result['windows'], 0)
        self.assertEqual(result['censoredWindows'], 1)
        self.assertIsNone(result['maeUSDPerHour'])


if __name__ == '__main__':
    unittest.main()
