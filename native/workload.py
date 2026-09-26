"""Read-only workload analytics over settled, device-matched warm credit evidence."""

import math
from collections import defaultdict
from model_combinations import members
from demand_baselines import read_view

BINS = [
    (0, '0'),
    (32, '1–32'),
    (128, '33–128'),
    (512, '129–512'),
    (2048, '513–2k'),
    (8192, '2k–8k'),
    (math.inf, '8k+'),
]


def token_count(value):
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2**53 - 1
        else None
    )


def credit_measurements(row):
    """Preserve measured zero versus absent legacy token fields in both reports."""
    output = (
        token_count(row['output_tokens'])
        if row['detail_id'] is not None
        else (row['tokens'] if token_count(row['tokens']) and row['tokens'] > 0 else None)
    )
    return {
        'at': row['at'],
        'model': row['model'],
        'micro_usd': row['micro_usd'],
        'prompt': token_count(row['prompt_tokens']),
        'output': output,
    }


def aggregate(rows):
    jobs = [r for r in rows if r['micro_usd'] >= 0]
    prompts = [r['prompt'] for r in jobs if r['prompt'] is not None]
    outputs = [r['output'] for r in jobs if r['output'] is not None]
    complete = [r for r in jobs if r['prompt'] is not None and r['output'] is not None]
    tokens = sum(r['prompt'] + r['output'] for r in complete)
    output_rows = [r for r in jobs if r['output'] is not None]

    def percentile(values, p):
        values = sorted(values)
        return values[max(0, math.ceil(len(values) * p) - 1)] if values else None

    return {
        'requests': len(jobs),
        'adjustments': len(rows) - len(jobs),
        'confirmedUSD': sum(r['micro_usd'] for r in rows) / 1e6,
        'usdPerRequest': sum(r['micro_usd'] for r in jobs) / 1e6 / len(jobs) if jobs else None,
        'promptSamples': len(prompts),
        'outputSamples': len(outputs),
        'completeTokenSamples': len(complete),
        'measuredPromptTokens': sum(r['prompt'] for r in complete),
        'measuredOutputTokens': sum(r['output'] for r in complete),
        'lastMeasuredAt': max((r['at'] for r in complete), default=None),
        'meanPrompt': sum(prompts) / len(prompts) if prompts else None,
        'meanOutput': sum(outputs) / len(outputs) if outputs else None,
        'medianOutput': percentile(outputs, 0.5),
        'p90Output': percentile(outputs, 0.9),
        'usdPerMillionTokens': sum(r['micro_usd'] for r in complete) / tokens if tokens else None,
        'usdPerMillionOutput': sum(r['micro_usd'] for r in output_rows) / sum(outputs)
        if sum(outputs)
        else None,
    }


def workload(store, account, device, start, end, now, selected=None):
    if (
        not all(isinstance(v, (float, int)) and math.isfinite(v) for v in (start, end, now))
        or start < 0
        or end <= start
    ):
        raise ValueError('Invalid period')
    settled_end = min(end, now - 120)
    read_start = max(0, start - 3600)
    # A read-only snapshot: long ranges must not hold the collector's write lock.
    with read_view(store) as view:
        # Full warm minutes and coverage match the optimizer's existing evidence gate.
        warm = view.h.db.execute(
            """SELECT at,model FROM opt_ready_minutes m
          WHERE account=? AND device=? AND at>=? AND at+60<=? AND seconds>=59.999999 AND seconds<=60.000001
          AND EXISTS(SELECT 1 FROM opt_coverage c WHERE c.account=m.account AND c.start<=m.at AND c.end>=m.at+60)""",
            (account, device, read_start, settled_end),
        ).fetchall()
        credits = view.h.db.execute(
            """SELECT c.at,c.model,c.micro_usd,c.tokens,t.prompt_tokens,t.output_tokens,t.id AS detail_id
          FROM opt_credits c LEFT JOIN workload_tokens t ON t.account=c.account AND t.id=c.id
          WHERE c.account=? AND c.at>=? AND c.at<? AND c.model!='base_reward'
          AND c.provider IN (SELECT provider FROM opt_identity WHERE device=?)
          ORDER BY c.at,c.id""",
            (account, read_start, max(read_start, settled_end), device),
        ).fetchall()
    ready = defaultdict(set)
    for r in warm:
        ready[r['at']].update(members(r['model']))
    qualified = []
    excluded = 0
    model_ids = set()
    for r in credits:
        if r['at'] >= start:
            model_ids.add(r['model'])
        if r['model'] not in ready.get(int(r['at'] // 60) * 60, set()) or (
            r['at'] >= start and int(r['at'] // 60) * 60 < start
        ):
            if r['at'] >= start and (not selected or r['model'] == selected):
                excluded += 1
            continue
        # Legacy positive output counts were stored explicitly. Legacy zeros may
        # instead mean an absent API field; new detail rows preserve that distinction.
        qualified.append(credit_measurements(r))
    period = [r for r in qualified if start <= r['at'] < end]
    chosen = [r for r in period if not selected or r['model'] == selected]
    by_model = defaultdict(list)
    for r in period:
        by_model[r['model']].append(r)
    models = [{'id': m, **aggregate(by_model[m])} for m in sorted(model_ids)]
    total = aggregate(chosen)
    times = [at for at, ids in ready.items() if at >= start and (not selected or selected in ids)]
    total['warmHours'] = len(times) / 60
    low = max(start, min(times + [r['at'] for r in chosen], default=start))
    high = min(
        settled_end, max([at + 60 for at in times] + [r['at'] for r in chosen], default=start)
    )
    step = max(300, math.ceil(max(0, high - low) / 180 / 300) * 300)
    buckets = defaultdict(list)
    for r in chosen:
        buckets[int(r['at'] // step) * step].append(r)
    samples = []
    if high > low:
        for at in range(int(low // step) * step, int(high // step) * step + 1, step):
            if at >= high:
                break
            a = max(start, at)
            z = min(settled_end, at + step)
            eligible = sum(a <= t and t + 60 <= z for t in times)
            g = aggregate(buckets.get(at, []))
            samples.append(
                {
                    'at': a,
                    **g,
                    'confirmedUSD': g['confirmedUSD']
                    if g['requests'] or g['adjustments'] or eligible
                    else None,
                    'warmMinutes': eligible,
                }
            )
    distributions = []
    for field in ('prompt', 'output'):
        values = [r[field] for r in chosen if r['micro_usd'] >= 0 and r[field] is not None]
        counts = [0] * len(BINS)
        for value in values:
            counts[next(i for i, (limit, _) in enumerate(BINS) if value <= limit)] += 1
        distributions.append(
            {
                'kind': field,
                'samples': len(values),
                'bins': [
                    {
                        'label': label,
                        'count': counts[i],
                        'percent': counts[i] * 100 / len(values) if values else 0,
                    }
                    for i, (_, label) in enumerate(BINS)
                ],
            }
        )
    # Fixed five-minute windows compare with the preceding hour for the SAME model.
    # Warm idle minutes remain in the denominator; cold/missing time cannot be idle.
    windows = defaultdict(list)
    exposure = defaultdict(set)
    for r in qualified:
        if r['micro_usd'] >= 0:
            windows[(r['model'], int(r['at'] // 300) * 300)].append(r)
    for at, ids in ready.items():
        for m in ids:
            exposure[(m, int(at // 300) * 300)].add(at)
    alerts = []
    for (model, at), recent in windows.items():
        if at < start or at + 300 > settled_end or (selected and model != selected):
            continue
        previous = [r for t in range(at - 3600, at, 300) for r in windows.get((model, t), [])]
        minutes = sum(len(exposure.get((model, t), set())) for t in range(at - 3600, at, 300))
        recent_minutes = len(exposure.get((model, at), set()))
        if recent_minutes < 4 or minutes < 30 or len(previous) < 30 or len(recent) < 20:
            continue
        ratio = (len(recent) / recent_minutes) / (len(previous) / minutes)
        old = aggregate(previous)
        new = aggregate(recent)
        if ratio >= 3:
            alerts.append(
                {
                    'at': at,
                    'model': model,
                    'kind': 'Request burst',
                    'ratio': round(ratio, 2),
                    'detail': f'{len(recent)} credited requests in {recent_minutes} warm minutes; {ratio:.1f}× the preceding hour’s rate.',
                }
            )
        if (
            old['outputSamples'] >= 30
            and new['outputSamples'] >= 20
            and old['meanOutput']
            and new['meanOutput'] is not None
        ):
            shift = new['meanOutput'] / old['meanOutput']
            if shift >= 3 or shift <= 1 / 3:
                alerts.append(
                    {
                        'at': at,
                        'model': model,
                        'kind': 'Output size shift',
                        'ratio': round(shift, 2),
                        'detail': f'Mean output {new["meanOutput"]:.0f} vs {old["meanOutput"]:.0f} tokens in the preceding hour.',
                    }
                )
    return {
        'at': now,
        'from': start,
        'to': end,
        'settledThrough': max(0, settled_end),
        'selectedModel': selected,
        'totals': total,
        'models': models,
        'samples': samples,
        'bucketSeconds': step,
        'distributions': distributions,
        'unusual': sorted(alerts, key=lambda a: a['at'], reverse=True)[:50],
        'excludedUnverifiedCredits': excluded,
        'coverageStart': min(times) if times else None,
        'coverageEnd': max(times) + 60 if times else None,
        'scope': 'This Mac · credited inference jobs during verified warm time · 2-minute settlement delay',
    }
