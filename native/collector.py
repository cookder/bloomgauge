#!/usr/bin/env python3
"""Loopback dashboard with owner-authenticated manual model control and opt-in experiments."""

from pulse_history import model_history as pulse_model_history
import argparse, collections, copy, gzip, hmac, http.cookies, json, math, os, pathlib, signal, subprocess, threading, time
import urllib.error, urllib.request
import logging
import bloom_log
import retention

log = logging.getLogger('bloom.collector')
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, unquote, parse_qs
from history import History
from network import Network
from network_evidence import NetworkEvidence
from model_catalog_watch import ModelCatalogWatch
from network_health import NetworkHealth
from opportunity_lab import OpportunityLab
from predictive_lab import PredictiveLab
from earnings_forecast_journal import ForecastJournal
from demand_curve_journal import CurveJournal
from model_combinations import selection_key
from energy import Energy
from setup import Setup
from installation import installation_id, personal_edition
from machines import Machines, local_summary
from concurrency_history import ConcurrencyHistory
from remote import Remote
from forecast import forecast, hour_start
from optimizer import Optimizer
from model_demand import model_demand
from model_insights import ModelInsights, InsightsUnavailable
from network_contributions import NetworkContributions, ContributionsUnavailable
from reputation import Reputation
from model_projection import ModelProjection
from provider_sessions import ProviderSessions
from provider_reporting import (
    ProviderReporting,
    observed_models,
    offered_not_downloaded,
    preloading,
    state_fresh,
)
from live_earnings import EarningsPulse, POLL_SECONDS, CACHE_SECONDS, credit_rows, retry_delay
from traffic_pulse import TrafficPulse
from pulse_demand import PulseDemand
from web_push import WebPush
from community_insights import CommunityInsights
from usage_integration import UsageIntegration
from feature_discovery import FeatureDiscovery
from whats_changed import WhatsChanged
from optimizer_store import device_id
from update_guard import UpdateBlocked
from support_reports import (
    SupportReports,
    SupportError,
    loads as support_loads,
    MAX_BYTES as SUPPORT_MAX_BYTES,
)
from user_contact import UserContact, ContactError
from pay_sharing import PaySharing, SharingError

ROOT = pathlib.Path(__file__).resolve().parent
APPLE_EPOCH = 978307200
GIB = 1073741824


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def read_json(path):
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError('Source file exceeds the read limit')
    return json.loads(path.read_text())


def counter_rate(previous, current, now):
    """Use the source write time, and break the line across resets/stale samples."""
    if not previous or now - current['at'] > 15 or now - previous['at'] > 20:
        return None
    elapsed = current['at'] - previous['at']
    if current['session'] != previous['session'] or not 0 < elapsed <= 20:
        return None
    delta = current['tokens'] - previous['tokens']
    return delta / elapsed if delta >= 0 else None


RUN_KEYS = (
    'at',
    'mode',
    'status',
    'detail',
    'selected',
    'blockHours',
    'controlVersion',
    'canManage',
    'busy',
    'currentModel',
    'currentModels',
    'originalModel',
    'requestedModel',
    'requestedKind',
    'controlError',
    'discoveryError',
    'nextSwitchAt',
    'warmup',
    'reporting',
    'lastSwitchResult',
)


def run_view(data):
    """The Pulse card's run chip reads a few live fields every 15 s; the full
    optimizer response is ~300 KB of per-model evidence and history."""
    out = {k: data[k] for k in RUN_KEYS if k in data}
    out.update(models=[], events=[])
    auto = data.get('demandAuto')
    if isinstance(auto, dict):
        trial = auto.get('trial') or {}
        out['demandAuto'] = {
            **{
                k: auto[k]
                for k in ('enabled', 'trial', 'paidAlternative', 'spikeReview')
                if k in auto
            },
            'runs': [
                r
                for r in auto.get('runs') or []
                if isinstance(r, dict) and r.get('id') == trial.get('runId')
            ],
        }
    return out


def next_sample_delay(now):
    """Seconds until 0.1 s into the next wall-clock second. Samples are keyed by
    int(at), so a fixed wait(1) after the work drifted and lost ~7-25% of seconds;
    aiming at the next second absorbs work time and survives clock changes."""
    return 1 - ((now - 0.1) % 1)


def monitor_snapshot(path, now):
    d = read_json(path)
    updated = path.stat().st_mtime
    return {
        '_account': d.get('accountID', ''),
        'status': 'ok' if now - updated < 180 else 'stale',
        'updatedAt': updated,
        'observedAt': d.get('lastIngestedAt', updated - APPLE_EPOCH) + APPLE_EPOCH,
        'coverageIntervals': [
            {
                'start': g['start'] + APPLE_EPOCH,
                'end': g.get('end', g['start'] + g.get('duration', 0)) + APPLE_EPOCH,
            }
            for g in d.get('coverageGaps', [])
            if 'start' in g and ('end' in g or 'duration' in g)
        ],
        'coverageStartedAt': d.get('coverageStartedAt', 0) + APPLE_EPOCH,
        'gaps': len(d.get('coverageGaps', [])),
        '_recentEarningIDs': d.get('recentEarningIDs'),
        '_categorizedEarningIDs': d.get('categorizedEarningIDs'),
        'hours': sorted(
            [
                {
                    'at': h['hour'] + APPLE_EPOCH,
                    'usd': h['microUSD'] / 1e6,
                    'jobs': h['jobs'],
                    'categories': {
                        k: v['microUSD'] / 1e6 for k, v in h.get('earningsByCategory', {}).items()
                    },
                    'categoryJobs': {
                        k: v['entries']
                        for k, v in h.get('earningsByCategory', {}).items()
                        if type(v.get('entries')) is int and v['entries'] >= 0
                    },
                }
                for h in d.get('hours', [])
            ],
            key=lambda h: h['at'],
        ),
    }


def earnings_snapshot(d, now):
    required = ['available_balance_micro_usd', 'total_micro_usd', 'count']
    if not all(finite(d.get(k)) for k in required) or not isinstance(d.get('earnings'), list):
        raise ValueError('Unexpected earnings response')
    return {
        'status': 'ok',
        'error': None,
        'updatedAt': now,
        'balance': d['available_balance_micro_usd'] / 1e6,
        'lifetime': d['total_micro_usd'] / 1e6,
        'count': d['count'],
        'entries': [
            {
                'id': x['id'],
                'model': x.get('model', 'Unknown'),
                'usd': x['amount_micro_usd'] / 1e6,
                'at': x['created_at'],
                'outputTokens': x.get('completion_tokens', 0),
            }
            for x in d['earnings']
        ],
    }


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Collector:
    def __init__(
        self,
        home=None,
        data_path=None,
        native_token='',
        usage_network_enabled=True,
        discovery_enabled=True,
        support_network_enabled=True,
        forecast_enabled=True,
    ):
        self.home = pathlib.Path(home) if home else pathlib.Path.home()
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.history = History(data_path or ':memory:')
        self.setup = Setup(self.history, self.home)
        self.installation = installation_id(self.history)
        self.machines = Machines(self)
        self.account = self.history.cache('account') or ''
        self.network = Network(self.history, self.stop)
        self.optimizer = Optimizer(self.history, self.network, self.home, self.stop)
        self.network_evidence = NetworkEvidence(
            self.history, self.network_self_ids, lambda: self.hardware
        )
        self.network.listeners.append(self.network_evidence.on_network)
        self.optimizer.network_evidence = self.network_evidence  # manager excursions
        # Network news: models joining/leaving and warm-capacity swings, from the same fetches.
        self.catalog_watch = ModelCatalogWatch(
            self.history,
            self.network_evidence,
            self.optimizer,
            notify=lambda key, title, body: self.web_push.enqueue_notice(
                self.account, key, title, body
            ),
        )
        self.network.listeners.append(self.catalog_watch.on_network)
        # "It's not you": Darkbloom-wide outages, from the same fetches (no extra polling).
        self.network_health = NetworkHealth(self.history, self.network_health_context)
        self.network.listeners.append(self.network_health.on_network)
        self.network.error_listeners.append(self.network_health.on_error)
        self.optimizer.network_health = self.network_health  # stall ladder, manager
        self.catalog_watch.health = self.network_health  # no 'left' news during an outage
        self.opportunity_lab = OpportunityLab(self.optimizer.store)
        self.predictive_lab = PredictiveLab(self.optimizer.store, enabled=personal_edition())
        self.earnings_forecast = ForecastJournal(self.optimizer.store, enabled=forecast_enabled)
        self.demand_curves = CurveJournal(self.optimizer.store, enabled=forecast_enabled)
        self.retention_enabled = forecast_enabled  # never trims a setup-preview or test history
        self.energy = Energy(self.optimizer.store)
        self.concurrency_history = ConcurrencyHistory(self.optimizer.store)
        self.sessions = ProviderSessions(self.history, self.home)
        self.model_projection = ModelProjection(self.optimizer.store)
        self.pulse = EarningsPulse(self.history, self.model_projection)
        self.pulse_demand = PulseDemand(self.optimizer.store, self.network)
        self.traffic = TrafficPulse(self.history)
        self.demand_alerts = self.optimizer.demand_auto.alerts
        self.model_insights = ModelInsights(self.optimizer.store, self.demand_identity)
        self.network_contributions = NetworkContributions(self.history, self.demand_identity)
        self.web_push = WebPush(
            pathlib.Path(data_path).parent if data_path and str(data_path) != ':memory:' else None,
            self.stop,
        )
        self.community_insights = CommunityInsights(
            pathlib.Path(data_path).parent if data_path and str(data_path) != ':memory:' else None
        )
        self.reputation = Reputation(
            self.history, self.home, native_token, self.sessions, self.reputation_identity
        )
        self.hardware = {}
        self.hardware_at = 0
        self.processes = []
        self.previous = None
        self.provider_reporting = ProviderReporting()
        self.samples = collections.deque(maxlen=2)
        self.earnings = {
            'status': 'connecting',
            'error': None,
            'updatedAt': None,
            'balance': None,
            'lifetime': None,
            'count': None,
            'entries': [],
        }
        cached = self.history.cache('earnings:' + self.account) if self.account else None
        if cached:
            self.earnings = {**cached, 'status': 'stale', 'error': 'Reconnecting to Darkbloom…'}
        self.snapshot = None
        self.forecast = None
        self.forecast_source = None
        self.forecast_inputs_key = None
        self.forecast_minutes, self.forecast_recent = [], []
        self.monitor = {
            'status': 'missing',
            'updatedAt': None,
            'coverageStartedAt': None,
            'gaps': 0,
            'hours': [],
        }
        # /api/snapshot omits monitor.hours when the client already has this revision.
        self.hours_list, self.hours_serial, self.hours_epoch = None, 0, f'{time.time():.0f}'
        cached_monitor = self.history.cache('monitor:' + self.account) if self.account else None
        if cached_monitor:
            self.monitor = {**cached_monitor, 'status': 'stale'}
        self.helper = None
        self.threads = []
        self.usage = UsageIntegration(self, data_path, network_enabled=usage_network_enabled)
        self.discovery = FeatureDiscovery(self, enabled=discovery_enabled)
        self.whats_changed = WhatsChanged(self, enabled=discovery_enabled)
        self.support_reports = SupportReports(self, network_enabled=support_network_enabled)
        self.demand_curves.hardware = self.hardware_class
        self.contact = UserContact(self.history, network_enabled=support_network_enabled)
        self.pay_sharing = PaySharing(self, network_enabled=support_network_enabled)

    def reputation_identity(self, now, raw=None):
        try:
            raw = self.optimizer.read_state() if raw is None else raw
        except (OSError, ValueError, TypeError, AttributeError):
            return None
        if not isinstance(raw, dict):
            return None
        if len(observed_models(raw.get('advertised_models'))) > 2:
            return self.optimizer.reporting_identity(raw, now, self.account)
        with self.optimizer.lock:
            if (
                self.optimizer.identity_ok
                and now - self.optimizer.identity_at < 180
                and self.optimizer.identity_session == (raw.get('started_at'), raw.get('pid'))
            ):
                return self.optimizer.identity_provider
        return None

    def network_health_context(self):
        """This Mac's hardware cell and offered models: which model or cell outages concern it."""
        with self.optimizer.lock:
            raw = self.optimizer.raw or {}
            models = observed_models(raw.get('advertised_models'))
        return self.network_evidence.own_cell(), models

    def network_self_ids(self):
        """This Mac's provider ids, to find it in /v1/stats; used in memory only."""
        ids = {self.optimizer.identity_provider}
        device = self.demand_identity()[1]
        if device:
            with self.history.lock:
                ids.update(
                    r[0]
                    for r in self.history.db.execute(
                        'SELECT provider FROM opt_identity WHERE device=?', (device,)
                    )
                )
        return ids - {None, ''}

    def start(self):
        self.network.start()
        self.optimizer.start()
        self.web_push.start()
        loops = [
            self.hardware_loop,
            self.earnings_loop,
            self.sample_loop,
            self.demand_loop,
            self.notification_loop,
            self.opportunity_loop,
            self.usage_loop,
        ]
        if self.retention_enabled:
            loops.append(lambda: retention.loop(self.history, self.stop))
        for target in loops:
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self.threads.append(thread)

    def usage_loop(self):
        while not self.stop.is_set():
            self.usage.observe()
            self.stop.wait(60)

    def hardware_class(self):
        try:
            meta = self.usage.metadata()
            return '%s|%s' % (meta['chipFamily'], meta['memoryBand'])
        except Exception:
            return None

    def demand_identity(self):
        with self.optimizer.lock:
            live = self.optimizer.live or {}
            account = live.get('account', '')
            device = live.get('device', '')
        return (account, device) if account and account == self.account else ('', '')

    def demand_loop(self):
        while not self.stop.is_set():
            try:
                now = time.time()
                account, device = self.demand_identity()
                with self.optimizer.lock:
                    live = copy.deepcopy(self.optimizer.live) or {}
                    settings = copy.deepcopy(self.optimizer.state)
                    raw = copy.deepcopy(self.optimizer.raw)
                    fresh = 0 <= now - self.optimizer.discovery_at < 600
                if account and device:
                    candidates = self.optimizer.candidates({}, {}, live, settings) if fresh else []
                    eligible = [m['id'] for m in candidates if m['available']]
                    current = (
                        raw.get('advertised_models', [])
                        if live.get('provider', {}).get('tracking', {}).get('counting')
                        else []
                    )
                    self.demand_alerts.scan(account, device, eligible, current, now)
            except Exception:
                log.exception('Demand alert scan failed')
                # A missing/changed demand feed cannot stop independent collection.
                pass
            self.stop.wait(60)

    def opportunity_loop(self):
        while not self.stop.is_set():
            try:
                now = time.time()
                account, device = self.demand_identity()
                with self.optimizer.lock:
                    live = copy.deepcopy(self.optimizer.live) or {}
                    raw = copy.deepcopy(self.optimizer.raw) or {}
                    settings = copy.deepcopy(self.optimizer.state)
                    fresh = 0 <= now - self.optimizer.discovery_at < 600
                    tracking = self.optimizer.tracking(raw, now)
                if account and device and device_id(raw) == device:
                    available = (
                        [
                            m['id']
                            for m in self.optimizer.candidates({}, {}, live, settings)
                            if m['available']
                        ]
                        if fresh
                        else []
                    )
                    self.opportunity_lab.record(
                        account,
                        device,
                        now,
                        self.network.snapshot(),
                        raw.get('advertised_models', []),
                        tracking.get('counting'),
                        settings['mode'],
                        available,
                    )
                    try:
                        self.predictive_lab.record(account, device, now, available)
                    except Exception:
                        log.exception('Predictive lab record failed')
                        pass
                    try:
                        forecast_now = time.time()
                        signals = self.demand_alerts.snapshot(account, device, forecast_now)
                        self.earnings_forecast.record(
                            account,
                            device,
                            forecast_now,
                            {m['model']: m for m in signals['models']},
                        )
                    except Exception:
                        log.exception('Earnings forecast journal failed')
                        # This independent passive journal never affects controls or the original study.
                        pass
                    try:
                        # Phase 2 shadow estimator: logged and scored, never used to switch.
                        curve_now = time.time()
                        scan = self.demand_alerts.current(account, device, curve_now)
                        self.demand_curves.record(
                            account,
                            device,
                            curve_now,
                            {m['model']: m for m in scan['models']},
                            selection_key(raw.get('advertised_models')),
                            raw.get('started_at'),
                        )
                    except Exception:
                        log.exception('Shadow estimator journal failed')
                        pass
                    try:
                        # Optional, off by default: weekly pay-curve summary for shared starting estimates.
                        self.pay_sharing.tick()
                    except Exception:
                        log.exception('Pay summary sharing failed')
                        pass
            except Exception:
                log.exception('Opportunity loop failed')
                # Passive analysis must never interrupt collection or controls.
                pass
            self.stop.wait(60)

    def notification_loop(self):
        while not self.stop.is_set():
            try:
                now = time.time()
                account, device = self.demand_identity()
                with self.optimizer.lock:
                    raw = copy.deepcopy(self.optimizer.raw) or {}
                    busy = bool(
                        self.optimizer.state.get('pending')
                        or self.optimizer.state.get('requestedModel')
                    )
                    tracking = self.optimizer.tracking(raw, now)
                if account and device and device_id(raw) == device:
                    self.optimizer.switch_alerts.observe(account, device, raw, tracking, busy, now)
                    self.optimizer.switch_alerts.send_pending(account, device, self.web_push, now)
                    self.optimizer.stall.send_pending(account, device, self.web_push, now)
            except Exception:
                log.exception('Notification loop failed')
                # Independent of telemetry, the demand scanner and control loop.
                pass
            self.stop.wait(5)

    def hardware_loop(self):
        # The helper can exit (crash, killed, sleep); without readings the optimizer
        # and stall recovery wait forever, so respawn it with a growing backoff.
        delay = 5
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                self.helper = subprocess.Popen(
                    [str(ROOT / 'telemetry')],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
                for line in self.helper.stdout:
                    if self.stop.is_set():
                        break
                    try:
                        d = json.loads(line)
                        with self.lock:
                            self.hardware = d
                            self.hardware_at = time.time()
                        try:
                            account, device = self.demand_identity()
                            self.energy.observe(account, device, d, time.time())
                        except Exception:
                            # Reporting must not interrupt the hardware feed.
                            self.energy.previous = None
                    except (ValueError, TypeError):
                        pass
                try:
                    self.helper.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.helper.kill()
                code = self.helper.returncode
            except OSError as error:
                code = str(error)
            if self.stop.is_set():
                return
            # A helper that ran for a while earns a quick restart; one that keeps failing backs off to 5 minutes.
            delay = 5 if time.monotonic() - started > 300 else min(300, delay * 2)
            log.warning('Hardware telemetry helper exited (%s); restarting in %d s', code, delay)
            self.stop.wait(delay)

    def earnings_loop(self):
        opener = urllib.request.build_opener(NoRedirect())
        failures = 0
        while not self.stop.is_set():
            delay = POLL_SECONDS
            try:
                token = (self.home / '.darkbloom/auth_token').read_text().strip()
                if not token or '\n' in token:
                    raise ValueError('Invalid token format')
                request = urllib.request.Request(
                    'https://api.darkbloom.dev/v1/provider/account-earnings?limit=1000',
                    headers={
                        'Authorization': 'Bearer ' + token,
                        'Accept': 'application/json',
                        'User-Agent': 'BloomDashboard/1.14',
                    },
                )
                requested_at = time.time()
                with opener.open(request, timeout=15) as response:
                    content = response.read(2 * 1024 * 1024 + 1)
                    if len(content) > 2 * 1024 * 1024:
                        raise ValueError('Response too large')
                    raw = json.loads(content)
                    self.accept_earnings(raw, time.time(), requested_at)
                failures = 0
            except urllib.error.HTTPError as exc:
                failures += 1
                delay = retry_delay(
                    failures,
                    exc.headers.get('Retry-After', '60' if exc.code == 429 else ''),
                    auth=exc.code in (401, 403),
                )
                self.earnings_error(
                    'Login expired. Sign in again using Darkbloom.'
                    if exc.code in (401, 403)
                    else f'Darkbloom returned HTTP {exc.code}; retrying automatically.'
                )
            except FileNotFoundError:
                failures += 1
                delay = retry_delay(failures, auth=True)
                self.earnings_error(
                    'No Darkbloom login found. Sign in using Darkbloom on this Mac.'
                )
            except (ValueError, KeyError, TypeError):
                failures += 1
                delay = retry_delay(failures)
                self.earnings_error(
                    'The earnings response could not be read; retrying automatically.'
                )
            except Exception:
                log.exception('Earnings fetch failed')
                failures += 1
                delay = retry_delay(failures)
                self.earnings_error('Could not reach Darkbloom; retrying automatically.')
            self.stop.wait(delay)

    def accept_earnings(self, raw, now, requested_at=None):
        if not isinstance(raw, dict):
            raise ValueError('Invalid earnings response')
        rows = credit_rows(raw.get('earnings'))
        d = earnings_snapshot(raw, now)
        account = raw.get('account_id', '')
        if not isinstance(account, str) or not account:
            raise ValueError('Missing earnings account')
        # The cache has no source timestamp; use a conservative TTL bound.
        requested_at = now if requested_at is None else requested_at
        as_of = min(
            now,
            max(
                requested_at - CACHE_SECONDS,
                max((r['at'] for r in rows if r['at'] <= now), default=0),
            ),
        )
        with self.lock:
            if account != self.account:
                self.optimizer.invalidate_reporting_identity()
                self.provider_reporting.reset()
                cached = self.history.cache('monitor:' + account)
                self.monitor = (
                    {**cached, 'status': 'stale'}
                    if cached
                    else {
                        'status': 'missing',
                        'updatedAt': None,
                        'coverageStartedAt': None,
                        'gaps': 0,
                        'hours': [],
                    }
                )
            new_ids = self.history.save_credits(account, raw['earnings'], as_of)
            self.optimizer.store.credits(account, raw['earnings'], as_of)
            self.pulse.ingest(account, rows, new_ids, now)
            self.history.cache('account', account)
            d.update(
                entries=d['entries'][:6],
                sourceAsOf=as_of,
                pollSeconds=POLL_SECONDS,
                revision=(self.earnings.get('revision', 0) + 1),
            )
            self.history.cache('earnings:' + account, d)
            self.account, self.earnings = account, d

    def earnings_error(self, message):
        with self.lock:
            self.earnings = {
                **self.earnings,
                'status': 'stale' if self.earnings['updatedAt'] else 'missing',
                'error': message,
            }

    def read_processes(self):
        try:
            result = subprocess.run(
                ['/bin/ps', '-axo', 'pid=,pcpu=,rss=,comm='],
                capture_output=True,
                text=True,
                timeout=3,
                check=True,
            )
            processes = []
            for line in result.stdout.splitlines():
                parts = line.strip().split(None, 3)
                if len(parts) != 4:
                    continue
                pid, cpu, rss, command = parts
                processes.append(
                    {
                        'pid': int(pid),
                        'name': os.path.basename(command),
                        'cpu': float(cpu),
                        'memoryGB': int(rss) * 1024 / GIB,
                    }
                )
            return processes
        except (OSError, ValueError, subprocess.SubprocessError):
            return []

    def sample_loop(self):
        count = 0
        while not self.stop.wait(next_sample_delay(time.time())):
            now = time.time()
            if count % 3 == 0:
                self.processes = self.read_processes()
            count += 1
            try:
                self.collect(now)
                self.discovery.observe()
                account, device = self.demand_identity()
                self.concurrency_history.observe(
                    account, device, self.reputation.snapshot(now), now
                )
            except Exception:
                log.exception('Reputation/concurrency snapshot failed')
                # Keep the last snapshot so its timestamp clearly becomes stale.
                pass

    def collect(self, now):
        # Account, base ledger, session and new-credit overlay must be published
        # together, even if an earnings response arrives during collection.
        with self.lock:
            self.collect_locked(now)

    def collect_locked(self, now):
        daemon = {}
        tracking = self.optimizer.tracking(daemon, now)
        provider = {
            'online': False,
            'starting': False,
            'active': False,
            'model': '',
            'version': '',
            'sessionTokens': None,
            'sessionJobs': None,
            'uptime': None,
            'tokensPerSecond': None,
            'memoryGB': None,
        }
        written = None
        try:
            d = read_json(self.home / '.darkbloom/daemon-state.json')
            daemon = d
            written = d.get('written_at', 0)
            # 0.9.10's startup preload refreshes the file every 30 s: starting, not stopped.
            online = state_fresh(d, now)
            stats = d.get('stats', {})
            tokens = stats.get('tokens_generated')
            tracking = self.optimizer.tracking(d, now)
            if len(observed_models(d.get('advertised_models'))) > 2:
                with self.optimizer.lock:
                    pending = bool(
                        self.optimizer.state.get('pending')
                        or self.optimizer.warmup.get('status') == 'warming'
                    )
                    roster_provider = self.optimizer.reporting_identity(d, now, self.account)
                    tracking = self.provider_reporting.observe(
                        self.account,
                        d,
                        now,
                        bool(roster_provider),
                        pending,
                        roster_provider,
                        self.optimizer.reporting_roster.routed if roster_provider else None,
                    )
            else:
                self.provider_reporting.reset()
                self.optimizer.invalidate_reporting_identity()
            provider.update(
                online=online,
                starting=online and preloading(d) and (d.get('trust') or {}).get('status') != 'online',
                active=online and bool(d.get('inference_active')),
                model=d.get('current_model', ''),
                version=d.get('version', ''),
                sessionTokens=tokens,
                sessionJobs=stats.get('requests_served'),
                uptime=max(0, now - d['started_at']) if online else None,
                memoryGB=d.get('capacity', {}).get('gpu_memory_active_gb'),
            )
            current = {
                'at': written,
                'tokens': tokens,
                'session': (
                    d.get('started_at'),
                    d.get('pid'),
                    tuple(sorted(d.get('advertised_models', []))),
                    tracking.get('verifiedAt'),
                ),
            }
            if online and tracking['counting'] and finite(tokens):
                if not self.previous or written != self.previous['at']:
                    provider['tokensPerSecond'] = counter_rate(self.previous, current, now)
                    self.previous = current
                elif self.snapshot and self.snapshot['provider']['online']:
                    provider['tokensPerSecond'] = self.snapshot['provider']['tokensPerSecond']
            else:
                self.previous = None
        except (OSError, ValueError, KeyError, TypeError):
            self.previous = None
            self.provider_reporting.reset()
            self.optimizer.invalidate_reporting_identity()
        session = self.sessions.observe(self.account, daemon, now, tracking)
        provider['session'] = session
        performance = (session or {}).get('performance') or {}
        provider['sessionJobs'] = performance.get('requests')
        provider['sessionTokens'] = performance.get('tokens')
        provider['uptime'] = performance.get('seconds')
        if not session or session['status'] != 'active':
            self.provider_reporting.reset()
            self.optimizer.invalidate_reporting_identity()
            tracking = {
                **tracking,
                'counting': False,
                'status': 'paused',
                'detail': 'Statistics paused · waiting for a verified active service.',
            }
            provider['tokensPerSecond'] = None
            self.previous = None
        provider['tracking'] = tracking
        if len(observed_models(daemon.get('advertised_models'))) > 2:
            with self.optimizer.lock:
                local, listed_at = self.optimizer.local, self.optimizer.discovery_at
            provider['multiModelReporting'] = {
                'at': now,
                'sessionId': (session or {}).get('id'),
                'models': observed_models(daemon.get('advertised_models')),
                'managedBy': 'darkbloom',
                'counting': tracking['counting'],
                'detail': tracking['detail'],
                'automationSupported': False,
                # Removed with `darkbloom models remove`, still offered until Darkbloom restarts.
                'offeredNotDownloaded': offered_not_downloaded(
                    daemon.get('advertised_models'),
                    local,
                    listed_at,
                    (session or {}).get('startedAt'),
                ),
            }
        if session and session['status'] == 'ended':
            provider.update(online=False, active=False, tokensPerSecond=None)
            self.previous = None
        try:
            monitor = monitor_snapshot(
                self.home / 'Library/Application Support/Darkbloom Monitor/activity-history.json',
                now,
            )
            monitor_account = monitor.get('_account', '')
            if self.account and self.account != monitor_account:
                monitor = {
                    'status': 'missing',
                    'updatedAt': None,
                    'coverageStartedAt': None,
                    'gaps': 0,
                    'hours': [],
                }
            elif monitor['updatedAt'] != self.monitor['updatedAt'] or monitor.get(
                '_recentEarningIDs'
            ) != self.monitor.get('_recentEarningIDs'):
                monitor = self.history.save_monitor(monitor_account, monitor)
            else:
                monitor = {**self.monitor, 'status': monitor['status']}
            self.monitor = monitor
        except (OSError, ValueError, KeyError, TypeError):
            monitor = {
                **self.monitor,
                'status': 'stale' if self.monitor['updatedAt'] else 'missing',
            }
        with self.lock:
            hardware = copy.deepcopy(self.hardware)
            gpu = {r['pid']: r.get('gpu') for r in hardware.pop('gpuProcesses', [])}
            procs = [{**p, 'gpu': gpu.get(p['pid'])} for p in self.processes]
            leaders = (
                sorted(procs, key=lambda p: p.get('gpu') or 0, reverse=True)[:20]
                + sorted(procs, key=lambda p: p['cpu'], reverse=True)[:20]
            )
            hardware['processes'] = list({p['pid']: p for p in leaders}.values())
            hw_fresh = now - self.hardware_at < 12
            if not hw_fresh:
                for key in [
                    'cpuPercent',
                    'gpuPercent',
                    'cpuTemp',
                    'gpuTemp',
                    'memoryUsedGB',
                    'memoryAvailableGB',
                    'cachedFilesGB',
                    'purgeableGB',
                    'compressedGB',
                    'swapGB',
                ]:
                    hardware[key] = None
                hardware['fanRPM'] = []
                hardware['thermal'] = 'Unavailable'
            earnings = copy.deepcopy(self.earnings)
            monitor = self.pulse.monitor_snapshot(monitor, self.account, earnings, now)
            hours = monitor.get('hours')
            if hours is not self.hours_list:
                if hours != self.hours_list:
                    self.hours_serial += 1
                self.hours_list = hours
            monitor['hoursRevision'] = f'{self.hours_epoch}.{self.hours_serial}'
            if (
                earnings['status'] != 'ok'
                and monitor.get('liveCredits', {}).get('status') == 'synced'
            ):
                monitor['liveCredits']['status'] = 'stale'
                monitor['status'] = 'stale'
            self.history.save_sample(now, provider, hardware)
            forecast_source = (
                monitor.get('revision', monitor.get('updatedAt')),
                monitor.get('status'),
                earnings.get('updatedAt'),
                earnings.get('status'),
                self.account,
                tuple(daemon.get('advertised_models', [])),
            )
            inputs_key = (*forecast_source, int(now // 15), hour_start(now))
            if inputs_key != self.forecast_inputs_key:
                self.forecast_minutes, self.forecast_recent = self.history.forecast_inputs(
                    self.account,
                    hour_start(now),
                    now,
                    monitor.get('observedAt') or monitor.get('updatedAt'),
                )
                self.forecast_inputs_key = inputs_key
            if (
                not self.forecast
                or forecast_source != self.forecast_source
                or now - self.forecast['at'] >= 1
                or self.forecast['hourStart'] != hour_start(now)
                or provider['online'] != (self.snapshot or {}).get('provider', {}).get('online')
            ):
                self.forecast = forecast(
                    now, monitor, provider, self.forecast_minutes, self.forecast_recent
                )
                self.forecast['modelProjection'] = self.model_projection.estimate(
                    self.account, daemon, provider, monitor, earnings, now
                )
                self.forecast_source = forecast_source
            self.samples.append(
                {
                    'at': now,
                    'tokensPerSecond': provider['tokensPerSecond'],
                    'cpuPercent': hardware.get('cpuPercent'),
                    'gpuPercent': hardware.get('gpuPercent'),
                }
            )
            pulse = self.pulse.snapshot(
                self.account, daemon, session, earnings, self.reputation_identity(now, daemon), now
            )
            try:
                pulse['demand'] = self.pulse_demand.snapshot(pulse.get('models') or [], now)
            except Exception:
                pulse['demand'] = None  # An overlay must never stop collection.
            try:
                # "Macs like yours": public per-cell rates (network_evidence.py), cached 60 s.
                pulse['peers'] = self.network_evidence.benchmark(now)
            except Exception:
                pulse['peers'] = None
            if provider.get('multiModelReporting'):
                pulse['reporting'] = provider['multiModelReporting']
                pulse['detail'] = tracking['detail']
                pulse['baseline'] = {
                    **pulse['baseline'],
                    'detail': 'Historical income comparisons for sets of three or more models are not supported yet. The dial uses your reference pace; live income and traffic are measured for this Mac.',
                }
            self.pulse.record_rate(self.account, pulse)
            traffic = self.traffic.observe(self.account, daemon, session, tracking, now)
            try:
                network_health = self.network_health.view(now)
            except Exception:
                log.exception('Network health view failed')
                network_health = None  # A notice must never stop collection.
            self.snapshot = {
                'at': now,
                'deviceName': self.machines.name,
                'hardware': hardware,
                'provider': provider,
                'earnings': earnings,
                'monitor': monitor,
                'forecast': self.forecast,
                'pulse': pulse,
                'traffic': traffic,
                'networkHealth': network_health,
                'samples': [s for s in self.samples if s['at'] >= now - 900],
                'sources': [
                    {
                        'name': 'Darkbloom earnings',
                        'status': earnings['status'],
                        'detail': 'Confirmed credits · 20s sync / upstream cache · 1s display',
                        'updatedAt': earnings['updatedAt'],
                    },
                    {
                        'name': monitor.get('source', 'Darkbloom Monitor'),
                        'status': monitor['status'],
                        'detail': 'Confirmed hourly credits and model breakdown',
                        'updatedAt': monitor['updatedAt'],
                    },
                    {
                        'name': 'Darkbloom provider',
                        'status': 'ok' if provider['online'] else 'stale',
                        'detail': 'Provider counters · source updates about every 3 seconds',
                        'updatedAt': written,
                    },
                    {
                        'name': 'macOS hardware',
                        'status': 'ok'
                        if hw_fresh and hardware.get('cpuTemp') is not None
                        else 'missing',
                        'detail': 'One-second samples · CPU, GPU & M5 thermal sensors',
                        'updatedAt': self.hardware_at or None,
                    },
                ],
            }
        self.optimizer.observe(self.account, daemon, self.snapshot)

    def close(self):
        self.stop.set()
        self.support_reports.close()
        self.usage.close()
        self.discovery.close()
        self.machines.close()
        if self.helper and self.helper.poll() is None:
            self.helper.terminate()
            try:
                self.helper.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.helper.kill()


class Handler(BaseHTTPRequestHandler):
    collector = None
    static_root = None
    dev = False
    remote = None
    # Per-launch secret the app window sends as an HttpOnly cookie (BLOOM_SESSION_TOKEN).
    # Empty (development, tests) means not enforced.
    session_token = ''
    NATIVE_ROUTES = ('/api/reputation/native', '/api/update/native')

    def session_ok(self):
        """Changes on the Mac listener need the app window's cookie, so other local
        programs and accounts can read the dashboard but not change anything. The
        phone listener has its own Tailscale identity check; native routes use
        X-Bloom-Native."""
        if (
            self.remote_view
            or not self.session_token
            or urlsplit(self.path).path in self.NATIVE_ROUTES
        ):
            return True
        try:
            cookies = http.cookies.SimpleCookie(self.headers.get('Cookie', ''))
        except http.cookies.CookieError:
            return False
        supplied = cookies.get('bloom_session')
        return bool(supplied) and hmac.compare_digest(
            supplied.value.encode(), self.session_token.encode()
        )

    def session_refused(self):
        log.warning(
            'Refused a change without the app session cookie: %s %s',
            self.command,
            urlsplit(self.path).path,
        )
        self.respond_json(
            {'status': 'session', 'error': 'Open Bloomkeeper on this Mac to change settings.'}, 403
        )

    remote_view = False
    setup_preview = False

    def log_message(self, *args):
        pass

    def parse_request(self):
        if not super().parse_request():
            return False
        if self.remote_view:
            # Tailscale Serve adds the phone secret to every path; a local program that
            # connects to the phone listener directly doesn't know it.
            path = self.remote.phone_path(self.path) if self.remote else None
            if path is None:
                self.send_error(404)
                return False
            self.path = path
        return True

    def permitted(self):
        if self.remote_view:
            if not self.remote or not self.remote.authorize(self.headers):
                return False
            origin = self.headers.get('Origin')
            if origin and origin != 'https://' + self.headers.get('Host', ''):
                return False
        else:
            # Serve may only target the separate, identity-gated listener.
            if any(k.lower().startswith(('tailscale-', 'x-forwarded-')) for k in self.headers):
                return False
            allowed = {
                f'127.0.0.1:{self.server.server_port}',
                f'localhost:{self.server.server_port}',
            }
            if self.dev:
                allowed.update({'127.0.0.1:5173', 'localhost:5173'})
            if self.headers.get_all('Host', []) not in [[h] for h in allowed]:
                return False
            origin = self.headers.get('Origin')
            if origin and origin not in {'http://' + h for h in allowed}:
                return False
        if self.headers.get('Sec-Fetch-Site') == 'cross-site':
            # Opening the dashboard from a message/bookmark is safe; cross-site API reads are not.
            if not (
                self.command == 'GET'
                and urlsplit(self.path).path == '/'
                and self.headers.get('Sec-Fetch-Mode') == 'navigate'
            ):
                return False
        return True

    def respond_json(self, data, status=200):
        body = json.dumps(data, allow_nan=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_body(body)

    def send_body(self, body):
        accepts = [part.strip() for part in self.headers.get('Accept-Encoding', '').split(',')]
        if len(body) > 1024 and 'gzip' in accepts:
            body = gzip.compress(body, compresslevel=3)
            self.send_header('Content-Encoding', 'gzip')
        self.send_header('Vary', 'Accept-Encoding')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if not self.session_ok():
            self.session_refused()
            return
        if urlsplit(self.path).path == '/api/support/auto':
            origins = self.headers.get_all('Origin', [])
            if (
                not self.permitted()
                or self.headers.get_all('X-Bloom-Action', []) != ['support']
                or len(origins) > 1
                or (self.remote_view and origins != ['https://' + self.headers.get('Host', '')])
            ):
                self.send_error(403)
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 64 or self.headers.get_all('Content-Type', []) != [
                    'application/json'
                ]:
                    raise ValueError('Invalid support request.')
                data = support_loads(self.rfile.read(size))
                # Like the other consents, opting in happens on the Mac; the phone may only turn it off.
                if (
                    self.remote_view
                    and isinstance(data, dict)
                    and data.get('autoSend') is not False
                ):
                    self.respond_json(
                        {'status': 'mac_only', 'error': 'Turn this on in Bloomkeeper on your Mac.'},
                        403,
                    )
                    return
                self.respond_json(self.collector.support_reports.set_auto(data))
            except (ValueError, TypeError, UnicodeError):
                self.respond_json({'error': 'Choose whether to send reports automatically.'}, 400)
            return
        if urlsplit(self.path).path in ('/api/support/preview', '/api/support/send'):
            origins = self.headers.get_all('Origin', [])
            if (
                not self.permitted()
                or self.headers.get_all('X-Bloom-Action', []) != ['support']
                or len(origins) > 1
                or (self.remote_view and origins != ['https://' + self.headers.get('Host', '')])
            ):
                self.send_error(403)
                return
            try:
                lengths = self.headers.get_all('Content-Length', [])
                if (
                    len(lengths) != 1
                    or len(lengths[0]) > 5
                    or not lengths[0].isascii()
                    or not lengths[0].isdigit()
                    or self.headers.get_all('Content-Type', []) != ['application/json']
                    or self.headers.get_all('Transfer-Encoding', [])
                ):
                    raise ValueError('Invalid support request.')
                sending = urlsplit(self.path).path == '/api/support/send'
                size = int(lengths[0])
                if not 0 < size <= (256 if sending else SUPPORT_MAX_BYTES):
                    raise ValueError('Invalid support request.')
                data = support_loads(self.rfile.read(size))
                if sending:
                    result = self.collector.support_reports.send(
                        data, self.remote_view, preview_only=self.setup_preview
                    )
                else:
                    result = self.collector.support_reports.preview(data, self.remote_view)
                self.respond_json(result)
            except SupportError as error:
                self.respond_json(error.response, error.http_status)
            except (ValueError, TypeError, UnicodeError):
                self.respond_json({'error': 'Review the report fields and try again.'}, 400)
            except Exception:
                self.respond_json(
                    {'error': 'The report is unavailable. Try again or use the support links.'}, 503
                )
            return
        if urlsplit(self.path).path == '/api/update/native':
            token = self.collector.reputation.native_token
            supplied = self.headers.get_all('X-Bloom-Native', [])
            if (
                self.remote_view
                or not self.permitted()
                or not token
                or len(supplied) != 1
                or not hmac.compare_digest(supplied[0].encode(), token.encode())
                or self.headers.get_all('X-Bloom-Action', []) != ['update']
            ):
                self.send_error(403)
                return
            try:
                lengths = self.headers.get_all('Content-Length', [])
                if len(lengths) != 1:
                    raise ValueError('Invalid request.')
                size = int(lengths[0])
                if (
                    not 0 < size <= 256
                    or self.headers.get_all('Content-Type', []) != ['application/json']
                    or self.headers.get_all('Transfer-Encoding', [])
                ):
                    raise ValueError('Invalid request.')
                result = self.collector.optimizer.update_guard.action(
                    json.loads(self.rfile.read(size))
                )
                self.respond_json(result)
            except UpdateBlocked as error:
                self.respond_json(
                    {'ready': False, 'reason': error.reason, 'detail': str(error)}, 409
                )
            except (ValueError, TypeError):
                self.respond_json({'ready': False, 'error': 'Invalid update request.'}, 400)
            except Exception:
                log.exception('Request failed: %s', urlsplit(self.path).path)
                self.respond_json(
                    {
                        'ready': False,
                        'error': 'Update readiness could not be verified. Try again shortly.',
                    },
                    503,
                )
            return
        if self.setup_preview and urlsplit(self.path).path not in (
            '/api/setup',
            '/api/energy/tariff',
            '/api/diagnostics/preview',
            '/api/machines',
            '/api/usage',
        ):
            self.send_error(403)
            return
        if urlsplit(self.path).path == '/api/network/news':
            # The "new model pays well" phone notice: on/off and a week's mute.
            if not self.permitted() or self.headers.get_all('X-Bloom-Action', []) != [
                'network-news'
            ]:
                self.send_error(403)
                return
            if self.remote_view and self.headers.get_all('Origin', []) != [
                'https://' + self.headers.get('Host', '')
            ]:
                self.send_error(403)
                return
            try:
                lengths = self.headers.get_all('Content-Length', [])
                if len(lengths) != 1:
                    raise ValueError('Invalid request.')
                size = int(lengths[0])
                if (
                    not 0 < size <= 128
                    or self.headers.get_all('Content-Type', []) != ['application/json']
                    or self.headers.get('Transfer-Encoding')
                ):
                    raise ValueError('Invalid request.')
                data = json.loads(self.rfile.read(size))
                self.respond_json(self.collector.catalog_watch.set_push(data))
            except (ValueError, TypeError):
                self.respond_json({'error': 'Choose a valid notification setting.'}, 400)
            except Exception:
                log.exception('Request failed: %s', urlsplit(self.path).path)
                self.respond_json({'error': 'Could not save this setting. Try again.'}, 503)
            return
        if urlsplit(self.path).path == '/api/whats-changed':
            if not self.permitted() or self.headers.get_all('X-Bloom-Action', []) != [
                'whats-changed'
            ]:
                self.send_error(403)
                return
            if self.remote_view and self.headers.get_all('Origin', []) != [
                'https://' + self.headers.get('Host', '')
            ]:
                self.send_error(403)
                return
            try:
                lengths = self.headers.get_all('Content-Length', [])
                if len(lengths) != 1:
                    raise ValueError('Invalid request.')
                size = int(lengths[0])
                if (
                    not 0 < size <= 256
                    or self.headers.get_all('Content-Type', []) != ['application/json']
                    or self.headers.get('Transfer-Encoding')
                ):
                    raise ValueError('Invalid request.')
                data = json.loads(self.rfile.read(size))
                self.respond_json(self.collector.whats_changed.action(data, self.setup_preview))
            except PermissionError:
                self.send_error(403)
            except (ValueError, TypeError):
                self.respond_json({'error': 'Choose a valid note.'}, 400)
            except Exception:
                log.exception('Request failed: %s', urlsplit(self.path).path)
                self.respond_json(
                    {'error': 'Could not save this choice. It stays closed on this device.'},
                    503,
                )
            return
        if urlsplit(self.path).path == '/api/discovery':
            if not self.permitted() or self.headers.get_all('X-Bloom-Action', []) != ['discovery']:
                self.send_error(403)
                return
            if self.remote_view and self.headers.get_all('Origin', []) != [
                'https://' + self.headers.get('Host', '')
            ]:
                self.send_error(403)
                return
            try:
                lengths = self.headers.get_all('Content-Length', [])
                if len(lengths) != 1:
                    raise ValueError('Invalid request.')
                size = int(lengths[0])
                if (
                    not 0 < size <= 256
                    or self.headers.get_all('Content-Type', []) != ['application/json']
                    or self.headers.get('Transfer-Encoding')
                ):
                    raise ValueError('Invalid request.')
                data = json.loads(self.rfile.read(size))
                configured = bool(self.remote and self.remote.config.get('enabled'))
                self.respond_json(
                    self.collector.discovery.action(
                        data, self.remote_view, configured, self.setup_preview
                    )
                )
            except PermissionError:
                self.send_error(403)
            except (ValueError, TypeError):
                self.respond_json({'error': 'Choose a valid feature suggestion action.'}, 400)
            except Exception:
                log.exception('Request failed: %s', urlsplit(self.path).path)
                self.respond_json(
                    {'error': 'Could not save this choice. Try again before closing Bloomkeeper.'},
                    503,
                )
            return
        if urlsplit(self.path).path == '/api/usage':
            if not self.permitted() or self.headers.get_all('X-Bloom-Action', []) != ['usage']:
                self.send_error(403)
                return
            if self.remote_view and self.headers.get_all('Origin', []) != [
                'https://' + self.headers.get('Host', '')
            ]:
                self.send_error(403)
                return
            try:
                lengths = self.headers.get_all('Content-Length', [])
                if len(lengths) != 1:
                    raise ValueError('Invalid request.')
                size = int(lengths[0])
                if (
                    not 0 < size <= 256
                    or self.headers.get_all('Content-Type', []) != ['application/json']
                    or self.headers.get('Transfer-Encoding')
                ):
                    raise ValueError('Invalid request.')
                data = json.loads(self.rfile.read(size))
                self.respond_json(self.collector.usage.action(data, self.remote_view))
            except PermissionError:
                self.send_error(403)
            except (ValueError, TypeError):
                self.respond_json(
                    {'error': 'Choose a valid usage-sharing action on this Mac.'}, 400
                )
            except Exception:
                log.exception('Request failed: %s', urlsplit(self.path).path)
                self.respond_json(
                    {
                        'error': 'Usage sharing is unavailable. Your Bloomkeeper features are unchanged.'
                    },
                    503,
                )
            return
        if urlsplit(self.path).path == '/api/pay-sharing':
            if not self.permitted() or self.headers.get_all('X-Bloom-Action', []) != [
                'pay-sharing'
            ]:
                self.send_error(403)
                return
            if self.remote_view and self.headers.get_all('Origin', []) != [
                'https://' + self.headers.get('Host', '')
            ]:
                self.send_error(403)
                return
            try:
                lengths = self.headers.get_all('Content-Length', [])
                if len(lengths) != 1:
                    raise ValueError('Invalid request.')
                size = int(lengths[0])
                if (
                    not 0 < size <= 64
                    or self.headers.get_all('Content-Type', []) != ['application/json']
                    or self.headers.get('Transfer-Encoding')
                ):
                    raise ValueError('Invalid request.')
                self.respond_json(
                    self.collector.pay_sharing.action(
                        support_loads(self.rfile.read(size)), self.remote_view
                    )
                )
            except SharingError as error:
                self.respond_json({'error': str(error)}, error.http_status)
            except (ValueError, TypeError, UnicodeError):
                self.respond_json({'error': 'Choose whether to share pay summaries.'}, 400)
            except Exception:
                log.exception('Request failed: %s', urlsplit(self.path).path)
                self.respond_json(
                    {
                        'error': 'Pay summary sharing is unavailable. Your Bloomkeeper features are unchanged.'
                    },
                    503,
                )
            return
        if urlsplit(self.path).path == '/api/contact':
            if not self.permitted() or self.headers.get_all('X-Bloom-Action', []) != ['contact']:
                self.send_error(403)
                return
            if self.remote_view and self.headers.get_all('Origin', []) != [
                'https://' + self.headers.get('Host', '')
            ]:
                self.send_error(403)
                return
            try:
                lengths = self.headers.get_all('Content-Length', [])
                if len(lengths) != 1:
                    raise ValueError('Invalid request.')
                size = int(lengths[0])
                if (
                    not 0 < size <= 1024
                    or self.headers.get_all('Content-Type', []) != ['application/json']
                    or self.headers.get('Transfer-Encoding')
                ):
                    raise ValueError('Invalid request.')
                self.respond_json(
                    self.collector.contact.action(
                        support_loads(self.rfile.read(size)), self.remote_view
                    )
                )
            except ContactError as error:
                self.respond_json({'error': str(error)}, error.http_status)
            except (ValueError, TypeError, UnicodeError) as error:
                message = (
                    str(error)
                    if str(error)
                    in (
                        'Tick the box to agree before saving.',
                        'Enter an email address or a Slack handle starting with @.',
                    )
                    else 'Choose a valid contact action.'
                )
                self.respond_json({'error': message}, 400)
            except Exception:
                log.exception('Request failed: %s', urlsplit(self.path).path)
                self.respond_json(
                    {
                        'error': 'Contact details are unavailable. Your Bloomkeeper features are unchanged.'
                    },
                    503,
                )
            return
        if urlsplit(self.path).path == '/api/machines':
            if (
                self.remote_view
                or not self.permitted()
                or self.headers.get_all('X-Bloom-Action', []) != ['machines']
            ):
                self.send_error(403)
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if (
                    not 0 < size <= 1024
                    or self.headers.get('Content-Type') != 'application/json'
                    or self.headers.get('Transfer-Encoding')
                ):
                    raise ValueError('Invalid Mac request.')
                data = json.loads(self.rfile.read(size))
                if self.setup_preview and (
                    not isinstance(data, dict) or data.get('action') != 'rename'
                ):
                    self.send_error(403)
                    return
                self.respond_json(self.collector.machines.action(data))
            except (ValueError, TypeError):
                self.respond_json(
                    {
                        'error': 'Could not save the Mac connection. Check the name and private address; both Macs need Phone access and the same Tailscale owner account. Ten Macs maximum; each Mac can be added once.'
                    },
                    400,
                )
            except Exception:
                log.exception('Request failed: %s', urlsplit(self.path).path)
                self.respond_json({'error': 'Mac connections are temporarily unavailable.'}, 503)
            return
        if urlsplit(self.path).path == '/api/diagnostics/preview':
            if not self.permitted() or self.headers.get_all('X-Bloom-Action', []) != [
                'diagnostics'
            ]:
                self.send_error(403)
                return
            if self.remote_view and self.headers.get_all('Origin', []) != [
                'https://' + self.headers.get('Host', '')
            ]:
                self.send_error(403)
                return
            try:
                from diagnostics import build_report, MAX_BYTES

                size = int(self.headers.get('Content-Length', '0'))
                if (
                    not 0 < size <= 256
                    or self.headers.get('Content-Type') != 'application/json'
                    or self.headers.get('Transfer-Encoding')
                ):
                    raise ValueError('Invalid request.')
                data = json.loads(self.rfile.read(size))
                if (
                    type(data) is not dict
                    or set(data) != {'includeEarnings'}
                    or type(data['includeEarnings']) is not bool
                ):
                    raise ValueError('Choose whether to include earnings.')
                report = build_report(self.collector, data['includeEarnings'], self.remote_view)
                if len(json.dumps(report, allow_nan=False).encode()) > MAX_BYTES:
                    raise ValueError('Report is too large.')
                self.respond_json(report)
            except (ValueError, TypeError):
                self.respond_json(
                    {'error': 'Could not capture a valid report. Refresh and try again.'}, 400
                )
            except Exception:
                log.exception('Request failed: %s', urlsplit(self.path).path)
                self.respond_json(
                    {
                        'error': 'Diagnostics are temporarily unavailable. Your provider was not changed.'
                    },
                    503,
                )
            return
        if urlsplit(self.path).path == '/api/setup':
            if (
                self.remote_view
                or not self.permitted()
                or self.headers.get_all('X-Bloom-Action', []) != ['setup']
            ):
                self.send_error(403)
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if (
                    not 0 < size <= 512
                    or self.headers.get('Content-Type') != 'application/json'
                    or self.headers.get('Transfer-Encoding')
                ):
                    raise ValueError('Invalid request.')
                result = self.collector.setup.complete(json.loads(self.rfile.read(size)))
                self.collector.usage.observe()
                self.respond_json(result)
            except (ValueError, TypeError):
                self.respond_json(
                    {'error': 'Confirm observation mode before opening the dashboard.'}, 400
                )
            return
        if urlsplit(self.path).path == '/api/energy/tariff':
            if not self.permitted() or self.headers.get_all('X-Bloom-Action', []) != [
                'energy-tariff'
            ]:
                self.send_error(403)
                return
            if self.remote_view and self.headers.get_all('Origin', []) != [
                'https://' + self.headers.get('Host', '')
            ]:
                self.send_error(403)
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if (
                    not 0 < size <= 1024
                    or self.headers.get('Content-Type') != 'application/json'
                    or self.headers.get('Transfer-Encoding')
                ):
                    raise ValueError('Invalid request.')
                self.respond_json(
                    self.collector.energy.configure(json.loads(self.rfile.read(size)))
                )
            except (ValueError, TypeError, KeyError) as e:
                self.respond_json({'error': str(e)}, 400)
            except Exception:
                log.exception('Request failed: %s', urlsplit(self.path).path)
                self.respond_json(
                    {'error': 'Could not save the electricity rate. Refresh before trying again.'},
                    503,
                )
            return
        if urlsplit(self.path).path == '/api/demand-alerts/notifications':
            if not self.permitted() or self.headers.get_all('X-Bloom-Action', []) != [
                'demand-alerts'
            ]:
                self.send_error(403)
                return
            if self.remote_view and self.headers.get_all('Origin', []) != [
                'https://' + self.headers.get('Host', '')
            ]:
                self.send_error(403)
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if (
                    not 0 < size <= 8192
                    or self.headers.get('Content-Type') != 'application/json'
                    or self.headers.get('Transfer-Encoding')
                ):
                    raise ValueError('Invalid request.')
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise ValueError('Invalid request.')
                account = self.collector.account
                if not account:
                    raise ValueError('Wait for the connected Darkbloom account.')
                if data.get('action') == 'subscribe' and set(data) == {'action', 'subscription'}:
                    result = self.collector.web_push.subscribe(account, data['subscription'])
                elif data.get('action') == 'unsubscribe' and set(data) == {'action', 'endpoint'}:
                    result = self.collector.web_push.unsubscribe(account, data['endpoint'])
                elif data.get('action') == 'test' and set(data) == {'action', 'subscriptionId'}:
                    try:
                        result = self.collector.web_push.test_notification(
                            account, data['subscriptionId']
                        )
                    except ValueError as error:
                        self.respond_json({'error': str(error)}, 400)
                        return
                elif data.get('action') == 'configure-contact' and set(data) == {
                    'action',
                    'contact',
                }:
                    # Sender settings stay on the Mac; the phone can only test
                    # its owner's registered devices under the existing guards.
                    if self.remote_view:
                        self.send_error(403)
                        return
                    try:
                        self.collector.web_push.configure_contact(data['contact'])
                    except ValueError as error:
                        self.respond_json({'error': str(error)}, 400)
                        return
                    result = self.collector.web_push.status(account)
                else:
                    raise ValueError('Unknown notification action.')
                self.respond_json(result)
            except (ValueError, TypeError, KeyError):
                self.respond_json(
                    {
                        'error': 'Could not save this notification subscription. Check permission and try again.'
                    },
                    400,
                )
            except Exception:
                log.exception('Request failed: %s', urlsplit(self.path).path)
                self.respond_json(
                    {'error': 'Notification setup is unavailable. Try again after reconnecting.'},
                    503,
                )
            return
        if urlsplit(self.path).path == '/api/model-control':
            # A narrow write route for the same authenticated phone owner. Other
            # remote mutation routes (connection settings, reputation) stay closed.
            if not self.permitted() or self.headers.get_all('X-Bloom-Action', []) != [
                'manual-model'
            ]:
                self.send_error(403)
                return
            if self.remote_view and self.headers.get_all('Origin', []) != [
                'https://' + self.headers.get('Host', '')
            ]:
                self.send_error(403)
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if (
                    len(self.headers.get_all('Content-Length', [])) != 1
                    or not 0 < size <= 2048
                    or self.headers.get_all('Content-Type', []) != ['application/json']
                    or self.headers.get('Transfer-Encoding')
                ):
                    raise ValueError('Invalid request.')
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise ValueError('Invalid request.')
                self.respond_json(
                    self.collector.optimizer.manual_action(
                        data, 'phone' if self.remote_view else 'mac'
                    )
                )
            except (ValueError, TypeError, KeyError) as e:
                self.respond_json({'error': str(e)}, 400)
            except Exception:
                log.exception('Request failed: %s', urlsplit(self.path).path)
                self.respond_json(
                    {
                        'error': 'Could not confirm the switch request. Check its live status before retrying.'
                    },
                    503,
                )
            return
        if urlsplit(self.path).path == '/api/reputation/native':
            token = self.collector.reputation.native_token
            if (
                self.remote_view
                or not self.permitted()
                or not token
                or not hmac.compare_digest(
                    self.headers.get('X-Bloom-Native', '').encode(), token.encode()
                )
            ):
                self.send_error(403)
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if (
                    not 0 < size <= 512 * 1024
                    or self.headers.get('Content-Type') != 'application/json'
                ):
                    raise ValueError()
                self.respond_json(
                    self.collector.reputation.ingest(json.loads(self.rfile.read(size)))
                )
            except (ValueError, TypeError, KeyError):
                self.respond_json({'error': 'The reputation response could not be read.'}, 400)
            return
        if urlsplit(self.path).path in ('/api/optimizer', '/api/optimizer/control'):
            if not self.permitted() or self.headers.get_all('X-Bloom-Action', []) != ['optimizer']:
                self.send_error(403)
                return
            if self.remote_view and self.headers.get_all('Origin', []) != [
                'https://' + self.headers.get('Host', '')
            ]:
                self.send_error(403)
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if (
                    not 0 < size <= 4096
                    or self.headers.get('Content-Type') != 'application/json'
                    or self.headers.get('Transfer-Encoding')
                ):
                    raise ValueError('Invalid request.')
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise ValueError('Invalid request.')
                light = urlsplit(self.path).path == '/api/optimizer/control'
                result = (
                    self.collector.optimizer.automatic_control.action
                    if light
                    else self.collector.optimizer.control_action
                )(data, 'phone' if self.remote_view else 'mac')
                if not light:
                    self.collector.usage.observe()
                result['remote'] = self.remote_view
                self.respond_json(result)
            except (ValueError, TypeError, KeyError) as e:
                self.respond_json({'error': str(e)}, 400)
            except Exception:
                log.exception('Request failed: %s', urlsplit(self.path).path)
                self.respond_json(
                    {
                        'error': 'Could not confirm the optimizer change. Check its live status before retrying.'
                    },
                    503,
                )
            return
        if urlsplit(self.path).path != '/api/remote':
            self.send_error(501)
            return
        if (
            self.remote_view
            or not self.permitted()
            or self.headers.get('X-Bloom-Action') != 'remote-access'
        ):
            self.send_error(403)
            return
        if not self.remote:
            self.send_error(503)
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 512 or self.headers.get('Content-Type') != 'application/json':
                raise ValueError('Invalid request.')
            action = json.loads(self.rfile.read(size)).get('action')
            if action not in ('enable', 'disable'):
                raise ValueError('Unknown action.')
            was_configured = bool(self.remote.config.get('enabled'))
            result = self.remote.action(action)
            if was_configured or self.remote.config.get('enabled'):
                # Remember successful configuration even if the user turns it
                # off before returning to the overview. This never changes access.
                try:
                    self.collector.discovery.status(
                        phone_configured=True, preview=self.setup_preview
                    )
                except Exception:
                    pass
            self.respond_json(result)
        except (ValueError, TypeError, AttributeError) as e:
            self.respond_json({'error': str(e)}, 400)
        except OSError:
            self.respond_json(
                {'error': 'Could not save phone-access settings. Try again on your Mac.'}, 503
            )

    def do_GET(self):
        if not self.permitted():
            self.send_error(403)
            return
        path = urlsplit(self.path).path
        if path == '/api/support/auto':
            self.respond_json(self.collector.support_reports.auto_status())
            return
        if path == '/api/network/contributions':
            try:
                self.respond_json(
                    self.collector.network_contributions.get(urlsplit(self.path).query)
                )
            except ValueError as error:
                self.respond_json({'error': str(error)}, 400)
            except ContributionsUnavailable as error:
                self.respond_json({'error': str(error)}, 503)
            except Exception:
                log.exception('Request failed: %s', urlsplit(self.path).path)
                self.respond_json(
                    {'error': 'Network reporting is unavailable. Model controls remain available.'},
                    503,
                )
            return
        if path == '/api/optimizer/live':
            if urlsplit(self.path).query:
                self.respond_json({'error': 'Optimizer live accepts no query parameters.'}, 400)
                return
            try:
                self.respond_json(
                    self.collector.optimizer.live_projection.snapshot(self.collector.account)
                )
            except Exception:
                log.exception('Request failed: %s', urlsplit(self.path).path)
                self.respond_json(
                    {'error': 'Optimizer display is unavailable. Model controls remain available.'},
                    503,
                )
            return
        if path == '/api/model-insights':
            try:
                self.respond_json(self.collector.model_insights.get(urlsplit(self.path).query))
            except ValueError as error:
                self.respond_json({'error': str(error)}, 400)
            except InsightsUnavailable as error:
                self.respond_json({'error': str(error)}, 503)
            except Exception:
                log.exception('Request failed: %s', urlsplit(self.path).path)
                self.respond_json(
                    {'error': 'Model statistics are unavailable. Switching remains available.'}, 503
                )
            return
        if path == '/api/release-notes':
            from release_notes import snapshot as release_notes_snapshot

            self.respond_json(release_notes_snapshot())
            return
        if path == '/api/optimizer/control':
            self.respond_json(self.collector.optimizer.automatic_control.snapshot())
            return
        if path == '/api/whats-changed':
            self.respond_json(self.collector.whats_changed.status(self.setup_preview))
            return
        if path == '/api/discovery':
            configured = bool(self.remote and self.remote.config.get('enabled'))
            self.respond_json(
                self.collector.discovery.status(self.remote_view, configured, self.setup_preview)
            )
            return
        if path == '/api/usage':
            self.respond_json(self.collector.usage.status(self.remote_view))
            return
        if path == '/api/pay-sharing':
            self.respond_json(self.collector.pay_sharing.status(self.remote_view))
            return
        if path == '/api/contact':
            if not self.session_ok():
                self.session_refused()
                return
            self.respond_json(self.collector.contact.status(self.remote_view))
            return
        if path in ('/api/machines', '/api/machines/summary'):
            try:
                q = parse_qs(urlsplit(self.path).query)
                if set(q) - {'hours'} or len(q.get('hours', [])) > 1:
                    raise ValueError('Invalid range.')
                hours = int(q.get('hours', ['24'])[0])
                data = (
                    local_summary(self.collector, hours)
                    if path.endswith('/summary')
                    else self.collector.machines.snapshot(hours, self.remote_view)
                )
                self.respond_json(data)
            except (ValueError, TypeError):
                self.respond_json({'error': 'Choose 1 hour, 24 hours or 7 days.'}, 400)
            except Exception:
                log.exception('Request failed: %s', urlsplit(self.path).path)
                self.respond_json({'error': 'Mac summaries are temporarily unavailable.'}, 503)
            return
        if path == '/api/setup':
            self.respond_json(self.collector.setup.status(self.collector, self.remote_view))
            return
        if path in ('/api/energy', '/api/concurrency-history'):
            try:
                q = parse_qs(urlsplit(self.path).query)
                now = time.time()
                start = float(q.get('from', [str(now - 3600)])[0])
                end = float(q.get('to', [str(now)])[0])
                account, device = self.collector.demand_identity()
                if path == '/api/energy':
                    data = self.collector.energy.report(account, device, start, end, now)
                else:
                    data = self.collector.concurrency_history.report(
                        account,
                        device,
                        start,
                        end,
                        now,
                        int(q['session'][0]) if q.get('session') else None,
                        q.get('model', [None])[0],
                    )
                self.respond_json(data)
            except (ValueError, TypeError, OverflowError):
                self.send_error(400)
            return
        if path == '/api/community-insights':
            self.respond_json(self.collector.community_insights.snapshot())
            return
        if path == '/api/demand-alerts':
            account, device = self.collector.demand_identity()
            self.respond_json(self.collector.demand_alerts.snapshot(account, device, time.time()))
            return
        if path == '/api/demand-alerts/notifications':
            self.respond_json(self.collector.web_push.status(self.collector.account))
            return
        if path == '/api/traffic-history':
            try:
                q = parse_qs(urlsplit(self.path).query)
                now = time.time()
                start = float(q.get('from', [str(now - 3600)])[0])
                end = float(q.get('to', [str(now)])[0])
                metric = q.get('metric', ['tokens'])[0]
                if (
                    not finite(start)
                    or not finite(end)
                    or start < 0
                    or end <= start
                    or metric not in ('tokens', 'requests')
                ):
                    raise ValueError()
                # Read the session under the collector lock, then query without it:
                # the one-second sampler needs that lock.
                with self.collector.lock:
                    traffic = (self.collector.snapshot or {}).get('traffic') or {}
                    session = traffic.get('sessionId')
                    account = self.collector.account
                if q.get('session', [str(session)])[0] != str(session):
                    raise ValueError()
                scope = q.get('scope', ['session'])[0]
                if scope not in ('session', 'models'):
                    raise ValueError()
                if scope == 'models':
                    data = pulse_model_history(
                        self.collector.history,
                        account,
                        session,
                        start,
                        end,
                        'traffic_intervals',
                        lambda sid, step: self.collector.traffic.rate_history(
                            account, sid, start, end, metric, bucket_seconds=step
                        ),
                    )
                else:
                    data = self.collector.traffic.rate_history(account, session, start, end, metric)
                self.respond_json(data)
            except (ValueError, TypeError, OverflowError):
                self.send_error(400)
            return
        if path == '/api/pulse-history':
            try:
                q = parse_qs(urlsplit(self.path).query)
                now = time.time()
                start = float(q.get('from', [str(now - 300)])[0])
                end = float(q.get('to', [str(now)])[0])
                if not finite(start) or not finite(end) or start < 0 or end <= start:
                    raise ValueError()
                with self.collector.lock:
                    pulse = (self.collector.snapshot or {}).get('pulse') or {}
                    session = pulse.get('sessionId')
                    account = self.collector.account
                if q.get('session', [str(session)])[0] != str(session):
                    raise ValueError()
                scope = q.get('scope', ['session'])[0]
                if scope not in ('session', 'models'):
                    raise ValueError()
                if scope == 'models':
                    data = pulse_model_history(
                        self.collector.history,
                        account,
                        session,
                        start,
                        end,
                        'pulse_rates',
                        lambda sid, step: self.collector.pulse.rate_history(
                            account, sid, start, end, bucket_seconds=step
                        ),
                    )
                else:
                    data = self.collector.pulse.rate_history(account, session, start, end)
                self.respond_json(data)
            except (ValueError, TypeError, OverflowError):
                self.send_error(400)
            return
        if path == '/api/sessions':
            self.respond_json(self.collector.sessions.snapshot())
            return
        if path == '/api/model-control':
            self.respond_json(self.collector.optimizer.manual_snapshot(remote=self.remote_view))
            return
        if path == '/api/reputation':
            data = self.collector.reputation.snapshot()
            if self.remote_view:
                data['nativeAvailable'] = False
            self.respond_json(data)
            return
        if path in ('/api/opportunities', '/api/network/model-detail', '/api/predictive-lab'):
            try:
                q = parse_qs(urlsplit(self.path).query)
                now = time.time()
                start = float(q.get('from', [str(now - 3600)])[0])
                end = float(q.get('to', [str(now)])[0])
                account, device = self.collector.demand_identity()
                if path == '/api/opportunities':
                    data = self.collector.opportunity_lab.report(account, device, start, end, now)
                elif path == '/api/predictive-lab':
                    from demand_optimizer import policy
                    from demand_targets import chosen_goal

                    try:
                        with self.collector.optimizer.lock:
                            goal = chosen_goal(
                                policy(self.collector.optimizer.state.get('demandPolicy'))
                            )
                    except ValueError:
                        goal = None
                    data = self.collector.predictive_lab.report(
                        account, device, start, end, now, goal
                    )
                else:
                    from demand_detail import detail

                    data = detail(
                        self.collector.optimizer.store,
                        account,
                        device,
                        q.get('model', [''])[0],
                        start,
                        end,
                        now,
                    )
                self.respond_json(data)
            except (ValueError, TypeError, OverflowError):
                self.send_error(400)
            return
        if path == '/api/network/weekly':
            try:
                from network_weekly import report

                q = parse_qs(urlsplit(self.path).query)
                now = time.time()
                start = float(q.get('from', [str(now - 2592000)])[0])
                end = float(q.get('to', [str(now)])[0])
                data = report(
                    self.collector.history, start, end, now, q.get('timezone', ['UTC'])[0]
                )
            except (ValueError, TypeError, OverflowError):
                self.send_error(400)
                return
            self.respond_json(data)
            return
        if path == '/api/network/news':
            try:
                self.respond_json(self.collector.catalog_watch.view())
            except Exception:
                log.exception('Request failed: %s', path)
                self.respond_json({'error': 'Network news is unavailable right now.'}, 503)
            return
        if path == '/api/network/models':
            try:
                q = parse_qs(urlsplit(self.path).query)
                now = time.time()
                start = float(q.get('from', [str(now - 3600)])[0])
                end = float(q.get('to', [str(now)])[0])
                data = model_demand(self.collector.history, start, end, now)
            except (ValueError, TypeError, OverflowError):
                self.send_error(400)
                return
            self.respond_json(data)
            return
        if path in (
            '/api/optimizer',
            '/api/optimizer/history',
            '/api/optimizer/baselines',
            '/api/model-history',
            '/api/workload',
            '/api/earnings-target',
            '/api/earnings-daily',
            '/api/optimizer/earnings-outlook',
            '/api/optimizer/shadow-estimator',
        ):
            try:
                q = parse_qs(urlsplit(self.path).query)
                now = time.time()
                start = float(q.get('from', [str(now - 604800)])[0])
                end = float(q.get('to', [str(now)])[0])
                if not finite(start) or not finite(end) or start < 0 or end <= start:
                    raise ValueError()
                if path == '/api/earnings-daily':
                    from daily_earnings import report

                    with self.collector.optimizer.lock:
                        live = copy.deepcopy(self.collector.optimizer.live) or {}
                    account, device = live.get('account', ''), live.get('device', '')
                    if account != self.collector.account:
                        account, device = '', ''
                    data = report(
                        self.collector.optimizer.store,
                        account,
                        device,
                        start,
                        end,
                        now,
                        q.get('timezone', ['UTC'])[0],
                        q.get('model', [None])[0],
                    )
                elif path == '/api/earnings-target':
                    from earnings_target import report
                    from demand_optimizer import policy
                    from demand_targets import chosen_goal

                    with self.collector.optimizer.lock:
                        live = copy.deepcopy(self.collector.optimizer.live) or {}
                        rules = policy(self.collector.optimizer.state.get('demandPolicy'))
                    account, device = live.get('account', ''), live.get('device', '')
                    if account != self.collector.account:
                        account, device = '', ''
                    data = report(
                        self.collector.optimizer.store,
                        account,
                        device,
                        start,
                        end,
                        now,
                        chosen_goal(rules),
                        q.get('model', [None])[0],
                    )
                elif path == '/api/optimizer/earnings-outlook':
                    from earnings_outlook import report

                    with self.collector.optimizer.lock:
                        live = copy.deepcopy(self.collector.optimizer.live) or {}
                    account, device = live.get('account', ''), live.get('device', '')
                    if account != self.collector.account:
                        account, device = '', ''
                    signals = self.collector.optimizer.demand_auto.alerts.snapshot(
                        account, device, now
                    )
                    data = report(
                        self.collector.optimizer.store,
                        account,
                        device,
                        start,
                        end,
                        now,
                        {r['model']: r for r in signals['models']},
                    )
                elif path == '/api/optimizer/shadow-estimator':
                    with self.collector.optimizer.lock:
                        live = copy.deepcopy(self.collector.optimizer.live) or {}
                    account, device = live.get('account', ''), live.get('device', '')
                    if account != self.collector.account:
                        account, device = '', ''
                    journal = self.collector.demand_curves
                    data = {
                        'latest': journal.latest(account, device, now),
                        'evaluation': journal.evaluation(account, device, now),
                    }
                elif path == '/api/optimizer/baselines':
                    from demand_baselines import report

                    model = q.get('model', [None])[0]
                    if model is not None and (not model or len(model) > 512):
                        raise ValueError()
                    with self.collector.optimizer.lock:
                        live = copy.deepcopy(self.collector.optimizer.live) or {}
                    account, device = live.get('account', ''), live.get('device', '')
                    signals = self.collector.optimizer.demand_auto.alerts.snapshot(
                        account, device, now
                    )
                    data = report(
                        self.collector.optimizer.store,
                        account,
                        device,
                        start,
                        end,
                        now,
                        {r['model']: r for r in signals['models']},
                        model,
                    )
                elif path in ('/api/model-history', '/api/workload'):
                    from model_history import model_history

                    model = q.get('model', [None])[0]
                    if model is not None and (not model or len(model) > 512):
                        raise ValueError()
                    with self.collector.optimizer.lock:
                        live = copy.deepcopy(self.collector.optimizer.live) or {}
                        raw = copy.deepcopy(self.collector.optimizer.raw)
                        mode = self.collector.optimizer.state['mode']
                    from workload import workload

                    report = workload if path == '/api/workload' else model_history
                    options = {}
                    if path == '/api/model-history':
                        scan = self.collector.optimizer.demand_auto.alerts.snapshot(
                            live.get('account', ''), live.get('device', ''), now
                        )
                        options['signals'] = {r['model']: r for r in scan['models']}
                    data = report(
                        self.collector.optimizer.store,
                        live.get('account', ''),
                        live.get('device', ''),
                        start,
                        end,
                        now,
                        model,
                        **options,
                    )
                    if path == '/api/workload':
                        from model_pricing import enrich_workload

                        enrich_workload(
                            data, self.collector.network.snapshot('pricing') or None, now
                        )
                    data['tracking'] = self.collector.optimizer.tracking(raw, now)
                    data['switchingMode'] = mode
                elif path.endswith('/history'):
                    model = q.get('model', [''])[0]
                    if not model or len(model) > 512:
                        raise ValueError()
                    with self.collector.optimizer.lock:
                        live = copy.deepcopy(self.collector.optimizer.live) or {}
                    data = self.collector.optimizer.store.chart(
                        live.get('account', ''), live.get('device', ''), model, start, end, now
                    )
                else:
                    data = self.collector.optimizer.snapshot(start, end, self.remote_view)
                    if q.get('view') == ['run']:
                        data = run_view(data)
                self.respond_json(data)
            except (ValueError, TypeError):
                self.send_error(400)
            return
        if path == '/api/remote':
            self.respond_json(
                self.remote.snapshot(self.remote_view)
                if self.remote
                else {'status': 'unavailable', 'canManage': False}
            )
            return
        if path == '/api/remote/qr.png':
            url = self.remote.snapshot().get('url') if self.remote else None
            if not url:
                self.send_error(404)
                return
            try:
                png = subprocess.run(
                    [str(ROOT / 'qr'), url], capture_output=True, timeout=5, check=True
                ).stdout
                self.send_response(200)
                self.send_header('Content-Type', 'image/png')
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(png)
            except (OSError, subprocess.SubprocessError):
                self.send_error(503)
            return
        if path in ('/api/history', '/api/credits', '/api/network'):
            try:
                q = parse_qs(urlsplit(self.path).query)
                start = float(q.get('from', ['0'])[0])
                end = float(q.get('to', [str(time.time())])[0])
                if not finite(start) or not finite(end) or start < 0 or end <= start:
                    raise ValueError()
                if path == '/api/history':
                    kind = q.get('kind', ['hardware'])[0]
                    data = (
                        self.collector.history.hourly_output(start, end)
                        if kind == 'output'
                        else self.collector.history.chart(start, end, kind == 'network')
                    )
                elif path == '/api/credits':
                    if set(q) - {'from', 'to', 'page', 'limit', 'sort', 'category', 'model'} or any(
                        len(v) != 1 for v in q.values()
                    ):
                        raise ValueError()
                    page = max(1, min(100000, int(q.get('page', ['1'])[0])))
                    limit = max(1, min(250, int(q.get('limit', ['100'])[0])))
                    data = self.collector.history.credits(
                        self.collector.account,
                        start,
                        end,
                        page,
                        limit,
                        q.get('sort', ['newest'])[0],
                        q.get('category', ['all'])[0],
                        q.get('model', [None])[0],
                    )
                else:
                    data = self.collector.network.snapshot()
            except (ValueError, TypeError):
                self.send_error(400)
                return
            self.respond_json(data)
            return
        if path in ('/api/snapshot', '/api/health'):
            with self.collector.lock:
                data = (
                    {'service': 'bloom-dashboard', 'ready': self.collector.snapshot is not None}
                    if path.endswith('health')
                    else self.collector.snapshot
                )
            known = parse_qs(urlsplit(self.path).query).get('hours', [None])[0]
            if (
                data
                and path == '/api/snapshot'
                and known
                and data['monitor'].get('hoursRevision') == known
            ):
                data = {
                    **data,
                    'monitor': {k: v for k, v in data['monitor'].items() if k != 'hours'},
                }
            self.respond_json(data, 200 if data else 503)
            return
        candidate = (self.static_root / unquote(path).lstrip('/')).resolve()
        if candidate == self.static_root:
            candidate = candidate / 'index.html'
        if not candidate.is_relative_to(self.static_root) or not candidate.is_file():
            self.send_error(404)
            return
        types = {
            '.html': 'text/html; charset=utf-8',
            '.js': 'text/javascript',
            '.css': 'text/css',
            '.svg': 'image/svg+xml',
            '.png': 'image/png',
            '.woff2': 'font/woff2',
            '.webmanifest': 'application/manifest+json',
        }
        self.send_response(200)
        self.send_header('Content-Type', types.get(candidate.suffix, 'application/octet-stream'))
        self.send_header(
            'Content-Security-Policy',
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
        )
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'no-cache')
        self.send_body(candidate.read_bytes())


def parse_args(argv=None):
    args = argparse.ArgumentParser()
    args.add_argument('--port', type=int, default=8765)
    args.add_argument('--remote-port', type=int, default=8766)
    args.add_argument('--static', type=pathlib.Path, default=ROOT.parent / 'dist/local')
    args.add_argument('--dev', action='store_true')
    args.add_argument(
        '--data',
        type=pathlib.Path,
        default=pathlib.Path.home() / 'Library/Application Support/Bloom Dashboard/history.sqlite3',
    )
    args.add_argument(
        '--setup-preview',
        action='store_true',
        help='Isolated first-run preview: no account, provider, network or optimizer workers.',
    )
    return args.parse_args(argv)


def main():
    args = parse_args()
    bloom_log.setup(args.data)
    collector = Collector(
        home=args.data.parent / 'empty-home' if args.setup_preview else None,
        data_path=args.data,
        native_token=os.environ.pop('BLOOM_NATIVE_TOKEN', ''),
        usage_network_enabled=not args.setup_preview,
        discovery_enabled=not args.setup_preview,
        support_network_enabled=not args.setup_preview,
        forecast_enabled=not args.setup_preview,
    )
    Handler.collector = collector
    Handler.static_root = args.static.resolve()
    Handler.dev = args.dev
    Handler.setup_preview = args.setup_preview
    Handler.session_token = os.environ.pop('BLOOM_SESSION_TOKEN', '')
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.daemon_threads = True
    remote = Remote(args.data.parent / 'remote-access.json', args.remote_port)
    Handler.remote = remote

    class PhoneHandler(Handler):
        remote_view = True

    phone = ThreadingHTTPServer(('127.0.0.1', args.remote_port), PhoneHandler)
    phone.daemon_threads = True
    threading.Thread(target=phone.serve_forever, daemon=True).start()
    if not args.setup_preview:
        remote.start()

    def quit_handler(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, quit_handler)
    signal.signal(signal.SIGINT, quit_handler)
    if args.setup_preview:
        collector.collect(time.time())
    else:
        collector.start()
    print(json.dumps({'url': f'http://127.0.0.1:{server.server_port}'}), flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        remote.stop.set()
        phone.shutdown()
        phone.server_close()
        collector.close()
        server.server_close()
        collector.history.db.commit()
        # A switch worker may be blocked in a model warm-up for up to 3 minutes, and the
        # app gives up on Quit after 30 s. Darkbloom's own commands finish without us,
        # and the next launch pauses automation after an interrupted switch, so stop here.
        worker = getattr(collector.optimizer, 'worker', None)
        if worker is not None and worker.is_alive() is True:
            worker.join(20)
            if worker.is_alive() is True:
                log.warning(
                    'Quitting during a model switch; the next launch will pause automatic switching.'
                )
                with collector.history.lock:
                    collector.history.db.commit()
                logging.shutdown()
                os._exit(0)


if __name__ == '__main__':
    main()
