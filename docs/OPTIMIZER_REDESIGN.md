# Optimizer redesign: learn every model, switch with confidence

Status: Phase 1 shipped in personal builds on September 25, 2026. 1.36.26 (pass A): protect level, learning time, learning boost, least-recently-measured selection, evidence table, report-only target. 1.36.27 (pass B): trials judged against the incumbent's demand-matched pay and on pay after first work (ramp recorded as rampSeconds), 35-second reading continuity (fewer 'waiting for fresh earnings' stalls), stuck-return escape for every trial type, and a learning-run demand floor (10+ requests, ~0.3 per warm provider). Phase 2 (pressure-curve estimator) is running in shadow from 1.36.28: see [Phase 2 in shadow](#phase-2-in-shadow).

## The goal

Keep the best model running, and keep enough fresh evidence on the others that when the current model fades, Bloomkeeper already knows where to go. Today gemma-4-26b earns 92% of this Mac's inference credits over the last 7 days. That is fine while it lasts, but Bloomkeeper has almost no usable evidence on anything else.

## How it works today, in plain terms

**Earnings target ($0.12/h default).** Not a target. It is a trigger band:
- If the paid pace stays below 75% of it ($0.09) for about 30 covered minutes, Bloomkeeper may start a trial of another model.
- Between $0.09 and $0.20 it only protects the current model from those shortfall trials.
- It does not rank models, forecast earnings, or block paid upgrades or spike trials.

**High-earnings hold ($0.20/h, hard-coded).** While the last three 5-minute windows each paid at least $0.20/h, no exploratory trial of any kind runs. Confident paid upgrades still can. Not configurable.

**What Bloomkeeper learns from.** Only this Mac's own paid, warm minutes for each model, matched to past minutes with similar network demand for that model (each of pressure, warm providers, active and load within 0.5× to 2× of now). A model's estimate counts only after 4 matched hours across 8 half-hour periods on 3 dates with 200 paid jobs, within 7 days.

**How trials work.**
- Ordinary and learning trials start only when the current model is idle for 20 minutes or earning below $0.09; a spike trial needs 3× normal demand.
- Trials last 20 warm minutes (learning trials can stop at 10), within a shared 120-minute daily budget and a 6-hour retry hold.
- The trial is judged against the current model's pace at the moment it was interrupted, which is usually near zero, so almost any payment counts as a win.
- A trial adds warm minutes to history, but at most one usable half-hour period, and none if it straddles :00 or :30.

**Consequences.**
- No model other than the incumbent ever reaches a usable estimate, so confident switches are effectively impossible.
- The data that is collected is biased toward the worst moments.
- The "Very aggressive" style cannot loosen the tightest rules, which are hard-coded (the 0.5 load-per-provider floor, the 120-minute budget, the 6-hour retry, the 10-minute idle scan, 2 learning and 3 spike trials a day, the $0.20 hold).
- Over the last 7 days the optimizer spent 46 hours watching with nothing qualifying and 17 hours waiting for fresh earnings evidence. That second state alternates with "waiting for fresh local readings" every minute.

## The redesign

### 1. Four controls that mean what they say

| Control | Replaces | What it does |
|---|---|---|
| **Protect earnings above** ($/h, default $0.20) | The hidden $0.20 hold, and the target's trigger role | While the current model pays at least this, Bloomkeeper never interrupts it to learn. |
| **Learning time** (Off, 30 min, 1 h, 3 h a day, or custom; optionally "for the next N days") | The fixed 120-minute budget, and the 24 h / 3 d / 7 d data-gathering presets | How much time per day Bloomkeeper may spend measuring other models, and only while the current model pays less than the protect level. |
| **Switching style** (existing slider) | — | How sure and how much better a model must be before a confident switch. |
| **Models Bloomkeeper can use** (existing) | — | Unchanged. |

The earnings target leaves the optimizer. It stays on Earnings → Target as a personal goal for the hours-met report only.

Cost of learning, for scale: learning only runs below the protect level, so an hour a day costs at most about $0.20, and usually far less because the measured model also earns.

### 2. Measure better, not only more

- **Measurement runs, not 20-minute trials.**
  - Default 45 minutes.
  - The first warm minutes after a switch are the routing ramp, before the network starts sending this Mac work. Record them separately to learn each model's ramp time, and leave them out of the steady-state rate.
- **Spread samples across the day.**
  - Keep a coverage grid per model (time of day × network demand level).
  - Aim each run at the model and conditions where the answer is most uncertain and could matter most: high possible upside, few samples in similar conditions.
  - Busy network hours are not the same as hours when your current model earns above the protect level, so most busy hours stay available for measuring.
- **Learn how pay responds to demand.**
  - Replace exact demand matching with a per-model curve: this Mac's $/warm-hour against that model's load per warm provider.
  - Count this Mac as one more warm provider when predicting a model it is not serving.
  - A run at moderate demand then informs a prediction at high demand, with a wider range when extrapolating. This is the answer to "data gathered when it's quiet is bad data": the model learns the slope, and measurement runs are steered toward the demand levels it has not seen.
- **Start with a reasonable guess for every model.**
  - Build a prior from what Bloomkeeper already fetches but never uses: per-token prices, this Mac's tokens per second for each model, and each model's share of network demand.
  - Measurements then correct the prior.
  - A never-served model gets a ranked, uncertain estimate instead of no estimate.
- **Judge a run fairly.**
  - Compare what the measured model earned with what the current model was expected to earn over the same minutes, at that time's demand.
  - Stop comparing against the frozen moment it was interrupted.

### 3. One decision rule for both switching and learning

Every few minutes, each model gets a predicted $/h for right now, with a range.

- **Confident switch:** the candidate's cautious estimate beats the current model's likely rate by the style's margin, after the cost of loading.
- **Learning run:** allowed when the current model pays below the protect level and learning time remains. Choose the model whose estimate could most plausibly beat the current model and is least certain.

This removes the special cases: the hard-coded gemma return, GPT-OSS as permanent last resort, alphabetical tie-breaks, and "any tiny payment counts as productive".

### 4. Show what Bloomkeeper knows

A table on the optimizer page, with a row per model:
- predicted $/h now, with its range
- hours measured
- how many of those were busy versus quiet
- when it was last measured
- whether it is ready to be switched to confidently

This is how you can see whether Bloomkeeper has the data to switch reliably.

### 5. Later: learn from other Macs (opt-in)

Share each model's demand-to-pay curve by chip and memory size across Bloomkeeper users who opt in, and use it as the starting prior. This is the biggest possible data multiplier, but it needs a server-side collection step and a privacy review. It is the existing backlog item "seed optimizer decisions on other Macs".

## Plan

**Phase 1: controls and unblocking** (uses today's estimator)
- Add the Protect level and Learning time; remove the target from decisions; fold data gathering into Learning time.
- Learning runs no longer need idle or shortfall, only a pace below the protect level and remaining budget.
- 45-minute runs with the ramp excluded, judged against the incumbent's expected rate.
- Fix the freshness flapping, the stuck ordinary-trial return, "tiny payment = productive", and the tie-breaks.
- Add the evidence table.

**Phase 2: new estimator, in shadow first**
- Demand-to-pay curves plus priors.
- Log a prediction for every model every 5 minutes and score it whenever that model actually runs. The forecast journal already does this for one-hour forecasts.
- Switch decisions move to the new estimator only after it beats the current one on the replay fixtures and on a week of live scoring.

**Phase 3: smarter learning and sharing**
- Uncertainty-aware scheduling across the coverage grid.
- Opt-in shared priors.

## Migration

- Existing plans keep their model lists and style.
- The protect level starts at $0.20, today's hold.
- Learning time starts at 1 hour a day. Macs currently in data gathering map to 3 hours a day until it ends.
- The earnings target keeps its value for the report.

## Phase 2 in shadow

Shipped in personal build 1.36.28 (September 25, 2026). Switching is unchanged.

**Estimator** (`native/demand_curves.py`, version `pressure-curve-v1`):
- Each model's pay curve is `level x (pressure + 0.01) ^ exponent`. Pressure is the model's active+queued requests per warm provider, capped at 2.
- Fitted to half-hour periods of steady paid warm time over 30 days, with a 7-day half-life.
  - Steady means the routing ramp is excluded: a stretch's minutes before its first paid minute, up to 15.
  - A period needs at least 10 minutes.
- The exponent is shared across models, then pulled toward each model's own fit by how widely its measured pressures range. The level is least squares in dollars, pulled toward the pooled level by 0.5 warm hours.
- A model not currently served is predicted at `load / (warm + 1)`, because this Mac would be one more warm provider.
- Part of the last half hour's gap from the curve carries forward (weight 0.7).
- The range comes from the model's own ratio quartiles once it has 8 periods (the pooled ones before that). It always spans at least 0.85x–1.15x, and widens when extrapolating or for a never-run model.
- Published prices and this Mac's tokens per second are not used. Predicting each model from the other models' data, adding them raised the error from $0.086/h to $0.12–0.17/h.

**Journal** (`native/demand_curve_journal.py`, table `demand_curve_observations`):
- Every 5 minutes, from the collector's opportunity loop, it freezes a packet for every model: the curve prediction plus the matched estimator's rate for the same moment, computed exactly as `DemandOptimizer.evaluate` does.
- Half-hour checkpoints are scored against the next 30 minutes of steady pay for whichever models ran at least 20 steady minutes.
- It keeps 45 days of half-hour checkpoints and 1 day of 5-minute ones. A record takes about 1.7 s on the live history.
- API: `GET /api/optimizer/shadow-estimator` returns `{latest, evaluation}`. The evaluation splits results into serving and other, and pairs the two estimators' errors on the same windows.

**Backtest before shipping.** Walk-forward over September 9–25, 591 half-hour windows. Errors are MAE in $ per warm hour:

| Windows | Pressure curve | Matched estimator | Flat 7-day mean |
|---|---|---|---|
| All | 0.0338 | 0.0364 | 0.0469 |
| Model already running (562) | 0.0334 | 0.0363 | 0.0473 |
| Model just switched to (29) | 0.0430 | 0.0373 | 0.0387 |

- The curve covers every model, but it is still weaker on the case that matters most for switching, a model just switched to. That is based on only 29 windows, mostly returns to gemma after trials and nemotron spike trials.
- Linear-space fitting was essential. A first log-space version overpredicted after the mean correction, because unpaid half-hours dominated the spread.

**Gate.** The evaluation reports `ready` after 7 scored days and 100 paired windows. The curve may drive switching only if it then beats the matched estimator on paired live windows, especially the "other" (not-serving) group, and on replay.

**Performance finding.** `OptimizerStore.evidence` over 30 days takes about 13 s on the live history, most of it the correlated coverage `EXISTS`. `DemandOptimizer.evidence` calls it with a 60 s cache, and `model_insights.warm_evidence` returns identical solo minutes in 0.2 s (checked per model on a September 25 snapshot). Worth moving the optimizer's reads to the fast path, keeping combination handling.

## Stall recovery (1.36.29)

**Trigger.** Steady work (at least 5 jobs a minute, with jobs in 12 of the 20 minutes before the silence) followed by 5 minutes of silence.
- Calibrated on September 8–25 silences after steady work, with no switch in the gap.
  - Below 5 jobs a minute, even 15-minute gaps ended on their own.
  - At 10+ jobs a minute, a 5-minute gap lasted 10+ more minutes 31% of the time, and an 8-minute gap 71%.
- Silences with network demand still holding happened most days. Sep 23 alone had silences of 60, 30, 55 and 119 minutes.

**Ladder** (`native/stall_recovery.py` decides, `native/stall_control.py` acts):
1. **Test request at 5 minutes.**
   - With an API key stored in the Keychain: a tiny request routed back to this Mac through Darkbloom (`X-Darkbloom-Route: self`). The key goes in with `security add-generic-password -U -a bloom -s bloom-darkbloom-api-key -w`.
   - Without a key: a one-token request to the local engine.
   - Providers on the Darkbloom Slack report the self-route version "sometimes" restarts routing. A `model_not_loaded` reply means the coordinator's record of this Mac is out of date.
2. **Same-model restart at 8 minutes.** It uses the guarded switch path with no purge (demand kind `stall-restart`) and a `recovery` run that counts toward the daily switch and downtime budgets. Limits: 3 a day, an hour apart.
3. **Escape.** 5 minutes after the restarted session starts, `decide(stall_escape=True)` opens the idle escape at once and compares against zero current pay, not the lagged averages.
4. **Hold.** Bloomkeeper stops and sends a push notice 5 minutes after the escape led to a switch, or after 20 minutes if no switch followed (the optimizer confirms an opportunity for 5 minutes before switching; with a 5-minute hold, the escape closed just as a move could qualify, as on Sep 26).

**Behavior.**
- If the model's own network demand fell below half its level before the silence, the ladder skips straight to step 3.
- Every step is an `opt_events` row (`stall-probe`, `stall-nudge`, `stall-restart`, `stall-escape`, `stall-hold`), shown in the activity log.
- Demand mode only. The ladder stands aside while a trial is running, a switch is pending, or the optimizer switched on its own after the silence began.
- Replay of the September 25 stall (verification problems after Darkbloom's v0.9.9 update): nudge at 10:03, restart at 10:06, another model at 10:12, alert at 10:18. In reality there was one switch at 10:37 and no alert.

## Phase 3: shared starting curves (1.36.37)

Shipped in personal build 1.36.37 (September 25, 2026). Still shadow only; switching is unchanged.

- **Bundled curves.** `native/shared-priors.json` (loaded by `native/shared_priors.py`) holds a per-model curve (`level`, `exponent`, `range`, hours, periods, Mac count) built by `native/build_priors.py` from Andrew's history (M5 Pro; 7 models with 2+ steady hours on Sep 25). Rebuild it before each release.
- **How they're used** (`demand_curves.build(..., shared)`): a model with a shared curve is pulled toward it instead of the pooled curve (level worth 2 warm hours, `SHARED_HOURS`; exponent via the usual `PRIOR_SLOPE`). A model never run here gets basis `shared` and a range widened 0.7x/1.3x. A Mac with no paid history at all still gets predictions (previously none).
- **Backtest** (`backtest_curves.py --priors ... --new-mac-since ...`). Simulated new Mac from Sep 23 with priors built from Sep 8–22: MAE $0.0448 → $0.0437 per warm hour over 96 windows, range coverage 36% → 41%; per model Qwen3.8 $0.065 → $0.037, gpt-oss $0.0083 → $0.0065. Mature history (Sep 17–25, 296 windows): unchanged at $0.0330. Same hardware and a small sample, so directional only. Other chips will differ; no hardware scaling yet.
- **Opt-in pay summaries** (`native/pay_sharing.py`, More → About Bloomkeeper → Improve starting estimates, off by default). Weekly POST to `bloomformac.com/api/pay-summaries/v1` of `demand_curves.summary` (curve numbers, hours, periods per model) plus chip family, memory band, version and a random ID/secret; turning it off DELETEs first. Site table `pay_summaries` (45-day retention, 5,000 cap). `/owner` → Shared pay summaries shows coverage and a Download JSON button; merge with `build_priors.py --summaries pay-summaries.json` (warm-hour-weighted).

### Hardware classes (1.36.40)

- Every curve in `shared-priors.json` lists the hardware (`chip family|memory band`) it came from; `byHardware` holds per-class merges. `build_priors.py --summaries` groups opt-in summaries by their chip family and memory band.
- The journal asks the collector for this Mac's class (`Collector.hardware_class`). A Mac gets its own class's curve when one exists (`sameHardware`), otherwise the all-hardware curve flagged `sameHardware: False`, whose never-run range widens to 0.55x/1.6x (same hardware: 0.7x/1.3x).
- Pay is not scaled across chips: with one Mac's data there is no evidence for a factor (and routing, not speed, dominates at low pressure). Revisit once summaries from other chips arrive: compare per-class levels for the same model.
