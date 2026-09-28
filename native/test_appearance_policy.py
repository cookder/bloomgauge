"""The Mac window follows the dashboard's Appearance choice (System, Light, Dark)."""

from swift_harness import compile_swift
from pathlib import Path
import shutil, subprocess, unittest

SOURCE = Path(__file__).with_name('App.swift').read_text()


@unittest.skipUnless(shutil.which('swiftc'), 'Swift compiler required')
class AppearancePolicyTests(unittest.TestCase):
    def test_only_light_and_dark_pin_the_appearance(self):
        policy = SOURCE.split('// BEGIN appearance policy')[1].split('// END appearance policy.')[0]
        policy = policy[policy.index('\n') :]
        harness = """
precondition(bloomAppearanceName("light") == .aqua)
precondition(bloomAppearanceName("dark") == .darkAqua)
for other in ["system", "", "Dark", "auto", nil] as [String?] { precondition(bloomAppearanceName(other) == nil) }
precondition(appearanceDefaultsKey == "BloomAppearance")
print("Appearance policy passed")
"""
        exe = compile_swift('import AppKit\n' + policy + harness)
        result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


class AppearanceBridgeSourceTests(unittest.TestCase):
    def test_no_forced_dark_appearance(self):
        self.assertNotIn('NSApp.appearance = NSAppearance(named: .darkAqua)', SOURCE)
        self.assertIn('add(self,name:"bloomAppearance")', SOURCE)

    def test_bridge_accepts_only_the_local_dashboard(self):
        handler = SOURCE.split('if message.name=="bloomAppearance" {')[1].split('return\n        }')[0]
        for check in (
            'message.webView === webView',
            'message.frameInfo.isMainFrame',
            'url.host=="127.0.0.1"',
            'url.port==localURL?.port',
            '["system","light","dark"].contains(choice)',
            '!setupPreview',
        ):
            self.assertIn(check, handler)

    THEME = Path(__file__).parent.parent / 'lib/theme.ts'

    # The release check runs these tests on a copy of native/ alone.
    @unittest.skipUnless(THEME.exists(), 'web sources are not in this copy')
    def test_page_reports_its_choice_to_the_window(self):
        theme = self.THEME.read_text()
        self.assertIn('messageHandlers?.bloomAppearance?.postMessage(preference)', theme)


if __name__ == '__main__':
    unittest.main()
