"""Shadow journal for the pressure-curve estimator (optimizer redesign Phase 2).

Every 5 minutes, freeze a curve prediction for every model next to the matched
estimator's rate for the same model and moment. Half-hour checkpoints are later
scored against the steady pay of whichever models actually ran in the next 30
minutes, so both estimators are judged on exactly the same windows. Nothing
here ranks, triggers or blocks a switch.
"""

import copy
import json
import math
import threading
from datetime import datetime
from demand_baselines import read_view, network_minutes, conditional
from demand_optimizer import estimate
from earnings_forecast import read_paid_evidence
from model_combinations import members
import demand_curves as curves
import shared_priors

REFRESH_SECONDS = 300
HORIZON_SECONDS = 1800
SETTLEMENT_SECONDS = 120
MIN_SCORED_MINUTES = 20
SCORE_SECONDS = 30 * 86400
KEEP_SECONDS = 45 * 86400
# The switch gate in docs/OPTIMIZER_REDESIGN.md: a week of live scoring.
READY_DAYS = 7
READY_WINDOWS = 100


def rounded(v):
    return round(v, 5) if type(v) is float and math.isfinite(v) else v


class CurveJournal:
    def __init__(self, store, enabled=False):
        self.store, self.enabled = store, enabled
        # 'chip family|memory band' of this Mac, set by the collector; picks matching starting curves.
        self.hardware = lambda: None
        self.lock = threading.RLock()
        self.next_checkpoint = {}
        self.score_cache = {}
        self.pruned_at = 0
        if enabled:
            with store.h.lock:
                store.h.db.execute("""CREATE TABLE IF NOT EXISTS demand_curve_observations(
                    account TEXT,device TEXT,checkpoint INTEGER,version TEXT,origin INTEGER,
                    created_at REAL,data TEXT,PRIMARY KEY(account,device,checkpoint,version))""")
                store.h.db.commit()

    def record(self, account, device, now, signals, current, current_since=None):
        if not self.enabled or not account or not device:
            return
        checkpoint = int(now // REFRESH_SECONDS) * REFRESH_SECONDS
        key = (account, device)
        with self.lock:
            if checkpoint <= self.next_checkpoint.get(key, -1):
                return
            with self.store.h.lock:
                exists = self.store.h.db.execute(
                    """SELECT 1 FROM demand_curve_observations
                    WHERE account=? AND device=? AND checkpoint=? AND version=?""",
                    (account, device, checkpoint, curves.METHOD_VERSION),
                ).fetchone()
            if not exists:
                self.insert(account, device, now, checkpoint, signals, current, current_since)
            self.next_checkpoint[key] = checkpoint
            while len(self.next_checkpoint) > 4:
                del self.next_checkpoint[next(iter(self.next_checkpoint))]
            if now - self.pruned_at >= 3600:
                self.prune(now)

    def insert(self, account, device, now, checkpoint, signals, current, current_since):
        origin = int(math.ceil(now / 60)) * 60
        with read_view(self.store) as view:
            # Same minute, coverage and identity rules as OptimizerStore.evidence,
            # solo models only, several times faster over 30 days.
            evidence = read_paid_evidence(
                view, account, device, now - curves.LOOKBACK_SECONDS, now, now
            )
            models = sorted(m for m in set(evidence) | set(signals) if len(members(m)) == 1)[:256]
            network = network_minutes(
                view, models, now - curves.LOOKBACK_SECONDS, now - SETTLEMENT_SECONDS
            )
        predictions = curves.build(
            evidence, network, signals, current, now, shared_priors.load(hardware=self.hardware())
        )
        if not predictions:
            return
        for model, row in predictions.items():
            # The matched estimator exactly as DemandOptimizer.evaluate calls it.
            earned, joined, signal = (
                evidence.get(model, {}),
                network.get(model, {}),
                signals.get(model, {}),
            )
            matched = estimate(
                earned,
                joined,
                signal,
                now,
                current_since if model == current else None,
                conditional(earned, joined, signal, now),
            )
            row['matched'] = (
                {
                    'usdPerHour': matched['rate'],
                    'lower': matched['lower'],
                    'upper': matched['upper'],
                    'usable': matched['forecastUsable'],
                    'scope': matched['scope'],
                }
                if matched
                else None
            )
        packet = {
            'at': now,
            'checkpoint': checkpoint,
            'origin': origin,
            'targetEnd': origin + HORIZON_SECONDS,
            'version': curves.METHOD_VERSION,
            'current': current,
            'models': {
                m: {
                    k: rounded(v) if k != 'matched' else v and {a: rounded(b) for a, b in v.items()}
                    for k, v in row.items()
                }
                for m, row in predictions.items()
            },
        }
        # Immutable: later ledger corrections never rewrite an issued prediction.
        with self.store.h.lock:
            self.store.h.db.execute(
                'INSERT OR IGNORE INTO demand_curve_observations VALUES(?,?,?,?,?,?,?)',
                (
                    account,
                    device,
                    checkpoint,
                    curves.METHOD_VERSION,
                    origin,
                    now,
                    json.dumps(packet, allow_nan=False, separators=(',', ':')),
                ),
            )
            self.store.h.db.commit()

    def prune(self, now):
        # Only half-hour checkpoints are scored; the 5-minute ones serve the
        # latest view. Keep a day of those and 45 days of scored checkpoints.
        with self.store.h.lock:
            self.store.h.db.execute(
                """DELETE FROM demand_curve_observations WHERE created_at<?
                OR (checkpoint%1800!=0 AND created_at<?)""",
                (now - KEEP_SECONDS, now - 86400),
            )
            self.store.h.db.commit()
        self.pruned_at = now

    def latest(self, account, device, now):
        if not self.enabled:
            return None
        with self.store.h.lock:
            row = self.store.h.db.execute(
                """SELECT data FROM demand_curve_observations
                WHERE account=? AND device=? AND version=? AND created_at<=?
                ORDER BY checkpoint DESC LIMIT 1""",
                (account, device, curves.METHOD_VERSION, now),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def evaluation(self, account, device, now):
        if not self.enabled:
            return {'status': 'disabled', 'methodVersion': curves.METHOD_VERSION, 'models': []}
        key = (account, device)
        with self.lock:
            old = self.score_cache.get(key)
            if old and 0 <= now - old['at'] < REFRESH_SECONDS:
                return copy.deepcopy(old)
            output = self.score(account, device, now)
            self.score_cache[key] = output
            while len(self.score_cache) > 4:
                del self.score_cache[next(iter(self.score_cache))]
            return copy.deepcopy(output)

    def score(self, account, device, now):
        with read_view(self.store) as view:
            packets = [
                json.loads(r[0])
                for r in view.h.db.execute(
                    """SELECT data FROM demand_curve_observations
                WHERE account=? AND device=? AND version=? AND checkpoint>=? AND created_at<=?
                AND checkpoint%1800=0 ORDER BY checkpoint""",
                    (account, device, curves.METHOD_VERSION, now - SCORE_SECONDS, now),
                )
            ]
            matured = [p for p in packets if p['targetEnd'] + SETTLEMENT_SECONDS <= now]
            # An hour of lead-in lets the ramp filter see how each warm stretch began.
            start = min((p['origin'] for p in matured), default=now) - 3600
            evidence = read_paid_evidence(view, account, device, start, now, now) if matured else {}
        steady = {
            m: {r['at']: r for r in curves.steady(e.get('minutes', []))}
            for m, e in evidence.items()
        }
        models, totals = (
            {},
            {'serving': self.blank(), 'other': self.blank(), 'overall': self.blank()},
        )
        for packet in packets:
            for model, row in packet['models'].items():
                item = models.setdefault(model, self.blank())
                if packet['targetEnd'] + SETTLEMENT_SECONDS > now:
                    item['pendingWindows'] += 1
                    continue
                window = [
                    steady.get(model, {}).get(t)
                    for t in range(packet['origin'], packet['targetEnd'], 60)
                ]
                window = [r for r in window if r]
                if len(window) < MIN_SCORED_MINUTES:
                    continue
                actual = sum(r['usd'] for r in window) * 3600 / sum(r['seconds'] for r in window)
                day = datetime.fromtimestamp(packet['origin']).date().isoformat()
                for target in (
                    item,
                    totals['serving' if row.get('serving') else 'other'],
                    totals['overall'],
                ):
                    self.add(target, row, actual, day)
        for item in list(models.values()) + list(totals.values()):
            self.finish(item)
        overall = totals['overall']
        ready = overall['daysScored'] >= READY_DAYS and overall['pairedWindows'] >= READY_WINDOWS
        leader = None
        if overall['pairedWindows']:
            leader = (
                'curve' if overall['pairedCurveMAE'] < overall['pairedMatchedMAE'] else 'matched'
            )
        return {
            'status': 'ready' if ready else 'collecting',
            'methodVersion': curves.METHOD_VERSION,
            'at': now,
            'recordedCheckpoints': len(packets),
            'overall': overall,
            'serving': totals['serving'],
            'other': totals['other'],
            'leader': leader,
            'models': sorted(
                ({'model': m, **v} for m, v in models.items()), key=lambda r: r['model']
            ),
            'basis': 'Immutable half-hour predictions scored against the next 30 minutes of steady paid warm time '
            f'(at least {MIN_SCORED_MINUTES} minutes, routing ramp excluded). Paired errors compare both '
            'estimators on the same windows. Shadow only: switching still uses the matched estimator.',
            'gate': f'Eligible to drive switching after {READY_DAYS} scored days and {READY_WINDOWS} paired windows, '
            'and only if it beats the matched estimator there and on replay.',
        }

    @staticmethod
    def blank():
        return {
            'windows': 0,
            'pairedWindows': 0,
            'pendingWindows': 0,
            'days': set(),
            'curveError': 0.0,
            'curveSigned': 0.0,
            'pairedCurveError': 0.0,
            'pairedMatchedError': 0.0,
            'inRange': 0,
        }

    @staticmethod
    def add(item, row, actual, day):
        if row.get('usdPerHour') is None:
            return
        item['windows'] += 1
        item['days'].add(day)
        item['curveError'] += abs(row['usdPerHour'] - actual)
        item['curveSigned'] += row['usdPerHour'] - actual
        item['inRange'] += row['lower'] <= actual <= row['upper']
        matched = (row.get('matched') or {}).get('usdPerHour')
        if matched is not None:
            item['pairedWindows'] += 1
            item['pairedCurveError'] += abs(row['usdPerHour'] - actual)
            item['pairedMatchedError'] += abs(matched - actual)

    @staticmethod
    def finish(item):
        n, p = item['windows'], item['pairedWindows']
        error, signed, inside = item.pop('curveError'), item.pop('curveSigned'), item.pop('inRange')
        paired_curve, paired_matched = item.pop('pairedCurveError'), item.pop('pairedMatchedError')
        item['curveMAE'] = error / n if n else None
        item['curveBias'] = signed / n if n else None
        item['inRangeShare'] = inside / n if n else None
        item['pairedCurveMAE'] = paired_curve / p if p else None
        item['pairedMatchedMAE'] = paired_matched / p if p else None
        item['daysScored'] = len(item.pop('days'))
