"""Work that stops abruptly: detect it in minutes, then try to get it going again.

A steady flow of jobs that suddenly drops to zero is rarely a gradual demand
decline. It can be the network routing elsewhere, a stale coordinator session,
or the local engine. Bloomkeeper escalates one step at a time, waiting after each:

  1. probe    a tiny test request routed back to this Mac through Darkbloom
              (or to the local engine when no API key is stored); no downtime
  2. restart  restart the provider on the same model (a fresh session), only
              while the model's network demand is high
  3. escape   let the optimizer move to another model now instead of after
              the ordinary idle escape
  4. hold     stop trying and tell the user; nothing more is automatic

When the model's own network demand collapsed as well, the flow stopping is
explained by demand, so Bloomkeeper goes straight to the escape.

Two triggers start the ladder. The first is this Mac's own history: steady work
that suddenly stops. The second is Macs like this one (same chip and memory,
same model, dedicated or mixed; network_evidence.py): they are clearly getting
work in the public counters while this Mac, warm and trusted, gets none. It
catches stalls the first misses, at rates below its steady-work bar, and it
means demand for this hardware held even when the model's demand readings fell.

This module is pure: it reads observations and returns the next step.
"""

import math
import statistics
from model_combinations import members

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
# A restart also needs this much absolute pressure (active + queued per warm provider over the
# last 3 minutes). Below it (gap-3's low tercile, 0.10-0.33) a restart earned no more than
# waiting ($0.00270 vs $0.00268 per stall); PLAN §6.5 gate 1.
RESTART_MIN_PRESSURE = 0.33
RESTARTS_PER_DAY = 3
RESTART_SPACING_SECONDS = 3600
EPISODE_LIMIT_SECONDS = 3 * 3600  # after this, a silence is ordinary quiet, not a stall
# A switch recorded shortly before a restart or escape step is that step's own
# switch: the step is stamped after the provider CLI and catalog checks, which
# took up to ~5 s on real switches (Sep 2026).
HANDOVER_MARGIN_SECONDS = 120

# Peer trigger ("Macs like yours are getting work; this one isn't"). Measured on the public
# /v1/stats poll of Sep 27 09:14 - Sep 28 08:34 CDT (10-minute windows, ~1,150 providers,
# dedicated boxes, Andrew's Mac left out): once a provider got nothing while at least 5 same-cell
# peers on its model had a median of >= 60 requests an hour, the silence lasted another 10+
# minutes 47% of the time after 10 minutes (n=171), 66% after 20 (n=73) and 82% after 30 (n=44).
# At a >= 30 req/h bar the same figures were 39/62/82%, at >= 120 req/h 66/80/87%. So: a test
# request after 10 minutes (the own trigger probes at 31%) and a restart after 20 (it restarts at
# 71%). Andrew's own 23 h in that poll never had a 10-minute window without work; his longest
# gap was two 5-minute windows (Sep 28 01:30-01:40) that ended by itself, which at most
# earns a test request. Filtering on the peers' share of empty windows (<= 0.25 or <= 0.10)
# didn't sharpen these figures, so the median bar is the only one.
PEER_MIN_PEERS = 5  # network_evidence.MIN_PROVIDERS
PEER_MIN_REQ_H = 60.0  # peers' median: ~5 requests per 5-minute window, P(none) < 1% at that rate
PEER_SILENCE_SECONDS = 10 * 60  # two 5-minute windows: first step
PEER_RESTART_SILENCE_SECONDS = 20 * 60  # four windows
PEER_FRESH_SECONDS = 15 * 60  # newest window (they close every ~5 min, up to ~6 min late)
PEER_NOTE = "Macs like yours are getting work; this one isn't."
# A test request routed to this Mac reaches it up to its 90 s reply timeout after the step.
PROBE_LAG_SECONDS = 120


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


def peer_stall(windows, model, now, probes=()):
    """The latest unbroken run of this Mac's public-counter windows on `model` with no work,
    and what its same-level peers got meanwhile; None without such a run.

    windows: network_evidence.own_windows() [{'at', 'start', 'model', 'requests', 'trusted',
    'peers', 'peer_rates'}]; a window exists only while this Mac was live with `model` loaded
    in both snapshots. probes: times of this episode's test requests, whose own request is not
    network work. 'busy' is the trigger: enough peers, clearly getting work, still now."""
    names = set(members(model)) or {model}
    run = []
    for w in sorted(windows or (), key=lambda w: -w['at']):
        allowed = sum(1 for t in probes if w['start'] - PROBE_LAG_SECONDS <= t <= w['at'])
        if (
            w.get('model') not in names
            or not finite(w.get('requests'))
            or w['requests'] > allowed
            or w.get('trusted') is False  # 'hardware' trust; None: the field is missing
            or (run and abs(run[-1]['start'] - w['at']) > 1)  # a gap: the run ends there
        ):
            break
        run.append(w)
    if not run or not 0 <= now - run[0]['at'] <= PEER_FRESH_SECONDS:
        return None
    rates = [r for w in run for r in w.get('peer_rates') or ()]
    latest = run[0].get('peer_rates') or ()
    peers = int(statistics.median(w.get('peers') or 0 for w in run))  # a typical window's count
    median = statistics.median(rates) if rates else 0.0
    return {
        'since': run[-1]['start'],
        'seconds': run[0]['at'] - run[-1]['start'],
        'windows': len(run),
        'peers': peers,
        'peerReqPerHour': median,
        'peerZeroShare': sum(1 for r in rates if r == 0) / len(rates) if rates else None,
        'cell': run[0].get('cell'),
        'dedicated': run[0].get('dedicated'),
        'busy': bool(
            peers >= PEER_MIN_PEERS
            and median >= PEER_MIN_REQ_H
            and latest
            and statistics.median(latest) > 0
        ),
    }


def peer_sentence(peer, silence):
    """The peer trigger in plain words, for the stall card and the decision log."""
    return (
        PEER_NOTE
        + ' %d similar Macs got a median of %d requests an hour; none reached this one in %d min.'
        % (peer['peers'], round(peer['peerReqPerHour']), max(1, round(silence / 60)))
    )


def assess(
    minutes, network, attempts, switches, session_start, now, probe_available=True, peers=None
):
    """Next recovery step for the current episode, or none.

    minutes: this Mac's warm minutes [{'at', 'model', 'seconds', 'jobs'}], any model.
    network: {model: [{'at', 'active', 'queued', 'warm'}]} network capacity samples.
    attempts: recovery steps already taken [{'at', 'step', 'model'}].
    switches: times Bloomkeeper started a model switch (any reason).
    peers: this Mac's recent public-counter windows with its peers' rates (see peer_stall),
    or None (no evidence, or a network-wide outage: the peer trigger stays quiet).
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
        'trigger': None,
        'peers': None,
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
    peer = peer_stall(
        peers,
        model,
        now,
        [a['at'] for a in attempts if a.get('step') == 'probe' and a['at'] >= last],
    )
    if peer:
        result['peers'] = {k: v for k, v in peer.items() if k != 'since'}
    steady = active >= MIN_ACTIVE_MINUTES and rate >= MIN_JOBS_PER_MINUTE
    # Macs like this one are getting work: this Mac's silence isn't the network's quiet time.
    by_peers = bool(peer and peer['busy'] and peer['seconds'] >= PEER_SILENCE_SECONDS)
    if not steady and not by_peers:
        result.update(
            status='quiet',
            reason='Work before this quiet period was not steady enough to call it a stall.',
        )
        return result
    result['trigger'] = 'own' if steady else 'peers'
    required = SILENCE_SECONDS if steady else PEER_SILENCE_SECONDS
    result['requiredSeconds'] = required
    if silence < required:
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
    # Peers on this model still getting work overrule a fall in its demand readings: demand
    # for Macs like this one held.
    if held is False and not by_peers and 'escape' not in taken:
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
            peer_sentence(peer, silence) + ' Sending a test request to nudge routing.'
            if by_peers
            else 'Work stopped abruptly while network demand held. Sending a test request to nudge routing.',
        )
    elif 'restart' not in taken and 'escape' not in taken and restart_allowed:
        step, reason = (
            'restart',
            peer_sentence(peer, silence)
            + ' Restarting the provider on the same model for a fresh session.'
            if by_peers
            else 'Still no work while network demand held. Restarting the provider on the same model for a fresh session.',
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
    restart_silence = RESTART_SILENCE_SECONDS if steady else PEER_RESTART_SILENCE_SECONDS
    if step == 'restart' and silence < restart_silence:
        result.update(
            reason='Waiting for %d minutes of silence before restarting.' % (restart_silence // 60)
        )
        return result
    if step == 'restart' and not (recent and recent['pressure'] >= RESTART_MIN_PRESSURE):
        # The gate skips the restart, not the ladder: the escape (then hold) still follows.
        step, reason = (
            'escape',
            (
                'Not restarting: demand for this model is low (%.2f requests per warm provider), so a restart does no better than waiting.'
                % recent['pressure']
                if recent
                else 'Not restarting: there are no fresh demand readings for this model.'
            )
            + ' Still no work after '
            + (' and '.join(taken) if taken else 'waiting')
            + '. Letting the optimizer try another model now.',
        )
    if by_peers and not reason.startswith(PEER_NOTE):
        reason = PEER_NOTE + ' ' + reason
    result.update(step=step, reason=reason)
    return result
