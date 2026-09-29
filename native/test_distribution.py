"""Release-tool regressions; mocked Apple responses are not live signing proof."""

import json
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import distribution as d


JOB = '8ccbd62c-0f83-45ca-837f-bb4a8a7e0a5c'
IDENTITY = 'Developer ID Application: Fixture Publisher (FAKETEAM12)'
FINGERPRINT = 'A' * 40


class DistributionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dmg = Path(self.temp.name) / 'BloomGauge Beta.dmg'
        self.dmg.write_bytes(b'fixture signed container')
        self.calls = []

    def command(self, args, **kwargs):
        self.calls.append(args)
        if args[1:3] == ['find-identity', '-v']:
            return f'1) {FINGERPRINT} "{IDENTITY}"\n1 valid identities found'
        if args[1] == '-dv':
            return 'Authority=' + IDENTITY
        if args[1:3] == ['notarytool', 'submit']:
            self.assertIn('--no-wait', args)
            return json.dumps({'id': JOB})
        if args[1:3] == ['notarytool', 'info']:
            return json.dumps({'id': JOB, 'status': self.apple_status})
        if args[1:3] == ['stapler', 'staple']:
            self.dmg.write_bytes(b'fixture signed container and ticket')
        return '{}'

    def begin(self):
        with patch.object(d, 'run', side_effect=self.command):
            return d.submit(self.dmg, 'fixture-profile')

    def test_preflight_accepts_exact_developer_identity_or_hash(self):
        with patch.object(d, 'run', side_effect=self.command):
            self.assertTrue(d.preflight(IDENTITY, 'fixture-profile')['ready'])
            self.assertTrue(d.preflight(FINGERPRINT, 'fixture-profile')['ready'])
            self.assertFalse(d.preflight('Apple Development: Fixture', 'fixture-profile')['ready'])
            self.assertFalse(d.preflight(IDENTITY)['ready'])

    def test_missing_identity_or_failed_auth_is_not_ready(self):
        with patch.object(d, 'run', return_value='0 valid identities found'):
            self.assertFalse(d.preflight(IDENTITY, 'fixture-profile')['ready'])

        def failed_profile(args, **kwargs):
            if 'history' in args:
                raise RuntimeError('fixture authentication failure')
            return self.command(args, **kwargs)

        with patch.object(d, 'run', side_effect=failed_profile):
            self.assertFalse(d.preflight(IDENTITY, 'fixture-profile')['ready'])

    def test_unsigned_container_does_not_submit(self):
        with patch.object(d, 'run', return_value='Signature=adhoc'):
            with self.assertRaisesRegex(RuntimeError, 'Developer ID'):
                d.submit(self.dmg, 'fixture-profile')
        self.assertFalse(d.receipt_path(self.dmg).exists())

    def test_receipt_binds_bytes_and_duplicate_upload_is_refused(self):
        self.begin()
        receipt = d.receipt_path(self.dmg)
        data = json.loads(receipt.read_text())
        self.assertEqual(data['submittedSha256'], d.sha256(self.dmg))
        self.assertEqual(data['submissionId'], JOB)
        self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o600)
        with patch.object(d, 'run') as mocked:
            with self.assertRaisesRegex(RuntimeError, 'already exists'):
                d.submit(self.dmg, 'fixture-profile')
            mocked.assert_not_called()

    def test_uncertain_upload_preserves_receipt_and_prevents_retry(self):
        def timeout(args, **kwargs):
            if 'submit' in args:
                raise subprocess.TimeoutExpired(args, 300)
            return self.command(args, **kwargs)

        with patch.object(d, 'run', side_effect=timeout):
            with self.assertRaises(subprocess.TimeoutExpired):
                d.submit(self.dmg, 'fixture-profile')
        data = json.loads(d.receipt_path(self.dmg).read_text())
        self.assertIsNone(data['submissionId'])
        with self.assertRaisesRegex(RuntimeError, 'already exists'):
            d.submit(self.dmg, 'fixture-profile')
        with self.assertRaisesRegex(RuntimeError, 'uncertain'):
            d.finish(self.dmg, 'fixture-profile')

    def test_changed_artifact_rejected_before_apple_access(self):
        self.begin()
        self.dmg.write_bytes(b'a different container')
        with patch.object(d, 'run') as mocked:
            with self.assertRaisesRegex(RuntimeError, 'changed since'):
                d.finish(self.dmg, 'fixture-profile')
            mocked.assert_not_called()

    def test_pending_or_rejected_job_never_staples(self):
        self.begin()
        for status in ('In Progress', 'Invalid', 'Rejected', 'Unknown'):
            self.apple_status = status
            self.calls.clear()
            with patch.object(d, 'run', side_effect=self.command):
                self.assertFalse(d.finish(self.dmg, 'fixture-profile')['verified'])
            self.assertFalse(any('staple' in command for command in self.calls))
        self.assertFalse(self.dmg.with_suffix('.dmg.sha256').exists())

    def test_accepted_job_verifies_ticket_gatekeeper_and_post_staple_hash(self):
        self.begin()
        before = d.sha256(self.dmg)
        self.apple_status = 'Accepted'
        with patch.object(d, 'run', side_effect=self.command):
            result = d.finish(self.dmg, 'fixture-profile')
        self.assertTrue(result['verified'])
        self.assertNotEqual(before, result['sha256'])
        self.assertEqual(result['sha256'], d.sha256(self.dmg))
        self.assertEqual(
            self.dmg.with_suffix('.dmg.sha256').read_text(),
            f'{result["sha256"]}  {self.dmg.name}\n',
        )
        self.assertTrue(any('/usr/sbin/spctl' in command for command in self.calls))
        self.assertTrue(any('validate' in command for command in self.calls))

    def test_validation_failure_can_resume_without_resubmitting(self):
        self.begin()
        self.apple_status = 'Accepted'

        def fail_validation(args, **kwargs):
            if 'validate' in args:
                raise RuntimeError('fixture validation failure')
            return self.command(args, **kwargs)

        with patch.object(d, 'run', side_effect=fail_validation):
            with self.assertRaises(RuntimeError):
                d.finish(self.dmg, 'fixture-profile')
        data = json.loads(d.receipt_path(self.dmg).read_text())
        self.assertFalse(data['verified'])
        self.assertEqual(data['stapledSha256'], d.sha256(self.dmg))
        self.calls.clear()
        with patch.object(d, 'run', side_effect=self.command):
            self.assertTrue(d.finish(self.dmg, 'fixture-profile')['verified'])
        self.assertFalse(any('submit' in command for command in self.calls))

    def test_wrong_submission_id_cannot_staple(self):
        self.begin()

        def wrong_job(args, **kwargs):
            if 'info' in args:
                return json.dumps({'id': 'f' * 36, 'status': 'Accepted'})
            return self.command(args, **kwargs)

        with patch.object(d, 'run', side_effect=wrong_job):
            with self.assertRaisesRegex(RuntimeError, 'different submission'):
                d.finish(self.dmg, 'fixture-profile')

    def test_receipt_symlink_refused(self):
        target = self.dmg.parent / 'unrelated.json'
        target.write_text('leave intact')
        d.receipt_path(self.dmg).symlink_to(target)
        with self.assertRaises(RuntimeError):
            d.submit(self.dmg, 'fixture-profile')
        with self.assertRaises(RuntimeError):
            d.finish(self.dmg, 'fixture-profile')
        self.assertEqual(target.read_text(), 'leave intact')

    def test_customer_notes_do_not_claim_unverified_notarization(self):
        source = Path(__file__).parent / 'BETA_README.txt'
        self.assertEqual(d.installer_readme(source, False), source.read_text())
        signed = d.installer_readme(source, True)
        self.assertIn('must finish notarization', signed)
        self.assertNotIn('this preview is not notarized', signed)
        self.assertNotIn('DEVELOPER PREVIEW.', signed)


if __name__ == '__main__':
    unittest.main()
