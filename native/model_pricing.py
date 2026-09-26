"""Published current quotes, separate from this Mac's confirmed job payments."""

import math


def number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def quote(feed, model, now):
    feed = feed if isinstance(feed, dict) else {}
    data = feed.get('data') if isinstance(feed.get('data'), dict) else {}
    rows = data.get('prices', [])
    matches = (
        [r for r in rows if isinstance(r, dict) and r.get('model') == model]
        if isinstance(rows, list)
        else []
    )
    # Missing explicit prices are unknown. Do not silently apply a fallback
    # quote, merge model variants, or replace a malformed component with zero.
    row = matches[0] if len(matches) == 1 else {}
    at = feed.get('updatedAt')
    fresh = feed.get('status') == 'ok' and number(at) and 0 <= now - at <= 1800
    values = [row.get(k) for k in ('input_price', 'output_price')]
    valid = all(number(v) for v in values)
    return {
        'inputUSDPerMillion': values[0] / 1e6 if valid else None,
        'outputUSDPerMillion': values[1] / 1e6 if valid else None,
        'at': at if number(at) else None,
        'fresh': bool(fresh and valid),
        'status': 'unlisted'
        if not matches
        else 'invalid'
        if not valid
        else 'current'
        if fresh
        else 'stale',
    }


def comparison(stats, price):
    prompt, output = stats.get('measuredPromptTokens', 0), stats.get('measuredOutputTokens', 0)
    total = prompt + output
    fraction = prompt / total if total else None
    i, o = price['inputUSDPerMillion'], price['outputUSDPerMillion']
    blend = (
        i * fraction + o * (1 - fraction)
        if fraction is not None and i is not None and o is not None
        else None
    )
    return {
        **price,
        'inputFraction': fraction,
        'completeJobs': stats.get('completeTokenSamples', 0),
        'totalJobs': stats.get('requests', 0),
        'blendUSDPerMillion': blend,
        'realizedUSDPerMillion': stats.get('usdPerMillionTokens'),
        'measuredPromptTokens': prompt,
        'measuredOutputTokens': output,
    }


def enrich_workload(report, feed, now):
    for row in report['models']:
        row['pricing'] = comparison(row, quote(feed, row['id'], now))
    report['priceBasis'] = (
        'Current public quote at the observed token mix; not the historical price paid.'
    )
    return report
