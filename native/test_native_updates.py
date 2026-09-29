"""Native update bridge/admission policy: isolated Swift; no app/provider launch."""

from swift_harness import compile_swift
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).parent


class NativeUpdatePolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='bloom-native-update-tests-')
        cls.addClassCleanup(cls.tmp.cleanup)
        source = (ROOT / 'Updates.swift').read_text()
        policy = source.split(
            '// BEGIN update bridge policy (compiled directly by its regression test).', 1
        )[1].split('// END update bridge policy.', 1)[0]
        runner = pathlib.Path(cls.tmp.name) / 'main.swift'
        runner.write_text(
            'import Foundation\nimport CoreFoundation\n'
            + policy
            + """
let fixtureID="11111111-1111-4111-8111-111111111111"
let leaseID="22222222-2222-4222-8222-222222222222"
func assertTrue(_ value:Bool,_ description:String){if !value{fputs(description+"\\n",stderr);exit(1)}}
final class Sender {
    var requests:[[String:Any]]=[]
    var replies:[(Int,[String:Any]?)->Void]=[]
    func send(_ body:[String:Any],_ reply:@escaping(Int,[String:Any]?)->Void){requests.append(body);replies.append(reply)}
    func reply(_ index:Int,_ status:Int,_ body:[String:Any]?){replies[index](status,body)}
}
let mode=CommandLine.arguments[1]
if mode=="bridge" {
    for action in ["status","check"]{assertTrue(BloomUpdateBridgeCommand(["action":action,"requestId":fixtureID]) != nil,"valid read action")}
    assertTrue(BloomUpdateBridgeCommand(["action":"set-automatic","requestId":fixtureID,"enabled":true])?.enabled == true,"explicit on")
    assertTrue(BloomUpdateBridgeCommand(["action":"set-automatic","requestId":fixtureID,"enabled":false])?.enabled == false,"explicit off")
    for bad:Any in [[:], ["action":"check","requestId":"bad"], ["action":"install","requestId":fixtureID], ["action":"status","requestId":fixtureID,"enabled":true], ["action":"set-automatic","requestId":fixtureID,"enabled":1], ["action":"set-automatic","requestId":fixtureID], ["action":"check","requestId":fixtureID,"url":"https://invalid.test"]] {
        assertTrue(BloomUpdateBridgeCommand(bad)==nil,"reject malformed action")
    }
} else if mode=="origin" {
    let base=URL(string:"http://127.0.0.1:8765/")!
    assertTrue(isBloomUpdateFrame(URL(string:"http://127.0.0.1:8765/?screen=more"),localURL:base,isMainFrame:true,belongsToDashboard:true),"native frame allowed")
    for address in ["http://127.0.0.1:8766/","https://127.0.0.1:8765/","http://localhost:8765/","http://127.0.0.1.evil.test:8765/","http://user:password@127.0.0.1:8765/"] {
        assertTrue(!isBloomUpdateFrame(URL(string:address),localURL:base,isMainFrame:true,belongsToDashboard:true),"reject foreign origin")
    }
    assertTrue(!isBloomUpdateFrame(base,localURL:base,isMainFrame:false,belongsToDashboard:true),"reject subframe")
    assertTrue(!isBloomUpdateFrame(base,localURL:base,isMainFrame:true,belongsToDashboard:false),"reject other webview")
    assertTrue(!isBloomUpdateFrame(base,localURL:nil,isMainFrame:true,belongsToDashboard:true),"reject uninitialized native origin")
} else if mode=="quiet-policy" {
    let P=BloomQuietInstallPolicy.self
    assertTrue(P.due(waiting:600,sinceLaunch:3600,userIdle:600,appActive:true),"away ten minutes: install")
    assertTrue(!P.due(waiting:600,sinceLaunch:3600,userIdle:30,appActive:false),"in use elsewhere, first day: wait")
    assertTrue(!P.due(waiting:600,sinceLaunch:3600,userIdle:30,appActive:true),"in use in front: wait")
    assertTrue(P.due(waiting:86_400,sinceLaunch:3600,userIdle:30,appActive:false),"after a day, background is enough")
    assertTrue(!P.due(waiting:86_400*3,sinceLaunch:3600,userIdle:30,appActive:true),"never under the person's cursor")
    assertTrue(!P.due(waiting:60,sinceLaunch:3600,userIdle:9999,appActive:false),"let the download settle")
    assertTrue(!P.due(waiting:9999,sinceLaunch:120,userIdle:9999,appActive:false),"not right after launch")
    assertTrue(!P.due(waiting:.nan,sinceLaunch:3600,userIdle:9999,appActive:false),"non-finite fails closed")
    assertTrue(!P.due(waiting:9999,sinceLaunch:3600,userIdle:.infinity,appActive:false),"non-finite idle fails closed")
    assertTrue(P.automatic(waiting:600) && !P.automatic(waiting:86_400),"soft holds for the first day only")
    assertTrue(P.automatic(waiting:.nan),"unknown wait keeps the soft holds")
} else if mode=="relaunch-window" {
    let now:TimeInterval=10_000
    assertTrue(bloomRelaunchWindow(bloomQuietRelaunchRecord(windowVisible:true,appActive:true,now:now),now:now+30) == .front,"front stays front")
    assertTrue(bloomRelaunchWindow(bloomQuietRelaunchRecord(windowVisible:true,appActive:false,now:now),now:now+30) == .back,"background stays behind")
    assertTrue(bloomRelaunchWindow(bloomQuietRelaunchRecord(windowVisible:false,appActive:true,now:now),now:now+30) == .hidden,"closed window stays closed")
    assertTrue(bloomRelaunchWindow(bloomQuietRelaunchRecord(windowVisible:false,appActive:false,now:now),now:now+901) == nil,"stale record: ordinary launch")
    assertTrue(bloomRelaunchWindow(bloomQuietRelaunchRecord(windowVisible:false,appActive:false,now:now),now:now-5) == nil,"future record: ordinary launch")
    assertTrue(bloomRelaunchWindow(bloomQuitRelaunchRecord(now:now),now:now+20) == .quit,"quit record right after the relaunch")
    assertTrue(bloomRelaunchWindow(bloomQuitRelaunchRecord(now:now),now:now+85) == .quit,"install and relaunch may take a while")
    assertTrue(bloomRelaunchWindow(bloomQuitRelaunchRecord(now:now),now:now+91) == nil,"a quit record never closes a later, manual launch")
    let back=bloomQuietRelaunchRecord(windowVisible:true,appActive:false,now:now,frontApp:"com.apple.finder")
    assertTrue(bloomRelaunchFrontApp(back) == "com.apple.finder","remember the app in front")
    assertTrue(bloomRelaunchFrontApp(bloomQuietRelaunchRecord(windowVisible:true,appActive:true,now:now,frontApp:"com.apple.finder")) == nil,"no hand-back when BloomGauge was in front")
    assertTrue(bloomRelaunchFrontApp(["frontApp":String(repeating:"a",count:300)]) == nil && bloomRelaunchFrontApp(nil) == nil,"malformed front app ignored")
    for bad:Any? in [nil,"hidden",["at":now],["window":"hidden"],["at":"x","window":"hidden"],["at":now,"window":"sideways"]] {
        assertTrue(bloomRelaunchWindow(bad,now:now) == nil,"malformed record: ordinary launch")
    }
} else if mode=="probe" {
    for (status,body,expect) in [(200,["ready":true,"lease":leaseID] as [String:Any],true),(409,["ready":false,"reason":"excursion"],false),(200,["ready":true,"lease":"bad"],false),(200,["ready":false,"lease":leaseID],false),(0,[:],false)] {
        let sender=Sender();var called=false;var probe:BloomQuietProbe?=nil
        bloomProbeQuietInstall(send:sender.send,automatic:true){called=true;probe=$0}
        assertTrue(sender.requests[0]["action"] as? String == "prepare" && sender.requests[0]["automatic"] as? Bool == true,"automatic probe")
        sender.reply(0,status,status==0 ? nil : body)
        assertTrue(called && (probe != nil)==expect,"probe result")
        assertTrue(sender.requests.count==1,"a granted reservation is kept for the install, never released by the probe")
        if let probe=probe {
            assertTrue(probe.lease==leaseID && probe.requestId==sender.requests[0]["requestId"] as? String,"probe hands over its request and lease")
            bloomReleaseQuietProbe(probe,send:sender.send)
            assertTrue(sender.requests[1]["action"] as? String == "release" && sender.requests[1]["lease"] as? String == leaseID,"explicit release when the install does not go ahead")
        }
    }
    let sender=Sender();bloomProbeQuietInstall(send:sender.send,automatic:false){_ in}
    assertTrue(sender.requests[0]["automatic"]==nil,"after a day the probe drops soft holds")
} else if mode=="automatic-admission" {
    let sender=Sender();let gate=BloomUpdateAdmission(send:sender.send,clock:{100})
    gate.begin(automatic:true,ready:{},failed:{_ in})
    assertTrue(sender.requests[0]["automatic"] as? Bool == true && Set(sender.requests[0].keys)==Set(["action","requestId","automatic"]),"automatic prepare")
    let manual=Sender();BloomUpdateAdmission(send:manual.send,clock:{100}).begin(ready:{},failed:{_ in})
    assertTrue(Set(manual.requests[0].keys)==Set(["action","requestId"]),"a person's install has no automatic flag")
    let reuse=Sender();BloomUpdateAdmission(send:reuse.send,clock:{100}).begin(automatic:true,requestId:fixtureID,ready:{},failed:{_ in})
    assertTrue(reuse.requests[0]["requestId"] as? String == fixtureID,"termination reuses the probe's request, so the same reservation comes back")
    let bad=Sender();BloomUpdateAdmission(send:bad.send,clock:{100}).begin(requestId:"not-a-uuid",ready:{},failed:{_ in})
    let fresh=bad.requests[0]["requestId"] as? String ?? ""
    assertTrue(fresh != "not-a-uuid" && UUID(uuidString:fresh) != nil,"a malformed reused id is replaced")
} else {
    var now:TimeInterval=100
    let sender=Sender();let gate=BloomUpdateAdmission(send:sender.send,clock:{now})
    var ready=0;var failures:[String]=[]
    func begin(){gate.begin(ready:{ready+=1},failed:{failures.append($0)})}
    begin()
    assertTrue(sender.requests[0]["action"] as? String == "prepare","prepare first")
    let requestID=sender.requests[0]["requestId"] as? String ?? ""
    assertTrue(UUID(uuidString:requestID) != nil && requestID==requestID.lowercased(),"canonical id")
    if mode=="success" {
        sender.reply(0,200,["ready":true,"lease":leaseID]);assertTrue(ready==0,"no termination before commit")
        assertTrue(sender.requests[1]["action"] as? String == "commit","commit second")
        sender.reply(1,200,["ready":true,"lease":leaseID,"expiresInSeconds":30]);assertTrue(ready==1 && failures.isEmpty,"commit authorizes termination")
        gate.cancel();assertTrue(sender.requests[2]["action"] as? String == "release","release known lease on cancel")
    } else if mode=="delayed-commit" {
        sender.reply(0,200,["ready":true,"lease":leaseID]);now=131
        sender.reply(1,200,["ready":true,"lease":leaseID,"expiresInSeconds":30])
        assertTrue(ready==0 && failures.count==1,"expired main queue callback refuses termination")
    } else if mode=="missing-expiry" {
        sender.reply(0,200,["ready":true,"lease":leaseID]);sender.reply(1,200,["ready":true,"lease":leaseID])
        assertTrue(ready==0 && failures.count==1,"unknown commit lifetime refuses termination")
    } else if mode=="busy" {
        sender.reply(0,409,["ready":false,"reason":"model-switch"])
        assertTrue(ready==0 && failures.count==1 && sender.requests.count==1,"busy leaves collector running")
    } else if mode=="bad-prepare" {
        sender.reply(0,200,["ready":true,"lease":"not-a-uuid"])
        assertTrue(ready==0 && failures.count==1 && sender.requests.count==1,"malformed prepare fails closed")
    } else if mode=="failed-commit" {
        sender.reply(0,200,["ready":true,"lease":leaseID]);sender.reply(1,409,["ready":false])
        assertTrue(ready==0 && failures.count==1,"expired commit refuses termination")
        assertTrue(sender.requests[2]["action"] as? String == "release","failed commit releases lease")
    } else if mode=="wrong-lease" {
        sender.reply(0,200,["ready":true,"lease":leaseID]);sender.reply(1,200,["ready":true,"lease":fixtureID])
        assertTrue(ready==0 && failures.count==1,"mismatched commit refuses termination")
    } else if mode=="cancelled-prepare" {
        gate.cancel();sender.reply(0,200,["ready":true,"lease":leaseID])
        assertTrue(ready==0 && failures.isEmpty,"cancelled attempt does not terminate")
        assertTrue(sender.requests[1]["action"] as? String == "release","late prepare lease released")
    } else if mode=="cancelled-commit" {
        sender.reply(0,200,["ready":true,"lease":leaseID]);gate.cancel();sender.reply(1,200,["ready":true,"lease":leaseID])
        assertTrue(ready==0 && failures.isEmpty,"late commit cannot terminate cancelled attempt")
        assertTrue(sender.requests[2]["action"] as? String == "release","cancelled commit releases lease")
    } else if mode=="network-failure" {
        sender.reply(0,0,nil);assertTrue(ready==0 && failures.count==1,"network error fails closed")
    } else if mode=="replaced-attempt" {
        begin();sender.reply(0,200,["ready":true,"lease":leaseID])
        assertTrue(ready==0 && failures.isEmpty,"old attempt has no effect")
        assertTrue(sender.requests[2]["action"] as? String == "release","old lease released")
    } else {exit(2)}
}
print("passed "+mode)
"""
        )
        cls.binary = compile_swift(runner.read_text())

    def run_case(self, name):
        subprocess.run(
            [str(self.binary), name], check=True, capture_output=True, text=True, timeout=5
        )

    def test_quiet_install_waits_for_a_quiet_moment(self):
        self.run_case('quiet-policy')

    def test_relaunch_restores_window_without_taking_focus(self):
        self.run_case('relaunch-window')

    def test_quiet_probe_never_keeps_a_reservation(self):
        self.run_case('probe')

    def test_automatic_admission_sends_soft_hold_flag(self):
        self.run_case('automatic-admission')

    def test_bridge_accepts_only_explicit_strict_actions(self):
        self.run_case('bridge')

    def test_bridge_rejects_non_native_origins_and_subframes(self):
        self.run_case('origin')

    def test_termination_requires_committed_matching_lease(self):
        self.run_case('success')

    def test_busy_model_prevents_termination(self):
        self.run_case('busy')

    def test_delayed_commit_callback_cannot_terminate(self):
        self.run_case('delayed-commit')

    def test_commit_without_expiry_fails_closed(self):
        self.run_case('missing-expiry')

    def test_malformed_prepare_fails_closed(self):
        self.run_case('bad-prepare')

    def test_expired_commit_releases_without_termination(self):
        self.run_case('failed-commit')

    def test_wrong_commit_lease_fails_closed(self):
        self.run_case('wrong-lease')

    def test_cancelled_prepare_releases_late_lease(self):
        self.run_case('cancelled-prepare')

    def test_cancelled_commit_cannot_terminate(self):
        self.run_case('cancelled-commit')

    def test_network_failure_preserves_collector(self):
        self.run_case('network-failure')

    def test_old_attempt_cannot_terminate_new_attempt(self):
        self.run_case('replaced-attempt')


if __name__ == '__main__':
    unittest.main()
