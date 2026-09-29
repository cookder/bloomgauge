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
    /// automatic: a quiet install also waits for trials and recoveries (update_guard soft holds).
    /// requestId: a quiet install reuses its probe's request, so the reservation the probe
    /// took (prepare is idempotent per request) covers the whole way to termination.
    func begin(automatic: Bool = false, requestId: String? = nil, ready: @escaping () -> Void, failed: @escaping (String) -> Void) {
        cancel()
        let id = UUID(); attempt = id
        let request = requestId.flatMap { UUID(uuidString: $0) != nil && $0 == $0.lowercased() ? $0 : nil } ?? id.uuidString.lowercased()
        var prepare: [String: Any] = ["action": "prepare", "requestId": request]
        if automatic { prepare["automatic"] = true }
        send(prepare) { [weak self] status, body in
            guard let self = self else { return }
            let candidate = body?["lease"] as? String
            guard self.attempt == id else {
                if let candidate = candidate, UUID(uuidString: candidate) != nil { self.release(candidate) }
                return
            }
            guard status == 200, body?["ready"] as? Bool == true,
                  let token = candidate, UUID(uuidString: token) != nil else {
                self.cancel()
                failed(status == 409 ? "BloomGauge is switching or warming a model. Wait for it to finish, then install the update again. Your current work and optimizer settings have not changed." : "BloomGauge could not confirm that an update is safe right now. Let the dashboard reconnect, then install the update again.")
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
                    self.cancel(); failed("The update safety check expired or failed. Install the update again. No model was stopped."); return
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

// Quiet installs (automatic updates are on by default). Sparkle downloads an update
// in the background and hands BloomGauge an install-now block; BloomGauge uses it
// only at a quiet moment, so the relaunch never interrupts the person or a model change.
enum BloomQuietInstallPolicy {
    static let settleSeconds: TimeInterval = 120        // after the download is ready
    static let afterLaunchSeconds: TimeInterval = 600   // let the collector and model settle
    static let userIdleSeconds: TimeInterval = 600      // no keyboard, mouse or trackpad input
    static let impatientSeconds: TimeInterval = 86_400  // after a day: in the background is enough
    static let retrySeconds: TimeInterval = 300
    /// Waiting and idle times in seconds. Before a day has passed the person must have been
    /// away for ten minutes; after it, it is enough that BloomGauge is not the front app.
    static func due(waiting: TimeInterval, sinceLaunch: TimeInterval, userIdle: TimeInterval, appActive: Bool) -> Bool {
        guard waiting.isFinite, sinceLaunch.isFinite, userIdle.isFinite,
              waiting >= settleSeconds, sinceLaunch >= afterLaunchSeconds else { return false }
        return userIdle >= userIdleSeconds || (!appActive && waiting >= impatientSeconds)
    }
    /// Soft holds (excursions, trials, recoveries) apply for the first day only, so a state
    /// that never clears cannot keep a Mac on an old version.
    static func automatic(waiting: TimeInterval) -> Bool { !(waiting >= impatientSeconds) }
}

/// A granted quiet-install probe: the collector's reservation (at most a minute) that
/// keeps new model work from starting until the install finishes or is released.
struct BloomQuietProbe { let requestId: String; let lease: String }
/// Asks the collector whether a quiet install could start now. When it can, the
/// reservation is kept and handed to the termination admission (same requestId).
func bloomProbeQuietInstall(send: @escaping BloomUpdateAdmission.Send, automatic: Bool, done: @escaping (BloomQuietProbe?) -> Void) {
    let request = UUID().uuidString.lowercased()
    var body: [String: Any] = ["action": "prepare", "requestId": request]
    if automatic { body["automatic"] = true }
    send(body) { status, reply in
        guard status == 200, reply?["ready"] as? Bool == true,
              let lease = reply?["lease"] as? String, UUID(uuidString: lease) != nil else { done(nil); return }
        done(BloomQuietProbe(requestId: request, lease: lease))
    }
}
func bloomReleaseQuietProbe(_ probe: BloomQuietProbe, send: BloomUpdateAdmission.Send) {
    send(["action": "release", "lease": probe.lease]) { _, _ in }
}

/// How the window comes back after a quiet relaunch: as it was, never stealing focus.
/// quit: the person quit BloomGauge while Sparkle's installer was waiting to relaunch
/// it, so the updated app closes again at once.
enum BloomRelaunchWindow: String { case front, back, hidden, quit }
let quietRelaunchDefaultsKey = "BloomQuietRelaunch"
func bloomQuietRelaunchRecord(windowVisible: Bool, appActive: Bool, now: TimeInterval, frontApp: String? = nil) -> [String: Any] {
    var record: [String: Any] = ["at": now, "window": (windowVisible ? (appActive ? BloomRelaunchWindow.front : .back) : .hidden).rawValue]
    if !appActive, let front = frontApp, !front.isEmpty, front.count <= 255 { record["frontApp"] = front }
    return record
}
func bloomQuitRelaunchRecord(now: TimeInterval) -> [String: Any] { ["at": now, "window": BloomRelaunchWindow.quit.rawValue] }
/// The app that was in front when a quiet install began, to hand focus back to.
func bloomRelaunchFrontApp(_ saved: Any?) -> String? {
    guard let front = (saved as? [String: Any])?["frontApp"] as? String, !front.isEmpty, front.count <= 255 else { return nil }
    return front
}
/// nil means an ordinary launch. A record counts for 15 minutes after it was saved; a quit
/// record (saved as the app exits) only for 90 seconds, since the installer relaunches
/// within seconds, so a person opening BloomGauge again later is never closed by it.
func bloomRelaunchWindow(_ saved: Any?, now: TimeInterval) -> BloomRelaunchWindow? {
    guard let record = saved as? [String: Any], let at = record["at"] as? Double, at.isFinite,
          now >= at, now - at <= 900, let value = record["window"] as? String,
          let window = BloomRelaunchWindow(rawValue: value) else { return nil }
    return window == .quit && now - at > 90 ? nil : window
}
// END update bridge policy.

// Sparkle owns checking, downloading, signatures and installation. Updates are
// automatic by default (Info.plist); the off switch sets both of Sparkle's saved
// preferences. A downloaded update waits for a quiet moment (App.swift) or for Quit.
// No second checker or custom telemetry is introduced.
final class BloomUpdates: NSObject, SPUUpdaterDelegate, NSMenuItemValidation {
    private(set) var controller: SPUStandardUpdaterController!
    var changed: (() -> Void)?
    var cancelledInstallation: (() -> Void)?
    /// Install Update Now in the app menu; App.swift runs it with the usual safety check.
    var installNowRequested: (() -> Void)?
    private var installationRequested = false
    private var status = "idle"
    private var errorMessage: String?
    private var quietInstall: (() -> Void)?
    /// Wall-clock time the downloaded update started waiting (sleep counts toward a day).
    private(set) var waitingSince: TimeInterval?
    private weak var installNowItem: NSMenuItem?
    override init() {
        super.init()
        controller = SPUStandardUpdaterController(startingUpdater: false, updaterDelegate: self, userDriverDelegate: nil)
        // Only the app version, so bloomformac.com can count Macs per version; no other detail.
        let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "unknown"
        controller.updater.userAgentString = "BloomGauge-Updater/\(version)"
    }
    func start() { controller.startUpdater() }
    func addMenuItems(to menu: NSMenu) {
        let check = menu.addItem(withTitle: "Check for Updates…", action: #selector(checkNow(_:)), keyEquivalent: "")
        check.target = self
        let now = menu.addItem(withTitle: "Install Update Now", action: #selector(installNow(_:)), keyEquivalent: "")
        now.target = self; now.isHidden = true; installNowItem = now
        let automatic = menu.addItem(withTitle: "Update Automatically", action: #selector(toggleAutomaticChecks(_:)), keyEquivalent: "")
        automatic.target = self
    }
    var automaticUpdates: Bool {
        controller.updater.automaticallyChecksForUpdates && controller.updater.automaticallyDownloadsUpdates
    }
    var quietInstallWaiting: Bool { quietInstall != nil }
    /// Sparkle's installer was told to install and relaunch and is waiting for the app to
    /// quit. It stays armed until the update installs or Sparkle aborts.
    private(set) var installerArmed = false
    /// Starts the downloaded update's install and relaunch. App.swift decides when.
    func installQuietly() {
        guard let install = quietInstall else { return }
        // Sparkle sends willInstallUpdate only the first time; arm the safe termination
        // on every attempt so a retry never becomes an ordinary quit.
        installationRequested = true; installerArmed = true
        status = "installing"; errorMessage = nil; changed?()
        install()
    }
    @objc func installNow(_ sender: Any?) { installNowRequested?() }
    /// A quiet attempt could not finish. Disarm, so a later Quit is an ordinary quit.
    func quietInstallPostponed() {
        installationRequested = false
        status = quietInstall == nil ? "idle" : "ready-to-install"; errorMessage = nil; changed?()
    }
    @objc func checkNow(_ sender: Any?) {
        guard controller.updater.canCheckForUpdates else { changed?(); return }
        status = "checking"; errorMessage = nil; changed?()
        controller.checkForUpdates(sender)
    }
    /// The one switch: on = check, download and install automatically; off = none of these.
    /// An update already downloaded stays ready: it installs when the app quits or with
    /// Install Update Now, and quietly again if the switch is turned back on.
    func setAutomaticChecks(_ enabled: Bool) {
        controller.updater.automaticallyChecksForUpdates = enabled
        controller.updater.automaticallyDownloadsUpdates = enabled
        errorMessage = nil; changed?()
    }
    @objc func toggleAutomaticChecks(_ sender: NSMenuItem) {
        setAutomaticChecks(!automaticUpdates)
        sender.state = automaticUpdates ? .on : .off
    }
    func validateMenuItem(_ menuItem: NSMenuItem) -> Bool {
        if menuItem.action == #selector(checkNow(_:)) { return controller.updater.canCheckForUpdates }
        if menuItem.action == #selector(installNow(_:)) { return quietInstall != nil }
        menuItem.state = automaticUpdates ? .on : .off
        return true
    }
    func snapshot(requestId: String? = nil) -> [String: Any] {
        ["requestId": requestId as Any? ?? NSNull(), "available": true,
         "installedVersion": Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "Unknown",
         "automaticChecks": controller.updater.automaticallyChecksForUpdates,
         "automaticUpdates": automaticUpdates,
         "canCheck": controller.updater.canCheckForUpdates,
         // A downloaded update holds Sparkle's session open; that is not a check.
         "checking": controller.updater.sessionInProgress && quietInstall == nil,
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
        installationRequested = true; installerArmed = true
    }
    // Downloaded in the background: keep Sparkle's install-now block for a quiet moment.
    // Returning true pauses further checks until it installs (now, or when the app quits).
    func updater(_ updater: SPUUpdater, willInstallUpdateOnQuit item: SUAppcastItem, immediateInstallationBlock immediateInstallHandler: @escaping () -> Void) -> Bool {
        quietInstall = immediateInstallHandler
        if waitingSince == nil { waitingSince = Date().timeIntervalSince1970 }
        installNowItem?.isHidden = false
        status = "ready-to-install"; errorMessage = nil; changed?()
        return true
    }
    private func clearQuietInstall() { quietInstall = nil; waitingSince = nil; installerArmed = false; installNowItem?.isHidden = true }
    func updater(_ updater: SPUUpdater, didAbortWithError error: Error) {
        clearQuietInstall()
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
            clearQuietInstall()
            if status != "blocked" { installationRequested = false }
            cancelledInstallation?()
        }
        if status == "checking" { status = error == nil ? "idle" : "error" }
        changed?()
    }
}
