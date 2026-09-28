"""Network demand for the serving model, shown as an overlay on the Pulse meter.

`pressure` is requests (active + queued) per warm provider, the same measure the
optimizer uses. The overlay compares the latest capacity reading with this
model's usual pressure for this time of day (`demand_alerts.usual_levels`, the
one definition the app uses), so it lines up with the earnings needle (pace vs
its own reference). High demand with a low needle points at routing; both low
means the network is quiet. Until the time-of-day level exists there is no ratio.

This runs inside the collector's once-a-second locked path, so it only reads a
cached capacity snapshot, and it recomputes the usual level at most every 10
minutes (and on each new local hour) through a separate read-only connection.
"""

import math
import threading
from datetime import datetime
from demand_alerts import usual_levels
from demand_baselines import read_view

TYPICAL_REFRESH_SECONDS = 600
CAPACITY_REFRESH_SECONDS = 15
FRESH_SECONDS = 120


def finite(v):
    return type(v) in (int, float) and math.isfinite(v)


class PulseDemand:
    def __init__(self, store, network):
        self.store, self.network = store, network
        self.lock = threading.Lock()
        self.capacity, self.capacity_read = None, -math.inf
        self.typical = {}  # model -> (computed_at, {'pressure', 'load', 'samples'} | None)
        self.pending = set()

    def current(self, now):
        if now - self.capacity_read >= CAPACITY_REFRESH_SECONDS:
            self.capacity = self.network.snapshot('capacity')
            self.capacity_read = now
        return self.capacity or {}

    def typical_for(self, model, now):
        cached = self.typical.get(model)
        if (
            cached is None
            or not 0 <= now - cached[0] < TYPICAL_REFRESH_SECONDS
            or datetime.fromtimestamp(now).hour != datetime.fromtimestamp(cached[0]).hour
        ) and model not in self.pending:
            self.pending.add(model)
            threading.Thread(target=self.refresh, args=(model, now), daemon=True).start()
        return cached[1] if cached else None

    def refresh(self, model, now):
        try:
            with read_view(self.store) as view:
                usual = usual_levels(view.h.db, [model], now)[model]
            value = (
                {
                    'pressure': usual['baselinePressure'],
                    'load': usual['baselineLoad'],
                    'samples': round(usual['baselineHours'] * 120),
                }
                if usual['baselineScope'] == 'daytype_hour' and finite(usual['baselinePressure'])
                else None
            )
            with self.lock:
                self.typical[model] = (now, value)
                while len(self.typical) > 32:
                    del self.typical[next(iter(self.typical))]
        except Exception:
            with self.lock:
                self.typical[model] = (now, None)
        finally:
            with self.lock:
                self.pending.discard(model)

    def snapshot(self, models, now):
        """None unless exactly one model is serving and fresh network data exists."""
        if len(models) != 1 or not isinstance(models[0], str):
            return None
        model = models[0]
        capacity = self.current(now)
        if (
            capacity.get('status') != 'ok'
            or not finite(capacity.get('updatedAt'))
            or not 0 <= now - capacity['updatedAt'] <= FRESH_SECONDS
        ):
            return None
        row = next(
            (
                m
                for m in (capacity.get('data') or {}).get('models', [])
                if isinstance(m, dict) and m.get('id') == model
            ),
            None,
        )
        if not row:
            return None
        active, queued, warm = (
            row.get(k) for k in ('active_requests', 'queued_requests', 'warm_providers')
        )
        if not all(finite(v) and v >= 0 for v in (active, queued, warm)):
            return None
        load = active + queued
        pressure = load / max(1, warm)
        with self.lock:
            typical = self.typical_for(model, now)
        ratio = pressure / typical['pressure'] if typical and typical['pressure'] > 0 else None
        return {
            'model': model,
            'at': capacity['updatedAt'],
            'load': load,
            'warm': warm,
            'pressure': pressure,
            'typicalPressure': typical['pressure'] if typical else None,
            'typicalSamples': typical['samples'] if typical else 0,
            'ratio': ratio,
        }
