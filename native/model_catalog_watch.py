"""Network news: models joining or leaving Darkbloom, and big swings in warm capacity.

Fed only by the public fetches the app already makes (Network.listeners): the
/v1/models/capacity list every 30 s (models with at least one routable Mac, with
routable/warm counts and requests in flight) and /v1/stats every 60 s (which model
each online Mac has loaded). The optimizer's own 5-minute /v1/models/catalog copy
is read, never fetched, to confirm a removal. No polling of its own.

Detects:
- 'new': a model not seen before reaches NEW_MIN_MACS Macs (routable in capacity,
  or serving it in /v1/stats) and keeps that for NEW_CONFIRM_SECONDS;
- 'left': a model that had LEFT_MIN_MACS routable Macs drops out of the capacity
  list for LEFT_CONFIRM_SECONDS while the rest of the list is intact, and the
  optimizer's fresh catalog no longer lists it as active. Missing alone never
  confirms it: not without a fresh catalog, nor during a Darkbloom-wide outage
  (network_health), when models drop off the list with their Macs;
- 'collapse' / 'surge': a model's warm Macs stay at half or double the 3 hours
  before for 15 minutes, by at least MIN_CHANGE_MACS Macs, and by that much more
  than the other models' warm total moved (a network-wide drop is an outage, not
  model news).

Thresholds come from Bloomkeeper's own capacity history (opt_network, every 30 s,
Sep 6-28 2026, ~20 recorded days) and the public /v1/stats research poll (every
10 min, Sep 26 21:41 - Sep 28 08:34); see each constant. On that data the rules
fire for the three real arrivals (nvidia-nemotron-3.5-lightning Sep 11, qwen3.8-
flash-next Sep 17, ternary-bonsai-2-27b Sep 18) and the one real removal (Sep 17
14:20: qwen3-vl-30b-a3b-instruct with 111 routable Macs and qwen3.8-flash-next
with 6), never for gemma-4-26b (1-2 Macs, in and out 40 times) or the 30-second
gaps where several models briefly vanish; and nothing at all on Sep 26-28. Diffing
the model lists naively (any Mac, no wait) gives 52 'new' and 50 'left' over those
three weeks, 11 of them on Sep 26-28, all gemma-4-26b coming and going.

Only model names and counts are stored (table model_catalog_events, 90 days via
retention.py; the known-model list in the cache table). Never provider ids.
"""

import copy, json, logging, math, re, sqlite3, statistics, threading, time
from collections import deque

from network_evidence import uncalibrated_error
from provider_reporting import observed_models

log = logging.getLogger('bloom.model_catalog_watch')

KEY = 'model-catalog-watch'
SCHEMA = 1
KINDS = ('new', 'left', 'collapse', 'surge')
MODEL_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/+@-]{0,199}')
MAX_KNOWN = 128
LIVE = ('online', 'serving')  # as network_evidence.LIVE

# New: >= 3 Macs for 10 minutes. The real arrivals reached it 28-49 min after first
# showing up (nemotron 15:02 after 14:34, ternary-bonsai 11:49 after 11:17, qwen3.8-
# flash-next 13:33 after 12:44). gemma-4-26b never had more than 2 routable Macs in
# 40 appearances (Sep 7-27) and 1 Mac in the three polls that saw it; 2 Macs for
# 10 minutes would have fired for it once (Sep 7 03:06).
NEW_MIN_MACS = 3
NEW_CONFIRM_SECONDS = 600
# Left: gone for 10 minutes after having >= 3 routable Macs. Established models
# vanished for a single 30-second poll twice (Sep 8 19:32; Sep 20 19:45, four models
# at once): without the wait that is 5 false 'left' and 5 false returns. The real
# removal stayed gone. Models with 1-2 Macs come and go.
LEFT_MIN_MACS = 3
LEFT_CONFIRM_SECONDS = 600
# A capacity list missing more than half of the known models is a bad or partial
# response, not news: the real removal took 2 of 10 models; the Sep 20 glitch 4 of 9.
PARTIAL_LIST_SHARE = 0.5
# The optimizer refreshes the catalog every 5 minutes; older copies are ignored.
CATALOG_FRESH_SECONDS = 1800
STATS_FRESH_SECONDS = 300
# Collapse / surge. Replayed over Sep 6-28 these fire 13 times (5 collapses, 8 surges;
# none on Sep 26-28): 8 for qwen3-vl-30b-a3b-instruct in the 10 days before it was
# removed, the rest as nemotron, ternary-bonsai and Qwen3.5-9B grew. Looser rules are
# noisy: 0.6x/1.67x fires 21 times (1 on Sep 26-28), 0.7x/1.4x 60 (10), one 5-minute
# bucket instead of 15 minutes 16 (1). The network guard drops a swing the other
# models share (nemotron 124 -> 52 on Sep 17 13:10, while the rest fell by a quarter).
BUCKET_SECONDS = 300
SHORT_BUCKETS = 3  # the last 15 minutes, all present
BASE_BUCKETS = 36  # the 3 hours before
BASE_MIN_BUCKETS = 18
COLLAPSE_RATIO = 0.5
SURGE_RATIO = 2.0
MIN_CHANGE_MACS = 10
REARM = (0.8, 1.25)  # a model re-arms once it is back within these of its baseline
SAVE_SECONDS = 600

# The news list and live estimates
NEWS_DAYS = 30
NEWS_LIMIT = 20
NEW_FRESH_DAYS = 7  # 'new' rows get a live $/h and Mac count for this long
VIEW_KEYS = (
    'macs', 'warm', 'serving', 'demand', 'returned', 'before', 'after',
    'usdPerHour', 'low', 'high', 'providers', 'source',
)  # fmt: skip

# Push: "a new model that pays well on Macs like yours", opt-in (off by default, like
# the demand-spike alerts; only switches this Mac made push by default), at most one
# a day, muted for a week on request. A model qualifies within PUSH_WINDOW_DAYS of
# arriving when its expected $/h for this Mac's hardware class (list price, >= 5 Macs,
# network_evidence) is at least PAYS_WELL_RATIO x the model this Mac serves, or the
# median of the other models when that is unknown, and still beats it at the low end of
# its $/request error (the estimate's usd_error: ln 4 for a new, niche model). The same
# bars as a network home change (manager.NETWORK_HOME_GAIN): list x mix is off by up to
# 2x for gemma and gpt-oss and 4x for other models (calibration-2026-09-28 (b)), so parity
# on list-price means (the old 1.0) says nothing about pay.
PUSH_EVERY_SECONDS = 86400
PUSH_CHECK_SECONDS = 300
PUSH_WINDOW_DAYS = 3
MUTE_SECONDS = 7 * 86400
PAYS_WELL_RATIO = 2.0


def number(value):
    ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    return value if ok and math.isfinite(value) and value >= 0 else None


def model_id(value):
    return value if isinstance(value, str) and MODEL_ID.fullmatch(value) else None


def capacity_counts(data):
    """model -> {'macs': routable, 'warm', 'demand': active + queued} from /v1/models/capacity."""
    models = data.get('models') if isinstance(data, dict) else None
    out = {}
    for m in models if isinstance(models, list) else ():
        if not isinstance(m, dict) or not model_id(m.get('id')):
            continue
        routable, warm = number(m.get('routable_providers')), number(m.get('warm_providers'))
        if routable is None or warm is None:
            continue
        demand = sum(number(m.get(k)) or 0 for k in ('active_requests', 'queued_requests'))
        out[m['id']] = {'macs': int(routable), 'warm': int(warm), 'demand': demand}
    return out


def stats_counts(data):
    """model -> {'serving', 'offering'}: online Macs with it loaded / advertising it."""
    providers = data.get('providers') if isinstance(data, dict) else None
    out = {}
    for p in providers if isinstance(providers, list) else ():
        if not isinstance(p, dict) or p.get('status') not in LIVE:
            continue
        current = model_id(p.get('current_model'))
        if current:
            out.setdefault(current, {'serving': 0, 'offering': 0})['serving'] += 1
        models = p.get('models') if isinstance(p.get('models'), list) else ()
        for m in {m for m in models if model_id(m)}:
            out.setdefault(m, {'serving': 0, 'offering': 0})['offering'] += 1
    return out


def optimizer_sources(optimizer):
    """Read-only accessors for what the optimizer already fetched and read."""

    def catalog():
        with optimizer.lock:
            return copy.deepcopy(optimizer.catalog), optimizer.discovery_at

    def this_mac():
        with optimizer.lock:
            raw = optimizer.raw if isinstance(optimizer.raw, dict) else {}
            offered = observed_models(raw.get('advertised_models'))
            local = [
                m['id']
                for m in optimizer.local
                if isinstance(m, dict) and isinstance(m.get('id'), str)
            ]
        return offered, local

    return catalog, this_mac


class ModelCatalogWatch:
    def __init__(
        self, history, evidence=None, optimizer=None, notify=None, catalog=None, this_mac=None
    ):
        """evidence: NetworkEvidence ($/h on Macs like this one); optimizer: for its catalog
        copy, offered and downloaded models (or pass catalog()/this_mac() directly);
        notify(key, title, body) -> bool sends a phone notice (WebPush.enqueue_notice)."""
        self.h = history
        self.evidence = evidence
        self.notify = notify
        if optimizer is not None:
            catalog, this_mac = optimizer_sources(optimizer)
        self.catalog, self.this_mac = catalog, this_mac
        self.lock = threading.RLock()
        self.capacity, self.capacity_at, self.first_capacity_at = {}, None, None
        self.stats, self.stats_at = {}, None
        self.pending = {}  # model -> since (new, above NEW_MIN_MACS)
        self.absent = {}  # model -> since (left, off the capacity list)
        self.bucket, self.values = None, {}  # current 5-min bucket: model -> [warm]
        self.buckets = {}  # model -> deque[(bucket, median warm)]; '*' = network total
        self.armed = {}  # model -> 'collapse' / 'surge' while that swing lasts
        self.saved_at = 0
        self.checked_at = None
        self.health = None  # network_health.NetworkHealth (collector): no 'left' during an outage
        with history.lock:
            history.db.execute(
                'CREATE TABLE IF NOT EXISTS model_catalog_events('
                'at REAL,kind TEXT,model TEXT,detail TEXT)'
            )
            history.db.execute(
                'CREATE INDEX IF NOT EXISTS model_catalog_events_at ON model_catalog_events(at)'
            )
            history.db.commit()
        self.state = self._load()

    # --- state ----------------------------------------------------------------

    def _load(self):
        saved = self.h.cache(KEY)
        state = {
            'schema': SCHEMA,
            'seededAt': None,
            'known': {},
            'left': {},  # model -> when it left (a return is flagged as such)
            'push': {'enabled': False, 'mutedUntil': None, 'lastSentAt': None, 'pushed': []},
        }
        if isinstance(saved, dict) and saved.get('schema') == SCHEMA:
            if number(saved.get('seededAt')) is not None:
                state['seededAt'] = saved['seededAt']
            known = saved.get('known') if isinstance(saved.get('known'), dict) else {}
            for m, k in list(known.items())[:MAX_KNOWN]:
                if model_id(m) and isinstance(k, dict):
                    state['known'][m] = {
                        'first': number(k.get('first')),
                        'macs': int(number(k.get('macs')) or 0),
                        'cap': k.get('cap') is True,
                    }
            left = saved.get('left') if isinstance(saved.get('left'), dict) else {}
            state['left'] = {
                m: t for m, t in list(left.items())[:MAX_KNOWN] if model_id(m) and number(t)
            }
            push = saved.get('push') if isinstance(saved.get('push'), dict) else {}
            state['push'].update(
                enabled=push.get('enabled') is True,
                mutedUntil=number(push.get('mutedUntil')),
                lastSentAt=number(push.get('lastSentAt')),
                pushed=[m for m in push.get('pushed') or [] if model_id(m)][-MAX_KNOWN:],
            )
        return state

    def _save(self, now, force=False):
        if not force and now - self.saved_at < SAVE_SECONDS:
            return
        self.saved_at = now
        try:
            self.h.cache(KEY, self.state)
        except sqlite3.Error:
            log.exception('Could not save the model watch state')

    def _record(self, at, kind, model, detail):
        try:
            with self.h.lock:
                self.h.db.execute(
                    'INSERT INTO model_catalog_events VALUES(?,?,?,?)',
                    (at, kind, model, json.dumps(detail, allow_nan=False)),
                )
                self.h.db.commit()
        except (sqlite3.Error, ValueError):
            log.exception('Could not save a model watch event')
        log.info('Network news: %s %s', kind, model)

    # --- input ----------------------------------------------------------------

    def on_network(self, key, data, at):
        """Network.listeners hook: every successful public fetch."""
        if key == 'capacity':
            self.observe_capacity(data, at)
        elif key == 'stats':
            self.observe_stats(data, at)
        else:
            return
        self.maybe_push(at)

    def observe_stats(self, data, at):
        counts = stats_counts(data)
        if not counts:
            return
        with self.lock:
            self.stats, self.stats_at = counts, at

    def observe_capacity(self, data, at):
        counts = capacity_counts(data)
        if not counts:
            return []
        with self.lock:
            self.capacity, self.capacity_at = counts, at
            stats = (
                self.stats
                if self.stats_at is not None and 0 <= at - self.stats_at <= STATS_FRESH_SECONDS
                else {}
            )
            if self.state['seededAt'] is None:
                if self.first_capacity_at is None:
                    self.first_capacity_at = at
                if self.stats_at is None and at - self.first_capacity_at < STATS_FRESH_SECONDS:
                    return []  # seed once both lists have been seen (or /v1/stats keeps failing)
                self._seed(counts, stats, at)
                return []
            events = self._membership(counts, stats, at)
            events += self._capacity_swings(counts, at)
        for event in events:
            self._record(*event)
        self._save(at, force=bool(events))
        return events

    def _seed(self, counts, stats, at):
        """First run: everything on the network now is already known (no news)."""
        known = self.state['known']
        for m in list(counts) + list(stats):
            macs = max(counts.get(m, {}).get('macs', 0), stats.get(m, {}).get('serving', 0))
            known[m] = {'first': None, 'macs': macs, 'cap': m in counts}
        self.state['seededAt'] = at
        self._save(at, force=True)

    def _catalog_active(self, now):
        """Active ids in the optimizer's fresh catalog copy, or None when unknown or stale."""
        if self.catalog is None:
            return None
        try:
            catalog, at = self.catalog()
        except Exception:
            return None
        if not isinstance(catalog, list) or not catalog or number(at) is None:
            return None
        if not 0 <= now - at <= CATALOG_FRESH_SECONDS:
            return None
        return {m['id'] for m in catalog if isinstance(m, dict) and m.get('active') is True}

    def _outage(self, now):
        """A Darkbloom-wide problem now (network_health), when models fall off the list."""
        if self.health is None:
            return False
        try:
            outage = self.health.outage(now)
        except Exception:
            return False
        return bool(outage and outage.get('scope') == 'network')

    def _membership(self, counts, stats, at):
        known = self.state['known']
        events = []
        for m in set(counts) | {m for m, s in stats.items() if s['serving']}:
            cap = counts.get(m, {})
            macs = max(cap.get('macs', 0), stats.get(m, {}).get('serving', 0))
            if m in counts:
                # Only the capacity list counts: Macs keep serving a removed model for a while.
                self.absent.pop(m, None)
            if m in known:
                k = known[m]
                if m in counts:
                    k['macs'], k['cap'] = cap['macs'], True
                continue
            if macs < NEW_MIN_MACS:
                self.pending.pop(m, None)
                continue
            since = self.pending.setdefault(m, at)
            if at - since < NEW_CONFIRM_SECONDS or len(known) >= MAX_KNOWN:
                continue
            del self.pending[m]
            known[m] = {'first': at, 'macs': cap.get('macs', 0), 'cap': m in counts}
            detail = {
                'macs': macs,
                'routable': cap.get('macs'),
                'warm': cap.get('warm'),
                'serving': stats.get(m, {}).get('serving'),
                'demand': cap.get('demand'),
                'since': since,
                'returned': m in self.state['left'],
            }
            detail.update(self._estimate(m, at))
            events.append((at, 'new', m, detail))
        for m in [m for m in self.pending if m not in counts and m not in stats]:
            del self.pending[m]
        # Left: only models seen on the capacity list with enough Macs, and only when
        # this list is not a partial one.
        listed = [m for m, k in known.items() if k['cap']]
        missing = [m for m in listed if m not in counts]
        if listed and len(missing) > PARTIAL_LIST_SHARE * len(listed):
            return events
        active = None
        for m in missing:
            k = known[m]
            if k['macs'] < LEFT_MIN_MACS:
                continue
            since = self.absent.setdefault(m, at)
            if at - since < LEFT_CONFIRM_SECONDS:
                continue
            if active is None:
                active = False if self._outage(at) else self._catalog_active(at)
            if not active:
                continue  # no fresh catalog, or Darkbloom is down: missing alone is not news
            if m in active:
                continue  # still in the catalog: its Macs went away (a collapse, not news here)
            del self.absent[m]
            del known[m]
            self.state['left'][m] = at
            events.append((at, 'left', m, {'macs': k['macs'], 'since': since}))
        left = self.state['left']
        for m in [m for m in left if m in known]:
            del left[m]
        if len(left) > MAX_KNOWN:
            for m in sorted(left, key=left.get)[: len(left) - MAX_KNOWN]:
                del left[m]
        return events

    def _capacity_swings(self, counts, at):
        bucket = int(at // BUCKET_SECONDS) * BUCKET_SECONDS
        events = []
        if self.bucket is not None and bucket != self.bucket:
            for m, values in self.values.items():
                self.buckets.setdefault(
                    m, deque(maxlen=SHORT_BUCKETS + BASE_BUCKETS + SHORT_BUCKETS)
                ).append((self.bucket, statistics.median(values)))
            events = self._swings(self.bucket)
            self.values = {}
        if self.bucket != bucket:
            self.bucket = bucket
        for m, c in counts.items():
            self.values.setdefault(m, []).append(c['warm'])
        self.values.setdefault('*', []).append(sum(c['warm'] for c in counts.values()))
        for m in [m for m in self.buckets if m != '*' and m not in counts]:
            if not self.buckets[m] or bucket - self.buckets[m][-1][0] > 4 * 3600:
                self.buckets.pop(m, None)
                self.armed.pop(m, None)
        return events

    def _window(self, m, last, rows=None):
        """(the last SHORT_BUCKETS medians, all present; median of the BASE_BUCKETS before)."""
        rows = dict(self.buckets.get(m) or ()) if rows is None else rows
        short = [rows.get(last - i * BUCKET_SECONDS) for i in range(SHORT_BUCKETS)]
        base = [
            rows.get(last - (SHORT_BUCKETS + i) * BUCKET_SECONDS) for i in range(BASE_BUCKETS)
        ]
        base = [v for v in base if v is not None]
        if None in short or len(base) < BASE_MIN_BUCKETS:
            return None, None
        return short, statistics.median(base)

    def _network(self, m, last):
        """How the warm total of every other model moved over the same windows."""
        total, own = dict(self.buckets.get('*') or ()), dict(self.buckets.get(m) or ())
        others = {b: v - own.get(b, 0) for b, v in total.items()}
        short, base = self._window(m, last, others)
        if not short or not base or base <= 0:
            return None
        return statistics.median(short) / base

    def _swings(self, last):
        events = []
        for m in self.buckets:
            if m == '*':
                continue
            short, base = self._window(m, last)
            if short is None:
                continue
            net = self._network(m, last)
            if not net or net <= 0:
                continue  # nothing else warm, or no network baseline: not model news
            # Sustained: even the highest of the last 15 minutes is half the baseline, or
            # even the lowest is double.
            highest, lowest, now = max(short), min(short), statistics.median(short)
            ratio = now / base if base > 0 else None
            kind = None
            if (
                ratio is not None
                and highest <= COLLAPSE_RATIO * base
                and base - highest >= MIN_CHANGE_MACS
                and ratio / net <= COLLAPSE_RATIO
            ):
                kind = 'collapse'
            elif (
                lowest >= SURGE_RATIO * max(base, 1)
                and lowest - base >= MIN_CHANGE_MACS
                and (ratio is None or ratio / net >= SURGE_RATIO)
            ):
                kind = 'surge'
            armed = self.armed.get(m)
            if kind and kind != armed:
                self.armed[m] = kind
                detail = {
                    'before': round(base, 1),
                    'after': round(now, 1),
                    'network': round(net, 2),
                    'minutes': SHORT_BUCKETS * BUCKET_SECONDS // 60,
                }
                events.append((last + BUCKET_SECONDS, kind, m, detail))
            elif not kind and armed:
                if (armed == 'collapse' and highest > REARM[0] * base) or (
                    armed == 'surge' and lowest < REARM[1] * base
                ):
                    del self.armed[m]
        return events

    # --- estimates --------------------------------------------------------------

    def _estimate(self, model, now, basis=None):
        """$/h on Macs like this one from network_evidence, or {} without evidence."""
        if self.evidence is None:
            return {}
        try:
            e = self.evidence.estimate(model, now=now, basis=basis)
        except Exception:
            log.exception('Network estimate failed for the model watch')
            return {}
        if not e or e.get('source') == 'none' or e.get('usd_per_h') is None:
            return {'cell': e.get('cell') if e else None}
        return {
            'cell': e.get('cell'),
            'usdPerHour': e['usd_per_h'],
            'low': e.get('low'),
            'high': e.get('high'),
            'providers': e.get('providers'),
            'source': e.get('source'),
        }

    # --- push -----------------------------------------------------------------

    def push_status(self, now=None):
        now = time.time() if now is None else now
        with self.lock:
            push = self.state['push']
            muted = push['mutedUntil'] if (push['mutedUntil'] or 0) > now else None
            return {
                'enabled': push['enabled'],
                'mutedUntil': muted,
                'lastSentAt': push['lastSentAt'],
                'perDay': 1,
                'available': self.notify is not None,
            }

    def set_push(self, data, now=None):
        """{'action': 'push', 'enabled': bool} | {'action': 'mute'} | {'action': 'unmute'}."""
        now = time.time() if now is None else now
        if not isinstance(data, dict):
            raise ValueError('Invalid request.')
        action = data.get('action')
        with self.lock:
            push = self.state['push']
            if action == 'push' and set(data) == {'action', 'enabled'}:
                if not isinstance(data['enabled'], bool):
                    raise ValueError('Invalid request.')
                push['enabled'] = data['enabled']
                push['mutedUntil'] = None
            elif action == 'mute' and set(data) == {'action'}:
                push['mutedUntil'] = now + MUTE_SECONDS
            elif action == 'unmute' and set(data) == {'action'}:
                push['mutedUntil'] = None
            else:
                raise ValueError('Invalid request.')
            self._save(now, force=True)
        return self.push_status(now)

    def _reference(self, table):
        """This Mac's current model's $/h in `table`, else the median of the others."""
        served = None
        try:
            served = (self.evidence.self_percentile() or {}).get('model')
        except Exception:
            pass
        if not served and self.this_mac is not None:
            try:
                offered = self.this_mac()[0]
                served = offered[0] if len(offered) == 1 else None
            except Exception:
                served = None
        rows = {r['model']: r for r in table if r.get('usd_per_h') is not None}
        return served, rows

    def pays_well(self, model, now):
        """(True, detail) when `model` earns at least PAYS_WELL_RATIO x the reference."""
        if self.evidence is None:
            return False, None
        try:
            table = self.evidence.table(now=now, basis='list')['models']
        except Exception:
            log.exception('Network table failed for the model watch')
            return False, None
        served, rows = self._reference(table)
        mine = rows.get(model)
        if not mine or not mine.get('low'):
            return False, None
        if served in rows and served != model:
            reference, label = rows[served]['usd_per_h'], served
        else:
            others = [r['usd_per_h'] for m, r in rows.items() if m != model]
            if not others:
                return False, None
            reference, label = statistics.median(others), None
        error = mine.get('usd_error')
        error = error if number(error) is not None else uncalibrated_error(model)
        if (
            reference <= 0
            or mine['usd_per_h'] < PAYS_WELL_RATIO * reference
            or mine['usd_per_h'] * math.exp(-error) <= reference
        ):
            return False, None
        return True, {
            'usdPerHour': mine['usd_per_h'],
            'ratio': mine['usd_per_h'] / reference,
            'against': label,
            'providers': mine.get('providers'),
        }

    def maybe_push(self, now):
        """At most one notice a day, only when turned on and not muted."""
        with self.lock:
            push = self.state['push']
            if (
                self.notify is None
                or not push['enabled']
                or (push['mutedUntil'] or 0) > now
                or now - (push['lastSentAt'] or 0) < PUSH_EVERY_SECONDS
                or (
                    self.checked_at is not None and 0 <= now - self.checked_at < PUSH_CHECK_SECONDS
                )
            ):
                return None
            self.checked_at = now
            pushed = set(push['pushed'])
            present = dict(self.capacity)
        for at, model in self._recent_new(now - PUSH_WINDOW_DAYS * 86400):
            if model in pushed or model not in present:
                continue
            good, detail = self.pays_well(model, now)
            if not good:
                continue
            macs = present[model]['warm']
            than = (
                f'{detail["ratio"]:.1f}x what {detail["against"]} makes there'
                if detail['against']
                else f'{detail["ratio"]:.1f}x the typical model there'
            )
            body = (
                f'{model} is new on Darkbloom: {macs} Macs serving it, about '
                f'${detail["usdPerHour"]:.2f}/h on Macs like yours ({than}). '
                'An estimate from public network data, not a promise.'
            )
            try:
                sent = self.notify(
                    f'catalog-new-{model}-{int(at)}', 'Bloomkeeper · new model pays well', body
                )
            except Exception:
                log.exception('Model watch notice failed')
                sent = False
            if sent:
                with self.lock:
                    push['lastSentAt'] = now
                    push['pushed'] = (push['pushed'] + [model])[-MAX_KNOWN:]
                    self._save(now, force=True)
                return model
            return None
        return None

    def _recent_new(self, since):
        with self.h.lock:
            return [
                (r[0], r[1])
                for r in self.h.db.execute(
                    "SELECT at,model FROM model_catalog_events WHERE kind='new' AND at>=? "
                    'ORDER BY at DESC LIMIT ?',
                    (since, NEWS_LIMIT),
                )
            ]

    # --- output ---------------------------------------------------------------

    def view(self, now=None):
        """The Network news list for the UI (GET /api/network/news)."""
        now = time.time() if now is None else now
        with self.h.lock:
            rows = self.h.db.execute(
                'SELECT at,kind,model,detail FROM model_catalog_events WHERE at>=? '
                'ORDER BY at DESC LIMIT ?',
                (now - NEWS_DAYS * 86400, NEWS_LIMIT),
            ).fetchall()
        offered, local = [], []
        if self.this_mac is not None:
            try:
                offered, local = self.this_mac()
            except Exception:
                pass
        with self.lock:
            present = dict(self.capacity) if self.capacity_at is not None else {}
            seeded = self.state['seededAt']
        items, newest = [], {}
        for at, kind, model, detail in rows:
            if kind not in KINDS or not model_id(model):
                continue
            try:
                detail = json.loads(detail) if detail else {}
            except ValueError:
                detail = {}
            item = {'at': at, 'kind': kind, 'model': model, 'here': None, 'current': False}
            if isinstance(detail, dict):
                item.update({k: v for k, v in detail.items() if k in VIEW_KEYS})
            latest = model not in newest
            newest.setdefault(model, kind)
            if kind == 'new' and latest and now - at <= NEW_FRESH_DAYS * 86400:
                if model in present:
                    item['warm'] = present[model]['warm']
                    item['macs'] = present[model]['macs']
                    item['demand'] = present[model]['demand']
                    item['current'] = True
                item.update(self._estimate(model, now))
            if kind == 'left' and latest and model not in present:
                here = 'offered' if model in offered else 'downloaded' if model in local else None
                item['here'] = here
            items.append(item)
        return {
            'schema': SCHEMA,
            'at': now,
            'watchingSince': seeded,
            'items': items,
            'push': self.push_status(now),
        }
