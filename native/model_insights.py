"""Read-only manual-choice observations and conditional eight-hour scenarios.

Concurrency is not an arrival rate. Warm-hour income is not clock-hour income.
No provider inspection, model commands, network requests or historical writes.
"""

import copy
import json
import math
import sqlite3
import threading
import time
from collections import OrderedDict, defaultdict
from datetime import datetime, timedelta
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from demand_baselines import read_view, network_minutes, joined_minutes, near
from model_combinations import members
from workload import aggregate, credit_measurements

HORIZON = 28800
LOOKBACK = 28 * 86400
RECENT = 7 * 86400


class InsightsUnavailable(Exception):
    pass


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def query(value):
    if not isinstance(value, str) or len(value) > 20000:
        raise ValueError('Choose a supported model insights query.')
    values = parse_qs(value, keep_blank_values=True, max_num_fields=34)
    if set(values) != {'timezone', 'model'} or len(values['timezone']) != 1:
        raise ValueError('Provide one timezone and the models to compare.')
    zone_name = values['timezone'][0]
    if not zone_name or len(zone_name) > 100:
        raise ValueError('Choose a valid IANA timezone.')
    try:
        zone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError('Choose a valid IANA timezone.') from None
    models = values['model']
    if (
        not 1 <= len(models) <= 32
        or len(set(models)) != len(models)
        or any(
            not 1 <= len(m) <= 512 or m != m.strip() or any(ord(c) < 32 for c in m) for m in models
        )
    ):
        raise ValueError('Choose one to32 distinct model IDs.')
    return models, zone


def check_time(deadline):
    if deadline is not None and time.monotonic() >= deadline:
        raise InsightsUnavailable(
            'Model statistics took too long. Switching remains available; try again.'
        )


def segments(start, end, zone):
    """UTC minute boundaries handle local folds, gaps and non-hour offsets."""
    result = []
    cursor = start
    while cursor < end:
        stop = min(end, (math.floor(cursor / 60) + 1) * 60)
        local = datetime.fromtimestamp(cursor, zone)
        key = (local.date().isoformat(), local.hour, local.utcoffset(), local.fold)
        if result and result[-1]['key'] == key:
            result[-1]['to'] = stop
        else:
            result.append(
                {
                    'key': key,
                    'from': cursor,
                    'to': stop,
                    'date': key[0],
                    'hour': local.hour,
                    'day': local.weekday(),
                }
            )
        cursor = stop
    return result


def historical_hours(start, end, zone):
    # Padding exposes complete boundaries; boundary hours outside the range are
    # discarded. Repeated date/hour instances share real exposure, not one hour.
    groups = {}
    for part in segments(math.floor(start / 60) * 60 - 7200, math.ceil(end / 60) * 60 + 7200, zone):
        key = (part['date'], part['hour'])
        item = groups.setdefault(key, {**part, 'seconds': 0})
        item['from'] = min(item['from'], part['from'])
        item['to'] = max(item['to'], part['to'])
        item['seconds'] += part['to'] - part['from']
    return {
        key: item for key, item in groups.items() if item['from'] >= start and item['to'] <= end
    }


def calendar_load(view, models, start, end, zone, deadline):
    hours = historical_hours(start, end, zone)
    placeholders = ','.join('?' for _ in models)
    rows = view.h.db.execute(
        """WITH latest AS (
        SELECT model,CAST(at/30 AS INTEGER) slot,MAX(at) at FROM opt_network
        WHERE model IN ("""
        + placeholders
        + """) AND at>=? AND at<? GROUP BY model,slot)
        SELECT n.model,n.at,n.active,n.queued,n.warm FROM latest l
        JOIN opt_network n ON n.model=l.model AND n.at=l.at ORDER BY n.model,n.at""",
        (*models, start, end),
    )
    groups = {}
    for index, row in enumerate(rows):
        if index % 1024 == 0:
            check_time(deadline)
        if not all(finite(row[k]) and row[k] >= 0 for k in ('at', 'active', 'queued', 'warm')):
            continue
        local = datetime.fromtimestamp(row['at'], zone)
        key = (local.date().isoformat(), local.hour)
        hour = hours.get(key)
        if hour is None:
            continue
        g = groups.setdefault(
            (row['model'], key),
            {
                **hour,
                'key': key,
                'samples': 0,
                'activeSum': 0,
                'loadSum': 0,
                'warmSum': 0,
                'pressureSum': 0,
                'positiveWarmSamples': 0,
                'asOf': None,
            },
        )
        g['samples'] += 1
        g['activeSum'] += row['active']
        g['loadSum'] += row['active'] + row['queued']
        g['warmSum'] += row['warm']
        if row['warm'] > 0:
            g['pressureSum'] += (row['active'] + row['queued']) / row['warm']
            g['positiveWarmSamples'] += 1
        g['asOf'] = row['at']
    result = defaultdict(list)
    for (model, _), item in groups.items():
        if item['samples'] * 30 >= item['seconds'] * 0.8:
            result[model].append(item)
    return result


def demand_part(hours, part, now):
    for exact in (True, False):
        rows = [
            r
            for r in hours
            if r['hour'] == part['hour']
            and (r['day'] == part['day'] if exact else (r['day'] >= 5) == (part['day'] >= 5))
        ]
        if (
            len({r['date'] for r in rows}) < 3
            or now - max((r['asOf'] for r in rows), default=0) > RECENT
        ):
            continue
        count = sum(r['samples'] for r in rows)
        positive = sum(r['positiveWarmSamples'] for r in rows)
        return {
            'basis': 'weekday_hour' if exact else 'daytype_hour',
            'rows': rows,
            'active': sum(r['activeSum'] for r in rows) / count,
            'load': sum(r['loadSum'] for r in rows) / count,
            'warm': sum(r['warmSum'] for r in rows) / count,
            'pressure': sum(r['pressureSum'] for r in rows) / positive
            if positive >= 0.8 * count
            else None,
        }
    return None


def warm_evidence(view, account, device, models, start, end, now, deadline):
    """Only the solo paid-minute fields this report consumes.

    Preserve OptimizerStore.evidence's exact minute, identity and settlement
    predicates. A prefix maximum of individual poll interval ends implements
    its correlated EXISTS without rescanning coverage for every warm minute.
    Adjacent/overlapping intervals are never merged to qualify a minute.
    """
    models = [m for m in models if len(members(m)) == 1]
    if not models:
        return {}
    check_time(deadline)
    end = min(end, now - 120)
    placeholders = ','.join('?' for _ in models)
    db = view.h.db
    coverage = db.execute(
        """SELECT start,end FROM opt_coverage
        WHERE account=? AND start<=? AND end>=? ORDER BY start""",
        (account, end, start + 60),
    ).fetchall()
    paid = {
        (r['at'], r['model']): r
        for r in db.execute(
            """
        SELECT CAST(c.at/60 AS INT)*60 AS at,c.model,SUM(c.micro_usd)/1000000.0 AS usd,COUNT(*) AS paidJobs
        FROM opt_credits c
        WHERE c.account=? AND c.at>=? AND c.at<? AND c.model!='base_reward'
          AND c.model IN ("""
            + placeholders
            + """)
          AND EXISTS(SELECT 1 FROM opt_identity i WHERE i.device=? AND i.provider=c.provider)
        GROUP BY CAST(c.at/60 AS INT),c.model""",
            (account, start, end, *models, device),
        )
    }
    rows = db.execute(
        """SELECT at,model,seconds,jobs,tokens,busy FROM opt_ready_minutes
        WHERE account=? AND device=? AND at>=? AND at+60<=?
          AND seconds>=59.999999 AND seconds<=60.000001
          AND model IN ("""
        + placeholders
        + """) ORDER BY at""",
        (account, device, start, end, *models),
    )
    index = 0
    covered_end = -math.inf
    groups = {}
    for count, row in enumerate(rows):
        if count % 1024 == 0:
            check_time(deadline)
        while index < len(coverage) and coverage[index]['start'] <= row['at']:
            covered_end = max(covered_end, coverage[index]['end'])
            index += 1
        if covered_end < row['at'] + 60:
            continue
        credit = paid.get((row['at'], row['model']), {'usd': 0, 'paidJobs': 0})
        group = groups.setdefault(row['model'], {'seconds': 0, 'usd': 0, 'jobs': 0, 'minutes': []})
        group['seconds'] += row['seconds']
        group['usd'] += credit['usd']
        group['jobs'] += credit['paidJobs']
        group['minutes'].append(
            {
                'at': row['at'],
                'usd': credit['usd'],
                'seconds': row['seconds'],
                'paidJobs': credit['paidJobs'],
                'requests': row['jobs'],
                'tokens': row['tokens'],
                'busy': row['busy'],
            }
        )
    check_time(deadline)
    return {
        model: {
            'hours': g['seconds'] / 3600,
            'usd': g['usd'],
            'jobs': g['jobs'],
            'usdPerHour': g['usd'] * 3600 / g['seconds'],
            'minutes': g['minutes'],
        }
        for model, g in groups.items()
    }


def credit_rows(view, account, device, models, start, end, evidence, deadline):
    ready = {
        model: {m['at'] for m in evidence.get(model, {}).get('minutes', [])}
        for model in models
        if len(members(model)) == 1
    }
    placeholders = ','.join('?' for _ in models)
    rows = view.h.db.execute(
        """SELECT c.at,c.model,c.micro_usd,c.tokens,
        t.prompt_tokens,t.output_tokens,t.id AS detail_id FROM opt_credits c
        LEFT JOIN workload_tokens t ON t.account=c.account AND t.id=c.id
        WHERE c.account=? AND c.at>=? AND c.at<? AND c.model IN ("""
        + placeholders
        + """)
        AND c.model!='base_reward' AND EXISTS(SELECT 1 FROM opt_identity i WHERE i.device=? AND i.provider=c.provider)
        ORDER BY c.at,c.id""",
        (account, start, end, *models, device),
    )
    result = defaultdict(list)
    for index, row in enumerate(rows):
        if index % 1024 == 0:
            check_time(deadline)
        if (
            finite(row['at'])
            and finite(row['micro_usd'])
            and int(row['at'] // 60) * 60 in ready.get(row['model'], set())
        ):
            result[row['model']].append(credit_measurements(row))
    return result


def local_dates(rows, zone):
    return {datetime.fromtimestamp(r['at'], zone).date() for r in rows}


def income_part(evidence, network, target, part, now, zone, paid_counts):
    empty = {'supported': False, 'rows': [], 'coverage': None, 'basis': None, 'rate': None}
    if target is None or target['pressure'] is None or target['warm'] < 1:
        return empty
    exposure, overlap = defaultdict(float), defaultdict(float)
    for m in evidence.get('minutes', []):
        key = int(m['at'] // 1800)
        exposure[key] += m['seconds']
        if m['at'] in network:
            overlap[key] += m['seconds']
    covered = {k for k, seconds in exposure.items() if overlap[k] >= 0.8 * seconds}
    joined = [
        m
        for m in joined_minutes(evidence, network, now)
        if int(m['at'] // 1800) in covered
        and all(
            near(m[k], target[k], k in ('active', 'load'))
            for k in ('pressure', 'active', 'warm', 'load')
        )
    ]
    clock = datetime.fromtimestamp((part['from'] + part['to']) / 2, zone).replace(
        tzinfo=None, minute=0, second=0, microsecond=0
    )
    result = empty
    for exact in (True, False):
        neighborhood = [clock + timedelta(hours=i) for i in range(-2, 3)]
        labels = {(d.weekday() if exact else d.weekday() >= 5, d.hour) for d in neighborhood}
        rows = []
        for m in joined:
            local = datetime.fromtimestamp(m['at'], zone)
            if (local.weekday() if exact else local.weekday() >= 5, local.hour) in labels:
                rows.append(m)
        periods = defaultdict(float)
        for m in rows:
            periods[int(m['at'] // 1800)] += m['seconds']
        substantial = {k for k, seconds in periods.items() if seconds >= 900}
        rows = [m for m in rows if int(m['at'] // 1800) in substantial]
        seconds = sum(m['seconds'] for m in rows)
        usd = sum(m['usd'] for m in rows)
        jobs = sum(paid_counts.get(m['at'], 0) for m in rows)
        supported = bool(
            seconds >= 14400
            and len(local_dates(rows, zone)) >= 3
            and len(substantial) >= 8
            and jobs >= 200
            and now - max((m['at'] + 60 for m in rows), default=0) <= RECENT
            and usd >= 0
        )
        result = {
            'supported': supported,
            'rows': rows,
            'basis': 'weekday_time' if exact else 'daytype_time',
            'rate': usd * 3600 / seconds if supported else None,
            'coverage': min((overlap[k] / exposure[k] for k in substantial), default=None),
        }
        if supported:
            break
    return result


def combined_basis(values):
    values = {v for v in values if v is not None}
    return next(iter(values)) if len(values) == 1 else 'mixed' if values else None


def model_row(model, evidence, credits, hours, network, parts, now, zone, deadline, scope_ok):
    earned = evidence.get(model, {}) if len(members(model)) == 1 else {}
    stats = aggregate(credits)
    minutes = earned.get('minutes', [])
    warm_hours = earned.get('hours', 0)
    observed = {
        'status': 'observed' if warm_hours else 'unknown',
        'usdPerWarmHour': earned.get('usdPerHour') if warm_hours else None,
        'confirmedInferenceUsd': earned.get('usd') if warm_hours else None,
        'warmHours': warm_hours,
        'days': len(local_dates(minutes, zone)),
        'creditedRequests': stats['requests'],
        'adjustments': stats['adjustments'],
        'asOf': max((m['at'] + 60 for m in minutes), default=None),
        'reason': 'Settled solo inference USD per verified warm hour; warm idle included, cold time and base rewards excluded.'
        if warm_hours
        else 'No complete, settled, credit-covered solo warm minutes for this Mac in the selected history.',
    }
    request = {
        'status': 'measured' if stats['outputSamples'] else 'unknown',
        'meanOutputTokens': stats['meanOutput'],
        'outputSamples': stats['outputSamples'],
        'creditedRequests': stats['requests'],
        'outputCoverage': stats['outputSamples'] / stats['requests'] if stats['requests'] else None,
        'asOf': max(
            (r['at'] for r in credits if r['micro_usd'] >= 0 and r['output'] is not None),
            default=None,
        ),
        'reason': 'Measured output tokens per qualifying credited request; missing values and adjustment records are excluded.'
        if stats['outputSamples']
        else 'No reported output-token samples from qualifying solo credited requests.',
    }
    paid_counts = defaultdict(int)
    for row in credits:
        if row['micro_usd'] >= 0:
            paid_counts[int(row['at'] // 60) * 60] += 1
    demand_seconds = income_seconds = demand_sum = income_sum = 0
    demand_rows, income_rows = {}, {}
    demand_bases, income_bases, coverages, date_counts = [], [], [], []
    for part in parts:
        check_time(deadline)
        seconds = part['to'] - part['from']
        target = demand_part(hours, part, now)
        if target:
            demand_seconds += seconds
            demand_sum += target['load'] * seconds
            demand_rows.update({r['key']: r for r in target['rows']})
            demand_bases.append(target['basis'])
            date_counts.append(len({r['date'] for r in target['rows']}))
        value = income_part(earned, network, target, part, now, zone, paid_counts)
        income_rows.update({r['at']: r for r in value['rows']})
        if value['rows']:
            income_bases.append(value['basis'])
        if value['coverage'] is not None:
            coverages.append(value['coverage'])
        if value['supported']:
            income_seconds += seconds
            income_sum += value['rate'] * seconds
    full_demand = math.isclose(demand_seconds, HORIZON, rel_tol=0, abs_tol=1e-6)
    full_income = math.isclose(income_seconds, HORIZON, rel_tol=0, abs_tol=1e-6)
    demand = {
        'scope': 'whole_network_model',
        'status': 'historical_pattern'
        if full_demand
        else 'partial'
        if demand_seconds
        else 'unknown',
        'meanConcurrentRequests': demand_sum / HORIZON if full_demand else None,
        'supportedSeconds': min(HORIZON, demand_seconds),
        'minimumDates': min(date_counts, default=0),
        'qualifiedHistoryHours': sum(r['samples'] * 30 for r in demand_rows.values()) / 3600,
        'basis': combined_basis(demand_bases),
        'asOf': max((r['asOf'] for r in demand_rows.values()), default=None),
        'reason': 'Typical active plus queued concurrent requests for these future clock/day slots, from covered historical hours; not requests per minute.'
        if full_demand
        else 'Every future slot needs at least three covered historical dates and a matching observation within seven days; missing slots remain unknown.',
    }
    matched = list(income_rows.values())
    income = {
        'status': 'conditional' if full_income else 'partial' if income_seconds else 'unknown',
        'usdPerWarmHour': income_sum / HORIZON if full_income else None,
        'supportedSeconds': min(HORIZON, income_seconds),
        'matchedWarmHours': sum(r['seconds'] for r in matched) / 3600,
        'matchedCreditedRequests': sum(paid_counts[r['at']] for r in matched),
        'days': len(local_dates(matched, zone)),
        'blocks': len({int(r['at'] // 1800) for r in matched}),
        'minimumSlotCoverage': min(coverages, default=None),
        'basis': combined_basis(income_bases),
        'asOf': max((r['at'] + 60 for r in matched), default=None),
        'reason': 'Conditional inference USD per verified warm hour if this Mac stays warm and comparable demand and supply recur throughout the next eight elapsed hours. Not a guaranteed or calibrated forecast.'
        if full_income
        else 'Each slot needs four comparable warm hours, three dates, eight substantial periods, 200 credited requests, covered demand and recent nonnegative earnings. Unsupported time is not extrapolated.',
    }
    if not scope_ok:
        for value in (observed, request, demand, income):
            value['reason'] = (
                'Waiting for a matching account and Mac identity. Model controls remain separate.'
            )
    return {
        'id': model,
        'observed': observed,
        'requestSize': request,
        'demandNext8h': demand,
        'incomeNext8h': income,
    }


def report(store, account, device, models, zone, now, deadline=None):
    if not finite(now) or not 0 <= now <= 4102416000:
        raise ValueError('Choose a valid analysis time.')
    start = max(0, now - LOOKBACK)
    settled = max(0, now - 120)
    parts = segments(now, now + HORIZON, zone)
    evidence, credits, loads, network = {}, {}, {}, {}
    scope_ok = bool(account and device)
    if scope_ok:
        with read_view(store) as view:
            if deadline is not None:
                view.h.db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            try:
                evidence = warm_evidence(view, account, device, models, start, now, now, deadline)
                check_time(deadline)
                credits = credit_rows(
                    view, account, device, models, start, settled, evidence, deadline
                )
                loads = calendar_load(view, models, start, now, zone, deadline)
                network = network_minutes(
                    view, [m for m in models if evidence.get(m)], start, settled
                )
            finally:
                if deadline is not None:
                    view.h.db.set_progress_handler(None, 0)
    rows = [
        model_row(
            m,
            evidence,
            credits.get(m, []),
            loads.get(m, []),
            network.get(m, {}),
            parts,
            now,
            zone,
            deadline,
            scope_ok,
        )
        for m in models
    ]
    value = {
        'schemaVersion': 1,
        'at': now,
        'timezone': zone.key,
        'history': {'from': start, 'to': now, 'settledThrough': settled, 'lookbackDays': 28},
        'horizon': {'from': now, 'to': now + HORIZON, 'seconds': HORIZON},
        'scope': 'this_mac_solo_inference',
        'models': rows,
    }
    # Malformed source numbers cannot escape as JSON NaN/Infinity.
    json.dumps(value, allow_nan=False)
    return value


class ModelInsights:
    """Small scope-bound cache; concurrent duplicate work fails independently."""

    def __init__(self, store, identity, timeout=5):
        self.store, self.identity, self.timeout = store, identity, timeout
        self.lock = threading.Lock()
        self.cache = OrderedDict()
        self.inflight = set()
        self.scope = None
        self.generation = 0

    def get(self, raw_query, now=None):
        models, zone = query(raw_query)
        now = time.time() if now is None else now
        scope = tuple(self.identity())
        key = (scope, zone.key, tuple(sorted(models)))
        with self.lock:
            if scope != self.scope:
                self.scope = scope
                self.generation += 1
                self.cache.clear()
            generation = self.generation
            saved = self.cache.get(key)
            if saved and 0 <= time.monotonic() - saved[0] < 60 and 0 <= now - saved[1]['at'] < 60:
                value = copy.deepcopy(saved[1])
                self.cache.move_to_end(key)
            else:
                value = None
                if key in self.inflight or len(self.inflight) >= 2:
                    raise InsightsUnavailable(
                        'Model statistics are already refreshing. Switching remains available.'
                    )
                self.inflight.add(key)
        if value is None:
            try:
                value = report(
                    self.store, *scope, models, zone, now, time.monotonic() + self.timeout
                )
                if tuple(self.identity()) != scope:
                    raise InsightsUnavailable(
                        'The account or Mac changed while statistics loaded. Refresh the comparison.'
                    )
                with self.lock:
                    if self.generation != generation:
                        raise InsightsUnavailable(
                            'The statistics scope changed. Refresh the comparison.'
                        )
                    self.cache[key] = (time.monotonic(), copy.deepcopy(value))
                    self.cache.move_to_end(key)
                    while len(self.cache) > 4:
                        self.cache.popitem(last=False)
            except (sqlite3.Error, OverflowError, OSError, ValueError) as error:
                raise InsightsUnavailable(
                    'Model statistics could not be read. Switching remains available; try again.'
                ) from error
            finally:
                with self.lock:
                    self.inflight.discard(key)
        if tuple(self.identity()) != scope:
            raise InsightsUnavailable(
                'The account or Mac changed while statistics loaded. Refresh the comparison.'
            )
        by_id = {r['id']: r for r in value['models']}
        value['models'] = [by_id[m] for m in models]
        return value
