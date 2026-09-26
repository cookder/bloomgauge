# Beta38 tester guide: new name, now open source

**1.36.57 beta38/build13657 · candidate tester guide · Apple Silicon/macOS 14+**

This guide targets beta38 and does not certify implementation, signing, notarization or a physical installation. Install only after the organizer confirms the release and final checksum. Use the confirmed build and an approved isolated fixture for technical cases; leave all results **not_run** until actually observed. The separate kit introduction identifies the current public release and preparation status.

## New in beta38

- **New name.** After updating, the window, menu bar and About should say Bloomkeeper Beta. History, settings, the optimizer plan and the phone link should carry over. The app file in Applications may keep its old name; that is expected.
- **Pace units.** The Pulse meter should show cents per hour. Tap the number to switch to dollars; the choice should survive a restart.
- **Phone view.** With phone access on, your phone link should keep working after the update.
- **Help & feedback.** It should offer the support page, the Bloomkeeper Slack channel and Email support (support@bloomkeeper.io).

## Carried forward from beta37

- **Settings that stick.** Change the dashboard layout or dismiss the What’s new banner, quit Bloomkeeper and reopen it: the change should still be there.
- **Model names.** With more than one Gemma 4 26B variant downloaded, model lists should show which is which (8-bit, QAT 4-bit).
- **Stall recovery.** If work stops and Bloomkeeper lets the optimizer try another model, note whether it switched and how long it took.

## Carried forward from beta36

- **Memory for larger models.** With cache cleanup permission on, open a larger model in the optimizer’s model details: it should show "File cache cleared before switching". A model that only fits after cleanup should no longer say it needs more memory. If a switch still finds too little memory after cleanup, the current model should keep running.

## Carried forward from beta35

- **Learning time after a failed switch.** If a switch fails, the optimizer’s learning time used should stay within the limit you set, and learning runs should resume the same day.

## Carried forward from beta34

- **What’s included.** When a problem-report prompt appears, click "What’s included": a short list of what the report contains should open inside the prompt.
- **Automatic reports.** With "Send these automatically" on, the same problem should not send again within six hours.

## Carried forward from beta33

- **Optimizer page.** Open Optimizer: your plan should load, not stay on "Your plan is reconnecting". If stall recovery has restarted your provider, the run history should list it as "Stall recovery restart".

## Carried forward from beta32

- **Switching with Darkbloom 0.9.9.** Check `darkbloom --version`. On 0.9.9 or later, switch models in Manual while the Mac is serving: the switch should start right away, the old model should finish its accepted requests, and the new model should become Warm and ready. Note how long it took. Afterwards, `~/.darkbloom/daemon-state.json` should not show the provider drained with no model loaded.
- **Recovery from a failed switch.** If a switch fails, Darkbloom should be serving again within a few minutes. The optimizer stays paused until you turn it back on.
- **Learning time.** Learning runs should use the learning time you set (not a fixed two runs a day), each model at most every four hours. Setting learning time to Off should leave the optimizer page working.

## Carried forward from beta31

- **Overview sentence.** Earnings → Overview should open with one line like "Earning $0.06/hr on Gemma 4 26B · $1.90 today · optimizer watching demand". It should match the Pulse dial and the optimizer card.
- **One pace, in dollars.** The Pulse dial, My Macs and the optimizer should show the same pace (last five warm minutes, in dollars). My Macs should not show $0.0000 while a Mac is serving.
- **Only Bloomkeeper's window can change settings.** Open http://127.0.0.1:8765 in Safari on the same Mac: numbers should load, but saving any setting should say "Open Bloomkeeper on this Mac to change settings". The same change in Bloomkeeper's window should work. Phone access should work as before.
- **Phone data.** Leave the phone view open on Overview for 5 minutes and note whether it feels lighter; screens other than Overview refresh every 5 seconds.
- **Names and polish.** Each model should have one name everywhere (for example GPT-OSS 20B). The What's new banner should appear only on Overview. Pair tests that ended show their date.
- **Automatic problem reports.** On the phone you should be able to turn them off but not on.
- **Optimizer explainer.** On a Mac with no saved optimizer plan, choose Optimizer on. A "What the optimizer does" window should appear with Turn on with 3-day boost, Turn on without boost and Not now. With the boost, check Learning boost shows about 3 days left once the optimizer is fully on. "What it does" on the optimizer card reopens the window without turning anything on.
- **Serving model on Pulse.** Earnings → Overview should show "Serving" and the current model (both for a pair) above the gauge, and the demand arc should describe the same model.
- **My Macs.** With two or more Macs connected, check the combined live pace, the Models across your Macs table and each Mac's demand label. A sleeping Mac should show Unreachable and drop out of the combined pace.
- **Stay in touch (optional).** More → About Bloomkeeper: Save needs an email or @handle and the consent tick; Remove should delete it. Only use your own contact details.
- **Improve starting estimates (optional, off by default).** Turning it on should report how many models were shared; turning it off should say the summary was deleted.
- **Stall recovery and shadow estimates.** If a stall panel appears on the optimizer page, record the steps it lists and whether work resumed.

## Carried forward from beta29

- **Protect level and learning time.** On the optimizer card, set Protect earnings above and Learning time. With the optimizer on, confirm Bloomkeeper does not start a learning run while the current model pays above your protect level, and that learning runs stay within the daily time you chose.
- **What Bloomkeeper knows about each model.** On the optimizer page, check that each model shows expected pay, measured time and when it was last measured, and that barely-used models are not picked for learning.
- **Optimizer style.** Move the slider from Very passive to Very aggressive and open Fine-tune to see the values change together. Type one value; the slider should show Custom. Out-of-range values should be refused with the allowed range shown.
- **Learning boost.** Start a 24-hour boost, save a plan change while it runs, then stop it. Your own limits should be unchanged afterwards.
- **Stall recovery.** If paid work stops for several minutes while network demand holds, note whether Bloomkeeper sent a test request, restarted or tried another model, and whether work resumed. Record the times in your result file. Do not try to cause a stall.
- **Live readings.** Leave the dashboard open for 10 minutes. The optimizer status should not flip to "Waiting for fresh local readings" every half minute.

## Dashboard layout

On Earnings, use **Edit layout** to reorder a card with a drag handle and with the move buttons. Hide a card, restore it, resize a desktop card and save. Reload to check persistence in that browser. Verify Cancel discards changes and Reset previews defaults before Save. Check desktop and phone layouts separately, including a narrow phone view; neither should overwrite the other. Confirm rearranging cards does not change reporting, plan settings or provider state.

## Experimental next-hour Earnings Outlook

Inspect a model with supported recent paid pace, one with complete historical hours, one with measured zero and one with missing history. Missing evidence must not become a zero-dollar forecast. The display must state its already-warm assumption and that it does not predict future changes in demand. In an isolated fixture, briefly fail refresh: the saved value may remain only for its original forecast window. Once that hour ends, it must not silently extend the forecast.

Verify customer forecasts persist locally through reopen, remain isolated by account and Mac, and are compared only with complete observed outcomes. Unselected models have no assumed earnings. Interrupted/incomplete hours stay excluded; later credit corrections can revise outcomes without rewriting the original prediction. Setup preview must create no journal activity. Viewing or recording a forecast must not enable automation, start a model, change usage consent or start the optimizer trial. No forecast-accuracy or earnings-uplift result is assumed.

## Trial reviews and earnings confirmation

Use isolated synthetic fixtures. An ordinary comparison needs a qualified saved paid benchmark. A weak or inconclusive trial cannot repeat immediately without the required new evidence. A review that cannot resolve by its deadline must close as inconclusive and release active review ownership. Check the displayed paid alternative and why it can or cannot switch; closure alone must not force an unavailable model.

Exercise fresh demand independently of the alert cache. A brief data or memory interruption may pause confirmation while retaining earned progress within its limits. Missing intervals, duplicate samples and elapsed wall time without evidence must not add confirmation credit. Check expiry and renewed evidence, and verify final identity/resource/freshness checks still govern dispatch.

## Manual selection, Optimizer on and optional cache cleanup

Choose a model different from the saved selection while stopped. The primary action must name and start that selected model, with automatic switching off. While running, the action must identify the selected switch target. Stale readiness from a previous session must not claim that a stopped model is ready. Ordinary model selection should not require Terminal commands.

In an isolated fixture with insufficient memory and eligible cleanup permission, test both Manual Start and Optimizer on. Each explicit operation may attempt one cleanup, respects the shared cooldown, then requires a newer measured memory reading with enough space before loading. Duplicate clicks, lost/5xx replies, reopen, changed account/session/config and choosing Manual must not create another cleanup or stale Start. On should keep its preparation state visible rather than briefly appearing to revert to Manual. Failed recovery must explain the remaining shortage.

Inspect the optional permission controls in the installed Mac app and the Mac-only explanation on the phone. Setup/removal must use the native macOS approval flow, accept only Bloomkeeper's fixed no-argument purge permission, and never start a model. Cancel must preserve the model selection. Use mocked commands for technical cases; leave actual administrator grant/revoke, live purge and physical model-start results **not_run** unless deliberately observed by the tester during normal use.

## Retained controls and report behavior

Keep reporting and Manual controls usable. Optimizer access is included without a countdown; opening charts or updating does not enable automation.

Verify reports still require review and explicit Send independently of optional usage sharing. Earlier unconfirmed reports are not recovered or automatically retried. A matching receipt establishes delivery; a lost receipt remains unconfirmed. Do not send live test reports solely to fill out this checklist. Independent customer Mac/phone installation, real permission changes, sleep/wake, update/rollback and earnings improvement remain **not_run** until observed.

## Earnings and model comparison updates

- Pulse opens with Earnings bars over Last 1h. Inspect model-colored intervals, choose the other graph styles and change the range; verify the selection is retained in the same browser. Confirm signed credits, covered zero and missing intervals remain distinct. The warm-time meter and elapsed-time bars use different denominators.
- In Manual model selection, inspect recorded warm-hour pay, output size, historical next-eight-hour demand and conditional income. Unknown evidence must stay unknown; a conditional estimate is not promised income. Inspect the live optimizer panel and its detailed comparison without issuing a model command for QA.
- In Network, inspect Activity, Requests, Output tokens and This Mac earnings across ranges. Activity is active plus queued concurrency, not completed traffic. Completed work has no invented model allocation; earnings exclude base rewards and other Macs. Missing roster values/coverage remain partial or gaps, including Other and summary totals.
- Verify weekly traffic follows the viewer's current local hour, preserves a separate Now marker during inspection and supports Current hour. This is whole-network traffic, not a switching recommendation or predicted local pay.
- Check credit sorting, model/reward filters and totals against confirmed records with the displayed account/device scope. Clear filters without changing credits or plan settings.

## Audit corrections and conservative experiment reviews

Use disposable synthetic fixtures for technical policy cases. The supervisor's local runtime observation is separate from customer installation or proof of added income.

- Evaluate the same five settled calendar minutes at multiple offsets within a minute. Rates and recent-pay protection must remain consistent; unsettled/future credits, real coverage gaps and other sessions remain excluded.
- A well-supported paid opportunity must not acquire the speculative discovery pressure floor solely because current income is below the aspiration. Running-experiment ownership, productive recovery, freshness, dwell, confirmation and command-time guards still apply. Incomplete evidence is not measured zero.
- An ordinary experiment has a purpose, saved comparison, review deadline and explicit keep/return/alternative/hold/inconclusive outcome. A deadline requests review, never a blind command into an unavailable or stale incumbent. Record clock occupancy separately from restart downtime.
- The discovery sampling budget shows elapsed minutes used over the last 24 hours, the limit and the reservation needed for another trial. At the limit, new discovery trials are held while returns and independently supported paid upgrades remain eligible. Unresolved legacy lifecycles retain their reserved allowance; completed trials awaiting review remain a hold only until resolved or closed as inconclusive at the bounded deadline.
- Receiving a tiny payment proves payment was seen, not economic success. Competitive outcomes require a sufficient contemporaneous comparison; sparse, stale or unmatched results stay uncertain. Existing history, original forecast windows and intentional pauses remain intact.
- Leave physical recovery, independent updates, economic uplift not_run unless actually observed. No live provider interruption, purge or artificial switch is needed to run these cases.

## On/Manual controls and serving-status compatibility

- Verify explicit On starts a reviewed stopped provider at most once, reports its observed progress, waits for warm/identity/resource checks and resumes the exact saved pool, policy, dates and history.
- Choose Manual while readiness or Start is pending. No later step may enable automation. A command already dispatched may finish; Manual keeps serving work separate from Stop.
- Reuse the exact request after a lost response. Match its receipt, not merely a healthy GET; no duplicate Start occurs. Restart and timeout never replay an unfinished On request.
- Keep cached controls responsive while historical summaries, service inspection or readiness checks are deliberately delayed in isolated fixtures. Stale/failed analytics stay separate from current control state.
- Verify completed-plan, unavailable serving model, unsupported multi-model automation and update blockers give one useful action. Phone controls must not bypass Mac-only endpoint setup.
- Observe increasing three-model output through online-to-serving-to-online roster transitions. Pulses/session remain live. Offline/unknown/non-hardware/wrong-identity rows revoke proof; changed mapping requires fresh serving output.
- Keep physical customer Mac/phone installation, natural expiry, real sleep/wake, customer recovery and economic improvement marked not_run unless independently observed.

## Three-plus-model reporting continuity

An already matched Mac with all selected models warm and observed serving output can keep aggregate live readings through a brief network roster timeout. The original verification expires after three minutes; a timeout does not extend it. Cold, stale, changed or authoritatively invalid provider state still pauses reporting. A successful verification refresh must not briefly reset an otherwise continuous live pulse.

- In an approved isolated fixture, test a transient roster timeout and a successful refresh while the same warm session stays fresh. Confirm continuity without adding credits or reconstructing missing readings.
- Check the original verification deadline and cold/stale/changed/invalid states still stop live reporting. A roster success alone after a real gap cannot claim new serving output.
- Provider controls must still fail closed on every roster refresh error. Aggregate multi-model readings never become solo/pair optimizer evidence.
- These are engineering fixture cases. Independent customer installation and recovery remain not_run until observed; do not interrupt a customer's provider to reproduce them.

## Bounded recovery after a genuine gap

After a real cold, stale or changed-session gap, a newly valid warm model set requests fresh roster verification from the existing background loop. Extra checks are coalesced and limited to one per15seconds; an unchanged failed scope keeps its normal retry delay. The old proof is never reused, and new output is still required. The traffic meter needs20seconds of verified warm intervals after recovery. Diagnostic identity warnings now distinguish automatic-control checks from separate multi-model reporting.

- In an isolated fixture, recover all selected models after a cold or changed-session gap. Verify fresh roster confirmation and new output before live readings resume.
- Repeated transitions must not trigger roster checks more than once per15seconds. Cold, invalid, pending and solo/pair states must not use this request path.
- A three-model diagnostic control-identity warning must not claim the separate reporting match is missing.
- Customer recovery and physical-device behavior remain not_run until observed.

## Daily earnings colors and outlook

Green starts at $2.50/day, purple at $3/day and gold at $4/day. Calendar cells remain confirmed credits. Today’s end-of-day card adds only the expected remaining earnings through local midnight to confirmed today, using current-model settled warm history with time slots and decaying recent pace. Base rewards are estimated separately from at least three complete covered days; otherwise future base rewards are explicitly excluded. The card follows the model filter but uses full today independently of the selected calendar range. Missing/stale coverage or an unsupported model history withholds a total. Hour-end earnings totals use the equivalent hourly color scale.

- Verify each boundary, local midnight and model/date filters. Confirm incomplete history never produces an apparently confirmed prediction.
- Independent Mac/phone layout, pause/refresh and real history checks remain not_run until observed.

## Qwen runtime verification

On a supported M5 Mac with a verified ready model, a downloaded capability-dependent model may show **Verify on switch**. The action names the selected target; choosing the offered verification action explicitly requests a manual attempt. Bloomkeeper checks Darkbloom network eligibility and warm readiness after startup. If verification fails, it attempts safe restoration of the previous ready model. This does not mark an unverified model eligible for automatic selection. Keep unsupported hardware, stale identity, missing files/template and insufficient-memory cases blocked. Verify Mac and authenticated-phone behavior only when deliberately testing your own provider; leave physical cases not_run until observed. No Terminal command or routine purge is required.

## Customer flow and privacy

Use **Review and report**, or **More → Help & feedback → Report a problem**. Description (up to 2,000 characters) and contact (up to 254) are optional and blank by default. **Review report** shows the exact payload; **Send to Bloomkeeper support** is a separate deliberate step. Opening, detecting, previewing, canceling and dismissing do not upload. Changing text requires a new review. Suggestions are deduplicated locally; dismiss snoozes prompts for 24 hours while manual reporting remains available.

The automatic summary excludes earnings, raw errors/stacks/logs, credentials, account/device/license/analytics identifiers, local paths and private URLs. Text you deliberately type in notes/contact is included as entered: review it and omit secrets or personal account details. Contact is optional and never filled in automatically.

The small automatic summary can contain app/provider versions, coarse OS/chip/memory, report category/context, readiness/failure/recovery and up to three fixed source-status/error categories. A random identifier is created for this report; it is not a stable device or analytics identifier. The ordinary full diagnostics export is not sent as the support report. If capture fails, unavailable/unknown values should be shown rather than invented readings. Deliberately supplied notes/contact are visible to the owner as plain text and are subject to the report retention policy.

Reporting works independently of usage sharing, optimizer access and phone/optimizer configuration. It enables no provider action, subscription, automatic follow-up or background retry. A fully closed/dead app or unreachable collector cannot send; use retry/support guidance. This feature is not a promise to capture every crash.

## Delivery, offline state and retry

A matching receipt confirms one report. An offline failure, timeout or lost receipt stays **unconfirmed**: it may already have arrived. Nothing retries automatically on reconnect/relaunch. **Retry same report** reuses the frozen ID, exact bytes and independent report credential while the review is valid. Matching retries produce one logical report; a conflicting payload/credential cannot overwrite it. Duplicate clicks must be serialized. A preview expires within 10 minutes and must then be reviewed again. A newly created report is a separate submission and is not promised to deduplicate a previous unconfirmed report.

Keep the report ID locally if you may contact support or request deletion. Never copy the review token or report authorization secret into feedback. No persistent background upload queue is intended. Late responses or report-system failures must not falsely claim success or recursively open new prompts.

## Customer and isolated client cases

| Result-template key | Expected observation | Initial result |
| --- | --- | --- |
| `reportManualAvailableWithoutDetectedIssue` | Help offers Report a problem even when there is no current incident. | **not_run** |
| `reportContextualSuggestionDoesNotUpload` | A contextual suggestion uses a fixed category/context; detection and opening send no hosted report. | **not_run** |
| `reportSustainedConnectionPromptThreshold` | A short polling interruption is not a repeated outage prompt; the sustained-failure threshold is at least 60 seconds in an isolated fixture. | **not_run** |
| `reportDedupAnd24HourDismiss` | Dismiss snoozes suggestions for 24 hours and local dedup prevents repeated prompts; manual reporting remains available. | **not_run** |
| `reportHostFailureDoesNotRecurse` | Failure of the reporting UI offers support fallback without generating recursive report prompts. | **not_run** |
| `reportOptionalTextBlankAndNotAutofilled` | Description/contact start blank, remain optional, and respect 2,000/254-character limits without autofilling contact. | **not_run** |
| `reportWorksWithoutUsageSharingConsent` | Preview/send remain available with usage sharing off and never change consent. | **not_run** |
| `reportExactPayloadReviewedBeforeSend` | Send is a separate explicit action after the exact small report payload is shown. | **not_run** |
| `reportEditingInvalidatesPreview` | Changing description/contact clears the old review; a new preview is required before sending. | **not_run** |
| `reportCloseBeforeSendDoesNotUpload` | Closing, canceling or dismissing before Send creates no hosted report. | **not_run** |
| `reportMinimalSummaryExcludesEarningsAndIdentifiers` | Automatically included fields contain only the allowlisted summary, not earnings, raw errors/stacks/logs, credentials, private paths/URLs or stable IDs. | **not_run** |
| `reportDiagnosticFailureUsesUnknownSummary` | A diagnostic-capture failure is labeled unavailable/unknown and does not invent readings or prevent optional review. | **not_run** |
| `reportExplicitSendPreservesProviderAndPlan` | One explicit send uploads only the reviewed report and changes no provider, optimizer plan, trial or settings. | **not_run** |
| `reportSuccessRequiresMatchingReceipt` | Sent is shown only after a successful service acknowledgment with the matching report ID; keep the ID for support. | **not_run** |
| `reportOfflineTimeoutStaysUnconfirmed` | Offline/lost receipt/timeout stays unconfirmed and explains that the report may already have arrived. | **not_run** |
| `reportNoAutomaticRetryOrPersistentQueue` | Reconnect, waiting, reopening or analytics opt-in does not trigger a queued/background upload. | **not_run** |
| `reportExplicitRetryKeepsFrozenIdAndPayload` | Retry same report uses the same frozen ID, contents and authorization; a matching retry creates one logical hosted report. | **not_run** |
| `reportDuplicateClicksSerializeSubmission` | Concurrent/repeated Send clicks do not create different report IDs or duplicate in-flight submissions. | **not_run** |
| `reportConflictingRetryCannotOverwrite` | A reused ID with changed payload or credential is rejected; test only against an isolated fixture service. | **not_run** |
| `reportExpiredPreviewRequiresNewReview` | After preview expiry, Send requires a newly reviewed report; a new report is not promised to deduplicate an earlier unconfirmed one. | **not_run** |
| `reportLocalAndPhoneActionGuards` | Existing authenticated local/private origin/action guards apply; unauthorized/cross-site requests cannot send. | **not_run** |
| `reportQuotaAndAvailabilityErrorsAreSafe` | Quota/unavailable/malformed responses are bounded, safely worded and never treated as successful delivery. | **not_run** |
| `reportUnreachableCollectorUsesSupportFallback` | An unreachable/closed collector shows retry/support guidance without claiming complete crash capture or confirmed delivery. | **not_run** |

Use disposable, isolated fixtures for forced timeouts, duplicate transport, expiration, unauthorized origins, schema conflicts and quota behavior. No real model switch/purge, clock change or live support upload is needed to manufacture these cases. Technical fixture evidence does not establish a physical phone/Mac pass.

## Owner-only inbox and service cases

These are for the authorized owner or an explicitly assigned reviewer using a disposable test service. Ordinary testers leave them not_run. The private inbox receives only voluntarily submitted reports; do not treat its count as all failures, affected Macs or installations. There is no public report retrieval and no automatic contact/reply action.

| Result-template key | Expected observation | Initial result |
| --- | --- | --- |
| `ownerReportInboxRequiresOwnerAuthorization` | Anonymous, forged-header and non-owner requests cannot read private reports or contacts. | **not_run** |
| `ownerReportInboxMatchesReceivedPayload` | The authorized inbox shows the submitted report and optional text accurately; public pages/responses do not reveal its body. | **not_run** |
| `ownerReportInboxPaginationAndTotal` | The owner inbox shows 20 reports per page with working next/previous navigation and labels the total as submitted reports, not all incidents or unique Macs. | **not_run** |
| `ownerReportTextIsPlainAndNonExecutable` | Description/contact render as plain text; markup-looking input does not execute or become automatic links/actions. | **not_run** |
| `ownerReportDeletionExplicitAndProtected` | An authorized explicit same-origin/action-protected deletion removes the selected report; GET never deletes it. | **not_run** |
| `ownerReportRetentionAndCaps` | Isolated fixtures verify 30-day retention/cleanup, 100-new/day quota and maximum 3,000 retained rows without sending test traffic to the live inbox. | **not_run** |
| `ownerReportsStaySeparateFromAnalytics` | Reports/contacts are not joined to analytics, license or device identity; no automatic Slack/email reply is sent. | **not_run** |

Contract limits: five new native submissions/hour; 100 new hosted reports/day; a maximum 3,000 retained records; 30-day retention with cleanup on submission/owner access; 20 reports per inbox page, next/previous pagination and total. Verify those bounds with seeded isolated data and a controlled test clock. Notes/contact must render only as text. Reads and deletes require owner authorization; deletion is an explicit protected action, never a GET. Do not flood or seed the live inbox, send real credentials or test someone else's account. Public hosting necessarily handles ordinary connection metadata; the support payload/database must not add stored IP/device identifiers or analytics joins.

## Retained app checks

The prior 109 baseline check keys remain in RESULT_TEMPLATE.json with fresh not_run values. Use the baseline guide below for setup, fixed local free optimizer access, updates, phone, saved diagnostics, history, fleet and earnings boundaries. These descriptions do not carry forward any test result. Natural expiry, independent hardware/phone, sleep/wake and version-specific update/rollback remain unverified until observed.

Before updating an older beta, let any model switch or warm-up finish. Beta5/6/7 need a manual current-version installation to gain the updater; beta9/10 first upgrade uses their old quit behavior. Beta11 and later retain optional checks and explicit download/install approval. Let the organizer confirm the actual release before installing this target.

Use the [support page](https://bloomformac.com/support), the [Bloomkeeper Slack channel](https://darkbloom.slack.com/archives/C0C4HC8HZLN), or [email support](mailto:support@bloomkeeper.io). Opening a contact sends nothing automatically. Read the [privacy notice](https://bloomformac.com/privacy) for the organizer-confirmed release. Local diagnostic export, optional earnings and optional usage sharing remain separate choices.

## Baseline guide carried forward for beta38 verification

# Bloomkeeper beta checklist

**1.36.20 beta24 · Apple Silicon · target macOS 14+**  
Use the first-session steps below to install and start in Free/Observe. Provider/model requirements can be higher; independent hardware and OS support are still being tested.

This guide is prepared for beta24; it does not certify signing, notarization or installation. Install only after the organizer confirms the release and its final installer checksum. Keep independent Mac, phone and other unobserved checks **not_run**.

Use **passed**, **failed**, **not_run** or **not_applicable** in RESULT_TEMPLATE.json. Explain failures and skipped cases without personal details. A blank or untested item is not a pass. Record exact macOS version/build from About This Mac; a marketing name alone is insufficient.



## Beta11 setup and update checks

Use an ordinary setup or operation you would perform anyway. For expired Darkbloom login, verify that setup asks for sign-in; an API/network gap should remain unconfirmed and retry. No raw error, token or private URL should be exposed. Confirm **Optimizer → Overview → Manual model controls** and **Optimizer → History** reach the intended controls and comparisons.

If a phone setup action naturally times out after 30 seconds, the UI should retain the last confirmed state, explain that the operation may still be finishing, and offer refreshed status. A new change stays unavailable until fresh operation status confirms completion, including after Bloomkeeper relaunch. Do not repeatedly press an uncertain action or run real Tailscale/provider commands to manufacture a failure. Late-response, pending-operation and relaunch fixtures are separate technical evidence; unobserved physical cases remain **not_run**.

Verify an approved update defers install/relaunch when a switch or warmup is active and gives a useful retry message. Do not start or interrupt paid work to create this test. Busy, unavailable, canceled or expired admission must leave provider and optimizer settings unchanged; retries occur by user choice after it is safe. Verify normal approved replacement/relaunch and preservation of preferences/history independently. Beta11 changes native updater/lifecycle behavior, so earlier beta8 evidence alone cannot mark these checks passed. The organizer's fresh isolated Sparkle exercise is separate from an independent customer-Mac update or rollback.

## Switching and recovery checks

Beta10 improves a reproduced race where new paid work or temporary capacity changes could make synthetic warmup report a failed switch. Verification keeps its original deadline and follows the new provider session; synthetic attempts are bounded. Paid serving work can verify readiness without a synthetic request. This is a tested fix, not proof of what caused a particular earlier tester failure.

For an intentional switch you would make anyway, record the requested model, time/timezone, Bloomkeeper version, Darkbloom version and whether paid work began. Check the last-switch timestamp and distinguish a retained failure from current readiness. A failure should describe the primary problem and any recovery result separately; it should not classify every failure as a memory or purge problem. Review optional diagnostics for safe switch-failure, cache and readiness details before saving or sharing.

Keep unobserved busy/capacity/deadline/stop/session-race checks **not_run**; engineering fixtures are separate evidence. Do not stop a productive provider, change the Mac clock, alter privileges or run purge just to manufacture a result. Purge admission and once-per-session/ten-minute limits are unchanged. A failed switch does not itself establish that a purge was required, permitted or executed. Explicitly stopped providers must remain stopped.

## First session: Free and Observe

| Check | Expected result |
|---|---|
| Browser download and install | The supplied signed/notarized DMG opens under normal macOS protections; drag to Applications and launch without developer tools. Record artifact version/checksum from the invitation and any installation error. |
| Account connection | Sign in through Darkbloom on this Mac. Bloomkeeper reports fresh confirmed earnings; “Login found” or a running provider alone is insufficient. Do not send credentials or use a support account. |
| Setup | Your Mac → Make it yours → Ready to observe → Open dashboard. Optional electricity price stays unknown when omitted. Observe starts without model changes. |
| Free access | Reports, manual-control screens and optional private phone access remain available without usage sharing. More → About Bloomkeeper explains free optimizer access. Viewing controls must not issue a command. |
| Reports | Compare settled credits with the official account view and source timestamps. Keep inference credits, base rewards and forecasts distinct. Try short/long ranges and model filters; history gaps remain visible. |
| Diagnostics | More → Help & feedback → Review diagnostics. Earnings are excluded by default. Review View exact report, save to a chosen location, and verify canceling Save leaves the app usable. Share only if you choose. |

Never run personal and beta collectors against the same provider together. Beta history is separate; a new installation does not import another Bloomkeeper app's history.

## Start in Free/Observe: check reliability at your own pace

- Close the dashboard window and reopen from the menu bar. Collection continues while Bloomkeeper and the Mac remain running.
- Quit and reopen Bloomkeeper. Saved history, setup and tariff remain; Observe remains selected during this phase. Quitting Bloomkeeper stops its collection/automation and leaves Darkbloom's existing provider state alone.
- Let your Mac sleep and wake normally. Record any gap; no collection during sleep is expected. After waking, wait for fresh source timestamps before calling it recovered.
- During an ordinary disconnection or wake-up, cached values must be marked stale/reconnecting or unavailable. They must not appear as newly confirmed money. Record an unobserved outage as not_run; do not interrupt earning work solely to create one.
- Optional Open at Login: enable it only if wanted and verify on a normal later login. Record not_run if no login occurred.

If you see an unexpected model command, account mix-up, exposed credentials or lost history, pause automation and DM the person who sent your Bloomkeeper beta invitation before continuing.

## Optional usage sharing

Sharing starts **off**. It requires an explicit choice on this Mac and can be changed later in **More → About Bloomkeeper**. Declining or turning it off does not reduce any Bloomkeeper feature. Read the [usage privacy notice](https://bloomformac.com/privacy) before choosing.

If you opt in, Bloomkeeper sends a separate random analytics ID, UTC day, app and macOS versions, chip family, memory band, setup status and daily dashboard, phone and optimizer activity flags. Older versions may include historical access flags. These are not purchases or a count of all users. Reports update at most every six hours apart from consent and relevant setup changes.

After deletion, the service keeps a one-way deletion receipt for 30 days to reject a delayed upload. The receipt contains no raw analytics ID, credential or usage report. New consent creates a new analytics identity. See the privacy notice for details.

The analytics ID is separate from the installation ID. Keep identifiers and credentials out of feedback. Reports exclude earnings, raw logs, private links and account details.

| Check | Expected result |
|---|---|
| Consent and access | Sharing is off on a new install. No upload occurs before explicit Mac consent; declining preserves Bloomkeeper features and phone access. |
| Allowed fields and identities | Reports contain only the coarse fields above. Analytics identity is separate from licensing; do not share either identifier or the write/delete credential as evidence. |
| Actual use | Closing the dashboard while Bloomkeeper continues running must not count a new dashboard opening. Daily flags persist across restart and repeated updates do not create more installations. |
| Disable and delete | Turning sharing off stops new uploads immediately and requests deletion of that analytics ID's hosted records. Completion is shown only after the server confirms deletion. |
| Offline deletion and retry | If offline, the app shows deletion pending and keeps only what is needed to retry deletion, with no new measurements queued. Reconnect and retry from the usage controls; pending clears only after success. An upload already in progress must settle before deletion so it cannot restore deleted records. |
| Service failure | A usage-service failure does not stop Bloomkeeper’s local reports, the provider or optimizer. Do not interrupt earning work just to manufacture a failure. |

If Bloomkeeper says it could not save your opt-out, sharing has stopped only for that running session. Keep Bloomkeeper open and retry before quitting; do not assume the choice will survive a restart until it is saved.

Payload, timing and in-flight checks need an observed result or reviewed technical evidence; reading this guide is not a pass. Leave them **not_run** if you cannot verify them. Never put network authorization headers or identifiers in feedback.

## Free optimizer access

Bloomkeeper, including the optimizer, is free while we evaluate whether it improves earnings over Darkbloom alone. Optimizer access has no scheduled expiration. Updating never turns automation on or resumes an explicitly paused plan. Choose Optimizer on separately when ready. Existing settings, history, access records and privacy choices are preserved.

- Verify new, previously expired and existing installations offer optimizer access without activation.
- Verify Mac and authenticated phone controls retain the same access, with private setup still on the Mac.
- Keep Manual/paused mode unchanged through update and reopen. Do not start a real model just for QA.
- Verify optional analytics remains independent. Historical access records must remain unchanged.
- Test malformed records and clock changes only in disposable technical fixtures, never on your real account.

## Optional phone and multiple Macs

Use **More → Phone access** on the Mac. Sign in to your own Tailscale account on both Mac and phone, connect both, then choose **Enable phone access** and scan the private QR code. Use a Tailscale plan appropriate for your use. Keep the Mac awake and online.

On a real phone, check navigation, charts, background/return, Wi-Fi/cellular changes and diagnostics save/share. Record phone OS/browser and Safari/Home Screen results separately. Do not share the private link or QR code.

For **My Macs**, start with two independently installed betas under the same Tailscale owner before extending to five. Add the second Mac using its Phone access address; keep that address out of your result file. Compare device-attributed inference totals and offline/partial coverage with each Mac. Shared account balances and base rewards must not be counted twice.

Fleet reporting is combined; each Mac's optimizer remains independent. Open a named Mac's own dashboard for its controls. There are no pooled controls or managed cloud access. A combined phone view needs its host Mac online. Do not delete real history to test changed device identity.

## Updates, results and leaving

Beta5/6/7 updates to the current release require a manual installation. Beta11 retains the optional update checks described below; every installation still requires your approval. Keep a backup of the app and its beta data before updating. Do not downgrade or delete history without the organizer's compatible rollback instructions. An isolated update test does not certify arbitrary versions or rollback.

DM your inviter with the reviewed result file and reproducible steps. Diagnostics, earnings and optional usage sharing are separate choices. Exclude names, serial numbers, account/installation/analytics IDs, credentials, private URLs, raw databases and full logs.

Optional earnings comparisons use EARNINGS_TEMPLATE.csv and agreed same-Mac baseline/optimizer periods. Count intended clock hours, including idle/switching/recovery; separate base rewards, electricity and missing coverage. No earnings or improvement is guaranteed.

To leave, turn off optional usage sharing first if enabled. If deletion is pending, keep or reopen Bloomkeeper with internet access and retry until completion; removing the app while pending prevents it from completing that retry. Then pause automation, turn off Phone access and Open at Login if enabled, quit and remove the beta app. Removing the app preserves its history. Any optional cache-recovery authorization has a separate removal procedure; DM your inviter for help.

## Optional suggestions after a couple of hours

After setup and about two accumulated hours of fresh, connected monitoring, a compact card below the live Pulse may introduce private phone access and the optimizer. Healthy readings with zero earnings still count. Time with Bloomkeeper closed, asleep, in setup preview or unable to read fresh provider/earnings data does not count. The card waits quietly during connection errors.

**Set up phone access** opens Mac setup; **Review optimizer setup** opens optimizer controls. Neither starts automation. Account and phone setup stay on the Mac.

Each suggestion has **Later · 24 hours** and **Don’t show again**. These choices and the measured monitoring time are stored locally on this Mac, survive relaunches and app updates, and do not depend on optional usage sharing. The phone suggestion stops once phone access has been configured or used. The optimizer suggestion stops once an optimizer plan has been started, including a subsequently paused plan. Opening a setup screen snoozes its suggestion; it is not treated as enabling the feature. No analytics fields, notification permission prompt or Slack message is added by discovery.

For independent checks, confirm an incomplete or stale setup stays quiet, normal use eventually reveals the card, both direct actions open the intended screen without changing the model, and “Don’t show again”/snooze choices survive closing and reopening Bloomkeeper. Leave checks not_run until actually observed; never change the system clock to simulate elapsed time .


## Updates and help

Before updating an older beta, let any model switch or warm-up finish. The installation guard is part of beta11. Beta9/10 use their previous quit behavior for the first update to beta11; the new guard protects later installations after beta11 is running.

Beta5/6/7 need one manual current-version installation; beta9/10 already have optional checks. In beta11, **Notify me about app updates** appears on the final native setup screen and in **More → Help & feedback**. Help shows the installed version, check status and **Check for updates**. Existing preferences must be preserved, and viewing the screen must not enable checks. Opted-in checks run about every six hours while Bloomkeeper is running. Declining leaves manual checks available; the native menu remains usable. Every download/install/relaunch still requires approval. Phone/browser Help directs you to the Mac. Usage-sharing consent remains separate.

Check the permission-declined, permission-accepted, **Skip This Version**, **Remind Me Later**, cancel, and network-failure states. Verify an approved signed update preserves setup, local history, historical access records, usage-sharing choice and discovery dismissals. Quit/update must save Bloomkeeper state and leave the provider running. Keep these independent checks `not_run` until observed. Local isolated tests do not establish an independent downloaded update or rollback.

Updates use the separate HTTPS appcast at https://bloomformac.com/updates/beta.xml and signed versioned installers. No account/license/analytics identifier or system profile is added. Preview launches make no update checks or permission prompts. Update checks and optional usage sharing have independent settings.

**Help → Help & Feedback…** in the Mac menu and **More → Help & feedback** on Mac/phone offer the support page, **Join the Bloomkeeper Slack channel** and **Email support**. Open a contact without preparing diagnostics; verify nothing is sent automatically. Diagnostics remain optional, previewed, and saved/shared only by your action.

MANUAL MODEL CONTROLS
Open Optimizer > Overview and expand the manual model controls. Choose a downloaded available model, then Start if stopped or Switch to change models. Stop Darkbloom asks for confirmation, interrupts requests and pauses automatic switching. Manual controls work on the Mac and authenticated phone; unavailable selections explain why. Manual model actions pause automation. A queued manual change waits for 12 seconds idle, then can interrupt active requests after five minutes if still busy.
Pre-warming setup is part of the selected-model action on the Mac: use Prepare for the current running model, or Start or Switch for your selected model. This adds an authenticated loopback endpoint alongside the coordinator; there is no separate Enable pre-warming button or Controller tab. Endpoint configuration stays Mac-only; once prepared, model controls work from your authenticated phone. No Terminal window needs to remain open. Follow any authentication/bind or readiness message in Model & service details, or open Help & feedback. Do not use --local, which runs without the coordinator. Routine switches do not require a cache purge.
Standard Mac shortcuts: Command-H hides Bloomkeeper; Command-W closes its window while monitoring continues. Reopen from the Dock or menu bar. Command-V pastes into text fields.

OPTIONAL USAGE INVITATION (beta24)

After setup has been complete for ten minutes, Bloomkeeper may show one small invitation on the Mac's Pulse page. It asks to share limited setup/feature reports; nothing is enabled by displaying it. Share optional usage is affirmative consent. No thanks stays local. A saved prior opt-out, active sharing or deletion suppresses the invitation. The invitation is recorded locally before display, so relaunches/updates do not repeat it. Manage the choice any time in More → About Bloomkeeper. Phone users and setup previews do not receive the invitation. Features and trial access are identical either way.

Independent checks: decline/relaunch stays quiet; prior opt-out stays quiet; no report before consent; explicit consent reports only disclosed fields; phone cannot offer or accept; a failed local save does not display the invitation. Record actual observations, not assumptions.

Resume after an outside pause or model change

Open Optimizer → Overview to review the current model and settings, then choose Optimizer on when ready. On may start a stopped provider once and waits for verified readiness before following the saved plan. Use Manual to choose and start a model yourself; this keeps automatic switching off. Settings lets you review the model pool and strategy without resetting history. These controls work on the Mac and an authenticated private phone connection; initial pre-warming setup stays Mac-only.

## Three-plus-model reporting regression

On an independent Mac already serving three or more models, update Bloomkeeper without changing Darkbloom. With every selected model warm and current traffic, confirm the earnings and traffic pulses appear after fresh matching/output and enough covered time. Darkbloom Monitor is not required. Confirm a Managed by Darkbloom label and the separate solo/pair optimization limitation. Compare confirmed model-specific credits with the account ledger; do not equate account-wide income with a single fleet Mac. Stale, stopped or partially cold runs must show a paused/unknown pace. Do not change a productive setup just for this check. Record actual observations; fixture tests do not fill this result.


## Beta24 reporting-delivery checks

Use an approved isolated fixture or an explicitly agreed customer report; do not insert synthetic reports into the production owner inbox for release QA. Leave these independent outcomes not_run until observed. The release technical checks use invalid, non-storing requests and cannot prove an actual customer's submission.

- Review a new report, confirm Send, and verify the matching report ID in the authorized inbox.
- Confirm that an earlier failed or expired preview is not silently uploaded after updating; the user reviews and sends again.
- With optional usage consent enabled, verify delivery independently of problem reports.
- Verify opt-out/deletion confirmation without changing consent or uploading measurements after opt-out.

## Beta27 independent status and navigation checks

Verify one status disclosure at desktop and narrow phone widths, neutral paused/background state, prompt attention for old earnings or clock mismatch, and focus recovery. Brief interruptions may retain already-confirmed Pulse data only within the unchanged freshness bound. Verify public website/setup/support/changelog links and current Prepare/Start/Switch guidance. Do not issue model commands solely for UI QA. These checks remain not_run until observed; no upstream recovery or income benefit is implied.
