"""Transparent hourly pace estimates. Confirmed totals are never overwritten."""

from datetime import datetime


def hour_start(now):
    return datetime.fromtimestamp(now).replace(minute=0, second=0, microsecond=0).timestamp()


def monitor_hour_start(hours, at):
    """Start of the hour containing `at`, on Monitor's own hour boundaries when known.

    In half-hour time zones (India, Adelaide) local hours start at :30 UTC; a
    local bucket would split or miss an hour Monitor keeps as one.
    """
    offset = next(
        (
            int(h['at']) % 3600
            for h in hours
            if isinstance(h, dict) and isinstance(h.get('at'), (int, float))
        ),
        None,
    )
    return at - (at - offset) % 3600 if offset is not None else hour_start(at)


def prior_rate(hours, key, hour):
    values = [
        (max(0, key(h)) / 3600, 0.65 ** ((hour - h['at']) / 3600 - 1))
        for h in hours
        if hour - 21600 <= h['at'] < hour
    ]
    return sum(v * w for v, w in values) / sum(w for _, w in values) if values else None


def pace(actual, elapsed, prior, recent=()):
    # Ten minutes of prior exposure damp the beginning of an hour. With no
    # prior, wait for a meaningful interval rather than extrapolating one job.
    baseline = (
        (actual + (prior or 0) * 600) / (elapsed + 600)
        if prior is not None
        else actual / max(300, elapsed)
    )
    if not recent:
        return max(0, baseline)
    weight = sum(w for _, w, _ in recent)
    momentum = sum(value * w for value, w, _ in recent) / weight
    trust = 0.7 * min(1, max(seconds for _, _, seconds in recent) / 900)
    return max(0, (1 - trust) * baseline + trust * momentum)


def forecast(now, monitor, provider, minutes, recent):
    hour = monitor_hour_start(monitor.get('hours', []), now)
    end = hour + 3600
    result = {
        'at': now,
        'hourStart': hour,
        'hourEnd': end,
        'earnings': {
            'status': 'warming',
            'actual': None,
            'projected': None,
            'additional': None,
            'jobsProjected': None,
            'detail': 'Waiting for a complete, fresh hourly earnings reading.',
        },
        'throughput': {
            'status': 'warming',
            'actual': None,
            'projected': None,
            'additional': None,
            'expectedRate': None,
            'detail': 'Learning your provider’s recent output pace.',
        },
    }
    e = result['earnings']
    t = result['throughput']
    observed = min(now, monitor.get('observedAt') or monitor.get('updatedAt') or 0)
    hours = monitor.get('hours', [])
    current = next((h for h in hours if h['at'] == hour), None)
    coverage = monitor.get('coverageStartedAt')
    gaps = monitor.get('coverageIntervals', [])

    def complete(start, stop):
        return (
            coverage is not None
            and coverage <= start
            and not (monitor.get('gaps', 0) and not gaps)
            and not any(g['start'] < stop and g['end'] > start for g in gaps)
        )

    fresh = monitor.get('status') == 'ok' and now - observed <= 180 and observed >= hour
    if current:
        e.update(actual=current['usd'], jobsActual=current['jobs'], asOf=observed)
    if not provider.get('online'):
        e.update(
            status='offline',
            detail='Provider offline. Forecast resumes when fresh readings return.',
        )
        t.update(
            status='offline',
            detail='Provider offline. Forecast resumes when fresh readings return.',
        )
    elif not provider.get('tracking', {}).get('counting'):
        e.update(
            status='paused',
            detail='Projection paused until the selected models are loaded and pre-warmed.',
        )
        t.update(
            status='paused',
            detail='Projection paused until the selected models are loaded and pre-warmed.',
        )
    elif fresh and complete(hour, observed):
        current = current or {'at': hour, 'usd': 0, 'jobs': 0, 'categories': {}}
        e.update(actual=current['usd'], jobsActual=current['jobs'], asOf=observed)
        elapsed = max(0, observed - hour)
        indexed = {h['at']: h for h in hours}
        previous = [
            indexed.get(start, {'at': start, 'usd': 0, 'jobs': 0, 'categories': {}})
            for start in range(int(hour) - 21600, int(hour), 3600)
            if complete(start, start + 3600)
        ]
        base = lambda h: h.get('categories', {}).get('base_reward', 0)
        work = lambda h: max(0, h['usd'] - base(h))
        prior = prior_rate(previous, work, hour)
        if elapsed >= 120 or prior is not None:
            work_rate = pace(
                work(current),
                elapsed,
                prior,
                [
                    (r['usdPerSecond'], 0.65 if r['window'] == 300 else 0.35, r['seconds'])
                    for r in recent
                ],
            )
            # Base rewards can arrive in batches. They use the smoothed hourly
            # pace, never the last few minutes of individual credit timestamps.
            base_prior = prior_rate(previous, base, hour)
            base_final = (
                max(base(current), base_prior * 3600)
                if base_prior is not None
                else base(current) + pace(base(current), elapsed, None) * max(0, end - observed)
            )
            job_rate = pace(
                current['jobs'],
                elapsed,
                prior_rate(previous, lambda h: h['jobs'], hour),
                [
                    (r['jobsPerSecond'], 0.65 if r['window'] == 300 else 0.35, r['seconds'])
                    for r in recent
                ],
            )
            additional = work_rate * max(0, end - observed) + max(0, base_final - base(current))
            e.update(
                status='ready',
                projected=current['usd'] + additional,
                additional=additional,
                jobsProjected=current['jobs'] + job_rate * max(0, end - observed),
                ratePerHour=additional / max(1, end - observed) * 3600,
                detail='Recent 5–15 minute job earnings blended with this hour and up to six completed hours. Base rewards smoothed separately.'
                if recent
                else 'This hour’s pace blended with up to six completed hours. Learning recent job activity.',
            )
    elif current and not fresh:
        e.update(
            status='stale', detail='Earnings source is stale. Projection paused until it updates.'
        )
    elif not complete(hour, observed):
        e.update(
            status='partial',
            detail='This hour has a tracking gap. An end-of-hour earnings total would be incomplete.',
        )

    current_minutes = [
        m for m in minutes if hour <= m['at'] < now and m['n'] > 0 and m['rate'] is not None
    ]
    seconds = sum(m['n'] for m in current_minutes)
    actual = sum(m['rate'] * m['n'] for m in current_minutes)
    t.update(
        actual=actual if seconds else None,
        coverageSeconds=seconds,
        coverageFraction=min(1, seconds / max(1, now - hour)),
        partial=seconds < max(0, now - hour - 10),
    )
    if not provider.get('online') or not provider.get('tracking', {}).get('counting'):
        return result
    windows = []
    for window, w in ((300, 0.65), (900, 0.35)):
        rows = [
            m
            for m in minutes
            if now - window <= m['at'] < now and m['n'] > 0 and m['rate'] is not None
        ]
        n = sum(m['n'] for m in rows)
        if n >= 120:
            windows.append((sum(m['rate'] * m['n'] for m in rows) / n, w, n))
    completed = []
    for start in range(int(hour) - 21600, int(hour), 3600):
        rows = [
            m
            for m in minutes
            if start <= m['at'] < start + 3600 and m['n'] > 0 and m['rate'] is not None
        ]
        n = sum(m['n'] for m in rows)
        if n >= 3500:
            completed.append(
                {'at': start, 'tokens': sum(m['rate'] * m['n'] for m in rows) / n * 3600}
            )
    prior = prior_rate(completed, lambda h: h['tokens'], hour)
    if not windows and prior is None and seconds < 120:
        return result
    expected = pace(actual, seconds, prior, windows)
    additional = expected * max(0, end - now)
    t.update(
        status='ready',
        expectedRate=expected,
        additional=additional,
        projected=None if t['partial'] else actual + additional,
        detail='Expected pace for the remaining hour, including idle periods. Output totals are approximated from provider counter samples.'
        + (
            ' Earlier readings are missing, so only remaining output is forecast.'
            if t['partial']
            else ''
        ),
    )
    return result
