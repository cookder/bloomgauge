"""What Macs like this one earn per model, from public /v1/stats counters.

Darkbloom routes each job to the fastest warm idle provider, so what a Mac earns
on a model depends mostly on its hardware class. /v1/stats lists every provider
with its chip, memory, loaded and advertised models and cumulative request and
token counters; diffing two snapshots about 5 minutes apart gives each
provider's requests per hour. Rates are grouped by cell (chip family and tier,
memory GB), model and whether the box advertises that model alone ("dedicated":
mixed gemma boxes get about half the traffic).

Provider ids stay in memory only (the previous snapshot and this Mac's own ids)
and are never written. The history keeps hourly aggregates per group, trimmed to
30 days by retention.py. Public counters give request rates and P(zero); dollars
per request come from this Mac's own recent credits when it has enough jobs on
the model, else list price x the tokens per request seen on the network.

Intervals are 90%: Student-t over providers (df = providers - 1), and a model needs
MIN_PROVIDERS providers at the level used. A ratio prices both models on one basis
(basis='list': list price x the network's token mix for both). The $/request error of
each model is calibrated from this Mac's own credits when it has OWN_MIN_JOBS jobs on
the model, else uncalibrated_error(model). For a model this Mac has not calibrated, relative()
also gives trial_low: the same bound with the model's error at USD_ERROR_TRIAL (ln 2), which a
trial excursion may use (excursions.py adds the memory, demand and once-a-week gates).
"""

import logging, math, sqlite3, statistics, threading, time
from collections import defaultdict, deque
from history import epoch

log = logging.getLogger('bloom.network_evidence')

STEP_SECONDS = 300  # /v1/stats arrives every 60 s; ~1,100 providers are diffed only every 5 min
MIN_GAP, MAX_GAP = 240, 2100  # 4-35 min between snapshots; a longer gap (sleep) is dropped
LIVE = ('online', 'serving')  # busy Macs report 'serving'; online-only drops the busiest boxes
# Providers per model at the level used (audit row 5: gap-2 found cells with 2-4 provider
# sessions too thin; 4 of 5 passing arming checks on Sep 27 rested on 2 providers).
MIN_PROVIDERS = 5
OWN_DAYS, OWN_MIN_JOBS, OWN_CACHE_SECONDS = 7, 50, 900
# 90% error of $/request, in log units. 'own': this Mac's own daily $/job moves about
# +-17%. List price x the network's token mix is calibrated per model against this Mac's
# own credits (>= OWN_MIN_JOBS jobs in OWN_DAYS): error = |ln(own $/job / list $/request)|
# + the daily 0.17. Without own jobs, per model (uncalibrated_error): Andrew's realized $/job per
# day (>= 50 jobs) against list x the network mix (the day's network prompt/request, the poll's
# completion/request per model; calibration-2026-09-28.md (b)). gemma and gpt-oss: 37 model-days,
# 90th percentile |ln| 0.74, 4 beyond x2, so ln 2. Every other model: 18 model-days over 5 models
# (Qwen3.5-9B, qwen3.5-35b, Qwen3.8, nemotron, qwen3.6-vl), median 0.57, 90th percentile 1.48,
# 5 beyond x2 and 2 beyond x4 (own completions per job ran 30-3,293 tokens on 9B), so ln 4.
USD_ERROR = {'own': 0.17}
USD_ERROR_UNCALIBRATED = math.log(2)
USD_ERROR_UNCALIBRATED_NICHE = math.log(4)
WELL_PRICED = ('gemma', 'gpt-oss')  # model families with the ln 2 uncalibrated error
# A trial excursion (Andrew, Sep 28: Macs that can hold large models should try them when demand
# is higher than usual and sustained, then learn per model) prices an uncalibrated model with the
# gemma/gpt-oss error again: a public ratio of about 2-2.5x over a calibrated home instead of ~4x.
# The trial's own ledger row, and the own-credit calibration once this Mac has OWN_MIN_JOBS jobs on
# the model, then replace this optimism (excursions.py).
USD_ERROR_TRIAL = math.log(2)
OWN_IDS_PER_QUERY = 500  # this Mac's provider ids per SQL query (SQLite variable limits)
# One-sided 95% (two-sided 90%) Student-t quantiles for df = 1..30; beyond, a Cornish-Fisher
# expansion around z (matches the exact quantile to 4 decimals from df 30).
T95 = (
    6.314, 2.920, 2.353, 2.132, 2.015, 1.943, 1.895, 1.860, 1.833, 1.812,
    1.796, 1.782, 1.771, 1.761, 1.753, 1.746, 1.740, 1.734, 1.729, 1.725,
    1.721, 1.717, 1.714, 1.711, 1.708, 1.706, 1.703, 1.701, 1.699, 1.697,
)  # fmt: skip
Z90 = 1.6448536
# One-sided 90% Student-t quantiles for df = 1..30 (lower bounds that are "90% sure": the
# excursion ledger's kill switch and the realized-pay bound of an early exit); beyond, the
# same expansion around z.
T90_ONE_SIDED = (
    3.078, 1.886, 1.638, 1.533, 1.476, 1.440, 1.415, 1.397, 1.383, 1.372,
    1.363, 1.356, 1.350, 1.345, 1.341, 1.337, 1.333, 1.330, 1.328, 1.325,
    1.323, 1.321, 1.319, 1.318, 1.316, 1.315, 1.314, 1.313, 1.311, 1.310,
)  # fmt: skip
Z80 = 1.2815516
SELF_WINDOWS = 48  # ~4 h of this Mac's own windows
# "Macs like yours" on the Pulse: this Mac's windows over the last BENCH_HOURS against its
# same-cell, same-model, same-level peers. Hidden in the UI below MIN_PROVIDERS peers or once
# the newest window is older than BENCH_FRESH_SECONDS (3 missed 5-minute windows).
BENCH_HOURS = 2
BENCH_FRESH_SECONDS = 900
BENCH_CACHE_SECONDS = 60  # the snapshot asks every second; windows close every ~5 min
NETWORK = '*'  # cell and model of the network-wide row (tokens per request, total req/h)
# Try the exact cell first, then the same chip with at least as much memory
# (memory barely changes req/h within a chip), dedicated boxes before mixed ones.
LEVELS = (('cell', True), ('neighbour', True), ('cell', False), ('neighbour', False))
COLUMNS = (
    'hour', 'cell', 'model', 'dedicated', 'providers', 'windows', 'obs', 'req_h_median',
    'req_h_mean', 'req_h_sd', 'p_zero', 'prompt_tokens', 'completion_tokens',
)  # fmt: skip


def number(value):
    ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    return value if ok and math.isfinite(value) else None


def t90(df):
    """Two-sided 90% Student-t quantile with `df` degrees of freedom (inf below 1)."""
    if df < 1:
        return math.inf
    if df <= len(T95):
        return T95[int(df) - 1]
    z = Z90
    return z + (z**3 + z) / (4 * df) + (5 * z**5 + 16 * z**3 + 3 * z) / (96 * df * df)


def t90_one_sided(df):
    """One-sided 90% Student-t quantile with `df` degrees of freedom (inf below 1)."""
    if df < 1:
        return math.inf
    if df <= len(T90_ONE_SIDED):
        return T90_ONE_SIDED[int(df) - 1]
    z = Z80
    return z + (z**3 + z) / (4 * df) + (5 * z**5 + 16 * z**3 + 3 * z) / (96 * df * df)


def uncalibrated_error(model):
    """90% $/request error (log units) of list x the network mix for a model this Mac has no
    own jobs on: ln 2 for the gemma and gpt-oss families, ln 4 for every other model."""
    name = model.lower() if isinstance(model, str) else ''
    if any(family in name for family in WELL_PRICED):
        return USD_ERROR_UNCALIBRATED
    return USD_ERROR_UNCALIBRATED_NICHE


def cell_of(family, tier, memory):
    """'M5 Pro|48'; base chips have no tier ('M4|32')."""
    memory = number(memory)
    if not isinstance(family, str) or not family or not memory or memory <= 0:
        return None
    tier = tier if isinstance(tier, str) and tier not in ('', 'Base') else ''
    return '%s|%d' % ((family + ' ' + tier).strip(), round(memory))


def hardware_cell(hardware):
    """This Mac's cell from the telemetry helper ('Apple M5 Pro', 48.0 GiB)."""
    chip = hardware.get('chip') if isinstance(hardware, dict) else None
    if not isinstance(chip, str) or not chip.startswith('Apple M'):
        return None
    family, _, tier = chip[len('Apple ') :].strip().partition(' ')
    return cell_of(family, tier, hardware.get('memoryTotalGB'))


def split_cell(cell):
    name, _, memory = cell.rpartition('|')
    return name, int(memory) if memory.isdigit() else 0


def compact(providers):
    """id -> (live, current model, advertised set, requests, completion tokens, cell, trust).

    trust: True for Darkbloom's 'hardware' trust level, False for any other, None when the
    field is missing."""
    snap = {}
    for p in providers:
        if not isinstance(p, dict) or not isinstance(p.get('id'), str):
            continue
        models = p.get('models') if isinstance(p.get('models'), list) else []
        models = tuple(sorted(m for m in models if isinstance(m, str)))
        model = p.get('current_model')
        snap[p['id']] = (
            p.get('status') in LIVE,
            model if isinstance(model, str) else '',
            models,
            number(p.get('requests_served')),
            number(p.get('tokens_generated')),
            cell_of(p.get('chip_family'), p.get('chip_tier'), p.get('memory_gb')),
            None if p.get('trust_level') is None else p.get('trust_level') == 'hardware',
        )
    return snap


def diff(prev, cur, seconds, mine):
    """(cell, model, dedicated) -> [req/h per provider, requests, tokens], and this Mac's entry
    (key, req/h, requests, trusted)."""
    groups = defaultdict(lambda: [[], 0, 0])
    own = None
    for pid, c in cur.items():
        p = prev.get(pid)
        if p is None or not (c[0] and p[0]) or not c[1] or c[1:3] != p[1:3] or c[5] != p[5]:
            continue  # missing, offline, nothing loaded, or it switched models in between
        if not c[5] or None in (c[3], c[4], p[3], p[4]) or c[3] < p[3] or c[4] < p[4]:
            continue  # unknown hardware, or a counter reset (restart)
        dreq, dtok = c[3] - p[3], c[4] - p[4]
        key = (c[5], c[1], len(c[2]) == 1)
        rate = dreq * 3600 / seconds
        if pid in mine:
            own = (key, rate, dreq, c[6] if len(c) > 6 else None)
            continue
        g = groups[key]
        g[0].append(rate)
        g[1] += dreq
        g[2] += dtok
    return groups, own


def pool(rows):
    """Combine hourly rows (several hours, maybe several cells) into one estimate."""
    rows = [r for r in rows if r['obs']]
    if not rows:
        return None
    obs = sum(r['obs'] for r in rows)
    mean = sum(r['req_h_mean'] * r['obs'] for r in rows) / obs
    squares = sum(((r['req_h_sd'] or 0) ** 2 + r['req_h_mean'] ** 2) * r['obs'] for r in rows) / obs
    hours = defaultdict(lambda: [0, 0])
    for r in rows:
        h = hours[r['hour']]
        h[0] += r['providers']
        h[1] = max(h[1], r['windows'])
    weights = [
        (r['completion_tokens'], r['req_h_mean'] * r['obs'])
        for r in rows
        if r['completion_tokens'] is not None
    ]
    requests = sum(w for _, w in weights)
    return {
        'providers': max(h[0] for h in hours.values()),
        'windows': sum(h[1] for h in hours.values()),
        'req_h': mean,
        'req_h_sd': math.sqrt(max(0.0, squares - mean * mean)),
        # Observation-weighted mean of hourly medians: a display figure, not a true median.
        'req_h_median': sum(r['req_h_median'] * r['obs'] for r in rows) / obs,
        'p_zero': sum(r['p_zero'] * r['obs'] for r in rows) / obs,
        'completion_tokens': sum(c * w for c, w in weights) / requests if requests else None,
        # Requests seen behind the estimate (each observation is one ~STEP_SECONDS window).
        'requests': sum(r['req_h_mean'] * r['obs'] for r in rows) * STEP_SECONDS / 3600,
    }


def members(rows, model, cell, scope, dedicated_only):
    name, memory = split_cell(cell)
    out = []
    for r in rows:
        if r['model'] != model or (dedicated_only and not r['dedicated']):
            continue
        if r['cell'] == cell:
            out.append(r)
        elif scope == 'neighbour':
            other, size = split_cell(r['cell'])
            if other == name and size >= memory:
                out.append(r)
    return out


class NetworkEvidence:
    def __init__(self, history, self_ids=None, hardware=None):
        """self_ids() -> this Mac's provider ids; hardware() -> telemetry (chip, memoryTotalGB)."""
        self.h = history
        self.self_ids = self_ids
        self.hardware = hardware
        self.lock = threading.RLock()
        self.prev = self.prev_t = self.prev_at = None
        self.prev_totals = (None, None, None)
        self.network_mix = None  # (prompt, completion) tokens per request over the last 24 h
        self.hour, self.acc = None, {}
        self.self_cell = None
        self.self_windows = deque(maxlen=SELF_WINDOWS)
        self.bench, self.bench_at = None, None
        self.last_window = None
        self.prices, self.fallback_price = {}, None
        self.own_cache, self.own_at = {}, None
        with history.lock:
            history.db.execute(
                'CREATE TABLE IF NOT EXISTS network_cell_rates(hour INTEGER,cell TEXT,model TEXT,'
                'dedicated INTEGER,providers INTEGER,windows INTEGER,obs INTEGER,req_h_median REAL,'
                'req_h_mean REAL,req_h_sd REAL,p_zero REAL,prompt_tokens REAL,completion_tokens REAL,'
                'PRIMARY KEY(hour,cell,model,dedicated))'
            )
            history.db.commit()

    # --- input -------------------------------------------------------------

    def on_network(self, key, data, at):
        """Network.listeners hook: every successful public fetch."""
        if key == 'stats':
            self.observe(data, at)
        elif key == 'pricing':
            self.set_pricing(data)

    def set_pricing(self, data):
        if not isinstance(data, dict) or not isinstance(data.get('prices'), list):
            return
        prices = {
            p['model']: (p['input_price'], p['output_price'])
            for p in data['prices']
            if isinstance(p, dict)
            and isinstance(p.get('model'), str)
            and number(p.get('input_price')) is not None
            and number(p.get('output_price')) is not None
        }
        fallback = tuple(
            number(data.get(k)) for k in ('fallback_input_price', 'fallback_output_price')
        )
        with self.lock:
            self.prices = prices
            self.fallback_price = None if None in fallback else fallback

    def observe(self, stats, at=None):
        """Feed each fresh /v1/stats payload; returns the window aggregate when one closes."""
        at = time.time() if at is None else at
        providers = stats.get('providers') if isinstance(stats, dict) else None
        if not isinstance(providers, list):
            return None
        try:
            t = epoch(stats['snapshot_at'])
        except (KeyError, TypeError, ValueError, AttributeError):
            t = at
        with self.lock:
            if self.prev_at is not None and 0 <= at - self.prev_at < STEP_SECONDS:
                return None
            if self.prev_t is not None and t <= self.prev_t:
                return None  # the server's cached snapshot; try again next fetch
        mine = self._mine()
        snap = compact(providers)
        totals = tuple(
            number(stats.get(k))
            for k in ('total_requests', 'total_prompt_tokens', 'total_completion_tokens')
        )
        requests = number(stats.get('last_24h_requests'))
        prompt = number(stats.get('last_24h_prompt_tokens'))
        completion = number(stats.get('last_24h_completion_tokens'))
        cell = next((snap[i][5] for i in mine if i in snap and snap[i][5]), None)
        with self.lock:
            prev, prev_t, prev_totals = self.prev, self.prev_t, self.prev_totals
            self.prev, self.prev_t, self.prev_at, self.prev_totals = snap, t, at, totals
            if requests and prompt is not None and completion is not None:
                self.network_mix = (prompt / requests, completion / requests)
            if cell:
                self.self_cell = cell
        window = None
        if prev is not None and MIN_GAP <= t - prev_t <= MAX_GAP:
            groups, own = diff(prev, snap, t - prev_t, mine)
            window = self._window(t, t - prev_t, groups, own, prev_totals, totals)
        flush = None
        with self.lock:
            hour = int(t // 3600) * 3600
            if self.hour != hour:
                if self.acc:
                    flush = (self.hour, self.acc)
                self.hour, self.acc = hour, {}
            if window is not None:
                self._accumulate(window)
                self.last_window = window
        if flush:
            self._write(*flush)
        return window

    def _mine(self):
        if self.self_ids is None:
            return frozenset()
        try:
            ids = self.self_ids()
            return frozenset(i for i in ids or () if isinstance(i, str) and i)
        except Exception:
            log.debug('Own provider ids unavailable', exc_info=True)
            return frozenset()

    def _window(self, t, seconds, groups, own, prev_totals, totals):
        summary = {}
        for key, (rates, req, tok) in groups.items():
            summary[key] = {
                'n': len(rates),
                'rates': rates,
                'req': req,
                'tok': tok,
                'median': statistics.median(rates),
                'mean': sum(rates) / len(rates),
                'p_zero': sum(1 for r in rates if r == 0) / len(rates),
                'completion_tokens': tok / req if req else None,
            }
        network = None
        if None not in prev_totals + totals:
            dreq, dprompt, dtok = (b - a for a, b in zip(prev_totals, totals))
            if dreq > 0 and dprompt >= 0 and dtok >= 0:
                network = {
                    'req_h': dreq * 3600 / seconds,
                    'req': dreq,
                    'prompt': dprompt,
                    'tok': dtok,
                }
        mine = None
        if own is not None:
            key, rate, requests, trusted = own
            peers = groups.get(key, [[]])[0]
            below = sum(1 for r in peers if r < rate) + 0.5 * sum(1 for r in peers if r == rate)
            mine = {
                'cell': key[0],
                'model': key[1],
                'dedicated': key[2],
                'req_h': rate,
                'requests': requests,
                'trusted': trusted,
                'start': t - seconds,
                'peers': len(peers),
                'percentile': below / len(peers) if peers else None,
                # Same-level peers' rates in this window (numbers only), for the benchmark
                # and the peer stall signal.
                'peer_rates': tuple(sorted(peers)),
            }
        return {
            'at': t,
            'minutes': seconds / 60,
            'providers': sum(g['n'] for g in summary.values()),
            'groups': summary,
            'network': network,
            'self': mine,
        }

    def _accumulate(self, window):
        def slot(key):
            return self.acc.setdefault(
                key, {'rates': [], 'req': 0, 'tok': 0, 'prompt': None, 'providers': 0, 'windows': 0}
            )

        for key, g in window['groups'].items():
            a = slot(key)
            a['rates'].extend(g['rates'])
            a['req'] += g['req']
            a['tok'] += g['tok']
            a['providers'] = max(a['providers'], g['n'])
            a['windows'] += 1
        net = window['network']
        if net:
            a = slot((NETWORK, NETWORK, False))
            a['rates'].append(net['req_h'])
            a['req'] += net['req']
            a['tok'] += net['tok']
            a['prompt'] = (a['prompt'] or 0) + net['prompt']
            a['providers'] = max(a['providers'], window['providers'])
            a['windows'] += 1
        s = window['self']
        if s:
            self.self_windows.append({**s, 'at': window['at']})
            self.bench_at = None  # a new window: recompute the benchmark

    @staticmethod
    def _row(hour, key, a):
        rates = a['rates']
        n = len(rates)
        return (
            hour,
            key[0],
            key[1],
            int(key[2]),
            a['providers'],
            a['windows'],
            n,
            statistics.median(rates),
            sum(rates) / n,
            statistics.pstdev(rates) if n > 1 else 0.0,
            sum(1 for r in rates if r == 0) / n,
            a['prompt'] / a['req'] if a['prompt'] is not None and a['req'] else None,
            a['tok'] / a['req'] if a['req'] else None,
        )

    def _write(self, hour, acc):
        """One write per finished hour: aggregates only, no provider ids."""
        rows = [self._row(hour, k, a) for k, a in acc.items() if a['rates']]
        try:
            with self.h.lock:
                self.h.db.executemany(
                    'INSERT OR REPLACE INTO network_cell_rates VALUES(%s)' % ','.join('?' * 13),
                    rows,
                )
                self.h.db.commit()
        except sqlite3.Error:
            log.exception('Could not save network cell rates')

    # --- output ------------------------------------------------------------

    def own_cell(self):
        with self.lock:
            if self.self_cell:
                return self.self_cell
        try:
            return hardware_cell(self.hardware()) if self.hardware else None
        except Exception:
            return None

    def _rows(self, hours, now):
        since = now - (hours + 1) * 3600  # hour buckets overlapping the last `hours`
        with self.h.lock:
            rows = [
                dict(zip(COLUMNS, r))
                for r in self.h.db.execute(
                    'SELECT %s FROM network_cell_rates WHERE hour>? AND hour<=?'
                    % ','.join(COLUMNS),
                    (since, now),
                )
            ]
        with self.lock:
            if self.hour is not None and since < self.hour <= now:
                current = [self._row(self.hour, k, a) for k, a in self.acc.items() if a['rates']]
                rows = [r for r in rows if r['hour'] != self.hour]
                rows += [dict(zip(COLUMNS, r)) for r in current]
        return rows

    def _own(self, now):
        """This Mac's trailing jobs and $/job per model (cached). Only this Mac's provider
        ids count: other Macs on the account have their own job mix."""
        if self.own_at is not None and abs(now - self.own_at) < OWN_CACHE_SECONDS:
            return self.own_cache
        out = {}
        try:
            account = self.h.cache('account') or ''
            ids = sorted(self._mine())
            with self.h.lock:
                for i in range(0, len(ids) if account else 0, OWN_IDS_PER_QUERY):
                    chunk = ids[i : i + OWN_IDS_PER_QUERY]
                    for model, jobs, micro in self.h.db.execute(
                        'SELECT model,COUNT(*),SUM(micro_usd) FROM opt_credits WHERE account=? '
                        'AND at>=? AND model!=? AND provider IN (%s) GROUP BY model'
                        % ','.join('?' * len(chunk)),
                        (account, now - OWN_DAYS * 86400, 'base_reward', *chunk),
                    ):
                        seen = out.setdefault(model, [0, 0])
                        seen[0] += jobs
                        seen[1] += micro or 0
        except sqlite3.Error:
            log.exception('Could not read own credits')
        out = {m: {'jobs': n, 'usd': micro / n / 1e6} for m, (n, micro) in out.items() if n}
        self.own_cache, self.own_at = out, now
        return out

    def _usd_per_request(self, model, completion, rows, basis, now):
        own = self._own(now).get(model) or {}
        enough = (own.get('jobs') or 0) >= OWN_MIN_JOBS and own.get('usd') is not None
        if basis != 'list' and enough:
            return own['usd'], 'own'
        with self.lock:
            price = self.prices.get(model) or self.fallback_price
            mix = self.network_mix or (None, None)
        # Public stats have no per-model prompt counters: the network's tokens per
        # request fill in (and stand in for completions on a model with no requests).
        # Every model gets the network's prompt mix, never this Mac's own prompt
        # lengths for some models only: a ratio must price both sides alike.
        net = [r for r in rows if r['cell'] == NETWORK and r['prompt_tokens'] is not None]
        weight = sum(r['req_h_mean'] * r['obs'] for r in net)
        if weight:
            mix = tuple(
                sum(r[k] * r['req_h_mean'] * r['obs'] for r in net) / weight
                for k in ('prompt_tokens', 'completion_tokens')
            )
        if completion is None:
            everywhere = pool([r for r in rows if r['model'] == model]) or {}
            completion = everywhere.get('completion_tokens')
        completion = mix[1] if completion is None else completion
        prompt = mix[0]
        if price is None or completion is None or prompt is None:
            return None, None
        return (prompt * price[0] + completion * price[1]) / 1e12, 'list'

    def _calibrated(self, model, usd, used, now):
        """True when this Mac's own credits calibrate `usd` ($/request) for `model`."""
        if used == 'own':
            return True
        own = self._own(now).get(model) or {}
        return (own.get('jobs') or 0) >= OWN_MIN_JOBS and (own.get('usd') or 0) > 0 and usd > 0

    def _usd_error(self, model, usd, used, now):
        """90% error of `usd` ($/request), in log units, calibrated per model when this Mac
        has enough own jobs on it (else uncalibrated_error)."""
        if used == 'own':
            return USD_ERROR['own']
        if self._calibrated(model, usd, used, now):
            own = self._own(now)[model]
            return abs(math.log(own['usd'] / usd)) + USD_ERROR['own']
        return uncalibrated_error(model)

    def _level(self, models, cell, rows):
        """First level at which every model has at least MIN_PROVIDERS providers."""
        if not cell:
            return None
        for scope, dedicated in LEVELS:
            pooled = [pool(members(rows, m, cell, scope, dedicated)) for m in models]
            if all(p and p['providers'] >= MIN_PROVIDERS for p in pooled):
                return scope, dedicated, pooled
        return None

    def _priced(self, model, cell, hours, level, rows, basis, now):
        out = {
            'model': model,
            'cell': cell,
            'hours': hours,
            'source': 'none',
            'dedicated_only': None,
            'providers': 0,
            'windows': 0,
            'usd_per_h': None,
            'low': None,
            'high': None,
            'req_h': None,
            'req_h_sd': None,
            'req_h_median': None,
            'p_zero': None,
            'completion_tokens': None,
            'usd_per_request': None,
            'usd_basis': None,
            'req_spread': None,
            'usd_error': None,
            'requests': 0,
            'calibrated': False,
        }
        if level is None:
            return out
        scope, dedicated, p = level
        usd, used = self._usd_per_request(model, p['completion_tokens'], rows, basis, now)
        margin = t90(p['providers'] - 1) * p['req_h_sd'] / math.sqrt(p['providers'])
        out.update(p, source=scope, dedicated_only=dedicated, usd_per_request=usd, usd_basis=used)
        # 90% relative sampling error of req/h (the $/request error is separate).
        out['req_spread'] = margin / p['req_h'] if p['req_h'] > 0 else None
        if usd is not None:
            e = out['usd_error'] = self._usd_error(model, usd, used, now)
            out['calibrated'] = self._calibrated(model, usd, used, now)
            out['usd_per_h'] = p['req_h'] * usd
            out['low'] = max(0.0, p['req_h'] - margin) * usd * math.exp(-e)
            out['high'] = (p['req_h'] + margin) * usd * math.exp(e)
        return out

    def estimate(self, model, cell=None, hours=2, now=None, basis=None):
        """Expected $/h for a Mac of `cell` (default: this Mac) on `model`, with a 90% interval.

        source: 'cell', 'neighbour' (same chip, >= memory) or 'none'.
        basis='list' prices every model at list price even when this Mac has its own $/job."""
        now = time.time() if now is None else now
        cell = cell or self.own_cell()
        rows = self._rows(hours, now)
        level = self._level([model], cell, rows)
        level = level and (level[0], level[1], level[2][0])
        return self._priced(model, cell, hours, level, rows, basis, now)

    def relative(self, model, home_model, cell=None, hours=2, now=None, basis=None):
        """$/h on `model` over $/h on `home_model` in the same cell and level, with a 90% interval.

        Predicted own $/h on model = own realized $/h on home_model x ratio. Both models are
        priced on one basis: when only one has this Mac's own $/job, both use list price.
        calibrated: this Mac's own credits calibrate `model`'s $/request (>= OWN_MIN_JOBS jobs in
        OWN_DAYS); trial_low: without that, the 90% low with `model`'s $/request error at
        USD_ERROR_TRIAL (None when calibrated)."""
        now = time.time() if now is None else now
        cell = cell or self.own_cell()
        rows = self._rows(hours, now)
        level = self._level([model, home_model], cell, rows)

        def priced(basis):
            return tuple(
                self._priced(m, cell, hours, level and level[:2] + (level[2][i],), rows, basis, now)
                for i, m in enumerate((model, home_model))
            )

        a, b = priced(basis)
        if a['usd_basis'] and b['usd_basis'] and a['usd_basis'] != b['usd_basis']:
            a, b = priced('list')
        out = {
            'model': model,
            'home': home_model,
            'cell': cell,
            'ratio': None,
            'low': None,
            'high': None,
            'source': a['source'],
            'dedicated_only': a['dedicated_only'],
            'estimate': a,
            'home_estimate': b,
            'calibrated': a['calibrated'],
            'trial_low': None,
        }
        ua, ub = a['usd_per_h'], b['usd_per_h']
        if ua is None or not ub:
            return out
        ratio = ua / ub
        if ua > 0:

            def spread(e, error=None):
                return math.hypot(e['req_spread'], e['usd_error'] if error is None else error)

            s = math.hypot(spread(a), spread(b))
            out.update(ratio=ratio, low=ratio * math.exp(-s), high=ratio * math.exp(s))
            if not a['calibrated']:
                trial = math.hypot(spread(a, min(a['usd_error'], USD_ERROR_TRIAL)), spread(b))
                out['trial_low'] = ratio * math.exp(-trial)
        else:
            out.update(ratio=0.0, low=0.0, high=a['high'] / ub)
            if not a['calibrated']:
                out['trial_low'] = 0.0
        return out

    def dedicated_table(self, models, cell=None, hours=24, now=None, basis=None):
        """Estimates on dedicated boxes of this exact cell only (no neighbour or mixed fallback)
        for each of `models` with >= MIN_PROVIDERS providers there ('models'); for the others,
        the same-chip, more-memory estimate when that level has them ('neighbours'); and the
        time of the latest window ('updatedAt', None before the first one). Each estimate also
        counts the clock hours behind it ('hours_seen'). The manager's starting home for a Mac
        without its own history (manager.network_home)."""
        now = time.time() if now is None else now
        cell = cell or self.own_cell()
        rows = self._rows(hours, now) if cell else []
        out, near = {}, {}
        for model in models if cell else ():
            for scope, found in (('cell', out), ('neighbour', near)):
                seen = members(rows, model, cell, scope, True)
                p = pool(seen)
                if p and p['providers'] >= MIN_PROVIDERS:
                    e = self._priced(model, cell, hours, (scope, True, p), rows, basis, now)
                    e['hours_seen'] = len({r['hour'] for r in seen if r['obs']})
                    found[model] = e
                    break
        with self.lock:
            updated = self.last_window['at'] if self.last_window else None
        return {
            'cell': cell,
            'hours': hours,
            'updatedAt': updated,
            'models': out,
            'neighbours': near,
        }

    def table(self, cell=None, hours=2, now=None, basis=None):
        """Every model with enough evidence for this cell, best expected $/h first (for the UI)."""
        now = time.time() if now is None else now
        cell = cell or self.own_cell()
        rows = self._rows(hours, now)
        estimates = []
        for model in sorted({r['model'] for r in rows if r['model'] != NETWORK}):
            level = self._level([model], cell, rows)
            if level:
                level = (level[0], level[1], level[2][0])
                estimates.append(self._priced(model, cell, hours, level, rows, basis, now))
        estimates.sort(key=lambda e: -(e['usd_per_h'] if e['usd_per_h'] is not None else -1))
        with self.lock:
            updated = self.last_window['at'] if self.last_window else None
        return {
            'cell': cell,
            'hours': hours,
            'updatedAt': updated,
            'models': estimates,
            'self': self.self_percentile(now=now),
        }

    def self_percentile(self, hours=4, now=None):
        """This Mac's req/h percentile among same-cell, same-model providers over recent windows.

        peer_req_h_median: the median of every peer rate in those windows (a typical Mac like
        this one); peers: the median peer count per window."""
        now = time.time() if now is None else now
        with self.lock:
            items = [x for x in self.self_windows if x['at'] > now - hours * 3600]
        if not items:
            return None
        scope = tuple(items[-1][k] for k in ('cell', 'model', 'dedicated'))
        same = [x for x in items if (x['cell'], x['model'], x['dedicated']) == scope]
        ranked = [x for x in same if x['percentile'] is not None]
        rates = [r for x in same for r in x.get('peer_rates', ())]
        return {
            'cell': scope[0],
            'model': scope[1],
            'dedicated': scope[2],
            'at': same[-1]['at'],
            'windows': len(same),
            'req_h': sum(x['req_h'] for x in same) / len(same),
            'percentile': statistics.median(x['percentile'] for x in ranked) if ranked else None,
            'peers': statistics.median(x['peers'] for x in ranked) if ranked else 0,
            'peer_req_h_median': statistics.median(rates) if rates else None,
            'peer_p_zero': sum(1 for r in rates if r == 0) / len(rates) if rates else None,
        }

    def own_windows(self, now=None, hours=1):
        """This Mac's recent windows with its peers' figures, oldest first, for the stall
        ladder's peer signal (stall_recovery.peer_stall). Numbers only, never ids."""
        now = time.time() if now is None else now
        with self.lock:
            items = [x for x in self.self_windows if now - hours * 3600 < x['at'] <= now + 60]
        out = []
        for x in items:
            rates = x.get('peer_rates', ())
            out.append(
                {
                    'at': x['at'],
                    'start': x['start'],
                    'model': x['model'],
                    'cell': x['cell'],
                    'dedicated': x['dedicated'],
                    'requests': x['requests'],
                    'trusted': x['trusted'],
                    'peers': len(rates),
                    'peer_rates': rates,
                }
            )
        return out

    def benchmark(self, now=None, hours=BENCH_HOURS):
        """The Pulse's "Macs like yours": this Mac's req/h and estimated $/h against the
        median of its same-cell, same-model, same-level peers (cached BENCH_CACHE_SECONDS).
        None until this Mac has a window; the UI hides it below MIN_PROVIDERS peers or when
        `at` is older than BENCH_FRESH_SECONDS."""
        now = time.time() if now is None else now
        with self.lock:
            if self.bench_at is not None and 0 <= now - self.bench_at < BENCH_CACHE_SECONDS:
                return self.bench
        s = self.self_percentile(hours, now)
        out = None
        if s and s['peer_req_h_median'] is not None:
            usd, basis = self._usd_per_request(s['model'], None, self._rows(hours, now), None, now)
            out = {
                'at': s['at'],
                'hours': hours,
                'cell': s['cell'],
                'model': s['model'],
                'dedicated': s['dedicated'],
                'windows': s['windows'],
                'peers': s['peers'],
                'percentile': s['percentile'],
                'reqPerHour': s['req_h'],
                'peerMedianReqPerHour': s['peer_req_h_median'],
                'peerZeroShare': s['peer_p_zero'],
                'usdPerRequest': usd,
                'usdBasis': basis,
                'usdPerHour': None if usd is None else s['req_h'] * usd,
                'peerUsdPerHour': None if usd is None else s['peer_req_h_median'] * usd,
            }
        with self.lock:
            self.bench, self.bench_at = out, now
        return out
