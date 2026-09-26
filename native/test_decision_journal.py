import sqlite3
import threading
import unittest

from decision_journal import DecisionJournal, reason_code


class DecisionHistoryTests(unittest.TestCase):
    def setUp(self):
        self.h = type('History', (), {})()
        self.h.db = sqlite3.connect(':memory:')
        self.h.db.row_factory = sqlite3.Row
        self.h.lock = threading.RLock()
        self.journal = DecisionJournal(self.h)

    def tearDown(self):
        self.h.db.close()

    def record(self, at, reason, account='owner', device='mac', phase='watching'):
        self.journal.record(account, device, at, 'demand', 'bonsai', None, phase, reason)

    def test_same_category_reason_transition_keeps_original_interval(self):
        measuring = 'Gathering baseline payment samples.'
        settling = (
            'The warm trial is complete; waiting up to five minutes for its credits to settle.'
        )
        self.record(100, measuring)
        self.record(115, measuring)
        self.record(130, settling)
        self.record(145, settling)
        result = self.journal.snapshot('owner', 'mac', 150)
        self.assertEqual(
            [(x['reason'], x['observedSeconds']) for x in result['history']],
            [(settling, 15), (measuring, 15)],
        )
        self.assertEqual(result['totals'], [{'code': 'observation', 'seconds': 30}])

    def test_reason_codes_follow_the_main_reason_not_incidental_words(self):
        self.assertEqual(
            reason_code(
                'Preserving productive paid work. A switch needs a supported earnings improvement.'
            ),
            'protected',
        )
        self.assertEqual(
            reason_code(
                'Could not evaluate the optimizer. Keeping the current model and retrying.'
            ),
            'freshness',
        )
        self.assertEqual(reason_code('Trying again later.'), 'observation')  # 'again' is not 'gain'
        self.assertEqual(
            reason_code(
                'Paid pace is below target. Alternative: waiting for fresh, sustained network observations.'
            ),
            'network',
        )

    def test_backward_clock_change_starts_a_new_row_instead_of_freezing(self):
        self.record(10000, 'Watching demand.')
        self.record(6400, 'Watching demand.')  # clock set back an hour
        self.record(6415, 'Watching demand.')
        rows = self.h.db.execute(
            'SELECT at,updated FROM optimizer_decisions ORDER BY id'
        ).fetchall()
        self.assertEqual([tuple(r) for r in rows], [(10000, 10000), (6400, 6415)])

    def test_totals_cover_all_intervals_beyond_display_limit(self):
        for i in range(1005):
            at = 1000 + i * 60
            phase = 'watching' if i % 2 else 'waiting'
            self.record(at, 'Observation %d.' % i, phase=phase)
            self.record(at + 15, 'Observation %d.' % i, phase=phase)
        result = self.journal.snapshot('owner', 'mac', 62000)
        self.assertEqual(len(result['history']), 30)
        self.assertEqual(result['totals'], [{'code': 'observation', 'seconds': 1005 * 15}])

    def test_totals_and_history_clip_to_both_window_boundaries(self):
        self.record(1000, 'Observation.')
        self.record(1150, 'Observation.')
        result = self.journal.snapshot('owner', 'mac', 87420)
        self.assertEqual(result['history'][0]['observedSeconds'], 130)
        self.assertEqual(result['totals'], [{'code': 'observation', 'seconds': 130}])
        historical = self.journal.snapshot('owner', 'mac', 1100)
        self.assertEqual(historical['history'][0]['observedSeconds'], 100)
        self.assertEqual(historical['totals'], [{'code': 'observation', 'seconds': 100}])

    def test_totals_keep_account_device_and_waiting_phase_scope(self):
        for account, device, phase in [
            ('owner', 'mac', 'watching'),
            ('other', 'mac', 'watching'),
            ('owner', 'other-mac', 'watching'),
            ('owner', 'mac', 'completed'),
        ]:
            self.record(100, 'Observation.', account, device, phase)
            self.record(115, 'Observation.', account, device, phase)
        result = self.journal.snapshot('owner', 'mac', 120)
        self.assertEqual(result['totals'], [{'code': 'observation', 'seconds': 15}])
        before = self.h.db.total_changes
        self.assertEqual(self.journal.snapshot('owner', 'mac', 120), result)
        self.assertEqual(self.h.db.total_changes, before)


if __name__ == '__main__':
    unittest.main()
