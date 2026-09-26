"""Consent invitation tests. Fresh disposable data and injected transport only."""

import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from collector import Collector
from usage_integration import INVITATION_KEY, UsageIntegration


class UsageInvitationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.path = self.home / 'history.sqlite3'
        self.c = Collector(home=self.home, data_path=self.path)
        self.u = self.c.usage
        self.transport = []
        self.u.reporter._transport = lambda *args: self.transport.append(args) or 200
        self.c.history.cache('setup-v1', {'completed': True, 'completedAt': time.time() - 3600})

    def tearDown(self):
        worker = self.u.reporter._worker
        if worker:
            worker.join(timeout=2)
        self.c.close()
        self.c.history.close()
        self.temp.cleanup()

    def offer(self):
        return self.u.action({'action': 'offer-invitation'})

    def test_read_is_passive_and_never_creates_reporting_credentials(self):
        self.assertTrue(self.u.status()['invitationEligible'])
        self.assertFalse(self.u.status()['invitationOffered'])
        self.assertIsNone(self.c.history.cache(INVITATION_KEY))
        self.assertFalse(self.u.invitation_path.exists())
        self.assertEqual(self.transport, [])

    def test_offer_is_once_per_installation_even_after_relaunch(self):
        self.assertTrue(self.offer()['invitationOffered'])
        self.assertFalse(self.offer()['invitationOffered'])
        self.u.close()
        self.u = self.c.usage = UsageIntegration(self.c, self.path)
        self.u.reporter._transport = lambda *args: self.fail('Unexpected hosted request')
        self.assertFalse(self.u.status()['invitationEligible'])
        self.assertFalse(self.offer()['invitationOffered'])

    def test_two_windows_cannot_both_claim_invitation(self):
        results = []
        threads = [
            threading.Thread(target=lambda: results.append(self.offer()['invitationOffered']))
            for _ in range(2)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=2)
        self.assertEqual(sorted(results), [False, True])

    def test_decline_stays_local_and_does_not_change_features(self):
        optimizer = copy.deepcopy(self.c.optimizer.state)
        self.offer()
        self.u.action({'action': 'dismiss-invitation'})
        self.assertEqual(self.c.history.cache(INVITATION_KEY)['state'], 'declined')
        self.assertFalse(self.u.status()['enabled'])
        self.assertFalse(self.u.invitation_path.exists())
        self.assertEqual(self.transport, [])
        self.assertEqual(self.c.optimizer.state, optimizer)

    def test_no_public_traffic_until_explicit_share_and_no_new_payload_fields(self):
        self.offer()
        self.u.observe()
        self.assertEqual(self.transport, [])
        self.u.action({'action': 'consent', 'enabled': True})
        self.u.reporter._worker.join(timeout=2) if self.u.reporter._worker else None
        self.assertTrue(self.u.status()['enabled'])
        self.assertTrue(self.transport)
        payload = json.loads(self.transport[0][2])
        self.assertFalse(any('invit' in key.lower() or 'declin' in key.lower() for key in payload))
        self.assertFalse(self.u.status()['invitationEligible'])

    def test_explicit_old_optout_saved_file_suppresses_invitation(self):
        self.u.invitation_path.parent.mkdir()
        self.u.invitation_path.write_text('{"schema":1,"enabled":false}')
        self.assertFalse(self.u.status()['invitationEligible'])
        self.assertFalse(self.offer()['invitationOffered'])

    def test_unreadable_or_unknown_previous_choice_never_reprompts(self):
        for saved in ({'unexpected': 'value'}, False):
            self.c.history.cache(INVITATION_KEY, saved)
            self.assertFalse(self.u.status()['invitationEligible'])
        with patch('usage_integration.os.lstat', side_effect=PermissionError()):
            self.assertFalse(self.u.status()['invitationEligible'])

    def test_new_setup_waits_ten_minutes_and_missing_time_fails_closed(self):
        for at in (None, True, float('nan'), time.time() + 60, time.time() - 599):
            # JSON cache does not allow NaN, so supply a synthetic read for it.
            original = self.c.history.cache

            def cache(key, *args):
                return (
                    {'completed': True, 'completedAt': at}
                    if key == 'setup-v1'
                    else original(key, *args)
                )

            with patch.object(self.c.history, 'cache', side_effect=cache):
                self.assertFalse(self.u.status()['invitationEligible'])
        self.c.history.cache('setup-v1', {'completed': False, 'completedAt': time.time() - 3600})
        self.assertFalse(self.u.status()['invitationEligible'])

    def test_phone_cannot_offer_dismiss_or_enable(self):
        self.assertFalse(self.u.status(remote=True)['invitationEligible'])
        for data in (
            {'action': 'offer-invitation'},
            {'action': 'dismiss-invitation'},
            {'action': 'consent', 'enabled': True},
        ):
            with self.assertRaises(PermissionError):
                self.u.action(data, remote=True)
        self.assertIsNone(self.c.history.cache(INVITATION_KEY))

    def test_preview_missing_reporter_and_pending_deletion_suppress(self):
        self.u.invitation_enabled = False
        self.assertFalse(self.offer()['invitationOffered'])
        self.u.invitation_enabled = True
        with patch.object(self.u.reporter, 'status', return_value={'deletionPending': True}):
            self.assertFalse(self.u.status()['invitationEligible'])
        reporter = self.u.reporter
        self.u.reporter = None
        self.assertFalse(self.u.status()['invitationEligible'])
        self.u.reporter = reporter

    def test_failed_persistence_never_grants_an_offer(self):
        original = self.c.history.cache

        def cache(key, *args):
            if key == INVITATION_KEY and args:
                raise OSError('not saved')
            return original(key, *args)

        with patch.object(self.c.history, 'cache', side_effect=cache):
            with self.assertRaises(OSError):
                self.offer()
        self.assertIsNone(self.c.history.cache(INVITATION_KEY))
        self.assertEqual(self.transport, [])

    def test_rejects_unexpected_invitation_fields(self):
        for action in ('offer-invitation', 'dismiss-invitation'):
            with self.assertRaises(ValueError):
                self.u.action({'action': action, 'analyticsId': 'anything'})


if __name__ == '__main__':
    unittest.main()
