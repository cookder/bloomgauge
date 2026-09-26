"""Versioned passive report forecasts, independent of the September21 study."""

import copy
import json
import math
import threading
from demand_baselines import read_view
from earnings_forecast import (
    build,
    METHOD_VERSION,
    LOOKBACK_SECONDS,
    REFRESH_SECONDS,
    read_paid_evidence,
)
from model_combinations import members


class ForecastJournal:
    def __init__(self, store, enabled=False):
        self.store, self.enabled = store, enabled
        self.lock = threading.RLock()
        self.next_checkpoint = {}
        self.score_cache = {}
        if enabled:
            with store.h.lock:
                store.h.db.execute("""CREATE TABLE IF NOT EXISTS earnings_forecast_observations(
                    account TEXT,device TEXT,checkpoint INTEGER,version TEXT,origin INTEGER,
                    target_end INTEGER,created_at REAL,data TEXT,
                    PRIMARY KEY(account,device,checkpoint,version))""")
                store.h.db.commit()
        store.earnings_forecast_journal = self

    def record(self, account, device, now, signals):
        if not self.enabled or not account or not device:
            return
        checkpoint = int(now // REFRESH_SECONDS) * REFRESH_SECONDS
        key = (account, device)
        with self.lock:
            if checkpoint <= self.next_checkpoint.get(key, -1):
                return
            with self.store.h.lock:
                exists = self.store.h.db.execute(
                    """SELECT 1 FROM earnings_forecast_observations
                    WHERE account=? AND device=? AND checkpoint=? AND version=?""",
                    (account, device, checkpoint, METHOD_VERSION),
                ).fetchone()
            if exists:
                self.next_checkpoint[key] = checkpoint
                return
            # Forecast starts at the next whole minute. Inputs are frozen before
            # that origin; the first forecast minute has not happened yet.
            origin = int(math.ceil(now / 60)) * 60
            with read_view(self.store) as view:
                evidence = read_paid_evidence(
                    view, account, device, max(0, origin - LOOKBACK_SECONDS), now, now
                )
            evidence = {m: e for m, e in evidence.items() if len(members(m)) == 1}
            models = sorted(set(evidence) | set(signals))[:256]
            packet = {
                'at': now,
                'origin': origin,
                'checkpoint': checkpoint,
                'version': METHOD_VERSION,
                'models': {
                    m: build(evidence.get(m, {}), signals.get(m, {}), origin, now) for m in models
                },
            }
            if not models:
                return
            # Immutable insert. Neither re-polling nor later ledger corrections
            # may rewrite the issued forecast or its input summaries.
            with self.store.h.lock:
                self.store.h.db.execute(
                    'INSERT OR IGNORE INTO earnings_forecast_observations VALUES(?,?,?,?,?,?,?,?)',
                    (
                        account,
                        device,
                        checkpoint,
                        METHOD_VERSION,
                        origin,
                        origin + 3600,
                        now,
                        json.dumps(packet, allow_nan=False, separators=(',', ':')),
                    ),
                )
                self.store.h.db.commit()
            self.next_checkpoint[key] = checkpoint
            while len(self.next_checkpoint) > 4:
                del self.next_checkpoint[next(iter(self.next_checkpoint))]

    def latest(self, account, device, now):
        if not self.enabled:
            return None
        with self.store.h.lock:
            row = self.store.h.db.execute(
                """SELECT data FROM earnings_forecast_observations
                WHERE account=? AND device=? AND version=? AND created_at<=?
                ORDER BY checkpoint DESC LIMIT 1""",
                (account, device, METHOD_VERSION, now),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def evaluation(self, account, device, now):
        if not self.enabled:
            return {'status': 'disabled', 'methodVersion': METHOD_VERSION, 'models': []}
        key = (account, device)
        with self.lock:
            old = self.score_cache.get(key)
            if old and 0 <= now - old['at'] < REFRESH_SECONDS:
                return copy.deepcopy(old)
            with read_view(self.store) as view:
                rows = view.h.db.execute(
                    """SELECT data FROM earnings_forecast_observations
                    WHERE account=? AND device=? AND version=? AND checkpoint>=? AND created_at<=?
                    AND checkpoint%3600=0 ORDER BY origin""",
                    (account, device, METHOD_VERSION, now - LOOKBACK_SECONDS, now),
                ).fetchall()
                packets = [json.loads(r[0]) for r in rows]
                count = view.h.db.execute(
                    """SELECT COUNT(*) FROM earnings_forecast_observations
                    WHERE account=? AND device=? AND version=?""",
                    (account, device, METHOD_VERSION),
                ).fetchone()[0]
                matured = [p for p in packets if p['origin'] + 3600 + 120 <= now]
                start = min((p['origin'] for p in matured), default=now)
                evidence = (
                    read_paid_evidence(view, account, device, start, now, now) if matured else {}
                )
            minutes = {m: {r['at']: r for r in e.get('minutes', [])} for m, e in evidence.items()}
            scores = {}
            last_end = {}
            for packet in packets:
                for model, forecast in packet['models'].items():
                    item = scores.setdefault(
                        model,
                        {
                            'model': model,
                            'windows': 0,
                            'absoluteError': 0.0,
                            'signedError': 0.0,
                            'censoredWindows': 0,
                            'unavailableForecasts': 0,
                            'pendingWindows': 0,
                            'overlappingWindows': 0,
                        },
                    )
                    origin, end = forecast['origin'], forecast['targetEnd']
                    if end + 120 > now:
                        item['pendingWindows'] += 1
                        continue
                    if forecast['usd'] is None:
                        item['unavailableForecasts'] += 1
                        continue
                    if origin < last_end.get(model, 0):
                        item['overlappingWindows'] += 1
                        continue
                    last_end[model] = end
                    future = [minutes.get(model, {}).get(t) for t in range(origin, end, 60)]
                    if len(future) != 60 or any(
                        r is None or abs(r['seconds'] - 60) > 1e-6 for r in future
                    ):
                        item['censoredWindows'] += 1
                        continue
                    actual = sum(r['usd'] for r in future)
                    item['windows'] += 1
                    item['absoluteError'] += abs(forecast['usd'] - actual)
                    item['signedError'] += forecast['usd'] - actual
            result = []
            for item in scores.values():
                n = item['windows']
                item['maeUSDPerHour'] = item.pop('absoluteError') / n if n else None
                item['biasUSDPerHour'] = item.pop('signedError') / n if n else None
                result.append(item)
            output = {
                'status': 'collecting',
                'methodVersion': METHOD_VERSION,
                'at': now,
                'recordedPackets': count,
                'models': sorted(result, key=lambda r: r['model']),
                'basis': 'Immutable issued forecasts; hourly checkpoints with non-overlapping fully warm same-model paid outcomes. Missing or switched windows are censored, not zero.',
                'creditOutcomeStatus': 'Outcomes use the currently settled ledger and may change with later corrections; issued predictions never change.',
                'intervalCalibration': 'not_available',
            }
            self.score_cache[key] = output
            while len(self.score_cache) > 4:
                del self.score_cache[next(iter(self.score_cache))]
            return copy.deepcopy(output)
