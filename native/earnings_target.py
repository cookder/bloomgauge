"""Confirmed earnings per clock hour. Cold/switch time is never normalized away.

The goal is the user's own (demand_targets.chosen_goal). With no goal, hours are
reported without a met/below judgement.
"""

import bisect
import math
from demand_baselines import read_view


def finite(v):
    return type(v) in (int, float) and math.isfinite(v)


def report(store, account, device, start, end, now, target=None, model=None):
    if (
        not all(finite(v) for v in (start, end, now))
        or (target is not None and (not finite(target) or target <= 0))
        or start < 0
        or end <= start
    ):
        raise ValueError('Invalid earnings target range.')
    if model is not None and (not isinstance(model, str) or not model or len(model) > 512):
        raise ValueError('Invalid model filter.')
    end = min(end, now)
    if end <= start:
        raise ValueError('The earnings target needs an observed time range.')
    include_base = model is None
    selected = None if model in (None, '@inference') else model
    settled_end = min(end, now - 120)
    with read_view(store) as view:
        db = view.h.db
        intervals = [
            tuple(r)
            for r in db.execute(
                'SELECT start,end FROM opt_coverage WHERE account=? AND end>? AND start<? ORDER BY start',
                (account, start, end),
            )
        ]
        rows = db.execute(
            """SELECT CAST(c.at/3600 AS INT)*3600 AS hour,c.model,SUM(c.micro_usd) AS amount,
            SUM(CASE WHEN c.at<? THEN c.micro_usd ELSE 0 END) AS settled_amount
            FROM opt_credits c WHERE c.account=? AND c.at>=? AND c.at<?
            AND (c.model='base_reward' OR EXISTS(SELECT 1 FROM opt_identity i WHERE i.device=? AND i.provider=c.provider))
            GROUP BY hour,c.model""",
            (settled_end, account, start, end, device),
        ).fetchall()
        switches = db.execute(
            """SELECT at,downtime FROM opt_events WHERE account=? AND device=? AND at>=? AND at<?
            AND kind IN ('switched','failed','recovered') AND downtime>0""",
            (account, device, start, end),
        ).fetchall()
        earliest = db.execute(
            'SELECT MIN(start) FROM opt_coverage WHERE account=?', (account,)
        ).fetchone()[0]
    requested_start = start
    # "All" starts with recorded history. We do not fabricate pre-install hours.
    if start == 0:
        start = math.floor(earliest / 3600) * 3600 if earliest is not None else max(0, end - 86400)
    if start >= end:
        start = max(requested_start, end - 86400)
    merged = []
    for a, b in intervals:
        a, b = max(start, a), min(end, b)
        if b <= a:
            continue
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    interval_starts = [r[0] for r in merged]

    def covered(a, b):
        i = bisect.bisect_right(interval_starts, a) - 1
        return i >= 0 and merged[i][1] >= b

    money = {}
    models = set()
    inference = base = settled_total = 0
    for row in rows:
        bucket = money.setdefault(row['hour'], {'inference': 0, 'base': 0})
        if row['model'] == 'base_reward':
            base += row['amount']
            bucket['base'] += row['amount']
            if include_base:
                settled_total += row['settled_amount']
        else:
            models.add(row['model'])
            if selected is None or row['model'] == selected:
                inference += row['amount']
                bucket['inference'] += row['amount']
                settled_total += row['settled_amount']
    loop_start = max(
        start,
        min(math.floor(earliest / 3600) * 3600 if earliest is not None else end, end - 31 * 86400),
    )
    # Very long custom ranges can include decades before installation. Count
    # those unknown full hours arithmetically instead of allocating their cells.
    unknown = max(0, int(loop_start // 3600) - int(math.ceil(start / 3600)))
    hourly = []
    complete = met = 0
    complete_usd = 0
    longest = streak = 0
    previous = None
    for at in range(int(loop_start // 3600) * 3600, int(math.ceil(end / 3600)) * 3600, 3600):
        a, b = max(start, at), min(end, at + 3600)
        m = money.get(at, {'inference': 0, 'base': 0})
        usd = (m['inference'] + (m['base'] if include_base else 0)) / 1e6
        full = a == at and b == at + 3600
        coverage = covered(a, b)
        settled = b <= now - 120
        valid = full and coverage and settled
        if valid:
            complete += 1
            complete_usd += usd
            if target is None:
                pass
            elif usd + 1e-12 >= target:
                met += 1
                streak = 0
            else:
                streak = streak + 1 if previous == at - 3600 else 1
                longest = max(longest, streak)
            previous = at
        else:
            streak = 0
            previous = None
            if full:
                unknown += 1
        status = (
            ('complete' if target is None else 'met' if usd + 1e-12 >= target else 'below')
            if valid
            else 'partial'
            if not full
            else 'settling'
            if coverage and not settled
            else 'unknown'
        )
        # Keep exact hourly cells, bounded to the latest 31 days of this range.
        if at >= end - 31 * 86400:
            hourly.append(
                {
                    'at': at,
                    'end': at + 3600,
                    'from': a,
                    'to': b,
                    'usd': usd,
                    'inferenceUsd': m['inference'] / 1e6,
                    'baseUsd': m['base'] / 1e6,
                    'status': status,
                    'covered': coverage,
                }
            )
    total = (inference + (base if include_base else 0)) / 1e6
    full_coverage = covered(start, end)
    return {
        'at': now,
        'from': start,
        'to': end,
        'requestedFrom': requested_start,
        'historyStart': earliest,
        'model': model,
        'models': sorted(models),
        'targetUsdPerHour': target,
        'dailyTargetUsd': None if target is None else target * 24,
        'usd': total,
        'inferenceUsd': inference / 1e6,
        'accountBaseUsd': base / 1e6,
        'includesBase': include_base,
        'coveredSeconds': sum(b - a for a, b in merged),
        'rangeSeconds': end - start,
        'completeCoverage': full_coverage,
        'clockUsdPerHour': settled_total / 1e6 * 3600 / (settled_end - start)
        if settled_end > start and covered(start, settled_end)
        else None,
        'settledTo': settled_end,
        'completeHours': complete,
        'metHours': None if target is None else met,
        'unknownHours': unknown,
        'metPercent': met * 100 / complete if complete and target is not None else None,
        'completeHourAverageUsd': complete_usd / complete if complete else None,
        'longestBelowHours': None if target is None else longest,
        'switchSeconds': sum(r['downtime'] for r in switches),
        'switchCount': len(switches),
        'hourly': hourly,
        'chartTruncated': end - start > 31 * 86400,
        'scope': 'This Mac inference + account base rewards'
        if include_base
        else 'This Mac inference only',
        'method': 'Confirmed credit timestamps / clock time. Full, covered hours settle after two minutes; partial and missing hours do not enter target attainment. Base rewards are account-level.',
    }
