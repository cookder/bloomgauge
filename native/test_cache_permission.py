import subprocess, unittest
from unittest.mock import Mock
from cache_permission import cache_permission

READY = 'Sudoers entry: /private/etc/sudoers.d/bloom-dashboard-cache\n    RunAsUsers: root\n    Options: !authenticate\n    Commands:\n\t/usr/sbin/purge ""\n    Matched: /usr/sbin/purge\n'


class PermissionTests(unittest.TestCase):
    def probe(self, text=READY, code=0):
        runner = Mock(
            return_value=Mock(
                returncode=code, stdout=text, stderr='private diagnostic never returned'
            )
        )
        result = cache_permission(runner)
        self.assertEqual(
            runner.call_args.args[0], ['/usr/bin/sudo', '-n', '-ll', '/usr/sbin/purge']
        )
        self.assertEqual(runner.call_args.kwargs['stdin'], subprocess.DEVNULL)
        self.assertEqual(runner.call_args.kwargs['timeout'], 5)
        self.assertEqual(
            runner.call_args.kwargs['env'],
            {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LC_ALL': 'C', 'LANG': 'C'},
        )
        self.assertNotIn('private', result['detail'])
        return result

    def test_exact_no_argument_nopasswd_permission_ready_without_purge(self):
        self.assertEqual(self.probe()['status'], 'ready')

    def test_cached_password_listing_is_not_passwordless_permission(self):
        self.assertEqual(
            self.probe(READY.replace('!authenticate', 'authenticate'))['status'], 'required'
        )

    def test_missing_permission_requires_one_time_setup(self):
        self.assertEqual(self.probe('', 1)['status'], 'required')

    def test_multiple_or_later_deny_entries_fail_closed(self):
        for text in [
            READY + READY,
            READY + READY.replace('/usr/sbin/purge ""', '!/usr/sbin/purge ""'),
            READY.replace('/usr/sbin/purge ""', '/usr/sbin/purge ""\n\t!/usr/sbin/purge ""'),
        ]:
            with self.subTest(text=text):
                self.assertEqual(self.probe(text)['status'], 'unknown')

    def test_broad_arguments_nonroot_and_mismatched_commands_fail_closed(self):
        for old, new in [
            ('/usr/sbin/purge ""', 'ALL'),
            ('/usr/sbin/purge ""', '/usr/sbin/purge'),
            ('/usr/sbin/purge ""', '/usr/sbin/purge *'),
            ('RunAsUsers: root', 'RunAsUsers: ALL'),
            ('Matched: /usr/sbin/purge', 'Matched: /usr/sbin/other'),
            ('Options: !authenticate', 'Options: !authenticate, authenticate'),
        ]:
            with self.subTest(new=new):
                self.assertNotEqual(self.probe(READY.replace(old, new))['status'], 'ready')

    def test_unknown_or_error_output_not_ready(self):
        self.assertEqual(self.probe('unexpected format')['status'], 'unknown')
        self.assertEqual(self.probe(READY + '    !/usr/sbin/purge\n')['status'], 'unknown')
        self.assertEqual(self.probe(READY + '    Options: authenticate\n')['status'], 'unknown')
        for err in [OSError('private'), subprocess.TimeoutExpired('private', 5)]:
            self.assertEqual(cache_permission(Mock(side_effect=err))['status'], 'unknown')


if __name__ == '__main__':
    unittest.main()
