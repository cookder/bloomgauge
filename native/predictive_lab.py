"""Local shadow forecasts. No commands, policy changes, or synthetic history.

Five-minute network observations predict subsequent mean load/warm (concurrency,
not arrivals). Ridge regression learns residuals around persistence. Every fit
uses only targets that ended before its origin. Prospective records are immutable.
"""

import json
import math
import statistics
import threading
from collections import defaultdict
from datetime import datetime
from demand_baselines import read_view, network_minutes, near, repeatable, summary
from model_combinations import members

VERSION = 'demand-ridge-shadow-v1'
STEP = 300
HORIZONS = (3, 12)
LOOKBACK = 14 * 86400


def finite(v):
    return type(v) in (int, float) and math.isfinite(v)


def mean(values):
    return sum(values) / len(values) if values else None


def series(store, start, end):
    """Last reading per cadence slot; no forward fill across missing periods."""
    with store.h.lock:
        rows = store.h.db.execute(
            """WITH slots AS (
          SELECT model,CAST(at/30 AS INTEGER) slot,MAX(at) at FROM opt_network
          WHERE at>=? AND at<? GROUP BY model,slot)
          SELECT n.* FROM slots s JOIN opt_network n ON n.model=s.model AND n.at=s.at
          ORDER BY n.model,n.at""",
            (start, end),
        ).fetchall()
    groups = defaultdict(list)
    for r in rows:
        if all(finite(r[k]) and r[k] >= 0 for k in ('active', 'queued', 'warm')) and r['warm'] > 0:
            groups[(r['model'], int(r['at'] // STEP) * STEP)].append(r)
    out = defaultdict(dict)
    for (model, at), rows in groups.items():
        if (
            at + STEP > end
            or len(rows) < 8
            or rows[0]['at'] - at > 60
            or at + STEP - rows[-1]['at'] > 60
        ):
            continue
        if any(b['at'] - a['at'] > 90 for a, b in zip(rows, rows[1:])):
            continue
        out[model][at + STEP] = {
            'pressure': mean([(r['active'] + r['queued']) / r['warm'] for r in rows]),
            **{k: mean([r[k] for r in rows]) for k in ('active', 'queued', 'warm')},
        }
    return dict(out)


def window(points, at, n):
    keys = [at - i * STEP for i in range(n)]
    if any(k not in points for k in keys):
        return None
    return [points[k]['pressure'] for k in keys]


def features(points, at):
    recent = window(points, at, 12)
    if recent is None:
        return None
    logs = [math.log1p(v) for v in recent]
    clock = datetime.fromtimestamp(at)
    hour = 2 * math.pi * (clock.hour + clock.minute / 60) / 24
    day = 2 * math.pi * clock.weekday() / 7
    baseline = mean(recent[:3])
    return [
        1.0,
        mean(logs[:3]),
        mean(logs[3:6]),
        mean(logs[6:]),
        logs[0] - logs[2],
        math.sin(hour),
        math.cos(hour),
        math.sin(day),
        math.cos(day),
    ], baseline


def outcome(points, origin, horizon):
    values = [points.get(origin + i * STEP, {}).get('pressure') for i in range(1, horizon + 1)]
    return mean(values) if all(finite(v) for v in values) else None


def solve(matrix, target):
    a = [list(row) + [y] for row, y in zip(matrix, target)]
    for i in range(len(a)):
        pivot = max(range(i, len(a)), key=lambda j: abs(a[j][i]))
        a[i], a[pivot] = a[pivot], a[i]
        if abs(a[i][i]) < 1e-12:
            return None
        divisor = a[i][i]
        a[i] = [v / divisor for v in a[i]]
        for j in range(len(a)):
            if j != i:
                factor = a[j][i]
                a[j] = [v - factor * w for v, w in zip(a[j], a[i])]
    return [row[-1] for row in a]


def predict(points, origin, horizon):
    feature = features(points, origin)
    if feature is None:
        return None
    x, baseline = feature
    n = len(x)
    matrix = [[10.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
    target = [0.0] * n
    training = []
    # Non-overlapping hourly training origins limit pseudo-replication. The
    # latest training target must be fully observed by this forecast's origin.
    for at in sorted(points):
        if at % 3600 or not origin - 7 * 86400 <= at <= origin - horizon * STEP:
            continue
        item = features(points, at)
        actual = outcome(points, at, horizon)
        if item is None or actual is None:
            continue
        values, old = item
        y = math.log1p(actual) - math.log1p(old)
        training.append(at)
        for i in range(n):
            target[i] += values[i] * y
            for j in range(n):
                matrix[i][j] += values[i] * values[j]
    supported = (
        len(training) >= 48 and len({datetime.fromtimestamp(t).date() for t in training}) >= 3
    )
    beta = solve(matrix, target) if supported else None
    value = (
        max(
            0.0,
            math.expm1(
                max(-20.0, min(20.0, math.log1p(baseline) + sum(a * b for a, b in zip(x, beta))))
            ),
        )
        if beta
        else None
    )
    # Persistence remains a visible independent baseline, never relabeled as AI.
    return {
        'minutes': horizon * 5,
        'prediction': value,
        'baseline': baseline,
        'trainingWindows': len(training),
        'trainedThrough': max((t + horizon * STEP for t in training), default=None),
        'status': 'shadow' if value is not None else 'learning',
    }


def evaluation(records, all_points, now):
    scores = {
        m: {'minutes': m, 'count': 0, 'modelError': 0.0, 'baselineError': 0.0} for m in (15, 60)
    }
    seen = set()
    for packet in records:
        origin = packet['origin']
        # Evaluate hourly checkpoints only; horizon outcomes do not overlap.
        if origin % 3600:
            continue
        for row in packet['models']:
            for forecast in row['forecasts']:
                mins = forecast['minutes']
                key = (origin, row['id'], mins)
                if key in seen or forecast['prediction'] is None or now < origin + mins * 60 + 120:
                    continue
                seen.add(key)
                actual = outcome(all_points.get(row['id'], {}), origin, mins // 5)
                if actual is None:
                    continue
                s = scores[mins]
                s['count'] += 1
                s['modelError'] += abs(actual - forecast['prediction'])
                s['baselineError'] += abs(actual - forecast['baseline'])
    result = []
    for s in scores.values():
        n = s['count']
        result.append(
            {
                'minutes': s['minutes'],
                'windows': n,
                'mae': s['modelError'] / n if n else None,
                'baselineMAE': s['baselineError'] / n if n else None,
                'improvementPercent': 100 * (1 - s['modelError'] / s['baselineError'])
                if n and s['baselineError'] > 0
                else None,
            }
        )
    return result


def portfolio(evidence):
    """Exact serving sets; never sum solo rates or invent pair covariance."""
    rows = []
    for selection, e in evidence.items():
        groups = defaultdict(list)
        for m in e['minutes']:
            groups[int(m['at'] // 900) * 900].append(m)
        blocks = [
            {'at': at, 'rate': sum(m['usd'] for m in ms) * 4}
            for at, ms in groups.items()
            if len({m['at'] for m in ms}) == 15
        ]
        rates = [b['rate'] for b in blocks]
        days = len({datetime.fromtimestamp(b['at']).date() for b in blocks})
        repeated = len(blocks) >= 24 and days >= 3
        rows.append(
            {
                'id': selection,
                'members': members(selection),
                'hours': e['hours'],
                'usd': e['usd'],
                'observedUSDPerHour': e['usdPerHour'],
                'blocks': len(blocks),
                'days': days,
                'stddevUSDPerHour': statistics.stdev(rates) if repeated else None,
                'belowTargetPercent': 100 * sum(v < 0.12 for v in rates) / len(rates)
                if rates
                else None,
                'status': 'measured' if repeated else 'limited',
                'perModel': e['perModel'],
            }
        )
    return sorted(rows, key=lambda r: (-len(r['members']), -r['hours']))


def income_scenario(evidence, network, pressure, warm, origin):
    """Conditional paid evidence, not a linear demand-to-money extrapolation."""
    if not finite(pressure) or not finite(warm) or warm < 1:
        return None
    exposure, overlap = defaultdict(int), defaultdict(int)
    for m in evidence.get('minutes', []):
        block = int(m['at'] // 1800)
        exposure[block] += 1
        overlap[block] += m['at'] in network
    rows = [
        {**m, **network[m['at']]}
        for m in evidence.get('minutes', [])
        if m['at'] in network
        and m['at'] + 60 <= origin - 120
        and overlap[int(m['at'] // 1800)] >= 0.8 * exposure[int(m['at'] // 1800)]
        and near(network[m['at']]['pressure'], pressure)
        and near(network[m['at']]['warm'], warm)
    ]
    counts = defaultdict(int)
    for m in rows:
        counts[int(m['at'] // 1800)] += 1
    rows = [m for m in rows if counts[int(m['at'] // 1800)] >= 15]
    result = summary(rows)
    supported = (
        repeatable(rows, 4, 3, 8)
        and result['paidJobs'] >= 200
        and origin - (result['asOf'] or 0) <= 7 * 86400
    )
    return {
        'usdPerWarmHour': result['usdPerHour'] if supported else None,
        'observedHours': result['hours'],
        'dates': result['days'],
        'paidJobs': result['paidJobs'],
        'status': 'conditional' if supported else 'limited',
        'assumption': 'If predicted load/warm occurs and warm-provider supply stays comparable. Historical paid pace; excludes switching and cold time.',
    }


class PredictiveLab:
    def __init__(self, store, enabled=False):
        self.store = store
        self.enabled = enabled
        self.lock = threading.RLock()
        self.next_record = 0
        if enabled:
            with store.h.lock:
                store.h.db.execute("""CREATE TABLE IF NOT EXISTS predictive_observations(
                    account TEXT,device TEXT,origin INTEGER,version TEXT,data TEXT,
                    PRIMARY KEY(account,device,origin,version))""")
                store.h.db.commit()

    def record(self, account, device, now, available):
        if not self.enabled or not account or not device:
            return
        origin = int(now // 900) * 900
        with self.lock:
            if origin < self.next_record or now - origin > 90:
                return
            with self.store.h.lock:
                exists = self.store.h.db.execute(
                    'SELECT 1 FROM predictive_observations WHERE account=? AND device=? AND origin=? AND version=?',
                    (account, device, origin, VERSION),
                ).fetchone()
            if exists:
                self.next_record = origin + 900
                return
            with read_view(self.store) as view:
                points = series(view, origin - LOOKBACK, origin)
                evidence = view.evidence(account, device, origin - LOOKBACK, origin, origin)
                networks = network_minutes(view, list(points), origin - LOOKBACK, origin)
            models = []
            for model, rows in sorted(points.items()):
                if origin not in rows:
                    continue
                forecasts = [predict(rows, origin, h) for h in HORIZONS]
                if any(f is None for f in forecasts):
                    continue
                for f in forecasts:
                    f['income'] = income_scenario(
                        evidence.get(model, {}),
                        networks.get(model, {}),
                        f['prediction'],
                        rows[origin]['warm'],
                        origin,
                    )
                models.append(
                    {
                        'id': model,
                        'available': model in available,
                        'observed': rows[origin],
                        'forecasts': forecasts,
                    }
                )
            if not models:
                return
            packet = {'origin': origin, 'createdAt': now, 'version': VERSION, 'models': models}
            with self.store.h.lock:
                self.store.h.db.execute(
                    'INSERT OR IGNORE INTO predictive_observations VALUES(?,?,?,?,?)',
                    (account, device, origin, VERSION, json.dumps(packet, allow_nan=False)),
                )
                self.store.h.db.commit()
            self.next_record = origin + 900

    def report(self, account, device, start, end, now):
        if not self.enabled:
            return {'enabled': False}
        if not all(finite(v) for v in (start, end, now)) or start < 0 or min(end, now) <= start:
            raise ValueError('Choose a valid recorded date range.')
        end = min(end, now)
        with read_view(self.store) as view:
            records = [
                json.loads(r[0])
                for r in view.h.db.execute(
                    """SELECT data FROM predictive_observations
                WHERE account=? AND device=? AND origin>=? AND origin<=? AND version=? ORDER BY origin""",
                    (account, device, start, end, VERSION),
                )
            ]
            # Outcome history may extend past the selected origins, but only up
            # to the present. This is scoring a frozen forecast, not refitting it.
            points = series(view, start, min(now, end + 3600))
            e = view.evidence(account, device, start, end, now) if account and device else {}
        latest = records[-1] if records else None
        return {
            'enabled': True,
            'version': VERSION,
            'at': now,
            'from': start,
            'to': end,
            'passive': True,
            'observations': len(records),
            'latest': latest,
            'fresh': bool(latest and 0 <= now - latest['origin'] <= 990),
            'accuracy': evaluation(records, points, now),
            'portfolios': portfolio(e),
            'pairSupport': {'maxModels': 2, 'automaticPicker': 'solo', 'jointForecast': False},
            'method': 'Predicted mean concurrent load per warm provider over the next 15 / 60 minutes. Learned from past-only hourly windows; compared with the last 15-minute mean. Shadow forecasts never switch models.',
        }
