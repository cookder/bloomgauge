"""'It's not you': notice when Darkbloom itself stops giving Macs work.

Fed from the app's existing public fetches (Network.listeners and
Network.error_listeners); nothing is polled for this:

- /v1/models/capacity, every 30 s: warm providers per model, and their total;
- /v1/stats, every 60 s: providers online or serving, network-wide and per hardware
  cell, and the network's request counter (requests a minute: shown, never a trigger);
- HTTP 5xx replies from Darkbloom's API.

Each count is compared with its own median over the previous 2 hours. A problem starts
when a count falls far below that and stays there (the rules below), and ends when it
is back to RECOVER of the level before for RECOVER_SECONDS. Readings taken during a
problem never become the baseline for the next one.

Evidence behind the numbers (Andrew's history, read-only: opt_network = every capacity
reading Sep 6 20:00 - Sep 28 08:40 CDT, ~52,700 readings; `network` = per-minute requests;
and the gap-2 /v1/stats poll Sep 26 21:41 - Sep 28 08:34, 10-minute spacing):
- Total warm providers are steady: a 5-minute median is within 0.974-1.03 of its 2-hour
  median 91% of the time, day and night (nightly demand dips move requests, not warm
  Macs). The Sep 26 01:11-09:58 drained night (this Mac's own problem) never went below
  0.98.
- Coordinator restarts collapse it to 0.02-0.25 of the baseline, below half for 30 s to
  3 min (Sep 7 01:43 and 18:54, Sep 8 19:32, Sep 14 23:03, Sep 20 15:56, Sep 23 16:22,
  Sep 27 23:37), while per-minute requests fell to 0.4-2% of normal in five of the seven
  (14% and 36% in the others), usually right after the API stopped answering (/v1/stats
  returned HTTP 503 at Sep 27 23:34). Warm Macs then come back over 5-45 minutes.
- Partial drops to 0.65-0.75 for 20-45 min happen too, with requests steady. This Mac got
  0.0-0.3 jobs a minute through three of them (Sep 11 00:56, Sep 17 12:59, Sep 22 18:56)
  against 2.4-12.5 in the hour before; in others it kept working.
- Replaying every stored reading through these rules flags 14 incidents in three weeks,
  all network-wide (5 coordinator restarts, 9 partial drops); no nightly dip on its own.
- Request throughput is not a trigger: its 5-minute mean fell below 0.2 of its 2-hour
  median in 0.1% of windows with warm counts steady (a big client stopping, e.g. Sep 9
  12:42 12,000 -> 2,400 a minute).
- The Sep 25 09:48-09:55 App Attest drop is not visible network-wide (warm -2%, requests
  up): it hit some Macs' trust (this one's until 11:13), which item 1 (peer stalls) and
  the trust reason cover.
Provider ids are never kept: stats are reduced to counts on arrival. Incidents
(aggregates only) are written to network_incidents and kept 90 days (retention.py).
"""

import logging, random, sqlite3, statistics, threading, time
from collections import deque
from history import epoch
from model_combinations import members
from network_evidence import LIVE, cell_of, number
import stall_recovery

log = logging.getLogger('bloom.network_health')

BASELINE_SECONDS = 2 * 3600
# Too little data until a count has 20 readings spanning 30 minutes (outage() is None).
BASELINE_MIN_SECONDS = 1800
BASELINE_MIN_READINGS = 20
GAP_SECONDS = 150  # readings further apart (sleep, a missed fetch) break a low run
STALE_SECONDS = 600  # an incident with no fresh evidence is not reported as current
MAX_SECONDS = 6 * 3600  # then the lower level is the new normal
# Recovery: back to 0.85 of the level before for 2 minutes. The partial drops above sat
# at 0.65-0.75 until they ended; normal 5-minute medians are >= 0.974 of it 95% of the time.
RECOVER = 0.85
RECOVER_SECONDS = 120
RECOVER_READINGS = 3
WRITE_SECONDS = 60
# Rules: (at most this share of the baseline, on at least this many readings in a row,
# spanning at least this many seconds). Warm providers in total (capacity, every 30 s):
# a collapse to half on two readings (only coordinator restarts went below 0.5; a single
# low reading is a blip), or a drop to 0.8 held for 5 minutes (the partial drops).
WARM_RULES = ((0.5, 2, 25), (0.8, 5, 300))
MIN_NETWORK_WARM = 100
# Online + serving providers (/v1/stats, every 60 s). Steady: the lowest reading in the
# poll was 0.978 of its 2-hour median, even across two coordinator restarts (providers
# reconnect within seconds), so a 25% drop for 2 minutes means Macs were dropped or
# marked untrusted.
LIVE_RULES = ((0.75, 3, 120),)
MIN_NETWORK_LIVE = 100
# One model's warm providers. Models with demand never went below 0.51 of their median in
# three weeks outside network-wide problems; a model without demand swings freely
# (qwen3-vl-30b: 4-55 warm, no requests), so a model needs active requests to count.
MODEL_RULES = ((0.35, 10, 300),)
MIN_MODEL_WARM = 20
MIN_MODEL_ACTIVE = 1.0
# One hardware cell's online providers ('M5 Pro|48'). Cells with >= 20 never went below
# 0.85 in the poll (>= 40: 0.93).
CELL_RULES = ((0.6, 3, 120),)
MIN_CELL_LIVE = 20
# Darkbloom's API down: two HTTP 5xx replies in a row from /v1/models/capacity at least
# 25 s apart, with another public endpoint also answering 5xx. A dropped connection
# (this Mac's own network) is not Darkbloom's reply and never counts.
API_FAILURES = 2
API_SECONDS = 25
API_CORROBORATE_SECONDS = 180
API_OTHER = ('stats', 'totals', 'series', 'pricing')
SCOPES = ('network', 'cell', 'model')
# After a problem ends, the stall ladder counts this Mac's silence from its end: the silence
# during it was the network's. Otherwise every Mac that went quiet in it has minutes of
# silence the moment it clears and the whole fleet probes and restarts at once (a restart
# storm on a coordinator just back). Each install adds its own random 0-5 minutes on top, so
# the Macs still quiet after it do not all act in the same minute either. Ended problems are
# remembered for an hour (the longest wait is 20 + 5 minutes).
AFTER_JITTER_SECONDS = 300
ENDED_SECONDS = 3600
ENDED_KEEP = 32


def http_code(error):
    """'HTTP 503' (network.py) -> 503; anything else (a dropped connection) -> None."""
    if isinstance(error, int) and not isinstance(error, bool):
        return error
    if isinstance(error, str) and error.startswith('HTTP '):
        try:
            return int(error[5:])
        except ValueError:
            return None
    return None


def clock_time(at):
    return time.strftime('%H:%M', time.localtime(at))


def count(value):
    return '{:,}'.format(int(round(value)))


class Watch:
    """One count against its own 2-hour median: a low run, then an incident, then recovery."""

    def __init__(self, rules, minimum):
        self.rules, self.minimum = rules, minimum
        self.loosest = max(r[0] for r in rules)
        self.readings = deque()  # [at, value, excluded from baselines]
        self.run = []  # consecutive low readings: (at, value, ratio)
        self.active = None
        self.bad_at = None  # the last reading that showed the problem
        self.base = None  # baseline at the last reading
        self.ended = None  # how the last incident ended: 'recovered', 'expired' or 'stale'

    def baseline(self, t):
        values, first = [], None
        for at, value, excluded in self.readings:
            if t - BASELINE_SECONDS <= at < t and not excluded:
                values.append(value)
                first = at if first is None else first
        if len(values) < BASELINE_MIN_READINGS or t - first < BASELINE_MIN_SECONDS:
            return None
        return statistics.median(values)

    def add(self, t, value, allow=True):
        """Record a reading; returns 'start', 'end' or None."""
        if self.readings and t <= self.readings[-1][0]:
            return None
        previous = self.readings[-1][0] if self.readings else None
        self.base = base = self.active['before'] if self.active else self.baseline(t)
        self.readings.append([t, value, self.active is not None])
        while self.readings and self.readings[0][0] < t - BASELINE_SECONDS - GAP_SECONDS:
            self.readings.popleft()
        if self.active:
            return self._track(t, value, previous)
        if base is None or base < self.minimum:
            self.run = []
            return None
        ratio = value / base
        if ratio > self.loosest:
            self.run = []
            return None
        if self.run and t - self.run[-1][0] > GAP_SECONDS:
            self.run = []
        self.run.append((t, value, ratio))
        del self.run[:-64]
        self.bad_at = t
        if not allow:
            return None
        for limit, readings, seconds in self.rules:
            tail = []
            for r in reversed(self.run):
                if r[2] > limit:
                    break
                tail.append(r)
            if len(tail) >= readings and tail[0][0] - tail[-1][0] >= seconds:
                since = self.run[0][0]
                self.active = {
                    'since': since,
                    'before': base,
                    'worst': min(r[1] for r in self.run),
                    'value': value,
                    'at': t,
                    'recover': None,
                    'recovered': 0,
                }
                for r in self.readings:
                    if r[0] >= since:
                        r[2] = True
                self.run = []
                return 'start'
        return None

    def _track(self, t, value, previous):
        a = self.active
        a['worst'], a['value'], a['at'] = min(a['worst'], value), value, t
        if value >= RECOVER * a['before'] and not (
            previous is not None and t - previous > GAP_SECONDS
        ):
            if a['recover'] is None:
                a['recover'], a['recovered'] = t, 0
            a['recovered'] += 1
            if t - a['recover'] >= RECOVER_SECONDS and a['recovered'] >= RECOVER_READINGS:
                return self.close('recovered')
        else:
            a['recover'] = None
            if value < RECOVER * a['before']:
                self.bad_at = t
        if t - a['since'] > MAX_SECONDS:
            return self.close('expired')
        return None

    def close(self, how):
        self.active, self.run, self.ended = None, [], how
        return 'end'


class NetworkHealth:
    def __init__(self, history, context=None, clock=time.time, jitter=None):
        """context() -> (this Mac's cell, [models it offers]): which model and cell problems
        concern this Mac. Network-wide problems always do. jitter: this install's extra wait
        after a problem ends (seconds; default random in 0-AFTER_JITTER_SECONDS)."""
        self.h, self.context, self.clock = history, context, clock
        self.jitter = random.uniform(0, AFTER_JITTER_SECONDS) if jitter is None else jitter
        self.ended = deque(maxlen=ENDED_KEEP)  # problems that ended (public fields + end, how)
        self.lock = threading.RLock()
        self.warm = Watch(WARM_RULES, MIN_NETWORK_WARM)
        self.live = Watch(LIVE_RULES, MIN_NETWORK_LIVE)
        self.models, self.demand, self.cells = {}, {}, {}
        self.requests = deque()  # (at, requests a minute)
        self.counter = None  # (snapshot time, total_requests)
        self.stats_t = None
        self.failures = {}  # endpoint -> {'first', 'last', 'count', 'code'} of HTTP 5xx
        self.api = None
        self.incidents = {}  # (scope, key) -> incident
        with history.lock:
            history.db.execute(
                'CREATE TABLE IF NOT EXISTS network_incidents(start REAL,end REAL,scope TEXT,'
                'key TEXT,before REAL,during REAL,detail TEXT)'
            )
            history.db.execute(
                'CREATE INDEX IF NOT EXISTS network_incidents_start ON network_incidents(start)'
            )
            history.db.commit()
        self._seed()

    # --- input -------------------------------------------------------------

    def _seed(self):
        """Warm counts from saved capacity readings (opt_network), so a restart needs no
        new 30-minute baseline. Seeded readings never start an incident."""
        now = self.clock()
        try:
            with self.h.lock:
                rows = self.h.db.execute(
                    'SELECT at,model,active,warm FROM opt_network WHERE at>=? AND at<=? '
                    'ORDER BY at',
                    (now - BASELINE_SECONDS, now),
                ).fetchall()
        except sqlite3.Error:
            return
        grouped = {}
        for at, model, active, warm in rows:
            grouped.setdefault(at, []).append(
                {'id': model, 'active_requests': active, 'warm_providers': warm}
            )
        with self.lock:
            for at in sorted(grouped):
                self._capacity(grouped[at], at, allow=False)
            for watch in (self.warm, *self.models.values()):
                watch.run = []

    def on_network(self, key, data, at):
        """Network.listeners hook: every successful public fetch."""
        with self.lock:
            self.failures.pop(key, None)
            models = data.get('models') if isinstance(data, dict) else None
            if key == 'capacity' and isinstance(models, list):
                self._capacity(models, at)
            elif key == 'stats' and isinstance(data, dict):
                self._stats(data, at)
            self._api_state(at)
            self._persist(self._settle(at), at)

    def on_error(self, key, error, at):
        """Network.error_listeners hook: a failed fetch ('HTTP 503', 'Connection unavailable')."""
        code = http_code(error)
        if code is None or not 500 <= code <= 599:
            return
        with self.lock:
            f = self.failures.get(key)
            if f is None or at - f['last'] > GAP_SECONDS:
                f = self.failures[key] = {'first': at, 'last': at, 'count': 0, 'code': code}
            f['last'], f['count'], f['code'] = at, f['count'] + 1, code
            self._api_state(at)
            self._persist(self._settle(at), at)

    def _capacity(self, models, at, allow=True):
        total, seen = 0, 0
        blocked = not allow or ('network', None) in self.incidents
        for m in models:
            if not isinstance(m, dict) or not isinstance(m.get('id'), str):
                continue
            warm, active = number(m.get('warm_providers')), number(m.get('active_requests'))
            if warm is None or warm < 0:
                continue
            total += warm
            seen += 1
            demand = self.demand.setdefault(m['id'], deque())
            if active is not None and active >= 0:
                demand.append((at, active))
            while demand and demand[0][0] < at - BASELINE_SECONDS:
                demand.popleft()
            watch = self.models.setdefault(m['id'], Watch(MODEL_RULES, MIN_MODEL_WARM))
            wanted = not blocked and self._has_demand(m['id'], at)
            self._change(('model', m['id']), watch, watch.add(at, warm, wanted), at)
        if seen:
            self._change(('network', None), self.warm, self.warm.add(at, total, allow), at)
        old = [n for n, w in self.models.items() if w.readings[-1][0] < at - BASELINE_SECONDS]
        for name in old:
            if not self.models[name].active:
                del self.models[name]
                self.demand.pop(name, None)

    def _has_demand(self, model, at):
        values = [a for t, a in self.demand.get(model, ()) if t < at]
        enough = len(values) >= BASELINE_MIN_READINGS
        return enough and statistics.median(values) >= MIN_MODEL_ACTIVE

    def _stats(self, data, at):
        providers = data.get('providers')
        if not isinstance(providers, list):
            return
        try:
            t = epoch(data['snapshot_at'])
        except (KeyError, TypeError, ValueError, AttributeError):
            t = at
        if self.stats_t is not None and t <= self.stats_t:
            return  # the server's cached snapshot
        self.stats_t = t
        live, cells = 0, {}
        for p in providers:
            if isinstance(p, dict) and p.get('status') in LIVE:
                live += 1
                cell = cell_of(p.get('chip_family'), p.get('chip_tier'), p.get('memory_gb'))
                if cell:
                    cells[cell] = cells.get(cell, 0) + 1
        total = number(data.get('total_requests'))
        if total is not None:
            last_t, last = self.counter or (None, None)
            if last is not None and 30 <= t - last_t <= GAP_SECONDS * 2 and total >= last:
                self.requests.append((at, (total - last) * 60 / (t - last_t)))
            self.counter = (t, total)
            while self.requests and self.requests[0][0] < at - BASELINE_SECONDS:
                self.requests.popleft()
        self._change(('network', None), self.live, self.live.add(at, live), at)
        blocked = ('network', None) in self.incidents
        for cell in set(cells) | set(self.cells):
            watch = self.cells.setdefault(cell, Watch(CELL_RULES, MIN_CELL_LIVE))
            self._change(('cell', cell), watch, watch.add(at, cells.get(cell, 0), not blocked), at)
        for cell in [c for c, w in self.cells.items() if not w.active and not cells.get(c)]:
            if all(v == 0 for _, v, _ in self.cells[cell].readings):
                del self.cells[cell]

    def _api_state(self, at):
        cap = self.failures.get('capacity')
        corroborated = cap and any(
            self.failures[k]['last'] >= cap['first'] - API_CORROBORATE_SECONDS
            for k in API_OTHER
            if k in self.failures
        )
        if (
            cap
            and corroborated
            and cap['count'] >= API_FAILURES
            and cap['last'] - cap['first'] >= API_SECONDS
        ):
            latest = max(f['last'] for f in self.failures.values())
            self.api = {'since': cap['first'], 'at': latest, 'code': cap['code']}
        else:
            self.api = None

    # --- incidents -----------------------------------------------------------

    @staticmethod
    def _incident(scope, key, since, at, watch):
        return {
            'scope': scope,
            'key': key,
            'since': since,
            'at': at,  # latest evidence either way
            'bad': at,  # latest evidence of the problem: the end once it is over
            'watch': watch,  # the count behind before/lowest
            'before': None,
            'lowest': None,
            'signals': set(),
            'code': None,
            'row': None,
            'written': None,
            'saved': None,  # the values last written to its row
            'closed': None,
            'how': None,  # 'recovered', 'expired' (the lower level is the new normal), 'stale'
        }

    def _change(self, ident, watch, event, at):
        """Keep a model or cell incident in step with its watch; note fresh evidence."""
        incident = self.incidents.get(ident)
        if incident is not None:
            incident['at'] = at
            if watch.bad_at == at:
                incident['bad'] = at
        if event == 'start' and ident[0] != 'network':
            incident = self.incidents[ident] = self._incident(
                ident[0], ident[1], watch.active['since'], at, watch
            )
            incident['signals'].add(ident[0])

    def _settle(self, at):
        """Open, update or close incidents; returns those whose row needs writing."""
        changes = []
        net = self.incidents.get(('network', None))
        sources = {'warm': self.warm.active, 'live': self.live.active, 'api': self.api}
        signals = {name for name, on in sources.items() if on}
        if signals:
            since = min(sources[name]['since'] for name in signals)
            if net is None:
                measure = self.live if signals == {'live'} else self.warm
                net = self._incident('network', None, since, at, measure)
                self.incidents[('network', None)] = net
            net['since'] = min(net['since'], since)
            net['signals'] |= signals
            net['at'] = at
            if self.api:
                net['bad'] = max(net['bad'], self.api['at'])
                net['code'] = self.api['code']
        elif net is not None and not (self.warm.run or self.live.run):
            # Every signal has recovered and no new drop is building up.
            watches = {'warm': self.warm, 'live': self.live}
            expired = any(watches[s].ended == 'expired' for s in net['signals'] if s in watches)
            self._end(net, 'expired' if expired else 'recovered')
            del self.incidents[('network', None)]
        if net is not None:
            changes.append(net)
        for ident, incident in list(self.incidents.items()):
            watch = incident['watch']
            if incident['closed'] is None:
                if watch.active:
                    incident['before'] = watch.active['before']
                    incident['lowest'] = watch.active['worst']
                elif incident['before'] is None:
                    incident['before'] = watch.base
                last = watch.readings[-1] if watch.readings else None
                if last and last[0] >= incident['since']:
                    low = incident['lowest']
                    incident['lowest'] = last[1] if low is None else min(low, last[1])
            if ident[0] != 'network':
                last = watch.readings[-1][0] if watch.readings else None
                if watch.active is not None and (last is None or at - last > STALE_SECONDS):
                    # The model left the capacity list (or the cell the stats): no evidence
                    # either way any more, so stop tracking it rather than keep it open.
                    watch.close('stale')
                if watch.active is None:
                    self._end(incident, watch.ended or 'recovered')
                    del self.incidents[ident]
                changes.append(incident)
        return changes

    def _end(self, incident, how):
        incident['closed'], incident['how'] = incident['bad'], how
        self.ended.append(
            {
                'scope': incident['scope'],
                'key': incident['key'],
                'since': incident['since'],
                'end': incident['closed'],
                'how': how,
            }
        )

    def _drop(self, incident):
        if 'api' in incident['signals']:
            # With the API down (capacity missing for 1.5-4.5 min, ten times in three weeks),
            # per-minute requests fell to 0.4-20% of normal, 2% in the middle case.
            return 1.0
        before, lowest = incident['before'], incident['lowest']
        return max(0.0, 1 - lowest / before) if before and lowest is not None else 0.0

    def _detail(self, incident):
        before, during = incident['before'], incident['lowest']
        parts = []
        if 'api' in incident['signals']:
            parts.append(
                "Darkbloom's API answered HTTP %s instead of network data" % incident['code']
            )
        if before is not None and during is not None:
            label = {
                'model': 'Macs warm on ' + str(incident['key']),
                'cell': 'Online Macs of this type (' + str(incident['key']) + ')',
            }.get(
                incident['scope'],
                'Online Macs' if incident['watch'] is self.live else 'Macs ready to serve',
            )
            parts.append(
                '%s: %s before, %s at the lowest' % (label, count(before), count(during))
            )
        rates = [r for t, r in self.requests if t < incident['since']]
        now = [r for t, r in self.requests if t >= incident['since']]
        if incident['scope'] == 'network' and rates and now:
            parts.append(
                'requests: %s a minute before, %s at the lowest'
                % (count(statistics.median(rates)), count(min(now)))
            )
        return ('; '.join(parts) or 'Darkbloom network readings fell sharply') + '.'

    def _public(self, incident):
        return {
            'since': incident['since'],
            'scope': incident['scope'],
            'key': incident['key'],
            'dropPct': round(100 * self._drop(incident), 1),
            'detail': self._detail(incident),
        }

    def _persist(self, changes, at):
        """Write new, changed (at most once a minute) and closed incidents: counts, times,
        scope and model or cell name only; a row whose values did not change is not written
        again. Called with self.lock held."""
        rows = []
        for incident in changes:
            closed = incident['closed'] is not None
            if not closed and incident['written'] is not None:
                if at - incident['written'] < WRITE_SECONDS:
                    continue
            if incident['row'] is None and not closed:
                log.info(
                    'Network problem started: %s, %d%% down',
                    incident['scope'],
                    round(100 * self._drop(incident)),
                )
            elif closed:
                log.info('Network problem ended (%s): %s', incident['how'], incident['scope'])
            incident['written'] = at
            end = incident['closed'] if closed else incident['bad']
            values = (
                incident['since'],
                end,
                incident['before'],
                incident['lowest'],
                self._detail(incident),
            )
            if incident['row'] is None or values != incident['saved']:
                rows.append((incident, values))
        if not rows:
            return
        try:
            with self.h.lock:
                for incident, values in rows:
                    if incident['row'] is None:
                        incident['row'] = self.h.db.execute(
                            'INSERT INTO network_incidents'
                            '(start,end,before,during,detail,scope,key) VALUES(?,?,?,?,?,?,?)',
                            (*values, incident['scope'], incident['key']),
                        ).lastrowid
                    else:
                        self.h.db.execute(
                            'UPDATE network_incidents SET start=?,end=?,before=?,during=?,detail=? '
                            'WHERE rowid=?',
                            (*values, incident['row']),
                        )
                self.h.db.commit()
            for incident, values in rows:
                incident['saved'] = values
        except sqlite3.Error:
            log.exception('Could not save a network incident')

    # --- output ------------------------------------------------------------

    def _context(self):
        try:
            cell, models = self.context() if self.context else (None, ())
            return cell, set(models or ())
        except Exception:
            log.debug('Network health context unavailable', exc_info=True)
            return None, set()

    def _current(self, now):
        with self.lock:
            return [
                (i, self._public(i))
                for i in self.incidents.values()
                if i['closed'] is None and 0 <= now - i['at'] <= STALE_SECONDS
            ]

    def _relevant(self, model):
        """(scope, key) -> whether a problem there concerns this Mac: network-wide always,
        its own cell, and a model it offers (or, given `model`, that model only)."""
        cell, offered = self._context()
        models = (set(members(model)) or {model}) if model else offered

        def concerns(scope, key):
            return (
                scope == 'network'
                or (scope == 'cell' and key == cell)
                or (scope == 'model' and key in models)
            )

        return concerns

    def outage(self, now=None, model=None):
        """The Darkbloom problem that concerns this Mac now, or None (also while there is
        too little data): {'since': epoch s, 'scope': 'network'|'model'|'cell', 'key': model,
        cell or None, 'dropPct': share of Macs that stopped getting work (0-100),
        'detail': str}. Network-wide problems come first. `model`: a problem of one model
        counts only for that model (the stall ladder's stalled model), not every one offered."""
        now = self.clock() if now is None else now
        current = self._current(now)
        network = [p for i, p in current if i['scope'] == 'network']
        if network:
            return network[0]
        others = [p for i, p in current if i['scope'] != 'network']
        if not others:
            return None
        concerns = self._relevant(model)
        relevant = [p for p in others if concerns(p['scope'], p['key'])]
        return max(relevant, key=lambda p: p['dropPct']) if relevant else None

    def signal(self, now=None, model=None):
        """What the current outage was measured on: 'api', 'warm', 'live', 'model' or 'cell'."""
        return self._signal(self.outage(now, model))

    def ended_outage(self, now=None, model=None):
        """The latest problem concerning this Mac (as in outage()) that ended in the last
        ENDED_SECONDS, or None: {'scope', 'key', 'since', 'end', 'how'}."""
        now = self.clock() if now is None else now
        concerns = self._relevant(model)
        with self.lock:
            ended = [
                dict(e)
                for e in self.ended
                if 0 <= now - e['end'] <= ENDED_SECONDS and concerns(e['scope'], e['key'])
            ]
        return max(ended, key=lambda e: e['end']) if ended else None

    def status_of(self, scope, key, since):
        """How a problem outage() reported stands now: 'open' (maybe without fresh
        evidence), 'recovered', 'expired' (the lower level became the new normal), 'stale'
        (its model left the list: no evidence either way) or None (not known any more)."""
        with self.lock:
            # An open problem's start only ever moves earlier (a second signal joining it).
            incident = self.incidents.get((scope, key))
            if incident is not None and incident['since'] <= since:
                return 'open'
            return next(
                (
                    e['how']
                    for e in reversed(self.ended)
                    if (e['scope'], e['key']) == (scope, key) and e['since'] <= since <= e['end']
                ),
                None,
            )

    def _signal(self, outage):
        if not outage:
            return None
        if outage['scope'] != 'network':
            return outage['scope']
        with self.lock:
            incident = self.incidents.get(('network', None))
            signals = incident['signals'] if incident else set()
        return next((s for s in ('api', 'warm', 'live') if s in signals), 'warm')

    def view(self, now=None):
        """For the snapshot (`networkHealth`): the outage, if any, and the latest counts."""
        now = self.clock() if now is None else now
        outage = self.outage(now)
        with self.lock:
            warm, live = self.warm, self.live
            rates = [r for _, r in self.requests]
            numbers = {
                'warm': warm.readings[-1][1] if warm.readings else None,
                'warmBaseline': warm.base,
                'live': live.readings[-1][1] if live.readings else None,
                'liveBaseline': live.base,
                'requestsPerMinute': rates[-1] if rates else None,
                'requestsBaseline': statistics.median(rates) if len(rates) >= 3 else None,
                'updatedAt': max(
                    (w.readings[-1][0] for w in (warm, live) if w.readings), default=None
                ),
            }
        learning = numbers['warmBaseline'] is None and numbers['liveBaseline'] is None
        return {
            'status': 'outage' if outage else 'learning' if learning else 'ok',
            'outage': {**outage, 'signal': self._signal(outage)} if outage else None,
            'network': numbers,
        }


def describe(outage, signal=None):
    """One sentence for the activity log and stall reasons (lib/network-health.ts words
    the banner the same way)."""
    at = clock_time(outage['since'])
    if signal == 'api':
        return (
            'Darkbloom network problem since %s: Darkbloom’s servers stopped answering, '
            'so Macs aren’t getting work.' % at
        )
    if outage['scope'] == 'model':
        who = 'of Macs serving %s' % outage['key']
    elif outage['scope'] == 'cell':
        name, _, memory = str(outage['key']).rpartition('|')
        who = 'of %s %s GB Macs' % (name, memory)
    else:
        who = 'of Macs'
    return 'Darkbloom network problem since %s: %d%% %s stopped getting work.' % (
        at,
        round(outage['dropPct']),
        who,
    )


def after_wait(result, ended):
    """Silence the stall ladder needs after `ended` (ended_outage) before `result`'s step, when
    that silence began before the problem ended, else None."""
    start = result.get('episodeStart')
    if not ended or not isinstance(start, (int, float)) or start >= ended['end']:
        return None
    if result['step'] == 'probe':
        required = result.get('requiredSeconds')
        if not isinstance(required, (int, float)):
            required = stall_recovery.SILENCE_SECONDS
    elif result.get('trigger') == 'peers':
        required = stall_recovery.PEER_RESTART_SILENCE_SECONDS
    else:
        required = stall_recovery.RESTART_SILENCE_SECONDS
    return required


def stall_wait(result, health, now):
    """Stall ladder (stall_control.py): no restart or escape while Darkbloom's network is
    down, since neither fixes it; wait with a reason naming the problem. A single model's
    problem still lets the ladder try another model, and only holds a restart for a stall on
    that model. After a problem ends, silence is counted from its end, plus this install's
    jitter (AFTER_JITTER_SECONDS), so Macs do not all probe and restart the moment it clears."""
    step = result.get('step') if isinstance(result, dict) else None
    if step not in ('probe', 'restart', 'escape') or health is None:
        return result
    model = result.get('model')
    try:
        outage = health.outage(now, model=model)
        ended = None
        if not outage and hasattr(health, 'ended_outage'):
            ended = health.ended_outage(now, model=model)
    except Exception:
        log.exception('Network health unavailable to the stall ladder')
        return result
    if outage:
        if step == 'probe' or (outage['scope'] == 'model' and step == 'escape'):
            return result
        action = 'restarting' if step == 'restart' else 'switching models'
        return {
            **result,
            'step': None,
            'networkOutage': outage,
            'reason': describe(outage, health.signal(now, model=model))
            + ' Not %s: nothing to fix on this Mac. Waiting for Darkbloom to recover.' % action,
        }
    required = after_wait(result, ended)
    if required is None:
        return result
    due = ended['end'] + required + (getattr(health, 'jitter', 0) or 0)
    if now >= due:
        return result
    action = {
        'probe': 'sending a test request',
        'restart': 'restarting',
        'escape': 'switching models',
    }[step]
    return {
        **result,
        'step': None,
        'afterOutage': ended,
        'reason': 'The Darkbloom network problem that began at %s ended at %s. Not %s yet: '
        'waiting until %s to see whether work comes back by itself.'
        % (clock_time(ended['since']), clock_time(ended['end']), action, clock_time(due)),
    }
