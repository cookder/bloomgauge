import Foundation

@main struct PermissionTests {
    static func main() throws {
        let id="af18c4a6-5ef1-4bb4-a562-48fa0451db24"
        assert(BloomCachePermissionCommand(["action":"cache-setup","requestId":id]) != nil)
        assert(BloomCachePermissionCommand(["action":"cache-remove","requestId":id]) != nil)
        for body in [["action":"purge","requestId":id],["action":"cache-setup","requestId":"bad"],
                     ["action":"cache-setup","requestId":id,"command":"/bin/sh"],["action":"cache-setup"]] {
            assert(BloomCachePermissionCommand(body) == nil)
        }
        for user in ["", "root;whoami", "$(id)", "user\nname", "'quoted'", "a b", "üser"] {
            assert(bloomCachePermissionScript(action:"cache-setup",user:user,uid:501) == nil)
        }
        assert(bloomCachePermissionScript(action:"cache-setup",user:"fixture",uid:0) == nil)
        assert(bloomCachePermissionScript(action:"purge",user:"fixture",uid:501) == nil)
        for action in ["cache-setup","cache-remove"] {
            let source=bloomCachePermissionScript(action:action,user:"fixture_user",uid:501)!
            assert(source.hasSuffix(" with administrator privileges"))
            assert(source.contains("/usr/bin/env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin LC_ALL=C"))
            assert(!source.contains(" password \""))
            var error:NSDictionary?
            let script=NSAppleScript(source:source)!
            let compiled=script.compileAndReturnError(&error)
            if !compiled { print("Compile error:",error ?? [:]); exit(1) }
            assert(error == nil)
        }
        // Exercise both quoting layers using a return-only AppleScript. The
        // command text is returned as data and is NEVER executed.
        let value="apostrophe' quote\" backtick` dollar$(id) slash\\ newline\nend"
        var error:NSDictionary?
        let result=NSAppleScript(source:"return " + bloomCacheAppleScriptString(value))!.executeAndReturnError(&error)
        assert(error == nil && result.stringValue == value)
        let proc=Process(),out=Pipe();proc.executableURL=URL(fileURLWithPath:"/bin/sh")
        proc.arguments=["-c","/usr/bin/printf '%s' " + bloomCacheShellQuote(value)];proc.standardOutput=out
        try proc.run();proc.waitUntilExit()
        assert(proc.terminationStatus == 0)
        assert(String(data:out.fileHandleForReading.readDataToEndOfFile(),encoding:.utf8)==value)
        assert(bloomCacheSetupScript.contains("NOPASSWD: /usr/sbin/purge \"\""))
        assert(bloomCacheSetupScript.contains("/bin/ln \"$temp_file\" \"$policy_file\""))
        assert(bloomCacheRemoveScript.contains("!= \"$expected\""))
        print("Native permission tests passed: strict commands, account validation, AppleScript compile, quote round trips, exact rule. No privileged execution.")
    }
}
