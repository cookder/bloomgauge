"""Calendar-day confirmed earnings, using the same scope/coverage as Target.

Read-only; no rates, projections or warm-time normalization enter daily totals.
"""

import bisect
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from demand_baselines import read_view


def report(store, account, device, start, end, now, timezone='UTC', model=None):
    if (
        not all(type(v) in (int, float) and math.isfinite(v) for v in (start, end, now))
        or start < 0
        or end <= start
        or end > 4102444800
    ):
        raise ValueError('Invalid daily earnings range')
    if not isinstance(timezone, str) or len(timezone) > 100:
        raise ValueError('Invalid timezone')
    try:
        zone = ZoneInfo(timezone)
    except (ValueError, ZoneInfoNotFoundError):
        raise ValueError('Invalid timezone') from None
    if model is not None and (not isinstance(model, str) or not model or len(model) > 512):
        raise ValueError('Invalid model')
    end = min(end, now)
    if end <= start:
        raise ValueError('No observed time range')
    selected = None if model in (None, '@inference') else model
    scope = "c.account=? AND (c.model='base_reward' OR EXISTS(SELECT 1 FROM opt_identity i WHERE i.device=? AND i.provider=c.provider))"
    with read_view(store) as view:
        db = view.h.db
        first = db.execute(
            'SELECT MIN(start) FROM opt_coverage WHERE account=?', (account,)
        ).fetchone()[0]
        first_credit = db.execute(
            'SELECT MIN(c.at) FROM opt_credits c WHERE ' + scope, (account, device)
        ).fetchone()[0]
        known = [v for v in (first, first_credit) if v is not None]
        low = start or (
            datetime.fromtimestamp(min(known), zone)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .timestamp()
            if known
            else end - 7 * 86400
        )
        if low >= end:
            low = max(start, end - 7 * 86400)
        intervals = db.execute(
            'SELECT start,end FROM opt_coverage WHERE account=? AND end>? AND start<? ORDER BY start',
            (account, low, end),
        ).fetchall()
        totals = db.execute(
            'SELECT c.model,SUM(c.micro_usd) AS amount FROM opt_credits c WHERE '
            + scope
            + ' AND c.at>=? AND c.at<? GROUP BY c.model',
            (account, device, low, end),
        ).fetchall()
        day = datetime.fromtimestamp(low, zone).replace(hour=0, minute=0, second=0, microsecond=0)
        last = datetime.fromtimestamp(end - 0.000001, zone).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        truncated = (last.date() - day.date()).days >= 366
        if truncated:
            day = last - timedelta(days=365)
        days = []
        while day.timestamp() < end:
            nxt = day + timedelta(days=1)
            a, b = max(low, day.timestamp()), min(end, nxt.timestamp())
            if b > a:
                days.append(
                    {
                        'date': day.date().isoformat(),
                        'at': day.timestamp(),
                        'end': nxt.timestamp(),
                        'from': a,
                        'to': b,
                    }
                )
            day = nxt
        # Bounded calendar bins join indexed credit timestamps. This also handles
        # half-hour zones, DST's 23/25-hour days and exact custom boundaries.
        values = ','.join('(?,?,?)' for _ in days)
        params = [v for d in days for v in (d['date'], d['from'], d['to'])]
        rows = (
            db.execute(
                'WITH days(day,a,b) AS (VALUES '
                + values
                + ') SELECT d.day,c.model,SUM(c.micro_usd) AS amount FROM days d JOIN opt_credits c ON c.at>=d.a AND c.at<d.b WHERE '
                + scope
                + ' GROUP BY d.day,c.model',
                params + [account, device],
            ).fetchall()
            if days
            else []
        )
    merged = []
    for a, b in intervals:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    starts = [a for a, _ in merged]

    def covered(a, b):
        i = bisect.bisect_right(starts, a) - 1
        return b > a and i >= 0 and merged[i][1] >= b

    def amounts(rows):
        base = sum(r['amount'] for r in rows if r['model'] == 'base_reward')
        inference = sum(
            r['amount']
            for r in rows
            if r['model'] != 'base_reward' and (selected is None or r['model'] == selected)
        )
        return {
            'usd': (inference + (base if model is None else 0)) / 1e6,
            'inferenceUsd': inference / 1e6,
            'baseUsd': base / 1e6,
        }

    grouped = {}
    for row in rows:
        grouped.setdefault(row['day'], []).append(row)
    today = datetime.fromtimestamp(now, zone).date().isoformat()
    for d in days:
        d.update(amounts(grouped.get(d['date'], [])))
        full = d['from'] == d['at'] and d['to'] == d['end']
        # The last two minutes are a normal settlement tail, not a fabricated gap.
        d['covered'] = covered(d['from'], min(d['to'], now - 120))
        d['status'] = (
            'unknown'
            if not d['covered']
            else 'today'
            if d['date'] == today
            else 'partial'
            if not full
            else 'settling'
            if d['end'] > now - 120
            else 'complete'
        )
    complete = [d for d in days if d['status'] == 'complete']
    return {
        'at': now,
        'from': low,
        'to': end,
        'timezone': timezone,
        'model': model,
        'includesBase': model is None,
        'historyStart': min(known) if known else None,
        'days': days,
        'chartTruncated': truncated,
        'models': sorted(r['model'] for r in totals if r['model'] != 'base_reward'),
        **amounts(totals),
        'completeDays': len(complete),
        'averageDayUsd': sum(d['usd'] for d in complete) / len(complete)
        if len(complete) >= 3
        else None,
        'scope': 'This Mac inference + account base rewards'
        if model is None
        else 'This Mac inference only',
    }
