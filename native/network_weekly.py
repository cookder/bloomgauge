"""Read-only weekday/hour traffic patterns from saved official network buckets.

Never spread coarse buckets, unfinished hours or gaps into invented hourly data.
Rates use recorded duration; one busy sample is not an hour of coverage.
"""

import math
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from demand_baselines import read_view


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def report(history, start, end, now, timezone='UTC'):
    if (
        not all(finite(v) for v in (start, end, now))
        or start < 0
        or end <= start
        or end > 4102444800
    ):
        raise ValueError('Invalid range')
    if not isinstance(timezone, str) or len(timezone) > 100:
        raise ValueError('Invalid timezone')
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError('Invalid timezone') from None
    with read_view(SimpleNamespace(h=history)) as view:
        db = view.h.db
        first = db.execute(
            'SELECT MIN(at) FROM network WHERE seconds>0 AND seconds<=3600'
        ).fetchone()[0]
        rows = db.execute(
            'SELECT * FROM network WHERE at>=? AND at<? ORDER BY at', (start, min(end, now))
        ).fetchall()
    # Only fully elapsed local hours inside the selected range can contribute.
    high = min(end, now)
    low = max(start, first) if first is not None else high

    def hour_start(at):
        return (
            datetime.fromtimestamp(at, zone).replace(minute=0, second=0, microsecond=0).timestamp()
        )

    low_hour = hour_start(low)
    if low_hour < low:
        low_hour += 3600
    high_hour = hour_start(high)
    instances = {}
    at = low_hour
    while at < high_hour:
        local = datetime.fromtimestamp(at, zone)
        key = (local.date().isoformat(), local.hour)
        entry = instances.setdefault(
            key,
            {
                'date': key[0],
                'hour': key[1],
                'day': local.weekday(),
                'expectedSeconds': 0,
                'seconds': 0,
                'requests': 0,
                'tokens': 0,
            },
        )
        # On autumn clock changes the repeated local hour has 7,200 real seconds.
        entry['expectedSeconds'] += 3600
        at += 3600
    skipped = {'coarseBuckets': 0, 'boundaryBuckets': 0, 'invalidBuckets': 0}
    last_end = None
    for row in rows:
        at, seconds = row['at'], row['seconds']
        values = (at, seconds, row['requests'], row['completion_tokens'])
        if not all(finite(v) and v >= 0 for v in values) or seconds <= 0:
            skipped['invalidBuckets'] += 1
            continue
        if seconds > 3600:
            skipped['coarseBuckets'] += 1
            continue
        finish = at + seconds
        local = datetime.fromtimestamp(at, zone)
        key = (local.date().isoformat(), local.hour)
        if at < low_hour or finish > high_hour or hour_start(at) != hour_start(finish - 0.001):
            skipped['boundaryBuckets'] += 1
            continue
        if last_end is not None and at < last_end:
            skipped['invalidBuckets'] += 1
            continue
        last_end = finish
        item = instances.get(key)
        if item is None:
            continue
        item['seconds'] += seconds
        item['requests'] += row['requests']
        item['tokens'] += row['completion_tokens']
    cells = []
    for day in range(7):
        for hour in range(24):
            dates = [r for r in instances.values() if r['day'] == day and r['hour'] == hour]
            qualified = [r for r in dates if r['seconds'] >= r['expectedSeconds'] * 0.8]
            seconds = sum(r['seconds'] for r in qualified)
            requests = sum(r['requests'] for r in qualified)
            tokens = sum(r['tokens'] for r in qualified)
            cells.append(
                {
                    'day': day,
                    'hour': hour,
                    'days': len(qualified),
                    'possibleDays': len(dates),
                    'observedSeconds': sum(r['seconds'] for r in dates),
                    'expectedSeconds': sum(r['expectedSeconds'] for r in dates),
                    'qualifiedSeconds': seconds,
                    'requests': requests,
                    'tokens': tokens,
                    'requestsPerMinute': requests * 60 / seconds if seconds else None,
                    'tokensPerSecond': tokens / seconds if seconds else None,
                    'dates': [
                        {
                            'date': r['date'],
                            'coverage': r['seconds'] / r['expectedSeconds'],
                            'requestsPerMinute': r['requests'] * 60 / r['seconds']
                            if r in qualified
                            else None,
                            'tokensPerSecond': r['tokens'] / r['seconds']
                            if r in qualified
                            else None,
                        }
                        for r in dates
                    ],
                }
            )
    qualified_dates = {
        r['date'] for r in instances.values() if r['seconds'] >= 0.8 * r['expectedSeconds']
    }
    return {
        'from': start,
        'to': high,
        'at': now,
        'timezone': timezone,
        'cells': cells,
        'coverageStart': low_hour if instances else None,
        'coverageEnd': high_hour if instances else None,
        'hourlyHistoryStart': first,
        'qualifiedDates': len(qualified_dates),
        'observedHours': sum(c['observedSeconds'] for c in cells) / 3600,
        'expectedHours': sum(c['expectedSeconds'] for c in cells) / 3600,
        'minimumCoverage': 0.8,
        **skipped,
    }
