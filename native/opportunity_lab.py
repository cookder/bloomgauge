"""Prospective, passive scoring. Nothing in this module dispatches model commands.

An index is not USD/hour. Unselected models have no counterfactual earnings.
Quotes, mix, source times and the actual selection are frozen at observation.
"""

import json
import math
import threading
from collections import defaultdict
from bisect import bisect_left
from demand_baselines import read_view
from model_pricing import quote, comparison, number
from workload import aggregate, credit_measurements

VERSION = 'price-pressure-v1'
LOOKBACK = 7 * 86400


def coverage(intervals, start, end):
    cursor, seconds = start, 0
    for a, b in sorted(intervals):
        low, high = max(cursor, a, start), min(b, end)
        if high > low:
            seconds += high - low
            cursor = high
    return seconds / max(1, end - start)


def recent_mix(store, account, device, now):
    """Only solo, complete warm minutes; same-job token totals, never mean ratios."""
    with store.h.lock:
        rows = store.h.db.execute(
            """SELECT c.at,c.model,c.micro_usd,c.tokens,
          t.prompt_tokens,t.output_tokens,t.id AS detail_id,m.at AS minute
          FROM opt_credits c JOIN workload_tokens t ON t.account=c.account AND t.id=c.id
          JOIN opt_ready_minutes m ON m.account=c.account AND m.device=? AND m.model=c.model
            AND m.at=CAST(c.at/60 AS INTEGER)*60 AND m.seconds BETWEEN 59.999999 AND 60.000001
          WHERE c.account=? AND c.at>=? AND m.at+60<=? AND c.micro_usd>=0
          AND EXISTS(SELECT 1 FROM opt_identity i WHERE i.device=m.device AND i.provider=c.provider)
          AND EXISTS(SELECT 1 FROM opt_coverage v WHERE v.account=c.account AND v.start<=m.at AND v.end>=m.at+60)
          ORDER BY c.at""",
            (device, account, now - LOOKBACK, now - 120),
        ).fetchall()
    groups, minutes = defaultdict(list), defaultdict(set)
    for row in rows:
        measured = credit_measurements(row)
        groups[row['model']].append(measured)
        if measured['prompt'] is not None and measured['output'] is not None:
            minutes[row['model']].add(row['minute'])
    return {
        m: {
            **aggregate(values),
            'measuredMinutes': len(minutes[m]),
            'asOf': now,
            'from': now - LOOKBACK,
            'to': now - 120,
        }
        for m, values in groups.items()
    }


def factors(rows, price, mix, now):
    # Deduplicate cadence slots: bursts of polls do not manufacture coverage.
    slots = {int(r['at'] // 30): r for r in rows if now - 900 <= r['at'] <= now}
    values = sorted(slots.values(), key=lambda r: r['at'])
    valid = [r for r in values if r['warm'] > 0]
    covered = min(1, len(valid) / 30)
    last = values[-1]['at'] if values else None
    fresh = last is not None and 0 <= now - last <= 90
    pressure = sum(r['active'] / r['warm'] for r in valid) / len(valid) if valid else None
    measured = comparison(mix, price)
    fraction = measured['inputFraction']
    enough_mix = bool(
        fraction is not None
        and mix.get('completeTokenSamples', 0) >= 50
        and mix.get('measuredMinutes', 0) >= 30
        and number(mix.get('lastMeasuredAt'))
        and 0 <= now - mix['lastMeasuredAt'] <= 86400
    )
    i, o = price['inputUSDPerMillion'], price['outputUSDPerMillion']
    blend = i * 0.85 + o * 0.15 if i is not None and o is not None else None
    used_blend = measured['blendUSDPerMillion'] if enough_mix else blend
    qualified = fresh and covered >= 0.8 and price['fresh'] and pressure is not None
    score = pressure * used_blend if qualified and used_blend is not None else None
    return {
        'pressure': pressure,
        'active': sum(r['active'] for r in values) / len(values) if values else None,
        'queued': sum(r['queued'] for r in values) / len(values) if values else None,
        'warm': sum(r['warm'] for r in values) / len(values) if values else None,
        'coverage': covered,
        'sourceAt': last,
        'fresh': fresh,
        'price': price,
        'mix': measured,
        'mixFrom': mix.get('from'),
        'mixTo': mix.get('to'),
        'mixLastAt': mix.get('lastMeasuredAt'),
        'mixMinutes': mix.get('measuredMinutes', 0),
        'mixBasis': 'measured' if enough_mix else 'assumed',
        'inputFractionUsed': fraction if enough_mix else 0.85,
        'blend': used_blend,
        'score': score,
        'fixedScore': pressure * blend if qualified and blend is not None else None,
        'priceOnlyRange': [pressure * min(i, o), pressure * max(i, o)]
        if qualified and i is not None and o is not None
        else None,
        'reason': 'Collecting 15-minute demand coverage'
        if not fresh or covered < 0.8
        else 'Current published price unavailable'
        if not price['fresh']
        else None,
    }


class OpportunityLab:
    def __init__(self, store):
        self.store, self.h = store, store.h
        self.lock = threading.RLock()
        self.mix_key, self.mix_at, self.mix = None, 0, {}
        with self.h.lock:
            self.h.db.executescript("""CREATE TABLE IF NOT EXISTS opportunity_observations(
              account TEXT,device TEXT,at REAL,source_at REAL,version TEXT,payload TEXT,
              PRIMARY KEY(account,device,source_at,version));
              CREATE INDEX IF NOT EXISTS opportunity_scope ON opportunity_observations(account,device,at);""")
            self.h.db.commit()

    def record(self, account, device, now, network, current, ready, mode, available):
        capacity = network.get('capacity') or {}
        source = capacity.get('updatedAt')
        if (
            not account
            or not device
            or capacity.get('status') != 'ok'
            or not number(source)
            or not 0 <= now - source <= 90
        ):
            return
        with self.lock:
            with self.h.lock:
                latest = self.h.db.execute(
                    'SELECT MAX(source_at) FROM opportunity_observations WHERE account=? AND device=? AND version=?',
                    (account, device, VERSION),
                ).fetchone()[0]
            if latest is not None and source <= latest:
                return
            with read_view(self.store) as view:
                if self.mix_key != (account, device) or not 0 <= now - self.mix_at < 900:
                    self.mix = recent_mix(view, account, device, now)
                    self.mix_key, self.mix_at = (account, device), now
                with view.h.lock:
                    rows = view.h.db.execute(
                        'SELECT * FROM opt_network WHERE at>=? AND at<=? ORDER BY at',
                        (now - 900, now),
                    ).fetchall()
                    decision = view.h.db.execute(
                        """SELECT updated,model,target,phase,reason FROM optimizer_decisions
                        WHERE account=? AND device=? AND updated<=? ORDER BY updated DESC LIMIT 1""",
                        (account, device, now),
                    ).fetchone()
            groups = defaultdict(list)
            for r in rows:
                groups[r['model']].append(dict(r))
            models = [
                {
                    'id': m,
                    'available': m in available,
                    **factors(
                        groups[m], quote(network.get('pricing'), m, now), self.mix.get(m, {}), now
                    ),
                }
                for m in groups
            ]
            models.sort(key=lambda m: (-(m['score'] if m['score'] is not None else -1), m['id']))

            def leader(key):
                eligible = [m for m in models if m['available'] and m[key] is not None]
                return max(eligible, key=lambda m: m[key])['id'] if eligible else None

            payload = {
                'at': now,
                'version': VERSION,
                'models': models,
                'selected': sorted(set(current)),
                'ready': bool(ready),
                'mode': mode,
                'leader': leader('score'),
                'fixedLeader': leader('fixedScore'),
                'decision': dict(decision)
                if decision and now - decision['updated'] <= 90
                else None,
            }
            with self.h.lock:
                self.h.db.execute(
                    'INSERT OR IGNORE INTO opportunity_observations VALUES(?,?,?,?,?,?)',
                    (account, device, now, source, VERSION, json.dumps(payload, allow_nan=False)),
                )
                self.h.db.commit()

    def report(self, account, device, start, end, now):
        if not all(number(v) for v in (start, end, now)) or end <= start:
            raise ValueError('Invalid opportunity period')
        with read_view(self.store) as view:
            return report(view, account, device, start, min(end, now), now)


def outcome_inputs(store, account, device, start, end):
    db = store.h.db
    credits = [
        dict(r)
        for r in db.execute(
            """SELECT c.at,c.model,c.micro_usd FROM opt_credits c
        WHERE c.account=? AND c.at>=? AND c.at<? AND c.model!='base_reward'
        AND EXISTS(SELECT 1 FROM opt_identity i WHERE i.device=? AND i.provider=c.provider) ORDER BY c.at""",
            (account, start, end, device),
        )
    ]
    intervals = [
        tuple(r)
        for r in db.execute(
            'SELECT start,end FROM opt_coverage WHERE account=? AND end>? AND start<?',
            (account, start, end),
        )
    ]
    warm = [
        dict(r)
        for r in db.execute(
            """SELECT at,model FROM opt_ready_minutes WHERE account=? AND device=? AND at>=? AND at+60<=?
        AND seconds BETWEEN 59.999999 AND 60.000001""",
            (account, device, start, end),
        )
    ]
    switches = [
        dict(r)
        for r in db.execute(
            "SELECT at,model FROM opt_events WHERE account=? AND device=? AND at>=? AND at<? AND kind='switching'",
            (account, device, start, end),
        )
    ]
    work = [
        dict(r)
        for r in db.execute(
            """SELECT model,MIN(first_work) AS first FROM demand_observed WHERE account=? AND device=?
        AND first_work>=? AND first_work<? GROUP BY model,start""",
            (account, device, start, end),
        )
    ]
    network = defaultdict(list)
    for r in db.execute(
        'SELECT model,at,active,warm FROM opt_network WHERE at>=? AND at<? ORDER BY at',
        (start, end),
    ):
        network[r['model']].append(dict(r))
    return {
        'credits': credits,
        'times': [r['at'] for r in credits],
        'intervals': intervals,
        'warm': warm,
        'switches': switches,
        'work': work,
        'network': network,
    }


def observed_outcome(inputs, model, start, seconds, now, pressure=None):
    end = start + seconds
    if end > now - 120:
        return {'minutes': seconds // 60, 'status': 'settling', 'usdPerClockHour': None}
    paid = inputs['credits'][
        bisect_left(inputs['times'], start) : bisect_left(inputs['times'], end)
    ]
    covered = coverage(inputs['intervals'], start, end)
    changed = any(start < r['at'] < end and r['model'] != model for r in inputs['switches']) or any(
        start <= r['at'] < end and r['model'] != model for r in inputs['warm']
    )
    minutes = {
        r['at']
        for r in inputs['warm']
        if r['model'] == model
        and start <= r['at']
        and r['at'] + 60 <= end
        and coverage(inputs['intervals'], r['at'], r['at'] + 60) >= 0.999999
    }
    warm_usd = (
        sum(
            r['micro_usd']
            for r in paid
            if r['model'] == model and int(r['at'] // 60) * 60 in minutes
        )
        / 1e6
    )
    usd = sum(r['micro_usd'] for r in paid) / 1e6
    slots = {
        int(r['at'] // 30): r
        for r in inputs['network'].get(model, [])
        if start <= r['at'] < end and r['warm'] > 0
    }
    p = (
        sum(r['active'] / r['warm'] for r in slots.values()) / len(slots)
        if len(slots) * 30 / seconds >= 0.8
        else None
    )
    first = min(
        (r['first'] for r in inputs['work'] if r['model'] == model and start <= r['first'] < end),
        default=None,
    )
    return {
        'minutes': seconds // 60,
        'status': 'complete' if covered >= 0.999999 else 'partial',
        'coveredPercent': covered * 100,
        'inferenceUSD': usd,
        'usdPerClockHour': usd * 3600 / seconds if covered >= 0.999999 else None,
        'warmUSDPerHour': warm_usd * 60 / len(minutes) if minutes else None,
        'warmMinutes': len(minutes),
        'selectionChanged': changed,
        'comparable': not changed and len(minutes) * 60 / seconds >= 0.8 and covered >= 0.999999,
        'firstWorkSeconds': first - start if first is not None else None,
        'demandRatio': p / pressure if p is not None and pressure and pressure > 0 else None,
    }


def smoothing_comparison(observations):
    """Replay saved indices only; never infer earnings for unchosen models."""
    variants = []
    for half_life in (0, 300, 900):
        ema = {}
        last = None
        universe = None
        leader = None
        changes = 0
        reversals = 0
        chain = []
        observations_used = 0
        resets = 0
        agreement = 0
        confirmed = 0
        lags = []
        unresolved = 0
        raw_leader = None
        raw_since = None
        raw_count = 0
        first_match = None
        counted = False
        for observation in observations:
            at = observation['at']
            values = {
                m['id']: m['score']
                for m in observation['models']
                if m['available'] and number(m.get('score'))
            }
            reset = (
                last is None or not 0 < at - last <= 150 or set(values) != universe or not values
            )
            if reset:
                if last is not None:
                    resets += 1
                if counted and first_match is None:
                    unresolved += 1
                ema = dict(values)
                leader = None
                chain = []
                raw_leader = None
                raw_count = 0
                counted = False
                first_match = None
            else:
                alpha = 1 if half_life == 0 else 1 - 2 ** (-(at - last) / half_life)
                ema = {m: ema[m] + alpha * (v - ema[m]) for m, v in values.items()}
            last = at
            universe = set(values)
            if not values:
                continue
            current = max(ema, key=lambda m: (ema[m], m))
            raw = max(values, key=lambda m: (values[m], m))
            observations_used += 1
            agreement += current == raw
            if current != leader:
                if leader is not None:
                    changes += 1
                if len(chain) >= 2 and chain[-2][0] == current and at - chain[-2][1] <= 900:
                    reversals += 1
                chain.append((current, at))
                chain = chain[-2:]
                leader = current
            if raw != raw_leader:
                if counted and first_match is None:
                    unresolved += 1
                raw_leader = raw
                raw_since = at
                raw_count = 0
                first_match = None
                counted = False
            raw_count += 1
            if current == raw and first_match is None:
                first_match = at
            if raw_count >= 3 and at - raw_since >= 120 and not counted:
                counted = True
                confirmed += 1
                if first_match is not None:
                    lags.append(first_match - raw_since)
            elif counted and current == raw and first_match == at:
                lags.append(first_match - raw_since)
        if counted and first_match is None:
            unresolved += 1
        variants.append(
            {
                'halfLifeSeconds': half_life,
                'observations': observations_used,
                'leaderChanges': changes,
                'reversals': reversals,
                'agreementPercent': 100 * agreement / observations_used
                if observations_used
                else None,
                'confirmedLeaders': confirmed,
                'unresolvedLeaders': unresolved,
                'meanFollowSeconds': sum(lags) / len(lags) if lags else None,
                'resets': resets,
                'leader': leader,
            }
        )
    return {
        'passive': True,
        'variants': variants,
        'method': 'Elapsed-time EMA of the saved 15-minute price-pressure index; 5m and 15m half-lives. Resets after 150s gaps or changes to the available score set. Reversals are A→B→A within 15m. Follow delay uses leaders seen at least three times over two minutes; unresolved cases stay counted separately. Rankings, not simulated switches or earnings.',
    }


def report(store, account, device, start, end, now):
    db = store.h.db
    first, last, count = db.execute(
        'SELECT MIN(at),MAX(at),COUNT(*) FROM opportunity_observations WHERE account=? AND device=? AND version=?',
        (account, device, VERSION),
    ).fetchone()
    latest = db.execute(
        'SELECT payload FROM opportunity_observations WHERE account=? AND device=? AND version=? ORDER BY at DESC LIMIT 1',
        (account, device, VERSION),
    ).fetchone()
    # Bound response density while keeping real collection gaps as explicit nulls.
    low = max(start, first or start)
    step = max(60, math.ceil(max(0, end - low) / 300 / 60) * 60)
    rows = db.execute(
        """SELECT at,payload FROM opportunity_observations WHERE account=? AND device=? AND version=?
        AND at>=? AND at<? ORDER BY at""",
        (account, device, VERSION, low, end),
    ).fetchall()
    groups, checkpoints = defaultdict(list), {}
    observations = []
    for row in rows:
        d = json.loads(row['payload'])
        observations.append(d)
        groups[int((row['at'] - low) // step)].append(d)
        if d['ready'] and len(d['selected']) == 1:
            checkpoints.setdefault(int(row['at'] // 3600), d)
    history = []
    for bucket in range(int(max(0, end - low) // step) + 1) if rows else []:
        readings = groups.get(bucket, [])
        sums = defaultdict(list)
        for d in readings:
            for m in d['models']:
                if m['score'] is not None:
                    sums[m['id']].append(m['score'])
        history.append(
            {'at': low + bucket * step, 'scores': {m: sum(v) / len(v) for m, v in sums.items()}}
        )
    checks = sorted(checkpoints.values(), key=lambda d: d['at'])[-24:]
    runs = db.execute(
        """SELECT id,at,model,payload,downtime,result FROM demand_switch_runs WHERE account=? AND device=?
        AND at>=? AND at<? ORDER BY at DESC LIMIT 12""",
        (account, device, max(start, first or now), end),
    ).fetchall()
    earliest = min([d['at'] for d in checks] + [r['at'] for r in runs], default=now)
    inputs = (
        outcome_inputs(store, account, device, earliest, min(now, end + 3600))
        if checks or runs
        else None
    )
    evaluations = []
    for d in reversed(checks):
        model = d['selected'][0]
        row = next((m for m in d['models'] if m['id'] == model), {})
        evaluations.append(
            {
                'at': d['at'],
                'model': model,
                'leader': d['leader'],
                'fixedLeader': d['fixedLeader'],
                'score': row.get('score'),
                'decision': d['decision'],
                'outcomes': [
                    observed_outcome(inputs, model, d['at'], s, now, row.get('pressure'))
                    for s in (900, 1800, 3600)
                ],
            }
        )
    switches = []
    for r in runs:
        saved = json.loads(r['payload'])
        candidate = saved.get('candidate') or {}
        estimate = candidate.get('estimate') or {}
        switches.append(
            {
                'id': r['id'],
                'at': r['at'],
                'model': r['model'],
                'result': r['result'],
                'reason': saved.get('reason'),
                'kind': saved.get('kind'),
                'switchSeconds': r['downtime'],
                'forecastUSDPerWarmHour': estimate.get('rate')
                if estimate.get('forecastUsable')
                else None,
                'outcomes': [
                    observed_outcome(
                        inputs,
                        r['model'],
                        r['at'],
                        s,
                        now,
                        (candidate.get('signal') or {}).get('pressure'),
                    )
                    for s in (900, 1800, 3600)
                ],
            }
        )
    return {
        'at': now,
        'version': VERSION,
        'passive': True,
        'from': start,
        'to': end,
        'bucketSeconds': step,
        'coverageStart': first,
        'coverageEnd': last,
        'observations': count,
        'fresh': last is not None and 0 <= now - last <= 120,
        'latest': json.loads(latest['payload']) if latest else None,
        'history': history,
        'evaluations': evaluations,
        'switches': switches,
        'smoothing': smoothing_comparison(observations),
        'scope': 'This Mac; confirmed inference only. Unchosen models have unknown outcomes.',
    }
