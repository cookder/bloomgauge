"""Verified local observation intervals and bounded demand-trial outcomes.

No counter backfill or inferred warm time. Collection gaps, identity changes and
pre-warming break intervals. Network demand is context, never a dollar forecast.
"""

import json
import math
import threading

from model_readiness import session_key
from model_combinations import selection_key
from baseline_learning import measured_jobs, sample_summary
from trial_economics import clock_evidence, competitive, session_span
from optimizer_store import CONTINUITY_SECONDS


def numeric(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


class DemandTrials:
    def __init__(self, history, store):
        self.h, self.store = history, store
        self.lock = threading.RLock()
        self.previous = None
        self.segment = None
        self.reviewed_at = 0
        with self.h.lock:
            self.h.db.execute("""CREATE TABLE IF NOT EXISTS demand_observed(
                account TEXT,device TEXT,session TEXT,model TEXT,start REAL,end REAL,
                requests REAL,tokens REAL,busy INTEGER,first_work REAL,
                PRIMARY KEY(account,device,session,start))""")
            self.h.db.commit()

    def observe(self, account, device, raw, now, ready, proof):
        with self.lock:
            stats = raw.get('stats') or {}
            at = raw.get('written_at')
            valid = bool(
                ready
                and account
                and device
                and numeric(at)
                and -5 < now - at < 15
                and numeric(proof)
                and at >= proof
                and isinstance(raw.get('inference_active'), bool)
                and all(numeric(stats.get(k)) for k in ('requests_served', 'tokens_generated'))
            )
            if not valid:
                self.previous = self.segment = None
                return
            key = (
                account,
                device,
                session_key(raw),
                selection_key(raw.get('advertised_models')),
                proof,
            )
            current = dict(
                key=key,
                at=at,
                requests=stats['requests_served'],
                tokens=stats['tokens_generated'],
                active=raw['inference_active'],
            )
            previous = self.previous
            if previous == current:
                return
            self.previous = current
            continuous = bool(
                previous
                and key == previous['key']
                and 0 < at - previous['at'] <= CONTINUITY_SECONDS
                and all(current[k] >= previous[k] for k in ('requests', 'tokens'))
            )
            if not continuous:
                self.segment = None
                return
            requests, tokens = (current[k] - previous[k] for k in ('requests', 'tokens'))
            busy = bool(current['active'] or previous['active'] or requests or tokens)
            segment = self.segment
            # Idle segments may stay open; work segments cap at a minute so
            # bounded trials do not smear a whole session across their cutoff.
            if (
                not segment
                or segment['end'] != previous['at']
                or segment['busy'] != busy
                or busy
                and at - segment['start'] > 60
            ):
                segment = dict(
                    start=previous['at'],
                    end=at,
                    requests=requests,
                    tokens=tokens,
                    busy=busy,
                    firstWorkAt=at if requests or tokens else None,
                )
            else:
                segment = {
                    **segment,
                    'end': at,
                    'requests': segment['requests'] + requests,
                    'tokens': segment['tokens'] + tokens,
                    'firstWorkAt': segment['firstWorkAt'] or (at if requests or tokens else None),
                }
            self.segment = segment
            with self.h.lock:
                self.h.db.execute(
                    'INSERT OR REPLACE INTO demand_observed VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (
                        *key[:4],
                        segment['start'],
                        at,
                        segment['requests'],
                        segment['tokens'],
                        int(busy),
                        segment['firstWorkAt'],
                    ),
                )
                self.h.db.commit()

    def activity(self, account, device, raw, now):
        with self.lock:
            previous, segment = self.previous, self.segment
            stats = raw.get('stats') or {}
            # The collector can advance while a decision reads history. A newer
            # sample with monotonic counters is fresh work, not lost telemetry.
            ordered = sorted(
                [
                    previous or {},
                    {
                        'at': raw.get('written_at'),
                        'requests': stats.get('requests_served'),
                        'tokens': stats.get('tokens_generated'),
                    },
                ],
                key=lambda r: r.get('at') if numeric(r.get('at')) else -1,
            )
            monotonic = (
                all(numeric(r.get(k)) for r in ordered for k in ('at', 'requests', 'tokens'))
                and all(ordered[1][k] >= ordered[0][k] for k in ('requests', 'tokens'))
                and (
                    ordered[1]['at'] != ordered[0]['at']
                    or all(ordered[1][k] == ordered[0][k] for k in ('requests', 'tokens'))
                )
            )
            fresh = bool(
                previous
                and segment
                and previous['key'][:4]
                == (account, device, session_key(raw), selection_key(raw.get('advertised_models')))
                and numeric(raw.get('written_at'))
                and abs(raw['written_at'] - previous['at']) <= CONTINUITY_SECONDS
                and -5 < now - previous['at'] < 15
                and -5 < now - raw['written_at'] < 15
                and monotonic
                and isinstance(raw.get('inference_active'), bool)
            )
            idle = bool(
                fresh
                and raw['inference_active'] is False
                and not segment['busy']
                and all(ordered[0][k] == ordered[1][k] for k in ('requests', 'tokens'))
            )
            return {
                'fresh': fresh,
                'idleSeconds': segment['end'] - segment['start'] if idle else 0,
                'idleSince': segment['start'] if idle else None,
                'observedAt': previous['at'] if fresh else None,
                'busy': not idle if fresh else None,
            }

    def outcome(self, account, device, run, raw, now):
        decision = run.get('decision') or {}
        session = decision.get('providerSession')
        if decision.get('kind') != 'explore' or run.get('result') != 'switched' or not session:
            return None
        start = run['completedAt']
        minutes = decision.get('trialMinutes', 20)
        learning = decision.get('learningTrial')
        measured = (
            measured_jobs(self.store, account, device, start, now, now, run['model'])
            if learning
            else []
        )
        saved_end = (
            (decision.get('outcome') or {}).get('windowEnd')
            if learning
            and (decision.get('outcome') or {}).get('status')
            in ('settling', 'productive', 'no_traffic', 'no_paid_work', 'insufficient_coverage')
            else None
        )
        with self.h.lock:
            rows = []
            covered = 0
            for r in self.h.db.execute(
                """SELECT start,end,requests,tokens,busy,first_work FROM demand_observed
                WHERE account=? AND device=? AND session=? AND model=? AND end>? AND start<? ORDER BY start""",
                (account, device, session, run['model'], start, now),
            ):
                r = dict(r)
                if r['end'] > now:
                    # Collection can extend an idle segment after the decision
                    # captured its timestamp. Keep its already observed prefix;
                    # dropping the whole row would erase completed warm time.
                    if r['busy']:
                        continue  # Work counts cannot be split at this boundary.
                    r['end'] = now
                if r['start'] < start:
                    # A daemon sample can precede the switch function's return
                    # by milliseconds. Do not discard an hours-long idle row
                    # because its first source timestamp crosses that boundary.
                    if r['busy']:
                        continue  # Individual request times are unknown here.
                    r['start'] = start
                rows.append(r)
                covered += r['end'] - r['start']
                if covered >= minutes * 60:
                    break
        seconds = busy = requests = tokens = 0
        first = None
        end = None
        warm_start = None
        first_seconds = None
        for row in rows:
            learned = (
                sample_summary([r for r in measured if end is not None and r['at'] < end])
                if learning
                else None
            )
            if (
                seconds >= minutes * 60
                or saved_end is not None
                and end is not None
                and end >= saved_end
                or (
                    saved_end is None
                    and learning
                    and seconds >= 600
                    and learned['completeTokenSamples'] >= learning['sampleGoal']
                    and learned['jobMinutes'] >= learning['jobMinutesGoal']
                )
            ):
                break
            duration = (
                min(row['end'] - row['start'], minutes * 60 - seconds)
                if not row['busy']
                else row['end'] - row['start']
            )
            if first is None and row['first_work'] is not None:
                first = row['first_work']
                first_seconds = seconds + first - row['start']
            warm_start = warm_start if warm_start is not None else row['start']
            seconds += duration
            busy += duration * row['busy']
            requests += row['requests']
            tokens += row['tokens']
            end = row['start'] + duration
        learned = (
            sample_summary([r for r in measured if end is not None and r['at'] < end])
            if learning
            else None
        )
        sample_done = bool(
            learning
            and seconds >= 600
            and learned['completeTokenSamples'] >= learning['sampleGoal']
            and learned['jobMinutes'] >= learning['jobMinutesGoal']
        )
        done = (
            seconds >= minutes * 60
            or sample_done
            or saved_end is not None
            and end is not None
            and end >= saved_end
        )
        # Use the same covered, complete, settled warm-minute money provenance as
        # every other model report. The fresh end of a trial is not unpaid work.
        earned = self.store.evidence(account, device, warm_start or start, end or start, now).get(
            run['model'], {}
        )
        paid_seconds = earned.get('hours', 0) * 3600
        settled = bool(done and now - (end or now) >= 120 and paid_seconds >= seconds - 120)
        deadline = end + 300 if done and end is not None else None
        incomplete = bool(done and not settled and deadline is not None and now >= deadline)
        current = session == session_key(raw)
        result = (
            (
                'productive'
                if earned.get('usd', 0) > 0
                else 'no_traffic'
                if not (requests or tokens or busy)
                else 'no_paid_work'
            )
            if settled
            else (
                'insufficient_coverage'
                if incomplete
                else 'settling'
                if done
                else 'running'
                if current
                else 'interrupted'
            )
        )
        # Steady pace: covered minutes after the first work arrived, so loading
        # and the wait for routing (the ramp) don't count against the model.
        steady = None
        if settled and first is not None and end is not None:
            steady_start = math.ceil(first / 60) * 60
            if end - steady_start >= 300:
                later = self.store.evidence(account, device, steady_start, end, now).get(
                    run['model'], {}
                )
                if later.get('hours', 0) * 3600 >= end - steady_start - 120:
                    steady = later.get('usdPerHour')
        clock = (
            clock_evidence(self.store, account, device, run, end, now)
            if decision.get('economicTrial')
            else {}
        )
        economic = competitive(decision.get('economicTrial'), clock, settled)
        occupancy = (
            session_span(self.h, device, run, now) if decision.get('economicTrial') else None
        )
        return {
            'runId': run['id'],
            'model': run['model'],
            'current': current,
            'status': result,
            'paymentSeen': clock.get('paymentSeen', bool(earned.get('usd', 0) > 0)),
            'competitive': economic,
            'clock': clock,
            'occupancy': occupancy,
            'learning': (
                {
                    **learned,
                    'sampleGoal': learning['sampleGoal'],
                    'jobMinutesGoal': learning['jobMinutesGoal'],
                    'targetReached': sample_done,
                }
                if learning
                else None
            ),
            'warmSeconds': seconds,
            'trialMinutes': minutes,
            'warmStartedAt': warm_start,
            'windowEnd': end,
            'firstTrafficSeconds': first_seconds,
            'rampSeconds': first_seconds,
            'steadyUsdPerHour': steady,
            'requests': requests,
            'tokens': tokens,
            'idlePercent': (seconds - busy) * 100 / seconds if seconds else None,
            'paidWarmSeconds': paid_seconds,
            'usd': earned.get('usd') if paid_seconds else None,
            'paidJobs': earned.get('jobs') if paid_seconds else None,
            'usdPerHour': earned.get('usdPerHour') if paid_seconds else None,
            'cooldownUntil': end + decision.get('trialCooldownMinutes', 120) * 60
            if settled and result != 'productive'
            else None,
            'pressure': decision.get('candidate', {}).get('signal', {}).get('pressure'),
            'load': decision.get('candidate', {}).get('signal', {}).get('load'),
            'evaluatedAt': now,
            'complete': settled or incomplete,
            'settled': settled,
            'coveragePercent': min(100, paid_seconds * 100 / seconds) if seconds else 0,
            'settlementDeadline': deadline,
            'partial': incomplete,
        }

    def review(self, account, device, runs, raw, now):
        # Background only; GET previews never write outcomes. Late credits can
        # correct a provisional zero rather than permanently poisoning history.
        with self.lock:
            if 0 <= now - self.reviewed_at < 60:
                return
            self.reviewed_at = now
        for run in runs:
            if now - run['at'] > 2 * 86400:
                continue
            outcome = self.outcome(account, device, run, raw, now)
            if outcome is None:
                continue
            with self.h.lock:
                saved = self.h.db.execute(
                    'SELECT payload FROM demand_switch_runs WHERE id=? AND account=? AND device=?',
                    (run['id'], account, device),
                ).fetchone()
                if not saved:
                    continue
                payload = {**json.loads(saved['payload']), 'outcome': outcome}
                self.h.db.execute(
                    "UPDATE demand_switch_runs SET payload=? WHERE id=? AND account=? AND device=? AND result='switched'",
                    (json.dumps(payload, allow_nan=False), run['id'], account, device),
                )
                self.h.db.commit()
