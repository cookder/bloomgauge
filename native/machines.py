"""Private, read-only multi-Mac summaries. No provider command proxy or cloud service.

Peers must expose Bloomkeeper through owner-authenticated Tailscale Serve. Registration
pins an installation; later identity changes require removal and explicit pairing.
Only allowlisted, device-attributed metrics leave the collector through this API.
"""

import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
import re
import threading
import time
import urllib.request
from urllib.parse import urlsplit

LIMIT = 10
MAX_READS = 6
HOURS = (1, 24, 168)
ID = re.compile(r'[a-f0-9]{32}')


def origin(value):
    if not isinstance(value, str) or len(value) > 220:
        raise ValueError('Use the private HTTPS address from Phone access on the other Mac.')
    try:
        u = urlsplit(value.strip())
        valid = (
            u.scheme == 'https'
            and u.port == 8443
            and not u.username
            and not u.password
            and not u.query
            and not u.fragment
            and u.path in ('', '/')
            and re.fullmatch(r'[a-z0-9-]+\.[a-z0-9-]+\.ts\.net', u.hostname or '')
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('Use the private Tailscale HTTPS address on port 8443, without a path.')
    return 'https://' + u.hostname + ':8443'


def label(value):
    if (
        not isinstance(value, str)
        or not 1 <= len(value.strip()) <= 48
        or any(ord(c) < 32 for c in value)
    ):
        raise ValueError('Give this Mac a name of 1–48 characters.')
    return value.strip()


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def text(value, limit=100):
    return value[:limit] if isinstance(value, str) else ''


def local_summary(collector, hours, now=None):
    now = time.time() if now is None else now
    if type(hours) is not int or hours not in HOURS:
        raise ValueError('Choose 1 hour, 24 hours or 7 days.')
    end = int(now // 30) * 30
    start = end - hours * 3600
    account, device = collector.demand_identity()
    with collector.lock:
        snap = copy.deepcopy(collector.snapshot or {})
    with collector.optimizer.lock:
        mode = collector.optimizer.state.get('mode', 'observe')
        switching = bool(collector.optimizer.state.get('pending'))
    known = None
    covered = 0
    matched = False
    h = collector.history
    # Never sum account balance, base rewards or other providers' credits here.
    with h.lock:
        if account and device:
            matched = bool(
                h.db.execute(
                    'SELECT 1 FROM opt_identity WHERE device=? LIMIT 1', (device,)
                ).fetchone()
            )
            if matched:
                row = h.db.execute(
                    "SELECT SUM(c.micro_usd) FROM opt_credits c WHERE c.account=? AND c.at>=? AND c.at<? AND c.model!='base_reward' AND EXISTS(SELECT 1 FROM opt_identity i WHERE i.device=? AND i.provider=c.provider)",
                    (account, start, end, device),
                ).fetchone()
                known = (row[0] or 0) / 1e6
                previous = start
                for a, b in h.db.execute(
                    'SELECT start,end FROM opt_coverage WHERE account=? AND end>? AND start<? ORDER BY start',
                    (account, start, end),
                ):
                    a, b = max(start, a, previous), min(end - 120, b)
                    covered += max(0, b - a)
                    previous = max(previous, b)
    provider, hw, pulse = (snap.get(k) or {} for k in ('provider', 'hardware', 'pulse'))
    tracking = provider.get('tracking') or {}
    at = number(snap.get('at'))
    fresh = at is not None and -10 <= now - at <= 15
    earn_at = number(pulse.get('updatedAt'))
    money_fresh = (
        fresh
        and earn_at is not None
        and -10 <= now - earn_at <= 120
        and (snap.get('earnings') or {}).get('status') == 'ok'
    )
    rate = (
        number(((pulse.get('windows') or {}).get('300') or {}).get('ratePerHour'))
        if fresh and pulse.get('status') == 'live'
        else None
    )
    return {
        'schema': 1,
        'installation': collector.installation,
        'deviceKey': hashlib.sha256(('bloom-fleet-v1:' + device).encode()).hexdigest()
        if matched
        else None,
        'at': at,
        'hours': hours,
        'from': start,
        'to': end,
        'name': collector.machines.name,
        'chip': text(hw.get('chip')),
        'memoryGB': number(hw.get('memoryTotalGB')),
        'models': [text(m, 160) for m in (pulse.get('models') or [])[:4] if isinstance(m, str)],
        'ready': bool(fresh and tracking.get('counting')),
        'switching': switching,
        'optimizer': mode
        if mode in ('observe', 'demand', 'week', 'optimize', 'combo')
        else 'observe',
        'pro': True,  # Bloom 1.36.45 and older require this key from peers.
        'cpuPercent': number(hw.get('cpuPercent')) if fresh else None,
        'gpuPercent': number(hw.get('gpuPercent')) if fresh else None,
        'cpuTempF': number(hw.get('cpuTemp')) * 1.8 + 32
        if fresh and number(hw.get('cpuTemp')) is not None
        else None,
        'gpuTempF': number(hw.get('gpuTemp')) * 1.8 + 32
        if fresh and number(hw.get('gpuTemp')) is not None
        else None,
        'ratePerHour': rate,
        'knownInferenceUsd': known,
        'earningsFresh': money_fresh,
        'coveredSeconds': covered,
        'coverageSeconds': hours * 3600 - 120,
        'scope': 'Device inference only; excludes account base rewards and balances',
    }


def validate_summary(value, hours):
    if (
        not isinstance(value, dict)
        or value.get('schema') != 1
        or not ID.fullmatch(str(value.get('installation', '')))
        or value.get('hours') != hours
    ):
        raise ValueError('This address did not return a compatible Bloomkeeper Mac summary.')
    keys = {
        'schema',
        'installation',
        'deviceKey',
        'at',
        'hours',
        'from',
        'to',
        'name',
        'chip',
        'memoryGB',
        'models',
        'ready',
        'switching',
        'optimizer',
        'cpuPercent',
        'gpuPercent',
        'cpuTempF',
        'gpuTempF',
        'ratePerHour',
        'knownInferenceUsd',
        'earningsFresh',
        'coveredSeconds',
        'coverageSeconds',
        'scope',
    }
    if not keys.issubset(value):
        raise ValueError('Incomplete Mac summary.')
    if value['deviceKey'] is not None and not re.fullmatch(
        r'[a-f0-9]{64}', str(value['deviceKey'])
    ):
        raise ValueError('Invalid device identity.')
    for key in (
        'at',
        'memoryGB',
        'cpuPercent',
        'gpuPercent',
        'cpuTempF',
        'gpuTempF',
        'ratePerHour',
        'knownInferenceUsd',
    ):
        if value[key] is not None and number(value[key]) is None:
            raise ValueError('Invalid Mac metric.')
    for key in ('from', 'to', 'coveredSeconds', 'coverageSeconds'):
        if number(value[key]) is None:
            raise ValueError('Invalid earnings window.')
    if (
        value['from'] < 0
        or value['to'] - value['from'] != hours * 3600
        or value['coverageSeconds'] != hours * 3600 - 120
        or not 0 <= value['coveredSeconds'] <= value['coverageSeconds']
    ):
        raise ValueError('Invalid earnings coverage.')
    for key in ('ready', 'switching', 'earningsFresh'):
        if type(value[key]) is not bool:
            raise ValueError('Invalid Mac state.')
    if (
        not isinstance(value['models'], list)
        or len(value['models']) > 4
        or any(not isinstance(m, str) or len(m) > 160 for m in value['models'])
    ):
        raise ValueError('Invalid model list.')
    if value['optimizer'] not in ('observe', 'demand', 'week', 'optimize', 'combo'):
        raise ValueError('Invalid optimizer mode.')
    for key, limit in (('name', 48), ('chip', 100), ('scope', 150)):
        if not isinstance(value[key], str) or len(value[key]) > limit:
            raise ValueError('Invalid summary text.')
    return {k: value[k] for k in keys}  # Never relay extra fields/secrets from peers.


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('The Mac address redirected. Check its Phone access address.')


def fetch_summary(address, hours):
    address = origin(address)
    request = urllib.request.Request(
        address + '/api/machines/summary?hours=' + str(hours),
        headers={'Accept': 'application/json'},
    )
    # Ignore environment proxies; no credentials, cookies or incoming headers are forwarded.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=4) as response:
        if response.status != 200 or response.headers.get_content_type() != 'application/json':
            raise ValueError('Could not read this Mac.')
        body = response.read(16385)
        if len(body) > 16384:
            raise ValueError('Mac summary is too large.')
        return validate_summary(json.loads(body), hours)


class Machines:
    def __init__(self, collector, fetcher=fetch_summary, clock=time.time):
        self.c, self.h, self.fetcher, self.clock = collector, collector.history, fetcher, clock
        self.lock = threading.RLock()
        self.operations = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='bloom-macs')
        self.cache, self.pending = {}, {}
        saved = self.h.cache('bloom-machines-v1') or {}
        self.name, self.peers = 'This Mac', []
        try:
            self.name = label(saved.get('name', 'This Mac'))
            for p in saved.get('peers', [])[: LIMIT - 1]:
                item = {
                    'name': label(p['name']),
                    'url': origin(p['url']),
                    'installation': p['installation'],
                }
                if (
                    not ID.fullmatch(item['installation'])
                    or item['installation'] == self.c.installation
                    or any(
                        x['installation'] == item['installation'] or x['url'] == item['url']
                        for x in self.peers
                    )
                ):
                    continue
                self.peers.append(item)
        except (ValueError, TypeError, KeyError, AttributeError):
            self.peers = []

    def persist(self):
        self.h.cache('bloom-machines-v1', {'name': self.name, 'peers': self.peers})

    def action(self, data):
        if type(data) is not dict:
            raise ValueError('Invalid Mac request.')
        # One pairing operation at a time; never hold the summary lock over I/O.
        if not self.operations.acquire(blocking=False):
            raise ValueError('A Mac connection is already being checked. Try again shortly.')
        try:
            action = data.get('action')
            if action == 'rename' and set(data) == {'action', 'name'}:
                with self.lock:
                    self.name = label(data['name'])
                    self.persist()
                with self.c.lock:
                    if self.c.snapshot is not None:
                        self.c.snapshot['deviceName'] = self.name
            elif action == 'remove' and set(data) == {'action', 'installation'}:
                with self.lock:
                    self.peers = [
                        p for p in self.peers if p['installation'] != data['installation']
                    ]
                    self.cache.clear()
                    self.persist()
            elif action == 'add' and set(data) == {'action', 'name', 'url'}:
                name, address = label(data['name']), origin(data['url'])
                with self.lock:
                    if len(self.peers) >= LIMIT - 1:
                        raise ValueError('This beta supports ten Macs including this one.')
                    if any(p['url'] == address for p in self.peers):
                        raise ValueError('This address is already connected.')
                try:
                    report = validate_summary(self.fetcher(address, 24), 24)
                except Exception:
                    raise ValueError(
                        'Could not verify this Mac. Keep it awake, enable Phone access in its Bloomkeeper beta, and connect both Macs to the same Tailscale owner account.'
                    ) from None
                with self.lock:
                    if report['installation'] == self.c.installation or any(
                        p['installation'] == report['installation'] for p in self.peers
                    ):
                        raise ValueError('This Mac is already in your dashboard.')
                    self.peers.append(
                        {'name': name, 'url': address, 'installation': report['installation']}
                    )
                    self.persist()
            else:
                raise ValueError('Unknown Mac connection action.')
            return {'saved': True}
        finally:
            self.operations.release()

    def _read(self, peer, hours):
        try:
            report = validate_summary(self.fetcher(peer['url'], hours), hours)
            if report['installation'] != peer['installation']:
                return None, 'identity-changed'
            return report, None
        except Exception:
            return None, 'unreachable'

    def snapshot(self, hours=24, remote=False):
        if type(hours) is not int or hours not in HOURS:
            raise ValueError('Choose 1 hour, 24 hours or 7 days.')
        now = self.clock()
        local = local_summary(self.c, hours, now)
        rows = [
            {
                'installation': local['installation'],
                'name': self.name,
                'local': True,
                'url': None,
                'status': 'connected',
                'report': local,
            }
        ]
        with self.lock:
            # Never queue unbounded work across repeated range changes or open tabs.
            for key, future in list(self.pending.items()):
                if future.done():
                    report, error = future.result()
                    old = self.cache.get(key, {})
                    self.cache[key] = {
                        'checked': now,
                        'error': error,
                        'report': report if not error else old.get('report'),
                    }
                    del self.pending[key]
            for peer in self.peers:
                key = (peer['installation'], peer['url'], hours)
                cached = self.cache.get(key, {})
                if (
                    now - cached.get('checked', 0) >= 15
                    and key not in self.pending
                    and len(self.pending) < MAX_READS
                ):
                    self.pending[key] = self.pool.submit(self._read, dict(peer), hours)
                report = cached.get('report')
                rows.append(
                    {
                        **peer,
                        'local': False,
                        'status': cached.get('error') or ('connected' if report else 'connecting'),
                        'report': copy.deepcopy(report),
                    }
                )
        seen = set()
        total, included = 0, 0
        for row in rows:
            r = row['report']
            row['included'] = False
            if not r:
                continue
            device = r.get('deviceKey')
            if device and device in seen:
                row['status'] = 'duplicate-device'
                continue
            if device:
                seen.add(device)
            if row['status'] == 'connected' and (r['at'] is None or not -10 <= now - r['at'] <= 30):
                row['status'] = 'stale'
            if (
                row['status'] == 'connected'
                and r['knownInferenceUsd'] is not None
                and r['earningsFresh']
                and abs(r['to'] - local['to']) <= 30
            ):
                total += r['knownInferenceUsd']
                included += 1
                row['included'] = True
        # Combined live pace and a per-model rollup, from connected, fresh Macs only.
        live_rows = [
            r
            for r in rows
            if r['status'] == 'connected' and r['report'] and r['report']['ratePerHour'] is not None
        ]
        models = {}
        for row in rows:
            r = row['report']
            if not r or row['status'] not in ('connected', 'stale'):
                continue
            names = r['models'] or []
            for m in names:
                item = models.setdefault(
                    m, {'model': m, 'macs': [], 'ratePerHour': None, 'demand': None}
                )
                item['macs'].append(row['installation'])
                rate = r['ratePerHour'] if row['status'] == 'connected' else None
                if rate is not None:
                    # A pair shares one pace; split it evenly so the model totals add up to the fleet pace.
                    item['ratePerHour'] = (item['ratePerHour'] or 0) + rate / len(names)
        for item in models.values():
            item['demand'] = self.demand(item['model'], now)
        return {
            'at': now,
            'hours': hours,
            'canManage': not remote,
            'limit': LIMIT,
            'machines': rows,
            'ratePerHour': sum(r['report']['ratePerHour'] for r in live_rows)
            if live_rows
            else None,
            'rateMacs': len(live_rows),
            'models': sorted(models.values(), key=lambda m: (-len(m['macs']), m['model'])),
            'knownInferenceUsd': total if included else None,
            'included': included,
            'complete': included == len(rows)
            and all(r['report']['coveredSeconds'] >= r['report']['coverageSeconds'] for r in rows),
            'managedRemoteAvailable': False,
        }

    def demand(self, model, now):
        """Network demand for a model versus its usual week, read on this dashboard Mac."""
        try:
            d = self.c.pulse_demand.snapshot([model], now)
        except Exception:
            return None
        if not d:
            return None
        return {k: d[k] for k in ('load', 'warm', 'pressure', 'typicalPressure', 'ratio')}

    def close(self):
        self.pool.shutdown(wait=False, cancel_futures=True)
