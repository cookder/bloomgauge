"""Persistent, account-scoped local history with bounded chart queries."""

import json, math, pathlib, sqlite3, threading, time, re
from datetime import datetime


def epoch(value):
    normalized = re.sub(
        r'\.(\d+)', lambda m: '.' + m.group(1)[:6].ljust(6, '0'), value.replace('Z', '+00:00')
    )
    return datetime.fromisoformat(normalized).timestamp()


class History:
    def __init__(self, path):
        self.lock = threading.RLock()
        if str(path) != ':memory:':
            pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;
        CREATE TABLE IF NOT EXISTS samples(at INTEGER PRIMARY KEY,tokens REAL,cpu REAL,gpu REAL,memory REAL,cpu_temp REAL,gpu_temp REAL);
        CREATE TABLE IF NOT EXISTS credits(account TEXT,id INTEGER,at REAL,model TEXT,micro_usd INTEGER,output_tokens INTEGER,PRIMARY KEY(account,id));
        CREATE INDEX IF NOT EXISTS credit_time ON credits(account,at DESC,id DESC);
        CREATE TABLE IF NOT EXISTS cache(key TEXT PRIMARY KEY,at REAL,data TEXT);
        CREATE TABLE IF NOT EXISTS network(at INTEGER PRIMARY KEY,seconds INTEGER,requests INTEGER,prompt_tokens INTEGER,completion_tokens INTEGER);
        """)
        self.pending = 0

    def save_sample(self, at, p, h):
        with self.lock:
            self.db.execute(
                'INSERT OR REPLACE INTO samples VALUES(?,?,?,?,?,?,?)',
                (
                    int(at),
                    p.get('tokensPerSecond'),
                    h.get('cpuPercent'),
                    h.get('gpuPercent'),
                    h.get('memoryUsedGB'),
                    h.get('cpuTemp'),
                    h.get('gpuTemp'),
                ),
            )
            self.pending += 1
            if self.pending >= 5:
                self.db.commit()
                self.pending = 0

    def chart(self, start, end, network=False):
        with self.lock:
            table = 'network' if network else 'samples'
            # Separate MIN/MAX subqueries are index lookups; together (or with COUNT) they scan every row.
            coverage = self.db.execute(
                f'SELECT (SELECT MIN(at) FROM {table}),(SELECT MAX(at) FROM {table})'
            ).fetchone()
            if coverage[0] is None:
                return {
                    'samples': [],
                    'coverageStart': None,
                    'coverageEnd': None,
                    'bucketSeconds': 1,
                    'count': 0,
                }
            low = max(start, coverage[0])
            high = min(end, coverage[1])
            step = max(1, math.ceil(max(0, high - low) / 600))
            if network:
                minimum = self.db.execute(
                    'SELECT MIN(seconds) FROM network WHERE at>=? AND at<=?', (start, end)
                ).fetchone()[0]
                step = max(step, minimum or 60)
                rows = self.db.execute(
                    'SELECT CAST((at-?)/? AS INT) AS bucket,AVG(at) AS at,SUM(requests)*60.0/SUM(seconds) AS requestsPerMinute,SUM(completion_tokens)*1.0/SUM(seconds) AS tokensPerSecond,MIN(at) AS first,MAX(at+seconds) AS last,COUNT(*) AS n FROM network WHERE at>=? AND at<=? GROUP BY bucket ORDER BY bucket',
                    (low, step, start, end),
                ).fetchall()
                keys = ['requestsPerMinute', 'tokensPerSecond']
            else:
                rows = self.db.execute(
                    'SELECT CAST((at-?)/? AS INT) AS bucket,AVG(at) AS at,AVG(tokens) AS tokensPerSecond,MAX(tokens) AS peakTokensPerSecond,AVG(cpu) AS cpuPercent,AVG(gpu) AS gpuPercent,AVG(memory) AS memoryUsedGB,AVG(cpu_temp)*1.8+32 AS cpuTempF,AVG(gpu_temp)*1.8+32 AS gpuTempF,MIN(at) AS first,MAX(at) AS last,COUNT(*) AS n FROM samples WHERE at>=? AND at<=? GROUP BY bucket ORDER BY bucket',
                    (low, step, start, end),
                ).fetchall()
                keys = [
                    'tokensPerSecond',
                    'peakTokensPerSecond',
                    'cpuPercent',
                    'gpuPercent',
                    'memoryUsedGB',
                    'cpuTempF',
                    'gpuTempF',
                ]
            output = []
            last = None
            for r in rows:
                if last is not None and r['first'] - last > max(5, step * 2):
                    output.append({'at': last + 1, **{k: None for k in keys}})
                output.append({'at': r['at'], **{k: r[k] for k in keys}})
                last = r['last']
            return {
                'samples': output,
                'coverageStart': coverage[0],
                'coverageEnd': coverage[1],
                'bucketSeconds': step,
                'count': sum(r['n'] for r in rows),
            }

    def save_monitor(self, account, data):
        previous = self.cache('monitor:' + account)
        merged = dict(data)
        if previous:
            hours = {h['at']: h for h in previous['hours']}
            hours.update({h['at']: h for h in data['hours']})
            merged['hours'] = sorted(hours.values(), key=lambda h: h['at'])
            starts = [
                x
                for x in [previous.get('coverageStartedAt'), data.get('coverageStartedAt')]
                if x is not None
            ]
            merged['coverageStartedAt'] = min(starts) if starts else None
            merged['gaps'] = max(previous.get('gaps', 0), data.get('gaps', 0))
        self.cache('monitor:' + account, merged)
        return merged

    def hourly_output(self, start, end):
        offset = datetime.fromtimestamp(end).astimezone().utcoffset().total_seconds() % 3600
        low = math.floor((start + offset) / 3600) * 3600 - offset
        high = math.ceil((end + offset) / 3600) * 3600 - offset
        with self.lock:
            coverage = self.db.execute(
                'SELECT (SELECT MIN(at) FROM samples),(SELECT MAX(at) FROM samples)'
            ).fetchone()
            if coverage[0] is None:
                return {
                    'samples': [],
                    'coverageStart': None,
                    'coverageEnd': None,
                    'bucketSeconds': 3600,
                    'count': 0,
                }
            low = max(low, math.floor((coverage[0] + offset) / 3600) * 3600 - offset)
            high = min(high, math.ceil((coverage[1] + 1 + offset) / 3600) * 3600 - offset)
            step = max(3600, math.ceil(max(0, high - low) / 3600 / 600) * 3600)
            rows = self.db.execute(
                'SELECT CAST((at-?)/? AS INT)*?+? AS at,SUM(tokens) AS outputTokens,COUNT(tokens) AS n FROM samples WHERE at>=? AND at<? GROUP BY CAST((at-?)/? AS INT) ORDER BY at',
                (low, step, step, low, low, high, low, step),
            ).fetchall()
            return {
                'samples': [dict(r) for r in rows],
                'coverageStart': coverage[0],
                'coverageEnd': coverage[1],
                'bucketSeconds': step,
                'count': sum(r['n'] for r in rows),
            }

    def save_credits(self, account, entries, observed_at=None):
        rows = [
            (
                account,
                x['id'],
                epoch(x['created_at']),
                x.get('model', 'Unknown'),
                x['amount_micro_usd'],
                x.get('completion_tokens', 0),
            )
            for x in entries
        ]
        with self.lock:
            known = set()
            ids = list({r[1] for r in rows})
            for offset in range(0, len(ids), 500):
                chunk = ids[offset : offset + 500]
                known.update(
                    r[0]
                    for r in self.db.execute(
                        'SELECT id FROM credits WHERE account=? AND id IN ('
                        + ','.join('?' for _ in chunk)
                        + ')',
                        (account, *chunk),
                    )
                )
            self.db.executemany('INSERT OR REPLACE INTO credits VALUES(?,?,?,?,?,?)', rows)
            self.db.commit()
            if observed_at is not None:
                # A latest-N poll covers its oldest result through the successful poll time.
                # Merge only overlapping polls; a missing interval is never treated as zero work.
                start = min((r[2] for r in rows), default=observed_at)
                previous = self.cache('credit-coverage:' + account)
                if previous and (start <= previous['end'] or not rows):
                    start = min(start, previous['start'])
                self.cache('credit-coverage:' + account, {'start': start, 'end': observed_at})
            return set(ids) - known

    def forecast_inputs(self, account, hour, now, earnings_at):
        with self.lock:
            minutes = [
                dict(r)
                for r in self.db.execute(
                    'SELECT CAST(at/60 AS INT)*60 AS at,AVG(tokens) AS rate,COUNT(tokens) AS n FROM samples WHERE at>=? AND at<? GROUP BY CAST(at/60 AS INT) ORDER BY at',
                    (hour - 21600, now),
                ).fetchall()
            ]
            coverage = self.cache('credit-coverage:' + account)
            recent = []
            if coverage and earnings_at and now - coverage['end'] <= 180:
                earnings_at = min(earnings_at, coverage['end'])
                for window in (300, 900):
                    start = max(coverage['start'], earnings_at - window)
                    seconds = earnings_at - start
                    if seconds < 120:
                        continue
                    row = self.db.execute(
                        "SELECT COALESCE(SUM(micro_usd),0)/1000000.0 AS usd,COUNT(*) AS jobs FROM credits WHERE account=? AND at>=? AND at<? AND model!='base_reward'",
                        (account, start, earnings_at),
                    ).fetchone()
                    recent.append(
                        {
                            'window': window,
                            'seconds': seconds,
                            'usdPerSecond': row['usd'] / seconds,
                            'jobsPerSecond': row['jobs'] / seconds,
                        }
                    )
            return minutes, recent

    def credits(self, account, start, end, page, limit, sort='newest', category='all', model=None):
        orders = {
            'newest': 'at DESC,id DESC',
            'oldest': 'at ASC,id ASC',
            'amount-desc': 'micro_usd DESC,at DESC,id DESC',
            'amount-asc': 'micro_usd ASC,at DESC,id DESC',
            'tokens-desc': 'COALESCE(output_tokens,0) DESC,at DESC,id DESC',
        }
        if (
            not isinstance(sort, str)
            or sort not in orders
            or not isinstance(category, str)
            or category not in ('all', 'inference', 'base_reward')
            or type(page) is not int
            or not 1 <= page <= 100000
            or type(limit) is not int
            or not 1 <= limit <= 250
            or type(start) not in (int, float)
            or type(end) not in (int, float)
            or not math.isfinite(start)
            or not math.isfinite(end)
            or start < 0
            or end <= start
            or model is not None
            and (
                not isinstance(model, str)
                or not 1 <= len(model) <= 512
                or any(ord(c) < 32 for c in model)
            )
        ):
            raise ValueError('Choose a supported credit range, sort and filter.')
        where = 'account=? AND at>=? AND at<=?'
        args = [account, start, end]
        if category == 'base_reward':
            where += " AND model='base_reward'"
        elif category == 'inference':
            where += " AND (model IS NULL OR model!='base_reward')"
        if model is not None:
            where += ' AND model=?'
            args.append(model)
        with self.lock:
            summary = self.db.execute(
                'SELECT COUNT(*) AS count,COALESCE(SUM(micro_usd),0)/1e6 AS totalUsd,'
                "COALESCE(SUM(CASE WHEN model='base_reward' THEN 0 ELSE micro_usd END),0)/1e6 AS inferenceUsd,"
                "COALESCE(SUM(CASE WHEN model='base_reward' THEN micro_usd ELSE 0 END),0)/1e6 AS baseRewardUsd,"
                'AVG(micro_usd)/1e6 AS averageUsd,MIN(micro_usd)/1e6 AS minUsd,MAX(micro_usd)/1e6 AS maxUsd,'
                'COALESCE(SUM(output_tokens),0) AS outputTokens FROM credits WHERE ' + where,
                args,
            ).fetchone()
            rows = self.db.execute(
                'SELECT * FROM credits WHERE '
                + where
                + ' ORDER BY '
                + orders[sort]
                + ' LIMIT ? OFFSET ?',
                (*args, limit, (page - 1) * limit),
            ).fetchall()
            leaders = self.db.execute(
                'SELECT model,COUNT(*) AS count,COALESCE(SUM(micro_usd),0)/1e6 AS usd,'
                'COALESCE(SUM(output_tokens),0) AS outputTokens FROM credits WHERE '
                + where
                + ' GROUP BY model ORDER BY SUM(micro_usd) DESC,model ASC',
                args,
            ).fetchall()
            options = self.db.execute(
                'SELECT DISTINCT model FROM credits WHERE account=? AND at>=? AND at<=? AND model IS NOT NULL ORDER BY model',
                (account, start, end),
            ).fetchall()
            coverage = self.db.execute(
                'SELECT MIN(at),MAX(at) FROM credits WHERE account=?', (account,)
            ).fetchone()
            return {
                'count': summary['count'],
                'page': page,
                'limit': limit,
                'coverageStart': coverage[0],
                'coverageEnd': coverage[1],
                'entries': [
                    {
                        'id': r['id'],
                        'at': datetime.fromtimestamp(r['at']).astimezone().isoformat(),
                        'model': r['model'],
                        'usd': r['micro_usd'] / 1e6,
                        'outputTokens': r['output_tokens'],
                    }
                    for r in rows
                ],
                'scope': 'account',
                'sort': sort,
                'category': category,
                'model': model,
                'models': [r['model'] for r in options],
                'modelOptions': [r['model'] for r in options],
                'summary': dict(summary),
                'leaderboard': [dict(r) for r in leaders],
            }

    def save_network_series(self, data):
        seconds = data['bucket_seconds']
        rows = [
            (
                int(epoch(x['timestamp'])),
                seconds,
                x['requests'],
                x['prompt_tokens'],
                x['completion_tokens'],
            )
            for x in data['time_series']
        ]
        with self.lock:
            # Replace only same-or-finer buckets, so historical backfill preserves finer live data.
            for row in rows:
                overlap = self.db.execute(
                    'SELECT MIN(seconds) FROM network WHERE at>=? AND at<?',
                    (row[0], row[0] + seconds),
                ).fetchone()[0]
                if overlap is not None and overlap < seconds:
                    continue
                self.db.execute(
                    'DELETE FROM network WHERE at<? AND at+seconds>?', (row[0] + seconds, row[0])
                )
                self.db.execute('INSERT OR REPLACE INTO network VALUES(?,?,?,?,?)', row)
            self.db.commit()

    def cache(self, key, data=None):
        with self.lock:
            if data is not None:
                self.db.execute(
                    'INSERT OR REPLACE INTO cache VALUES(?,?,?)',
                    (key, time.time(), json.dumps(data)),
                )
                self.db.commit()
                return data
            row = self.db.execute('SELECT data FROM cache WHERE key=?', (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def close(self):
        with self.lock:
            self.db.commit()
            self.db.close()
