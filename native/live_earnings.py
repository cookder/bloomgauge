"""Confirmed credit updates and a device/session-scoped earnings pulse.

The upstream account response is cached for 20 seconds. Local display time can
advance every second, but it must never turn a pace estimate into paid money.
"""

import copy, hashlib, json, math, sqlite3, time, uuid
from datetime import datetime
from email.utils import parsedate_to_datetime
from history import epoch
from forecast import hour_start, monitor_hour_start
from optimizer_store import device_id
from model_combinations import selection_key

POLL_SECONDS = 20
CACHE_SECONDS = 20
# Until earlier sessions give a model set 30 minutes, the running session's settled
# minutes build its first baseline once they are this old, so the live 5-minute
# reading is never compared with itself.
CURRENT_SESSION_LAG = 600


def historical_baseline(rows, models, now):
    """Choose a sufficiently observed local-time comparison, then fall back.

    Callers supply settled, covered, exact-model warm minutes from earlier
    sessions or, while those are under 30 minutes, also this session's minutes
    older than CURRENT_SESSION_LAG. Clock matching uses the Mac's local timezone
    at each actual timestamp, including DST, rather than assuming every date is 24 hours long.
    A single busy date cannot establish a daily pattern: contextual comparisons
    require at least three dates and two hours, and cap each date's weight.
    """
    local_now = datetime.fromtimestamp(now).astimezone()
    dated = [(r, datetime.fromtimestamp(r['at']).astimezone()) for r in rows]
    total_seconds = sum(r['seconds'] for r in rows)
    total_days = len({local.date() for _, local in dated})
    result = {
        'ratePerHour': sum(r['usd'] for r in rows) * 3600 / total_seconds
        if total_seconds >= 1800
        else None,
        'hours': total_seconds / 3600,
        'days': total_days,
        'models': models,
        'totalHours': total_seconds / 3600,
        'totalDays': total_days,
        'scope': 'model' if total_seconds >= 1800 else 'learning',
        'localHour': local_now.hour,
        'localWeekday': local_now.weekday(),
        'hourRadius': 1,
        'timezone': local_now.tzname() or 'Local',
        'dayWeightCapHours': None,
        'detail': (
            'All hours for this exact model set over the last 30 days. '
            'Includes verified warm idle time. Time/day adjustment needs at least 2 comparable '
            'warm hours across 3 dates, with at least 30 minutes on each date.'
        ),
    }
    if total_seconds < 1800:
        result['detail'] = (
            'Building a baseline for this exact model set. It needs 30 minutes of settled, '
            'covered, verified warm runtime; this session counts after 10 minutes.'
        )
        return result

    near_hour = [
        (r, local)
        for r, local in dated
        if min(abs(local.hour - local_now.hour), 24 - abs(local.hour - local_now.hour)) <= 1
    ]
    candidates = (
        (
            'weekday_hour',
            [(r, local) for r, local in near_hour if local.weekday() == local_now.weekday()],
            local_now.strftime('%A') + 's',
        ),
        (
            'daytype_hour',
            [
                (r, local)
                for r, local in near_hour
                if (local.weekday() >= 5) == (local_now.weekday() >= 5)
            ],
            'weekends' if local_now.weekday() >= 5 else 'weekdays',
        ),
        ('hour', near_hour, 'all days'),
    )
    for scope, items, day_label in candidates:
        dates = {}
        for row, local in items:
            day = dates.setdefault(local.date(), {'seconds': 0, 'usd': 0})
            day['seconds'] += row['seconds']
            day['usd'] += row['usd']
        # Scattered short bursts on otherwise unobserved dates cannot qualify.
        days = [day for day in dates.values() if day['seconds'] >= 1800]
        seconds = sum(day['seconds'] for day in days)
        if len(days) < 3 or seconds < 7200:
            continue
        weights = [min(day['seconds'], 3600) for day in days]
        rate = sum(
            day['usd'] * 3600 / day['seconds'] * weight for day, weight in zip(days, weights)
        ) / sum(weights)
        result.update(
            ratePerHour=rate,
            hours=seconds / 3600,
            days=len(days),
            scope=scope,
            dayWeightCapHours=1,
            detail=(
                f'Comparable {day_label} around this local hour (the current clock hour and one hour '
                f'either side), across {len(days)} earlier dates in the last 30 days. '
                'Each date contributes at most one warm hour of weight; verified warm idle time '
                'is included. Other models and the current session are excluded.'
            ),
        )
        break
    return result


def retry_delay(failures, retry_after=None, now=None, auth=False):
    delay = max(60 if auth else 20, min(900, 20 * 2 ** min(failures, 6)))
    if retry_after:
        try:
            requested = float(retry_after)
        except (ValueError, TypeError):
            try:
                requested = parsedate_to_datetime(retry_after).timestamp() - (
                    time.time() if now is None else now
                )
            except (ValueError, TypeError, OverflowError):
                requested = 0
        if math.isfinite(requested):
            delay = max(delay, requested)
    return max(1, delay)


def credit_rows(entries):
    """Validate before any ledger writes, preserving integer microdollars."""
    rows = {}
    if not isinstance(entries, list) or len(entries) > 1000:
        raise ValueError('Invalid credit list')
    for x in entries:
        if (
            not isinstance(x, dict)
            or type(x.get('id')) is not int
            or not 0 <= x['id'] <= 9007199254740991
            or type(x.get('amount_micro_usd')) is not int
            or abs(x['amount_micro_usd']) > 9007199254740991
        ):
            raise ValueError('Invalid credit')
        tokens = x.get('completion_tokens')
        if tokens is not None and (type(tokens) is not int or not 0 <= tokens <= 9007199254740991):
            raise ValueError('Invalid token count')
        at = epoch(x['created_at'])
        model, provider = x.get('model', 'Unknown'), x.get('provider_id', '')
        if (
            not math.isfinite(at)
            or not isinstance(model, str)
            or not model
            or len(model) > 512
            or not isinstance(provider, str)
        ):
            raise ValueError('Invalid credit fields')
        row = {
            'id': x['id'],
            'at': at,
            'microUsd': x['amount_micro_usd'],
            'model': model,
            '_provider': provider,
        }
        if row['id'] in rows and rows[row['id']] != row:
            raise ValueError('Conflicting credit IDs')
        rows[row['id']] = row
    return list(rows.values())


def supplement_monitor(monitor, rows, fetched_at, now, source_as_of=None, covered_start=None):
    """Reconcile against Monitor's retained IDs, never a timestamp/max-ID guess.

    Only IDs above its retained floor can be proven absent. Older unknown IDs
    remain with Monitor rather than risking re-adding previously forgotten IDs.
    This is an ephemeral overlay; Monitor's source file and saved base are intact.
    """
    out = {k: copy.deepcopy(v) for k, v in monitor.items() if not k.startswith('_')}
    known = monitor.get('_recentEarningIDs')
    categorized = monitor.get('_categorizedEarningIDs')
    valid = lambda ids: (
        isinstance(ids, list) and bool(ids) and all(type(i) is int and i >= 0 for i in ids)
    )
    if not valid(known) or not valid(categorized) or not fetched_at:
        out['liveCredits'] = {
            'status': 'waiting',
            'added': 0,
            'detail': 'Waiting for Monitor’s credit IDs to reconcile live earnings.',
        }
        return out
    known, categorized = set(known), set(categorized)
    floor, category_floor = min(known), min(categorized)
    hours = {h['at']: h for h in out.get('hours', [])}
    monitor_hours = list(hours.values())
    added = 0
    for row in rows:
        if row['at'] > now or row['at'] < (out.get('coverageStartedAt') or 0):
            continue
        new_total = row['id'] > floor and row['id'] not in known
        new_category = row['id'] > category_floor and row['id'] not in categorized
        if not new_total and not new_category:
            continue
        at = monitor_hour_start(monitor_hours, row['at'])
        h = hours.setdefault(
            at, {'at': at, 'usd': 0, 'jobs': 0, 'categories': {}, 'categoryJobs': {}}
        )
        if new_total:
            h['usd'] = (round(h['usd'] * 1e6) + row['microUsd']) / 1e6
            h['jobs'] += int(row['model'] != 'base_reward')
            added += 1
        if new_category:
            category = row['model']
            had_category = category in h['categories']
            h['categories'][category] = (
                round(h['categories'].get(category, 0) * 1e6) + row['microUsd']
            ) / 1e6
            # Old category totals without counts stay unknown rather than
            # acquiring a denominator consisting only of the new tail.
            jobs = h.setdefault('categoryJobs', {})
            if category in jobs or not had_category:
                jobs[category] = jobs.get(category, 0) + 1
    out['hours'] = sorted(hours.values(), key=lambda h: h['at'])
    observed = monitor.get('observedAt') or monitor.get('updatedAt') or 0
    as_of = min(now, source_as_of if source_as_of is not None else fetched_at - CACHE_SECONDS)
    fresh = 0 <= now - fetched_at <= POLL_SECONDS * 2 + 5
    oldest = (
        covered_start if covered_start is not None else min((r['at'] for r in rows), default=as_of)
    )
    overlaps = bool(rows and any(r['id'] in known for r in rows) and oldest <= observed)
    if fresh and as_of > observed and overlaps:
        out.update(observedAt=as_of, status='ok')
    elif fresh and as_of > observed and added:
        # Preserve the money without pretending the gap was observed idle time.
        out.update(observedAt=as_of, status='partial')
        out.setdefault('coverageIntervals', []).append(
            {'start': observed, 'end': max(observed, oldest)}
        )
        out['gaps'] = max(out.get('gaps', 0) + 1, len(out['coverageIntervals']))
    out['liveCredits'] = {
        'status': 'synced' if fresh and overlaps else 'partial',
        'added': added,
        'updatedAt': fetched_at,
        'detail': 'Confirmed API credits reconciled with Monitor; older hourly coverage is unchanged.',
    }
    # Includes totals and categories so a correction cannot be hidden by memoization.
    out['revision'] = hashlib.sha256(
        repr((out.get('observedAt'), out['hours'], out['liveCredits'])).encode()
    ).hexdigest()[:20]
    return out


def shared_view(overlay):
    """A per-second view of the cached overlay. The hour list is shared, not
    copied (about 70% of collect() time went to deep copies of it); readers
    treat it as read-only. Only fields the collector edits are copied."""
    view = dict(overlay)
    if isinstance(view.get('liveCredits'), dict):
        view['liveCredits'] = dict(view['liveCredits'])
    return view


class EarningsPulse:
    def __init__(self, history, projection):
        self.h, self.projection = history, projection
        self.account, self.stream = None, None
        self.rows, self.arrivals = [], []
        self.fetched_at = None
        self.baseline_key, self.baseline_at, self.baseline = None, 0, None
        self.total_key, self.session_total = None, None
        self.overlay_key, self.overlay = None, None
        with self.h.lock:
            self.h.db.execute("""CREATE TABLE IF NOT EXISTS pulse_connections(
                account TEXT,session INTEGER,device TEXT,provider TEXT,
                PRIMARY KEY(account,session,device,provider))""")
            self.h.db.execute("""CREATE TABLE IF NOT EXISTS pulse_rates(
                account TEXT,session INTEGER,at INTEGER,rate60 REAL,rate300 REAL,
                PRIMARY KEY(account,session,at))""")
            self.h.db.commit()

    def record_rate(self, account, pulse):
        """Save the exact meter readings, not reconstructed historical earnings.

        Cold/stale/unmatched periods are null. History's normal sample commits
        flush these writes, and closing BloomGauge commits the final partial batch.
        """
        if not account or type(pulse.get('sessionId')) is not int:
            return
        rates = []
        for window in ('60', '300'):
            value = pulse.get('windows', {}).get(window, {}).get('ratePerHour')
            rates.append(
                value
                if pulse.get('status') == 'live'
                and type(value) in (int, float)
                and math.isfinite(value)
                else None
            )
        with self.h.lock:
            self.h.db.execute(
                'INSERT OR REPLACE INTO pulse_rates VALUES(?,?,?,?,?)',
                (account, pulse['sessionId'], int(pulse['at']), *rates),
            )

    def rate_history(self, account, session, start, end, bucket_seconds=None):
        result = {
            'sessionId': session,
            'samples': [],
            'coverageStart': None,
            'coverageEnd': None,
            'bucketSeconds': 1,
            'count': 0,
        }
        if not account or session is None:
            return result
        with self.h.lock:
            coverage = self.h.db.execute(
                'SELECT MIN(at),MAX(at) FROM pulse_rates WHERE account=? AND session=?',
                (account, session),
            ).fetchone()
            if coverage[0] is None:
                return result
            low, high = max(start, coverage[0]), min(end, coverage[1])
            step = max(1, bucket_seconds or math.ceil(max(0, high - low) / 600))
            rows = self.h.db.execute(
                """SELECT AVG(at) AS at,MIN(at) AS first,MAX(at) AS last,COUNT(*) AS n,
                CASE WHEN COUNT(rate60)=COUNT(*) THEN AVG(rate60) END AS rate60,
                CASE WHEN COUNT(rate300)=COUNT(*) THEN AVG(rate300) END AS rate300
                FROM pulse_rates WHERE account=? AND session=? AND at>=? AND at<=?
                GROUP BY CAST((at-?)/? AS INT) ORDER BY at""",
                (account, session, start, end, low, step),
            ).fetchall()
        samples = []
        previous = None
        for row in rows:
            if previous is not None and row['first'] - previous > max(5, step * 2):
                samples.append({'at': previous + 1, 'rate60': None, 'rate300': None})
            # Never draw a continuous average across a substantial unobserved
            # gap hidden inside a coarse bucket.
            gap = row['last'] - row['first'] > row['n'] * 2 + 5
            samples.append(
                {
                    'at': row['at'],
                    'rate60': None if gap else row['rate60'],
                    'rate300': None if gap else row['rate300'],
                }
            )
            previous = row['last']
        return {
            **result,
            'samples': samples,
            'coverageStart': coverage[0],
            'coverageEnd': coverage[1],
            'bucketSeconds': step,
            'count': sum(row['n'] for row in rows),
        }

    def monitor_snapshot(self, monitor, account, earnings, now):
        if account and monitor.get('_account') != account:
            return self.api_hourly_snapshot(account, earnings, now)
        if account and not monitor.get('_recentEarningIDs'):
            return self.api_hourly_snapshot(account, earnings, now)
        if self.account != account:
            return {k: copy.deepcopy(v) for k, v in monitor.items() if not k.startswith('_')}
        key = (
            account,
            monitor.get('updatedAt'),
            monitor.get('status'),
            repr(monitor.get('_recentEarningIDs')),
            repr(monitor.get('_categorizedEarningIDs')),
            self.fetched_at,
            earnings.get('status'),
            now - (self.fetched_at or 0) > 45,
        )
        if key != self.overlay_key:
            ids = (monitor.get('_recentEarningIDs') or []) + (
                monitor.get('_categorizedEarningIDs') or []
            )
            rows = []
            if ids and all(type(i) is int and i >= 0 for i in ids):
                # Query saved credits, not only the latest API page. A lagging
                # Monitor must not make confirmed money disappear at rollover.
                with self.h.lock:
                    rows = [
                        {
                            'id': r['id'],
                            'at': r['at'],
                            'model': r['model'],
                            'microUsd': r['micro_usd'],
                        }
                        for r in self.h.db.execute(
                            'SELECT id,at,model,micro_usd FROM credits WHERE account=? AND id>=? AND at>=? AND at<=?',
                            (account, min(ids), monitor.get('coverageStartedAt') or 0, now),
                        )
                    ]
            coverage = self.h.cache('credit-coverage:' + account) or {}
            self.overlay = supplement_monitor(
                monitor,
                rows,
                self.fetched_at,
                now,
                earnings.get('sourceAsOf'),
                coverage.get('start'),
            )
            self.overlay_key = key
        return shared_view(self.overlay)

    def api_hourly_snapshot(self, account, earnings, now):
        """A standalone ledger view, never added on top of Monitor totals.

        Confirmed amounts remain visible even when historical coverage is unknown.
        Gaps come from recorded credit coverage, never interpolated paid work.
        """
        key = ('api-ledger', account, earnings.get('updatedAt'), earnings.get('status'))
        if key != self.overlay_key:
            hours = {}
            with self.h.lock:
                rows = self.h.db.execute(
                    """SELECT CAST(at/60 AS INT)*60 at,model,SUM(micro_usd) usd,COUNT(*) jobs
                    FROM credits WHERE account=? AND at<=? GROUP BY 1,model ORDER BY 1""",
                    (account, now),
                ).fetchall()
                try:
                    coverage = [
                        tuple(r)
                        for r in self.h.db.execute(
                            'SELECT start,end FROM opt_coverage WHERE account=? ORDER BY start',
                            (account,),
                        )
                    ]
                except Exception:
                    coverage = []
            for row in rows:
                at = hour_start(row['at'])
                model = row['model']
                h = hours.setdefault(
                    at, {'at': at, 'usd': 0, 'jobs': 0, 'categories': {}, 'categoryJobs': {}}
                )
                h['usd'] = (round(h['usd'] * 1e6) + row['usd']) / 1e6
                h['jobs'] += row['jobs'] if model != 'base_reward' else 0
                h['categories'][model] = (
                    round(h['categories'].get(model, 0) * 1e6) + row['usd']
                ) / 1e6
                h['categoryJobs'][model] = h['categoryJobs'].get(model, 0) + row['jobs']
            first = min([r['at'] for r in rows] + [r[0] for r in coverage], default=None)
            end = min(now, max((r[1] for r in coverage), default=first or now))
            cursor = first
            gaps = []
            for start, stop in coverage:
                if cursor is not None and start > cursor:
                    gaps.append({'start': cursor, 'end': min(start, end)})
                cursor = max(cursor or start, min(stop, end))
            if cursor is not None and cursor < end:
                gaps.append({'start': cursor, 'end': end})
            if not coverage and first is not None:
                gaps = [{'start': first, 'end': now}]
            self.overlay = {
                'status': 'stale'
                if earnings.get('status') == 'stale'
                else 'ok'
                if earnings.get('status') == 'ok' and coverage
                else 'partial'
                if rows
                else 'missing',
                'updatedAt': earnings.get('updatedAt'),
                'coverageStartedAt': first,
                'observedAt': end if coverage else None,
                'coverageIntervals': gaps,
                'gaps': len(gaps),
                'hours': list(hours.values()),
                'source': 'BloomGauge confirmed API ledger',
                'liveCredits': {
                    'status': 'synced' if earnings.get('status') == 'ok' else 'stale',
                    'added': 0,
                    'detail': 'Confirmed account API credits. Monitor is optional; unknown prior coverage stays unknown.',
                },
                'revision': hashlib.sha256(repr((key, list(hours.values()))).encode()).hexdigest()[
                    :20
                ],
            }
            self.overlay_key = key
        return shared_view(self.overlay)

    def ingest(self, account, rows, new_ids, at):
        if self.account != account:
            self.account, self.stream = account, uuid.uuid4().hex
            self.arrivals = []
            new_ids = set()  # Initial sync/history is never animated as new work.
        self.rows, self.fetched_at = rows, at
        self.arrivals.extend({**r, 'receivedAt': at} for r in rows if r['id'] in new_ids)
        self.arrivals = sorted(
            (r for r in self.arrivals if r['receivedAt'] >= at - 120),
            key=lambda r: (r['receivedAt'], r['at'], r['id']),
        )[-128:]

    def set_minutes(self, account, device, models, now):
        """Warm minutes for a set the optimizer store doesn't key (three or more models).

        Built from this device's sessions that served exactly this set: their verified
        ready intervals, inside credit-poll coverage and settled, with this set's
        credits from those sessions' providers. The same shape as store evidence.
        """
        start, end, want = now - 30 * 86400, now - 120, sorted(models)
        try:
            with self.h.lock:
                db = self.h.db
                providers = {}
                for sid, provider in db.execute(
                    'SELECT session,provider FROM pulse_connections WHERE account=? AND device=?',
                    (account, device),
                ):
                    providers.setdefault(sid, set()).add(provider)
                marks = ','.join('?' for _ in providers)
                sessions = {}
                for sid, data in (
                    db.execute(
                        f'SELECT id,data FROM provider_sessions WHERE id IN ({marks})',
                        list(providers),
                    )
                    if providers
                    else []
                ):
                    try:
                        served = json.loads(data).get('models')
                    except (TypeError, ValueError, AttributeError):
                        continue
                    if isinstance(served, list) and sorted(served) == want:
                        sessions[sid] = providers[sid]
                if not sessions:
                    return []
                ready = [
                    (sid, a, b)
                    for sid in sessions
                    for a, b in db.execute(
                        'SELECT start,end FROM session_ready_intervals WHERE session=? AND end>? AND start<?',
                        (sid, start, end),
                    )
                ]
                coverage = db.execute(
                    'SELECT start,end FROM opt_coverage WHERE account=? AND end>? AND start<?',
                    (account, start, end),
                ).fetchall()
                credits = db.execute(
                    f"""SELECT at,provider,micro_usd FROM opt_credits WHERE account=? AND at>? AND at<=?
                    AND model IN ({','.join('?' for _ in models)})""",
                    (account, start, end, *models),
                ).fetchall()
        except sqlite3.OperationalError:
            return []
        minutes = {}
        for sid, a, b in ready:
            for c, d in coverage:
                lo, hi = max(a, c, start), min(b, d, end)
                if hi <= lo:
                    continue
                at = lo
                while at < hi:
                    minute = int(at // 60) * 60
                    stop = min(hi, minute + 60)
                    row = minutes.setdefault(minute, {'at': minute, 'seconds': 0, 'usd': 0})
                    row['seconds'] += stop - at
                    at = stop
                for when, provider, micro in credits:
                    if lo < when <= hi and provider in sessions[sid]:
                        # A credit stamped on a boundary belongs to the minute it ends.
                        minute = (math.ceil(when / 60) - 1) * 60
                        minutes.setdefault(minute, {'at': minute, 'seconds': 0, 'usd': 0})
                        minutes[minute]['usd'] += micro / 1e6
        return [minutes[k] for k in sorted(minutes) if minutes[k]['seconds'] > 0]

    def snapshot(self, account, raw, session, earnings, connection_id, now):
        models = (session or {}).get('models', [])
        device = device_id(raw)
        live_device = device
        if not device and account and session:
            # An ended/unreadable daemon may lose its identity. Retain income
            # only through this exact session's previously verified mapping.
            with self.h.lock:
                saved = self.h.db.execute(
                    'SELECT DISTINCT device FROM pulse_connections WHERE account=? AND session=?',
                    (account, session['id']),
                ).fetchall()
            if len(saved) == 1:
                device = saved[0][0]
        key = (account, device, tuple(models), (session or {}).get('id'))
        clock = datetime.fromtimestamp(now).astimezone()
        baseline_key = (*key, clock.date(), clock.hour, clock.utcoffset(), clock.tzname())
        if baseline_key != self.baseline_key or now - self.baseline_at >= 60:
            rows = (
                []
                if not (account and device and models and session)
                else self.projection.evidence(account, device, models, now, shared=True)
                if selection_key(models)
                else self.set_minutes(account, device, models, now)
            )
            # Normal means earlier matched runtime for this exact model set,
            # including idle time, rather than this minute's burst or fleet demand.
            # A set without 30 earlier minutes builds its first baseline from this
            # session's settled minutes; the live window itself is never part of it.
            started = (session or {}).get('startedAt', 0)
            earlier = [r for r in rows if r['at'] + 60 <= started]
            rows = (
                earlier
                if sum(r['seconds'] for r in earlier) >= 1800
                else [r for r in rows if r['at'] + 60 <= now - CURRENT_SESSION_LAG]
            )
            self.baseline = historical_baseline(rows, models, now)
            self.baseline_key, self.baseline_at = baseline_key, now
        fresh = bool(
            earnings.get('status') == 'ok'
            and earnings.get('updatedAt')
            and 0 <= now - earnings['updatedAt'] <= 45
        )
        matched = bool(
            fresh
            and live_device
            and session
            and session['status'] == 'active'
            and connection_id
            and account == self.account
        )
        performance = (session or {}).get('performance') or {}
        ready = bool(matched and performance.get('status') == 'counting')
        start = (session or {}).get('startedAt', now)
        coverage = self.h.cache('credit-coverage:' + account) if account else None
        low = max(start, (coverage or {}).get('start', now))
        session_end = (session or {}).get('endedAt') or now
        end = min(
            now, (coverage or {}).get('end', start), earnings.get('sourceAsOf', start), session_end
        )
        eligible = []
        connections = []
        total_key = None
        if account and session and device and models:
            marks = ','.join('?' for _ in models)
            with self.h.lock:
                if matched:
                    inserted = self.h.db.execute(
                        'INSERT OR IGNORE INTO pulse_connections VALUES(?,?,?,?)',
                        (account, session['id'], device, connection_id),
                    )
                    if inserted.rowcount:
                        self.h.db.commit()
                connections = [
                    r[0]
                    for r in self.h.db.execute(
                        'SELECT provider FROM pulse_connections WHERE account=? AND session=? AND device=?',
                        (account, session['id'], device),
                    )
                ]
                eligible = [
                    dict(r)
                    for r in self.h.db.execute(
                        f"""SELECT c.id,c.at,c.model,c.micro_usd FROM opt_credits c
                    JOIN pulse_connections p ON p.account=c.account AND p.provider=c.provider AND p.session=? AND p.device=?
                    WHERE c.account=? AND c.at>=? AND c.at<=? AND c.model IN ({marks}) ORDER BY c.at,c.id""",
                        (session['id'], device, account, max(start, end - 300), end, *models),
                    )
                ]
                total_key = (key, connection_id, self.fetched_at, session.get('endedAt'))
                if total_key != self.total_key:
                    self.session_total = self.h.db.execute(
                        f"""SELECT COALESCE(SUM(c.micro_usd),0) FROM opt_credits c
                        JOIN pulse_connections p ON p.account=c.account AND p.provider=c.provider AND p.session=? AND p.device=?
                        WHERE c.account=? AND c.at>=? AND c.at<=? AND c.model IN ({marks})""",
                        (
                            session['id'],
                            device,
                            account,
                            start,
                            min(now, session.get('endedAt') or now),
                            *models,
                        ),
                    ).fetchone()[0]
                    self.total_key = total_key
        windows = {}
        intervals = []
        if session and connections:
            with self.h.lock:
                intervals = [
                    tuple(r)
                    for r in self.h.db.execute(
                        'SELECT start,end FROM session_ready_intervals WHERE session=? AND start<? AND end>? ORDER BY start',
                        (session['id'], end, max(low, end - 300)),
                    )
                ]
        for seconds in (60, 300):
            begin = max(low, end - seconds)
            covered = [(max(begin, a), min(end, b)) for a, b in intervals if b > begin and a < end]
            duration = sum(max(0, b - a) for a, b in covered)
            micro = sum(
                r['micro_usd'] for r in eligible if any(a < r['at'] <= b for a, b in covered)
            )
            windows[str(seconds)] = {
                'ratePerHour': micro / 1e6 * 3600 / duration if ready and duration >= 20 else None,
                'microUsd': micro,
                'seconds': duration,
                'start': begin,
                'end': end,
            }
        events = [
            {k: v for k, v in r.items() if not k.startswith('_')}
            for r in self.arrivals
            if connection_id
            and session
            and r['_provider'] in connections
            and r['model'] in models
            and start <= r['at'] <= now
            and r['receivedAt'] >= max(now - 120, performance.get('segmentStartedAt') or now)
            and any(a < r['at'] <= b for a, b in intervals)
        ]
        return {
            'at': now,
            'streamId': str(self.stream) + ':' + str(performance.get('segmentStartedAt')),
            'sessionId': (session or {}).get('id'),
            'models': models,
            'status': 'live'
            if ready
            else 'offline'
            if session and session['status'] == 'ended'
            else 'stale'
            if not fresh
            else 'paused'
            if matched
            else 'unmatched',
            'detail': performance.get('detail'),
            'updatedAt': earnings.get('updatedAt'),
            'pollSeconds': POLL_SECONDS,
            'cacheSeconds': CACHE_SECONDS,
            'windows': windows,
            'baseline': self.baseline,
            'sessionMicroUsd': self.session_total
            if connections and session and total_key == self.total_key
            else None,
            'events': events[-64:] if ready else [],
            'scope': 'This Mac · current session · confirmed inference credits',
        }
