"""Observed internal system energy; no wall-power or provider-only attribution."""

import math
import threading
import time
from collections import defaultdict
from demand_baselines import read_view


def number(v):
    return type(v) in (int, float) and math.isfinite(v)


def period(start, end, now):
    if not all(number(v) for v in (start, end, now)) or start < 0 or end <= start or now <= start:
        raise ValueError('Choose an observed time range.')
    return start, min(end, now)


class Energy:
    def __init__(self, store):
        self.store = store
        self.lock = threading.RLock()
        self.previous = None
        self.latest = None
        with store.h.lock:
            store.h.db.executescript("""
              CREATE TABLE IF NOT EXISTS energy_tariffs(id INTEGER PRIMARY KEY,at REAL,rate REAL,label TEXT);
              CREATE TABLE IF NOT EXISTS energy_minutes(account TEXT,device TEXT,at INTEGER,tariff INTEGER,
                power_source TEXT,seconds REAL,kwh REAL,cost REAL,
                PRIMARY KEY(account,device,at,tariff,power_source));
            """)
            if not store.h.db.execute('SELECT 1 FROM energy_tariffs LIMIT 1').fetchone():
                store.h.db.execute(
                    'INSERT INTO energy_tariffs(at,rate,label) VALUES(?,?,?)',
                    (time.time(), None, 'Electricity rate not configured'),
                )
                store.h.db.commit()

    def tariff(self):
        with self.store.h.lock:
            return dict(
                self.store.h.db.execute(
                    'SELECT id,at,rate,label FROM energy_tariffs ORDER BY id DESC LIMIT 1'
                ).fetchone()
            )

    def configure(self, data, now=None):
        if (
            not isinstance(data, dict)
            or set(data) != {'rate', 'label', 'expectedId'}
            or not number(data['rate'])
            or not 0 <= data['rate'] <= 10
            or type(data['expectedId']) is not int
            or not isinstance(data['label'], str)
            or not 1 <= len(data['label'].strip()) <= 120
            or any(ord(c) < 32 for c in data['label'])
        ):
            raise ValueError('Enter a USD/kWh rate from 0 to 10 and a short rate description.')
        with self.lock, self.store.h.lock:
            if self.tariff()['id'] != data['expectedId']:
                raise ValueError('The rate changed. Refresh this page before saving.')
            self.store.h.db.execute(
                'INSERT INTO energy_tariffs(at,rate,label) VALUES(?,?,?)',
                (time.time() if now is None else now, data['rate'], data['label'].strip()),
            )
            self.store.h.db.commit()
            self.previous = None
            return self.tariff()

    def observe(self, account, device, hardware, now):
        at, watts = hardware.get('at'), hardware.get('systemWatts')
        source = hardware.get('powerSource')
        with self.lock:
            if (
                not account
                or not device
                or not number(at)
                or not 0 <= now - at <= 5
                or not number(watts)
                or not 0 < watts <= 1000
                or source not in ('AC Power', 'Battery Power', 'UPS Power')
            ):
                self.previous = None
                self.latest = None
                return
            tariff = self.tariff()
            point = (account, device, at, watts, source, tariff['id'])
            old = self.previous
            if old and at == old[2] and point[:2] == old[:2]:
                return
            self.previous = point
            self.latest = {
                'at': at,
                'watts': watts,
                'powerSource': source,
                'account': account,
                'device': device,
            }
            if not old or old[:2] != point[:2] or old[4:] != point[4:] or not 0 < at - old[2] <= 5:
                return
            cursor = old[2]
            with self.store.h.lock:
                while cursor < at:
                    minute = math.floor(cursor / 60) * 60
                    end = min(at, minute + 60)

                    def w(t):
                        return old[3] + (watts - old[3]) * (t - old[2]) / (at - old[2])

                    kwh = (w(cursor) + w(end)) / 2 * (end - cursor) / 3600000
                    cost = (
                        kwh * tariff['rate']
                        if source == 'AC Power' and number(tariff['rate'])
                        else None
                    )
                    self.store.h.db.execute(
                        """INSERT INTO energy_minutes VALUES(?,?,?,?,?,?,?,?)
                        ON CONFLICT(account,device,at,tariff,power_source) DO UPDATE SET
                        seconds=seconds+excluded.seconds,kwh=kwh+excluded.kwh,cost=cost+excluded.cost""",
                        (account, device, minute, tariff['id'], source, end - cursor, kwh, cost),
                    )
                    cursor = end
                self.store.h.db.commit()

    def report(self, account, device, start, end, now):
        start, end = period(start, end, now)
        with read_view(self.store) as view:
            db = view.h.db
            first, last = db.execute(
                'SELECT MIN(at),MAX(at+60) FROM energy_minutes WHERE account=? AND device=?',
                (account, device),
            ).fetchone()
            low = max(start, first or start)
            # Only whole minute bins inside the requested range. Never prorate a gap.
            rows = db.execute(
                """SELECT at,SUM(seconds) seconds,SUM(kwh) kwh,SUM(cost) cost,
                SUM(CASE WHEN power_source='AC Power' THEN seconds ELSE 0 END) ac_seconds
                FROM energy_minutes WHERE account=? AND device=? AND at>=? AND at+60<=?
                GROUP BY at ORDER BY at""",
                (account, device, low, end),
            ).fetchall()
            covered = {
                r[0]
                for r in db.execute(
                    """SELECT DISTINCT e.at FROM energy_minutes e
                WHERE e.account=? AND e.device=? AND e.at>=? AND e.at+60<=?
                AND EXISTS(SELECT 1 FROM opt_coverage c WHERE c.account=e.account AND c.start<=e.at AND c.end>=e.at+60)""",
                    (account, device, low, min(end, now - 120)),
                )
            }
            credits = defaultdict(lambda: {'base': 0, 'inference': 0})
            for r in db.execute(
                """SELECT CAST(c.at/60 AS INT)*60 at,c.model,SUM(c.micro_usd) usd FROM opt_credits c
                WHERE c.account=? AND c.at>=? AND c.at<? AND (c.model='base_reward' OR
                EXISTS(SELECT 1 FROM opt_identity i WHERE i.device=? AND i.provider=c.provider)) GROUP BY 1,c.model""",
                (account, low, min(end, now - 120), device),
            ):
                credits[r['at']]['base' if r['model'] == 'base_reward' else 'inference'] += (
                    r['usd'] / 1e6
                )
            tariff = dict(
                db.execute(
                    'SELECT id,at,rate,label FROM energy_tariffs ORDER BY id DESC LIMIT 1'
                ).fetchone()
            )
            tariffs = [
                dict(r)
                for r in db.execute(
                    """SELECT DISTINCT t.id,t.at,t.rate,t.label FROM energy_tariffs t
                JOIN energy_minutes e ON e.tariff=t.id WHERE e.account=? AND e.device=? AND e.at>=? AND e.at+60<=? ORDER BY t.id""",
                    (account, device, low, end),
                )
            ]
        step = max(60, math.ceil(max(0, end - low) / 400 / 60) * 60)
        groups = defaultdict(list)
        totals = {'seconds': 0, 'acSeconds': 0, 'kwh': 0, 'costUsd': None}
        comparison = {
            'seconds': 0,
            'inferenceUsd': 0,
            'accountBaseUsd': 0,
            'costUsd': 0,
            'afterCostUsd': None,
        }
        matched = {}
        for r in rows:
            groups[int((r['at'] - low) // step)].append(r)
            totals['seconds'] += r['seconds']
            totals['acSeconds'] += r['ac_seconds']
            totals['kwh'] += r['kwh']
            if r['cost'] is not None:
                totals['costUsd'] = (totals['costUsd'] or 0) + r['cost']
            if (
                r['cost'] is not None
                and 59.999999 <= r['seconds'] <= 60.000001
                and r['ac_seconds'] >= 59.999999
                and r['at'] in covered
            ):
                c = credits[r['at']]
                matched[r['at']] = c
                comparison['seconds'] += 60
                comparison['inferenceUsd'] += c['inference']
                comparison['accountBaseUsd'] += c['base']
                comparison['costUsd'] += r['cost'] or 0
        if comparison['seconds']:
            comparison['afterCostUsd'] = (
                comparison['inferenceUsd'] + comparison['accountBaseUsd'] - comparison['costUsd']
            )
        samples = []
        for i in range(int(max(0, end - low) // step) + 1) if rows else []:
            rs = groups.get(i, [])
            seconds = sum(r['seconds'] for r in rs)
            cs = [r for r in rs if r['at'] in matched]
            samples.append(
                {
                    'at': low + i * step,
                    'watts': sum(r['kwh'] for r in rs) * 3600000 / seconds
                    if seconds >= 0.8 * step
                    else None,
                    'inferenceRate': sum(matched[r['at']]['inference'] for r in cs) * 60 / len(cs)
                    if len(cs) * 60 >= 0.8 * step
                    else None,
                    'costRate': sum(r['cost'] or 0 for r in cs) * 60 / len(cs)
                    if len(cs) * 60 >= 0.8 * step
                    else None,
                }
            )
        with self.lock:
            live = (
                dict(self.latest)
                if self.latest
                and self.latest['account'] == account
                and self.latest['device'] == device
                and 0 <= now - self.latest['at'] <= 5
                else None
            )
        if live:
            live = {k: live[k] for k in ('at', 'watts', 'powerSource')}
        return {
            'at': now,
            'from': low,
            'to': end,
            'coverageStart': first,
            'coverageEnd': min(last, now) if last else None,
            'tariff': tariff,
            'tariffs': tariffs,
            'latest': live,
            'samples': samples,
            'bucketSeconds': step,
            'totals': totals,
            'comparison': comparison,
            'source': 'Apple SMC PSTR · uncalibrated internal system power',
            'method': 'All Mac activity, not just Darkbloom. Trapezoidal integration of readings no more than five seconds apart. Costs apply only on AC; battery/UPS and missing readings have no cost estimate. Whole minutes inside this range only. No wall-power efficiency assumption.',
        }
