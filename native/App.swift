import AppKit
import WebKit
import ServiceManagement
import UniformTypeIdentifiers
import UserNotifications

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKScriptMessageHandler, NSWindowDelegate {
    var window: NSWindow!
    var webView: WKWebView!
    var collector: Process?
    var pipe: Pipe?
    var message: NSTextField!
    var localURL: URL?
    var starting = true
    var statusItem: NSStatusItem?
    var quitting = false
    var restartTimes: [TimeInterval] = []
    var diagnosticsSaveOpen = false
    var cachePermissionOpen = false
    var cachePermissionReceipts: [String:String] = [:]
    var updates: BloomUpdates?
    var pendingTermination = false
    var updateAdmission: BloomUpdateAdmission?
    var terminationAttempt: UUID?
    var updateTermination = false
    // Quiet installs of automatically downloaded updates (see BloomQuietInstallPolicy).
    var launchUptime: TimeInterval = 0
    var quietTimer: Timer?
    var quietProbeInFlight = false
    var quietRetryAt: TimeInterval = 0
    /// A quiet install that asked Sparkle to quit the app: when (systemUptime), whether it
    /// carries the automatic soft holds, and the probe's reservation it reuses. Only the
    /// first termination request within a few seconds counts as that install.
    var quietPending: (at: TimeInterval, automatic: Bool, probe: BloomQuietProbe)?
    /// The termination in progress is a quiet install (never a person's Quit).
    var quietAttempt = false
    /// The waiting update's install was last started by the quiet path, not by a person.
    var quietIntent = false
    /// A person quit while Sparkle's installer waited to relaunch: mark the relaunch to close.
    var quitRecordOnExit = false
    var relaunchFocusObserver: NSObjectProtocol?
    var admissionSend: BloomUpdateAdmission.Send {
        {[weak self] body,completion in
            guard let self=self else{completion(0,nil);return}
            self.updateAdmissionRequest(body,completion:completion)
        }
    }
    /// Releases a held quiet-install reservation so model changes are not blocked by it.
    func dropQuietProbe(){
        if let pending=quietPending {bloomReleaseQuietProbe(pending.probe,send:admissionSend)}
        quietPending=nil
    }
    /// Sparkle's installer asks the app to quit with a quit Apple event sent by its Updater
    /// helper. Menu, Dock and logout quits come from elsewhere: those are a person's Quit.
    func sparkleQuitRequest()->Bool{
        guard let event=NSAppleEventManager.shared().currentAppleEvent,
              event.eventClass==AEEventClass(kCoreEventClass),event.eventID==AEEventID(kAEQuitApplication),
              let pid=event.attributeDescriptor(forKeyword:AEKeyword(keySenderPIDAttr))?.int32Value,pid>0,
              let sender=NSRunningApplication(processIdentifier:pid) else{return false}
        return sender.bundleIdentifier=="org.sparkle-project.Sparkle.Updater"
    }
    func endRelaunchFocusWatch(){
        if let observer=relaunchFocusObserver {NotificationCenter.default.removeObserver(observer);relaunchFocusObserver=nil}
    }
    /// Gives focus back to the app the person was using when a quiet install began.
    func handBackFocus(_ bundleIdentifier:String?){
        if let id=bundleIdentifier,id != Bundle.main.bundleIdentifier,
           let app=NSRunningApplication.runningApplications(withBundleIdentifier:id).first(where:{!$0.isTerminated}) {
            NSApp.yieldActivation(to:app)
            if app.activate(from:NSRunningApplication.current,options:[]) {return}
        }
        NSApp.deactivate()
    }
    #if BLOOM_UPDATE_TESTING
    // Only the separately compiled updater test app uses this. Its collector
    // stays in preview mode even after Sparkle relaunches without arguments.
    let setupPreview = true
    #else
    let setupPreview = ProcessInfo.processInfo.arguments.contains("--setup-preview")
    #endif
    let nativeToken = UUID().uuidString + UUID().uuidString
    // Sent only by this window, as an HttpOnly cookie: other local programs and
    // accounts can read the dashboard but cannot change anything.
    let sessionToken = UUID().uuidString + UUID().uuidString
    var reputationConnection: ReputationConnection?
    var notificationRelay: BloomNotificationRelay?
    /// A notification clicked before the local service was up: its screen opens with the first load.
    var pendingScreen: String?
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        // The dashboard's Appearance choice (System, Light or Dark), saved when the
        // page last reported it, so the window opens in the right colours.
        NSApp.appearance = bloomAppearanceName(UserDefaults.standard.string(forKey:appearanceDefaultsKey)).flatMap{NSAppearance(named:$0)}
        #if BLOOM_UPDATE_TESTING
        if Bundle.main.bundleIdentifier?.hasPrefix("local.bloom.dashboard.updater-test.") == true { updates = BloomUpdates() }
        #else
        if !setupPreview && !ProcessInfo.processInfo.arguments.contains("--disable-updates") && ProcessInfo.processInfo.environment["BLOOM_DISABLE_UPDATES"] != "1" && Bundle.main.object(forInfoDictionaryKey:"BloomReleaseChannel") as? String == "beta" {
            updates = BloomUpdates()
        }
        #endif
        launchUptime = ProcessInfo.processInfo.systemUptime
        updates?.changed = { [weak self] in self?.publishUpdateStatus() }
        updates?.cancelledInstallation = { [weak self] in self?.cancelUpdateTermination() }
        // A person's Install Update Now takes the ordinary path: safety check, and a
        // "postponed" alert if a model change is running.
        updates?.installNowRequested = { [weak self] in
            guard let self=self,!self.pendingTermination,!self.quitting else{return}
            self.quietIntent=false;self.dropQuietProbe();self.updates?.installQuietly()
        }
        if updates != nil {
            quietTimer = Timer.scheduledTimer(withTimeInterval:60,repeats:true){[weak self] _ in self?.quietInstallTick()}
        }
        let menu = NSMenu()
        let appItem = NSMenuItem(); menu.addItem(appItem)
        let appMenu = NSMenu(); appItem.submenu = appMenu
        appMenu.addItem(withTitle: "About BloomGauge", action: #selector(about), keyEquivalent: "")
        if let updates = updates { updates.addMenuItems(to:appMenu) }
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle:"Open at Login",action:#selector(toggleLogin(_:)),keyEquivalent:"")
        appMenu.item(withTitle:"Open at Login")?.state = !setupPreview && SMAppService.mainApp.status == .enabled ? .on : .off
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle:"Hide BloomGauge",action:#selector(NSApplication.hide(_:)),keyEquivalent:"h")
        let hideOthers=appMenu.addItem(withTitle:"Hide Others",action:#selector(NSApplication.hideOtherApplications(_:)),keyEquivalent:"h")
        hideOthers.keyEquivalentModifierMask=[.command,.option]
        appMenu.addItem(withTitle:"Show All",action:#selector(NSApplication.unhideAllApplications(_:)),keyEquivalent:"")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit BloomGauge", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        let editItem = NSMenuItem(); menu.addItem(editItem); let edit = NSMenu(title:"Edit"); editItem.submenu=edit
        edit.addItem(withTitle:"Undo",action:Selector(("undo:")),keyEquivalent:"z")
        let redo=edit.addItem(withTitle:"Redo",action:Selector(("redo:")),keyEquivalent:"z");redo.keyEquivalentModifierMask=[.command,.shift]
        edit.addItem(.separator())
        edit.addItem(withTitle:"Cut",action:#selector(NSText.cut(_:)),keyEquivalent:"x")
        edit.addItem(withTitle:"Copy",action:#selector(NSText.copy(_:)),keyEquivalent:"c")
        edit.addItem(withTitle:"Paste",action:#selector(NSText.paste(_:)),keyEquivalent:"v")
        edit.addItem(withTitle:"Select All",action:#selector(NSText.selectAll(_:)),keyEquivalent:"a")
        let viewItem=NSMenuItem();menu.addItem(viewItem);let view=NSMenu(title:"View");viewItem.submenu=view
        view.addItem(withTitle:"Reload Dashboard",action:#selector(reload),keyEquivalent:"r")
        view.addItem(withTitle:"Zoom In",action:#selector(zoomIn),keyEquivalent:"+")
        view.addItem(withTitle:"Zoom Out",action:#selector(zoomOut),keyEquivalent:"-")
        view.addItem(withTitle:"Actual Size",action:#selector(zoomReset),keyEquivalent:"0")
        let windowItem=NSMenuItem();menu.addItem(windowItem);let windowMenu=NSMenu(title:"Window");windowItem.submenu=windowMenu
        windowMenu.addItem(withTitle:"Close Window",action:#selector(NSWindow.performClose(_:)),keyEquivalent:"w")
        windowMenu.addItem(withTitle:"Open Dashboard",action:#selector(showDashboard),keyEquivalent:"")
        NSApp.windowsMenu=windowMenu
        let helpItem=NSMenuItem();menu.addItem(helpItem);let help=NSMenu(title:"Help");helpItem.submenu=help
        help.addItem(withTitle:"Help & Feedback…",action:#selector(openSupport),keyEquivalent:"")
        NSApp.mainMenu = menu
        statusItem=NSStatusBar.system.statusItem(withLength:NSStatusItem.variableLength)
        statusItem?.button?.image=NSImage(systemSymbolName:"leaf",accessibilityDescription:"BloomGauge")
        let statusMenu=NSMenu()
        statusMenu.addItem(withTitle:"Open BloomGauge",action:#selector(showDashboard),keyEquivalent:"")
        statusMenu.addItem(withTitle:"Monitoring continues when this window closes",action:nil,keyEquivalent:"")
        statusMenu.addItem(.separator())
        statusMenu.addItem(withTitle:"Quit BloomGauge",action:#selector(NSApplication.terminate(_:)),keyEquivalent:"")
        statusItem?.menu=statusMenu
        window=NSWindow(contentRect:NSRect(x:0,y:0,width:1280,height:910),styleMask:[.titled,.closable,.miniaturizable,.resizable],backing:.buffered,defer:false)
        window.title=Bundle.main.object(forInfoDictionaryKey:"CFBundleDisplayName") as? String ?? "BloomGauge"
        window.delegate=self
        window.isReleasedWhenClosed=false
        window.minSize=NSSize(width:700,height:540)
        window.setFrameAutosaveName("BloomDashboardMainWindow")
        // Matches the page background of each theme until the dashboard paints.
        window.backgroundColor=NSColor(name:nil){appearance in
            appearance.bestMatch(from:[.darkAqua,.aqua]) == .darkAqua
                ? NSColor(calibratedRed:0.039,green:0.051,blue:0.071,alpha:1)
                : NSColor(calibratedRed:0.953,green:0.961,blue:0.973,alpha:1)
        }
        window.center()
        let configuration=WKWebViewConfiguration()
        // Dashboard preferences (layout, dismissed notices, graph style) persist across
        // launches in their own store: not the default store, which the Darkbloom
        // account view uses and Disconnect wipes. The per-launch session cookie is
        // replaced below before every load.
        configuration.websiteDataStore = WKWebsiteDataStore(forIdentifier:dashboardStoreID)
        configuration.userContentController.add(self,name:"bloomAccount")
        configuration.userContentController.add(self,name:"bloomDiagnostics")
        configuration.userContentController.add(self,name:"bloomUpdates")
        configuration.userContentController.add(self,name:"bloomAppearance")
        webView=WKWebView(frame:window.contentView!.bounds,configuration:configuration)
        webView.autoresizingMask=[.width,.height]
        webView.navigationDelegate=self
        webView.setValue(false,forKey:"drawsBackground")
        window.contentView!.addSubview(webView)
        message=NSTextField(labelWithString:"Connecting to your Mac and Darkbloom…")
        message.textColor = .secondaryLabelColor;message.alignment = .center
        message.frame=NSRect(x:40,y:window.contentView!.bounds.midY,width:window.contentView!.bounds.width-80,height:60)
        message.autoresizingMask=[.width,.minYMargin,.maxYMargin]
        window.contentView!.addSubview(message)
        // After a quiet update the window comes back the way it was, without taking focus.
        let saved=UserDefaults.standard.object(forKey:quietRelaunchDefaultsKey)
        let relaunch=bloomRelaunchWindow(saved,now:Date().timeIntervalSince1970)
        UserDefaults.standard.removeObject(forKey:quietRelaunchDefaultsKey)
        // The person quit while an update waited; Sparkle installed it and relaunched. Stay closed.
        if relaunch == .quit {DispatchQueue.main.async{NSApp.terminate(nil)};return}
        switch relaunch {
        case .hidden?: break
        case .back?: window.orderBack(nil)
        default: window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps:true)
        }
        // macOS activates a relaunched app, sometimes a moment after launch. Hand focus back
        // to what the person was using, once, within the first few seconds.
        if relaunch == .hidden || relaunch == .back {
            let front=bloomRelaunchFrontApp(saved)
            if NSApp.isActive {handBackFocus(front)}
            else {
                relaunchFocusObserver=NotificationCenter.default.addObserver(forName:NSApplication.didBecomeActiveNotification,object:nil,queue:.main){[weak self] _ in
                    self?.handBackFocus(front);self?.endRelaunchFocusWatch()
                }
                // Only the relaunch's own activation; a person opening the window later keeps it.
                DispatchQueue.main.asyncAfter(deadline:.now()+4){[weak self] in self?.endRelaunchFocusWatch()}
            }
        }
        // Set before launch finishes, so a click that launched the app still opens its screen.
        if !setupPreview {UNUserNotificationCenter.current().delegate=self}
        startCollector()
        updates?.start()
    }
    /// Once a minute while a downloaded update waits: install it if this is a quiet moment
    /// and the collector confirms nothing is switching, warming or on a trial.
    func quietInstallTick(){
        // Sparkle never asked to quit after the last attempt (installer failure): start over.
        if let pending=quietPending,!pendingTermination,ProcessInfo.processInfo.systemUptime-pending.at>20 {postponeQuietInstall()}
        guard let updates=updates,updates.automaticUpdates,updates.quietInstallWaiting,let since=updates.waitingSince,quietPending==nil,
              !pendingTermination,!quitting,!quietProbeInFlight,!cachePermissionOpen,!diagnosticsSaveOpen,
              collector?.isRunning==true else{return}
        let now=Date().timeIntervalSince1970
        guard now>=quietRetryAt else{return}
        let waiting=now-since
        let idle=CGEventSource.secondsSinceLastEventType(.combinedSessionState,eventType:CGEventType(rawValue:~0)!)
        guard BloomQuietInstallPolicy.due(waiting:waiting,sinceLaunch:ProcessInfo.processInfo.systemUptime-launchUptime,
                                          userIdle:idle,appActive:NSApp.isActive) else{return}
        let automatic=BloomQuietInstallPolicy.automatic(waiting:waiting)
        let send:BloomUpdateAdmission.Send={[weak self] body,completion in
            guard let self=self else{completion(0,nil);return}
            self.updateAdmissionRequest(body,completion:completion)
        }
        quietProbeInFlight=true
        bloomProbeQuietInstall(send:send,automatic:automatic){[weak self] probe in
            guard let self=self else{if let probe=probe{bloomReleaseQuietProbe(probe,send:send)};return}
            self.quietProbeInFlight=false
            guard let probe=probe else{self.quietRetryAt=Date().timeIntervalSince1970+BloomQuietInstallPolicy.retrySeconds;return}
            guard !self.pendingTermination,!self.quitting,self.updates?.automaticUpdates==true,self.updates?.quietInstallWaiting==true else{
                bloomReleaseQuietProbe(probe,send:send)
                self.quietRetryAt=Date().timeIntervalSince1970+BloomQuietInstallPolicy.retrySeconds;return
            }
            UserDefaults.standard.set(bloomQuietRelaunchRecord(windowVisible:self.window.isVisible && !NSApp.isHidden,
                                                               appActive:NSApp.isActive,now:Date().timeIntervalSince1970,
                                                               frontApp:NSWorkspace.shared.frontmostApplication?.bundleIdentifier),
                                      forKey:quietRelaunchDefaultsKey)
            // The probe's reservation stays held (about a minute at most), so no model work
            // can start between here and the collector stopping.
            self.quietPending=(ProcessInfo.processInfo.systemUptime,automatic,probe)
            self.quietIntent=true
            self.updates?.installQuietly()
        }
    }
    /// A quiet attempt that could not finish: no alert, try again later, normal launch next time.
    func postponeQuietInstall(){
        dropQuietProbe();quietAttempt=false
        UserDefaults.standard.removeObject(forKey:quietRelaunchDefaultsKey)
        quietRetryAt=Date().timeIntervalSince1970+BloomQuietInstallPolicy.retrySeconds
        updates?.quietInstallPostponed()
    }
    func startCollector(){
        guard let resources=Bundle.main.resourceURL else { fail("App resources are missing.");return }
        // App-managed history belongs outside Documents. Do not probe the old
        // location at startup: migration is an explicit, one-time install step.
        let files=FileManager.default
        guard let support=files.urls(for:.applicationSupportDirectory,in:.userDomainMask).first else { fail("Could not locate app storage.");return }
        let dataName=Bundle.main.object(forInfoDictionaryKey:"BloomDataDirectory") as? String ?? "Bloom Dashboard"
        #if BLOOM_UPDATE_TESTING
        let dataDirectory=files.temporaryDirectory.appendingPathComponent("bloom-update-test-\(Bundle.main.bundleIdentifier ?? "isolated")",isDirectory:true)
        #else
        let dataDirectory=setupPreview ? files.temporaryDirectory.appendingPathComponent("bloom-setup-preview-\(ProcessInfo.processInfo.processIdentifier)",isDirectory:true) : support.appendingPathComponent(dataName,isDirectory:true)
        #endif
        do { try files.createDirectory(at:dataDirectory,withIntermediateDirectories:true,attributes:[.posixPermissions:0o700]) }
        catch { fail("Could not open BloomGauge's saved data: \(error.localizedDescription)");return }
        let process=Process();collector=process
        let python=resources.appendingPathComponent("python/bin/python3")
        guard files.isExecutableFile(atPath:python.path) else{fail("BloomGauge’s bundled runtime is missing. Reinstall the application; no separate Python installation is required.");return}
        process.executableURL=python
        process.arguments=["-I","-B","-u",resources.appendingPathComponent("runtime-entry.py").path,"--port",setupPreview ? "0":"8765","--remote-port",setupPreview ? "0":"8766","--static",resources.appendingPathComponent("web").path,"--data",dataDirectory.appendingPathComponent("history.sqlite3").path]
        if setupPreview{process.arguments?.append("--setup-preview")}
        process.currentDirectoryURL=dataDirectory
        process.environment=ProcessInfo.processInfo.environment.filter{!$0.key.hasPrefix("PYTHON") && !$0.key.hasPrefix("DYLD_")}.merging(["BLOOM_NATIVE_TOKEN":nativeToken,"BLOOM_SESSION_TOKEN":sessionToken,"PYTHONDONTWRITEBYTECODE":"1","SSL_CERT_FILE":resources.appendingPathComponent("push_vendor/certifi/cacert.pem").path]) { _, value in value }
        let output=Pipe();pipe=output;process.standardOutput=output
        process.standardError=FileHandle.nullDevice
        process.terminationHandler={ [weak self] _ in DispatchQueue.main.async { self?.collectorStopped() } }
        do{ try process.run() }catch{fail("Could not start the local collector: \(error.localizedDescription)");return}
        DispatchQueue.global().async { [weak self] in
            var bytes=Data()
            while bytes.count<4096{let next=output.fileHandleForReading.readData(ofLength:1);if next.isEmpty || next==Data([10]){break};bytes.append(next)}
            guard let object=try? JSONSerialization.jsonObject(with:bytes) as? [String:String],let value=object["url"],let url=URL(string:value) else {DispatchQueue.main.async { self?.fail("The local collector could not start.") };return}
            DispatchQueue.main.async {
                guard let self=self else{return}
                self.localURL=url
                if !self.setupPreview{
                    self.reputationConnection=ReputationConnection(baseURL:url,token:self.nativeToken);self.reputationConnection?.restore()
                    self.startNotificationRelay()
                }
                print("BloomGauge: \(url.absoluteString)");fflush(stdout)
                guard let cookie=bloomSessionCookie(url:url,token:self.sessionToken) else{self.fail("The local collector could not start.");return}
                self.webView.configuration.websiteDataStore.httpCookieStore.setCookie(cookie){[weak self] in
                    guard let self=self else{return}
                    let screen=self.pendingScreen.flatMap{bloomScreenURL(url,$0)};self.pendingScreen=nil
                    self.webView.load(URLRequest(url:screen ?? url))
                }
            }
        }
    }
    func fail(_ text:String){message.stringValue=text;message.isHidden=false}
    func collectorStopped(){
        guard !quitting else{return}
        reputationConnection?.refreshTimer?.invalidate()
        reputationConnection?.accountView?.stopLoading()
        reputationConnection?.accountWindow?.orderOut(nil);reputationConnection=nil
        notificationRelay?.stop();notificationRelay=nil
        let now=ProcessInfo.processInfo.systemUptime
        restartTimes=restartTimes.filter{now-$0<300}
        guard restartTimes.count<3 else{fail("BloomGauge could not keep its local service running. Another copy may already be open. Quit the extra copy, then reopen BloomGauge. Your saved history is intact.");showDashboard();return}
        restartTimes.append(now)
        fail("Reconnecting to BloomGauge’s local service…")
        DispatchQueue.main.asyncAfter(deadline:.now()+Double(restartTimes.count*3)){[weak self] in
            guard let self=self,!self.quitting else{return};self.startCollector()
        }
    }
    @objc func showDashboard(){window.makeKeyAndOrderFront(nil);NSApp.activate(ignoringOtherApps:true)}
    /// Mac notifications queued by the collector (never in setup preview).
    func startNotificationRelay(){
        notificationRelay?.stop()
        guard !setupPreview,!quitting else{notificationRelay=nil;return}
        notificationRelay=BloomNotificationRelay(send:{[weak self] body,completion in
            guard let self=self else{completion(0,nil);return}
            self.nativeRequest("/api/notifications/native",action:"notifications",body:body,limit:65536,completion:completion)
        })
        notificationRelay?.start()
    }
    /// Brings the dashboard forward at an allow-listed screen (from a clicked notification).
    func openScreen(_ screen:String){
        guard !quitting,bloomNotificationScreens.contains(screen) else{return}
        showDashboard()
        guard let base=localURL,let url=bloomScreenURL(base,screen) else{pendingScreen=screen;return}
        webView.load(URLRequest(url:url))
    }
    @objc func openSupport(){NSWorkspace.shared.open(URL(string:"https://bloomformac.com/support")!)}
    @objc func toggleLogin(_ sender:NSMenuItem){
        guard !setupPreview else{return}
        do{if SMAppService.mainApp.status == .enabled{try SMAppService.mainApp.unregister();sender.state = .off}else{try SMAppService.mainApp.register();sender.state = .on}}
        catch{let alert=NSAlert();alert.messageText="Could not change Open at Login";alert.informativeText="Move BloomGauge into Applications first. You can also manage login items in System Settings.";alert.runModal()}
    }
    @objc func reload(){ if let url=localURL {message.stringValue="Reconnecting…";message.isHidden=false;webView.load(URLRequest(url:url))} }
    @objc func zoomIn(){webView.pageZoom=min(2.5,webView.pageZoom+0.1)}
    @objc func zoomOut(){webView.pageZoom=max(0.75,webView.pageZoom-0.1)}
    @objc func zoomReset(){webView.pageZoom=1}
    @objc func about(){NSApp.orderFrontStandardAboutPanel(options:[.applicationName:"BloomGauge",.applicationVersion:Bundle.main.object(forInfoDictionaryKey:"CFBundleShortVersionString") as? String ?? "",.credits:NSAttributedString(string:"An independent companion for Darkbloom, formerly Bloomkeeper. Not affiliated with Darkbloom or Eigen Labs.\nA local dashboard for Darkbloom earnings and your Mac’s hardware.\nIncludes opt-in model experiments. Sensor mappings adapted from Stats (MIT).")])}
    func publishUpdateStatus(_ requestId:String? = nil){
        guard webView != nil else{return}
        let snapshot=updates?.snapshot(requestId:requestId) ?? ["requestId":requestId as Any? ?? NSNull(),"available":false,
            "installedVersion":Bundle.main.object(forInfoDictionaryKey:"CFBundleShortVersionString") as? String ?? "Unknown",
            "automaticChecks":false,"automaticUpdates":false,"canCheck":false,"checking":false,"lastCheck":NSNull(),"status":"idle","error":NSNull()]
        guard let data=try? JSONSerialization.data(withJSONObject:snapshot),let json=String(data:data,encoding:.utf8) else{return}
        webView.evaluateJavaScript("window.dispatchEvent(new CustomEvent('bloom-updates-status',{detail:\(json)}))",completionHandler:nil)
    }
    func userContentController(_ userContentController:WKUserContentController,didReceive message:WKScriptMessage){
        if message.name=="bloomUpdates" {
            guard isBloomUpdateFrame(message.frameInfo.request.url,localURL:localURL,
                                     isMainFrame:message.frameInfo.isMainFrame,belongsToDashboard:message.webView === webView),
                  let command=BloomUpdateBridgeCommand(message.body) else{return}
            if let updates=updates {
                if command.action=="check"{updates.checkNow(nil)}
                if command.action=="set-automatic",let enabled=command.enabled{updates.setAutomaticChecks(enabled)}
            }
            publishUpdateStatus(command.requestId);return
        }
        if message.name=="bloomAppearance" {
            guard message.webView === webView,message.frameInfo.isMainFrame,
                  let url=message.frameInfo.request.url,url.scheme=="http",url.host=="127.0.0.1",url.port==localURL?.port,
                  let choice=message.body as? String,["system","light","dark"].contains(choice) else{return}
            NSApp.appearance = bloomAppearanceName(choice).flatMap{NSAppearance(named:$0)}
            if !setupPreview {UserDefaults.standard.set(choice,forKey:appearanceDefaultsKey)}
            return
        }
        if message.name=="bloomDiagnostics" {
            guard message.webView === webView,message.frameInfo.isMainFrame,
                  let url=message.frameInfo.request.url,url.scheme=="http",url.host=="127.0.0.1",url.port==localURL?.port else{return}
            saveDiagnostics(message.body);return
        }
        guard message.name=="bloomAccount",message.webView === webView,message.frameInfo.isMainFrame,
              let url=message.frameInfo.request.url,url.scheme=="http",url.host=="127.0.0.1",url.port==localURL?.port,
              let body=message.body as? [String:String] else{return}
        if body["action"]=="connect"{reputationConnection?.connect()}
        if body["action"]=="disconnect"{reputationConnection?.disconnect()}
        if let command=BloomCachePermissionCommand(body){changeCachePermission(command)}
    }
    func cachePermissionResult(_ requestId:String,_ status:String){
        guard let data=try? JSONSerialization.data(withJSONObject:["requestId":requestId,"status":status]),
              let json=String(data:data,encoding:.utf8) else{return}
        webView.evaluateJavaScript("window.dispatchEvent(new CustomEvent('bloom-cache-permission-result',{detail:\(json)}))",completionHandler:nil)
    }
    func finishCachePermission(_ command:BloomCachePermissionCommand,_ status:String){
        cachePermissionOpen=false
        cachePermissionReceipts[command.requestId]=status
        cachePermissionResult(command.requestId,status)
    }
    func changeCachePermission(_ command:BloomCachePermissionCommand){
        guard !setupPreview else{cachePermissionResult(command.requestId,"unavailable");return}
        guard !quitting,!pendingTermination,!updateTermination else{cachePermissionResult(command.requestId,"busy");return}
        if let prior=cachePermissionReceipts[command.requestId]{cachePermissionResult(command.requestId,prior);return}
        guard !cachePermissionOpen else{cachePermissionResult(command.requestId,"busy");return}
        // Bound receipts without forgetting an old request and repeating it.
        guard cachePermissionReceipts.count<256 else{cachePermissionResult(command.requestId,"unavailable");return}
        guard let script=bloomCachePermissionScript(action:command.action,user:NSUserName(),uid:getuid()) else{
            cachePermissionResult(command.requestId,"unavailable");return
        }
        cachePermissionOpen=true
        let enabling=command.action=="cache-setup",alert=NSAlert()
        alert.messageText=enabling ? "Enable optional cache recovery?" : "Remove BloomGauge’s cache permission?"
        alert.informativeText=enabling
            ? "macOS will ask for administrator approval once. This allows your Mac account to run only /usr/sbin/purge with no arguments without another password prompt. BloomGauge never receives your password. Setup does not clear cache or start a model. You can remove BloomGauge’s permission here later."
            : "macOS will ask for administrator approval to remove only BloomGauge’s exact cache-cleanup permission. Other administrator rules are left alone. This does not stop a model or clear cache."
        alert.addButton(withTitle:enabling ? "Continue" : "Remove permission");alert.addButton(withTitle:"Cancel")
        alert.beginSheetModal(for:window){[weak self] response in
            guard let self=self else{return}
            guard response == .alertFirstButtonReturn else{self.finishCachePermission(command,"cancelled");return}
            // NSAppleScript executes on the main thread and lets macOS own the
            // authentication UI. Raw authorization errors are never sent to JS.
            DispatchQueue.main.async {
                var error:NSDictionary?
                guard let appleScript=NSAppleScript(source:script) else{self.finishCachePermission(command,"failed");return}
                appleScript.executeAndReturnError(&error)
                let status=error == nil ? "completed" : (error?[NSAppleScript.errorNumber] as? Int == -128 ? "cancelled" : "failed")
                self.finishCachePermission(command,status)
            }
        }
    }

    func diagnosticsResult(_ requestId:String,_ status:String){
        guard let data=try? JSONSerialization.data(withJSONObject:["requestId":requestId,"status":status]),
              let json=String(data:data,encoding:.utf8) else{return}
        webView.evaluateJavaScript("window.dispatchEvent(new CustomEvent('bloom-diagnostics-saved',{detail:\(json)}))",completionHandler:nil)
    }
    func saveDiagnostics(_ value:Any){
        guard let body=value as? [String:String],body["action"]=="save",let requestId=body["requestId"],
              UUID(uuidString:requestId) != nil else{return}
        guard !diagnosticsSaveOpen,let text=body["content"],let data=text.data(using:.utf8),data.count<=262144,
              let report=(try? JSONSerialization.jsonObject(with:data)) as? [String:Any],
              report["schema"] as? String == "bloom-diagnostics-v1" else{diagnosticsResult(requestId,"failed");return}
        diagnosticsSaveOpen=true
        let panel=NSSavePanel();panel.title="Save reviewed diagnostics";panel.nameFieldStringValue="BloomGauge diagnostics.json"
        panel.allowedContentTypes=[.json];panel.canCreateDirectories=true
        panel.beginSheetModal(for:window){[weak self] response in
            guard let self=self else{return};self.diagnosticsSaveOpen=false
            guard response == .OK,let url=panel.url else{self.diagnosticsResult(requestId,"cancelled");return}
            do{try data.write(to:url,options:.atomic);try FileManager.default.setAttributes([.posixPermissions:0o600],ofItemAtPath:url.path);self.diagnosticsResult(requestId,"saved")}
            catch{self.diagnosticsResult(requestId,"failed")}
        }
    }
    func windowShouldClose(_ sender:NSWindow)->Bool{if sender === window{sender.orderOut(nil);return false};return true}
    func applicationShouldHandleReopen(_ sender:NSApplication,hasVisibleWindows flag:Bool)->Bool{showDashboard();return true}
    func webView(_ webView:WKWebView,didFinish navigation:WKNavigation!){message.isHidden=true;starting=false}
    func webView(_ webView:WKWebView,didFailProvisionalNavigation navigation:WKNavigation!,withError error:Error){fail("Could not load the dashboard. Choose View → Reload Dashboard.")}
    func webView(_ webView:WKWebView,decidePolicyFor navigationAction:WKNavigationAction,decisionHandler:@escaping (WKNavigationActionPolicy)->Void){
        guard let url=navigationAction.request.url else{decisionHandler(.cancel);return}
        if url.scheme=="http",url.host=="127.0.0.1",url.port==localURL?.port{decisionHandler(.allow);return}
        let slackSource = url.scheme == "https" && url.user == nil && url.password == nil && url.port == nil && url.query == nil && url.fragment == nil &&
            url.host?.range(of: "^[a-z0-9][a-z0-9-]*\\.slack\\.com$", options: .regularExpression) != nil &&
            url.path.range(of: "^/archives/[CG][A-Z0-9]+/p[0-9]{15,20}$", options: .regularExpression) != nil
        let peerDashboard = navigationAction.navigationType == .linkActivated && navigationAction.sourceFrame.isMainFrame && isBloomPeerDashboard(url)
        let helpLink = navigationAction.navigationType == .linkActivated && navigationAction.sourceFrame.isMainFrame && isBloomHelpLink(url)
        if helpLink || peerDashboard || slackSource || (url.scheme=="https" && ["console.darkbloom.dev","tailscale.com","login.tailscale.com"].contains(url.host ?? "")){NSWorkspace.shared.open(url)}
        decisionHandler(.cancel)
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender:NSApplication)->Bool{false}
    func updateAdmissionRequest(_ body:[String:Any],completion:@escaping (Int,[String:Any]?)->Void){
        nativeRequest("/api/update/native",action:"update",body:body,limit:4096,completion:completion)
    }
    /// POSTs JSON to one of the collector's native routes: token header, no cookies, no redirects,
    /// 5 s, and a reply of at most `limit` bytes. Completes on the main thread (status 0 on failure).
    func nativeRequest(_ path:String,action:String,body:[String:Any],limit:Int,completion:@escaping (Int,[String:Any]?)->Void){
        guard let localURL=localURL,localURL.scheme=="http",localURL.host=="127.0.0.1",
              let url=URL(string:path,relativeTo:localURL)?.absoluteURL,
              let data=try? JSONSerialization.data(withJSONObject:body) else{completion(0,nil);return}
        var request=URLRequest(url:url,cachePolicy:.reloadIgnoringLocalCacheData,timeoutInterval:5)
        request.httpMethod="POST";request.httpBody=data
        request.setValue("application/json",forHTTPHeaderField:"Content-Type")
        request.setValue(nativeToken,forHTTPHeaderField:"X-Bloom-Native")
        request.setValue(action,forHTTPHeaderField:"X-Bloom-Action")
        let configuration=URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForResource=5;configuration.httpShouldSetCookies=false
        let session=URLSession(configuration:configuration,delegate:BloomLocalUpdateSessionDelegate(),delegateQueue:nil)
        session.dataTask(with:request){data,response,error in
            let status=(response as? HTTPURLResponse)?.statusCode ?? 0
            let result=(data != nil && data!.count<=limit) ? ((try? JSONSerialization.jsonObject(with:data!)) as? [String:Any]) : nil
            session.finishTasksAndInvalidate()
            DispatchQueue.main.async{completion(error == nil ? status : 0,result)}
        }.resume()
    }
    func cancelUpdateTermination(){
        if quietPending != nil || quietAttempt {dropQuietProbe();quietAttempt=false;UserDefaults.standard.removeObject(forKey:quietRelaunchDefaultsKey)}
        guard updateTermination,pendingTermination,!quitting else{return}
        updateAdmission?.cancel();pendingTermination=false;terminationAttempt=nil;updateTermination=false
        NSApp.reply(toApplicationShouldTerminate:false)
    }
    func applicationShouldTerminate(_ sender:NSApplication)->NSApplication.TerminateReply {
        // A quit from Sparkle's installer is always an update and always gets the safety
        // check; it is quiet when the quiet path started it. Any other quit (menu, Dock,
        // logout) is a person's Quit: never refused silently, and it cancels a quiet attempt.
        let fromSparkle=sparkleQuitRequest()
        let pending=quietPending
        quietPending=nil
        let release={[weak self] in if let pending=pending,let self=self {bloomReleaseQuietProbe(pending.probe,send:self.admissionSend)}}
        let waited=updates?.waitingSince.map{Date().timeIntervalSince1970-$0} ?? 0
        let quiet:(automatic:Bool,requestId:String?)?=fromSparkle && quietIntent
            ? (pending?.automatic ?? BloomQuietInstallPolicy.automatic(waiting:waited),pending?.probe.requestId) : nil
        if !fromSparkle && pending != nil {release();updates?.quietInstallPostponed()}
        if cachePermissionOpen{if quiet != nil {release();postponeQuietInstall()};return .terminateCancel}
        guard let process=collector,process.isRunning else{return .terminateNow}
        if pendingTermination{if quiet != nil {release()};return .terminateLater}
        pendingTermination=true
        let attempt=UUID();terminationAttempt=attempt
        quietAttempt = quiet != nil
        if fromSparkle || updates?.requiresSafeTermination() == true {
            updateTermination=true
            let admission=BloomUpdateAdmission(send:{[weak self] body,completion in
                guard let self=self else{completion(0,nil);return}
                self.updateAdmissionRequest(body,completion:completion)
            })
            updateAdmission=admission
            // Even a local preflight failure must reply after this delegate has
            // returned terminateLater, never from inside the initial callback.
            DispatchQueue.main.async { [weak self] in
            guard let self=self,self.pendingTermination,self.terminationAttempt==attempt else{admission.cancel();return}
            admission.begin(automatic:quiet?.automatic ?? false,requestId:quiet?.requestId,ready:{[weak self] in
                guard let self=self,self.pendingTermination,self.terminationAttempt==attempt else{admission.cancel();return}
                self.beginCollectorTermination(process,attempt:attempt)
            },failed:{[weak self] message in
                guard let self=self,self.pendingTermination,self.terminationAttempt==attempt else{return}
                self.pendingTermination=false;self.terminationAttempt=nil;self.updateTermination=false
                if quiet != nil {
                    // Nobody asked for this install: wait quietly for the next good moment.
                    release();self.postponeQuietInstall()
                    NSApp.reply(toApplicationShouldTerminate:false)
                    return
                }
                self.updates?.installationBlocked(message)
                NSApp.reply(toApplicationShouldTerminate:false)
                self.showDashboard()
                let alert=NSAlert();alert.messageText="Update postponed"
                alert.informativeText=message;alert.addButton(withTitle:"OK");alert.beginSheetModal(for:self.window)
            })
            }
        } else {
            updateTermination=false
            // Intentional Quit preserves its existing behavior. Update admission
            // is only required for a termination initiated by the installer. If Sparkle's
            // installer is still waiting from a postponed quiet attempt, it will install and
            // relaunch after this Quit: the relaunched app then closes again.
            quitRecordOnExit = updates?.installerArmed == true
            beginCollectorTermination(process,attempt:attempt)
        }
        return .terminateLater
    }
    func beginCollectorTermination(_ process:Process,attempt:UUID){
        guard pendingTermination,terminationAttempt==attempt else{return}
        quitting=true
        reputationConnection?.refreshTimer?.invalidate()
        reputationConnection?.accountView?.stopLoading()
        notificationRelay?.stop()
        // Stop only our collector. Never signal Darkbloom or force-kill it.
        process.terminationHandler={ [weak self] _ in DispatchQueue.main.async {
            guard let self=self,self.pendingTermination,self.terminationAttempt==attempt else{return}
            self.pendingTermination=false;self.terminationAttempt=nil;NSApp.reply(toApplicationShouldTerminate:true)
        } }
        if !process.isRunning{pendingTermination=false;terminationAttempt=nil;NSApp.reply(toApplicationShouldTerminate:true);return}
        process.terminate()
        DispatchQueue.main.asyncAfter(deadline:.now()+30){[weak self] in
            guard let self=self,self.pendingTermination,self.terminationAttempt==attempt else{return}
            self.pendingTermination=false;self.terminationAttempt=nil
            if !process.isRunning{NSApp.reply(toApplicationShouldTerminate:true);return}
            self.updateAdmission?.cancel()
            self.quitting=false;self.quitRecordOnExit=false
            process.terminationHandler={ [weak self] _ in DispatchQueue.main.async {self?.collectorStopped()} }
            self.startNotificationRelay()
            NSApp.reply(toApplicationShouldTerminate:false)
            if self.quietAttempt {self.updateTermination=false;self.postponeQuietInstall();return}
            self.fail("BloomGauge is still saving local state. Please try the update or Quit again in a moment.");self.showDashboard()
        }
    }
    func applicationWillTerminate(_ notification:Notification){
        quitting=true;collector?.terminationHandler=nil;notificationRelay?.stop()
        // Timed from the actual exit, so the installer's relaunch finds it fresh.
        if quitRecordOnExit {
            UserDefaults.standard.set(bloomQuitRelaunchRecord(now:Date().timeIntervalSince1970),forKey:quietRelaunchDefaultsKey)
            CFPreferencesAppSynchronize(kCFPreferencesCurrentApplication)
        }
    }
}

/// Fixed identity of the dashboard window's persistent WebKit data store.
let dashboardStoreID=UUID(uuidString:"F90F7395-494B-4DFE-87C0-2C98B71DEB3F")!

// BEGIN session cookie policy (compiled directly by its regression test).
func bloomSessionCookie(url:URL, token:String) -> HTTPCookie? {
    guard url.scheme == "http", url.host == "127.0.0.1", let port = url.port, port > 0,
          token.count >= 32, token.allSatisfy({ $0.isLetter || $0.isNumber || $0 == "-" }) else { return nil }
    return HTTPCookie(properties:[.name:"bloom_session", .value:token, .domain:"127.0.0.1", .path:"/",
                                  HTTPCookiePropertyKey("HttpOnly"):"TRUE", .sameSitePolicy:HTTPCookieStringPolicy.sameSiteStrict])
}
// END session cookie policy.

// BEGIN appearance policy (compiled directly by its regression test).
let appearanceDefaultsKey = "BloomAppearance"
/// "light" or "dark" pins the app's appearance; anything else follows macOS.
func bloomAppearanceName(_ choice:String?)->NSAppearance.Name? {
    switch choice {
    case "light": return .aqua
    case "dark": return .darkAqua
    default: return nil
    }
}
// END appearance policy.

// BEGIN help link policy (compiled directly by its regression test).
func isBloomHelpLink(_ url:URL)->Bool {
    if url.absoluteString == "mailto:support@bloomgauge.io" {return true}
    guard url.scheme == "https",url.user == nil,url.password == nil,url.port == nil,url.query == nil else{return false}
    if url.host == "darkbloom.slack.com" {return url.path == "/archives/C0C4HC8HZLN" && url.fragment == nil}
    return url.host == "bloomformac.com" && ((["","/","/support","/privacy","/changelog","/beta/quickstart"].contains(url.path) && url.fragment == nil) || (url.path == "/" && url.fragment == "release"))
}
// END help link policy.

// BEGIN peer dashboard policy (compiled directly by its regression test).
func isBloomPeerDashboard(_ url:URL)->Bool {
    url.scheme == "https" && url.user == nil && url.password == nil && url.port == 8443 &&
    url.path == "/" && url.query == "screen=overview" && url.fragment == nil &&
    url.host?.range(of:"^[a-z0-9-]+\\.[a-z0-9-]+\\.ts\\.net$",options:.regularExpression) != nil
}
// END peer dashboard policy.

// BEGIN reputation refresh policy (compiled directly by its regression test).
// Uptime avoids wall-clock adjustments. Only an established, collector-accepted
// connection can trigger a reload; an expired/invalid login cannot loop.
struct ReputationAuthRefresh {
    private var healthySince:TimeInterval?
    private var lastHealthyAt:TimeInterval?
    private var lastAttemptAt:TimeInterval?
    private var armed=false

    mutating func reset(){self=Self()}
    mutating func shouldReload(status:String,now:TimeInterval,connected:Bool,visible:Bool,url:URL?)->Bool{
        guard connected else{reset();return false}
        guard let url=url,url.scheme=="https",url.host=="console.darkbloom.dev",
              url.port==nil || url.port==443,url.user==nil,url.password==nil else{return false}
        if status=="ok" {
            if let last=lastHealthyAt,now>=last,now-last<=45 {} else{healthySince=now}
            lastHealthyAt=now
            if let start=healthySince,now-start>=60 {armed=true}
            return false
        }
        healthySince=nil;lastHealthyAt=nil
        if status=="unmatched"{armed=false}
        guard status=="auth_required",armed,!visible else{return false}
        if let last=lastAttemptAt,now-last<30*60{return false}
        armed=false;lastAttemptAt=now
        return true
    }
}
// END reputation refresh policy.

// The console owns sign-in. Its authenticated requests remain in its WebKit
// session; only the reputation allowlist crosses to the loopback collector.
final class ReputationConnection: NSObject, WKScriptMessageHandler, WKNavigationDelegate, WKUIDelegate, URLSessionTaskDelegate {
    let baseURL:URL
    let token:String
    var accountView:WKWebView?
    var accountWindow:NSWindow?
    var refreshTimer:Timer?
    var sequence=0
    var clearing=false
    var authRefresh=ReputationAuthRefresh()
    lazy var session=URLSession(configuration:.ephemeral,delegate:self,delegateQueue:nil)
    init(baseURL:URL,token:String){self.baseURL=baseURL;self.token=token;super.init()}
    func restore(){if UserDefaults.standard.bool(forKey:"BloomReputationConnected"){start(visible:false)}}
    func connect(){
        guard !clearing else{return}
        authRefresh.reset()
        UserDefaults.standard.set(true,forKey:"BloomReputationConnected")
        start(visible:true)
    }
    func start(visible:Bool){
        if accountView == nil {
            let config=WKWebViewConfiguration();config.websiteDataStore = .default()
            config.preferences.javaScriptCanOpenWindowsAutomatically=true
            config.userContentController.add(self,name:"bloomReputation")
            guard let path=Bundle.main.url(forResource:"reputation-bridge",withExtension:"js"),let script=try? String(contentsOf:path,encoding:.utf8) else{send(["status":"unavailable"]);return}
            config.userContentController.addUserScript(WKUserScript(source:script,injectionTime:.atDocumentStart,forMainFrameOnly:true))
            let view=WKWebView(frame:NSRect(x:0,y:0,width:1000,height:760),configuration:config)
            view.navigationDelegate=self;view.uiDelegate=self;accountView=view
            let window=NSWindow(contentRect:view.bounds,styleMask:[.titled,.closable,.miniaturizable,.resizable],backing:.buffered,defer:false)
            window.title="Connect Darkbloom — reputation and concurrency"
            window.isReleasedWhenClosed=false;window.contentView=view;window.center();accountWindow=window
            send(["status":"connecting"])
            view.load(URLRequest(url:URL(string:"https://console.darkbloom.dev/earn")!))
            refreshTimer=Timer.scheduledTimer(withTimeInterval:15,repeats:true){[weak self] _ in
                self?.accountView?.evaluateJavaScript("void window.__bloomRefreshReputation?.()",completionHandler:nil)
            }
        } else if visible {
            // Reload invokes the console's own auth refresh and provider query.
            accountView?.reload()
        }
        if visible{accountWindow?.makeKeyAndOrderFront(nil);NSApp.activate(ignoringOtherApps:true)}
    }
    func disconnect(){
        clearing=true;UserDefaults.standard.set(false,forKey:"BloomReputationConnected")
        authRefresh.reset()
        refreshTimer?.invalidate();refreshTimer=nil
        accountView?.stopLoading();accountView?.configuration.userContentController.removeScriptMessageHandler(forName:"bloomReputation")
        accountWindow?.orderOut(nil);accountWindow=nil;accountView=nil
        send(["status":"disconnected"])
        WKWebsiteDataStore.default().removeData(ofTypes:WKWebsiteDataStore.allWebsiteDataTypes(),modifiedSince:.distantPast){[weak self] in self?.clearing=false}
    }
    func userContentController(_ userContentController:WKUserContentController,didReceive message:WKScriptMessage){
        guard message.name=="bloomReputation",message.webView === accountView,message.frameInfo.isMainFrame,
              let url=message.frameInfo.request.url,url.scheme=="https",url.host=="console.darkbloom.dev",url.port==nil || url.port==443,
              let body=message.body as? [String:Any] else{return}
        send(body)
    }
    func send(_ body:[String:Any]){
        sequence+=1;let sentSequence=sequence
        var payload=body;payload["sequence"]=sentSequence
        guard let data=try? JSONSerialization.data(withJSONObject:payload),data.count<=512*1024 else{return}
        var request=URLRequest(url:baseURL.appendingPathComponent("api/reputation/native"))
        request.httpMethod="POST";request.httpBody=data;request.timeoutInterval=10
        request.setValue("application/json",forHTTPHeaderField:"Content-Type")
        request.setValue(token,forHTTPHeaderField:"X-Bloom-Native")
        session.dataTask(with:request){[weak self] data,response,error in
            guard let data=data,(response as? HTTPURLResponse)?.statusCode==200,
                  let result=try? JSONSerialization.jsonObject(with:data) as? [String:Any] else{return}
            DispatchQueue.main.async{
                guard let self=self,self.sequence==sentSequence else{return}
                let status=result["status"] as? String ?? "unavailable"
                let reload=self.authRefresh.shouldReload(status:status,now:ProcessInfo.processInfo.systemUptime,
                    connected:!self.clearing && self.accountView != nil && UserDefaults.standard.bool(forKey:"BloomReputationConnected"),
                    visible:self.accountWindow?.isVisible ?? false,url:self.accountView?.url)
                if status=="ok"{self.accountWindow?.orderOut(nil)}
                // Let the official console renew its own session. Do not read
                // credentials, foreground a window, or touch provider control.
                if reload{self.accountView?.reload()}
            }
        }.resume()
    }
    func urlSession(_ session:URLSession,task:URLSessionTask,willPerformHTTPRedirection response:HTTPURLResponse,newRequest request:URLRequest,completionHandler:@escaping (URLRequest?)->Void){completionHandler(nil)}
    func webView(_ webView:WKWebView,decidePolicyFor navigationAction:WKNavigationAction,decisionHandler:@escaping (WKNavigationActionPolicy)->Void){
        guard let url=navigationAction.request.url else{decisionHandler(.cancel);return}
        // HTTPS auth frames may belong to Privy or the user's selected identity
        // provider. Only the exact console main-frame origin has a data bridge.
        if navigationAction.targetFrame?.isMainFrame==true,url.scheme=="https",url.host != "console.darkbloom.dev"{
            NSWorkspace.shared.open(url);decisionHandler(.cancel);return
        }
        decisionHandler(url.scheme=="https" || url.absoluteString=="about:blank" ? .allow : .cancel)
    }
    func webView(_ webView:WKWebView,createWebViewWith configuration:WKWebViewConfiguration,for navigationAction:WKNavigationAction,windowFeatures:WKWindowFeatures)->WKWebView?{
        // Email sign-in stays in the official console. Open help/other tabs in
        // the default browser without handing them the reputation bridge.
        if let url=navigationAction.request.url,url.scheme=="https"{NSWorkspace.shared.open(url)}
        return nil
    }
    func webView(_ webView:WKWebView,didFailProvisionalNavigation navigation:WKNavigation!,withError error:Error){send(["status":"unavailable"])}
}
@main
struct BloomApplication {
    static func main() {
        let app=NSApplication.shared
        let delegate=AppDelegate()
        app.delegate=delegate
        withExtendedLifetime(delegate){app.run()}
    }
}
