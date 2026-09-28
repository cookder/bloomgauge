"""Manager strategy: hold a home model, recover instead of pausing, move only on evidence.

The legacy demand optimizer (demand_optimizer.decide) still runs for display, but
under this strategy its trials, idle/stall escape, protect-level shortfall, spike
and learning trials, gpt-oss fallback and paid-upgrade exploration never pick a
target. The manager returns to the home model, holds it, and leaves room for
evidence-armed excursions (`manager_excursion`, rules in excursions.py): a move to a
model that public network data shows clearly better for this hardware class, for
hours, with a ledger that turns excursions off (until the user turns them back on)
when their results over 14 days aren't clearly positive.

Home, in order: the user's manual pick (a pin, never reverted), the own-history
solo model with the best multi-day lower bound of realized $/ready-hour over the
last 30 days (>= 3 days with a ready hour each; a challenger's bound must beat the
saved home's mean), else the current model (never a gpt-oss fallback or trial pick).

A failed automatic switch never pauses: the manager checks whether the target
became ready anyway, else restores the previous (or home) model; after two failed
restores the watchdog's back-off takes over and the user is told. A readiness
watchdog restores home (or the pin) when no model has been ready for
W = max(10 min, drain deadline + 3 x median load time).
State lives in `optimizer.state['manager']`; Optimizer owns every command.
"""

import copy, logging, math, pathlib, statistics, subprocess, threading, time
from datetime import datetime
import excursions
from model_combinations import members, selection_key, same_selection, selection_label
from model_readiness import session_key
from optimizer_store import device_id
from provider_sessions import matching_process, process_identity
from provider_reporting import state_fresh
from demand_targets import GEMMA

# Mixed advertisements earned ~0.46x dedicated gemma in the same hardware cell.
SOLO_MODELS = frozenset({GEMMA})
HOME_LOOKBACK = 14 * 86400  # history_rates (the excursions' realized home rate)
HOME_MIN_HOURS = 4
# Home choice: all the evidence the optimizer reads (demand_optimizer.LOOKBACK), as one realized
# $/ready-hour per 24 h block with >= 1 ready hour of the model (PLAN §6.3: the estimate's error
# falls from $0.074 to $0.049 after one busy hour). Each day weighs the same, so a one-day regime
# (qwen3.5-35b, Sep 9-10: 19 h at $0.22, then ~$0.00) cannot carry a model.
HOME_WINDOW = 30 * 86400
HOME_DAY_SECONDS = 3600
HOME_MIN_DAYS = 3
# Days are local calendar days, not 24 h counted back from now. One stint (ready minutes
# <= 1 h apart) counts as at most one day per full 24 h it spans, so a 26-36 h regime or pin
# is not three days.
HOME_STINT_GAP_SECONDS = 3600
HOME_HOLD_SECONDS = 86400  # the saved home must be a day old before a challenger replaces it
# One-sided 90% Student-t quantiles for 1..30 degrees of freedom (days - 1); 1.282 beyond. The
# same 90% level as the network evidence's lower bound.
T90 = (
    3.078, 1.886, 1.638, 1.533, 1.476, 1.440, 1.415, 1.397, 1.383, 1.372,
    1.363, 1.356, 1.350, 1.345, 1.341, 1.337, 1.333, 1.330, 1.328, 1.325,
    1.323, 1.321, 1.319, 1.318, 1.316, 1.315, 1.314, 1.313, 1.311, 1.310,
)  # fmt: skip
# Auto-chosen failed targets: 24 h, then 48 h, then 96 h for repeat failures (PLAN §6.4); a
# model that becomes ready starts over. Manual picks are never blocked.
BLOCK_SECONDS = 86400
BLOCK_MAX_SECONDS = 4 * 86400
HOME_RETRY_SECONDS = 3600  # a failed return home retries after 1 h, 2 h, 4 h ... (<= 24 h)
RETRY_SECONDS = 15 * 60  # a move that could not start (no command sent)
DEFER_RETRY_SECONDS = 60  # a deferred move retries after 1, 2, 4 ... min (<= RETRY_SECONDS)
RESTORE_GRACE_SECONDS = 180  # after a failed switch: does the target become ready anyway?
MEMORY_WAIT_SECONDS = 180  # a pre-warm waiting for memory fails after this; restore follows
OBSERVE_SECONDS = 180  # after launch or wake, readings settle before the watchdog acts
RESTORE_ATTEMPTS = 2  # failed restores before the watchdog's back-off takes over
RESTORE_RETRY_SECONDS = 60
RELOAD_DEFERRALS = 5  # watchdog/recovery reloads deferred in a row before a restart is used
WATCHDOG_MIN_SECONDS = 600
WATCHDOG_BACKOFF_SECONDS = 1800
# 30 min, 1 h, then every 2 h, sooner after a state change. Two restores per 2 h is in line
# with the one-restart-an-hour App Attest budget (PLAN §6.5 gate 3).
WATCHDOG_BACKOFF_MAX = 2 * 3600
# The grace above is a floor: a failed target gets this Mac's p90 of command -> ready for that
# model (successful switches in 30 days) when there are >= 10 of them (fewer make the p90 just
# the maximum).
GRACE_MIN_SAMPLES = 10
LOAD_SAMPLES = 20  # per model, for the restore grace (load_timed)
LOAD_SECONDS = 90
WAKE_GAP_SECONDS = 90
IDLE_DEFAULT_MINUTES = 60  # Darkbloom's idle-unload default (IdleCommand.swift)
EXCURSION_MAX_MINUTES = 24 * 60  # runaway cap; excursions end on evidence (excursions.end_check)
STOPPED_WINDOW_SECONDS = 20 * 60  # Bloomkeeper restarts a provider its own command left stopped
STOPPED_GRACE_SECONDS = 90
BUSY = {'draining', 'switching', 'loading', 'starting', 'restarting', 'verifying', 'preloading'}
PIN_SOURCES = ('manual', 'external')
# Darkbloom's idle timeout unloaded a model that served: base rewards need a loaded model at
# each 5-minute settlement, so it is loaded again at once, at most once an hour per model.
# Darkbloom's default timeout is 60 idle minutes (IdleCommand.swift), so under that default a
# reloaded model cannot be unloaded again sooner; an earlier unload is some other policy, and
# an hourly reload stops the manager from fighting it in a loop.
IDLE_RELOAD_SECONDS = 3600

log = logging.getLogger('bloom.manager')


class Blocked(Exception):
    """A restore precondition failed; nothing was sent."""


def finite(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def enabled(rules):
    return (rules if isinstance(rules, dict) else {}).get('managerStrategy', 1) == 1


def strategy(rules):
    return 'manager' if enabled(rules) else 'legacy'


def active(state):
    return state.get('mode') == 'demand' and enabled(state.get('demandPolicy'))


def hold_error(selection):
    """Why the manager can't hold this serving selection, or None: one model, or a pair
    without a solo-only model (SOLO_MODELS). Pairs are held and restored, never scored."""
    models = members(selection)
    if len(models) == 1 or (len(models) == 2 and not set(models) & SOLO_MODELS):
        return None
    if set(models) & SOLO_MODELS:
        return (
            'Gemma is served alone: next to another model it earns about half as much. '
            'Serve it alone, or a pair without it.'
        )
    return (
        'Automatic control needs one serving model, or a pair without gemma. Choose it in the '
        'manual model controls in Optimizer → Overview.'
    )


def in_pool(selection, pool):
    """The plan's model pool includes this selection: a pair counts when both its models do."""
    models = members(selection)
    return bool(selection) and (selection in pool or len(models) == 2 and set(models) <= set(pool))


def pin(model, now, source='manual'):
    return {'model': model, 'source': source, 'at': now, 'failures': 0}


def pinned(home):
    return bool(home and home.get('source') in PIN_SOURCES)


def clock(at):
    return datetime.fromtimestamp(at).strftime('%I:%M %p').lstrip('0') if finite(at) else 'later'


def release(state):
    """Turning automatic control off releases a pin and any recovery in progress."""
    m = state.get('manager')
    if isinstance(m, dict):
        if pinned(m.get('home')):
            m.pop('home')
        finish_excursion(m, time.time(), 'automatic control was turned off', 'off')
        for key in (
            'resume',
            'recovery',
            'watchdog',
            'retryAt',
            'homeRetry',
            'excursion',
            'arming',
            'restoreAt',
            'restoreBlocker',
            'deferred',
        ):
            m.pop(key, None)


def interrupted(state, pending, now):
    """The app closed mid-switch. Keep automatic control on; recover from what is serving."""
    m = state.setdefault('manager', {})
    # An interrupted restore is the watchdog's again; a demand move gets the recovery check.
    if pending.get('kind') == 'demand' and pending.get('model') and pending.get('previous'):
        m['recovery'] = {
            'failedTarget': pending['model'],
            'previous': pending['previous'],
            'at': now,
            'attempts': 0,
            'interrupted': True,
        }


def resume_manual(state, request_id, model, status):
    """A manual pick made under the manager ends: it becomes the pin and automatic control resumes."""
    m = state.get('manager') or {}
    resume = m.get('resume') or {}
    if not request_id or resume.get('id') != request_id:
        return False
    m.pop('resume')
    model = model or resume.get('model')
    if status in ('completed', 'recovered', 'failed', 'unchanged', 'interrupted') and model:
        m['home'] = pin(model, time.time())
        if status in ('recovered', 'failed'):
            m['home']['failures'] = 1
        for key in ('recovery', 'watchdog', 'retryAt'):
            m.pop(key, None)
        finish_excursion(m, time.time(), 'you picked a model', 'manual')
    state['manager'] = m
    state['mode'] = 'demand'
    return True


def served(raw):
    """Requests this provider session served (the counters restart with the process)."""
    value = (raw.get('stats') if isinstance(raw.get('stats'), dict) else {}).get('requests_served')
    return value if finite(value) and value > 0 else 0


def provider_busy(raw):
    """The provider's own drain, load or switch, whoever started it. 'draining' never ends early."""
    lifecycle = raw.get('lifecycle') if isinstance(raw.get('lifecycle'), dict) else {}
    switch = raw.get('model_switch') if isinstance(raw.get('model_switch'), dict) else {}
    if lifecycle.get('outcome') == 'draining' or switch.get('outcome') == 'draining':
        return 'draining'
    capacity = raw.get('capacity') if isinstance(raw.get('capacity'), dict) else {}
    active = capacity.get('gpu_memory_active_gb')
    if (
        lifecycle.get('outcome') in BUSY
        or switch.get('outcome') in BUSY
        or raw.get('startup_preload_pending_models')
        or (finite(active) and active > 0 and not raw.get('warm_models'))
    ):
        return 'loading'
    return None


def loaded(raw, now):
    """Local proof a model is loaded and accepted, independent of the public roster."""
    from optimizer import drained_idle

    selected = members(selection_key(raw.get('advertised_models')))
    written = raw.get('written_at')
    failure = raw.get('last_model_load_error') or {}
    warm = raw.get('warm_models') if isinstance(raw.get('warm_models'), list) else []
    return bool(
        selected
        and finite(written)
        and -5 < now - written < 15
        and not drained_idle(raw)
        and (raw.get('trust') or {}).get('status') == 'online'
        and (raw.get('inference_active') is True or all(m in warm for m in selected))
    ), bool(
        selected
        and failure.get('model') in selected
        and finite(failure.get('at'))
        and failure['at'] >= (raw.get('started_at') or 0)
        and failure['model'] not in warm
    )


def config_path(home, options):
    """The provider.toml Darkbloom reads: `--config`, else the first that exists of
    ConfigManager.defaultConfigPath's candidates (the new path when none does)."""
    paths = [options[i + 1] for i, v in enumerate(options[:-1]) if v in ('--config', '-c')]
    if paths:
        return pathlib.Path(paths[0]).expanduser()
    candidates = [
        pathlib.Path(home) / suffix
        for suffix in (
            '.config/darkbloom/provider.toml',
            'Library/Application Support/darkbloom/provider.toml',
            '.config/eigeninference/provider.toml',
            'Library/Application Support/eigeninference/provider.toml',
        )
    ]
    return next((p for p in candidates if p.exists()), candidates[0])


def toml_selection(home, options):
    """`[backend] enabled_models` from provider.toml. Darkbloom 0.9.10's launchd child
    follows it rather than the launch agent's --model arguments. None when absent."""
    try:
        text = config_path(home, options).read_text()
    except (OSError, ValueError):
        return None
    section = ''
    for line in text.splitlines():
        line = line.split('#', 1)[0].strip()
        if line.startswith('['):
            section = line.strip('[] ')
        elif section == 'backend' and line.split('=', 1)[0].strip() == 'enabled_models':
            value = line.split('=', 1)[1] if '=' in line else ''
            if not value.strip().startswith('[') or not value.strip().endswith(']'):
                return None
            models = [m.strip().strip('"\'') for m in value.strip()[1:-1].split(',') if m.strip()]
            return selection_key(models)
    return None


def idle_unload_chosen(home, options):
    """The user chose Darkbloom's "free when idle" policy: `idle_timeout_mins` in provider.toml's
    [backend], or (only when the TOML doesn't set it, as Darkbloom's launchd child reads it:
    StartCommand.swift, idleTimeoutPinned) a `--idle-timeout` in the launch agent, other than
    0 ("always ready") and 60. Neither set, or exactly 60, is Darkbloom's default (unload after
    60 idle minutes; `darkbloom start`'s setup writes 60 into provider.toml), which Andrew
    decided (Sep 27) not to treat as a choice: the manager reloads the model. A setting that
    can't be read counts as chosen."""

    def chosen(value):
        try:
            return int(value.strip().strip('"\'')) not in (0, IDLE_DEFAULT_MINUTES)
        except (TypeError, ValueError):
            return True

    flags = [options[i + 1] for i, v in enumerate(options[:-1]) if v == '--idle-timeout']
    fallback = chosen(flags[-1]) if flags else False
    try:
        text = config_path(home, options).read_text()
    except FileNotFoundError:
        return fallback
    except (OSError, ValueError):
        return True
    section = ''
    for line in text.splitlines():
        line = line.split('#', 1)[0].strip()
        if line.startswith('['):
            section = line.strip('[] ')
            continue
        key, _, value = line.partition('=')
        key = key.strip().replace('"', '').replace("'", '').replace(' ', '')
        if (section, key) == ('backend', 'idle_timeout_mins') or (
            not section and key == 'backend.idle_timeout_mins'
        ):
            return chosen(value)
    return fallback


def history_rates(earned, now, exclude=()):
    """Realized $/ready-hour per solo model over 14 days (>= 4 h). `exclude`: [model, start,
    end] windows (excursions) whose minutes do not count."""
    rates = {}
    for model, evidence in (earned or {}).items():
        if len(members(model)) != 1:
            continue
        skip = [(a, b) for m, a, b in exclude if m == model]
        minutes = [
            m
            for m in evidence.get('minutes', [])
            if m.get('at', 0) >= now - HOME_LOOKBACK
            and not any(a <= m.get('at', 0) < b for a, b in skip)
        ]
        seconds = sum(m['seconds'] for m in minutes)
        if seconds >= HOME_MIN_HOURS * 3600:
            rates[model] = {
                'usdPerHour': sum(m['usd'] for m in minutes) * 3600 / seconds,
                'hours': seconds / 3600,
            }
    return rates


def stint_days(ats):
    """Separate days behind these ready-minute times: each stint (minutes <= HOME_STINT_GAP_SECONDS
    apart) counts one day per full 24 h it spans, at least one."""
    count, first, last = 0, None, None
    for at in sorted(ats):
        if last is not None and at - last > HOME_STINT_GAP_SECONDS:
            count += max(1, int((last + 60 - first) // 86400))
            first = None
        first = at if first is None else first
        last = at
    if first is not None:
        count += max(1, int((last + 60 - first) // 86400))
    return count


def local_day(at, span):
    """(ordinal, start, end) of the local calendar day holding `at`; `span` is the last one."""
    if span and span[1] <= at < span[2]:
        return span
    day = datetime.fromtimestamp(at).date()
    start = datetime(day.year, day.month, day.day).timestamp()
    return day.toordinal(), start, datetime.fromordinal(day.toordinal() + 1).timestamp()


def home_rates(earned, now, exclude=()):
    """Home-choice evidence per solo model over HOME_WINDOW: realized $/ready-hour on each local
    calendar day with >= 1 ready hour, their plain mean ('usdPerHour'), the ready hours and days
    behind it, and 'low', a one-sided 90% Student-t lower bound of that mean once there are
    >= 3 such days that are also >= 3 separate days (stint_days) and >= 4 hours (else None).
    `exclude`: [model, start, end] windows (excursions) whose minutes do not count."""
    out = {}
    for model, evidence in (earned or {}).items():
        if len(members(model)) != 1:
            continue
        skip = [(a, b) for m, a, b in exclude if m == model]
        days, span = {}, None
        for x in evidence.get('minutes', []):
            at = x.get('at', 0)
            if at >= now - HOME_WINDOW and not any(a <= at < b for a, b in skip):
                span = local_day(at, span)
                day = days.setdefault(span[0], [0, 0, []])
                day[0] += x['seconds']
                day[1] += x['usd']
                day[2].append(at)
        full = [d for d in days.values() if d[0] >= HOME_DAY_SECONDS]
        if not full:
            continue
        rates = [usd * 3600 / s for s, usd, _ in full]
        mean = sum(rates) / len(rates)
        hours = sum(d[0] for d in full) / 3600
        low = None
        separate = len(rates) >= HOME_MIN_DAYS and (
            stint_days(at for d in full for at in d[2]) >= HOME_MIN_DAYS
        )
        if separate and hours >= HOME_MIN_HOURS:
            t = T90[len(rates) - 2] if len(rates) - 1 <= len(T90) else 1.282
            low = mean - t * statistics.stdev(rates) / math.sqrt(len(rates))
        out[model] = {'usdPerHour': mean, 'hours': hours, 'days': len(rates), 'low': low}
    return out


def trial_selection(runs, model, last_switch):
    """The old optimizer's gpt-oss last resort (or a trial) put this model on: never a home."""
    run = next((r for r in runs or [] if r.get('result') == 'switched'), None)
    decision = (run or {}).get('decision') or {}
    return bool(
        run
        and run.get('model') == model
        and finite(run.get('completedAt'))
        and run['completedAt'] >= (last_switch or 0) - 60
        and (
            (decision.get('candidate') or {}).get('selectionReason') == 'fallback'
            or decision.get('kind') == 'explore'
        )
    )


def choose_home(m, earned, runs, current, allowed, now, last_switch=0):
    """Pin, else own history: the allowed model with the best lower bound (home_rates), kept
    until a day-old saved home's mean is beaten by a challenger's lower bound; else current.
    A saved home keeps that protection only while it is allowed and has a lower bound itself
    (the same minimum days); one no longer allowed is dropped."""
    saved = m.get('home') or {}
    if pinned(saved) and saved.get('model'):
        return dict(saved)
    rates = home_rates(earned, now, excursions.windows(m, now))
    best = max(
        (k for k in rates if k in allowed and rates[k]['low'] is not None),
        key=lambda k: (rates[k]['low'], k),
        default=None,
    )
    if best:
        old = rates.get(saved.get('model')) if saved.get('source') == 'history' else None
        if (
            old
            and saved['model'] in allowed
            and old['low'] is not None
            and saved['model'] != best
            and (
                rates[best]['low'] <= old['usdPerHour']
                or now - saved.get('at', 0) < HOME_HOLD_SECONDS
            )
        ):
            best = saved['model']
        return {
            'model': best,
            'source': 'history',
            'at': saved.get('at', now) if saved.get('model') == best else now,
            **rates[best],
        }
    if saved.get('model') in allowed:
        return dict(saved)
    if current and not hold_error(current) and not trial_selection(runs(), current, last_switch):
        return {'model': current, 'source': 'current', 'at': now}
    return None


def admission(row, rules):
    """Why this model cannot be loaded now, or None. Mirrors the legacy memory check."""
    if not row:
        return 'it is not in this Mac’s model list.'
    if not row.get('available'):
        return (row.get('reason') or 'it is not available').rstrip('.') + '.'
    budget = row.get('loadBudget')
    if not budget:
        return 'waiting for memory readings.'
    credit = budget.get('reclaimableGB') if finite(budget.get('reclaimableGB')) else 0
    if budget['afterUnloadGB'] + max(0, credit) < budget['requiredGB'] + rules['memoryHeadroomGB']:
        return (
            'waiting for enough free memory (%.1f GB needed, %.1f GB available after unloading).'
            % (
                budget['requiredGB'] + rules['memoryHeadroomGB'],
                budget['afterUnloadGB'] + max(0, credit),
            )
        )
    return None


def blocked(m, model, now):
    until = (m.get('blocked') or {}).get(model)
    return until if finite(until) and until > now else None


def policy_saved(state, previous, rules, now):
    """A saved policy change (caller holds the lock). Turning excursions off and on again
    lifts the ledger's pause and starts its 14-day window afresh."""
    m = state.get('manager')
    return isinstance(m, dict) and excursions.policy_saved(m, previous, rules, now)


def manager_excursion(state, now, context=None):
    """Excursion hook: an evidence-armed move away from home, or None (hold home).

    Called on each manager decision while the Mac serves its home model, the home is
    not a pin, and no excursion, recovery or retry hold is active. It must be pure and
    fast: no I/O and no state changes. The rules live in excursions.py (propose).

    state    read-only copy of the optimizer state; state['manager'] holds home,
             blocked (model -> until), arming, excursionHolds, excursionsOff,
             lastExcursion and lastGood.
    context  {'current', 'home' (dict), 'rows' ({id: candidate row with available,
             selected, memoryGB, loadBudget}), 'rules' (policy), 'blocked' (model ->
             until), 'account', 'device', 'excursions' (evidence gathered beforehand by
             excursions.ExcursionData), 'arming' (this decision's arming record) and
             'excursionEvaluation' (excursions.evaluate)}.

    Return None, or a proposal dict:
        target               solo model id: available, not home, not blocked
        reason               one sentence, shown as "Excursion to <target>: <reason>"
        predictedUsdPerHour  expected $/h on this Mac while away (> home's rate)
        maxMinutes           optional runaway cap (default 24 h); evidence ends it sooner
        evidence             optional JSON-safe dict, saved with the excursion
    The manager still applies memory admission, the 5-minute confirmation, the
    minimum run, the daily switch cap (excursion starts only), the 24/48/96 h block
    on a failed load and
    auto-restore. `manager_excursion_end` and `end_excursion` bring it home.
    """
    return excursions.propose(state, now, context or {})


def manager_excursion_end(state, excursion, now, context=None):
    """Early exit for an active excursion: {'code', 'reason'} (or a reason string) to go
    home, else None.

    Same purity rules and context as `manager_excursion`; `excursion` is the saved
    {'target', 'from', 'startedAt', 'leftAt', 'predictedUsdPerHour', 'reason',
    'maxMinutes', 'evidence'}. Rules in excursions.end_check: realized pay after the
    model's ramp below home's lower bound ('early-exit'), faded evidence ('faded'),
    excursions turned off ('off') or by the ledger ('paused'). maxMinutes is checked first
    by `excursion_end`; readiness belongs to the watchdog.
    """
    return excursions.end_check(state, excursion, now, context or {})


def end_excursion(state, reason, now, code=None):
    """Ask the manager to bring an active excursion home; the return is a normal move."""
    excursion = (state.get('manager') or {}).get('excursion')
    if excursion and not excursion.get('endReason'):
        excursion.update(endReason=reason, endingAt=now)
        if code:
            excursion['endCode'] = code
    return excursion


def finish_excursion(m, now, reason, code=None):
    """The excursion is over (caller holds the lock): keep a summary, queue it for the
    ledger and hold the model after an early exit or a faded/failed run."""
    excursion = m.pop('excursion', None)
    if excursion:
        record = {
            **{k: v for k, v in excursion.items() if k != 'evidence'},
            'endedAt': now,
            'endReason': excursion.get('endReason') or reason,
            'endCode': excursion.get('endCode') or code or 'returned',
        }
        m['lastExcursion'] = record
        excursions.finished(m, excursion, record, now)


def excursion_end(state, excursion, now, context):
    """(code, reason) when an active excursion should end, else None."""
    if excursion.get('endReason'):
        return excursion.get('endCode') or 'requested', excursion['endReason']
    limit = excursion.get('maxMinutes')
    limit = limit if finite(limit) else EXCURSION_MAX_MINUTES
    if now - excursion.get('startedAt', now) >= limit * 60:
        return 'max-duration', 'it reached its %s safety limit' % (
            '%d-minute' % limit if limit % 60 else '%d-hour' % (limit // 60)
        )
    end = manager_excursion_end(state, excursion, now, context)
    if isinstance(end, dict) and end.get('reason'):
        return end.get('code') or 'other', end['reason']
    if isinstance(end, str) and end:
        return 'other', end
    return None


def proposal_error(proposal, context, now):
    home = context['home']['model']
    target = proposal.get('target') if isinstance(proposal, dict) else None
    if not isinstance(target, str) or len(members(target)) != 1 or target == home:
        return 'invalid'
    if not isinstance(proposal.get('reason'), str) or not finite(
        proposal.get('predictedUsdPerHour')
    ):
        return 'invalid'
    if context['blocked'].get(target):
        return 'blocked'
    return admission(context['rows'].get(target), context['rules'])


def decide(decision, state, context, now):
    """Pure: replace the legacy target with the manager's. Never mutates its inputs."""
    m = state.get('manager') or {}
    rules, home, current = context['rules'], context['home'], context['current']
    excursion = m.get('excursion') if (m.get('excursion') or {}).get('target') == current else None
    target = kind = proposal = end = arming = None
    evaluation = excursions.evaluate(state, context, now)
    action = 'hold'
    name = (home or {}).get('model')
    if decision.get('controlError'):
        reason = decision['controlError']
        arming = m.get('arming')  # a passing problem: arming neither advances nor resets
    elif not home:
        reason = (
            'No home model yet: none has %d verified ready hours on %d separate days here in 30 days. Holding the current model.'
            % (HOME_MIN_HOURS, HOME_MIN_DAYS)
        )
    elif excursion:
        end = excursion_end(state, excursion, now, context)
        why = end and (admission(context['rows'].get(name), rules) or retry_hold(m, name, now))
        if end and not why:
            target, kind, action = name, 'home', 'end-excursion'
            reason = 'Ending the excursion to %s (%s). Returning to home model %s.' % (
                current,
                end[1],
                name,
            )
        elif end:
            reason = 'The excursion to %s is over (%s), but home model %s must wait: %s' % (
                current,
                end[1],
                name,
                why,
            )
        else:
            action = 'excursion'
            reason = 'Excursion to %s: %s' % (
                current,
                excursion.get('reason') or 'evidence favours it.',
            )
    elif current != name:
        failed = home.get('failedAt')
        why = admission(context['rows'].get(name), rules) or retry_hold(m, name, now)
        if failed:
            reason = (
                'Your pick %s could not be restored, so %s keeps serving. Pick it again to retry.'
                % (name, current or 'the current model')
            )
        elif finite(m.get('retryAt')) and m['retryAt'] > now:
            reason = 'The last move could not start. Returning to home model %s after %s.' % (
                name,
                clock(m['retryAt']),
            )
        elif why:
            reason = 'Waiting to return to %s model %s: %s' % (
                'your pinned' if pinned(home) else 'home',
                name,
                why,
            )
        else:
            target, kind, action = name, 'home', 'return-home'
            reason = (
                'Returning to your pick %s.' % name
                if pinned(home)
                else 'Returning to home model %s.' % name
            )
    elif pinned(home):
        reason = 'Holding your pick %s. Bloomkeeper will not switch away from it.' % name
    else:
        reason = 'Holding home model %s' % name + (
            ' (best paid on this Mac: $%.3f per ready hour over the last 30 days).'
            % home['usdPerHour']
            if home.get('source') == 'history' and finite(home.get('usdPerHour'))
            else '.'
        )
        if not (finite(m.get('retryAt')) and m['retryAt'] > now):
            arming = excursions.next_arming(m.get('arming'), evaluation, now)
            proposal = manager_excursion(
                copy.deepcopy(state),
                now,
                {**context, 'arming': copy.deepcopy(arming), 'excursionEvaluation': evaluation},
            )
            if proposal is not None and not proposal_error(proposal, context, now):
                target, kind, action = proposal['target'], 'excursion', 'excursion'
                reason = 'Excursion to %s: %s' % (target, proposal['reason'])
            else:
                proposal = None
                reason += excursions.arming_note(arming, clock)
        else:
            arming = m.get('arming')
    for selection in {name, current} - {None}:
        if len(members(selection)) == 2:  # a held pair reads "a + b", not its internal key
            reason = reason.replace(selection, selection_label(selection))
    result = dict(decision)
    result['opportunities'] = [
        {**r, 'confirmationEligible': False, 'confirmationMissing': False}
        for r in decision.get('opportunities', [])
    ]
    result.update(
        target=target,
        kind=kind,
        reason=reason,
        sourceAt=now if target else None,
        explorationTrigger=None,
        escapeReady=False,
        completedTrialExit=False,
        stallEscape=False,
        paidAlternative=None,
        spikeReview=None,
        manager=view(
            state,
            context,
            now,
            action,
            reason,
            proposal,
            arming=arming,
            evaluation=evaluation,
            end=end,
        ),
    )
    return result


def excursion_view(excursion, context):
    """The saved excursion plus its running realized $/h (the early-exit measure: wall clock
    since the start + 10 min, credits of the last 2 min excluded; null until 10 min count)."""
    if not isinstance(excursion, dict):
        return None
    out = copy.deepcopy(excursion)
    active = (context.get('excursions') or {}).get('active') if isinstance(context, dict) else None
    active = active if isinstance(active, dict) and active.get('model') == out.get('target') else {}
    realized = active.get('realizedUsdPerHour')
    out['realizedUsdPerHour'] = round(realized, 5) if finite(realized) else None
    return out


def control_summary(state, decision, watchdog, now):
    """Compact manager status for the 3-second control poll (optimizer_control.projection).

    decision  the latest background decision (optimizer.last_demand_decision) or None; its
              action, reason and realized pay are used while fresh (<= 60 s) and about the
              serving model. While no model is ready the watchdog's reason wins.
    """
    m = state.get('manager') or {}
    home = m.get('home') or {}
    view = (decision or {}).get('manager') or {}
    at = (decision or {}).get('at')
    fresh = bool(view) and finite(at) and -5 <= now - at <= 60
    dark = watchdog or {}
    out = {
        'at': at if fresh else None,
        'active': active(state),
        'home': home.get('model'),
        'homeSource': home.get('source'),
        'pinned': pinned(home),
        'action': view.get('action') if fresh else None,
        'reason': view.get('reason') if fresh else None,
        'watchdog': {'darkSince': dark.get('darkSince'), 'reason': dark.get('reason')}
        if finite(dark.get('darkSince'))
        else None,
        'recovery': {
            k: m['recovery'].get(k) for k in ('failedTarget', 'previous', 'at', 'attempts')
        }
        if isinstance(m.get('recovery'), dict)
        else None,
        'excursion': None,
        'arming': None,
    }
    if out['watchdog']:
        out['action'], out['reason'] = 'recover', out['watchdog']['reason'] or out['reason']
    excursion = m.get('excursion')
    if isinstance(excursion, dict) and excursion.get('target'):
        shown = view.get('excursion') if fresh else None
        same = isinstance(shown, dict) and shown.get('target') == excursion['target']
        out['excursion'] = {
            'target': excursion['target'],
            'startedAt': excursion.get('startedAt'),
            'predictedUsdPerHour': excursion.get('predictedUsdPerHour'),
            'realizedUsdPerHour': shown.get('realizedUsdPerHour') if same else None,
            'endReason': excursion.get('endReason'),
        }
    arming = view.get('arming') if fresh else None
    if isinstance(arming, dict) and arming.get('model'):
        out['arming'] = {
            k: arming.get(k)
            for k in ('model', 'checks', 'needed', 'checkSeconds', 'neededSeconds', 'since')
        }
    return out


def unpin(state, now):
    """Release the user's pin under the manager (caller holds the lock). The manager then
    chooses the home model again (own-history best, else the serving model) and returns to
    it right away (a return home needs no confirmation). Returns the released model."""
    if not active(state):
        raise ValueError('Turn the manager on before releasing a pinned model.')
    m = state.setdefault('manager', {})
    home = m.get('home') or {}
    if not pinned(home):
        raise ValueError('No model is pinned; the manager already chooses the home model.')
    if m.get('resume') or state.get('requestedModel') or state.get('pending'):
        raise ValueError('Wait for the current model change to finish, then release the pin.')
    m.pop('home')
    for key in ('homeRetry', 'retryAt', 'arming'):
        m.pop(key, None)
    m['lastAction'] = {'action': 'unpin', 'model': home.get('model'), 'at': now, 'success': True}
    state.pop('demandProposal', None)
    return home.get('model')


def retry_hold(m, model, now):
    hold = m.get('homeRetry') or {}
    if hold.get('model') == model and finite(hold.get('until')) and hold['until'] > now:
        return 'retrying after a failed load at %s.' % clock(hold['until'])
    return None


def back_off(wd, now, live):
    """Two restores failed: the watchdog waits 30 min, then 1 h, then 2 h at most before the next
    pair. `since` and `freeGB` let a state change end the wait early (ManagerControl.changed)."""
    backoff = min(wd.get('backoff') or WATCHDOG_BACKOFF_SECONDS, WATCHDOG_BACKOFF_MAX)
    free = ((live or {}).get('hardware') or {}).get('memoryAvailableGB')
    wd.pop('early', None)  # each back-off step allows one early retry
    wd.update(
        attempts=0,
        nextAt=now + backoff,
        backoff=min(2 * backoff, WATCHDOG_BACKOFF_MAX),
        since=now,
        freeGB=free if finite(free) else None,
    )
    return wd


def view(
    state,
    context,
    now,
    action=None,
    reason=None,
    proposal=None,
    arming=None,
    evaluation=None,
    end=None,
):
    """What the UI and the decision journal read: plain facts, no commands.

    Excursion fields (all optional and null without network evidence data):
      evidence      {cell, updatedAt, home, homeUsdPerHour, homeBasis ('72h' | '14d' |
                    'saved'), gate, environment, rows: [{model, usdPerHour, low, high,
                    ratio, ratioLow, latestRatio, providers, source, dedicated, eligible,
                    why, gainUsdPerHour?, needUsdPerHour?, costUsd?, pFail?}]} with at most
                    10 rows, home first. $/h figures are predictions for this Mac (own
                    home $/h x the public ratio). `why` is null for an eligible model,
                    else one of: home, not available, not selected, pair, gemma pair,
                    memory, blocked, cooling down, no evidence, mixed boxes, no home rate,
                    weak evidence, fading, gain too small, or the gate. `gate` (why no
                    model may be proposed now) is null or one of: off, paused, no home,
                    pinned, away, daily limit, dwell, environment.
      arming        {model, since, checks, needed, checkSeconds (3600: hourly checks),
                    neededSeconds (first check to the earliest move), ratio, lastCheckAt,
                    nextCheckAt} or null.
      excursion     the saved excursion plus realizedUsdPerHour (running, as judged by
                    the early exit; null for the first 10 minutes).
      ledger        {days, count, gainUsd, predictedUsd, enabled, disabledReason,
                    disabledUntil}: excursions that ended in the last `days` days,
                    realized gain against staying home and the gain that was predicted.
      excursionEnd  {code, reason} when this decision ends the active excursion.
    """
    m = state.get('manager') or {}
    home = context.get('home')
    return {
        'strategy': 'manager',
        'active': active(state),
        'action': action,
        'reason': reason,
        'home': copy.deepcopy(home),
        'pinned': pinned(home),
        'proposal': copy.deepcopy(proposal),
        'excursion': excursion_view(m.get('excursion'), context),
        'lastExcursion': copy.deepcopy(m.get('lastExcursion')),
        'recovery': copy.deepcopy(m.get('recovery')),
        'watchdog': copy.deepcopy(context.get('watchdog')),
        'retryAt': m.get('retryAt') if finite(m.get('retryAt')) and m['retryAt'] > now else None,
        'blocked': [
            {'model': k, 'until': v} for k, v in sorted(context.get('blocked', {}).items())
        ],
        'lastGood': copy.deepcopy(m.get('lastGood')),
        'lastAction': copy.deepcopy(m.get('lastAction')),
        'evidence': excursions.evidence_view(context, evaluation),
        'arming': excursions.arming_view(arming, evaluation),
        'ledger': excursions.ledger_view(state, context, now),
        'excursionEnd': {'code': end[0], 'reason': end[1]} if end else None,
    }


class ManagerControl:
    """Background half of the manager: recovery, watchdog and bookkeeping. Caller holds no lock."""

    def __init__(self, optimizer):
        self.o = optimizer
        self.dark_since = None
        self.dark_text = None
        self.offline_since = None
        self.last_command = None  # Bloomkeeper's own last `darkbloom start` (this app run)
        self.tick_at = None
        self.observed_since = time.time()  # launch; a wake resets it
        self.load_cache = (0, LOAD_SECONDS)
        self.excursions = excursions.ExcursionData(optimizer)

    def clock_tick(self, now):
        """Every control tick, any mode: a long gap means the Mac slept."""
        if self.tick_at is not None and not 0 <= now - self.tick_at <= WAKE_GAP_SECONDS:
            self.observed_since = now
            self.dark_since = None
        self.tick_at = now

    def active(self, state=None):
        return active(self.o.state if state is None else state)

    # ---- decisions (pure; GET previews call this too) ----

    def home(self, settings, live, current, now, rows=None):
        o = self.o
        account, device = live.get('account', ''), live.get('device', '')
        rows = o.candidates({}, {}, live, settings) if rows is None else rows
        models = sorted({r['id'] for r in rows} | ({current} if current else set()))
        earned, _ = o.demand_auto.evidence(account, device, models, now)
        selected = set(settings.get('models') or [])
        allowed = {
            r['id'] for r in rows if r.get('available') and (not selected or r['id'] in selected)
        }
        return choose_home(
            settings.get('manager') or {},
            earned,
            lambda: o.demand_auto.runs(account, device, now, 20),
            current,
            allowed,
            now,
            settings.get('lastSwitchAt', 0),
        )

    def decide(self, decision, settings, live, raw, rows, now):
        account, device = live.get('account', ''), live.get('device', '')
        current = decision.get('currentModel')
        m = settings.get('manager') or {}
        home = self.home(settings, live, current, now, rows)
        context = {
            'current': current,
            'home': home,
            'rows': {r['id']: r for r in rows},
            'rules': decision.get('policy') or {},
            'blocked': {
                k: v for k, v in (m.get('blocked') or {}).items() if finite(v) and v > now
            },
            'account': account,
            'device': device,
            'watchdog': self.watchdog_view(settings, raw, now),
        }
        if home:
            try:
                context['excursions'] = self.excursions.context(
                    settings, live, raw, rows, home, current, now
                )
            except Exception:
                log.exception('Excursion evidence failed; holding home')
        return decide(decision, settings, context, now)

    def watchdog_view(self, settings, raw, now):
        wd = (settings.get('manager') or {}).get('watchdog') or {}
        return {
            'darkSince': self.dark_start(settings, raw),
            'windowSeconds': self.window(raw) if raw else None,
            'attempts': wd.get('attempts', 0),
            'nextAt': wd.get('nextAt'),
            'reason': self.dark_text,
        }

    def summary(self, settings, raw, now):
        """The control poll's compact manager status; None under the legacy strategy."""
        if not enabled(settings.get('demandPolicy')):
            return None
        with self.o.lock:
            decision = copy.deepcopy(self.o.last_demand_decision)
        watchdog = None
        if self.dark_since is not None and active(settings):
            watchdog = {'darkSince': self.dark_start(settings, raw), 'reason': self.dark_text}
        return control_summary(settings, decision, watchdog, now)

    def release_pin(self, now):
        """Unpin (the control endpoint's 'release-pin'): the manager chooses home again."""
        o = self.o
        with o.lock:
            model = unpin(o.state, now)
            o.save()
            o.proposal = None
            o.status = 'optimizing'
            o.detail = (
                'Your pick %s is released. Bloomkeeper chooses the home model again and returns '
                'to it right away if another model is serving.' % model
            )
            detail = o.detail
            account, device = o.state.get('account', ''), o.state.get('device', '')
        o.store.event(account, device, now, 'manager', model, detail)
        return model

    def remember(self, decision, now):
        """Background: persist what the pure decision used or decided (home, arming, the end
        of an excursion), then book finished excursions. The ledger's kill switch never
        lifts by itself (excursions.policy_saved lifts it)."""
        view = decision.get('manager') or {}
        home = view.get('home')
        o = self.o
        detail = None
        with o.lock:
            if not self.active():
                return
            m = o.state.setdefault('manager', {})
            changed = False
            arming = view.get('arming')
            arming = (
                {k: arming[k] for k in ('model', 'since', 'checks', 'lastCheckAt') if k in arming}
                if arming
                else None
            )
            if arming != m.get('arming'):
                if arming:
                    m['arming'] = arming
                else:
                    m.pop('arming')
                changed = True
            end = view.get('excursionEnd') or {}
            excursion = m.get('excursion') or {}
            if (
                end.get('reason')
                and excursion.get('target') == decision.get('currentModel')
                and not excursion.get('endReason')
            ):
                end_excursion(o.state, end['reason'], now, end.get('code'))
                changed = True
            saved = m.get('home') or {}
            if (
                home
                and not pinned(saved)
                and (saved.get('model'), saved.get('source')) != (home['model'], home['source'])
            ):
                m['home'] = {
                    k: home[k]
                    for k in ('model', 'source', 'at', 'usdPerHour', 'hours')
                    if k in home
                }
                changed = True
                detail = 'Home model is now %s (%s).' % (
                    home['model'],
                    'best realized pay per ready hour on this Mac'
                    if home['source'] == 'history'
                    else 'the model serving when the manager started',
                )
            if changed:
                o.save()
            account, device = o.state.get('account', ''), o.state.get('device', '')
        if detail:
            o.store.event(account, device, now, 'manager', home['model'], detail)
        self.book(now)

    def book(self, now):
        """Background: write finished excursions to the ledger once their credits settled,
        and pause excursions when they lost money over 14 days (the kill switch)."""
        o = self.o
        with o.lock:
            m = o.state.get('manager') or {}
            due = [
                copy.deepcopy(r)
                for r in m.get('unbooked') or []
                if finite(r.get('endedAt')) and now - r['endedAt'] >= excursions.BOOK_DELAY_SECONDS
            ]
            account, device = o.state.get('account', ''), o.state.get('device', '')
        if not due:
            return
        ledger = self.excursions.ledger
        notes, notices = [], []
        for entry in due:
            try:
                record = excursions.book(o, ledger, entry, account, device)
            except Exception:
                log.exception('Could not book an excursion')
                record = None
            if record:
                notes.append((record['model'], excursions.booked_text(record)))
        with o.lock:
            m = o.state.setdefault('manager', {})
            done = {(r.get('target'), r.get('startedAt')) for r in due}
            left = [
                r for r in m.get('unbooked') or [] if (r.get('target'), r.get('startedAt')) not in done
            ]
            if left:
                m['unbooked'] = left
            else:
                m.pop('unbooked', None)
            if not excursions.paused(m, now):
                try:
                    off = excursions.kill(ledger, account, device, m, now)
                except Exception:
                    log.exception('Could not check the excursion ledger')
                    off = None
                if off:
                    m['excursionsOff'] = off
                    notices.append(off['reason'])
            o.save()
        self.excursions.invalidate()
        for model, text in notes:
            o.store.event(account, device, now, 'manager', model, text)
        for text in notices:
            o.store.event(account, device, now, 'manager-notice', None, text)

    # ---- background tick: readiness, recovery, watchdog ----

    def dark_start(self, settings, raw):
        if self.dark_since is None:
            return None
        start = self.dark_since
        session = raw.get('started_at')
        if served(raw) == 0 and finite(session):
            # This session never served: it has been dark since it started or last
            # counted a ready minute, even if Bloomkeeper only noticed now (e.g. On).
            start = min(start, max(session, self.last_ready(settings)))
        # Our own switch or restore owns its time; the watchdog counts from its end.
        ended = (settings.get('lastSwitchResult') or {}).get('at')
        return max(start, ended) if finite(ended) else start

    def last_ready(self, settings):
        h = self.o.store.h
        with h.lock:
            row = h.db.execute(
                'SELECT MAX(at) FROM opt_ready_minutes WHERE account=? AND device=?',
                (settings.get('account', ''), settings.get('device', '')),
            ).fetchone()
        return row[0] + 60 if row and finite(row[0]) else 0

    def what(self, settings, raw, current, start):
        """The concrete not-ready reason the status shows."""
        from optimizer import drained_idle

        failure = settings.get('lastSwitchFailure') or {}
        session = raw.get('started_at')
        if (
            current
            and failure.get('model') == current
            and finite(failure.get('at'))
            and finite(session)
            and failure['at'] >= session
        ):
            why = self.failure_note(settings, current, failure['at'])
            return '%s never loaded after the switch at %s%s' % (
                current,
                clock(failure['at'] - (failure.get('elapsedSeconds') or 0)),
                ' (%s)' % why if why else '',
            )
        if drained_idle(raw):
            return 'Darkbloom has been drained and serving nothing since %s' % clock(start)
        if loaded(raw, time.time())[1]:
            return 'Darkbloom reported that %s failed to load' % current
        return '%s has not been ready since %s: nothing is loaded and no work has arrived' % (
            current or 'No model',
            clock(start),
        )

    def failure_note(self, settings, model, at):
        h = self.o.store.h
        with h.lock:
            row = h.db.execute(
                """SELECT detail FROM opt_events WHERE account=? AND device=? AND kind='failed'
                AND model=? AND at>=? ORDER BY at DESC LIMIT 1""",
                (settings.get('account', ''), settings.get('device', ''), model, at - 5),
            ).fetchone()
        text = (row[0] or '').split('. ')[0].strip().rstrip('.') if row else ''
        return text[:1].lower() + text[1:160] if text else None

    def say(self, text, status='waiting'):
        with self.o.lock:
            self.o.status = status
            self.o.detail = text
            self.dark_text = text

    def window(self, raw, drained=False):
        """W = max(10 min, drain deadline + 3 x median load). `drained`: Bloomkeeper's own
        command already waited out the drain, so only the loads remain."""
        from optimizer import graceful_drain, DRAIN_SECONDS

        now = time.time()
        at, seconds = self.load_cache
        if not 0 <= now - at < 600:
            o = self.o
            with o.store.h.lock:
                rows = o.store.h.db.execute(
                    """SELECT downtime FROM opt_events WHERE account=? AND device=? AND kind='switched'
                    AND downtime>0 ORDER BY at DESC LIMIT 10""",
                    (o.state.get('account', ''), o.state.get('device', '')),
                ).fetchall()
            values = [r[0] for r in rows if finite(r[0])]
            seconds = statistics.median(values) if values else LOAD_SECONDS
            self.load_cache = (now, seconds)
        drain = DRAIN_SECONDS if graceful_drain(raw) and not drained else 0
        return max(WATCHDOG_MIN_SECONDS, drain + 3 * seconds)

    def tick(self, now, settings, live, raw, current, options, environment):
        """Returns True when the manager owns this tick (no model ready, or it acted)."""
        m = settings.get('manager') or {}
        ready, load_failed = loaded(raw, now)
        if ready and not load_failed:
            self.dark_since = self.dark_text = self.offline_since = None
            self.settle(now, m, raw, current)
            return False
        from optimizer import drained_idle

        # Darkbloom's idle timeout unloaded a model that loaded and served in this session.
        # Advertised but cold with nothing served (0.9.10 loads on demand) is dark.
        resting = bool(
            served(raw)
            and not m.get('recovery')
            and not load_failed
            and not drained_idle(raw)
            and not provider_busy(raw)
            and (raw.get('trust') or {}).get('status') == 'online'
            and finite(raw.get('written_at'))
            and -5 < now - raw['written_at'] < 15
        )
        if not live.get('provider', {}).get('online'):
            return self.stopped(now, settings, live, raw, current, m)
        self.offline_since = None
        excursion = bool(current) and (m.get('excursion') or {}).get('target') == current
        if resting and excursion:
            # An idle-unloaded excursion target earns nothing there: go home, don't reload it.
            self.dark_since = self.dark_text = None
            with self.o.lock:
                if self.active():
                    end_excursion(
                        self.o.state, 'Darkbloom unloaded it after it sat idle', now, 'idle'
                    )
            return False
        if resting and not self.cannot_reload(current, options):
            self.dark_since = self.dark_text = None
            return self.reload_resting(now, settings, live, raw, current, options, m)
        if self.dark_since is None:
            self.dark_since = now
        if m.get('recovery'):
            return self.recover(now, settings, live, raw, current, m)
        return self.watch(now, settings, live, raw, current, m)

    def cannot_reload(self, current, options):
        """A resting selection the idle reload can never load (no usable local endpoint) and
        that the user did not choose to rest: it counts as dark, so the watchdog restores it
        (a restart) instead of it resting unpaid."""
        from provider_control import endpoint_issue

        return bool(
            endpoint_issue(options or []) and not idle_unload_chosen(self.o.home, options or [])
        )

    def reload_resting(self, now, settings, live, raw, current, options, m):
        """Darkbloom's idle timeout unloaded a model (or pair) that served in this session. An
        unloaded model earns no base reward, so load it again through the local endpoint right
        away, unless the user chose idle unloading, it was reloaded within IDLE_RELOAD_SECONDS,
        or a warm-up could not run now (memory, fresh readings). Returns True when a reload
        started; otherwise the normal decision keeps the tick (it may go home)."""
        last = m.get('idleReload') or {}
        if (
            hold_error(current)
            or idle_unload_chosen(self.o.home, options)
            or (
                last.get('model') == current
                and finite(last.get('at'))
                and 0 <= now - last['at'] < IDLE_RELOAD_SECONDS
            )
            or self.o.prewarm_reason(raw, now, allow_cache_recovery=True, move='restore')
        ):
            return False
        return self.dispatch(
            now,
            current,
            'idle',
            settings,
            live,
            raw,
            current,
            'Darkbloom unloaded %s after it sat idle' % current,
        )

    def reloaded(self, target, account, device, command_at, end, outcome):
        """An idle reload ended. Not a switch or a restore: no switch result, no notice and no
        change to the last switch time (the excursion dwell and minimum run count from it)."""
        attempted, success, _, _, detail = outcome
        o = self.o
        with o.lock:
            o.state.pop('pending', None)
            o.previous = None
            o.idle_since = None
            m = o.state.setdefault('manager', {})
            m['lastAction'] = {'action': 'idle', 'model': target, 'at': end, 'success': success}
            if attempted:
                # The hourly limit counts from the attempt, loaded or not.
                m['idleReload'] = {'model': target, 'at': command_at}
                text = (
                    'Loaded %s again after Darkbloom unloaded it while idle, so it keeps earning base rewards.'
                    % target
                    if success
                    else 'Could not load %s again after Darkbloom unloaded it while idle. Bloomkeeper tries again after %s.'
                    % (target, clock(command_at + IDLE_RELOAD_SECONDS))
                )
            else:
                m['restoreAt'] = end + RESTORE_RETRY_SECONDS
                m['restoreBlocker'] = detail
                text = 'Loading %s again is waiting: %s' % (target, detail)
            o.status = 'optimizing' if success else 'waiting'
            o.detail = text
            self.dark_text = None
            o.save()
        if attempted:
            o.store.event(account, device, end, 'manager', target, text)

    def commanded(self, target, now):
        self.last_command = {'at': now, 'target': target}

    def stopped(self, now, settings, live, raw, current, m):
        """The provider is not running. Start it again only if Bloomkeeper's own command left it
        stopped moments ago; a stop by the user (or before this app run) is always respected."""
        command = self.last_command or {}
        self.dark_since = None
        if not finite(command.get('at')) or not 0 <= now - command['at'] <= STOPPED_WINDOW_SECONDS:
            self.dark_text = None
            return False
        if command.get('restarted'):
            self.say(
                'Darkbloom stopped again after Bloomkeeper started %s. Start it in Optimizer → Overview or run `darkbloom start`.'
                % command.get('target')
            )
            return True
        if self.offline_since is None:
            self.offline_since = now
        home = self.home(settings, live, current, now) or {}
        good = (m.get('lastGood') or {}).get('model')
        target = home.get('model') or good or command.get('target')
        what = 'Darkbloom stopped after Bloomkeeper’s switch to %s at %s' % (
            command.get('target'),
            clock(command['at']),
        )
        if now - self.offline_since < STOPPED_GRACE_SECONDS:
            self.say('%s. Starting %s at %s unless it comes back.' % (
                what, target, clock(self.offline_since + STOPPED_GRACE_SECONDS)
            ))
            return True
        if self.o.service_disabled() is not False:
            self.say('%s, and its launch agent is disabled. Start Darkbloom on the Mac.' % what)
            return True
        return self.dispatch(now, target, 'stopped', settings, live, raw, current, what)

    def settle(self, now, m, raw, current):
        o = self.o
        notes = []
        with o.lock:
            if not self.active():
                return
            state = o.state.setdefault('manager', {})
            changed = False
            good = state.get('lastGood') or {}
            if not hold_error(current) and (
                good.get('model') != current or not 0 <= now - good.get('at', 0) < 600
            ):
                state['lastGood'] = {'model': current, 'at': now}
                changed = True
            recovery = state.pop('recovery', None)
            if recovery:
                changed = True
                notes.append(
                    '%s became ready after a delayed load. Automatic control continues.' % current
                    if current == recovery.get('failedTarget')
                    else '%s is ready again. Automatic control continues.' % current
                )
            if state.get('watchdog'):
                state.pop('watchdog')
                changed = True
            home = state.get('home') or {}
            if pinned(home) and home.get('model') == current and (
                home.get('failures') or home.get('failedAt')
            ):
                home.update(failures=0)  # the pick is serving: its earlier failures are moot
                home.pop('failedAt', None)
                changed = True
            hold = state.get('homeRetry') or {}
            if hold and hold.get('model') == current:
                state.pop('homeRetry')
                changed = True
            counts = state.get('blockCounts') or {}
            if current in counts:  # it loaded: a later failure is blocked 24 h again
                counts.pop(current)
                if not counts:
                    state.pop('blockCounts')
                changed = True
            excursion = state.get('excursion')
            if excursion and excursion.get('target') != current:
                finish_excursion(state, now, 'the serving model changed', 'changed')
                changed = True
            if changed:
                o.save()
            account, device = o.state.get('account', ''), o.state.get('device', '')
        for note in notes:
            o.store.event(account, device, now, 'manager', current, note)

    def recover(self, now, settings, live, raw, current, m):
        """After a failed automatic switch: wait briefly for the target, then restore."""
        o = self.o
        rec = m['recovery']
        age = now - rec.get('at', now)
        if rec.get('attempts', 0) >= RESTORE_ATTEMPTS:
            self.give_up(now, rec)
            return True
        busy = provider_busy(raw)
        what = self.what(settings, raw, current, rec.get('at', now))
        grace = self.grace(rec.get('failedTarget'))
        if age < grace or busy == 'draining' or (busy and age < self.window(raw)):
            self.say(
                '%s. Checking until %s whether it loads, then restoring %s.'
                % (what, clock(rec.get('at', now) + grace), rec.get('previous'))
            )
            return True
        home = self.home(settings, live, current, now) or {}
        candidates = [
            rec.get('previous'),
            home.get('model'),
            (m.get('lastGood') or {}).get('model'),
            current,
        ]
        target = next(
            (
                c
                for c in candidates
                if c
                and not hold_error(c)
                and (c != rec.get('failedTarget') or c == rec.get('previous'))
                and not blocked(m, c, now)
            ),
            None,
        )
        return self.dispatch(now, target, 'recovery', settings, live, raw, current, what)

    def watch(self, now, settings, live, raw, current, m):
        o = self.o
        wd = m.get('watchdog') or {}
        start = self.dark_start(settings, raw)
        elapsed = now - start
        ended = (settings.get('lastSwitchResult') or {}).get('at')
        window = self.window(raw, drained=finite(ended) and start == ended)
        busy = provider_busy(raw)
        home = self.home(settings, live, current, now) or {}
        good = (m.get('lastGood') or {}).get('model')
        target = home.get('model') or good or current
        if (
            wd.get('attempts', 0) >= 1
            or home.get('failedAt')
            or blocked(m, target, now)
            or retry_hold(m, target, now)
        ):
            target = good if good and not blocked(m, good, now) else target
        # The first restore waits the full window; its one retry follows the failed model's grace.
        failed = (settings.get('lastSwitchFailure') or {}).get('model') or target
        due = max(
            ended + self.grace(failed)
            if wd.get('attempts', 0) >= 1 and finite(ended)
            else start + window,
            self.observed_since + OBSERVE_SECONDS,
        )
        what = self.what(settings, raw, current, start)
        if busy == 'draining' or (busy and elapsed < 2 * window):
            self.say(
                '%s. Darkbloom is %s; Bloomkeeper restores %s if nothing loads after it.'
                % (what, busy, target or 'the home model')
            )
            return True
        if now < due:
            self.say(
                '%s. If it is still not ready at %s, Bloomkeeper restores %s.'
                % (what, clock(due), target or 'the home model')
            )
            return True
        if (
            finite(wd.get('nextAt'))
            and now < wd['nextAt']
            and (wd.get('early') or not self.changed(wd, settings, live, raw, target))
        ):
            self.say(
                '%s, and restoring did not work. Bloomkeeper tries again at %s; check Darkbloom on the Mac.'
                % (what, clock(wd['nextAt']))
            )
            return True
        return self.dispatch(now, target, 'watchdog', settings, live, raw, current, what)

    def dispatch(self, now, target, purpose, settings, live, raw, current, what):
        """Start one restore in the worker slot, after cheap in-tick checks."""
        o = self.o
        m = settings.get('manager') or {}
        reason = None
        stopped = purpose == 'stopped'
        if stopped:
            # Readings other than the provider's own must be fresh; it is known to be stopped.
            live = {**live, 'provider': {**live.get('provider', {}), 'online': True, 'memoryGB': 0}}
        if not target:
            reason = 'No known-good model to restore.'
        elif finite(m.get('restoreAt')) and now < m['restoreAt']:
            reason = m.get('restoreBlocker') or 'Waiting before retrying the restore.'
        elif not stopped and raw.get('inference_active') is not False:
            reason = 'Work is in flight; the restore waits so no accepted request is cancelled.'
        elif not stopped and provider_busy(raw) == 'draining':
            reason = 'Darkbloom is draining accepted work; the restore waits for it.'
        else:
            # A restore is not a voluntary move: battery power, the 95 °C line and (for a
            # model served here before) catalog age don't hold it.
            reason = o.environment_reason(live, now, manual=True, move='restore', target=target)
            if not reason:
                budget = o.selection_budget(target, live, raw)
                credit = o.purge_credit(target, live, now) if budget and target != current else 0
                if not budget:
                    reason = '%s is not available to load right now.' % target
                elif budget['afterUnloadGB'] + credit < budget['requiredGB']:
                    reason = 'Not enough free memory to restore %s yet.' % target
        if reason and purpose == 'idle':
            return False  # not a dark Mac: the normal decision keeps the tick
        if reason:
            self.say('%s. %s' % (what, 'Restoring %s: %s' % (target, reason) if target else reason))
            return True
        # A cold but correct selection is force-loaded first; a restart is the retry, and
        # also follows a reload that can't work (no local endpoint) or kept being deferred.
        attempts = (m.get('recovery') if purpose == 'recovery' else m.get('watchdog')) or {}
        reload = purpose == 'idle' or bool(
            not stopped
            and target == current
            and not attempts.get('attempts')
            and attempts.get('deferred', 0) < RELOAD_DEFERRALS
            and not self.endpoint_missing()
        )
        with o.lock:
            if (
                o.update_guard.active()
                or not self.active()
                or o.state.get('pending')
                or o.state.get('requestedModel')
                or (o.worker and o.worker.is_alive())
                or (o.warmup_worker and o.warmup_worker.is_alive())
                or o.command_lock.locked()
            ):
                return True
            o.state['pending'] = {
                'model': target,
                'previous': current,
                'at': now,
                'kind': 'manager-restore',
                'purpose': purpose,
                'reload': reload,
                'session': session_key(raw),
            }
            o.save()
            o.status = 'switching'
            o.detail = '%s. %s %s and verifying it is warm.' % (
                what,
                'Loading' if reload else 'Starting' if stopped else 'Restoring',
                target,
            )
            self.dark_text = None if purpose == 'idle' else o.detail
            if stopped:
                self.last_command = {**(self.last_command or {}), 'restarted': True}
            o.worker = threading.Thread(
                target=self.restore,
                args=(
                    target,
                    purpose,
                    live['account'],
                    live['device'],
                    self.dark_start(settings, raw) or now,
                ),
                daemon=False,
            )
            o.worker.start()
        return True

    def endpoint_missing(self):
        from provider_control import endpoint_issue

        try:
            return bool(endpoint_issue(self.o.read_options()[1]))
        except Exception:
            return False  # the restore reads the settings again and waits

    def restore(self, target, purpose, account, device, dark_at):
        """Worker: force-load `target` (restart only if needed, never --force) and verify warm."""
        from optimizer import ExternalChange, NothingSent, launch_signature
        from prewarm import WarmupDeferred, WarmupError

        o = self.o
        start = time.time()
        success = attempted = False
        stage, code, detail, command_at = 'preflight', 'unknown', '', None
        with o.command_lock:
            try:
                selection, options, environment = o.read_options()
                raw = o.read_state()
                with o.lock:
                    pending = copy.deepcopy(o.state.get('pending')) or {}
                stopped = purpose == 'stopped'
                # A provider loading its startup models (every 30 s writes) is running.
                running = matching_process(process_identity(raw)) is True and state_fresh(
                    raw, time.time()
                )
                if (
                    o.stop.is_set()
                    or not self.active()
                    or pending.get('kind') != 'manager-restore'
                    or pending.get('model') != target
                    or session_key(raw) != pending.get('session')
                    or (raw and device_id(raw) != device)
                    or o.service_disabled() is not False
                    or running != (not stopped)
                ):
                    raise ExternalChange('The provider stopped or changed before the restore.')
                if not stopped and (
                    raw.get('inference_active') is not False or provider_busy(raw) == 'draining'
                ):
                    raise Blocked('Work arrived before the restore; no request was cancelled.')
                idle = purpose == 'idle'
                if idle and not (target == selection and same_selection(raw, target)):
                    raise Blocked('The selection changed; there is nothing to reload.')
                if pending.get('reload') and target == selection and same_selection(raw, target):
                    # The right model is selected but cold: load it through the local endpoint.
                    if not idle:
                        o.store.event(
                            account,
                            device,
                            start,
                            'manager',
                            target,
                            'Loading %s through the local endpoint; it was selected but not loaded.'
                            % target,
                        )
                    attempted, stage, command_at = True, 'verify', time.time()
                    try:
                        success = bool(
                            o.perform_prewarm(
                                target, raw, options, deadline=time.time() + 240, move='restore'
                            )
                        )
                    except (WarmupDeferred, NothingSent) as error:
                        # Nothing was sent (a recheck just before the local request, or a
                        # launchctl or settings read that failed): a retry after
                        # RESTORE_RETRY_SECONDS, not a failed reload or restore.
                        attempted, code = False, 'reload-deferred'
                        detail = str(error) or 'The provider changed before the reload.'
                    except WarmupError as error:
                        code = getattr(error, 'code', None) or 'warmup-failed'
                    return
                if target != selection and o.purge_before_load(target):
                    purged = o.read_state()
                    if session_key(purged) != session_key(raw):
                        raise ExternalChange('The provider changed during file-cache cleanup.')
                    raw = purged
                live = o.live or {}
                if stopped:
                    live = {
                        **live,
                        'provider': {**live.get('provider', {}), 'online': True, 'memoryGB': 0},
                    }
                budget = o.selection_budget(target, live, raw)
                if (
                    not budget
                    or budget['afterUnloadGB'] < budget['requiredGB']
                    or o.environment_reason(
                        live, time.time(), manual=True, move='restore', target=target
                    )
                ):
                    raise Blocked('Memory, heat or fresh readings changed before the restore.')
                o.store.event(
                    account,
                    device,
                    start,
                    'switching',
                    target,
                    'Restoring %s (%s). No accepted work is cancelled; --force is never used.'
                    % (
                        target,
                        {'watchdog': 'readiness watchdog', 'stopped': 'provider stopped'}.get(
                            purpose, 'failed switch'
                        ),
                    ),
                )
                attempted, stage, command_at = True, 'start', time.time()
                if not stopped:
                    self.commanded(target, command_at)
                o.command(target, options, environment)
                stage = 'verify'
                expected = launch_signature(options, environment)
                success = o.verify_started(
                    target,
                    raw.get('started_at'),
                    360,
                    expected,
                    MEMORY_WAIT_SECONDS,
                    move='restore',
                )
                if not success and o.target_serving_after_wait(
                    target, raw.get('started_at'), device, expected
                ):
                    success = True
                if not success:
                    failure = getattr(o, 'verification_failure', None)
                    code = getattr(failure, 'code', None) or 'readiness-timeout'
            except Blocked as error:
                detail = str(error)
            except ExternalChange as error:
                detail = str(error) or 'The provider changed during the restore.'
            except subprocess.TimeoutExpired:
                code = 'startup-timeout' if stage == 'start' else code
            except Exception:
                code = 'startup-command' if stage == 'start' else 'unknown'
            finally:
                self.restored(
                    target,
                    purpose,
                    account,
                    device,
                    (command_at, dark_at),
                    (attempted, success, stage, code, detail),
                )

    def restored(self, target, purpose, account, device, times, outcome):
        command_at, dark_at = times
        attempted, success, stage, code, detail = outcome
        o = self.o
        end = time.time()
        duration = max(0, end - command_at) if command_at else 0
        if purpose == 'idle':
            return self.reloaded(target, account, device, command_at, end, outcome)
        try:
            selection = o.read_options()[0]
        except Exception:
            selection = None
        notices = []
        with o.lock:
            o.state.pop('pending', None)
            o.previous = None
            o.idle_since = None
            m = o.state.setdefault('manager', {})
            home = m.get('home') or {}
            m['lastAction'] = {'action': purpose, 'model': target, 'at': end, 'success': success}
            if not attempted:
                # e.g. launch settings that cannot be read (the watchdog still runs, _tick)
                detail = (
                    detail or 'Darkbloom’s settings or status could not be read; nothing was sent.'
                )
                m['restoreAt'] = end + RESTORE_RETRY_SECONDS
                m['restoreBlocker'] = detail
                text = 'Restoring %s is waiting: %s' % (target, detail)
                if code == 'reload-deferred':
                    # After RELOAD_DEFERRALS of these, the next try is a restart (dispatch).
                    key = 'recovery' if purpose == 'recovery' else 'watchdog'
                    if key == 'watchdog' or m.get(key):
                        tries = m[key] = m.get(key) or {}
                        tries['deferred'] = tries.get('deferred', 0) + 1
            else:
                m.pop('restoreAt', None)
                m.pop('restoreBlocker', None)
                o.state['lastSwitchResult'] = {
                    'at': end,
                    'outcome': 'recovered' if success else 'failed',
                }
            if attempted and success:
                o.state['expectedModel'] = target
                o.state['lastSwitchAt'] = end
                o.state.pop('rollbackModel', None)
                failed = (m.pop('recovery', None) or {}).get('failedTarget')
                m.pop('watchdog', None)
                if pinned(home) and home.get('model') != target:
                    home['failedAt'] = end
                text = (
                    'Restored %s after the switch to %s failed. Automatic control continues.'
                    % (target, failed)
                    if purpose == 'recovery'
                    else 'Started %s again: Darkbloom had stopped after a switch. Automatic control continues.'
                    % target
                    if purpose == 'stopped'
                    else 'Watchdog restored %s after %d min with no ready model. Automatic control continues.'
                    % (target, max(0, end - dark_at) // 60 + 1)
                )
                if pinned(home) and home.get('model') != target:
                    text += (
                        ' Your pick %s could not be restored; pick it again to retry.'
                        % home['model']
                    )
                notices.append(text)
            elif attempted:
                o.state['lastSwitchFailure'] = {
                    'model': target,
                    'stage': stage,
                    'code': code,
                    'recovery': 'failed',
                    'recoveryCode': 'restore-not-ready',
                    'at': end,
                    'elapsedSeconds': duration,
                }
                if selection:
                    o.state['expectedModel'] = selection
                if target == home.get('model'):
                    self.hold_target(m, target, end)  # home: retry hold; pin: counts a failure
                text = 'Restoring %s did not verify.' % target
                if purpose == 'recovery':
                    rec = m.get('recovery') or {}
                    rec['attempts'] = rec.get('attempts', 0) + 1
                    rec['at'] = end
                    m['recovery'] = rec
                else:
                    wd = m.get('watchdog') or {}
                    if finite(wd.get('nextAt')) and command_at < wd['nextAt']:
                        # A state change ended the back-off early (changed): that one retry
                        # failed, so the back-off holds until its time with no further early
                        # retry (Darkbloom relaunching a crashing provider is a new session
                        # every time).
                        wd['early'] = True
                    else:
                        wd.pop('nextAt', None)  # this restore took the back-off's turn
                        wd['attempts'] = wd.get('attempts', 0) + 1
                    if wd.get('attempts', 0) >= RESTORE_ATTEMPTS:
                        back_off(wd, end, o.live)
                        notices.append(
                            'No model is ready and two restores did not work. Bloomkeeper tries again at %s; check Darkbloom on the Mac (darkbloom doctor).'
                            % clock(wd['nextAt'])
                        )
                    m['watchdog'] = wd
            o.status = 'optimizing' if success else 'waiting'
            o.detail = text
            self.dark_text = None if success else text
            o.next_identity = 0
            o.save()
        if attempted:
            kind = 'recovered' if success else 'failed'
            o.store.event(account, device, end, kind, target, text, duration)
        for note in notices:
            o.store.event(account, device, end, 'manager-notice', target, note)

    def give_up(self, now, rec):
        """Two failed restores after a failed switch: the watchdog's back-off takes over and the
        user is told (notice and phone push). Automatic control stays on, so a dark Mac is
        still restored later."""
        o = self.o
        with o.lock:
            if not self.active():
                return
            m = o.state.setdefault('manager', {})
            m.pop('recovery', None)
            wd = m['watchdog'] = back_off(m.get('watchdog') or {}, now, o.live)
            detail = (
                'Restoring after the failed switch to %s did not work twice. Automatic control stays on. Bloomkeeper tries again at %s; check Darkbloom on the Mac (darkbloom doctor).'
                % (rec.get('failedTarget'), clock(wd['nextAt']))
            )
            o.status = 'waiting'
            o.detail = self.dark_text = detail
            o.save()
            account, device = o.state.get('account', ''), o.state.get('device', '')
        o.store.event(account, device, now, 'manager-notice', rec.get('failedTarget'), detail)

    def changed(self, wd, settings, live, raw, target):
        """A watchdog back-off ends early when Darkbloom started a new session after it began (a
        restart by the user or by Darkbloom), or when free memory grew by at least `target`'s own
        memory estimate (room for the whole model) since then."""
        started = raw.get('started_at')
        if finite(started) and finite(wd.get('since')) and started > wd['since']:
            return True
        free = (live.get('hardware') or {}).get('memoryAvailableGB')
        if not (finite(free) and finite(wd.get('freeGB'))):
            return False
        rows = self.o.candidates({}, {}, live, settings)
        size = next((r.get('memoryGB') for r in rows if r.get('id') == target), None)
        return finite(size) and size > 0 and free - wd['freeGB'] >= size

    def grace(self, model):
        """Seconds a failed `model` gets to come up before a restore: this Mac's p90 load time
        (start command returned -> verified ready, so without a >= 0.9.9 drain, which the
        failed switch already waited out) over its last LOAD_SAMPLES successful switches to it
        in 30 days (load_timed), once there are GRACE_MIN_SAMPLES, never under
        RESTORE_GRACE_SECONDS."""
        now = time.time()
        with self.o.lock:
            loads = (self.o.state.get('manager') or {}).get('loadSeconds') or {}
            rows = copy.deepcopy(loads.get(model) or []) if isinstance(loads, dict) else []
        values = sorted(
            r[1]
            for r in rows
            if isinstance(r, list)
            and len(r) == 2
            and finite(r[0])
            and 0 <= now - r[0] < HOME_WINDOW
            and finite(r[1])
            and r[1] > 0
        )
        seconds = RESTORE_GRACE_SECONDS
        if len(values) >= GRACE_MIN_SAMPLES:
            seconds = max(seconds, values[math.ceil(0.9 * len(values)) - 1])
        return seconds

    def load_timed(self, model, seconds, now):
        """A switch to `model` was verified ready `seconds` after its start command returned
        (caller holds o.lock). Kept per model, the last LOAD_SAMPLES in 30 days, for grace()."""
        if not model or not finite(seconds) or seconds <= 0:
            return
        m = self.o.state.setdefault('manager', {})
        loads = m.get('loadSeconds') if isinstance(m.get('loadSeconds'), dict) else {}
        rows = [
            r
            for r in loads.get(model) or []
            if isinstance(r, list)
            and len(r) == 2
            and finite(r[0])
            and 0 <= now - r[0] < HOME_WINDOW
        ]
        loads[model] = (rows + [[now, seconds]])[-LOAD_SAMPLES:]
        m['loadSeconds'] = loads

    # ---- hooks from the switch, manual and external-change paths (caller holds o.lock) ----

    def hold_target(self, m, model, now):
        """A failed auto-chosen target: blocked 24 h, 48 h, then 96 h while it keeps failing (a
        model that becomes ready starts over); home retries with a doubling hold; pins never."""
        home = m.get('home') or {}
        if model == home.get('model'):
            if pinned(home):
                home['failures'] = home.get('failures', 0) + 1
                if home['failures'] >= 2:
                    home['failedAt'] = now
                return
            failures = (m.get('homeRetry') or {}).get('failures', 0) + 1
            m['homeRetry'] = {
                'model': model,
                'failures': failures,
                'until': now + min(BLOCK_SECONDS, HOME_RETRY_SECONDS * 2 ** (failures - 1)),
            }
        else:
            counts = m.setdefault('blockCounts', {})
            counts[model] = counts.get(model, 0) + 1
            m.setdefault('blocked', {})[model] = now + min(
                BLOCK_MAX_SECONDS, BLOCK_SECONDS * 2 ** (counts[model] - 1)
            )
            m['blocked'] = {k: v for k, v in m['blocked'].items() if finite(v) and v > now}

    def not_taken(self, target):
        """After a command: why the provider is not serving `target` (for the status), or None."""
        o = self.o
        try:
            raw = o.read_state()
        except (OSError, ValueError):
            raw = {}
        # 0.9.10 loads its startup models before it registers, writing only every 30 s:
        # that is starting, not stopped.
        running = matching_process(process_identity(raw)) is True and state_fresh(raw, time.time())
        if not running:
            return 'Darkbloom stopped after the switch to %s' % target
        advertised = selection_key(raw.get('advertised_models'))
        if advertised == target:
            return None
        try:
            plist, options, _ = o.read_options(plist_only=True)
        except Exception:
            plist, options = None, []
        pinned_models = toml_selection(o.home, options)
        return (
            'Darkbloom restarted with %s instead of %s '
            '(provider.toml enabled_models: %s; launch agent: %s)'
            % (advertised or 'no model', target, pinned_models or 'not set', plist or 'unreadable')
        )

    def switch_failed(self, request_kind, previous, target, recovered, attempted, failure, now):
        """Replaces 'pause after a failed automatic switch'. Returns False for legacy paths."""
        o = self.o
        if request_kind != 'demand' or not self.active():
            return False
        m = o.state.setdefault('manager', {})
        if attempted and failure is not None and target != previous:
            self.hold_target(m, target, now)
        if recovered or failure is None or not attempted:
            m.pop('recovery', None)
            if not recovered:
                m['retryAt'] = now + RETRY_SECONDS
        else:
            m['recovery'] = {
                'failedTarget': target,
                'previous': previous,
                'at': now,
                'attempts': 1 if failure.get('recoveryCode') == 'restore-not-ready' else 0,
            }
            try:
                selection = o.read_options()[0]
            except Exception:
                selection = None
            if selection in (target, previous):
                o.state['expectedModel'] = selection
        m['lastAction'] = {'action': 'switch', 'model': target, 'at': now, 'success': False}
        if recovered:
            o.store.event(
                o.state.get('account', ''),
                o.state.get('device', ''),
                now,
                'manager-notice',
                previous,
                'Restored %s after the switch to %s failed. Automatic control continues; %s is held back.'
                % (previous, target, target),
            )
        return True

    def switch_deferred(self, request_kind, target, now):
        """A manager move deferred before its command (caller holds o.lock): nothing was sent,
        so it is retried after a short, growing wait, not on every tick (each try purges,
        lists models and fetches the catalog)."""
        if request_kind != 'demand' or not self.active():
            return
        m = self.o.state.setdefault('manager', {})
        last = m.get('deferred') or {}
        count = (
            last.get('count', 0) + 1
            if last.get('model') == target
            and finite(last.get('at'))
            and 0 <= now - last['at'] < 2 * RETRY_SECONDS
            else 1
        )
        m['deferred'] = {'model': target, 'count': count, 'at': now}
        m['retryAt'] = now + min(RETRY_SECONDS, DEFER_RETRY_SECONDS * 2 ** (count - 1))

    def switch_done(
        self, request_kind, request_id, pending, previous, target, success, decision, now
    ):
        o = self.o
        if request_kind.startswith('manual'):
            return self.manual_finished(
                request_id, target, (o.state.get('manualResult') or {}).get('status')
            )
        if request_kind != 'demand' or not success or not self.active():
            return False
        m = o.state.setdefault('manager', {})
        m.pop('retryAt', None)
        m.pop('deferred', None)
        m['lastAction'] = {
            'action': pending.get('demandKind'),
            'model': target,
            'at': now,
            'success': True,
        }
        proposal = ((decision or {}).get('manager') or {}).get('proposal') or {}
        if pending.get('demandKind') == 'excursion' and proposal.get('target') == target:
            finish_excursion(m, now, 'another excursion started', 'replaced')
            m.pop('arming', None)
            m['excursion'] = {
                'target': target,
                'from': previous,
                'startedAt': now,
                'leftAt': pending['at'] if finite(pending.get('at')) else now,
                'predictedUsdPerHour': proposal.get('predictedUsdPerHour'),
                'reason': proposal.get('reason'),
                'maxMinutes': proposal.get('maxMinutes')
                if finite(proposal.get('maxMinutes'))
                else EXCURSION_MAX_MINUTES,
                'evidence': proposal.get('evidence'),
            }
        elif target == (m.get('home') or {}).get('model'):
            end = ((decision or {}).get('manager') or {}).get('excursionEnd') or {}
            finish_excursion(m, now, end.get('reason') or 'returned home', end.get('code'))
            m.pop('homeRetry', None)
        return True

    def manual_queued(self, request_id, model, unchanged=False):
        """A manual pick while the manager runs becomes the pin; automatic control resumes after."""
        o = self.o
        if not self.active():
            return False
        m = o.state.setdefault('manager', {})
        if unchanged:
            m['home'] = pin(model, time.time())
            finish_excursion(m, time.time(), 'you picked a model', 'manual')
        else:
            m['resume'] = {'id': request_id, 'model': model}
        return True

    def manual_finished(self, request_id, model, status):
        return resume_manual(self.o.state, request_id, model, status)

    def external(self, now, settings, live, raw, current):
        """The serving model changed outside Bloomkeeper: keep it (a pin), never pause or revert.

        Returns True once handled. False: nothing to adopt yet (stopped, or a selection that
        disagrees with what the daemon advertises); the caller still runs the watchdog.
        """
        o = self.o
        online = live.get('provider', {}).get('online')
        if not online or not same_selection(raw, current):
            with o.lock:
                if not online and not o.state.get('pending'):
                    o.state['expectedModel'] = current  # nothing to adopt while stopped
                o.status = 'waiting'
                o.detail = (
                    'Darkbloom is not running.'
                    if not online
                    else 'Darkbloom advertises %s while its launch settings select %s. Holding until they agree; a dark Mac is still restored.'
                    % (selection_key(raw.get('advertised_models')) or 'no model', current)
                )
            return False
        m = settings.get('manager') or {}
        own = {
            (m.get('recovery') or {}).get('failedTarget'),
            (m.get('recovery') or {}).get('previous'),
            (m.get('home') or {}).get('model') if pinned(m.get('home')) else None,
        } - {None}
        with o.lock:
            if o.state.get('pending') or o.state.get('expectedModel') != settings.get(
                'expectedModel'
            ):
                return
            o.state['expectedModel'] = current
            o.state['lastSwitchAt'] = now
            o.state.pop('rollbackModel', None)
            if current in own or hold_error(current):
                detail = 'Now serving %s.' % current
            else:
                state = o.state.setdefault('manager', {})
                state['home'] = pin(current, now, 'external')
                for key in ('recovery', 'retryAt'):
                    state.pop(key, None)
                finish_excursion(state, now, 'the model was changed outside Bloomkeeper', 'external')
                detail = (
                    'The serving model changed outside Bloomkeeper to %s. Holding it as your pick; the manager will not switch away from it.'
                    % current
                )
            o.status = 'optimizing'
            o.detail = detail
            o.save()
        account, device = live.get('account', ''), live.get('device', '')
        o.store.event(account, device, now, 'manager', current, detail)
        return True

    def load_failed(self, now, settings, current):
        """A verified model failed to load again after reconnecting: recover, do not pause."""
        o = self.o
        with o.lock:
            m = o.state.setdefault('manager', {})
            previous = settings.get('rollbackModel')
            if current != previous:
                self.hold_target(m, current, now)
            m['recovery'] = {
                'failedTarget': current,
                'previous': previous,
                'at': now,
                'attempts': 0,
            }
            o.state.pop('rollbackModel', None)
            o.status = 'waiting'
            o.detail = (
                'Darkbloom reported that %s failed to load after reconnecting. Restoring %s unless it recovers.'
                % (current, previous)
            )
            o.save()
