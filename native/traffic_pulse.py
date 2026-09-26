"""Observed local traffic, scoped to one account, provider and warm model session.

The daemon's cumulative counters are not past traffic observations. Only deltas
between fresh, matching warm endpoints are recorded. Reopening Bloomkeeper establishes
a new baseline and leaves the closed period as a gap.
"""

import collections
import math

from model_readiness import session_key
from optimizer_store import device_id


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def counter(value):
    return value if type(value) is int and 0 <= value <= 2**53 - 1 else None


class TrafficPulse:
    def __init__(self, history):
        self.h = history
        self.key, self.previous = None, None
        self.intervals = collections.deque()
        self.gap_key = None
        self.baseline_key, self.baseline_at, self.baseline = None, 0, None
        with self.h.lock:
            self.h.db.execute("""CREATE TABLE IF NOT EXISTS traffic_intervals(
                account TEXT,session INTEGER,start REAL,end REAL,
                tokens REAL,requests REAL,tokens60 REAL,tokens300 REAL,
                requests60 REAL,requests300 REAL,PRIMARY KEY(account,session,end))""")
            self.h.db.commit()

    def save(self, account, session, start, end, tokens=None, requests=None, windows=None):
        windows = windows or {}
        values = [
            windows.get(window, {}).get(metric)
            for metric in ('tokensPerSecond', 'requestsPerMinute')
            for window in ('60', '300')
        ]
        with self.h.lock:
            self.h.db.execute(
                'INSERT OR REPLACE INTO traffic_intervals VALUES(?,?,?,?,?,?,?,?,?,?)',
                (account, session, start, end, tokens, requests, *values),
            )

    def reference(self, account, session, end):
        # This is observed local traffic, not network demand or a money forecast.
        # Exclude the newest five minutes so a burst does not chase its reference.
        key = (account, session)
        if key != self.baseline_key or abs(end - self.baseline_at) >= 30:
            with self.h.lock:
                row = self.h.db.execute(
                    """SELECT SUM(MIN(end,:until)-start) AS seconds,
                    SUM(tokens*(MIN(end,:until)-start)/(end-start)) AS tokens,
                    SUM(requests*(MIN(end,:until)-start)/(end-start)) AS requests,
                    SUM(CASE WHEN tokens IS NOT NULL THEN MIN(end,:until)-start ELSE 0 END) AS token_seconds,
                    SUM(CASE WHEN requests IS NOT NULL THEN MIN(end,:until)-start ELSE 0 END) AS request_seconds
                    FROM traffic_intervals WHERE account=:account AND session=:session AND start<:until AND end>start""",
                    {'until': end - 300, 'account': account, 'session': session},
                ).fetchone()
            seconds = row['seconds'] or 0
            token_seconds, request_seconds = row['token_seconds'] or 0, row['request_seconds'] or 0
            self.baseline = {
                'tokensPerSecond': row['tokens'] / token_seconds if token_seconds >= 300 else None,
                'requestsPerMinute': row['requests'] * 60 / request_seconds
                if request_seconds >= 300
                else None,
                'seconds': seconds,
                'tokensSeconds': token_seconds,
                'requestsSeconds': request_seconds,
                'scope': 'Current session · earlier observed warm time · latest 5 minutes excluded',
            }
            self.baseline_key, self.baseline_at = key, end
        return dict(self.baseline)

    def windows(self, end, available):
        windows = {}
        for size in (60, 300):
            start = max(end - size, self.intervals[0]['start']) if self.intervals else end
            rows = [
                (r, max(0, min(r['end'], end) - max(r['start'], start)))
                for r in self.intervals
                if r['end'] > start
            ]
            duration = sum(seconds for _, seconds in rows)
            totals = {}
            for key in ('tokens', 'requests'):
                totals[key] = (
                    sum(r[key] * seconds / (r['end'] - r['start']) for r, seconds in rows)
                    if rows and all(r[key] is not None for r, _ in rows)
                    else None
                )
            ready = available and duration >= 20
            windows[str(size)] = {
                'tokensPerSecond': totals['tokens'] / duration
                if ready and totals['tokens'] is not None
                else None,
                'requestsPerMinute': totals['requests'] * 60 / duration
                if ready and totals['requests'] is not None
                else None,
                'outputTokens': totals['tokens'],
                'requests': totals['requests'],
                'seconds': duration,
                'start': start,
                'end': end,
            }
        return windows

    def observe(self, account, raw, session, tracking, now):
        raw, session, tracking = raw or {}, session or {}, tracking or {}
        sid = session.get('id')
        models = session.get('models', [])
        written = raw.get('written_at')
        fresh = finite(written) and -5 < now - written < 15
        selected = raw.get('advertised_models')
        matched = bool(
            account
            and type(sid) is int
            and device_id(raw)
            and models
            and isinstance(selected, list)
            and all(isinstance(m, str) and m for m in selected)
            and all(isinstance(m, str) and m for m in models)
            and sorted(selected) == sorted(models)
        )
        ready = bool(
            matched
            and fresh
            and session.get('status') == 'active'
            and (session.get('performance') or {}).get('status') == 'counting'
            and tracking.get('counting')
            and finite(tracking.get('verifiedAt'))
            and written >= tracking['verifiedAt']
        )
        key = (account, sid, session_key(raw), tracking.get('verifiedAt')) if ready else None
        if key != self.key:
            self.previous = None
            self.intervals.clear()
            self.key = key
        if not ready:
            self.previous = None
            self.intervals.clear()
            # A point marker breaks charts without inventing a cold duration or
            # repeatedly storing the same paused source once per display tick.
            gap_key = (account, sid, key)
            if account and type(sid) is int and gap_key != self.gap_key:
                self.save(account, sid, now, now)
                self.gap_key = gap_key
        else:
            self.gap_key = None
            stats = raw.get('stats') if isinstance(raw.get('stats'), dict) else {}
            current = {
                'at': written,
                'tokens': counter(stats.get('tokens_generated')),
                'requests': counter(stats.get('requests_served')),
            }
            previous = self.previous
            changed = previous is None or written != previous['at']
            reset = bool(
                previous
                and any(
                    current[k] is not None and previous[k] is not None and current[k] < previous[k]
                    for k in ('tokens', 'requests')
                )
            )
            if (
                previous
                and written == previous['at']
                and any(current[k] != previous[k] for k in ('tokens', 'requests'))
            ):
                # A changed counter under an unchanged source timestamp has no
                # measured duration. Do not reinterpret it as a sudden burst.
                reset, changed = True, True
            if changed:
                continuous = (
                    previous is not None and 0 < written - previous['at'] <= 15 and not reset
                )
                if continuous:
                    interval = {
                        'start': previous['at'],
                        'end': written,
                        **{
                            k: current[k] - previous[k]
                            if current[k] is not None and previous[k] is not None
                            else None
                            for k in ('tokens', 'requests')
                        },
                    }
                    self.intervals.append(interval)
                    while self.intervals and self.intervals[0]['end'] <= written - 300:
                        self.intervals.popleft()
                    windows = self.windows(written, True)
                    self.save(
                        account,
                        sid,
                        interval['start'],
                        written,
                        interval['tokens'],
                        interval['requests'],
                        windows,
                    )
                else:
                    self.intervals.clear()
                    self.save(account, sid, written, written)
                self.previous = current
        end = written if ready else now
        windows = self.windows(end, ready)
        usable = any(
            w.get('tokensPerSecond') is not None or w.get('requestsPerMinute') is not None
            for w in windows.values()
        )
        status = (
            ('live' if usable else 'warming')
            if ready
            else (
                'offline'
                if session.get('status') == 'ended'
                else 'unmatched'
                if not matched
                else 'stale'
                if not fresh
                else 'paused'
            )
        )
        detail = (
            'Observed output tokens and completed requests on this Mac.'
            if usable
            else 'Waiting for valid output-token or completed-request counters.'
            if ready and windows['60']['seconds'] >= 20
            else 'Collecting 20 seconds of verified warm traffic.'
            if ready
            else tracking.get('detail', 'Waiting for a verified warm model session.')
        )
        return {
            'at': now,
            'updatedAt': written if fresh else None,
            'sessionId': sid,
            'models': models,
            'status': status,
            'detail': detail,
            'pollSeconds': 3,
            'windows': windows,
            'baseline': self.reference(account, sid, end) if account and type(sid) is int else None,
            'scope': 'This Mac · current session · observed output tokens and completed requests',
        }

    def rate_history(self, account, session, start, end, metric='tokens', bucket_seconds=None):
        if metric not in ('tokens', 'requests'):
            raise ValueError('Unknown traffic metric')
        result = {
            'sessionId': session,
            'samples': [],
            'coverageStart': None,
            'coverageEnd': None,
            'bucketSeconds': 3,
            'count': 0,
        }
        if not account or type(session) is not int:
            return result
        with self.h.lock:
            coverage = self.h.db.execute(
                'SELECT MIN(start),MAX(end) FROM traffic_intervals WHERE account=? AND session=?',
                (account, session),
            ).fetchone()
            if coverage[0] is None:
                return result
            low, high = max(start, coverage[0]), min(end, coverage[1])
            step = max(3, bucket_seconds or math.ceil(max(0, high - low) / 600))
            count = (
                self.h.db.execute(
                    'SELECT COUNT(*) FROM traffic_intervals WHERE account=? AND session=? AND end>=? AND start<=?',
                    (account, session, low, high),
                ).fetchone()[0]
                if high >= low
                else 0
            )
            # Split source intervals across bucket boundaries and the requested
            # endpoints, so irregular daemon writes cannot bias the averages.
            # Each stored interval is <=15s: recursion is bounded to five pieces.
            rows = (
                self.h.db.execute(
                    f"""WITH RECURSIVE clipped AS (
                SELECT MAX(start,:low) AS a,MIN(end,:high) AS z,{metric}60 AS r60,{metric}300 AS r300
                FROM traffic_intervals WHERE account=:account AND session=:session
                AND end>=:low AND start<=:high
            ), pieces(a,b,z,r60,r300,bucket) AS (
                SELECT a,MIN(z,:low+(CAST((a-:low)/:step AS INT)+1)*:step),z,r60,r300,
                       CAST((a-:low)/:step AS INT) FROM clipped WHERE z>=a
                UNION ALL
                SELECT b,MIN(z,:low+(bucket+2)*:step),z,r60,r300,bucket+1 FROM pieces WHERE b<z
            ) SELECT bucket,MIN(a) AS first,MAX(b) AS last,COUNT(*) AS n,SUM(b-a) AS seconds,
                CASE WHEN COUNT(r60)=COUNT(*) THEN SUM(r60*(b-a))/NULLIF(SUM(b-a),0) END AS rate60,
                CASE WHEN COUNT(r300)=COUNT(*) THEN SUM(r300*(b-a))/NULLIF(SUM(b-a),0) END AS rate300
                FROM pieces GROUP BY bucket ORDER BY bucket""",
                    {
                        'low': low,
                        'high': high,
                        'step': step,
                        'account': account,
                        'session': session,
                    },
                ).fetchall()
                if high >= low
                else []
            )
        samples, previous = [], None
        for row in rows:
            if previous is not None and row['first'] - previous > 0.01:
                samples.append(
                    {
                        'at': previous + (row['first'] - previous) / 2,
                        'rate60': None,
                        'rate300': None,
                    }
                )
            gap = row['last'] - row['first'] - row['seconds'] > 0.01
            samples.append(
                {
                    'at': (row['first'] + row['last']) / 2,
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
            'count': count,
        }
