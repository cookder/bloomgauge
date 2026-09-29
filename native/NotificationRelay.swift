import AppKit
import UserNotifications

// BEGIN notification item policy (Foundation only, so a regression test can compile it directly).
/// Screens a Mac notification may open. Anything else opens Test.
let bloomNotificationScreens=["overview","test","demand"]
/// One queued Mac notification from the collector, strictly validated.
struct BloomNotificationItem {
    let id:String
    let title:String
    let body:String
    let screen:String
    /// 32 lowercase hex characters, or nil.
    static func id(_ value:Any?)->String? {
        guard let id=value as? String,id.count==32,id.allSatisfy({"0123456789abcdef".contains($0)}) else{return nil}
        return id
    }
    init?(_ value:Any){
        guard let item=value as? [String:Any],let id=Self.id(item["id"]),
              let title=(item["title"] as? String)?.trimmingCharacters(in:.whitespacesAndNewlines),!title.isEmpty,
              let body=(item["body"] as? String)?.trimmingCharacters(in:.whitespacesAndNewlines),!body.isEmpty else{return nil}
        let screen=item["screen"] as? String ?? ""
        self.id=id;self.title=String(title.prefix(80));self.body=String(body.prefix(300))
        self.screen=bloomNotificationScreens.contains(screen) ? screen : "test"
    }
}
/// The dashboard address that opens an allow-listed screen, on the loopback collector only.
func bloomScreenURL(_ base:URL,_ screen:String)->URL? {
    guard base.scheme=="http",base.host=="127.0.0.1",base.port != nil,bloomNotificationScreens.contains(screen),
          var parts=URLComponents(url:base,resolvingAgainstBaseURL:false) else{return nil}
    parts.user=nil;parts.password=nil;parts.path="/";parts.query="screen=\(screen)";parts.fragment=nil
    return parts.url
}
/// The collector's suggested poll interval, kept to 5...120 seconds (15 when missing).
func bloomPollSeconds(_ value:Any?)->TimeInterval {
    guard let seconds=value as? Double,seconds.isFinite else{return 15}
    return min(120,max(5,seconds))
}
// END notification item policy.

/// Polls the collector for queued Mac notifications, posts them, then acknowledges them
/// (also when notifications are off, so the queue drains). Main thread only; never in setup preview.
final class BloomNotificationRelay {
    let send:BloomUpdateAdmission.Send
    var timer:Timer?
    var interval:TimeInterval=15
    var polling=false
    var stopped=false
    /// Recently posted ids: an item whose ack was lost is acked again without sounding twice.
    var posted:[String]=[]
    /// macOS asks once per launch, and only when there is something to show.
    static var askedPermission=false
    init(send:@escaping BloomUpdateAdmission.Send){self.send=send}
    func start(){schedule(interval);poll()}
    func stop(){stopped=true;timer?.invalidate();timer=nil}
    func schedule(_ seconds:TimeInterval){
        timer?.invalidate();interval=seconds
        timer=Timer.scheduledTimer(withTimeInterval:seconds,repeats:true){[weak self] _ in self?.poll()}
    }
    func poll(){
        guard !polling,!stopped else{return}
        polling=true
        UNUserNotificationCenter.current().getNotificationSettings{settings in
            let status=settings.authorizationStatus
            DispatchQueue.main.async{[weak self] in
                guard let self=self,!self.stopped else{return}
                let permission=status == .notDetermined ? "notDetermined" : status == .denied ? "denied" : "granted"
                self.send(["action":"poll","permission":permission]){[weak self] code,result in
                    guard let self=self,!self.stopped else{return}
                    self.polling=false
                    guard code==200,let result=result else{return}
                    let seconds=bloomPollSeconds(result["pollSeconds"])
                    if seconds != self.interval {self.schedule(seconds)}
                    // At most 20 per ack; the rest arrive on the next poll.
                    let raw=(result["items"] as? [Any] ?? []).prefix(20)
                    let ids=raw.compactMap{BloomNotificationItem.id(($0 as? [String:Any])?["id"])}
                    let items=raw.compactMap{BloomNotificationItem($0)}
                    guard !ids.isEmpty else{return}
                    switch status {
                    case .denied: self.ack(ids)
                    case .notDetermined:
                        guard !items.isEmpty else{self.ack(ids);return}
                        // Items stay queued (up to 15 minutes) while the person decides.
                        guard !Self.askedPermission else{return}
                        Self.askedPermission=true
                        UNUserNotificationCenter.current().requestAuthorization(options:[.alert,.sound]){granted,error in
                            // An error is not a refusal: leave the items queued (they expire) and say why.
                            if let error=error {NSLog("BloomGauge: notification permission failed: %@",error.localizedDescription)}
                            DispatchQueue.main.async{[weak self] in if granted{self?.deliver(items,ids)}else if error == nil{self?.ack(ids)}}
                        }
                    default: self.deliver(items,ids)
                    }
                }
            }
        }
    }
    func deliver(_ items:[BloomNotificationItem],_ ids:[String]){
        let group=DispatchGroup()
        for item in items where !posted.contains(item.id) {
            let content=UNMutableNotificationContent()
            content.title=item.title;content.body=item.body;content.sound = .default
            content.userInfo=["screen":item.screen]
            group.enter();posted.append(item.id)
            UNUserNotificationCenter.current().add(UNNotificationRequest(identifier:item.id,content:content,trigger:nil)){_ in group.leave()}
        }
        posted=Array(posted.suffix(200))
        group.notify(queue:.main){[weak self] in self?.ack(ids)}
    }
    func ack(_ ids:[String]){send(["action":"ack","ids":ids]){_,_ in}}
}

extension AppDelegate: UNUserNotificationCenterDelegate {
    /// Shows BloomGauge's notifications even while its window is in front.
    nonisolated func userNotificationCenter(_ center:UNUserNotificationCenter,willPresent notification:UNNotification,
                                            withCompletionHandler completionHandler:@escaping (UNNotificationPresentationOptions)->Void){
        completionHandler([.banner,.list,.sound])
    }
    /// A clicked notification brings the dashboard forward at the screen it names.
    nonisolated func userNotificationCenter(_ center:UNUserNotificationCenter,didReceive response:UNNotificationResponse,
                                            withCompletionHandler completionHandler:@escaping ()->Void){
        let screen=response.actionIdentifier == UNNotificationDefaultActionIdentifier
            ? response.notification.request.content.userInfo["screen"] as? String : nil
        DispatchQueue.main.async{[weak self] in
            if let screen=screen {self?.openScreen(screen)}
            completionHandler()
        }
    }
}
