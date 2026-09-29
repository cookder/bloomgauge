"""Bounded read-only network observations and separately scoped Mac credits.

Concurrency never apportions completed work. Current prices never invent pay.
"""

import copy
import json
import math
import sqlite3
import threading
import time
from collections import OrderedDict, defaultdict
from types import SimpleNamespace
from urllib.parse import parse_qs

from demand_baselines import read_view

METRICS = {
    'activity': ('network', 'concurrent_requests', 'concurrent_requests'),
    'requests': ('network', 'requests_per_minute', 'requests'),
    'tokens': ('network', 'tokens_per_second', 'tokens'),
    'earnings': ('this_mac', 'usd_per_hour', 'usd'),
}
MAX_POINTS = 600
MAX_MODELS = 128
NAMED_MODELS = 8
MIN_COVERAGE = 0.8
NETWORK_MONEY = {
    'available': False,
    'reason': 'The official public feeds provide no model-level paid-dollar history. Network capacity and current prices are not paid income.',
}


class ContributionsUnavailable(Exception):
    pass


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def check(deadline):
    if deadline is not None and time.monotonic() >= deadline:
        raise ContributionsUnavailable(
            'The report took too long. Model controls remain available; try a shorter range.'
        )


def query(raw, now):
    if not isinstance(raw, str) or len(raw) > 2000:
        raise ValueError('Choose a bounded network report query.')
    values = parse_qs(raw, keep_blank_values=True, max_num_fields=4)
    if set(values) != {'metric', 'from', 'to'} or any(len(v) != 1 for v in values.values()):
        raise ValueError('Provide one metric, from and to.')
    metric = values['metric'][0]
    if metric not in METRICS:
        raise ValueError('Choose activity, requests, tokens or earnings.')
    try:
        start, end = float(values['from'][0]), float(values['to'][0])
    except (TypeError, ValueError):
        raise ValueError('Choose a valid time range.') from None
    if (
        not all(finite(v) for v in (start, end, now))
        or start < 0
        or start >= end
        or end > 4102444800
        or end - start > 31 * 86400
        or start >= now
    ):
        raise ValueError('Choose an elapsed range of at most 31 days.')
    return metric, start, min(end, now)


def layout(start, end, grain):
    step = max(grain, math.ceil((end - start) / (MAX_POINTS - 1) / grain) * grain)
    low = math.floor(start / step) * step
    count = math.ceil((end - low) / step)
    points = [
        {
            'from': max(start, low + i * step),
            'to': min(end, low + (i + 1) * step),
            'values': [],
            'coverageFraction': 0,
        }
        for i in range(count)
    ]
    return step, low, points


def model_groups(ranks):
    ordered = sorted(ranks, key=lambda model: (-ranks[model], model is None, model or ''))
    if len(ordered) > MAX_MODELS:
        raise ContributionsUnavailable(
            'Too many model identities in this report. Choose a shorter range.'
        )
    known = [m for m in ordered if m is not None]
    groups = [[m] for m in known[:NAMED_MODELS]]
    series = [{'id': 'model:' + m, 'name': m} for m in known[:NAMED_MODELS]]
    if len(known) > NAMED_MODELS:
        groups.append(known[NAMED_MODELS:])
        series.append({'id': 'other', 'name': 'Other models'})
    if None in ranks:
        groups.append([None])
        series.append({'id': 'unattributed', 'name': 'Unattributed'})
    return series, groups


def coverage_value(observed, start, end):
    seconds = max(0, end - start)
    return {
        'observedSeconds': min(seconds, observed),
        'requestedSeconds': seconds,
        'fraction': min(1, observed / seconds) if seconds else 0,
    }


def activity(db, start, end, deadline):
    step, low, points = layout(start, end, 30)
    bins = [{'frames': 0, 'sum': defaultdict(float), 'count': defaultdict(int)} for _ in points]
    ranks = defaultdict(float)
    pending_at, pending_frame, pending_slot, slot_frame = None, {}, None, None

    def consume(slot, frame):
        if slot is None or slot * 30 < start or (slot + 1) * 30 > end:
            return
        index = int((slot * 30 - low) // step)
        item = bins[index]
        item['frames'] += 1
        for model, value in frame.items():
            ranks.setdefault(model, 0)
            if len(ranks) > MAX_MODELS:
                raise ContributionsUnavailable('Too many model identities. Choose a shorter range.')
            if value is not None:
                ranks[model] += value
                item['sum'][model] += value
                item['count'][model] += 1

    def finish_frame(at, frame):
        nonlocal pending_slot, slot_frame
        if at is None:
            return
        slot = math.floor(at / 30)
        if pending_slot is not None and slot != pending_slot:
            consume(pending_slot, slot_frame)
        pending_slot, slot_frame = slot, frame

    for index, row in enumerate(
        db.execute(
            """SELECT at,model,active,queued FROM opt_network
            WHERE at>=? AND at<? ORDER BY at,model""",
            (start, end),
        )
    ):
        if index % 1024 == 0:
            check(deadline)
        if (
            not finite(row['at'])
            or not isinstance(row['model'], str)
            or not 1 <= len(row['model']) <= 512
        ):
            continue
        if row['at'] != pending_at:
            finish_frame(pending_at, pending_frame)
            pending_at, pending_frame = row['at'], {}
        values = (row['active'], row['queued'])
        value = sum(values) if all(finite(v) and v >= 0 for v in values) else None
        pending_frame[row['model']] = value if finite(value) else None
    finish_frame(pending_at, pending_frame)
    consume(pending_slot, slot_frame)
    series, groups = model_groups(ranks)
    weights = []
    for point, item in zip(points, bins):
        observed = item['frames'] * 30
        point['coverageFraction'] = min(1, observed / (point['to'] - point['from']))
        qualified = observed > 0 and point['coverageFraction'] + 1e-9 >= MIN_COVERAGE
        point['values'] = [
            sum(item['sum'][m] for m in group) / item['frames']
            if qualified and all(item['count'][m] == item['frames'] for m in group)
            else None
            for group in groups
        ]
        weights.append(observed if qualified else 0)
    weight = sum(weights)
    values = [
        sum(point['values'][i] * seconds for point, seconds in zip(points, weights) if seconds)
        / weight
        if weight
        and all(
            point['values'][i] is not None for point, seconds in zip(points, weights) if seconds
        )
        else None
        for i in range(len(series))
    ]
    total = sum(values) if values and all(v is not None for v in values) else None
    observed = sum(b['frames'] * 30 for b in bins)
    notes = [
        'Average active plus queued requests from aligned source snapshots; not completed requests, token throughput or paid income.',
        'Model rosters change. Missing models stay unknown; every known model in an interval uses the same source timestamps and denominator.',
        'Coverage measures distinct 30-second source slots; intervals below 80% remain gaps.',
    ]
    return step, series, points, total, values, observed, notes


def traffic(db, metric, start, end, deadline):
    rows = db.execute(
        'SELECT * FROM network WHERE at>=? AND at<? ORDER BY at', (start, end)
    ).fetchall()
    valid = []
    last_end = None
    for index, row in enumerate(rows):
        if index % 1024 == 0:
            check(deadline)
        fields = (row['at'], row['seconds'], row['requests'], row['completion_tokens'])
        if (
            not all(finite(v) and v >= 0 for v in fields)
            or not 0 < row['seconds'] <= 86400
            or row['at'] + row['seconds'] > end
            or last_end is not None
            and row['at'] < last_end
        ):
            continue
        valid.append(row)
        last_end = row['at'] + row['seconds']
    grain = max([60, *[r['seconds'] for r in valid]])
    step, low, points = layout(start, end, grain)
    counts, durations = [0.0] * len(points), [0.0] * len(points)
    for row in valid:
        i = int((row['at'] - low) // step)
        if row['at'] < points[i]['from'] or row['at'] + row['seconds'] > points[i]['to']:
            continue
        counts[i] += row['requests' if metric == 'requests' else 'completion_tokens']
        durations[i] += row['seconds']
    for point, count, seconds in zip(points, counts, durations):
        point['coverageFraction'] = min(1, seconds / (point['to'] - point['from']))
        point['values'] = [
            count * (60 if metric == 'requests' else 1) / seconds
            if seconds and point['coverageFraction'] + 1e-9 >= MIN_COVERAGE
            else None
        ]
    observed = sum(durations)
    total = sum(counts) if observed else None
    notes = [
        'Official whole-network usage. The public series does not attribute completed work to models.',
        'Tokens means completion/output tokens. Rates use recorded source duration; period totals are recorded counts.',
        'Coarse, boundary-crossing or missing source buckets are never spread into finer data. Intervals below 80% coverage remain gaps.',
    ]
    return (
        step,
        [{'id': 'unattributed', 'name': 'Unattributed network traffic'}],
        points,
        total,
        [total],
        observed,
        notes,
    )


def poll_coverage(db, account, start, end):
    intervals = []
    for row in db.execute(
        """SELECT start,end FROM opt_coverage
            WHERE account=? AND end>? AND start<? ORDER BY start""",
        (account, start, end),
    ):
        if not all(finite(row[k]) for k in ('start', 'end')):
            continue
        a, b = max(start, row['start']), min(end, row['end'])
        if b <= a:
            continue
        if intervals and a <= intervals[-1][1]:
            intervals[-1][1] = max(intervals[-1][1], b)
        else:
            intervals.append([a, b])
    return intervals


def earnings(db, account, device, start, end, deadline):
    step, low, points = layout(start, end, 60)
    bins = [defaultdict(int) for _ in points]
    totals, ranks = defaultdict(int), defaultdict(int)
    invalid_bins = [set() for _ in points]
    invalid_totals = set()
    count = 0
    # EXISTS retains the account/time index scan. A provider-first JOIN can
    # repeat the full credit range for each historical provider identity.
    rows = db.execute(
        """SELECT c.at,c.model,c.micro_usd FROM opt_credits c
        WHERE c.account=? AND c.at>=? AND c.at<? AND (c.model IS NULL OR c.model!='base_reward')
          AND EXISTS(SELECT 1 FROM opt_identity i WHERE i.device=? AND i.provider=c.provider)
        ORDER BY c.at""",
        (account, start, end, device),
    )
    for index, row in enumerate(rows):
        if index % 1024 == 0:
            check(deadline)
        if not finite(row['at']):
            continue
        model = (
            row['model']
            if isinstance(row['model'], str)
            and row['model'] not in ('', 'Unknown')
            and len(row['model']) <= 512
            else None
        )
        i = int((row['at'] - low) // step)
        if not finite(row['micro_usd']):
            ranks.setdefault(model, 0)
            invalid_bins[i].add(model)
            invalid_totals.add(model)
            continue
        bins[i][model] += row['micro_usd']
        totals[model] += row['micro_usd']
        ranks[model] += abs(row['micro_usd'])
        count += 1
        if len(ranks) > MAX_MODELS:
            raise ContributionsUnavailable(
                'Too many credited model identities. Choose a shorter range.'
            )
    # Base rewards: account-wide, as the hourly Monitor and daily totals count them
    # (not tied to a provider identity). Kept out of `series` and `summary` so the
    # model report stays inference-only; the Pulse's 5-minute bars add them.
    base_bins = [0] * len(points)
    base_invalid = [False] * len(points)
    base_total, base_count, base_bad = 0, 0, False
    rows = db.execute(
        """SELECT at,micro_usd FROM opt_credits
        WHERE account=? AND at>=? AND at<? AND model='base_reward'
        ORDER BY at""",
        (account, start, end),
    )
    for index, row in enumerate(rows):
        if index % 1024 == 0:
            check(deadline)
        if not finite(row['at']):
            continue
        i = int((row['at'] - low) // step)
        if not finite(row['micro_usd']):
            base_invalid[i] = base_bad = True
            continue
        base_bins[i] += row['micro_usd']
        base_total += row['micro_usd']
        base_count += 1
    intervals = poll_coverage(db, account, start, end)
    observed = sum(b - a for a, b in intervals)
    if not ranks:
        ranks[None] = 0
    series, groups = model_groups(ranks)
    interval_index = 0
    base_values = []
    for point, values, invalid, base, base_unknown in zip(
        points, bins, invalid_bins, base_bins, base_invalid
    ):
        while interval_index < len(intervals) and intervals[interval_index][1] <= point['from']:
            interval_index += 1
        seconds = 0
        i = interval_index
        while i < len(intervals) and intervals[i][0] < point['to']:
            a, b = intervals[i]
            seconds += max(0, min(b, point['to']) - max(a, point['from']))
            i += 1
        elapsed = point['to'] - point['from']
        point['coverageFraction'] = min(1, seconds / elapsed)
        point['values'] = [
            sum(values[m] for m in group) / 1e6 * 3600 / elapsed
            if seconds
            and point['coverageFraction'] + 1e-9 >= MIN_COVERAGE
            and not invalid.intersection(group)
            else None
            for group in groups
        ]
        base_values.append(
            base / 1e6 * 3600 / elapsed
            if seconds and point['coverageFraction'] + 1e-9 >= MIN_COVERAGE and not base_unknown
            else None
        )
    known = bool(count or observed or invalid_totals)
    values = [
        sum(totals[m] for m in group) / 1e6
        if known and not invalid_totals.intersection(group)
        else None
        for group in groups
    ]
    total = sum(totals.values()) / 1e6 if known and not invalid_totals else None
    notes = [
        'Confirmed inference credits attributed to this Mac through recorded provider identities, not whole-network earnings or account-wide totals.',
        'Base rewards are excluded; signed corrections are retained. Period totals include recorded credits even where poll coverage is incomplete.',
        'Chart rates divide recorded dollars by elapsed interval hours, not warm hours. Intervals below 80% poll coverage remain gaps.',
    ]
    base_rewards = {
        'attribution': 'account',
        'values': base_values,
        'total': base_total / 1e6 if (base_count or observed) and not base_bad else None,
    }
    return step, series, points, total, values, observed, notes, base_rewards


def unavailable(metric, start, end, now, reason):
    scope, unit, summary_unit = METRICS[metric]
    step, low, points = layout(start, end, 60)
    return {
        'schemaVersion': 1,
        'metric': metric,
        'scope': scope,
        'status': 'unavailable',
        'at': now,
        'from': start,
        'to': end,
        'bucketSeconds': step,
        'unit': unit,
        'attribution': 'models',
        'series': [],
        'points': points,
        'summary': {'total': None, 'values': [], 'unit': summary_unit},
        'coverage': coverage_value(0, start, end),
        'notes': [reason],
        'networkMoney': dict(NETWORK_MONEY),
    }


def report(history, metric, start, end, now, account='', device='', deadline=None):
    check(deadline)
    scope, unit, summary_unit = METRICS[metric]
    if metric == 'earnings' and not (account and device):
        return unavailable(
            metric, start, end, now, 'Waiting for a matching account and Mac identity.'
        )
    with read_view(SimpleNamespace(h=history)) as view:
        db = view.h.db
        if deadline is not None:
            db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        try:
            if metric == 'activity':
                data = activity(db, start, end, deadline)
            elif metric == 'earnings':
                if not db.execute(
                    'SELECT 1 FROM opt_identity WHERE device=? LIMIT 1', (device,)
                ).fetchone():
                    return unavailable(
                        metric,
                        start,
                        end,
                        now,
                        'No recorded provider identity for this Mac in saved history.',
                    )
                data = earnings(db, account, device, start, end, deadline)
            else:
                data = traffic(db, metric, start, end, deadline)
        finally:
            if deadline is not None:
                db.set_progress_handler(None, 0)
    step, series, points, total, values, observed, notes, *extra = data
    # Malformed source arithmetic must remain unknown, never JSON NaN/Infinity.
    for point in points:
        point['values'] = [v if finite(v) else None for v in point['values']]
    values = [v if finite(v) else None for v in values]
    total = total if finite(total) else None
    covered = coverage_value(observed, start, end)
    gaps = any(v is None for p in points for v in p['values'])
    status = (
        'empty'
        if observed == 0 and total is None
        else 'partial'
        if gaps or covered['fraction'] < 0.999999
        else 'ok'
    )
    result = {
        'schemaVersion': 1,
        'metric': metric,
        'scope': scope,
        'status': status,
        'at': now,
        'from': start,
        'to': end,
        'bucketSeconds': step,
        'unit': unit,
        'attribution': 'unavailable' if metric in ('requests', 'tokens') else 'models',
        'series': series,
        'points': points,
        'summary': {'total': total, 'values': values, 'unit': summary_unit},
        'coverage': covered,
        'notes': notes,
        'networkMoney': dict(NETWORK_MONEY),
    }
    if extra:
        # Earnings only: base-reward USD per elapsed hour aligned with `points`.
        # An additive field; readers of `series`/`summary` are unaffected.
        base = extra[0]
        result['baseRewards'] = {
            'attribution': base['attribution'],
            'values': [v if finite(v) else None for v in base['values']],
            'total': base['total'] if finite(base['total']) else None,
        }
    check(deadline)
    json.dumps(result, allow_nan=False)
    return result


class NetworkContributions:
    def __init__(self, history, identity, timeout=5):
        self.history, self.identity, self.timeout = history, identity, timeout
        self.lock = threading.Lock()
        self.cache = OrderedDict()
        self.inflight = set()
        self.scope = None
        self.generation = 0

    def get(self, raw_query, now=None):
        now = time.time() if now is None else now
        metric, start, end = query(raw_query, now)
        scope = tuple(self.identity()) if metric == 'earnings' else ('', '')
        key = (metric, start, end, scope)
        with self.lock:
            if metric == 'earnings' and self.scope != scope:
                self.scope = scope
                self.generation += 1
                self.cache.clear()
            generation = self.generation
            saved = self.cache.get(key)
            if saved and 0 <= time.monotonic() - saved[0] < 30 and 0 <= now - saved[1]['at'] < 30:
                value = copy.deepcopy(saved[1])
                self.cache.move_to_end(key)
            else:
                value = None
                if key in self.inflight or len(self.inflight) >= 2:
                    raise ContributionsUnavailable(
                        'Reports are already refreshing. Model controls remain available.'
                    )
                self.inflight.add(key)
        if value is None:
            try:
                value = report(
                    self.history,
                    metric,
                    start,
                    end,
                    now,
                    *scope,
                    deadline=time.monotonic() + self.timeout,
                )
                if metric == 'earnings' and tuple(self.identity()) != scope:
                    raise ContributionsUnavailable(
                        'The account or Mac changed while the report loaded. Refresh it.'
                    )
                with self.lock:
                    if metric == 'earnings' and generation != self.generation:
                        raise ContributionsUnavailable(
                            'The earnings scope changed while the report loaded. Refresh it.'
                        )
                    self.cache[key] = (time.monotonic(), copy.deepcopy(value))
                    self.cache.move_to_end(key)
                    while len(self.cache) > 8:
                        self.cache.popitem(last=False)
            except (sqlite3.Error, OSError, OverflowError) as error:
                raise ContributionsUnavailable(
                    'The report could not be read. Model controls remain available.'
                ) from error
            finally:
                with self.lock:
                    self.inflight.discard(key)
        if metric == 'earnings' and tuple(self.identity()) != scope:
            raise ContributionsUnavailable(
                'The account or Mac changed. Refresh the earnings report.'
            )
        return value
