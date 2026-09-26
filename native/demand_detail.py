"""Aligned supply, demand and measured solo work. Gaps remain gaps."""

import math
from collections import defaultdict, deque
from demand_baselines import read_view
from model_pricing import number
from opportunity_lab import coverage


def detail(store, account, device, model, start, end, now):
    if (
        not isinstance(model, str)
        or not model
        or len(model) > 512
        or not all(number(v) for v in (start, end, now))
        or end <= start
    ):
        raise ValueError('Invalid demand detail range')
    with read_view(store) as view:
        db = view.h.db
        end = min(end, now)
        first, last = db.execute(
            'SELECT MIN(at),MAX(at) FROM opt_network WHERE model=?', (model,)
        ).fetchone()
        low = max(start, first if first is not None else start)
        step = max(30, math.ceil(max(0, end - low) / 240 / 30) * 30)
        raw = db.execute(
            'SELECT at,active,queued,warm FROM opt_network WHERE model=? AND at>=? AND at<? ORDER BY at',
            (model, max(0, low - 900), end),
        ).fetchall()
        slots = {int(r['at'] // 30): dict(r) for r in raw}
        smooth = deque()
        buckets = defaultdict(list)
        for slot, r in sorted(slots.items()):
            # A missing source reading breaks the line; trailing averages do
            # not draw across outages or recover instantly from a lone point.
            if smooth and r['at'] - smooth[-1]['at'] > 90:
                smooth.clear()
            smooth.append(r)
            while smooth and r['at'] - smooth[0]['at'] >= 900:
                smooth.popleft()
            if r['at'] < low:
                continue
            valid = len(smooth) >= 24
            item = {
                'at': r['at'],
                'rawActive': r['active'],
                'rawWarm': r['warm'],
                **{
                    k: sum(x[k] for x in smooth) / len(smooth) if valid else None
                    for k in ('active', 'queued', 'warm')
                },
            }
            positive = [x for x in smooth if x['warm'] > 0]
            item['pressure'] = (
                sum((x['active'] + x['queued']) / x['warm'] for x in positive) / len(positive)
                if valid and len(positive) >= 24
                else None
            )
            buckets[int((r['at'] - low) // step)].append(item)
        result = []
        count = max(0, int((end - low) // step) + 1) if first is not None and low < end else 0
        intervals = [
            tuple(r)
            for r in db.execute(
                'SELECT start,end FROM opt_coverage WHERE account=? AND end>? AND start<?',
                (account, low, end),
            )
        ]
        warm = db.execute(
            """SELECT at,jobs FROM opt_ready_minutes WHERE account=? AND device=? AND model=? AND at>=? AND at+60<=?
            AND seconds BETWEEN 59.999999 AND 60.000001 ORDER BY at""",
            (account, device, model, low, min(end, now - 120)),
        ).fetchall()
        minute_money = {
            r['minute']: r['usd']
            for r in db.execute(
                """SELECT CAST(c.at/60 AS INTEGER)*60 AS minute,SUM(c.micro_usd)/1e6 AS usd
            FROM opt_credits c WHERE c.account=? AND c.model=? AND c.at>=? AND c.at<?
            AND EXISTS(SELECT 1 FROM opt_identity i WHERE i.device=? AND i.provider=c.provider) GROUP BY minute""",
                (account, model, low, min(end, now - 120), device),
            )
        }
        # Local work has five-minute-or-larger buckets; a tiny 30s chart slot
        # must not imply second-by-second settled earnings attribution.
        local_step = max(300, math.ceil(step / 300) * 300)
        local = defaultdict(list)
        for r in warm:
            if coverage(intervals, r['at'], r['at'] + 60) >= 0.999999:
                local[int((r['at'] - low) // local_step)].append(r)
        local_points = []
        for bucket in range(int(max(0, end - low) // local_step) + 1) if count else []:
            a = low + bucket * local_step
            z = min(end, now - 120, a + local_step)
            minutes = [r for r in local.get(bucket, []) if r['at'] + 60 <= z]
            enough = len(minutes) * 60 >= 0.8 * local_step
            local_points.append(
                {
                    'at': a,
                    'requestsPerMinute': sum(r['jobs'] for r in minutes) / len(minutes)
                    if enough
                    else None,
                    'usdPerWarmHour': sum(minute_money.get(r['at'], 0) for r in minutes)
                    * 60
                    / len(minutes)
                    if enough
                    else None,
                    'warmMinutes': len(minutes),
                }
            )
        for bucket in range(count):
            points = buckets.get(bucket, [])
            item = {'at': low + bucket * step}
            for k in ('rawActive', 'rawWarm', 'active', 'warm', 'queued', 'pressure'):
                values = [p[k] for p in points if p[k] is not None]
                item[k] = sum(values) / len(values) if values else None
            result.append(item)
        marks = [
            dict(r)
            for r in db.execute(
                "SELECT at,kind,model FROM opt_events WHERE account=? AND device=? AND at>=? AND at<? AND kind IN ('switching','switched','failed','recovered') ORDER BY at",
                (account, device, low, end),
            )
        ]
        return {
            'model': model,
            'from': start,
            'to': end,
            'coverageStart': first,
            'coverageEnd': last,
            'samples': result,
            'localSamples': local_points,
            'markers': marks[-100:],
            'bucketSeconds': step,
            'localBucketSeconds': local_step,
            'smoothingSeconds': 900,
        }
