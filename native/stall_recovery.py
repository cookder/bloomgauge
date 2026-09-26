"""Work that stops abruptly: detect it in minutes, then try to get it going again.

A steady flow of jobs that suddenly drops to zero is rarely a gradual demand
decline. It can be the network routing elsewhere, a stale coordinator session,
or the local engine. Bloomkeeper escalates one step at a time, waiting after each:

  1. probe    a tiny test request routed back to this Mac through Darkbloom
              (or to the local engine when no API key is stored); no downtime
  2. restart  restart the provider on the same model (a fresh session)
  3. escape   let the optimizer move to another model now instead of after
              the ordinary idle escape
  4. hold     stop trying and tell the user; nothing more is automatic

When the model's own network demand collapsed as well, the flow stopping is
explained by demand, so Bloomkeeper goes straight to the escape.

This module is pure: it reads observations and returns the next step.
"""

import math

BASELINE_SECONDS = 20 * 60
MIN_ACTIVE_MINUTES = 12  # of the 20 before the silence
# Calibrated on Sep 8-25, 2026 gaps after steady work with no switch in them.
# Below 5 jobs a minute, even 15-minute gaps ended on their own. At 10+ a
# minute, a 5-minute gap lasted 10+ more minutes 31% of the time and an
# 8-minute gap 71%. Gaps of 3 minutes are routine at every rate.
MIN_JOBS_PER_MINUTE = 5.0
SILENCE_SECONDS = 5 * 60  # first step
RESTART_SILENCE_SECONDS = 8 * 60
STEP_WAIT_SECONDS = {'probe': 3 * 60, 'restart': 5 * 60, 'escape': 5 * 60}
# The optimizer confirms an opportunity for 5 minutes before it switches, so an
# escape that hasn't led to a switch yet gets longer before Bloomkeeper gives up.
ESCAPE_UNMOVED_SECONDS = 20 * 60
DEMAND_HELD = 0.5  # recent load and pressure vs the baseline window
RESTARTS_PER_DAY = 3
RESTART_SPACING_SECONDS = 3600
EPISODE_LIMIT_SECONDS = 3 * 3600  # after this, a silence is ordinary quiet, not a stall
# A switch recorded shortly before a restart or escape step is that step's own
# switch: the step is stamped after the provider CLI and catalog checks, which
# took up to ~5 s on real switches (Sep 2026).
HANDOVER_MARGIN_SECONDS = 120


def finite(v):
    return type(v) in (int, float) and math.isfinite(v)


def demand(samples, start, end):
    rows = [
        s
        for s in samples
        if start <= s['at'] < end and all(finite(s.get(k)) for k in ('active', 'queued', 'warm'))
    ]
    if len(rows) < 3:
        return None
    load = sum(s['active'] + s['queued'] for s in rows) / len(rows)
    warm = sum(s['warm'] for s in rows) / len(rows)
    return {'load': load, 'pressure': load / max(1, warm), 'samples': len(rows)}


def assess(minutes, network, attempts, switches, session_start, now, probe_available=True):
    """Next recovery step for the current episode, or none.

    minutes: this Mac's warm minutes [{'at', 'model', 'seconds', 'jobs'}], any model.
    network: {model: [{'at', 'active', 'queued', 'warm'}]} network capacity samples.
    attempts: recovery steps already taken [{'at', 'step', 'model'}].
    switches: times Bloomkeeper started a model switch (any reason).
    """
    probes = {int(a['at'] // 60) * 60 for a in attempts if a.get('step') == 'probe'}
    # A probe's own one-token job is not network work.
    worked = [
        m
        for m in minutes
        if finite(m.get('jobs'))
        and m['jobs'] >= (2 if m['at'] in probes or m['at'] - 60 in probes else 1)
        and m['at'] + 60 <= now
    ]
    result = {
        'step': None,
        'status': 'ok',
        'reason': None,
        'silenceSeconds': None,
        'requiredSeconds': None,
        'baselineJobsPerMinute': None,
        'demandHeld': None,
        'model': None,
        'episodeStart': None,
        'taken': [],
    }
    if not worked:
        result.update(status='quiet', reason='No recent steady work to compare against.')
        return result
    last = max(m['at'] for m in worked) + 60
    model = next(m['model'] for m in worked if m['at'] + 60 == last)
    # Silence is observed warm time since the last job, not wall time: a Mac that
    # slept, or a Bloomkeeper that was closed, saw nothing and must not count it.
    silence = sum(
        max(0, m.get('seconds', 0)) for m in minutes if m['at'] >= last and m['at'] + 60 <= now
    )
    before = [
        m
        for m in minutes
        if m['model'] == model
        and last - BASELINE_SECONDS <= m['at'] < last
        and m.get('seconds', 0) >= 59
    ]
    jobs = sum(max(0, m['jobs']) for m in before if finite(m.get('jobs')))
    active = sum(1 for m in before if finite(m.get('jobs')) and m['jobs'] >= 1)
    rate = jobs / (BASELINE_SECONDS / 60)
    result.update(
        model=model, silenceSeconds=silence, baselineJobsPerMinute=rate, episodeStart=last
    )
    if active < MIN_ACTIVE_MINUTES or rate < MIN_JOBS_PER_MINUTE:
        result.update(
            status='quiet',
            reason='Work before this quiet period was not steady enough to call it a stall.',
        )
        return result
    result['requiredSeconds'] = SILENCE_SECONDS
    if silence < SILENCE_SECONDS:
        return result
    if now - last > EPISODE_LIMIT_SECONDS:
        result.update(
            status='quiet', reason='Work stopped hours ago; ordinary idle handling applies.'
        )
        return result
    base = demand(network.get(model, []), last - BASELINE_SECONDS, last)
    recent = demand(network.get(model, []), now - 180, now)
    # Three states: True/False only with demand readings on both sides. Missing
    # readings (a capacity outage, a model absent from the list) are unknown and
    # never count as a demand collapse.
    held = (
        None
        if not (base and recent and base['load'] > 0)
        else recent['load'] >= DEMAND_HELD * base['load']
        and recent['pressure'] >= DEMAND_HELD * base['pressure']
    )
    result.update(status='stalled', demandHeld=held, baselineDemand=base, recentDemand=recent)
    episode = sorted((a for a in attempts if a['at'] >= last), key=lambda a: a['at'])
    taken = [a['step'] for a in episode]
    latest = episode[-1] if episode else None
    result['taken'] = taken
    # A trial or manual switch owns its own no-traffic handling. Only switches
    # this episode asked for (restart, escape) keep the episode going.
    handover = min(
        (a['at'] for a in episode if a['step'] in ('restart', 'escape')), default=math.inf
    )
    if any(last <= t < handover - HANDOVER_MARGIN_SECONDS for t in switches):
        result.update(
            status='switching',
            reason='Bloomkeeper switched models after work stopped; that run handles its own quiet time.',
        )
        return result
    if 'hold' in taken:
        result.update(
            reason='Bloomkeeper stopped trying for this stall; waiting for work to resume.'
        )
        return result
    # Each step gets time to work. A restart or switch waits from the moment
    # the new session started, so loading time is not counted as silence.
    if latest:
        started = (
            max(latest['at'], session_start or 0)
            if latest['step'] in ('restart', 'escape')
            else latest['at']
        )
        wait = STEP_WAIT_SECONDS[latest['step']]
        if latest['step'] == 'escape' and not any(t >= latest['at'] for t in switches):
            wait = ESCAPE_UNMOVED_SECONDS
        if now - started < wait:
            result.update(
                reason='Waiting to see whether work resumes after the ' + latest['step'] + '.'
            )
            return result
    day = [a for a in attempts if a.get('step') == 'restart' and now - 86400 < a['at'] <= now]
    restart_allowed = len(day) < RESTARTS_PER_DAY and all(
        now - a['at'] >= RESTART_SPACING_SECONDS for a in day
    )
    if held is False and 'escape' not in taken:
        step, reason = (
            'escape',
            "Work stopped and this model's network demand fell as well. Moving on without waiting for the usual idle time.",
        )
    elif (
        'probe' not in taken
        and 'restart' not in taken
        and 'escape' not in taken
        and probe_available
    ):
        step, reason = (
            'probe',
            'Work stopped abruptly while network demand held. Sending a test request to nudge routing.',
        )
    elif 'restart' not in taken and 'escape' not in taken and restart_allowed:
        step, reason = (
            'restart',
            'Still no work while network demand held. Restarting the provider on the same model for a fresh session.',
        )
    elif 'escape' not in taken:
        step, reason = (
            'escape',
            'Still no work after '
            + (' and '.join(taken) if taken else 'waiting')
            + '. Letting the optimizer try another model now.',
        )
    else:
        # The escape only lets the optimizer move; say whether it actually did.
        escaped = max((a['at'] for a in attempts if a.get('step') == 'escape'), default=None)
        moved = escaped is not None and any(t >= escaped for t in switches)
        step, reason = (
            'hold',
            (
                'No work after a test request, a restart and '
                + ('a switch to another model' if moved else 'a chance to switch models')
                + '. Bloomkeeper has stopped trying; '
                'check Darkbloom (darkbloom doctor, Slack) for a routing or verification problem.'
                if 'restart' in taken
                else 'No work after '
                + ' and '.join(taken)
                + '. Bloomkeeper has stopped trying; check Darkbloom for a routing problem.'
            ),
        )
    if step == 'restart' and silence < RESTART_SILENCE_SECONDS:
        result.update(reason='Waiting for 8 minutes of silence before restarting.')
        return result
    result.update(step=step, reason=reason)
    return result
