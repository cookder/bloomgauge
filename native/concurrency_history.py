"""History of already-authenticated, warm-session owner API observations."""

import json
import math
from collections import defaultdict
from demand_baselines import read_view
from energy import period, number


class ConcurrencyHistory:
    def __init__(self, store):
        self.store = store
        with store.h.lock:
            store.h.db.executescript("""CREATE TABLE IF NOT EXISTS concurrency_observations(
              account TEXT,device TEXT,at REAL,session INTEGER,models TEXT,payload TEXT,
              PRIMARY KEY(account,device,at));""")

    def observe(self, account, device, snapshot, now):
        data = snapshot.get('data') or {}
        session = snapshot.get('session') or {}
        c = data.get('concurrency')
        perf = session.get('performance') or {}
        if (
            not account
            or not device
            or snapshot.get('status') != 'ok'
            or not c
            or session.get('status') != 'active'
            or perf.get('status') != 'counting'
            or data.get('observedSessionId') != session.get('id')
            or not number(c.get('at'))
            or not 0 <= now - c['at'] <= 60
            or not number(data.get('requestedAt'))
            or not 0 <= now - data['requestedAt'] <= 45
            or c['at'] < max(session.get('startedAt', 0), perf.get('segmentStartedAt', now))
        ):
            return
        models = session.get('models')
        if (
            not isinstance(models, list)
            or not models
            or not all(isinstance(m, str) and m for m in models)
        ):
            return
        payload = {
            **c,
            'receivedAt': now,
            'requestedAt': data['requestedAt'],
            **{k: data.get(k) for k in ('score', 'failedJobs', 'totalJobs', 'responseTimeMs')},
        }
        with self.store.h.lock:
            self.store.h.db.execute(
                'INSERT OR IGNORE INTO concurrency_observations VALUES(?,?,?,?,?,?)',
                (
                    account,
                    device,
                    c['at'],
                    session['id'],
                    json.dumps(sorted(models)),
                    json.dumps(payload, allow_nan=False),
                ),
            )
            self.store.h.db.commit()

    def report(self, account, device, start, end, now, session=None, model=None):
        start, end = period(start, end, now)
        if session is not None and (type(session) is not int or session < 1):
            raise ValueError('Invalid session.')
        if model is not None and (not isinstance(model, str) or not 1 <= len(model) <= 512):
            raise ValueError('Invalid model.')
        with read_view(self.store) as view:
            db = view.h.db
            first, last = db.execute(
                'SELECT MIN(at),MAX(at) FROM concurrency_observations WHERE account=? AND device=?',
                (account, device),
            ).fetchone()
            rows = db.execute(
                """SELECT at,session,models,payload FROM concurrency_observations WHERE account=? AND device=?
                AND at>=? AND at<? AND (? IS NULL OR session=?) ORDER BY at DESC LIMIT 100001""",
                (account, device, start, end, session, session),
            ).fetchall()
        truncated = len(rows) > 100000
        rows = list(reversed(rows[:100000]))
        low = max(start, first or start, rows[0]['at'] if truncated else 0)
        step = max(30, math.ceil(max(0, end - low) / 400 / 30) * 30)
        groups = defaultdict(list)
        sessions = {}
        models = set()
        previous = None
        failures = 0
        failure_intervals = 0
        latest = None
        for row in rows:
            ms = json.loads(row['models'])
            models.update(ms)
            sessions[row['session']] = ms
            if model is not None and model not in ms:
                continue
            c = json.loads(row['payload'])
            slots = [s for s in c['slots'] if model is None or s['model'] == model]

            def total(k):
                return (
                    sum(s[k] for s in slots)
                    if slots and all(number(s.get(k)) for s in slots)
                    else None
                )

            point = {
                'at': row['at'],
                'session': row['session'],
                'models': ms,
                'pending': c['pending'] if model is None or len(ms) == 1 else None,
                'providerLimit': c['limit'] if model is None or len(ms) == 1 else None,
                'running': total('running'),
                'waiting': total('waiting'),
                'slotLimit': total('limit'),
                'score': c.get('score'),
                'responseTimeMs': c.get('responseTimeMs'),
                'failedJobs': c.get('failedJobs'),
            }
            # These are changes in the network-record counter, never a lifetime
            # counter presented as this session's total. Missing/reset breaks it.
            if (
                previous
                and point['session'] == previous['session']
                and point['at'] - previous['at'] <= 90
                and number(point['failedJobs'])
                and number(previous['failedJobs'])
                and point['failedJobs'] >= previous['failedJobs']
            ):
                failures += point['failedJobs'] - previous['failedJobs']
                failure_intervals += 1
            previous = point
            latest = point
            groups[(int((row['at'] - low) // step), row['session'])].append(point)
        samples = []
        last_session = None
        last_at = None
        keys = [
            'pending',
            'providerLimit',
            'running',
            'waiting',
            'slotLimit',
            'score',
            'responseTimeMs',
        ]
        for (bucket, sid), points in sorted(groups.items()):
            if last_session is not None and (sid != last_session or points[0]['at'] - last_at > 90):
                samples.append({'at': last_at + 0.001, **{k: None for k in keys}})
            # Do not connect across missing readings inside an aggregated bin.
            gaps = any(b['at'] - a['at'] > 90 for a, b in zip(points, points[1:]))
            samples.append(
                {
                    'at': sum(p['at'] for p in points) / len(points),
                    **{
                        k: sum(p[k] for p in points) / len(points)
                        if not gaps and all(number(p[k]) for p in points)
                        else None
                        for k in keys
                    },
                }
            )
            last_session = sid
            last_at = points[-1]['at']
        return {
            'at': now,
            'from': low,
            'to': end,
            'coverageStart': first,
            'coverageEnd': last,
            'bucketSeconds': step,
            'samples': samples,
            'latest': latest,
            'fresh': bool(latest and 0 <= now - latest['at'] <= 60),
            'session': session,
            'model': model,
            'sessions': [
                {'id': sid, 'models': ms} for sid, ms in sorted(sessions.items(), reverse=True)
            ],
            'models': sorted(models),
            'observations': sum(len(v) for v in groups.values()),
            'truncated': truncated,
            'failureIncrements': failures if failure_intervals else None,
            'method': 'Coordinator reservations and backend running/waiting gauges stay separate. Distinct fresh heartbeats only, matched to this Mac and a verified warm reporting session. Blank means unknown. No historical backfill or concurrency setting changes.',
        }
