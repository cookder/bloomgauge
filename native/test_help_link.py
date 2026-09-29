"""Exercise the native external-link boundary with contact and hostile URLs."""

from swift_harness import compile_swift
from pathlib import Path
import shutil, subprocess, tempfile, unittest


@unittest.skipUnless(shutil.which('swiftc'), 'Swift compiler required')
class HelpLinkTests(unittest.TestCase):
    def test_exact_contact_routes_without_query_or_credentials(self):
        source = Path(__file__).with_name('App.swift').read_text()
        policy = source.split('// BEGIN help link policy')[1].split('// END help link policy.')[0]
        policy = policy[policy.index('\n') :]
        harness = """
let accepted = ["https://bloomformac.com/support", "https://bloomformac.com/privacy", "https://bloomformac.com/#release", "https://darkbloom.slack.com/archives/C0C4HC8HZLN", "mailto:support@bloomgauge.io"]
let website = ["https://bloomformac.com", "https://bloomformac.com/", "https://bloomformac.com/changelog", "https://bloomformac.com/beta/quickstart"]
for text in website {
    precondition(isBloomHelpLink(URL(string:text)!))
    precondition(!isBloomHelpLink(URL(string:text+"?token=secret")!))
    precondition(!isBloomHelpLink(URL(string:text+"#private-data")!))
    precondition(!isBloomHelpLink(URL(string:text.replacingOccurrences(of:"https:",with:"http:"))!))
    precondition(!isBloomHelpLink(URL(string:text.replacingOccurrences(of:"bloomformac.com",with:"bloomformac.com.evil.test"))!))
}
for text in ["https://bloomformac.com/beta", "https://bloomformac.com/changelog/private", "https://bloomformac.com/beta/quickstart/extra", "https://bloomformac.com/downloads/private.dmg", "https://bloomformac.com/api/owner/support"] {
    precondition(!isBloomHelpLink(URL(string:text)!))
}
let rejected = ["http://bloomformac.com/support", "https://bloomformac.com.evil.test/support", "https://bloomformac.com@evil.test/support", "https://user@bloomformac.com/support", "https://bloomformac.com:444/support", "https://bloomformac.com/support?token=secret", "https://bloomformac.com/support#token", "https://bloomformac.com/owner", "https://bloomformac.com/updates/beta.xml", "https://darkbloom.slack.com/team/UOTHER", "https://darkbloom.slack.com/team/U0BTZ9KGG22", "slack://user?user=U0BTZ9KGG22", "mailto:support@bloomgauge.io?body=secret", "mailto:someone@example.com", "javascript:alert(1)", "file:///tmp/support"]
for text in ["https://darkbloom.slack.com/archives/C0C4HC8HZLN/p1", "https://darkbloom.slack.com/archives/C0C4HC8HZLN?x=1", "https://darkbloom.slack.com/archives/C0OTHER"] { precondition(!isBloomHelpLink(URL(string:text)!)) }
for text in accepted { precondition(isBloomHelpLink(URL(string:text)!)) }
for text in rejected { precondition(!isBloomHelpLink(URL(string:text)!)) }
print("Exact native support routes passed")
"""
        exe = compile_swift('import Foundation\n' + policy + harness)
        result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
