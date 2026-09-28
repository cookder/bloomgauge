"""Persistent, observational alerts for sustained model concurrency pressure.

The public model feed reports concurrent active/queued requests, not completed
requests per minute. Alerts never convert its ratios into predicted earnings or
change a model. Recorded local earnings retain exact-device warm provenance.
"""

import copy
import json
import math
import threading
from datetime import datetime
from demand_baselines import read_view

LOOKBACK = 30 * 86400
SCAN_SECONDS = 60
RECENT_SECONDS = 300
FRESH_SECONDS = 90
COOLDOWN_SECONDS = 2 * 3600
REARM_SECONDS = 10 * 60
MIN_BASELINE_SAMPLES = 240  # Two hours of distinct, valid 30-second observations.
# A spike: load and pressure per warm provider both >= SPIKE_RATIO x usual, with load >=
# SPIKE_MIN_LOAD. At 2x the old usual (a mean of the same daytype +-1 h) this fired 339 times
# in a 15.4-day replay of Andrew's opt_network (Sep 13-28; within 3 of the logged alerts for each
# model but Qwen3.8, which was not always eligible). The time-of-day median usual (Sep 27) sits
# below that mean (x1.2 for gemma, gpt-oss and qwen3.6-vl, x3-4 for nemotron and Qwen3.5-9B), and
# 2x it fired 500 times (x1.47). 2.8x fires 339 again (22 a day); a higher load minimum changed
# <= 6 of them. calibration-2026-09-28.md (c).
SPIKE_RATIO = 2.8
SPIKE_MIN_LOAD = 1


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _samples(marks):
    """Latest valid sample per model and 30s bucket, so repeated polls never count twice."""
    return f"""WITH latest AS (
        SELECT model,CAST(at/30 AS INTEGER) AS bucket,MAX(at) AS at
        FROM opt_network WHERE model IN ({marks}) AND at>=? AND at<? AND at<=?
        GROUP BY model,bucket), samples AS (
        SELECT n.model,n.at,n.active,n.queued,n.warm,
            n.active+n.queued AS load,
            CASE WHEN n.warm>=1 THEN (n.active+n.queued)/n.warm END AS pressure
        FROM latest l JOIN opt_network n ON n.model=l.model AND n.at=l.at
        WHERE n.active>=0 AND n.active<=1e12 AND n.queued>=0 AND n.queued<=1e12
            AND n.warm>=0 AND n.warm<=1e12)
    """


def _medians(db, models, now, context):
    """Median load and pressure per model over the lookback, before the recent window.

    With `context` (weekend flag, three local hours) only those samples count,
    and only from local dates with at least 30 minutes of them. SQLite localtime
    applies each timestamp's own DST rule. Returns one row per model.
    """
    marks = ','.join('?' for _ in models)
    where = (
        """AND ((CAST(strftime('%w',at,'unixepoch','localtime') AS INTEGER)+6)%7>=5)=?
        AND CAST(strftime('%H',at,'unixepoch','localtime') AS INTEGER) IN (?,?,?)"""
        if context
        else ''
    )
    rows = db.execute(
        _samples(marks)
        + f""", tagged AS (
            SELECT model,load,pressure,day,COUNT(*) OVER (PARTITION BY model,day) AS day_n
            FROM (SELECT model,load,pressure,
                strftime('%Y-%m-%d',at,'unixepoch','localtime') AS day
                FROM samples WHERE warm>=1 {where})),
        ranked AS (
            SELECT model,day,load,pressure,COUNT(*) OVER (PARTITION BY model) AS n,
                ROW_NUMBER() OVER (PARTITION BY model ORDER BY load) AS load_rank,
                ROW_NUMBER() OVER (PARTITION BY model ORDER BY pressure) AS pressure_rank
            FROM tagged WHERE day_n>=?)
        SELECT model,MAX(n) AS n,COUNT(DISTINCT day) AS days,
            AVG(CASE WHEN load_rank IN ((n+1)/2,(n+2)/2) THEN load END) AS load,
            AVG(CASE WHEN pressure_rank IN ((n+1)/2,(n+2)/2) THEN pressure END) AS pressure
        FROM ranked GROUP BY model""",
        (
            *models,
            now - LOOKBACK,
            int(now // 30) * 30 - RECENT_SECONDS,
            now,
            *(context or ()),
            60 if context else 0,
        ),
    ).fetchall()
    return {row['model']: dict(row) for row in rows}


def usual_levels(db, models, now):
    """The app's one "usual" network demand for each model.

    Usual = the median 30-second reading on the same kind of day (weekday or
    weekend) within an hour of this time of day, over the last 30 days. It
    needs two hours of such samples from at least three dates. A median of the
    same time of day is what a reading at this hour normally looks like; a
    mean of all hours is pulled up by busy hours and bursts, so it made quiet
    look like the norm. Until the time-of-day level exists, alerts fall back to
    the median of all hours ('all_hours'); the Pulse overlay shows no ratio.
    The caller holds the connection's lock.
    """
    models = list(models)
    if not models:
        return {}
    clock = datetime.fromtimestamp(now)
    context = (int(clock.weekday() >= 5), *((clock.hour + d) % 24 for d in (-1, 0, 1)))

    def level(row, scope):
        return {
            'baselineLoad': row['load'],
            'baselinePressure': row['pressure'],
            'baselineHours': row['n'] / 120,
            'baselineDays': row['days'],
            'baselineScope': scope,
        }

    result = {}
    for model, row in _medians(db, models, now, context).items():
        if row['n'] >= MIN_BASELINE_SAMPLES and row['days'] >= 3:
            result[model] = level(row, 'daytype_hour')
    rest = [model for model in models if model not in result]
    broad = _medians(db, rest, now, None) if rest else {}
    for model in rest:
        row = broad.get(model) or {'n': 0, 'days': 0}
        result[model] = (
            level(row, 'all_hours')
            if row['n'] >= MIN_BASELINE_SAMPLES
            else {
                'baselineLoad': None,
                'baselinePressure': None,
                'baselineHours': row['n'] / 120,
                'baselineDays': row['days'],
                'baselineScope': 'learning',
            }
        )
    return result


def current_row(row, observation, now):
    """Recompute a current five-minute signal without advancing alert state."""
    row = copy.deepcopy(row)
    at = observation.get('observedAt')
    row.update(
        observedAt=at,
        coverage=observation.get('n', 0) / 10,
        active=observation.get('active'),
        queued=observation.get('queued'),
        load=observation.get('load'),
        pressure=observation.get('pressure'),
        warmProviders=observation.get('warm'),
        loadRatio=None,
        pressureRatio=None,
    )
    fresh = at is not None and 0 <= now - at < FRESH_SECONDS and observation.get('n', 0) >= 8
    if not fresh:
        row.update(
            status='stale',
            detail='Waiting for fresh network samples covering at least 80% of the last five minutes.',
        )
    elif observation.get('minimum_warm', 0) < 1:
        row.update(
            status='no_headroom',
            detail='Warm-provider capacity was below one in this window; a per-provider pressure comparison is unavailable.',
        )
    elif row['baselineLoad'] is None:
        row.update(
            status='learning',
            detail='Learning model demand: at least two hours of earlier network observations with warm capacity are needed.',
        )
    elif row['baselineLoad'] <= 0 or row['baselinePressure'] <= 0:
        row.update(
            status='learning',
            detail='Earlier demand is zero; no finite spike ratio can be established yet.',
        )
    else:
        row['loadRatio'] = row['load'] / row['baselineLoad']
        row['pressureRatio'] = row['pressure'] / row['baselinePressure']
        if not _number(row['loadRatio']) or not _number(row['pressureRatio']):
            row.update(
                status='learning',
                loadRatio=None,
                pressureRatio=None,
                detail='Earlier demand is too small for a finite pressure comparison.',
            )
        else:
            candidate = (
                row['load'] >= SPIKE_MIN_LOAD
                and row['loadRatio'] >= SPIKE_RATIO
                and row['pressureRatio'] >= SPIKE_RATIO
            )
            row.update(
                status='watching' if candidate else 'normal',
                detail='Checking sustained concurrent active/queued demand and pressure per warm provider; these are not completed requests per minute.',
            )
    return row


class DemandAlerts:
    def __init__(self, history, store):
        self.h, self.store = history, store
        self.lock = threading.RLock()
        self.cached = None
        self.cache_key = None
        self.cache_at = None
        self.history_key = None
        self.history_at = None
        self.historical, self.local_evidence = {}, {}
        with self.h.lock:
            self.h.db.executescript("""
                CREATE TABLE IF NOT EXISTS demand_alert_state(
                    account TEXT,device TEXT,model TEXT,last_scan REAL,last_source REAL,
                    streak INTEGER NOT NULL DEFAULT 0,last_alert REAL,normal_since REAL,
                    armed INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(account,device,model));
                CREATE TABLE IF NOT EXISTS demand_alert_events(
                    id INTEGER PRIMARY KEY,account TEXT,device TEXT,model TEXT,at REAL,payload TEXT,
                    UNIQUE(account,device,model,at));
                CREATE INDEX IF NOT EXISTS demand_alert_scope
                    ON demand_alert_events(account,device,at);
            """)
            self.h.db.commit()

    def _observations(self, models, now, include_history=True):
        """Latest sample per 30s bucket; never count repeated polls as coverage.

        The usual level is computed in SQLite (one row per model), rather than
        passing a month of raw snapshots through the collector.
        """
        cutoff = int(now // 30) * 30
        start = cutoff - RECENT_SECONDS
        marks = ','.join('?' for _ in models)
        with self.h.lock:
            recent = [
                dict(row)
                for row in self.h.db.execute(
                    _samples(marks)
                    + """
                SELECT model,MAX(at) AS observedAt,COUNT(*) AS n,AVG(active) AS active,
                    AVG(queued) AS queued,AVG(load) AS load,AVG(warm) AS warm,
                    AVG(pressure) AS pressure,COUNT(pressure) AS pressure_n,MIN(warm) AS minimum_warm
                FROM samples GROUP BY model""",
                    (*models, start, cutoff, now),
                )
            ]
            usual = usual_levels(self.h.db, models, now) if include_history else {}
        return {row['model']: row for row in recent}, usual

    def _events(self, account, device, models):
        if not models:
            return []
        marks = ','.join('?' for _ in models)
        with self.h.lock:
            rows = self.h.db.execute(
                f"""SELECT id,payload FROM demand_alert_events
                WHERE account=? AND device=? AND model IN ({marks}) ORDER BY at DESC,id DESC LIMIT 20""",
                (account, device, *models),
            ).fetchall()
        return [{**json.loads(row['payload']), 'id': row['id']} for row in rows]

    def scan(self, account, device, eligible_models, current_models, now):
        with self.lock:
            return self._scan(account, device, eligible_models, current_models, now)

    def _scan(self, account, device, eligible_models, current_models, now):
        models = sorted(
            {model for model in eligible_models if isinstance(model, str) and 0 < len(model) <= 512}
        )[:128]
        current = set(current_models)
        key = (account, device, tuple(models), tuple(sorted(current)))
        if not account or not device:
            return self._empty(
                now, 'unavailable', 'Waiting for this Mac’s verified account and device identity.'
            )
        if (
            key == self.cache_key
            and self.cache_at is not None
            and 0 <= now - self.cache_at < SCAN_SECONDS
        ):
            return self.snapshot(account, device, now)
        if not models:
            result = self._empty(
                now, 'learning', 'No currently eligible supported models to compare.'
            )
            self.cache_key, self.cache_at, self.cached = key, now, result
            return copy.deepcopy(result)

        clock = datetime.fromtimestamp(now).astimezone()
        history_key = (
            account,
            device,
            tuple(models),
            clock.date(),
            clock.hour,
            clock.utcoffset(),
            clock.tzname(),
        )
        refresh_history = (
            history_key != self.history_key
            or self.history_at is None
            or not 0 <= now - self.history_at < 300
        )
        recent, historical = self._observations(models, now, refresh_history)
        if refresh_history:
            with read_view(self.store) as view:
                local = view.evidence(account, device, now - LOOKBACK, now, now)
            # Do not retain tens of thousands of minute rows just to display a
            # historical rate. Read-only evidence is refreshed every five minutes.
            self.local_evidence = {
                model: {'hours': values.get('hours'), 'usdPerHour': values.get('usdPerHour')}
                for model, values in local.items()
                if model in models
            }
            self.historical = historical
            self.history_key, self.history_at = history_key, now
        historical, local = self.historical, self.local_evidence
        output, new_ids, new_alerts = [], [], []
        with self.h.lock:
            states = {
                row['model']: dict(row)
                for row in self.h.db.execute(
                    """
                SELECT * FROM demand_alert_state WHERE account=? AND device=?""",
                    (account, device),
                )
            }
            for model in models:
                observation = recent.get(model) or {}
                baseline = historical[model]
                known = local.get(model) or {}
                hours = known.get('hours')
                rate = known.get('usdPerHour')
                observed_local = _number(hours) and hours >= 0.5 and _number(rate)
                at = observation.get('observedAt')
                row = {
                    'model': model,
                    'current': model in current,
                    'observedAt': at,
                    'coverage': observation.get('n', 0) / 10,
                    'active': observation.get('active'),
                    'queued': observation.get('queued'),
                    'load': observation.get('load'),
                    'pressure': observation.get('pressure'),
                    'warmProviders': observation.get('warm'),
                    **baseline,
                    'baselineAsOf': self.history_at,
                    'localEvidenceAsOf': self.history_at,
                    'loadRatio': None,
                    'pressureRatio': None,
                    'localWarmHours': hours if _number(hours) and hours >= 0 else 0,
                    'localRatePerHour': rate if observed_local else None,
                    'localEvidence': 'observed' if observed_local else 'untested',
                }
                row = current_row(row, observation, now)

                state = states.get(model) or {
                    'last_scan': None,
                    'last_source': None,
                    'streak': 0,
                    'last_alert': None,
                    'normal_since': None,
                    'armed': 1,
                }
                elapsed = now - state['last_scan'] if state['last_scan'] is not None else None
                # A second caller or restart in the same minute cannot advance
                # a streak, and a stale cached source cannot be replayed.
                due = elapsed is None or elapsed >= SCAN_SECONDS or elapsed < 0
                continuous = (
                    elapsed is not None
                    and SCAN_SECONDS <= elapsed <= 150
                    and at is not None
                    and state['last_source'] is not None
                    and at > state['last_source']
                )
                if due:
                    if not continuous:
                        state.update(streak=0, normal_since=None)
                    if row['status'] == 'watching':
                        state['streak'] = min(3, state['streak'] + 1)
                        state['normal_since'] = None
                    elif row['status'] == 'normal':
                        state['streak'] = 0
                        if state['normal_since'] is None:
                            state['normal_since'] = now
                        if now - state['normal_since'] >= REARM_SECONDS:
                            state['armed'] = 1
                    else:
                        state.update(streak=0, normal_since=None)
                    state['last_scan'], state['last_source'] = now, at
                    cooled = (
                        state['last_alert'] is None or now - state['last_alert'] >= COOLDOWN_SECONDS
                    )
                    if (
                        row['status'] == 'watching'
                        and state['streak'] >= 3
                        and state['armed']
                        and cooled
                    ):
                        payload = {
                            **row,
                            'at': now,
                            'status': 'spike',
                            'detail': 'Sustained network concurrency pressure spike. Local earnings are historical observations, not a forecast of this spike.',
                        }
                        inserted = self.h.db.execute(
                            """INSERT OR IGNORE INTO demand_alert_events(account,device,model,at,payload)
                            VALUES(?,?,?,?,?)""",
                            (account, device, model, now, json.dumps(payload, allow_nan=False)),
                        )
                        if inserted.rowcount:
                            new_ids.append(inserted.lastrowid)
                            new_alerts.append({**payload, 'id': inserted.lastrowid})
                        state.update(last_alert=now, armed=0)
                    self.h.db.execute(
                        """INSERT OR REPLACE INTO demand_alert_state
                        (account,device,model,last_scan,last_source,streak,last_alert,normal_since,armed)
                        VALUES(?,?,?,?,?,?,?,?,?)""",
                        (
                            account,
                            device,
                            model,
                            state['last_scan'],
                            state['last_source'],
                            state['streak'],
                            state['last_alert'],
                            state['normal_since'],
                            state['armed'],
                        ),
                    )
                if row['status'] == 'watching' and state['streak'] >= 3:
                    row.update(
                        status='spike',
                        detail='Sustained network concurrency pressure is elevated. This does not guarantee more work or earnings on this Mac.',
                    )
                row.update(
                    streak=state['streak'],
                    rearmed=bool(state['armed']),
                    cooldownUntil=state['last_alert'] + COOLDOWN_SECONDS
                    if state['last_alert'] is not None
                    else None,
                )
                output.append(row)
            self.h.db.commit()

        priority = {
            'spike': 0,
            'watching': 1,
            'normal': 2,
            'learning': 3,
            'no_headroom': 4,
            'stale': 5,
        }
        output.sort(
            key=lambda row: (
                priority[row['status']],
                row['localRatePerHour'] is None,
                -(row['localRatePerHour'] or 0),
                row['model'],
            )
        )
        result = {
            'at': now,
            'status': 'ready'
            if any(row['status'] in ('spike', 'watching', 'normal') for row in output)
            else 'stale'
            if all(row['status'] == 'stale' for row in output)
            else 'learning',
            'models': output,
            'alerts': self._events(account, device, models),
            'newAlertIds': [],
            'newAlerts': [],
            'scanSeconds': SCAN_SECONDS,
            'detail': 'Network concurrent active/queued demand and pressure per warm provider. This Mac’s recorded earnings stay separate; no model switches are made.',
        }
        self.cache_key, self.cache_at, self.cached = key, now, result
        return {**copy.deepcopy(result), 'newAlertIds': new_ids, 'newAlerts': new_alerts}

    @staticmethod
    def _empty(now, status, detail):
        return {
            'at': now,
            'status': status,
            'models': [],
            'alerts': [],
            'newAlertIds': [],
            'newAlerts': [],
            'scanSeconds': SCAN_SECONDS,
            'detail': detail,
        }

    def current(self, account, device, now):
        """Cheap read-only optimizer signal; alert/history cadence is unchanged."""
        with self.lock:
            result = self._snapshot(account, device, now)
            if not result['models']:
                return result
            recent, _ = self._observations([r['model'] for r in result['models']], now, False)
            rows = []
            for prior in self.cached['models']:
                row = current_row(prior, recent.get(prior['model']) or {}, now)
                if (
                    row['status'] == 'watching'
                    and prior.get('streak', 0) >= 3
                    and 0 <= now - self.cache_at <= 150
                ):
                    row.update(
                        status='spike',
                        detail='Current covered pressure remains elevated; alert cadence is unchanged.',
                    )
                rows.append(row)
            result.update(
                models=rows,
                status='ready'
                if any(r['status'] in ('normal', 'watching', 'spike') for r in rows)
                else 'stale'
                if all(r['status'] == 'stale' for r in rows)
                else 'learning',
            )
            return result

    def snapshot(self, account, device, now):
        with self.lock:
            return self._snapshot(account, device, now)

    def _snapshot(self, account, device, now):
        if self.cached is None or self.cache_key[:2] != (account, device):
            return self._empty(now, 'learning', 'Waiting for the first scoped model-demand scan.')
        result = copy.deepcopy(self.cached)
        result['newAlertIds'] = []
        result['newAlerts'] = []
        for row in result['models']:
            at = row['observedAt']
            if at is None or not 0 <= now - at < FRESH_SECONDS:
                row.update(
                    status='stale',
                    detail='Network observations are stale; waiting for fresh samples.',
                    streak=0,
                )
        if result['models'] and all(row['status'] == 'stale' for row in result['models']):
            result['status'] = 'stale'
        return result
