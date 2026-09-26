"""Device-scoped evidence for model selection. Never backfills invented run time."""

import hashlib, math, sqlite3, statistics
from datetime import datetime
from history import epoch
from model_combinations import members


OPTIMIZER_TABLES = (
    'opt_identity',
    'opt_credits',
    'workload_tokens',
    'opt_coverage',
    'opt_minutes',
    'opt_ready_minutes',
    'opt_residency',
    'opt_network',
    'opt_events',
)


def device_id(state):
    value = state.get('attestation_public_key')
    return hashlib.sha256(value.encode()).hexdigest() if isinstance(value, str) and value else ''


def context(at):
    d = datetime.fromtimestamp(at)
    return (int(d.weekday() >= 5), d.hour // 4)


# Readings arrive about every 15 s but can run late when the provider CLI is
# slow. Within one verified warm session, a gap up to this long is still
# continuous warm time; a restart changes the session and breaks continuity.
CONTINUITY_SECONDS = 35


class OptimizerStore:
    def __init__(self, history):
        self.h = history
        with history.lock:
            history.db.executescript("""
            CREATE TABLE IF NOT EXISTS opt_identity(device TEXT,provider TEXT,PRIMARY KEY(device,provider));
            CREATE TABLE IF NOT EXISTS opt_credits(account TEXT,id INTEGER,provider TEXT,at REAL,model TEXT,micro_usd INTEGER,tokens INTEGER,PRIMARY KEY(account,id));
            CREATE INDEX IF NOT EXISTS opt_credit_time ON opt_credits(account,at);
            CREATE TABLE IF NOT EXISTS workload_tokens(account TEXT,id INTEGER,prompt_tokens INTEGER,output_tokens INTEGER,PRIMARY KEY(account,id));
            CREATE TABLE IF NOT EXISTS opt_coverage(account TEXT,start REAL,end REAL,PRIMARY KEY(account,start));
            CREATE TABLE IF NOT EXISTS opt_minutes(account TEXT,device TEXT,at INTEGER,model TEXT,seconds REAL,jobs REAL,tokens REAL,busy REAL,PRIMARY KEY(account,device,at,model));
            CREATE TABLE IF NOT EXISTS opt_ready_minutes(account TEXT,device TEXT,at INTEGER,model TEXT,seconds REAL,jobs REAL,tokens REAL,busy REAL,PRIMARY KEY(account,device,at,model));
            CREATE TABLE IF NOT EXISTS opt_residency(account TEXT,device TEXT,at INTEGER,model TEXT,seconds REAL,PRIMARY KEY(account,device,at,model));
            CREATE TABLE IF NOT EXISTS opt_network(at INTEGER,model TEXT,active REAL,queued REAL,warm REAL,routable REAL,PRIMARY KEY(model,at));
            CREATE INDEX IF NOT EXISTS opt_network_time ON opt_network(at);
            CREATE TABLE IF NOT EXISTS opt_events(id INTEGER PRIMARY KEY,account TEXT,device TEXT,at REAL,kind TEXT,model TEXT,detail TEXT,downtime REAL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS opt_event_scope ON opt_events(account,device,at);
            """)
        self.refresh_statistics()

    def refresh_statistics(self):
        # Statistics taken while tables were small make SQLite drive queries from
        # the wrong table (a 30-day evidence read took ~13 s). optimize re-analyzes
        # only tables whose size changed about tenfold; the limit bounds each scan.
        with self.h.lock:
            self.h.db.execute('PRAGMA analysis_limit=1000')
            if sqlite3.sqlite_version_info >= (3, 46):
                self.h.db.execute('PRAGMA optimize=0x10002').fetchall()
            else:
                # Older SQLite (a system Python when running from source) ignores
                # 0x10000 and skips tables this connection hasn't queried yet.
                for table in OPTIMIZER_TABLES:
                    self.h.db.execute('ANALYZE ' + table)
            self.h.db.commit()

    def identity(self, device, provider):
        if not device or not provider:
            return
        with self.h.lock:
            self.h.db.execute('INSERT OR IGNORE INTO opt_identity VALUES(?,?)', (device, provider))
            self.h.db.commit()

    def credits(self, account, entries, now):
        rows = [
            (
                account,
                x['id'],
                x.get('provider_id', ''),
                epoch(x['created_at']),
                x.get('model', 'Unknown'),
                x['amount_micro_usd'],
                x.get('completion_tokens', 0),
            )
            for x in entries
        ]
        with self.h.lock:
            self.h.db.executemany('INSERT OR REPLACE INTO opt_credits VALUES(?,?,?,?,?,?,?)', rows)
            from workload import token_count

            self.h.db.executemany(
                """INSERT INTO workload_tokens VALUES(?,?,?,?) ON CONFLICT(account,id) DO UPDATE SET
              prompt_tokens=COALESCE(excluded.prompt_tokens,workload_tokens.prompt_tokens),
              output_tokens=COALESCE(excluded.output_tokens,workload_tokens.output_tokens)""",
                [
                    (
                        account,
                        x['id'],
                        token_count(x.get('prompt_tokens')),
                        token_count(x.get('completion_tokens')),
                    )
                    for x in entries
                ],
            )
            # Overlapping latest-N polls establish continuous coverage, including idle time.
            start = min((r[3] for r in rows), default=now)
            overlaps = self.h.db.execute(
                'SELECT start,end FROM opt_coverage WHERE account=? AND end>=? AND start<=?',
                (account, start, now),
            ).fetchall()
            if not rows:
                # Consecutive empty account responses include observed idle time.
                overlaps = self.h.db.execute(
                    'SELECT start,end FROM opt_coverage WHERE account=? ORDER BY end DESC LIMIT 1',
                    (account,),
                ).fetchall()
            end = now
            if overlaps:
                start = min(start, min(r['start'] for r in overlaps))
                end = max(end, max(r['end'] for r in overlaps))
                self.h.db.execute(
                    'DELETE FROM opt_coverage WHERE account=? AND end>=? AND start<=?',
                    (account, start, end),
                )
            self.h.db.execute(
                'INSERT OR REPLACE INTO opt_coverage VALUES(?,?,?)', (account, start, end)
            )
            self.h.db.commit()

    def sample(self, account, device, start, end, model, jobs, tokens, busy, all_warm=False):
        # Explicit proof is supplied only after both daemon endpoints pass the
        # shared readiness gate. Old opt_minutes have no such provenance.
        if (
            not all_warm
            or not account
            or not device
            or not model
            or not 0 < end - start <= CONTINUITY_SECONDS
        ):
            return
        duration = end - start
        with self.h.lock:
            while start < end:
                minute = int(start // 60) * 60
                stop = min(end, minute + 60)
                seconds = stop - start
                fraction = seconds / duration
                self.h.db.execute(
                    """INSERT INTO opt_ready_minutes VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(account,device,at,model) DO UPDATE SET
                seconds=seconds+excluded.seconds,jobs=jobs+excluded.jobs,tokens=tokens+excluded.tokens,busy=busy+excluded.busy""",
                    (
                        account,
                        device,
                        minute,
                        model,
                        seconds,
                        jobs * fraction,
                        tokens * fraction,
                        seconds * bool(busy),
                    ),
                )
                if len(members(model)) == 2 and all_warm:
                    self.h.db.execute(
                        """INSERT INTO opt_residency VALUES(?,?,?,?,?) ON CONFLICT(account,device,at,model)
                        DO UPDATE SET seconds=seconds+excluded.seconds""",
                        (account, device, minute, model, seconds),
                    )
                start = stop

    def network(self, at, models):
        rows = []
        for m in models:
            values = [
                m.get(k)
                for k in (
                    'active_requests',
                    'queued_requests',
                    'warm_providers',
                    'routable_providers',
                )
            ]
            if isinstance(m.get('id'), str) and all(
                isinstance(v, (int, float))
                and not isinstance(v, bool)
                and math.isfinite(v)
                and v >= 0
                for v in values
            ):
                rows.append((int(at), m['id'], *values))
        with self.h.lock:
            self.h.db.executemany('INSERT OR IGNORE INTO opt_network VALUES(?,?,?,?,?,?)', rows)
            self.h.db.commit()

    def event(self, account, device, at, kind, model, detail, downtime=0):
        with self.h.lock:
            self.h.db.execute(
                'INSERT INTO opt_events(account,device,at,kind,model,detail,downtime) VALUES(?,?,?,?,?,?,?)',
                (account, device, at, kind, model, detail, max(0, downtime)),
            )
            self.h.db.commit()

    def evidence(self, account, device, start, end, now):
        # A 2-minute settlement lag plus verified poll coverage prevents a fresh, unpaid
        # minute from becoming evidence of zero revenue. Require a complete warm
        # minute so a cold boundary cannot contribute its whole-minute credits.
        end = min(end, now - 120)
        with self.h.lock:
            db = self.h.db
            rows = db.execute(
                """SELECT m.* FROM opt_ready_minutes m WHERE account=? AND device=? AND at>=? AND at+60<=? AND seconds>=59.999999 AND seconds<=60.000001
              AND EXISTS(SELECT 1 FROM opt_coverage c WHERE c.account=m.account AND c.start<=m.at AND c.end>=m.at+60) ORDER BY at""",
                (account, device, start, end),
            ).fetchall()
            # A provider list built once, not a JOIN or EXISTS: with stale statistics
            # those loop every known provider per credit or per window (~13 s / 30 days).
            credits = db.execute(
                """SELECT CAST(c.at/60 AS INT)*60 AS at,c.model,SUM(c.micro_usd)/1000000.0 AS usd,COUNT(*) AS paidJobs
              FROM opt_credits c WHERE c.account=? AND c.at>=? AND c.at<? AND c.model!='base_reward'
              AND c.provider IN (SELECT provider FROM opt_identity WHERE device=?)
              GROUP BY CAST(c.at/60 AS INT),c.model""",
                (account, start, end, device),
            ).fetchall()
            delays = db.execute(
                """SELECT model,SUM(downtime) AS seconds FROM opt_events WHERE account=? AND device=? AND at>=? AND at<? GROUP BY model""",
                (account, device, start, end),
            ).fetchall()
            residency = {
                (r['at'], r['model']): r['seconds']
                for r in db.execute(
                    'SELECT at,model,seconds FROM opt_residency WHERE account=? AND device=? AND at>=? AND at<?',
                    (account, device, start, end),
                )
            }
        paid = {(r['at'], r['model']): r for r in credits}
        groups = {}
        for r in rows:
            # The nearly complete minute is the denominator. Include actual credited
            # inference revenue for that exact provider, concrete model and minute only.
            g = groups.setdefault(
                r['model'],
                {
                    'seconds': 0,
                    'usd': 0,
                    'jobs': 0,
                    'localJobs': 0,
                    'tokens': 0,
                    'busy': 0,
                    'dates': set(),
                    'blocks': {},
                    'contexts': {},
                    'minutes': [],
                    'warmSeconds': 0,
                    'perModel': {},
                },
            )
            p = {'usd': 0, 'paidJobs': 0}
            for model in members(r['model']):
                credit = paid.get((r['at'], model), {'usd': 0, 'paidJobs': 0})
                p['usd'] += credit['usd']
                p['paidJobs'] += credit['paidJobs']
                share = g['perModel'].setdefault(model, {'usd': 0, 'jobs': 0})
                share['usd'] += credit['usd']
                share['jobs'] += credit['paidJobs']
            g['warmSeconds'] += min(r['seconds'], residency.get((r['at'], r['model']), 0))
            g['seconds'] += r['seconds']
            g['usd'] += p['usd']
            g['jobs'] += p['paidJobs']
            g['localJobs'] += r['jobs']
            g['tokens'] += r['tokens']
            g['busy'] += r['busy']
            g['dates'].add(datetime.fromtimestamp(r['at']).date().isoformat())
            block = int(r['at'] // 7200)
            g['blocks'][block] = g['blocks'].get(block, 0) + r['seconds']
            key = context(r['at'])
            c = g['contexts'].setdefault(key, {'seconds': 0, 'usd': 0})
            c['seconds'] += r['seconds']
            c['usd'] += p['usd']
            g['minutes'].append(
                {
                    'at': r['at'],
                    'usd': p['usd'],
                    'seconds': r['seconds'],
                    'paidJobs': p['paidJobs'],
                    'requests': r['jobs'],
                    'tokens': r['tokens'],
                    'busy': r['busy'],
                }
            )
        costs = {r['model']: r['seconds'] for r in delays}
        output = {}
        for model, g in groups.items():
            seconds = g['seconds']
            cost = costs.get(model, 0)
            rate = g['usd'] * 3600 / seconds
            c = g['contexts'].get(context(now), {'seconds': 0, 'usd': 0})
            weight = min(0.5, c['seconds'] / 14400) if c['seconds'] >= 3600 else 0
            contextual = c['usd'] * 3600 / c['seconds'] if c['seconds'] else rate
            full_blocks = sum(s >= 5400 for s in g['blocks'].values())
            tested = seconds >= 21600 and len(g['dates']) >= 2 and full_blocks >= 3
            eligible = tested and g['jobs'] >= 20
            output[model] = {
                'hours': seconds / 3600,
                'usd': g['usd'],
                'jobs': g['jobs'],
                'localJobs': round(g['localJobs']),
                'tokens': round(g['tokens']),
                'usdPerHour': rate,
                'jobsPerHour': g['jobs'] * 3600 / seconds,
                'tokensPerSecond': g['tokens'] / seconds,
                'busyPercent': g['busy'] * 100 / seconds,
                'days': len(g['dates']),
                'blocks': full_blocks,
                'switchMinutes': cost / 60,
                'tested': tested,
                'eligible': eligible,
                'score': (1 - weight) * rate + weight * contextual,
                'contextHours': c['seconds'] / 3600,
                'bothWarmPercent': g['warmSeconds'] * 100 / seconds
                if len(members(model)) == 2
                else None,
                'perModel': g['perModel'],
                'timeSlots': [
                    {
                        'weekend': bool(k[0]),
                        'hour': k[1] * 4,
                        'hours': v['seconds'] / 3600,
                        'usdPerHour': v['usd'] * 3600 / v['seconds'],
                    }
                    for k, v in sorted(g['contexts'].items())
                ],
                'minutes': g['minutes'],
            }
        return output

    def summary(self, account, device, start, end, now):
        evidence = self.evidence(account, device, start, end, now)
        with self.h.lock:
            rows = self.h.db.execute(
                """SELECT model,AVG(active) AS active,AVG(queued) AS queued,AVG(warm) AS warm,
                AVG((active+queued)/MAX(1,warm)) AS pressure,COUNT(*) AS samples,MIN(at) AS first,MAX(at) AS last
                FROM opt_network WHERE at>=? AND at<=? GROUP BY model""",
                (start, end),
            ).fetchall()
            recent = self.h.db.execute(
                """SELECT model,AVG(active+queued) AS recentLoad,AVG((active+queued)/MAX(1,warm)) AS recentPressure,COUNT(*) AS recentSamples,
                MAX(at)-MIN(at) AS recentSpan FROM opt_network WHERE at>=? AND at<=? GROUP BY model""",
                (now - 1800, now),
            ).fetchall()
            events = self.h.db.execute(
                'SELECT at,kind,model,detail,downtime FROM opt_events WHERE account=? AND device=? ORDER BY at DESC LIMIT 40',
                (account, device),
            ).fetchall()
            coverage = self.h.db.execute(
                'SELECT MIN(at),MAX(at) FROM opt_ready_minutes WHERE account=? AND device=?',
                (account, device),
            ).fetchone()
        demand = {r['model']: dict(r) for r in rows}
        for r in recent:
            if r['model'] in demand:
                demand[r['model']].update(dict(r))
        return evidence, demand, [dict(r) for r in events], list(coverage)

    def chart(self, account, device, model, start, end, now):
        evidence = self.evidence(account, device, start, end, now).get(model, {})
        minutes = evidence.get('minutes', [])
        with self.h.lock:
            rows = self.h.db.execute(
                'SELECT at,active,queued,warm FROM opt_network WHERE model=? AND at>=? AND at<=? ORDER BY at',
                (model, start, end),
            ).fetchall()
        all_times = [r['at'] for r in rows] + [r['at'] for r in minutes]
        if not all_times:
            return {
                'samples': [],
                'bucketSeconds': 60,
                'coverageStart': None,
                'coverageEnd': None,
                'count': 0,
            }
        low, high = min(all_times), max(all_times)
        step = max(60, math.ceil((high - low) / 300 / 60) * 60)
        groups = {}
        for r in rows:
            key = int(r['at'] // step) * step
            g = groups.setdefault(key, {'load': [], 'pressure': [], 'usd': 0, 'seconds': 0})
            g['load'].append(r['active'] + r['queued'])
            g['pressure'].append((r['active'] + r['queued']) / max(1, r['warm']))
        for r in minutes:
            key = int(r['at'] // step) * step
            g = groups.setdefault(key, {'load': [], 'pressure': [], 'usd': 0, 'seconds': 0})
            g['usd'] += r['usd']
            g['seconds'] += r['seconds']
        samples = []
        previous = None
        for at, g in sorted(groups.items()):
            if previous is not None and at - previous > step * 2:
                samples.append(
                    {
                        'at': previous + step,
                        'activeRequests': None,
                        'pressure': None,
                        'usdPerHour': None,
                    }
                )
            samples.append(
                {
                    'at': at,
                    'activeRequests': statistics.mean(g['load']) if g['load'] else None,
                    'pressure': statistics.mean(g['pressure']) if g['pressure'] else None,
                    'usdPerHour': g['usd'] * 3600 / g['seconds'] if g['seconds'] else None,
                }
            )
            previous = at
        return {
            'samples': samples,
            'bucketSeconds': step,
            'coverageStart': low,
            'coverageEnd': high,
            'count': len(all_times),
        }
