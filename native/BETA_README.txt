BloomGauge — 1.36.63 beta44 · Fixes for Macs that stay online without getting paid work

CHANGES IN THIS UPDATE
If a freshly started Mac gets no paid requests for 10 minutes while its model is in demand, BloomGauge sends one small test request and, if needed, restarts Darkbloom (at most 3 times a day). If a Darkbloom restart leaves its background service unloaded, BloomGauge loads it again after 90 seconds. Previous beta43 change: Bloomkeeper became BloomGauge, updates install by themselves, and uptime fixes.

FEATURES RETAINED
Arrange the Earnings dashboard with separate phone and desktop layouts saved in each browser. Compare experimental one-hour earnings baselines with clearer evidence, saved forecast windows and local accuracy tracking. Optimizer trial reviews now close unresolvable comparisons at their deadline, require a supported paid benchmark and explain the paid alternative; confirmation can pause through a brief data gap without counting unseen time.

Manual controls start or switch to the model you select. Eligible Manual starts and Optimizer on can try one cache cleanup and remeasure memory before loading. On shows preparation progress, and Manual cancels remaining activation. Optional cleanup permission is enabled or removed through macOS administrator approval in the installed Mac app.

BloomGauge, including the optimizer, is free while we evaluate whether it improves earnings over Darkbloom alone. Optimizer access has no scheduled expiration. Updating never turns automation on or resumes an explicitly paused plan. Choose Optimizer on separately when ready. Existing settings, history, access records and privacy choices are preserved. Forecasts are experimental and advisory, remain on this Mac, and do not drive automatic switches or promise future income. The private phone dashboard updates with its Mac host.

The previous report-delivery repair is included. Earlier unconfirmed reports are not recovered or retried automatically; update, review and send the problem again.

REPORT DELIVERY IN THIS UPDATE
Problem reports and optional usage requests can now reach the service when the old client signature was blocked. Review and explicitly send reports as before. Earlier unconfirmed reports are not recovered or automatically retried; after updating, review and send the problem again. Usage sharing remains optional.


DEVELOPER PREVIEW. This build is not yet Apple-notarized or approved for public distribution.
Do not bypass Gatekeeper to distribute this preview. A signed, notarized beta follows Apple Developer ID setup and independent Mac testing.

INSTALLATION (for the signed beta)
Drag BloomGauge Beta into Applications, then open it.
The app includes Python and its dependencies; you do not need developer tools, Codex, Node, or Python.
Use your existing Darkbloom provider and account. BloomGauge is an independent companion, not a compute network.

FIRST LAUNCH
Follow the three-step setup. BloomGauge begins in observation mode. An expired login asks you to sign in again through Darkbloom; a connection or API problem stays unconfirmed and retries instead of implying that your login is wrong.
Electricity pricing is optional and starts unconfigured. Confirmed API earnings work without Darkbloom Monitor; an existing, account-matched Monitor history can enrich reports.
Every dashboard, model report, demand comparison and manual model switch is free. Private phone access, safe warm-up and recovery are included.
Review available models and the optimizer's criteria before enabling automation. Use the manual model controls in Optimizer > Overview for manual changes and Optimizer > History for comparisons. With Darkbloom 0.9.9 or later, trials let accepted requests finish first; older versions may interrupt active work. There is no guaranteed income or earnings improvement.
Phone access is optional via your own Tailscale account in More > Phone access. If setup takes more than 30 seconds, BloomGauge shows that the action may still be finishing and refreshes its operation status before allowing another change. After a timeout or relaunch, follow the refreshed status rather than repeating an uncertain action. Install Tailscale on each Mac and phone, signed into the same owner account. Separate Tailscale plan terms apply.

EARNINGS AND MODEL CHOICES
Pulse opens with model-colored earnings bars over the last hour, with saved graph choices and adjustable ranges. Bars use confirmed signed inference credits per elapsed hour; the meter retains its separate warm-time pace. The Network view separates sampled model activity, unattributed completed traffic and this Mac's actual inference credits. Base rewards, missing coverage and conditional estimates retain their labels.
Manual model selection adds recorded warm-hour pay, output size, historical next-eight-hour demand and conditional income. A compact live optimizer view explains current comparisons and blockers. Weekly traffic follows the viewer's local hour; credit filters expose the confirmed totals in the selected scope.
The optimizer uses aligned settled-minute evidence, separates supported paid opportunities from speculative discovery, and gives exploration an explicit review and economic outcome. Receiving a payment alone is not proof of a competitive result. These updates preserve admission, privacy, trial and recovery safeguards and do not guarantee additional earnings.

OPTIONAL NEXT STEPS
After setup and about two accumulated hours of fresh connected monitoring, a compact card below Pulse may introduce phone access and the optimizer. Closed-app, sleep and stale-reading gaps do not count; healthy readings with zero earnings do. Choose Later for a 24-hour snooze or Don’t show again for a permanent dismissal. These choices survive relaunches and updates locally, independently of usage-sharing consent. Configured or used features stop appearing.
The actions open Phone access or optimizer setup. They never enable automation. Phone and account configuration remain on the Mac.

MY MACS
My Macs provides a combined dashboard for up to five Macs, including this dashboard host. Run this beta on every Mac, enable Phone access, and add each other Mac's private HTTPS address on the host Mac. Name the Macs clearly. Your phone can then monitor all five from the host's My Macs page. The host must stay awake and connected. Each Mac keeps collecting and optimizing independently even when the host or phone disconnects.
Combined income includes device-attributed inference credits only: shared account balances and base rewards are excluded. Missing coverage, offline readings and duplicate devices are labeled. Open a named Mac's separate dashboard for full charts and controls. No bulk model commands are sent.

MODEL SWITCHING
This release retains improved switch verification when paid work begins during warm-up or temporary capacity is unavailable. BloomGauge continues checking the selected provider session within the existing deadline, with bounded synthetic warm-up attempts. A retained failure now has a last-switch time and distinguishes the failure from recovery; current readiness is shown separately. This does not prove the cause of every past failure.
Cache recovery remains optional and guarded. Purge permissions and frequency limits are unchanged; a failure alone does not mean purge was needed or attempted. If a switch fails naturally, record the time, requested model and current readiness, then review the optional diagnostics before sharing. Do not interrupt productive work to create a test failure.

OPTIONAL PROBLEM REPORTS
If something goes wrong, BloomGauge may offer Review and report. You can also use More > Help & feedback > Report a problem without waiting for a suggestion. Detection, opening, preview and dismissal do not upload. Dismiss snoozes suggestions for 24 hours; manual reporting stays available.
Description and contact are optional and start blank. Choose Review report to inspect the exact small payload, then Send to BloomGauge support only if you want to send it to Andrew's private owner inbox. Editing requires a new review. Closing before Send uploads nothing. Reporting works without usage sharing and changes no provider, optimizer plan, trial or settings.
The automatic summary uses coarse hardware, app/provider versions and fixed status/failure categories. It excludes earnings, raw errors/stacks/logs, credentials, account/device/license/analytics identifiers, local paths and private URLs. Deliberately typed notes/contact are included as entered: review them and leave out secrets or personal account details. The existing full diagnostics preview/save remains a separate optional export. Unavailable capture is labeled, not invented.
A matching report ID receipt confirms delivery. If offline or unconfirmed, the report may already have arrived. No automatic retry or persistent background upload queue exists. Retry same report uses the same frozen ID/contents while the review is valid; a preview expires within 10 minutes and a newly reviewed report is a separate submission. Keep the report ID locally for intentional support/deletion requests; never share review tokens or report authorization secrets.
The owner inbox is private; optional notes/contact are rendered as plain text and reports are subject to 30-day retention, with cleanup on submission/owner access. Reporting does not enable automatic follow-up contact. If BloomGauge is closed or its local collector is unreachable, use retry/support guidance; no feature can promise capture of every crash. Report-system errors must not recursively open suggestions.

GETTING HELP
Open More > Help & feedback on your Mac or authenticated phone.
Review diagnostics creates a report you can read before saving. Earnings are excluded unless you check the optional inclusion box.
Reports include hardware capacity, source freshness, recent optimizer decision categories and safe switch-failure, recovery, cache and readiness measurements. They exclude credentials, account/device IDs, private URLs, user paths and raw logs. Model names use aliases with family and size.
Save report uses the exact preview: a Save dialog on Mac, a browser download on phone. Share report appears when your phone supports sharing files. You choose the recipient; BloomGauge never uploads the report automatically.

KEEPING BLOOM RUNNING
Closing the window keeps collection and enabled automation running. Reopen it from the leaf in the menu bar.
Quit BloomGauge ends BloomGauge's collection and automation. It does not stop Darkbloom.
Open at Login is an explicit option in the application menu. It is off by default.

DATA AND PRIVACY
History and settings live in ~/Library/Application Support/Bloom Dashboard Beta/.
Beta history is separate from an existing personal BloomGauge installation. Do not run both against the same provider: quit one before starting the other normally.
Optional usage sharing starts off. An explicit Mac choice enables coarse platform and daily feature reports. Earnings, account identifiers, prompts, raw logs and private URLs are excluded. Historical access flags from older versions retain their original meaning; this version does not create new commercial access events.
Removing the app preserves history. Delete that beta data folder only if you intentionally want to erase its history and settings. Disconnect the Darkbloom console and disable phone access before uninstalling; turn off Open at Login too.
Cache recovery is optional and narrowly scoped. Setup and removal scripts are inside the app's Contents/Resources folder. They ask for your Mac administrator password in Terminal; BloomGauge never receives it. No blanket administrator access is requested.

COMPATIBILITY AND LIMITS
Apple Silicon; BloomGauge's executable minimum is macOS 14. Darkbloom admission and model requirements may be higher. M5/48 GB is the current development machine; other hardware and physical phones still need independent beta validation.
Existing alpha APIs and console sign-in may change. Missing sensors and history are labeled unavailable. Direct download does not require App Store review; this preview is not notarized. Specific API, automation and code-provenance questions remain under review.
Codex research, periodic AI source reviews and private community digests are not included services.

NOTICES
Open Contents/Resources/THIRD_PARTY_NOTICES.md, python-licenses and push_vendor for bundled dependency licenses. No personal account, history, keys or private phone settings are part of this release.


Updates and contact
-------------------

Before updating an older beta, let any model switch or warm-up finish. The installation guard is part of beta11. Beta9/10 use their previous quit behavior for the first update to beta11; the new guard protects later installations after beta11 is running.
Beta5, beta6 and beta7 need one manual installation of the current release to gain the updater. Beta9, beta10 and beta11 already support optional checks.
BloomGauge updates itself. From beta43 it checks about every six hours while running, downloads a new version in the background and installs it at a quiet moment: after you've been away from the Mac for about ten minutes (or, after a day of waiting, whenever BloomGauge isn't the front app), and never while it is switching, warming up, trying a bigger model, testing a model or settling a model change. BloomGauge saves its state, reopens on the new version with its window as it was, and the manager keeps its home model. Darkbloom keeps running throughout. Quitting BloomGauge also installs a downloaded update.
To turn this off, clear Update BloomGauge automatically on the final setup screen or in More > Help & feedback on the Mac, or use Update Automatically in the BloomGauge menu. If you had turned automatic checks off before, they stay off. Help also shows the installed version, last check/status and Check for updates. To install a waiting update right away, choose Install Update Now in the BloomGauge menu. Usage-sharing consent is separate.
Install Update Now and Install and Relaunch defer while BloomGauge is switching or warming a model, or cannot confirm that installation is safe. Let the current operation finish and try again when prompted. A blocked update leaves the provider and optimizer settings unchanged.
Phones and ordinary browsers show instructions to install on the Mac. Update checks use the HTTPS feed and signed installers; they add no account, license, analytics identifier or system profile.
Use Help > Help & Feedback on Mac, or More > Help & feedback on Mac or phone, for the support page, Slack contact and email. Nothing is sent automatically.

MANUAL MODEL CONTROLS
Open Optimizer > Overview and expand the manual model controls. Choose a downloaded available model, then Start if stopped or Switch to change models. Stop Darkbloom asks for confirmation, interrupts requests and pauses automatic switching. Manual controls work on the Mac and authenticated phone; unavailable selections explain why. Manual model actions pause automation. A queued manual change waits for 12 seconds idle, then can interrupt active requests after five minutes if still busy.
Pre-warming setup is part of the selected-model action on the Mac: use Prepare for the current running model, or Start or Switch for your selected model. This adds an authenticated loopback endpoint alongside the coordinator; there is no separate Enable pre-warming button or Controller tab. Endpoint configuration stays Mac-only; once prepared, model controls work from your authenticated phone. No Terminal window needs to remain open. Follow any authentication/bind or readiness message in Model & service details, or open Help & feedback. Do not use --local, which runs without the coordinator. Routine switches do not require a cache purge.
Standard Mac shortcuts: Command-H hides BloomGauge; Command-W closes its window while monitoring continues. Reopen from the Dock or menu bar. Command-V pastes into text fields.

OPTIONAL USAGE INVITATION (beta18)

After setup has been complete for ten minutes, BloomGauge may show one small invitation on the Mac's Pulse page. It asks to share limited setup/feature reports; nothing is enabled by displaying it. Share optional usage is affirmative consent. No thanks stays local. A saved prior opt-out, active sharing or deletion suppresses the invitation. The invitation is recorded locally before display, so relaunches/updates do not repeat it. Manage the choice any time in More → About BloomGauge. Phone users and setup previews do not receive the invitation. All features work identically either way.

Independent checks: decline/relaunch stays quiet; prior opt-out stays quiet; no report before consent; explicit consent reports only disclosed fields; phone cannot offer or accept; a failed local save does not display the invitation. Record actual observations, not assumptions.

MODEL CONTROLS AND DEFAULT CONFIGURATION
Darkbloom’s generated provider memory_reserve_gb = 4 is supported for demand following and model pairs. BloomGauge keeps your configuration intact; do not delete it to clear a warning. Other custom memory/runtime overrides still need compatibility review through Help & feedback.
Optimizer Overview separates current model/readiness, automatic switching, and manual model controls. Start appears only when stopped; prior command results appear in Model & service details. Reading status in Terminal is harmless. External start/stop/model changes may pause automation to preserve your choice; review and enable it again in Optimizer when ready.

Resume after an outside pause or model change

Open Optimizer → Overview to review the current model and settings, then choose Optimizer on when ready. On may start a stopped provider once and waits for verified readiness before following the saved plan. Use Manual to choose and start a model yourself; this keeps automatic switching off. Settings lets you review the model pool and strategy without resetting history. These controls work on the Mac and an authenticated private phone connection; initial pre-warming setup stays Mac-only.

MANUAL RUNTIME VERIFICATION (beta18)
A downloaded model such as Qwen 3.8 can show Verify on switch on a supported, trusted M5 Mac. Choose Verify & switch to let Darkbloom check runtime support and BloomGauge check warm readiness. If it fails, BloomGauge attempts safe restoration of your previous ready model. Automatic selection stays restricted to verified models; this is not an attestation bypass.

MULTI-MODEL MONITORING (beta18)
BloomGauge now reports live earnings and traffic with three or more models selected in Darkbloom. It matches this exact Mac to the network, checks that every selected model is loaded and warm, and observes serving output before counting new warm time. Traffic is aggregate across the set; individual income uses confirmed model-specific credits. Darkbloom Monitor is optional. Other Macs' earnings and base rewards stay out of the live inference meter.

A multi-model run is labeled Managed by Darkbloom. Automatic demand-following still selects one model, and optional combination tests cover pairs. Three-plus-model optimization and historical income baselines are not included. Keep an existing productive model set to monitor it; no Terminal commands, purge or model restart are needed for this reporting update. Historical credits remain; missed live/warm samples cannot be reconstructed.

Existing local access records remain unchanged. They do not limit optimizer access or create a new deadline. Updating preserves intentional pauses and Manual mode.
