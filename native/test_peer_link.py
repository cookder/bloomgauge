"""Exercise the actual native allowlist used by a user-activated peer link."""

from swift_harness import compile_swift
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipUnless(shutil.which('swiftc'), 'Swift compiler required')
class PeerLinkTests(unittest.TestCase):
    def test_exact_private_dashboard_links_only(self):
        source = Path(__file__).with_name('App.swift').read_text()
        policy = source.split('// BEGIN peer dashboard policy')[1].split(
            '// END peer dashboard policy.'
        )[0]
        policy = policy[policy.index('\n') :]
        harness = """
let accepted = ["https://studio.test.ts.net:8443/?screen=overview"]
let rejected = ["http://studio.test.ts.net:8443/?screen=overview", "https://studio.test.ts.net/?screen=overview", "https://studio.test.ts.net:443/?screen=overview", "https://studio.test.ts.net:8443/api/model-control", "https://studio.test.ts.net:8443/?screen=overview&action=switch", "https://user@studio.test.ts.net:8443/?screen=overview", "https://studio.test.ts.net.evil.com:8443/?screen=overview", "https://127.0.0.1:8443/?screen=overview", "https://studio.test.ts.net:8443/?screen=overview#x"]
for text in accepted { precondition(isBloomPeerDashboard(URL(string:text)!)) }
for text in rejected { precondition(!isBloomPeerDashboard(URL(string:text)!)) }
print("Native peer URL policy passed")
"""
        exe = compile_swift('import Foundation\n' + policy + harness)
        result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
