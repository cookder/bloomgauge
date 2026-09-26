import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo
from community_insights import CommunityInsights, slack_link, validate_review

NOW = 1789388280.0


def review():
    return {
        'status': 'ok',
        'detail': 'Reviewed provider discussions.',
        'from': NOW - 86400,
        'to': NOW,
        'channels': ['providers'],
        'headline': 'Provider notes',
        'summary': 'A test fixture, not actual Slack findings.',
        'items': [
            {
                'kind': 'model_tip',
                'title': 'Compare matched warm sessions',
                'body': 'Example observation.',
                'relevance': 'Check on this Mac.',
                'measure': 'Record paid dollars per warm hour.',
                'models': ['Gemma'],
                'sources': [
                    {
                        'url': 'https://example.slack.com/archives/C12345/p1789380000123456',
                        'channel': 'providers',
                        'at': NOW - 100,
                        'author': 'Test author',
                        'authority': 'community',
                    }
                ],
            }
        ],
    }


class CommunityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CommunityInsights(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_is_unreviewed_and_has_no_synthetic_findings(self):
        value = self.store.snapshot(NOW)
        self.assertEqual(value['status'], 'waiting')
        self.assertEqual(value['digests'], [])
        self.assertIsNone(value['lastSuccessAt'])
        self.assertFalse(value['schedule']['enabled'])

    def test_round_trip_and_private_atomic_file(self):
        original = review()
        untouched = copy.deepcopy(original)
        self.store.ingest(original, NOW)
        value = CommunityInsights(self.tmp.name).snapshot(NOW)
        self.assertEqual(value['status'], 'ok')
        self.assertEqual(value['lastSuccessAt'], NOW)
        self.assertEqual(value['reviewedThrough'], NOW)
        self.assertEqual(
            value['digests'][0]['items'][0]['sources'], original['items'][0]['sources']
        )
        self.assertEqual(original, untouched)
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_failed_or_partial_checks_preserve_digest_and_complete_coverage(self):
        self.store.ingest(review(), NOW)
        first = self.store.read()['digests'][0]
        for status in ('blocked', 'error', 'partial'):
            payload = {
                **review(),
                'status': status,
                'items': [],
                'to': NOW + 100,
                'detail': 'Could not complete the read.',
            }
            self.store.ingest(payload, NOW + 100)
            value = self.store.snapshot(NOW + 100)
            self.assertEqual(value['status'], status)
            self.assertEqual(value['digests'], [first])
            self.assertEqual(value['reviewedThrough'], NOW)
            self.assertEqual(value['lastSuccessAt'], NOW)

    def test_no_new_insights_updates_check_without_erasing_prior_summary(self):
        self.store.ingest(review(), NOW)
        first = self.store.read()['digests'][0]
        self.store.ingest(
            {**review(), 'to': NOW + 120, 'items': [], 'detail': 'No new relevant discussions.'},
            NOW + 120,
        )
        value = self.store.snapshot(NOW + 120)
        self.assertEqual(value['digests'], [first])
        self.assertEqual(value['lastAttempt']['at'], NOW + 120)
        self.assertEqual(value['lastSuccessAt'], NOW + 120)
        self.assertEqual(value['reviewedThrough'], NOW + 120)
        self.assertEqual(self.store.snapshot(NOW + 16 * 3600)['status'], 'stale')

    def test_bad_evidence_is_rejected_without_overwriting_good_data(self):
        self.store.ingest(review(), NOW)
        before = self.store.path.read_bytes()
        invalid = []
        a = review()
        a['items'][0]['sources'] = []
        invalid.append(a)
        a = review()
        a['items'][0]['kind'] = 'team_update'
        invalid.append(a)
        a = review()
        a['items'][0]['sources'][0]['at'] = NOW + 100
        invalid.append(a)
        a = review()
        a['status'] = 'blocked'
        invalid.append(a)
        a = review()
        a['channels'] = []
        invalid.append(a)
        a = review()
        a['items'][0]['body'] = 'xoxb-example-secret'
        invalid.append(a)
        a = review()
        a['extra'] = 'not allowed'
        invalid.append(a)
        a = review()
        a['items'] *= 13
        invalid.append(a)
        a = review()
        a['to'] = float('nan')
        invalid.append(a)
        for payload in invalid:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    self.store.ingest(payload, NOW)
                self.assertEqual(self.store.path.read_bytes(), before)

    def test_slack_sources_cannot_launch_commands_or_other_websites(self):
        good = review()['items'][0]['sources'][0]['url']
        self.assertEqual(slack_link(good), good)
        for url in (
            'javascript:alert(1)',
            'file:///etc/passwd',
            'http://example.slack.com/archives/C1/p1789380000123456',
            good + '?token=secret',
            good + '#fragment',
            good.replace('example.slack.com', 'example.slack.com.evil.org'),
            good.replace('example.slack.com', 'user@example.slack.com'),
            good.replace('example.slack.com', 'example.slack.com:443'),
            'https://example.slack.com/redirect',
            good.replace('/C12345/', '/D12345/'),
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    slack_link(url)

    def test_team_update_requires_team_evidence(self):
        payload = review()
        payload['items'][0]['kind'] = 'team_update'
        payload['items'][0]['sources'][0]['authority'] = 'team'
        self.assertEqual(validate_review(payload, NOW)['items'][0]['kind'], 'team_update')

    def test_old_review_cannot_move_the_watermark_back(self):
        self.store.ingest(review(), NOW)
        with self.assertRaises(ValueError):
            self.store.ingest({**review(), 'to': NOW - 100}, NOW - 100)
        self.assertEqual(self.store.read()['lastSuccessAt'], NOW)

    def test_corrupt_or_symlinked_store_is_not_reported_as_fresh(self):
        self.store.path.write_text('{')
        self.assertEqual(self.store.snapshot(NOW)['status'], 'error')
        self.store.path.unlink()
        target = Path(self.tmp.name) / 'other.json'
        target.write_text(json.dumps(self.store.empty()))
        self.store.path.symlink_to(target)
        self.assertEqual(self.store.snapshot(NOW)['status'], 'error')
        with self.assertRaises(ValueError):
            self.store.ingest(review(), NOW)

    def test_saved_links_are_revalidated_and_unknown_root_fields_do_not_leak(self):
        self.store.ingest(review(), NOW)
        value = self.store.read()
        value['privateExtra'] = 'secret'
        self.store.path.write_text(json.dumps(value))
        self.assertNotIn('privateExtra', self.store.snapshot(NOW))
        value['digests'][0]['items'][0]['sources'][0]['url'] = 'https://evil.example/'
        self.store.path.write_text(json.dumps(value))
        self.assertEqual(self.store.snapshot(NOW)['status'], 'error')

    def test_schedule_uses_central_time_across_dst_and_preserves_history(self):
        self.store.ingest(review(), NOW)
        self.store.configure(True)
        for day in ('2026-09-14', '2026-11-01', '2026-03-08'):
            current = dt.datetime.fromisoformat(day + 'T07:30:00').replace(
                tzinfo=ZoneInfo('America/Chicago')
            )
            value = self.store.snapshot(current.timestamp())
            next_time = dt.datetime.fromtimestamp(value['nextCheckAt'], ZoneInfo('America/Chicago'))
            self.assertEqual(next_time.hour, 8)
        self.store.configure(False)
        self.assertIsNone(self.store.snapshot(NOW)['nextCheckAt'])
        self.assertEqual(len(self.store.read()['digests']), 1)

    def test_saved_digest_history_is_bounded(self):
        for n in range(35):
            self.store.ingest(review(), NOW + n)
        self.assertEqual(len(self.store.read()['digests']), 30)
        self.assertEqual(self.store.read()['digests'][0]['at'], NOW + 34)


if __name__ == '__main__':
    unittest.main()
