"""Compile and exercise the actual Foundation-only policy embedded in App.swift.

No WebKit, real account, provider command, or network request is used here.
"""

from swift_harness import compile_swift
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipUnless(shutil.which('swiftc'), 'Swift compiler required')
class ReputationRefreshTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='bloom-reputation-refresh-')
        cls.addClassCleanup(cls.temp.cleanup)
        root = Path(cls.temp.name)
        source = Path(__file__).with_name('App.swift').read_text()
        policy = source.split('// BEGIN reputation refresh policy', 1)[1]
        policy = policy[policy.index('\n') :].split('// END reputation refresh policy.', 1)[0]
        main = root / 'main.swift'
        main.write_text(
            'import Foundation\n'
            + policy
            + r"""
let official=URL(string:"https://console.darkbloom.dev/earn")!
var policy=ReputationAuthRefresh()
func tick(_ status:String,_ at:Double,connected:Bool=true,visible:Bool=false,url:URL?=official)->Bool{
    policy.shouldReload(status:status,now:at,connected:connected,visible:visible,url:url)
}
func healthy(_ start:Double=0){
    for offset in stride(from:0.0,through:60.0,by:15.0){assert(!tick("ok",start+offset))}
}
switch CommandLine.arguments[1] {
case "initial_login":
    for at in stride(from:0.0,through:7200.0,by:15.0){assert(!tick("auth_required",at))}
case "expiry_once":
    healthy();assert(tick("auth_required",75))
    for at in stride(from:90.0,through:7200.0,by:15.0){assert(!tick("auth_required",at))}
case "success_rearms":
    healthy();assert(tick("auth_required",75));healthy(90)
    assert(!tick("auth_required",165));assert(!tick("auth_required",1874))
    assert(tick("auth_required",1875));assert(!tick("auth_required",1890))
case "brief_success_no_loop":
    healthy();assert(tick("auth_required",75));assert(!tick("ok",90))
    assert(!tick("auth_required",105));assert(!tick("auth_required",7200))
case "sparse_success":
    for at in stride(from:0.0,through:600.0,by:60.0){assert(!tick("ok",at))}
    assert(!tick("auth_required",615))
    policy.reset();assert(!tick("ok",100));assert(!tick("ok",90))
    assert(!tick("ok",120));assert(!tick("auth_required",135))
case "visible_signin":
    healthy();assert(!tick("auth_required",75,visible:true))
    assert(!tick("auth_required",1900,visible:true));assert(tick("auth_required",1915))
case "disconnect_and_manual_reset":
    healthy();assert(!tick("auth_required",75,connected:false))
    assert(!tick("auth_required",90));healthy(100)
    policy.reset();assert(!tick("auth_required",180))
case "origin_guard":
    for raw in ["http://console.darkbloom.dev/earn","https://console.darkbloom.dev:444/earn",
                "https://console.darkbloom.dev.evil.example/earn","https://evil.example/earn",
                "https://user@console.darkbloom.dev/earn","about:blank"] {
        policy.reset();healthy();assert(!tick("auth_required",75,url:URL(string:raw)))
    }
    policy.reset();healthy();assert(!tick("auth_required",75,url:nil))
    assert(tick("auth_required",90,url:URL(string:"https://console.darkbloom.dev:443/earn")))
case "other_failures":
    healthy()
    for status in ["unavailable","connecting","stale"] {assert(!tick(status,75))}
    assert(tick("auth_required",90))
    healthy(100);assert(!tick("unmatched",170));assert(!tick("auth_required",4000))
case "sleep_after_established":
    healthy();assert(tick("auth_required",7200))
default: fatalError("Unknown test")
}
"""
        )
        cls.binary = compile_swift(main.read_text())

    def run_case(self, name):
        result = subprocess.run([str(self.binary), name], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


for _case in (
    'initial_login',
    'expiry_once',
    'success_rearms',
    'brief_success_no_loop',
    'sparse_success',
    'visible_signin',
    'disconnect_and_manual_reset',
    'origin_guard',
    'other_failures',
    'sleep_after_established',
):
    setattr(ReputationRefreshTests, 'test_' + _case, lambda self, name=_case: self.run_case(name))

if __name__ == '__main__':
    unittest.main()
