import AppKit
import Sparkle
import CoreFoundation

// BEGIN update bridge policy (compiled directly by its regression test).
func isBloomUpdateFrame(_ url:URL?,localURL:URL?,isMainFrame:Bool,belongsToDashboard:Bool)->Bool {
    guard belongsToDashboard,isMainFrame,let url=url,let localURL=localURL,let port=localURL.port else{return false}
    return localURL.scheme=="http" && localURL.host=="127.0.0.1" &&
        url.scheme=="http" && url.host=="127.0.0.1" && url.port==port && url.user==nil && url.password==nil
}
final class BloomLocalUpdateSessionDelegate:NSObject,URLSessionTaskDelegate {
    func urlSession(_ session:URLSession,task:URLSessionTask,willPerformHTTPRedirection response:HTTPURLResponse,
                    newRequest request:URLRequest,completionHandler:@escaping (URLRequest?)->Void){completionHandler(nil)}
}
struct BloomUpdateBridgeCommand {
    let action: String
    let requestId: String
    let enabled: Bool?
    init?(_ value: Any) {
        guard let body = value as? [String: Any], let action = body["action"] as? String,
              ["status", "check", "set-automatic"].contains(action),
              let requestId = body["requestId"] as? String, UUID(uuidString: requestId) != nil else { return nil }
        let keys = Set(body.keys)
        if action == "set-automatic" {
            guard keys == Set(["action", "requestId", "enabled"]), let value = body["enabled"],
                  CFGetTypeID(value as CFTypeRef) == CFBooleanGetTypeID(), let enabled = value as? Bool else { return nil }
            self.enabled = enabled
        } else {
            guard keys == Set(["action", "requestId"]) else { return nil }
            self.enabled = nil
        }
        self.action = action; self.requestId = requestId
    }
}

// A short, native-only backend lease closes the gap between checking whether a
// model command is active and asking the collector to exit. This class performs
// no provider commands and never changes optimizer mode.
final class BloomUpdateAdmission {
    typealias Send = ([String: Any], @escaping (Int, [String: Any]?) -> Void) -> Void
    private let send: Send
    private let clock: () -> TimeInterval
    private var attempt: UUID?
    private var lease: String?
    init(send: @escaping Send, clock: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime }) { self.send = send; self.clock = clock }
    func begin(ready: @escaping () -> Void, failed: @escaping (String) -> Void) {
        cancel()
        let id = UUID(); attempt = id
        send(["action": "prepare", "requestId": id.uuidString.lowercased()]) { [weak self] status, body in
            guard let self = self else { return }
            let candidate = body?["lease"] as? String
            guard self.attempt == id else {
                if let candidate = candidate, UUID(uuidString: candidate) != nil { self.release(candidate) }
                return
            }
            guard status == 200, body?["ready"] as? Bool == true,
                  let token = candidate, UUID(uuidString: token) != nil else {
                self.cancel()
                failed(status == 409 ? "Bloomkeeper is switching or warming a model. Wait for it to finish, then try Install and Relaunch again. Your current work and optimizer settings have not changed." : "Bloomkeeper could not confirm that an update is safe right now. Let the dashboard reconnect, then try Install and Relaunch again.")
                return
            }
            self.lease = token
            let commitStarted = self.clock()
            self.send(["action": "commit", "lease": token]) { [weak self] status, body in
                guard let self = self, self.attempt == id else { return }
                let elapsed = self.clock() - commitStarted
                let expiry = body?["expiresInSeconds"] as? NSNumber
                guard status == 200, body?["ready"] as? Bool == true,
                      body?["lease"] as? String == token,
                      let expiry = expiry, CFGetTypeID(expiry) != CFBooleanGetTypeID(),
                      expiry.doubleValue.isFinite, expiry.doubleValue > 1, expiry.doubleValue <= 30,
                      elapsed.isFinite, elapsed >= 0, elapsed < expiry.doubleValue - 1 else {
                    self.cancel(); failed("The update safety check expired or failed. Try Install and Relaunch again. No model was stopped."); return
                }
                ready()
            }
        }
    }
    func cancel() {
        attempt = nil
        if let token = lease { lease = nil; release(token) }
    }
    private func release(_ token: String) { send(["action": "release", "lease": token]) { _, _ in } }
}
// END update bridge policy.

// Sparkle owns consent, skip/remind-later state and installation approval.
// No second checker, custom telemetry, or automatic installation is introduced.
final class BloomUpdates: NSObject, SPUUpdaterDelegate, NSMenuItemValidation {
    private(set) var controller: SPUStandardUpdaterController!
    var changed: (() -> Void)?
    var cancelledInstallation: (() -> Void)?
    private var installationRequested = false
    private var status = "idle"
    private var errorMessage: String?
    override init() {
        super.init()
        controller = SPUStandardUpdaterController(startingUpdater: false, updaterDelegate: self, userDriverDelegate: nil)
        // Only the app version, so bloomformac.com can count Macs per version; no other detail.
        let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "unknown"
        controller.updater.userAgentString = "Bloomkeeper-Updater/\(version)"
    }
    func start() { controller.startUpdater() }
    func addMenuItems(to menu: NSMenu) {
        let check = menu.addItem(withTitle: "Check for Updates…", action: #selector(checkNow(_:)), keyEquivalent: "")
        check.target = self
        let automatic = menu.addItem(withTitle: "Automatically Check for Updates", action: #selector(toggleAutomaticChecks(_:)), keyEquivalent: "")
        automatic.target = self
    }
    @objc func checkNow(_ sender: Any?) {
        guard controller.updater.canCheckForUpdates else { changed?(); return }
        status = "checking"; errorMessage = nil; changed?()
        controller.checkForUpdates(sender)
    }
    func setAutomaticChecks(_ enabled: Bool) {
        controller.updater.automaticallyChecksForUpdates = enabled
        errorMessage = nil; changed?()
    }
    @objc func toggleAutomaticChecks(_ sender: NSMenuItem) {
        setAutomaticChecks(!controller.updater.automaticallyChecksForUpdates)
        sender.state = controller.updater.automaticallyChecksForUpdates ? .on : .off
    }
    func validateMenuItem(_ menuItem: NSMenuItem) -> Bool {
        if menuItem.action == #selector(checkNow(_:)) { return controller.updater.canCheckForUpdates }
        menuItem.state = controller.updater.automaticallyChecksForUpdates ? .on : .off
        return true
    }
    func snapshot(requestId: String? = nil) -> [String: Any] {
        ["requestId": requestId as Any? ?? NSNull(), "available": true,
         "installedVersion": Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "Unknown",
         "automaticChecks": controller.updater.automaticallyChecksForUpdates,
         "canCheck": controller.updater.canCheckForUpdates,
         "checking": controller.updater.sessionInProgress,
         "lastCheck": controller.updater.lastUpdateCheckDate?.timeIntervalSince1970 as Any? ?? NSNull(),
         "status": status, "error": errorMessage as Any? ?? NSNull()]
    }
    // Sparkle can retry a postponed termination without another willInstall
    // callback. Keep this armed while its already-approved update is pending.
    func requiresSafeTermination() -> Bool { installationRequested }
    func installationBlocked(_ message: String) {
        status = "blocked"; errorMessage = message; changed?()
    }
    func feedParameters(for updater: SPUUpdater, sendingSystemProfile: Bool) -> [[String: String]] { [] }
    func allowedSystemProfileKeys(for updater: SPUUpdater) -> [String]? { [] }
    func updater(_ updater: SPUUpdater, didFindValidUpdate item: SUAppcastItem) {
        status = "update-available"; errorMessage = nil; changed?()
    }
    func updaterDidNotFindUpdate(_ updater: SPUUpdater, error: Error) {
        status = "up-to-date"; errorMessage = nil; changed?()
    }
    func updater(_ updater: SPUUpdater, willInstallUpdate item: SUAppcastItem) {
        installationRequested = true
    }
    func updater(_ updater: SPUUpdater, didAbortWithError error: Error) {
        if status != "blocked" { installationRequested = false }
        cancelledInstallation?()
        if status != "up-to-date" && status != "blocked" {
            status = "error"; errorMessage = "The update could not complete. Check your connection or try Check now again."
        }
        changed?()
    }
    func userDidCancelDownload(_ updater: SPUUpdater) {
        installationRequested = false; cancelledInstallation?()
        status = "idle"; errorMessage = nil; changed?()
    }
    func updater(_ updater: SPUUpdater, didFinishUpdateCycleFor updateCheck: SPUUpdateCheck, error: Error?) {
        if error != nil {
            if status != "blocked" { installationRequested = false }
            cancelledInstallation?()
        }
        if status == "checking" { status = error == nil ? "idle" : "error" }
        changed?()
    }
}
