"""Run CachePermissionTests.swift against the native cache-permission helper.

The Swift tests compile the setup and removal AppleScripts and round-trip the
quoting through a return-only AppleScript and printf. Nothing privileged runs.
"""

from swift_harness import compile_swift
from pathlib import Path
import shutil, subprocess, unittest


@unittest.skipUnless(shutil.which('swiftc'), 'Swift compiler required')
class CachePermissionNativeTests(unittest.TestCase):
    def test_native_permission_commands_scripts_and_quoting(self):
        native = Path(__file__).parent
        tests = (native / 'CachePermissionTests.swift').read_text()
        # One main.swift file can't use @main, so call the test entry point directly.
        tests = tests.replace('@main struct', 'struct') + '\ntry PermissionTests.main()\n'
        exe = compile_swift((native / 'CachePermission.swift').read_text() + '\n' + tests)
        result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Native permission tests passed', result.stdout)


if __name__ == '__main__':
    unittest.main()
