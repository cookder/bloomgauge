"""Network-only demand comparisons from saved capacity snapshots.

These gauges measure concurrent load, never completed requests or revenue.
Missing readings are not zeros; averages include only observed snapshots.
"""

import math

CADENCE = 30


def model_demand(history, start, end, now):
    if not all(math.isfinite(x) for x in (start, end, now)) or start < 0 or end <= start:
        raise ValueError('Invalid date range')
    end = min(end, now)
    with history.lock:
        db = history.db
        coverage = db.execute('SELECT MIN(at),MAX(at) FROM opt_network').fetchone()
        first, last = coverage
        # All history compares the halves of the recorded lifetime. Other ranges
        # retain their requested start, so a partial first day cannot claim a trend.
        comparison_start = first if start == 0 and first is not None else start
        midpoint = (comparison_start + end) / 2
        half = max(0, (end - comparison_start) / 2)
        low = max(start, first or start)
        high = min(end, last if last is not None else end)
        step = max(CADENCE, math.ceil(max(0, high - low) / 360 / CADENCE) * CADENCE)
        result = {
            'models': [],
            'from': start,
            'to': end,
            'coverageStart': first,
            'coverageEnd': last,
            'bucketSeconds': step,
            'count': 0,
            'comparison': {
                'start': comparison_start,
                'midpoint': midpoint,
                'end': end,
                'minimumCoverage': 80,
            },
        }
        if first is None or end <= start or high < low:
            return result
        ranks = db.execute(
            """SELECT model,COUNT(*) AS samples,MIN(at) AS first,MAX(at) AS last,
            AVG(active) AS averageActive,AVG(queued) AS averageQueued,
            AVG(active+queued) AS averageLoad,MAX(active+queued) AS peakLoad,
            AVG(CASE WHEN warm>0 THEN (active+queued)/warm END) AS averagePressure,
            SUM(CASE WHEN warm=0 AND active+queued>0 THEN 1 ELSE 0 END) AS noWarmSamples,
            SUM(active+queued) AS loadSum,
            COUNT(DISTINCT CAST((at-:begin)/30 AS INT)) AS coveredSlots,
            AVG(CASE WHEN at<:mid THEN active+queued END) AS firstHalfAverage,
            AVG(CASE WHEN at>=:mid THEN active+queued END) AS lastHalfAverage,
            COUNT(DISTINCT CASE WHEN at<:mid THEN CAST((at-:begin)/30 AS INT) END) AS firstSlots,
            COUNT(DISTINCT CASE WHEN at>=:mid THEN CAST((at-:mid)/30 AS INT) END) AS lastSlots
            FROM opt_network WHERE at>=:start AND at<:end GROUP BY model""",
            {'begin': comparison_start, 'mid': midpoint, 'start': start, 'end': end},
        ).fetchall()
        buckets = db.execute(
            """SELECT model,CAST((at-?)/? AS INT) AS bucket,
            AVG(at) AS at,MIN(at) AS first,MAX(at) AS last,
            AVG(active) AS active,AVG(queued) AS queued,AVG(active+queued) AS load,
            AVG(CASE WHEN warm>0 THEN (active+queued)/warm END) AS pressure,
            AVG(warm) AS warm,SUM(active+queued) AS loadSum
            FROM opt_network WHERE at>=? AND at<? GROUP BY model,bucket ORDER BY model,bucket""",
            (low, step, start, end),
        ).fetchall()

    total = sum(r['loadSum'] for r in ranks)
    bucket_totals = {}
    for r in buckets:
        bucket_totals[r['bucket']] = bucket_totals.get(r['bucket'], 0) + r['loadSum']
    models = {}
    for row in ranks:
        m = dict(row)
        m['id'] = m.pop('model')
        m['sharePercent'] = m.pop('loadSum') * 100 / total if total > 0 else None
        m['coveragePercent'] = min(100, m.pop('coveredSlots') * CADENCE * 100 / max(1, 2 * half))
        m['firstHalfCoverage'] = min(100, m.pop('firstSlots') * CADENCE * 100 / max(1, half))
        m['lastHalfCoverage'] = min(100, m.pop('lastSlots') * CADENCE * 100 / max(1, half))
        a, b = m['firstHalfAverage'], m['lastHalfAverage']
        # At least five minutes per half, plus coverage in both halves. Short
        # bursts after an outage cannot be presented as a reliable rising trend.
        enough = (
            half >= 300
            and min(m['firstHalfCoverage'], m['lastHalfCoverage']) >= 80
            and a is not None
            and b is not None
        )
        m['trendPercent'] = (b - a) * 100 / a if enough and a > 0 else None
        m['trendState'] = (
            ('new' if b > 0 else 'steady')
            if enough and a == 0
            else ('up' if b > a else 'down' if b < a else 'steady')
            if enough
            else 'insufficient'
        )
        m['chart'] = [
            {
                'at': low + i * step,
                **{k: None for k in ('active', 'queued', 'load', 'pressure', 'warm', 'share')},
            }
            for i in range(int(max(0, high - low) // step) + 1)
        ]
        models[m['id']] = m
    for r in buckets:
        chart = models[r['model']]['chart']
        denominator = bucket_totals[r['bucket']]
        chart[r['bucket']].update(
            {
                **{k: r[k] for k in ('active', 'queued', 'load', 'pressure', 'warm')},
                'share': r['loadSum'] * 100 / denominator if denominator > 0 else None,
            }
        )
    result['models'] = sorted(models.values(), key=lambda m: (-m['averageLoad'], m['id']))
    result['count'] = sum(m['samples'] for m in models.values())
    return result
