"""Passive, device-scoped model history independent of any switching schedule.

Old observations stay visible without being promoted to verified warm evidence.
Read only: no backfill, model commands, or changes to the financial ledger.
"""

import math
from collections import defaultdict
from model_combinations import members
from workload import aggregate, credit_measurements
from demand_baselines import conditional, network_minutes, read_view


def unit_earnings(rows):
    """Realized job yields, weighted by matching requests/tokens, never local counters."""
    stats = aggregate(rows)
    return {
        key: stats[key]
        for key in (
            'requests',
            'adjustments',
            'usdPerRequest',
            'usdPerMillionOutput',
            'usdPerMillionTokens',
            'outputSamples',
            'completeTokenSamples',
        )
    }


def model_history(store, account, device, start, end, now, selected=None, signals=None):
    with read_view(store) as view:
        return _model_history(view, account, device, start, end, now, selected, signals)


def _model_history(store, account, device, start, end, now, selected=None, signals=None):
    if (
        not all(
            isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
            for x in (start, end, now)
        )
        or start < 0
        or end <= start
    ):
        raise ValueError('Choose a valid model history range.')
    end = min(end, now)
    evidence = store.evidence(account, device, start, end, now) if end > start else {}
    solo = [m for m in evidence if len(members(m)) == 1]
    network = (
        network_minutes(store, solo, start, min(end, now - 120)) if signals is not None else {}
    )
    comparisons = {}
    compared_minutes = {}
    for model in solo:
        value = conditional(
            evidence[model],
            network.get(model, {}),
            (signals or {}).get(model, {}),
            now,
            include_minutes=True,
        )
        compared_minutes[model] = set(value.pop('matchedMinutes'))
        comparisons[model] = value
    with read_view(store) as view:
        db = view.h.db
        # A newer minute takes priority over any legacy record for the same
        # device/minute. Their precise sub-minute overlap cannot be recovered.
        older = db.execute(
            """SELECT model,at,seconds FROM opt_minutes m
            WHERE account=? AND device=? AND at+60>? AND at<? AND seconds>0 AND seconds<=60.000001
            AND NOT EXISTS(SELECT 1 FROM opt_ready_minutes r WHERE r.account=m.account AND r.device=m.device AND r.at=m.at)
            ORDER BY at""",
            (account, device, start, end),
        ).fetchall()
        warm = db.execute(
            """SELECT model,at,seconds FROM opt_ready_minutes
            WHERE account=? AND device=? AND at+60>? AND at<? AND seconds>0 AND seconds<=60.000001 ORDER BY at""",
            (account, device, start, end),
        ).fetchall()
        credits = db.execute(
            """SELECT c.at,c.model,c.micro_usd,c.tokens,t.prompt_tokens,t.output_tokens,t.id AS detail_id
            FROM opt_credits c LEFT JOIN workload_tokens t ON t.account=c.account AND t.id=c.id
            WHERE c.account=? AND c.at>=? AND c.at<? AND c.model!='base_reward'
            AND c.provider IN (SELECT provider FROM opt_identity WHERE device=?) ORDER BY c.at,c.id""",
            (account, start, end, device),
        ).fetchall()
        credit_coverage = [
            tuple(r)
            for r in db.execute(
                'SELECT start,end FROM opt_coverage WHERE account=? AND end>? AND start<? ORDER BY start',
                (account, start, end),
            )
        ]

    # Exact solo/pair evidence has already passed the full warm-minute, identity,
    # coverage and settlement gates. Do not mix a model's pair jobs into its solo yield.
    configurations = defaultdict(set)
    for configuration, e in evidence.items():
        for minute in e['minutes']:
            for model in members(configuration):
                configurations[(minute['at'], model)].add(configuration)
    matched = defaultdict(list)
    paid_groups = {}
    for credit in credits:
        g = paid_groups.setdefault(
            credit['model'],
            {
                'model': credit['model'],
                'microUSD': 0,
                'jobs': 0,
                'first': credit['at'],
                'last': credit['at'],
            },
        )
        g['microUSD'] += credit['micro_usd']
        g['jobs'] += 1
        g['last'] = credit['at']
        for configuration in configurations.get(
            (int(credit['at'] // 60) * 60, credit['model']), ()
        ):
            matched[configuration].append(credit_measurements(credit))
    paid = [{**g, 'usd': g['microUSD'] / 1e6} for g in paid_groups.values()]
    rows = {}

    def row(model):
        if model not in rows:
            rows[model] = {
                'id': model,
                'members': members(model),
                'credits': None,
                'warmHours': 0,
                'earlierHours': 0,
                'first': None,
                'last': None,
                'evidence': {k: v for k, v in evidence.get(model, {}).items() if k != 'minutes'},
                'unitEarnings': unit_earnings(matched.get(model, [])),
                'demandComparison': comparisons.get(model),
                'demandUnitEarnings': unit_earnings(
                    [
                        c
                        for c in matched.get(model, [])
                        if int(c['at'] // 60) * 60 in compared_minutes.get(model, set())
                    ]
                ),
            }
        return rows[model]

    def extent(value, first, last):
        value['first'] = first if value['first'] is None else min(value['first'], first)
        value['last'] = last if value['last'] is None else max(value['last'], last)

    for source, key in ((older, 'earlierHours'), (warm, 'warmHours')):
        for r in source:
            low, high = max(start, r['at']), min(end, r['at'] + 60)
            if high <= low:
                continue
            value = row(r['model'])
            value[key] += min(60, r['seconds']) * (high - low) / 60 / 3600
            extent(value, low, high)
    for r in paid:
        value = row(r['model'])
        value['credits'] = {k: r[k] for k in ('usd', 'jobs', 'first', 'last')}
        extent(value, r['first'], r['last'])

    output = sorted(rows.values(), key=lambda r: (-(r['credits'] or {}).get('usd', 0), r['id']))
    if selected is None:
        selected = output[0]['id'] if output else None
    chosen = rows.get(selected)
    samples = []
    step = 3600
    if chosen:
        low, high = chosen['first'], chosen['last']
        step = max(3600, math.ceil(max(0, high - low) / 240 / 3600) * 3600)
        groups = {}

        def bucket(at):
            key = math.floor(at / step) * step
            return groups.setdefault(
                key,
                {
                    'microUSD': 0,
                    'paidJobs': 0,
                    'warmHours': 0,
                    'earlierHours': 0,
                    'matchedUSD': 0,
                    'matchedSeconds': 0,
                },
            )

        for source, name in ((older, 'earlierHours'), (warm, 'warmHours')):
            for r in source:
                if r['model'] != selected:
                    continue
                begin, finish = max(start, r['at']), min(end, r['at'] + 60)
                bucket(r['at'])[name] += min(60, r['seconds']) * max(0, finish - begin) / 60 / 3600
        for r in evidence.get(selected, {}).get('minutes', []):
            b = bucket(r['at'])
            b['matchedUSD'] += r['usd']
            b['matchedSeconds'] += r['seconds']
        for r in credits:
            if r['model'] != selected:
                continue
            b = bucket(r['at'])
            b['microUSD'] += r['micro_usd']
            b['paidJobs'] += 1
        yield_buckets = defaultdict(list)
        for credit in matched.get(selected, []):
            yield_buckets[int(credit['at'] // step) * step].append(credit)
        demand_buckets = defaultdict(list)
        for m in evidence.get(selected, {}).get('minutes', []):
            if m['at'] in compared_minutes.get(selected, set()):
                demand_buckets[int(m['at'] // step) * step].append(m)
        # Emit bounded gaps, never interpolate unknown history or call it idle.
        for at in range(int(low // step) * step, int(high // step) * step + 1, step):
            begin, finish = max(start, at), min(end, at + step)
            if finish <= begin:
                continue
            b = groups.get(at)
            cursor = begin
            for a, z in credit_coverage:
                if a <= cursor < z:
                    cursor = max(cursor, z)
                if cursor >= finish:
                    break
            covered = cursor >= finish
            solo = len(chosen['members']) == 1
            demand_yields = unit_earnings(
                [
                    c
                    for c in yield_buckets.get(at, [])
                    if int(c['at'] // 60) * 60 in compared_minutes.get(selected, set())
                ]
            )
            dm = demand_buckets.get(at, [])
            seconds = sum(m['seconds'] for m in dm)
            samples.append(
                {
                    'at': begin,
                    **unit_earnings(yield_buckets.get(at, [])),
                    'matchedUsdPerWarmHour': sum(m['usd'] for m in dm) * 3600 / seconds
                    if seconds
                    else None,
                    **{
                        'matched' + key[0].upper() + key[1:]: demand_yields[key]
                        for key in ('usdPerRequest', 'usdPerMillionOutput', 'usdPerMillionTokens')
                    },
                    'confirmedUSD': (
                        b['microUSD'] / 1e6 if b and b['paidJobs'] else 0 if covered else None
                    )
                    if solo
                    else None,
                    'creditCoverageComplete': covered,
                    'warmHours': b['warmHours'] if b and b['warmHours'] else None,
                    'earlierHours': b['earlierHours'] if b and b['earlierHours'] else None,
                    'usdPerWarmHour': b['matchedUSD'] * 3600 / b['matchedSeconds']
                    if b and b['matchedSeconds']
                    else None,
                }
            )
    firsts = [r['first'] for r in output if r['first'] is not None]
    lasts = [r['last'] for r in output if r['last'] is not None]
    return {
        'at': now,
        'from': start,
        'to': end,
        'models': output,
        'selectedModel': selected,
        'totals': {
            'inferenceUSD': sum(r['usd'] for r in paid),
            'paidJobs': sum(r['jobs'] for r in paid),
            'warmHours': sum(r['warmHours'] for r in output),
            'earlierHours': sum(r['earlierHours'] for r in output),
        },
        'coverageStart': min(firsts) if firsts else None,
        'coverageEnd': max(lasts) if lasts else None,
        'samples': samples,
        'bucketSeconds': step,
        'scope': 'Saved inference credits matched to this Mac; base rewards excluded.',
        'historyResetsOnTestStart': False,
    }
