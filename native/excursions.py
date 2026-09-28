"""Evidence-armed excursions: the manager leaves its home model only on clear public evidence.

Andrew's decision (Sep 27 2026): keep automatic switching, but as a manager. It holds
the home model and moves only when public network evidence (per-cell request rates
from /v1/stats, network_evidence.py) shows a clearly better model for Macs of this
hardware class. No blind trials and no spike chasing: excursions last hours, not
minutes, and a ledger switches them off when they lose money.

Pieces
  ExcursionData.context  (I/O, cached per minute) numbers for the pure rules: each
      catalog model's $/h relative to home in this cell over 2 h and over the latest
      hour, this Mac's own realized home $/h, realized pay on an active excursion,
      excursions started in 24 h, the power/thermal/freshness gate and the ledger.
  evaluate / next_arming / propose / end_check  (pure) the rules below. The caller
      (manager.ManagerControl.remember) persists state['manager']['arming'].
  Ledger + book + kill  one row per finished excursion and the 14-day kill switch.

Arming (every default is a module constant). A model arms after CHECKS_NEEDED passing
checks at least CHECK_SECONDS apart (~2 h of persistence). A check passes when:
  - the 2 h ratio of its $/h to home's (same cell and level, both at list price x the
    network token mix, so this Mac's own $/job cannot skew it) has a 90% lower bound
    > MIN_RATIO_LOW, and the latest-hour ratio is still >= FADE_RATIO. The bound is a
    Student-t interval over providers plus each model's $/request error, calibrated
    against this Mac's own credits where it has jobs (network_evidence.py);
  - predicted gain = own home $/h x (ratio - 1) >= max(MIN_GAIN_SHARE x home $/h,
    round-trip switch cost / AMORTIZE_HOURS + TAU_USD_PER_HOUR), with the ratio capped at
    the model's network $/h over home's network $/h in the window of the own home rate
    (72 h), so a momentarily quiet home cannot inflate it;
  - home's network rate, both over 2 h and over that window, rests on >= MIN_BASE_REQUESTS
    requests with a 90% relative error < MAX_BASE_SPREAD;
  - both models have >= MIN_PROVIDERS (5) providers in this cell or a same-chip
    neighbour with at least as much memory (source != 'none'); with a gemma home only
    dedicated boxes count (gemma on mixed boxes gets ~0.46x, which would inflate it);
  - it is a solo catalog model the user selected, it fits memory (manager.admission),
    it is not blocked after a failed switch (24 h) and not cooling down after an
    early exit (24 h) or a faded/failed excursion (60 min);
  - a model with a lesson (below) has its ratio divided by the lesson's factor first.
Trial checks (Andrew, Sep 28: Macs that can hold large models try them when demand is higher
than usual and sustained, then learn per model). A model this Mac has not calibrated (< 50 own
jobs in 7 days) that fits memory may pass on its trial bound instead: the 90% low with the ln 2
$/request error (network_evidence.USD_ERROR_TRIAL) instead of ln 4, so a public ratio of about
2-2.5x instead of ~4x, when also
  - its network demand (active + queued requests in the public capacity feed) has had a median
    >= TRIAL_DEMAND_RATIO x its usual level for that hour (demand_alerts.usual_levels) in each
    of the last TRIAL_DEMAND_HOURS hours, so it held at both checks, and
  - no excursion to it started in the last TRIAL_EVERY_SECONDS (one trial a week per model).
Its own jobs then calibrate it (network_evidence.OWN_MIN_JOBS), and its ledger row may give it a
lesson. Every other rule is the same.
Lessons: a booked excursion that paid less per clock hour than home would have
(homeCounterfactualUsdPerHour), or that ended by early exit, divides that model's future
ratios by exp(penalty), penalty = ln(counterfactual / realized) up to LESSON_MAX (early exit:
LESSON_MAX), fading linearly to nothing over LESSON_SECONDS; losses add up.
Global gates: the managerExcursions setting, the ledger kill switch, a pinned home
(never), serving home, < MAX_PER_DAY excursions in 24 h (or the policy's lower
maxSwitchesPerDay, which dispatch enforces), DWELL_SECONDS since the last switch and
Optimizer.environment_reason (AC power, thermal, fresh readings).

Switch cost, round trip (PLAN 2.3 and 6.3, plus the return leg):
  C = y_m (t_m + R_m)/60 + y_home (t_home + R_home)/60 + 2 x 1.1 F_epoch
      + (p_m + p_home) D_fail (y_home + F_tier)/60
with y_m the predicted $/h, t the median minutes from a switch command to the first
paid job on that model on this Mac (opt_events + opt_credits, else 5 min), ramp R 10
min gemma, 5 gpt-oss, 25 other models, F from the base-reward floor for this Mac's
memory, p_fail 1% for gemma, gpt-oss and 9B, 25% for a model needing > 65% of RAM with
no ready time on this Mac, else 4.7%, and D_fail 9 min (auto-restore).

Ending (manager.excursion_end), on evidence: an early exit once there is an hour of ready
data after the per-model ramp (command -> first paid job + R, >= 10 min) and even the
90% upper bound of realized pay per ready hour is below the lower bound of what home
would have paid per ready hour (own trailing home $/h x the network's home change from
the same window, with its 90% interval; x/÷ 2 without comparable network figures), and a
24 h hold; the latest-hour ratio < FADE_RATIO (or no longer visible) after DWELL_SECONDS;
the setting turned off or the ledger's kill switch. MAX_MINUTES (24 h) is only a runaway cap. A target that is not
ready is the watchdog's (it restores home and settle() finishes the excursion); these
rules never fight it.

Ledger: realized pay per clock hour vs that home counterfactual per clock hour (home's
credits over its stints in the same window), less the return trip's unmeasured costs.
The kill switch turns excursions off when the one-sided 90% lower bound of the mean gain
over the last 14 days (>= KILL_MIN_COUNT excursions) is below zero, until the user
turns excursions off and on again (PLAN 6.6 and 6.9).
"""

import copy, json, logging, math, sqlite3, statistics, threading
from collections import defaultdict
from datetime import datetime
from model_combinations import members
from network_evidence import MIN_PROVIDERS, t90_one_sided

log = logging.getLogger('bloom.excursions')

# --- arming ---------------------------------------------------------------
CHECK_SECONDS = 3600  # hourly checks
CHECKS_NEEDED = 2  # consecutive passing checks before a move (~2 h persistence)
STALE_SECONDS = 2 * 3600 + 900  # a check older than this (sleep, a long gate) is void
EVIDENCE_HOURS = 2  # network window for the ratio and the prediction
# Both models of a ratio are priced alike: list price x the network's token mix. This
# Mac's own pay level enters once, through its realized home $/h (prediction = own home
# $/h x ratio). Own $/job for one model only would skew the ratio (a low own gemma $/job
# made every other model look better).
BASIS = 'list'
LATEST_HOURS = 1  # latest-hour window (fade)
MIN_RATIO_LOW = 1.0  # 90% lower bound of candidate/home $/h must exceed this
# The gain bar is one out-of-sample error of the 1-h gain (PLAN 6.4's definition of tau), sized on
# the public /v1/stats poll (checks Sep 27 08:34 - Sep 28 07:34 in 30 cells; see
# calibration-2026-09-28.md). The 2 h ratio's error on the next hour grows with home's rate, so it
# is mostly a share of home. Where a candidate looked better (1.1 <= ratio < 2, latest-hour ratio
# >= 1.1) the mean absolute error of the gain was 0.96 x home $/h with a gemma home (90% CI
# 0.80-1.16; n 194, 14 cells; the candidate paid no more than home in 51% of the next hours) and
# 0.69 x (0.58-0.77; n 1,744, 28 cells) with a gpt-oss home; 0.5 sat below every interval. In $/h
# the error was $0.011 at homes under $0.01/h, $0.020 at $0.01-0.02 and $0.031 at $0.02-0.04: a
# flat $0.04 was 2-4x it on the slower half of the fleet, so only a $0.01 floor stays absolute. On
# the poll replay neither change moved a gemma-home Mac (the 90% lower bound binds first).
MIN_GAIN_SHARE = 1.0  # predicted gain >= home $/h (ratio >= 2) ...
TAU_USD_PER_HOUR = 0.01  # ... and >= switch cost over AMORTIZE_HOURS + $0.01/h
AMORTIZE_HOURS = 2
HOME_TRAILING_SECONDS = 72 * 3600  # own home $/h: the last 72 h on home ...
HOME_TRAILING_MIN_SECONDS = 4 * 3600  # ... with >= 4 ready hours, else 14 days
# Home's network rate is read over the same window as the own home rate it scales.
BASE_HOURS = {'72h': HOME_TRAILING_SECONDS // 3600, '14d': 14 * 24}
MAX_BASE_SPREAD = 1.0  # a home network rate with a 90% relative error >= 100% is no base
MIN_BASE_REQUESTS = 30  # ... nor one resting on fewer requests
SPAN_GAP_SECONDS = 1800  # home's ready minutes this close form one stint (clock-hour rate)
PRIOR_SUCCESS_SECONDS = 600  # ready time on a model that counts as a prior success
# Excursion windows kept out of the home choice: all of manager.HOME_WINDOW (30 days), at
# MAX_PER_DAY a day.
HOME_WINDOW_SECONDS = 30 * 86400
MAX_WINDOWS = 90
# --- caps -----------------------------------------------------------------
MAX_PER_DAY = 3  # excursion moves started in any 24 h
DWELL_SECONDS = 1800  # minimum time on a model before leaving it (home or target)
COOLDOWN_SECONDS = 3600  # after a faded or failed excursion to the same model
EARLY_BLOCK_SECONDS = 86400  # after an early exit
# --- ending ---------------------------------------------------------------
EARLY_EXIT_SECONDS = 3600  # the earliest early exit (PLAN 6.6)
# Pay is judged only after the target's ramp: this Mac's command -> first paid job plus
# the ramp minutes below, never less than 10 min (niche models: >= 5 + 25 min).
RAMP_FLOOR_SECONDS = 600
PAY_BLOCK_MINUTES = 20  # realized pay's error: the spread of its 20-minute block rates
# Without comparable network figures home's level while away is unknown: x/÷ 2 (gemma's 2 h
# network rate in Andrew's cell went 942 -> 206 req/h in one day).
TRAILING_SPREAD = math.log(2)
SETTLEMENT_SECONDS = 120  # credits of the last 2 min may not be recorded yet
FADE_RATIO = 1.1
# A runaway cap only: excursions end on evidence (fade, early exit). Niche up-shifts on
# Andrew's Mac averaged 6.4-8.7 h (gap-1), so a 4 h clock split regimes into round trips.
MAX_MINUTES = 24 * 60
# --- switch cost (PLAN 2.3, 6.3) ------------------------------------------
# Command -> first paid job, per model, from this Mac's switches in the last 30 days (a
# model needs DEAD_MIN_SWITCHES of them); else 5 min, the fallback in code-switching-costs
# (measured medians: gemma 115 s, gpt-oss 212 s, qwen3.6 >= 732 s).
DEAD_FALLBACK_SECONDS = 300
DEAD_LOOKBACK_SECONDS = 30 * 86400
DEAD_MIN_SWITCHES = 3  # the smallest sample whose median is not an extreme value
DEAD_CACHE_SECONDS = 3600
RAMP_MINUTES = (('gemma', 10), ('gpt-oss', 5))  # earnings-minutes lost while warming
NICHE_RAMP_MINUTES = 25
RELIABLE = ('gemma', 'gpt-oss', '-9b')  # 1 of 117 failed
P_FAIL_RELIABLE, P_FAIL_LARGE, P_FAIL_DEFAULT = 0.01, 0.25, 0.047
LARGE_SHARE = 0.65  # of physical RAM, with no ready time on this Mac
FAIL_DARK_MINUTES = 9  # dark time of a failed load with auto-restore
EPOCHS_LOST = 1.1  # base-reward epochs lost per `darkbloom start`
FLOORS = ((24, 10), (32, 12), (48, 16), (64, 18), (96, 22), (128, 26), (192, 30), (512, 40))
EPOCHS_PER_MONTH = 30 * 288
# --- ledger ---------------------------------------------------------------
LEDGER_DAYS = 14
KILL_MIN_COUNT = 3  # ended excursions before the kill switch may judge
BOOK_DELAY_SECONDS = 300  # book an excursion once its credits have settled
# End codes with no return trip home to charge to the excursion.
NO_RETURN = frozenset({'manual', 'external', 'replaced'})
MAX_ROWS = 10
CACHE_SECONDS = 60
# --- trials (models this Mac has not calibrated) ---------------------------------
# TRIAL_DEMAND_RATIO from Andrew's opt_network (the public capacity feed every 30 s, Sep 6-28,
# 6 niche models, 2,488 model-hours): at the first hour when two consecutive hourly medians reach
# k x usual, the next 3 hours (a niche switch and ramp plus the 2 h the gain bar amortizes over)
# stayed above usual 61% of the time at k = 1.0, 75% at 1.25, 77% at 1.5, 80% at 2 and 85% at 2.5
# (51% after any hour). Past 1.25-1.5 the gain is slow while episodes thin out (110 in 22 days at
# 1.5, 76 at 2). The Sep 9-10 qwen3.5-35b regime (this Mac's own qwen pay $0.14-0.38/h against
# gemma's ~$0.10) held 2.3-17x usual for 13 hours. On the public poll (Sep 27-28) demand >= 1.5x
# did not make the public pay ratio itself more persistent: the gate keeps trials to unusual,
# sustained demand; the pay bar and the ledger judge them.
TRIAL_DEMAND_RATIO = 1.5
TRIAL_DEMAND_HOURS = 2  # hourly medians, the last two hours: both checks
TRIAL_DEMAND_SAMPLES = 60  # 30-second capacity samples per hour (half coverage) for a reading
TRIAL_EVERY_SECONDS = 7 * 86400  # one trial per model a week (OWN_DAYS: own jobs calibrate it after)
DEMAND_CACHE_SECONDS = 300  # as demand_alerts refreshes its usual levels
# --- lessons (per model) ------------------------------------------------------------
# One ledger row's noise sets the most a single excursion can teach: a null excursion (gemma
# against its own trailing 72 h rate on Andrew's Mac, Sep 6-28, ready-hour windows) read
# |ln(realized / counterfactual)| >= 0.73 one time in ten over 4 h (n 55; 1.1 over 2 h, n 116) and
# read as a loss 50-67% of the time, so one row is worth up to about x2 (ln 2, the ledger's own
# TRAILING_SPREAD) and a small shortfall only moves the bar a little. For scale: 57 of Andrew's 58
# legacy trips away from gemma (>= 30 ready min, Sep 8-26) paid less than gemma's trailing rate.
LESSON_SECONDS = LEDGER_DAYS * 86400
LESSON_MAX = math.log(2)
LESSON_SKIP = NO_RETURN | {'off', 'changed'}  # the user, an outside change or a failed load ended it

# Gates that clear arming; the others (daily limit, dwell, environment) only pause it.
RESET_GATES = frozenset({'off', 'paused', 'no home', 'pinned', 'away'})
HOLDS = {'early-exit': EARLY_BLOCK_SECONDS, 'faded': COOLDOWN_SECONDS, 'changed': COOLDOWN_SECONDS}


def finite(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def rounded(v, digits=4):
    return round(v, digits) if finite(v) else None


def gemma_family(model):
    return isinstance(model, str) and 'gemma' in model.lower()


# --- pure economics ---------------------------------------------------------


def floor_usd_per_month(memory_gb):
    """Base-reward floor for this Mac's memory tier (PLAN 2.2)."""
    value = FLOORS[2][1] if not finite(memory_gb) else FLOORS[0][1]
    for gb, usd in FLOORS:
        if finite(memory_gb) and memory_gb + 0.5 >= gb:
            value = usd
    return value


def ramp_minutes(model):
    name = model.lower()
    return next((m for key, m in RAMP_MINUTES if key in name), NICHE_RAMP_MINUTES)


def dead_seconds(model, dead):
    """This Mac's median seconds from a switch command to the first paid job on `model`."""
    value = (dead or {}).get(model)
    return value if finite(value) and value >= 0 else DEAD_FALLBACK_SECONDS


def ramp_seconds(model, dead):
    """Seconds after the switch command before pay on `model` is judged."""
    return max(RAMP_FLOOR_SECONDS, dead_seconds(model, dead) + 60 * ramp_minutes(model))


def failure_chance(model, required_gb, total_gb, prior_success):
    name = model.lower()
    if any(key in name for key in RELIABLE):
        return P_FAIL_RELIABLE
    if (
        finite(required_gb)
        and finite(total_gb)
        and total_gb > 0
        and required_gb > LARGE_SHARE * total_gb
        and not prior_success
    ):
        return P_FAIL_LARGE
    return P_FAIL_DEFAULT


def switch_cost(model, predicted, home_rate, p_fail, memory_gb, home, dead=None, p_home=None):
    """Round-trip cost in $ of an excursion to `model` and back to `home` (PLAN 6.3 plus the
    return leg): each leg loses its target's $/h over this Mac's command -> first paid job
    and the target's ramp, EPOCHS_LOST base periods, and a failure's dark minutes."""
    floor = floor_usd_per_month(memory_gb)
    p_home = P_FAIL_RELIABLE if p_home is None else p_home
    dark = FAIL_DARK_MINUTES / 60 * (home_rate + floor / 720)
    return (
        predicted * (dead_seconds(model, dead) / 60 + ramp_minutes(model)) / 60
        + home_rate * (dead_seconds(home, dead) / 60 + ramp_minutes(home)) / 60
        + 2 * EPOCHS_LOST * floor / EPOCHS_PER_MONTH
        + (p_fail + p_home) * dark
    )


def required_gain(home_rate, cost):
    return max(MIN_GAIN_SHARE * home_rate, cost / AMORTIZE_HOURS + TAU_USD_PER_HOUR)


def usable(point):
    """A network $/h point ({'usdPerHour', 'spread', 'requests'?}) firm enough to scale by."""
    p = point if isinstance(point, dict) else {}
    requests = p.get('requests')
    return bool(
        finite(p.get('usdPerHour'))
        and p['usdPerHour'] > 0
        and finite(p.get('spread'))
        and 0 <= p['spread'] < MAX_BASE_SPREAD
        and (requests is None or (finite(requests) and requests >= MIN_BASE_REQUESTS))
    )


def counterfactual(trailing, before, during):
    """What home would have paid per hour: this Mac's trailing home $/h x the network's home
    $/h change from `before` (home's network rate over the window of the trailing rate) to
    `during`, with a 90% interval from both windows' sampling error ({'usdPerHour', 'spread',
    'source', 'dedicated'}). No clamp: Saturday night to Sunday morning moved dedicated gemma
    5.3x in Andrew's cell (gap-2). Without comparable, usable network figures it is the
    trailing rate, x/÷ exp(TRAILING_SPREAD)."""
    if not finite(trailing):
        return None
    b, d = before or {}, during or {}
    level = (b.get('source'), b.get('dedicated'))
    if not (
        usable(b)
        and usable(d)
        and (level[0] is None or level == (d.get('source'), d.get('dedicated')))
    ):
        return {
            'usdPerHour': trailing,
            'low': trailing * math.exp(-TRAILING_SPREAD),
            'high': trailing * math.exp(TRAILING_SPREAD),
            'basis': 'trailing',
        }
    value = trailing * d['usdPerHour'] / b['usdPerHour']
    spread = math.hypot(
        *(e['spread'] if finite(e.get('spread')) and e['spread'] >= 0 else 0.0 for e in (b, d))
    )
    return {
        'usdPerHour': value,
        'low': value * math.exp(-spread),
        'high': value * math.exp(spread),
        'basis': 'network-scaled',
    }


def network_point(e):
    """The fields counterfactual() needs from a network_evidence estimate."""
    e = e if isinstance(e, dict) else {}
    return {
        'usdPerHour': e.get('usd_per_h'),
        'spread': e.get('req_spread'),
        'source': e.get('source'),
        'dedicated': e.get('dedicated_only'),
        'requests': e.get('requests'),
    }


def predicted_ratio(c, network):
    """The candidate's ratio to home, capped at its network $/h over home's network $/h in
    the window of the own home rate (same level): a quiet 2 h at home inflates the ratio."""
    ratio = c.get('ratio')
    usd, base = c.get('usdPerHour'), (network or {}).get('usdPerHour')
    if (
        finite(ratio)
        and finite(usd)
        and finite(base)
        and base > 0
        and (c.get('source'), c.get('dedicated')) == (network.get('source'), network.get('dedicated'))
    ):
        return min(ratio, usd / base)
    return ratio


def daily_limit(rules):
    """Excursions per 24 h: MAX_PER_DAY, or the policy's lower maxSwitchesPerDay (dispatch
    enforces that one: demand_optimizer.excursion_limit)."""
    v = (rules if isinstance(rules, dict) else {}).get('maxSwitchesPerDay')
    return min(MAX_PER_DAY, v) if finite(v) else MAX_PER_DAY


def realized_high(minutes, realized):
    """One-sided 90% upper bound of realized $/ready-hour from its PAY_BLOCK_MINUTES block
    rates (Student-t), or None with fewer than two blocks."""
    k = len(minutes) // PAY_BLOCK_MINUTES
    if k < 2 or not finite(realized):
        return None
    rates = []
    for i in range(k):
        block = minutes[i * PAY_BLOCK_MINUTES : (i + 1) * PAY_BLOCK_MINUTES]
        seconds = sum(x['seconds'] for x in block)
        rates.append(sum(x['usd'] for x in block) * 3600 / seconds if seconds else 0.0)
    return realized + t90_one_sided(k - 1) * statistics.stdev(rates) / math.sqrt(k)


def ledger_bound(gains):
    """(mean, one-sided 90% lower bound) of the gain per excursion: the bootstrap SD of the
    mean over these excursions (SD / sqrt(n), closed form) times the one-sided 90% Student-t
    quantile (df = n - 1, as the home choice), so a handful of excursions is not over-trusted."""
    n = len(gains)
    mean = sum(gains) / n
    if n < 2:
        return mean, -math.inf
    return mean, mean - t90_one_sided(n - 1) * statistics.stdev(gains) / math.sqrt(n)


def lesson_penalty(r):
    """Log-units penalty from one ledger row: ln(counterfactual / realized) when it paid less
    per clock hour than home would have, up to LESSON_MAX; LESSON_MAX after an early exit; 0
    for rows the user, an outside change or a failed load ended."""
    code = r.get('endCode')
    if code in LESSON_SKIP:
        return 0.0
    if code == 'early-exit':
        return LESSON_MAX
    real, cf = r.get('realizedUsdPerHour'), r.get('homeCounterfactualUsdPerHour')
    if not (finite(real) and finite(cf) and cf > 0 and real < cf):
        return 0.0
    return min(LESSON_MAX, math.log(cf / real)) if real > 0 else LESSON_MAX


def lessons(records, now):
    """model -> {'factor', 'penalty', 'until', 'triedAt', 'endedAt', 'home', 'code',
    'realizedUsdPerHour', 'homeUsdPerHour'} from ledger rows (pure): each losing row's penalty
    fades linearly to 0 over LESSON_SECONDS after it ended, several add up, and the model's
    ratios are divided by factor = exp(penalty). The latest losing row describes it."""
    out = {}
    for r in records or []:
        model, end = r.get('model'), r.get('endedAt')
        if not isinstance(model, str) or not finite(end) or not 0 <= now - end < LESSON_SECONDS:
            continue
        p = lesson_penalty(r)
        if p <= 0:
            continue
        item = out.setdefault(model, {'model': model, 'penalty': 0.0, 'endedAt': -math.inf})
        item['penalty'] += p * (1 - (now - end) / LESSON_SECONDS)
        if end >= item['endedAt']:
            item.update(
                triedAt=r.get('startedAt'),
                endedAt=end,
                until=end + LESSON_SECONDS,
                home=r.get('from'),
                code=r.get('endCode'),
                realizedUsdPerHour=r.get('realizedUsdPerHour'),
                homeUsdPerHour=r.get('homeCounterfactualUsdPerHour'),
            )
    for item in out.values():
        item['factor'] = math.exp(item['penalty'])
    return out


def day(at):
    t = datetime.fromtimestamp(at)
    return '%s %d' % (t.strftime('%b'), t.day)


def lesson_text(item):
    """"qwen3.5-35b-a3b: tried Sep 28, paid less than gemma-4-26b-qat-4bit; needs stronger
    evidence until Oct 12." (the UI builds the same sentence with display names)."""
    tried = item.get('triedAt') if finite(item.get('triedAt')) else item.get('endedAt')
    return '%s: tried %s, %s %s; needs stronger evidence until %s.' % (
        item['model'],
        day(tried),
        'ended early, paying less than' if item.get('code') == 'early-exit' else 'paid less than',
        item.get('home') or 'the home model',
        day(item['until']),
    )


def demand_high(reading):
    """The trial demand gate: every one of the last TRIAL_DEMAND_HOURS hours had a median load
    >= TRIAL_DEMAND_RATIO x usual (and at least demand_alerts' minimum load)."""
    from demand_alerts import SPIKE_MIN_LOAD

    hours = (reading or {}).get('hours') or []
    return len(hours) == TRIAL_DEMAND_HOURS and all(
        finite(h.get('ratio'))
        and h['ratio'] >= TRIAL_DEMAND_RATIO
        and finite(h.get('load'))
        and h['load'] >= SPIKE_MIN_LOAD
        for h in hours
    )


# --- pure state helpers ---------------------------------------------------


def paused(m, now):
    """The ledger's kill switch. It never lifts by itself: only turning excursions off and
    on again does (policy_saved). `now` is unused."""
    off = (m or {}).get('excursionsOff')
    return off if isinstance(off, dict) else None


def holds(m, now):
    """model -> {'until', 'code'} for models cooling down after an excursion ended badly."""
    return {
        k: v
        for k, v in ((m or {}).get('excursionHolds') or {}).items()
        if isinstance(v, dict) and finite(v.get('until')) and v['until'] > now
    }


def enabled(rules):
    return (rules if isinstance(rules, dict) else {}).get('managerExcursions', 1) == 1


def finished(m, excursion, record, now):
    """Bookkeeping when an excursion ends (caller holds the optimizer lock): hold the model
    after an early exit or a faded/failed run, and queue the record for the ledger."""
    model, code = record.get('target'), record.get('endCode')
    kept = holds(m, now)
    if model and code in HOLDS:
        kept[model] = {'until': now + HOLDS[code], 'code': code}
    if kept:
        m['excursionHolds'] = kept
    else:
        m.pop('excursionHolds', None)
    evidence = excursion.get('evidence') if isinstance(excursion.get('evidence'), dict) else {}
    entry = {
        k: record.get(k)
        for k in (
            'target',
            'from',
            'startedAt',
            'leftAt',
            'endedAt',
            'predictedUsdPerHour',
            'endCode',
            'endReason',
        )
    }
    entry.update(
        {
            k: evidence.get(k)
            for k in (
                'homeUsdPerHour',
                'homeWallUsdPerHour',
                'homeNetworkUsdPerHour',
                'homeNetworkSpread',
                'homeNetworkSource',
                'homeNetworkDedicated',
                'homeNetworkRequests',
                'memoryGB',
            )
        }
    )
    if finite(entry.get('startedAt')) and finite(entry.get('endedAt')):
        m['unbooked'] = (m.get('unbooked') or [])[-9:] + [entry]
        start = entry['leftAt'] if finite(entry.get('leftAt')) else entry['startedAt']
        kept = [w for w in windows(m, now) if finite(w[2]) and w[2] > now - HOME_WINDOW_SECONDS]
        m['excursionWindows'] = kept[-(MAX_WINDOWS - 1) :] + [[model, start, entry['endedAt']]]


def windows(m, now):
    """[model, start, end] of recent excursions, the active one open-ended. Pay earned on an
    excursion reflects a passing regime, so it never counts toward choosing the home."""
    out = [
        list(w)
        for w in (m or {}).get('excursionWindows') or []
        if isinstance(w, (list, tuple)) and len(w) == 3 and finite(w[1]) and finite(w[2])
    ]
    active = (m or {}).get('excursion') or {}
    start = active.get('leftAt') if finite(active.get('leftAt')) else active.get('startedAt')
    if active.get('target') and finite(start):
        out.append([active['target'], start, math.inf])
    return out


def policy_saved(m, previous, rules, now):
    """The user turned excursions off and on again: lift the ledger's pause, start afresh."""
    if enabled(rules) and not enabled(previous) and m.get('excursionsOff'):
        m.pop('excursionsOff')
        m['excursionLedgerSince'] = now
        return True
    return False


# --- pure rules ---------------------------------------------------------------


def evaluate(state, context, now):
    """Per-model eligibility for an excursion from this context, or None without evidence data.

    Returns {'gate', 'environment', 'rows', 'byModel', 'passing'}: `passing` lists the
    models that pass every per-model check (best gain first) whatever the global gate;
    a row is `eligible` only when it passes and no gate applies."""
    x = context.get('excursions') if isinstance(context, dict) else None
    if not isinstance(x, dict):
        return None
    from manager import admission, pinned  # manager imports this module

    m = state.get('manager') or {}
    rules = context.get('rules') or {}
    home = context.get('home') or {}
    name = home.get('model')
    rate = x.get('homeUsdPerHour') if finite(x.get('homeUsdPerHour')) else None
    rows = context.get('rows') or {}
    chosen = any(r.get('selected') for r in rows.values())
    blocked = context.get('blocked') or {}
    cooling = holds(m, now)
    total = x.get('memoryGB')
    dead = x.get('deadSeconds') or {}
    p_home = failure_chance(name, None, total, True) if name else P_FAIL_RELIABLE
    network = x.get('homeNetwork') or {}
    taught = x.get('lessons') or {}
    demand = x.get('demand') or {}
    # One trial a week per model: any excursion to it that started in the last week counts.
    tried = {w[0] for w in windows(m, now) if w[1] > now - TRIAL_EVERY_SECONDS}
    gate = None
    if not enabled(rules):
        gate = 'off'
    elif paused(m, now):
        gate = 'paused'
    elif not name:
        gate = 'no home'
    elif pinned(home):
        gate = 'pinned'
    elif context.get('current') != name:
        gate = 'away'
    elif (x.get('today') or 0) >= daily_limit(rules):
        gate = 'daily limit'
    elif finite(x.get('lastSwitchAt')) and now - x['lastSwitchAt'] < DWELL_SECONDS:
        gate = 'dwell'
    elif x.get('environment'):
        gate = 'environment'
    out, passing, predictions = [], [], {}
    for c in x.get('candidates') or []:
        model = c.get('model')
        if not isinstance(model, str) or model == name:
            continue
        row = rows.get(model) or {}
        ratio, low, high = c.get('ratio'), c.get('low'), c.get('high')
        latest = c.get('latestRatio')
        mult = predicted_ratio(c, network)
        trial_low = c.get('trialLow')
        lesson = taught.get(model) or {}
        factor = lesson['factor'] if finite(lesson.get('factor')) and lesson['factor'] > 1 else 1.0
        reading = demand.get(model) or {}
        r = {
            'model': model,
            'usdPerHour': rounded(rate * mult) if rate is not None and finite(mult) else None,
            'low': rounded(rate * low) if rate is not None and finite(low) else None,
            'high': rounded(rate * high) if rate is not None and finite(high) else None,
            'ratio': rounded(ratio, 3),
            'ratioLow': rounded(low, 3),
            'latestRatio': rounded(latest, 3),
            'providers': c.get('providers') or 0,
            'source': c.get('source') or 'none',
            'dedicated': c.get('dedicated'),
            'calibrated': c.get('calibrated'),
            'trialLow': rounded(trial_low, 3),
            'demandRatio': rounded(reading.get('ratio'), 2),
            'lessonFactor': rounded(factor, 3) if factor > 1 else None,
            'trial': False,
            'eligible': False,
            'why': None,
        }
        why = None
        if len(members(model)) != 1:
            why = 'gemma pair' if any(gemma_family(p) for p in members(model)) else 'pair'
        elif not row.get('available'):
            why = 'not available'
        elif chosen and not row.get('selected'):
            why = 'not selected'
        elif admission(row, {'memoryHeadroomGB': 1, **rules}):
            why = 'memory'
        elif blocked.get(model):
            why = 'blocked'
        elif model in cooling:
            why = 'cooling down'
        elif (
            r['source'] == 'none'
            or not finite(ratio)
            or not finite(low)
            or r['providers'] < MIN_PROVIDERS
            or c.get('homeUsable') is False  # home's side of the 2 h ratio is too thin
            or not usable(network)  # no base for home's network rate
        ):
            why = 'no evidence'
        elif gemma_family(name) and c.get('dedicated') is False:
            why = 'mixed boxes'
        elif rate is None or rate <= 0:
            why = 'no home rate'
        else:
            budget = row.get('loadBudget') or {}
            required = budget.get('requiredGB') if finite(budget.get('requiredGB')) else row.get('memoryGB')
            p = failure_chance(model, required, total, c.get('priorSuccess'))
            # It fits memory (admission above): an uncalibrated model may pass on its trial bound.
            trialable = c.get('calibrated') is False and finite(trial_low)

            def judge(f):
                """(why, trial, predicted, cost, need) with the model's ratios divided by f."""
                predicted = rate * mult / f
                cost = switch_cost(model, predicted, rate, p, total, name, dead, p_home)
                need = required_gain(rate, cost)

                def bar(bound):
                    if bound / f <= MIN_RATIO_LOW:
                        return 'weak evidence'
                    if not finite(latest) or latest < FADE_RATIO:
                        return 'fading'
                    return 'gain too small' if predicted - rate < need else None

                why, trial = bar(low), False
                if why == 'weak evidence' and trialable:
                    why = bar(trial_low)
                    if why is None:
                        if model in tried:
                            why = 'tried this week'
                        elif not demand_high(reading):
                            why = 'demand not high enough'
                        else:
                            trial = True
                return why, trial, predicted, cost, need

            why, trial, predicted, cost, need = judge(factor)
            if why and factor > 1 and judge(1.0)[0] is None:
                why = 'paid less before'  # a lesson from its ledger row holds it back
            predictions[model] = predicted
            r.update(
                gainUsdPerHour=rounded(predicted - rate),
                needUsdPerHour=rounded(need),
                costUsd=rounded(cost),
                pFail=p,
                trial=trial,
            )
            if why is None:
                passing.append((predicted - rate, model))
        r['why'] = why or gate
        r['eligible'] = r['why'] is None
        out.append(r)
    passing = [model for _, model in sorted(passing, key=lambda p: (-p[0], p[1]))]
    by_model = {r['model']: r for r in out}
    ranked = [by_model[k] for k in passing] + sorted(
        (r for r in out if r['model'] not in passing),
        key=lambda r: (-(r['ratio'] if finite(r['ratio']) else -1), r['model']),
    )
    head = []
    if name:
        head = [
            {
                'model': name,
                'usdPerHour': rounded(rate),
                'low': rounded(rate),
                'high': rounded(rate),
                'ratio': 1.0,
                'ratioLow': 1.0,
                'latestRatio': 1.0,
                'providers': network.get('providers') or 0,
                'source': network.get('source') or 'none',
                'dedicated': network.get('dedicated'),
                'eligible': False,
                'why': 'home',
            }
        ]
    return {
        'gate': gate,
        'environment': x.get('environment') if gate == 'environment' else None,
        'rows': (head + ranked)[:MAX_ROWS],
        'byModel': by_model,
        'passing': passing,
        'predicted': predictions,
    }


def next_arming(saved, evaluation, now):
    """Pure: the arming record after this decision. One check per CHECK_SECONDS; a move
    needs CHECKS_NEEDED consecutive passing checks by the same model."""
    if not evaluation or evaluation['gate'] in RESET_GATES:
        return None
    if not (
        isinstance(saved, dict)
        and isinstance(saved.get('model'), str)
        and finite(saved.get('lastCheckAt'))
        and 0 <= now - saved['lastCheckAt'] <= STALE_SECONDS
    ):
        saved = None
    if evaluation['gate']:
        return copy.deepcopy(saved)  # paused: neither advances nor resets
    if saved and now - saved['lastCheckAt'] < CHECK_SECONDS:
        return copy.deepcopy(saved)  # between checks
    passing = evaluation['passing']
    if saved and saved['model'] in passing:
        return {
            **copy.deepcopy(saved),
            'checks': min(CHECKS_NEEDED, int(saved.get('checks') or 0) + 1),
            'lastCheckAt': now,
        }
    if passing:
        return {'model': passing[0], 'since': now, 'checks': 1, 'lastCheckAt': now}
    return None


def armed(arming):
    return bool(arming and (arming.get('checks') or 0) >= CHECKS_NEEDED)


def propose(state, now, context):
    """Pure (manager.manager_excursion): a proposal for an armed, still eligible model."""
    evaluation = context.get('excursionEvaluation')
    if evaluation is None:
        evaluation = evaluate(state, context, now)
    arming = context.get('arming')
    if not evaluation or evaluation['gate'] or not armed(arming):
        return None
    r = evaluation['byModel'].get(arming['model'])
    if not r or not r['eligible']:
        return None
    x = context['excursions']
    home = context['home']['model']
    rate = x['homeUsdPerHour']
    predicted = (evaluation.get('predicted') or {}).get(r['model'])
    predicted = predicted if finite(predicted) else rate * r['ratio']
    cell = x.get('cell')
    where = ('%s, %s GB' % tuple(cell.split('|'))) if isinstance(cell, str) and '|' in cell else 'this hardware class'
    if r.get('trial'):
        reason = (
            'a trial of a model this Mac has not served lately: public data for Macs like this '
            'one (%s) shows it paying %.1fx %s (90%% low %.2fx at the trial margin) at %d hourly '
            'checks, with its demand at %.1fx its usual level for these hours; expected about '
            '$%.3f/h here vs $%.3f/h at home.'
            % (
                where,
                r['ratio'],
                home,
                r['trialLow'],
                arming['checks'],
                r['demandRatio'],
                predicted,
                rate,
            )
        )
    else:
        reason = (
            'public data for Macs like this one (%s) shows it paying %.1fx %s (90%% low %.2fx) '
            'at %d hourly checks; expected about $%.3f/h here vs $%.3f/h at home.'
            % (where, r['ratio'], home, r['ratioLow'], arming['checks'], predicted, rate)
        )
    network = x.get('homeNetwork') or {}
    return {
        'target': r['model'],
        'reason': reason,
        'predictedUsdPerHour': round(predicted, 5),
        'maxMinutes': MAX_MINUTES,
        'evidence': {
            'cell': cell,
            'ratio': r['ratio'],
            'ratioLow': r['ratioLow'],
            'latestRatio': r['latestRatio'],
            'providers': r['providers'],
            'source': r['source'],
            'dedicated': r['dedicated'],
            'homeUsdPerHour': rounded(rate, 5),
            'homeBasis': x.get('homeBasis'),
            'homeWallUsdPerHour': rounded(x.get('homeWallUsdPerHour'), 5),
            'homeNetworkUsdPerHour': rounded(network.get('usdPerHour'), 5),
            'homeNetworkSpread': rounded(network.get('spread')),
            'homeNetworkSource': network.get('source'),
            'homeNetworkDedicated': network.get('dedicated'),
            'homeNetworkRequests': rounded(network.get('requests'), 1),
            'memoryGB': x.get('memoryGB'),
            'costUsd': r.get('costUsd'),
            'needUsdPerHour': r.get('needUsdPerHour'),
            'pFail': r.get('pFail'),
            'checks': arming['checks'],
            'armedSince': arming.get('since'),
            'trial': bool(r.get('trial')),
            'trialLow': r.get('trialLow'),
            'demandRatio': r.get('demandRatio'),
            'lessonFactor': r.get('lessonFactor'),
        },
    }


def end_check(state, excursion, now, context):
    """Pure (manager.manager_excursion_end): {'code', 'reason'} to go home, else None."""
    rules = context.get('rules') or {}
    if not enabled(rules):
        return {'code': 'off', 'reason': 'excursions were turned off'}
    if paused(state.get('manager') or {}, now):
        return {'code': 'paused', 'reason': 'the switch record turned excursions off'}
    x = context.get('excursions')
    active = x.get('active') if isinstance(x, dict) else None
    if not isinstance(active, dict) or active.get('model') != excursion.get('target'):
        return None
    started = excursion.get('startedAt')
    elapsed = now - started if finite(started) else 0
    realized, high = active.get('realizedUsdPerHour'), active.get('realizedHigh')
    low = (active.get('home') or {}).get('low')
    if (
        elapsed >= EARLY_EXIT_SECONDS
        and (active.get('readySeconds') or 0) >= EARLY_EXIT_SECONDS  # an hour after the ramp
        and finite(realized)
        and finite(high)
        and finite(low)
        and high < low
    ):
        return {
            'code': 'early-exit',
            'reason': 'it paid $%.3f/h after warming up; home would likely have paid at least $%.3f/h'
            % (realized, low),
        }
    if elapsed >= DWELL_SECONDS and active.get('network'):
        latest = active.get('latestRatio')
        mixed = gemma_family((context.get('home') or {}).get('model')) and (
            active.get('latestDedicated') is False
        )
        if not finite(latest) or mixed:
            return {'code': 'faded', 'reason': 'public evidence for it is no longer visible'}
        if latest < FADE_RATIO:
            return {
                'code': 'faded',
                'reason': 'public evidence faded (latest hour %.2fx home)' % latest,
            }
    return None


# --- views --------------------------------------------------------------------


def evidence_view(context, evaluation):
    x = context.get('excursions') if isinstance(context, dict) else None
    if not isinstance(x, dict) or not evaluation:
        return None
    return {
        'cell': x.get('cell'),
        'updatedAt': x.get('updatedAt'),
        'home': (context.get('home') or {}).get('model'),
        'homeUsdPerHour': rounded(x.get('homeUsdPerHour')),
        'homeBasis': x.get('homeBasis'),
        'gate': evaluation['gate'],
        'environment': evaluation['environment'],
        'rows': copy.deepcopy(evaluation['rows']),
        'trialDemandRatio': TRIAL_DEMAND_RATIO,
        # Models with a lesson from the ledger, in plain words and as fields for the UI.
        'lessons': [
            {
                'model': item['model'],
                'triedAt': item.get('triedAt'),
                'endedAt': item.get('endedAt'),
                'until': item.get('until'),
                'home': item.get('home'),
                'code': item.get('code'),
                'realizedUsdPerHour': rounded(item.get('realizedUsdPerHour')),
                'homeUsdPerHour': rounded(item.get('homeUsdPerHour')),
                'factor': rounded(item.get('factor'), 3),
                'text': lesson_text(item),
            }
            for item in sorted(
                (x.get('lessons') or {}).values(), key=lambda i: (-(i.get('until') or 0), i['model'])
            )
            if finite(item.get('until'))
        ],
    }


def arming_view(arming, evaluation=None):
    if not arming:
        return None
    last = arming.get('lastCheckAt')
    row = ((evaluation or {}).get('byModel') or {}).get(arming.get('model')) or {}
    return {
        'model': arming.get('model'),
        'since': arming.get('since'),
        'checks': arming.get('checks') or 0,
        'needed': CHECKS_NEEDED,
        'checkSeconds': CHECK_SECONDS,  # checks are hourly
        'neededSeconds': (CHECKS_NEEDED - 1) * CHECK_SECONDS,  # first check to earliest move
        'ratio': row.get('ratio'),
        'lastCheckAt': last,
        'nextCheckAt': last + CHECK_SECONDS
        if finite(last) and (arming.get('checks') or 0) < CHECKS_NEEDED
        else None,
    }


def ledger_view(state, context, now):
    x = context.get('excursions') if isinstance(context, dict) else None
    if not isinstance(x, dict):
        return None
    summary = x.get('ledger') or {}
    off = paused(state.get('manager') or {}, now)
    setting = enabled(context.get('rules'))
    return {
        'days': LEDGER_DAYS,
        'count': summary.get('count') or 0,
        'gainUsd': rounded(summary.get('gainUsd') or 0.0),
        'predictedUsd': rounded(summary.get('predictedUsd') or 0.0),
        'enabled': bool(setting and not off),
        # 'ledger': the kill switch turned excursions off; 'setting': the user did.
        'disabledBy': 'ledger' if off else None if setting else 'setting',
        'disabledReason': off.get('reason')
        if off
        else None
        if setting
        else 'Turned off in the optimizer settings.',
        'disabledUntil': off.get('until') if off else None,
    }


def arming_note(arming, clock):
    """A short status suffix while a model is being watched."""
    if not arming or armed(arming):
        return ''
    return ' Public data favours %s; watching it (check %d of %d, next at %s).' % (
        arming['model'],
        arming.get('checks') or 0,
        CHECKS_NEEDED,
        clock(arming['lastCheckAt'] + CHECK_SECONDS),
    )


# --- ledger ---------------------------------------------------------------------


class Ledger:
    """One row per finished excursion (a few a day at most; kept like other optimizer evidence)."""

    COLUMNS = (
        ('started_at', 'startedAt'),
        ('ended_at', 'endedAt'),
        ('model', 'model'),
        ('home', 'from'),
        ('predicted', 'predictedUsdPerHour'),
        ('realized', 'realizedUsdPerHour'),
        ('counterfactual', 'homeCounterfactualUsdPerHour'),
        ('gain', 'gainUsd'),
        ('predicted_gain', 'predictedGainUsd'),
        ('end_code', 'endCode'),
        ('end_reason', 'endReason'),
        ('counterfactual_low', 'homeCounterfactualLow'),  # its 90% interval
        ('counterfactual_high', 'homeCounterfactualHigh'),
    )

    def __init__(self, history):
        self.h = history
        self.version = 0
        with history.lock:
            history.db.execute(
                'CREATE TABLE IF NOT EXISTS manager_excursions(account TEXT,device TEXT,'
                'started_at REAL,ended_at REAL,model TEXT,home TEXT,predicted REAL,realized REAL,'
                'counterfactual REAL,gain REAL,predicted_gain REAL,end_code TEXT,end_reason TEXT,'
                'PRIMARY KEY(account,device,started_at))'
            )
            have = {r[1] for r in history.db.execute('PRAGMA table_info(manager_excursions)')}
            for column in ('counterfactual_low', 'counterfactual_high'):
                if column not in have:
                    history.db.execute('ALTER TABLE manager_excursions ADD COLUMN %s REAL' % column)
            history.db.commit()

    def add(self, account, device, r):
        with self.h.lock:
            self.h.db.execute(
                'INSERT OR REPLACE INTO manager_excursions(account,device,%s) VALUES(?,?,%s)'
                % (','.join(c for c, _ in self.COLUMNS), ','.join('?' * len(self.COLUMNS))),
                (account, device, *(r.get(k) for _, k in self.COLUMNS)),
            )
            self.h.db.commit()
            self.version += 1

    def records(self, account, device, since):
        with self.h.lock:
            rows = self.h.db.execute(
                'SELECT %s FROM manager_excursions WHERE account=? AND device=? AND ended_at>=? '
                'ORDER BY ended_at' % ','.join(c for c, _ in self.COLUMNS),
                (account, device, since),
            ).fetchall()
        return [dict(zip((k for _, k in self.COLUMNS), tuple(r))) for r in rows]

    def summary(self, account, device, now):
        rows = self.records(account, device, now - LEDGER_DAYS * 86400)
        return {
            'count': len(rows),
            'gainUsd': sum(r['gainUsd'] for r in rows if finite(r['gainUsd'])),
            'predictedUsd': sum(
                r['predictedGainUsd'] for r in rows if finite(r['predictedGainUsd'])
            ),
        }

    def realized(self, account, device, start, end):
        """Inference credits this Mac earned in [start, end), $ (base rewards excluded)."""
        with self.h.lock:
            row = self.h.db.execute(
                """SELECT COALESCE(SUM(micro_usd),0) FROM opt_credits WHERE account=? AND at>=? AND at<?
                AND model!='base_reward' AND provider IN (SELECT provider FROM opt_identity WHERE device=?)""",
                (account, start, end, device),
            ).fetchone()
        return (row[0] or 0) / 1e6


def book(optimizer, ledger, entry, account, device):
    """Background: write one finished excursion to the ledger; returns the record or None."""
    start = entry.get('leftAt') if finite(entry.get('leftAt')) else entry.get('startedAt')
    end = entry.get('endedAt')
    if not finite(start) or not finite(end) or end <= start:
        return None
    hours = (end - start) / 3600
    usd = ledger.realized(account, device, start, end)
    trailing = entry.get('homeUsdPerHour') if finite(entry.get('homeUsdPerHour')) else None
    # Realized pay here is per clock hour (all credits in the window), so home's is too.
    wall = entry.get('homeWallUsdPerHour') if finite(entry.get('homeWallUsdPerHour')) else trailing
    home = entry.get('from')
    before = {
        'usdPerHour': entry.get('homeNetworkUsdPerHour'),
        'spread': entry.get('homeNetworkSpread'),
        'source': entry.get('homeNetworkSource'),
        'dedicated': entry.get('homeNetworkDedicated'),
        'requests': entry.get('homeNetworkRequests'),
    }
    during = None
    ne = getattr(optimizer, 'network_evidence', None)
    if ne is not None and home and finite(before['usdPerHour']) and before['usdPerHour'] > 0:
        try:
            during = network_point(
                ne.estimate(home, hours=max(1, math.ceil(hours)), now=end, basis=BASIS)
            )
        except Exception:
            log.exception('Could not read the network home rate for the excursion ledger')
    cf = counterfactual(wall, before, during) or {}
    rate = cf.get('usdPerHour')
    # Costs the credits in [start, end) do not show: base periods lost by both switches (base
    # rewards are left out on both sides) and, after a return home, home's ramp.
    back = entry.get('endCode') not in NO_RETURN
    floor = floor_usd_per_month(entry.get('memoryGB'))
    unseen = (1 + back) * EPOCHS_LOST * floor / EPOCHS_PER_MONTH
    if back and home and rate is not None:
        unseen += rate * ramp_minutes(home) / 60
    predicted = entry.get('predictedUsdPerHour')
    record = {
        'model': entry.get('target'),
        'from': home,
        'startedAt': entry.get('startedAt'),
        'endedAt': end,
        'predictedUsdPerHour': predicted if finite(predicted) else None,
        'realizedUsdPerHour': usd / hours,
        'homeCounterfactualUsdPerHour': rate,
        'homeCounterfactualLow': cf.get('low'),
        'homeCounterfactualHigh': cf.get('high'),
        'counterfactualBasis': cf.get('basis', 'trailing'),
        'unseenCostUsd': unseen,
        'gainUsd': usd - rate * hours - unseen if rate is not None else None,
        'predictedGainUsd': (predicted - trailing) * hours
        if finite(predicted) and trailing is not None
        else None,
        'endCode': entry.get('endCode'),
        'endReason': entry.get('endReason'),
    }
    ledger.add(account, device, record)
    return record


def booked_text(r):
    gain = r.get('gainUsd')
    return 'Excursion to %s ended (%s): $%.3f/h realized vs $%s/h expected at home%s.' % (
        r['model'],
        r.get('endReason') or r.get('endCode') or 'returned',
        r['realizedUsdPerHour'],
        '%.3f' % r['homeCounterfactualUsdPerHour']
        if finite(r.get('homeCounterfactualUsdPerHour'))
        else '?',
        ', %s$%.3f against staying home' % ('+' if gain >= 0 else '-', abs(gain))
        if finite(gain)
        else '',
    )


def kill(ledger, account, device, m, now):
    """The kill switch (PLAN 6.6, 6.9): with >= KILL_MIN_COUNT excursions booked over
    LEDGER_DAYS (since excursions were last turned on), turn excursions off unless the 90%
    lower bound of their gain is at least zero. They stay off until the user turns them
    off and on again: excursions must prove they pay."""
    since = max(now - LEDGER_DAYS * 86400, m.get('excursionLedgerSince') or 0)
    gains = [r['gainUsd'] for r in ledger.records(account, device, since) if finite(r['gainUsd'])]
    if len(gains) < KILL_MIN_COUNT:
        return None
    mean, low = ledger_bound(gains)
    if low >= 0:
        return None
    total = mean * len(gains)
    result = (
        'earned $%.2f less than staying home.' % -total
        if total < 0
        else 'earned only $%.2f more than staying home, not clearly enough.' % total
    )
    return {
        'at': now,
        'count': len(gains),
        'gainUsd': round(total, 4),
        'lowUsd': round(low * len(gains), 4),
        'reason': 'The last %d excursions %s To try again, turn excursions off and back on.'
        % (len(gains), result),
    }


# --- data (I/O) -------------------------------------------------------------------


def demand_readings(store, models, now):
    """model -> {'ratio', 'hours'}: for each of the last TRIAL_DEMAND_HOURS hours (newest first),
    the median load (active + queued requests, the latest capacity sample per 30 s, as
    demand_alerts reads it) over that hour's usual load (demand_alerts.usual_levels, time-of-day
    scope only); ratio = the lowest hour's (None while any hour lacks a reading). Reads through a
    read-only snapshot (demand_baselines.read_view): usual_levels scans 30 days."""
    from demand_alerts import _samples, usual_levels  # the app's one definition of "usual"
    from demand_baselines import read_view

    span = TRIAL_DEMAND_HOURS * 3600
    loads = defaultdict(list)
    with read_view(store) as view:
        with view.h.lock:
            for row in view.h.db.execute(
                _samples(','.join('?' * len(models))) + 'SELECT model,at,load FROM samples',
                (*models, now - span, now + 1, now),
            ):
                loads[row['model']].append((row['at'], row['load']))
            usual = [
                usual_levels(view.h.db, models, now - k * 3600) for k in range(TRIAL_DEMAND_HOURS)
            ]
    out = {}
    for model in models:
        hours = []
        for k in range(TRIAL_DEMAND_HOURS):
            end = now - k * 3600
            seen = [load for at, load in loads[model] if end - 3600 < at <= end]
            level = usual[k].get(model) or {}
            base = level.get('baselineLoad') if level.get('baselineScope') == 'daytype_hour' else None
            load = statistics.median(seen) if len(seen) >= TRIAL_DEMAND_SAMPLES else None
            hours.append(
                {
                    'end': end,
                    'load': load,
                    'usualLoad': base,
                    'samples': len(seen),
                    'ratio': load / base if finite(load) and finite(base) and base > 0 else None,
                }
            )
        ratios = [h['ratio'] for h in hours]
        out[model] = {
            'at': now,
            'ratio': min(ratios) if all(finite(v) for v in ratios) else None,
            'hours': hours,
        }
    return out


class ExcursionData:
    """Builds context['excursions'] for the pure rules. Background and GET previews share it."""

    def __init__(self, optimizer):
        self.o = optimizer
        self.ledger = Ledger(optimizer.h)
        self.lock = threading.Lock()
        self.key = self.value = None
        self.value_at = None
        self.dead = (None, None, {})  # (account/device, at, model -> seconds)
        self.demand_cache = (None, {})  # (models, 5-minute slot), model -> reading

    def invalidate(self):
        with self.lock:
            self.key = self.value = None
            self.demand_cache = (None, {})

    def context(self, settings, live, raw, rows, home, current, now):
        m = settings.get('manager') or {}
        name = (home or {}).get('model')
        excursion = m.get('excursion') if (m.get('excursion') or {}).get('target') == current else None
        models = sorted(
            r['id']
            for r in rows
            if r.get('available') and len(members(r['id'])) == 1 and r['id'] != name
        )
        key = (
            live.get('account', ''),
            live.get('device', ''),
            name,
            current,
            tuple(models),
            (excursion or {}).get('startedAt'),
            settings.get('lastSwitchAt'),
            self.ledger.version,
            int(now // CACHE_SECONDS),
        )
        with self.lock:
            if self.key == key:
                return copy.deepcopy(self.value)
        value = self.build(settings, live, rows, home, current, now, models, excursion)
        with self.lock:
            self.key, self.value = key, value
        return copy.deepcopy(value)

    def build(self, settings, live, rows, home, current, now, models, excursion):
        from manager import history_rates

        o = self.o
        account, device = live.get('account', ''), live.get('device', '')
        name = (home or {}).get('model')
        ne = getattr(o, 'network_evidence', None)
        everything = sorted({r['id'] for r in rows} | ({current} if current else set()))
        earned, _ = o.demand_auto.evidence(account, device, everything, now)
        rate, basis = self.home_rate(earned, home, now, history_rates)
        wall = self.wall_rate(account, device, earned, name, basis, now)
        dead = self.dead_seconds(account, device, now)
        cell = updated = None
        home_network = {}
        if ne is not None:
            try:
                cell = ne.own_cell()
                with ne.lock:
                    updated = (ne.last_window or {}).get('at')
                if name and basis in BASE_HOURS:
                    e = ne.estimate(name, cell, hours=BASE_HOURS[basis], now=now, basis=BASIS)
                    home_network = {
                        **network_point(e),
                        'providers': e.get('providers'),
                    }
            except Exception:
                log.exception('Network evidence for the home model failed')
        candidates = []
        for model in models:
            c = {
                'model': model,
                'priorSuccess': ((earned.get(model) or {}).get('seconds') or 0)
                >= PRIOR_SUCCESS_SECONDS,
            }
            if ne is not None and name:
                c.update(self.ratio(ne, model, name, cell, now))
            candidates.append(c)
        # Demand readings only for models a trial could move to (the SQL reads 30 days).
        trials = [
            c['model']
            for c in candidates
            if c.get('calibrated') is False
            and finite(c.get('trialLow'))
            and c['trialLow'] > MIN_RATIO_LOW
        ]
        demand = self.demand(trials, now) if trials else {}
        active = None
        if excursion and name:
            saved = excursion.get('evidence') if isinstance(excursion.get('evidence'), dict) else {}
            trailing = saved.get('homeUsdPerHour') if finite(saved.get('homeUsdPerHour')) else rate
            before = {
                'usdPerHour': saved.get('homeNetworkUsdPerHour'),
                'spread': saved.get('homeNetworkSpread'),
                'source': saved.get('homeNetworkSource'),
                'dedicated': saved.get('homeNetworkDedicated'),
                'requests': saved.get('homeNetworkRequests'),
            }
            active = self.active(
                ne,
                earned,
                excursion,
                current,
                name,
                cell,
                now,
                ramp_seconds(current, dead),
                trailing,
                before,
            )
        environment = None
        if name and current == name:
            try:
                environment = o.environment_reason(live, now)
            except Exception:
                environment = 'Waiting for current readings.'
        try:
            summary = self.ledger.summary(account, device, now)
            taught = lessons(self.ledger.records(account, device, now - LESSON_SECONDS), now)
        except sqlite3.Error:
            log.exception('Could not read the excursion ledger')
            summary, taught = {}, {}
        total = (live.get('hardware') or {}).get('memoryTotalGB')
        return {
            'cell': cell,
            'updatedAt': updated,
            'homeUsdPerHour': rate,
            'homeBasis': basis,
            'homeWallUsdPerHour': wall,
            'homeNetwork': home_network,
            'memoryGB': total if finite(total) else None,
            'deadSeconds': dead,
            'today': self.started_today(account, device, now),
            'lastSwitchAt': settings.get('lastSwitchAt'),
            'environment': environment,
            'candidates': candidates,
            'active': active,
            'ledger': summary,
            'lessons': taught,
            'demand': demand,
        }

    @staticmethod
    def home_rate(earned, home, now, history_rates):
        """Own realized home $/ready-hour: last 72 h (>= 4 h), else 14 days, else the saved home."""
        name = (home or {}).get('model')
        if not name:
            return None, None
        minutes = [
            x
            for x in (earned.get(name) or {}).get('minutes', [])
            if x.get('at', 0) >= now - HOME_TRAILING_SECONDS
        ]
        seconds = sum(x['seconds'] for x in minutes)
        if seconds >= HOME_TRAILING_MIN_SECONDS:
            return sum(x['usd'] for x in minutes) * 3600 / seconds, '72h'
        rates = history_rates(earned, now)
        if name in rates:
            return rates[name]['usdPerHour'], '14d'
        if finite(home.get('usdPerHour')):
            return home['usdPerHour'], 'saved'
        return None, None

    def wall_rate(self, account, device, earned, name, basis, now):
        """Own home $ per clock hour over its stints (ready minutes <= SPAN_GAP_SECONDS apart)
        in the window of the home rate: every credit counts, as in the ledger's realized pay
        (never less than the credits of its ready minutes, which are known)."""
        from manager import HOME_LOOKBACK

        window = {'72h': HOME_TRAILING_SECONDS, '14d': HOME_LOOKBACK}.get(basis)
        if not name or not window:
            return None
        minutes = [
            x for x in (earned.get(name) or {}).get('minutes', []) if x.get('at', 0) >= now - window
        ]
        spans = []
        for at in sorted(x['at'] for x in minutes):
            if spans and at - spans[-1][1] <= SPAN_GAP_SECONDS:
                spans[-1][1] = at + 60
            else:
                spans.append([at, at + 60])
        seconds = sum(b - a for a, b in spans)
        if seconds < HOME_TRAILING_MIN_SECONDS:
            return None
        try:
            usd = sum(self.ledger.realized(account, device, a, b) for a, b in spans)
        except sqlite3.Error:
            log.exception('Could not read home credits')
            return None
        return max(usd, sum(x['usd'] for x in minutes)) * 3600 / seconds

    @staticmethod
    def ratio(ne, model, home, cell, now):
        try:
            wide = ne.relative(model, home, cell, hours=EVIDENCE_HOURS, now=now, basis=BASIS)
            latest = ne.relative(model, home, cell, hours=LATEST_HOURS, now=now, basis=BASIS)
        except Exception:
            log.exception('Network evidence for %s failed', model)
            return {}
        a, b = wide.get('estimate') or {}, wide.get('home_estimate') or {}
        return {
            'ratio': wide.get('ratio'),
            'low': wide.get('low'),
            'high': wide.get('high'),
            'source': wide.get('source'),
            'dedicated': wide.get('dedicated_only'),
            'providers': min(a.get('providers') or 0, b.get('providers') or 0),
            'latestRatio': latest.get('ratio'),
            'latestLow': latest.get('low'),
            'usdPerHour': a.get('usd_per_h'),  # for predicted_ratio's cap
            'homeUsable': usable(network_point(b)),
            'calibrated': a.get('calibrated'),  # own jobs calibrate its $/request
            'trialLow': wide.get('trial_low'),  # the 90% low at the trial margin (uncalibrated)
        }

    @staticmethod
    def active(ne, earned, excursion, current, home, cell, now, ramp, trailing, before):
        """Realized pay per ready hour on the target from the switch command + its ramp, with its
        90% upper bound, what home would have paid per ready hour over the same window
        (counterfactual) and the latest ratio."""
        left = excursion.get('leftAt') if finite(excursion.get('leftAt')) else None
        start = (left if left is not None else excursion.get('startedAt', now)) + ramp
        end = now - SETTLEMENT_SECONDS
        minutes = [
            x
            for x in (earned.get(current) or {}).get('minutes', [])
            if x.get('at', 0) >= start and x.get('at', 0) + 60 <= end
        ]
        wall = end - start
        ready = sum(x['seconds'] for x in minutes)
        realized = sum(x['usd'] for x in minutes) * 3600 / ready if ready >= 600 else None
        out = {
            'model': current,
            'rampSeconds': ramp,
            'realizedUsdPerHour': realized,
            'realizedHigh': realized_high(sorted(minutes, key=lambda x: x['at']), realized),
            'realizedSeconds': max(0, wall),
            'readySeconds': ready,
            'network': ne is not None,
            'latestRatio': None,
            'home': None,
        }
        during = None
        if ne is not None:
            try:
                latest = ne.relative(
                    current, home, cell, hours=LATEST_HOURS, now=now, basis=BASIS
                )
                out.update(
                    latestRatio=latest.get('ratio'),
                    latestSource=latest.get('source'),
                    latestDedicated=latest.get('dedicated_only'),
                )
                if wall >= 600:
                    hours = max(1, math.ceil(wall / 3600))
                    during = network_point(
                        ne.estimate(home, cell, hours=hours, now=now, basis=BASIS)
                    )
            except Exception:
                log.exception('Network evidence for the active excursion failed')
                out['network'] = False
        out['home'] = counterfactual(trailing, before, during)
        return out

    def demand(self, models, now):
        """model -> demand reading (demand_readings), cached DEMAND_CACHE_SECONDS."""
        key = (tuple(sorted(models)), int(now // DEMAND_CACHE_SECONDS))
        with self.lock:
            if self.demand_cache[0] == key:
                return copy.deepcopy(self.demand_cache[1])
        store = getattr(self.o, 'store', None)
        value = {}
        if store is not None:
            try:
                value = demand_readings(store, list(key[0]), now)
            except Exception:
                log.exception('Could not read network demand for trial excursions')
        with self.lock:
            self.demand_cache = (key, value)
        return copy.deepcopy(value)

    def dead_seconds(self, account, device, now):
        """model -> this Mac's median seconds from a switch command to the first paid job."""
        key = (account, device)
        with self.lock:
            if self.dead[0] == key and 0 <= now - self.dead[1] < DEAD_CACHE_SECONDS:
                return dict(self.dead[2])
        try:
            value = self.measure_dead(account, device, now)
        except sqlite3.Error:
            log.exception('Could not measure switch dead times')
            value = {}
        with self.lock:
            self.dead = (key, now, value)
        return dict(value)

    def measure_dead(self, account, device, now):
        """Successful switches ('switching' then 'switched' for the same solo model, not a
        restart of the model already serving) in DEAD_LOOKBACK_SECONDS; for each, the first
        paid job on that model from this Mac after the command. An episode with none before
        the next command counts with its observed time, a lower bound; the open episode
        counts once it has been paid."""
        h = self.o.h
        with h.lock:
            events = h.db.execute(
                "SELECT at,kind,model FROM opt_events WHERE account=? AND device=? AND at>=? AND at<=? "
                "AND kind IN ('switching','switched','failed','recovered','switch-deferred') ORDER BY at",
                (account, device, now - DEAD_LOOKBACK_SECONDS, now),
            ).fetchall()
        switches, serving, command = [], None, None
        for at, kind, model in events:
            if kind == 'switching':
                if switches and switches[-1][2] is None:
                    switches[-1][2] = at  # the next command censors the previous episode
                command = (model, at, serving)
                continue
            if kind == 'switched':
                if command and command[0] == model != command[2] and len(members(model)) == 1:
                    switches.append([model, command[1], None])
                serving = model
            elif kind != 'switch-deferred':  # a deferred switch left the model serving
                serving = None
            command = None
        found = {}
        for model, start, stop in switches:
            with h.lock:
                row = h.db.execute(
                    'SELECT at FROM opt_credits WHERE account=? AND at>=? AND at<? AND model=? '
                    'AND micro_usd>0 AND provider IN (SELECT provider FROM opt_identity WHERE device=?) '
                    'ORDER BY at LIMIT 1',
                    (account, start, now if stop is None else stop, model, device),
                ).fetchone()
            if row or stop is not None:
                found.setdefault(model, []).append((row[0] if row else stop) - start)
        return {
            model: statistics.median(values)
            for model, values in found.items()
            if len(values) >= DEAD_MIN_SWITCHES
        }

    def started_today(self, account, device, now):
        try:
            with self.o.h.lock:
                rows = self.o.h.db.execute(
                    'SELECT payload FROM demand_switch_runs WHERE account=? AND device=? AND at>? AND at<=?',
                    (account, device, now - 86400, now),
                ).fetchall()
        except sqlite3.Error:
            return 0
        count = 0
        for (payload,) in rows:
            try:
                count += json.loads(payload).get('kind') == 'excursion'
            except (TypeError, ValueError, AttributeError):
                pass
        return count
