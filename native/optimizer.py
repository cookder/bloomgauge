"""Local, opt-in model experiments and evidence-based provider switching."""

import copy, hashlib, json, math, os, pathlib, plistlib, re, ssl, subprocess, threading, time, urllib.error, uuid
from xml.parsers.expat import ExpatError
from optimizer_store import OptimizerStore, device_id, CONTINUITY_SECONDS
from decision_journal import DecisionJournal, reason_code
from switch_alerts import SwitchAlerts, switch_reason
from model_readiness import readiness, session_key
from provider_sessions import matching_process, process_identity
from provider_reporting import observed_models, reporting_scope, ReportingIdentity
from prewarm import prewarm, local_request, WarmupError, WarmupDeferred
from cache_recovery import file_cache_blocked, clear_file_cache, CacheRecoveryError
from model_combinations import (
    members,
    selection_key,
    same_selection,
    selection_label,
    pair_budget,
    pair_candidates,
    combination_config_error,
    configured_reserve_gb,
    DEFAULT_RESERVE_GB,
)
from demand_targets import high_earnings, fresh_paid
from demand_optimizer import (
    DemandOptimizer,
    policy as demand_policy,
    repaired_policy,
    POLICY_REVISION,
)
import data_gathering
import manager
import network_health
import logging
import bloom_log  # noqa: F401  (quiet until the app configures logging)

log = logging.getLogger('bloom.optimizer')
from update_guard import UpdateGuard
from serving_trust import daemon_authorized, daemon_verifying, roster_authorized
from provider_control import (
    DRAINED,
    ENDPOINT_SETUP,
    ProviderControl,
    endpoint_fix,
    endpoint_issue,
    multi_model_notice,
)
from manual_selection import ManualSelection
from optimizer_control import OptimizerControl
from stall_control import StallControl
from optimizer_live import OptimizerLive
from manager import ManagerControl
from demand_confirmation import (
    advance as advance_confirmation,
    pause as pause_confirmation,
    scope as confirmation_scope,
    view as confirmation_view,
)

MODES = ('observe', 'week', 'optimize', 'combo', 'demand')
VALUE_FLAGS = {'--config', '-c', '--coordinator-url', '--idle-timeout', '--port', '--bind'}
BOOL_FLAGS = {'--local-endpoint', '--no-auth'}
MANUAL_IDLE_TIMEOUT = 300
# `darkbloom start` returns in seconds on older providers. From 0.9.9 it first
# drains: new work is refused and accepted requests finish (600 s default
# deadline), then it restarts. Killing it mid-drain leaves the provider drained
# and serving nothing, so give it the whole deadline.
START_SECONDS = 60
DRAIN_SECONDS = 600
DRAIN_VERSION = (0, 9, 9)
# A failed switch can still leave the provider drained (for example a request
# that outlasts the deadline). Restart it once it has sat drained this long.
DRAINED_RESTORE_SECONDS = 120
DRAINED_RESTORE_WINDOW = 6 * 3600
SWITCH_PURGE_GAP = 120
# After a switch purge still left a model short, stop counting file cache for it.
PURGE_SHORTFALL_HOLD = 3600
# Planner statistics go stale as history grows; see OptimizerStore.refresh_statistics.
STATISTICS_SECONDS = 6 * 3600
# Catalog models with runtime requirements (apple_m5, mlx_nax): see Optimizer.runtime_proof.
RUNTIME_UNVERIFIED = 'Runtime support is not yet verified for automatic selection.'
RUNTIME_PROOF_MINUTES = 10  # complete verified ready minutes served alone on this Mac
RUNTIME_PROOF_DAYS = 30
RUNTIME_PROOF_CACHE_SECONDS = 600
RUNTIME_PROOFS_KEY = 'runtime-proofs-v1'  # history cache: {device: {model: lastProvenAt}}


class ExternalChange(Exception):
    pass


class NothingSent(ExternalChange):
    """A change seen before a warm-up sent anything (a failed launchctl or settings read)."""


class WorkResumed(Exception):
    """A busy final recheck leaves a deliberate selection queued."""


class DemandDeferred(Exception):
    """An economic or resource change invalidated an automatic opportunity."""


def finite(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def activity_counters(raw):
    """Only work counters indicate activity; diagnostic counters may change idle."""
    stats = raw.get('stats') if isinstance(raw, dict) else None
    if not isinstance(stats, dict):
        return None
    values = tuple(stats.get(k) for k in ('requests_served', 'tokens_generated'))
    return values if all(finite(v) and v >= 0 and int(v) == v for v in values) else None


def same_activity(a, b):
    counters = activity_counters(a)
    return counters is not None and counters == activity_counters(b)


def graceful_drain(raw):
    """True when this provider's `start` drains accepted requests before restarting."""
    version = raw.get('version') if isinstance(raw, dict) else None
    match = re.match(r'(\d+)\.(\d+)\.(\d+)', version) if isinstance(version, str) else None
    return bool(match) and tuple(int(v) for v in match.groups()) >= DRAIN_VERSION


def drained_idle(raw):
    """The provider finished a drain and nothing restarted it: it serves no work."""
    lifecycle = raw.get('lifecycle') if isinstance(raw, dict) else None
    return bool(
        isinstance(lifecycle, dict)
        and lifecycle.get('outcome') == 'drained'
        and lifecycle.get('remaining') == 0
        and raw.get('inference_active') is False
        and not raw.get('warm_models')
    )


def manual_pause_at(state):
    queued_at = state.get('requestedAt')
    if (
        state.get('mode') == 'observe'
        and state.get('requestedKind') == 'manual'
        and state.get('requestedModel')
        and state.get('requestId')
        and finite(queued_at)
        and queued_at > 0
    ):
        return queued_at + MANUAL_IDLE_TIMEOUT
    return None


def manual_pause_due(state, now):
    deadline = manual_pause_at(state)
    return deadline is not None and finite(now) and now >= deadline


def launch_signature(options, environment):
    # Persist a comparison digest, never configuration secrets.
    return hashlib.sha256(
        json.dumps([options, environment], sort_keys=True, separators=(',', ':')).encode()
    ).hexdigest()


def reporting_models_match(roster_models, advertised):
    """The roster row lists at least one offered model and nothing else.

    Darkbloom's coordinator (0.9.10) builds this row from the models the provider
    registered, keeping only those that pass the same check that routes public
    requests to this provider: in the catalog with a matching weight hash, and the
    model's hardware, runtime and App Attest requirements met (registry/verification.go
    ForEachProviderVerification; provider_capabilities.go
    providerModelAllowedByCatalogLocked; routing_eligibility.go
    providerServesRoutableModelLocked). An offered model the row leaves out gets no
    network work on this Mac (only the owner's own requests may reach an off-catalog
    model), so it can't hide another Mac's output; statistics count the models the row
    lists. The row doesn't depend on which models are loaded: heartbeats update warm
    models only (registry/heartbeat.go)."""
    roster_set = set(roster_models)
    return bool(roster_set) and roster_set <= set(advertised)


def roster_identity(raw, rows):
    """Public models are coordinator eligibility, separate from hardware identity."""
    key = raw.get('attestation_public_key')
    if not isinstance(key, str) or not key or not isinstance(rows, list):
        raise ValueError('Invalid provider identity.')
    matches = [r for r in rows if isinstance(r, dict) and r.get('se_public_key') == key]
    if (
        len(matches) != 1
        or not isinstance(matches[0].get('provider_id'), str)
        or not matches[0]['provider_id']
    ):
        raise ValueError('Provider identity is not uniquely matched.')
    row = matches[0]
    models = row.get('models')
    valid = isinstance(models, list) and all(isinstance(m, str) and m for m in models)
    advertised = raw.get('advertised_models')
    eligible = bool(valid and selection_key(advertised) and set(models) == set(advertised))
    return {
        'provider': row['provider_id'],
        'models': models if valid else [],
        'servingEligible': eligible,
        'reportingEligible': bool(
            observed_models(advertised)
            and observed_models(models)
            and reporting_models_match(models, advertised)
            and all(isinstance(r, dict) for r in rows)
            and sum(r.get('provider_id') == row['provider_id'] for r in rows if isinstance(r, dict))
            == 1
        ),
        # Serving-authorized: hardware trust, or App Attest without MDM (serving_trust).
        'hardwareVerified': roster_authorized(row),
    }


def memory_budget(
    hardware, provider, model, weights, gpu_cache=0, held=0, config_reserve=DEFAULT_RESERVE_GB
):
    """v0.8.16 default load reserves. Cached files overlap available pages: don't add them.

    `held`: weights of models the provider has loaded, which a switch releases even when
    its reported active GPU memory misses them.
    """
    total = hardware.get('memoryTotalGB')
    available = hardware.get('memoryAvailableGB')
    resident = provider.get('memoryGB')
    if not all(finite(v) and v >= 0 for v in (total, available, resident, weights, gpu_cache)):
        return None
    # UnifiedMemoryCap.loadReserveBytes: provider.toml memory_reserve_gb, but at least what
    # the 90% cap (with its 2 GiB floor) leaves the OS.
    reserve = max(config_reserve, total * 0.1, 2)
    activation = 3.5 if model == 'gpt-oss-20b' else 5.5
    return {
        # A switch frees what Darkbloom reports as resident. Only when it reports nothing,
        # count the loaded models' weights: `held` is the CLI estimate, weights x 1.2.
        'afterUnloadGB': min(
            total,
            available
            + (resident if resident > 0 else held / 1.2 if finite(held) and held > 0 else 0)
            + gpu_cache,
        ),
        'requiredGB': weights + reserve + activation + 1,
        'reserveGB': reserve,
    }


def unknown_flag(token):
    """A `--name`/`-x` option BloomGauge does not parse (bare `-`/`--` never qualify)."""
    known = VALUE_FLAGS | BOOL_FLAGS | {'--model', '--foreground'}
    return (
        isinstance(token, str)
        and re.match(r'--?[A-Za-z]', token) is not None
        and token.split('=', 1)[0] not in known
    )



# After a start the network verifies the new session before it sends work (serving_trust
# daemon_verifying). Statistics wait for that clearance only this long after the start: longer
# than any verification modelled (7 min in the bug matrix). A Mac still not cleared after that
# isn't being verified but stuck; it counts as before, so its silent minutes reach the stall
# ladder, which may restart it (stall_recovery: probe at 5, restart at 8 silent minutes).
CLEARANCE_GRACE_SECONDS = 900

AGENT_READS = 3


def read_launch_agent(path, wait=time.sleep):
    """The provider's launch agent, read as a whole.

    Darkbloom rewrites this plist in place while it starts (not atomically), so a read can
    catch it empty or half-written: plistlib then raises InvalidFileException or Expat's
    parse error. Retry briefly; a plist still unreadable after that is reported as a
    ValueError, which every caller already treats as "settings unreadable, try again"."""
    for attempt in range(AGENT_READS):
        data = path.read_bytes()
        try:
            return plistlib.loads(data)
        except (plistlib.InvalidFileException, ExpatError, ValueError) as error:
            if attempt == AGENT_READS - 1:
                raise ValueError(
                    'The provider launch settings are being rewritten. Try again in a moment.'
                ) from error
            wait(0.25)

def launch_options(plist, allow_auto=False, allow_many=False):
    """Retain the user's endpoint, idle policy, config and coordinator. No shell.

    `allow_many`: three or more --model flags (the start picker's picks) read as no selection
    (None) instead of an error, for the model controls that replace them with one model.

    Flags BloomGauge does not know (a newer Darkbloom's) are kept verbatim and in order, so a
    restart passes them on unchanged. `--flag=value` is one token. A bare unknown `--flag`
    takes the next token as its value unless that token looks like a flag (starts with `-`
    followed by a letter, or `--`): `--x 5`, `--x -5` and `--x auto` keep their value, while
    `--x --local-endpoint` is a switch. Either way the tokens keep their order. A known flag
    written as `--flag=value`, a bare `-`/`--` or a stray word is refused."""
    args = plist.get('ProgramArguments', [])
    if not isinstance(args, list) or not args:
        raise ValueError('No installed Darkbloom service was found.')
    selected = []
    options = []
    i = 1
    while i < len(args):
        flag = args[i]
        if flag in ('--foreground', 'start'):
            i += 1
            continue
        if flag == '--model':
            if i + 1 >= len(args):
                raise ValueError('The provider model selection could not be read.')
            selected.append(args[i + 1])
            i += 2
            continue
        if flag in VALUE_FLAGS:
            if i + 1 >= len(args):
                raise ValueError('The provider settings could not be read.')
            options.extend(args[i : i + 2])
            i += 2
            continue
        if flag in BOOL_FLAGS:
            options.append(flag)
            i += 1
            continue
        if unknown_flag(flag):
            value = args[i + 1] if i + 1 < len(args) and '=' not in flag else None
            takes = isinstance(value, str) and not (
                value.startswith('--') or re.match(r'-[A-Za-z]', value)
            )
            options.extend(args[i : i + 2] if takes else [flag])
            i += 2 if takes else 1
            continue
        raise ValueError('This provider uses settings that automatic switching does not support.')
    key = selection_key(selected)
    many = allow_many and len(selected) >= 3 and len(set(selected)) == len(selected)
    if not key and not (allow_auto and not selected) and not many:
        raise ValueError(
            'Select one or two distinct serving models in Darkbloom before enabling experiments.'
        )
    return key, options


def planned_model(models, start, now, hours):
    slot = max(0, int((now - start) // (hours * 3600)))
    per_day = 24 // hours
    return models[(slot % per_day + slot // per_day) % len(models)], start + (
        slot + 1
    ) * hours * 3600


def best_candidate(rows, current):
    def recent(r):
        d = r.get('demand') or {}
        return d.get('recentSamples', 0) >= 25 and d.get('recentSpan', 0) >= 900

    def score(r):
        d = r.get('demand') or {}
        normal = d.get('pressure') or 0
        factor = max(0.75, min(1.25, (d.get('recentPressure') or 0) / normal)) if normal > 0 else 1
        return r['evidence']['score'] * factor

    eligible = [
        r
        for r in rows
        if r.get('selected')
        and r.get('available')
        and r.get('evidence', {}).get('eligible')
        and recent(r)
        and (r.get('demand') or {}).get('recentLoad', 0) > 0
    ]
    baseline = next(
        (r for r in rows if r['id'] == current and r.get('evidence', {}).get('tested')), None
    )
    if not baseline:
        return (
            None,
            'More verified warm time is needed on the current model. Passive runs and optional week tests both add to its history.',
        )
    if not recent(baseline):
        return (
            None,
            'Collecting at least 15 minutes of fresh network observations before comparing models.',
        )
    if not eligible:
        return (
            None,
            'No alternative has enough measured time and paid jobs yet. Passive runs and optional week tests both contribute.',
        )
    best = max(eligible, key=score)
    old = score(baseline)
    new = score(best)
    if best['id'] == current or new < old * 1.2 or new - old < 0.005:
        return (
            None,
            'Keeping the current model: no sufficiently tested alternative clears the 20% improvement threshold.',
        )
    return (
        best['id'],
        'A tested alternative has stronger earnings per warm hour after accounting for recent network demand.',
    )


class Optimizer:
    def __init__(self, history, network, home, stop, runner=None):
        self.h = history
        self.store = OptimizerStore(history)
        self.network = network
        self.home = pathlib.Path(home)
        self.stop = stop
        self.demand_auto = DemandOptimizer(history, self.store)
        self.switch_alerts = SwitchAlerts(history)
        self.decisions = DecisionJournal(history)
        self.last_demand_decision = None
        self.lock = threading.RLock()
        self.command_lock = threading.Lock()
        self.runner = runner or subprocess.run
        self.binary = self.home / '.darkbloom/bin/darkbloom'
        self.plist_path = self.home / 'Library/LaunchAgents/io.darkbloom.provider.plist'
        self.state = history.cache('optimizer-settings') or {
            'mode': 'observe',
            'models': [],
            'blockHours': 2,
            'startedAt': None,
            'endsAt': None,
            'lastSwitchAt': 0,
            'originalModel': None,
            'expectedModel': None,
        }
        if (self.state.get('requestedKind') or '').startswith('manual') or (
            self.state.get('pending', {}).get('kind') or ''
        ).startswith('manual'):
            self.state['manualResult'] = {
                **self.state.get('manualResult', {}),
                'status': 'interrupted',
                'detail': 'BloomGauge closed before the manual switch finished. Check the currently serving model before trying again.',
            }
        self.state['requestedModel'] = None
        self.state['requestedKind'] = None
        self.state['requestId'] = None
        # A restart cannot turn an old confirmation into a fresh opportunity.
        self.state.pop('demandProposal', None)
        # A policy saved by another build (an app update or downgrade) is repaired, not a reason
        # to turn the manager off. Only an unreadable one, or repaired rules under the legacy
        # strategy (they'd switch on rules the user didn't pick), turn automatic control off.
        was_on = self.state.get('mode', 'observe') != 'observe'
        notice = off_reason = None
        try:
            rules, repaired = repaired_policy(
                self.state.get('demandPolicy'), self.state.get('demandPolicyRevision', 0)
            )
        except ValueError:
            rules, repaired = demand_policy(), None
            off_reason = 'BloomGauge could not read its saved optimizer settings when it reopened, so automatic control is off. Your model keeps serving; review the plan and turn it on again.'
        self.state['demandPolicy'] = rules
        if repaired and was_on and not manager.enabled(rules):
            off_reason = 'Some saved optimizer settings no longer fit this version of BloomGauge and were moved to the nearest allowed values, so automatic switching is off. Review them, then turn it on again.'
        elif repaired and was_on:
            notice = 'Some saved optimizer settings no longer fit this version of BloomGauge and were moved to the nearest allowed values. Automatic control stays on.'
        if off_reason and was_on:
            self.state['mode'] = 'observe'
        self.state['demandPolicyRevision'] = POLICY_REVISION
        if manager.enabled(rules) and self.state.get('mode') in ('week', 'optimize', 'combo'):
            # Seven-day, historical and pair tests are legacy strategies, hidden under the
            # Manager; a run saved by a pre-manager build (its policy now upgrades to the
            # Manager) would still pause on any failed switch. The Manager takes it over.
            self.cancel_combo('The Manager took over automatic control.')
            self.state['mode'] = 'demand'
            notice = 'BloomGauge now runs automatic control with the Manager, which replaces seven-day tests, historical optimization and pair tests. Automatic control stays on.'
        if (off_reason and was_on or notice) and self.state.get('account'):
            self.store.event(
                self.state['account'],
                self.state.get('device', ''),
                time.time(),
                'manager-notice' if notice and not off_reason else 'paused',
                self.state.get('expectedModel'),
                off_reason or notice,
            )
        self.status = 'observing'
        self.detail = (
            off_reason
            if off_reason and was_on
            else 'Recording network demand and this Mac’s model performance.'
        )
        interrupted = self.state.pop('pending', None)
        if interrupted and manager.active(self.state):
            # Automatic control stays on: the manager checks what is serving and restores.
            manager.interrupted(self.state, interrupted, time.time())
            self.detail = 'BloomGauge closed during a switch. Checking the provider; the manager restores the home model if nothing becomes ready.'
        elif interrupted:
            self.state['mode'] = 'observe'
            self.detail = 'The dashboard closed during a switch. Automatic switching is paused; check the provider status.'
            self.cancel_combo(self.detail)
        if self.state.get('providerResult', {}).get('status') == 'working':
            self.state['providerResult'].update(
                status='interrupted',
                detail='BloomGauge closed before this provider command was verified. Refresh the model controls before retrying; no command is automatically replayed.',
            )
        if self.state.get('selectionRequest') and self.state.get('manualResult', {}).get(
            'status'
        ) in ('working', 'interrupted'):
            self.state['manualResult'].update(
                status='failed',
                detail='BloomGauge reopened before the selected start was verified. Refresh its status; no command was replayed.',
            )
        result = self.state.get('manualResult') or {}
        if result.get('status') in ('interrupted', 'failed') and manager.resume_manual(
            self.state, result.get('id'), result.get('model'), result['status']
        ):
            self.detail = 'BloomGauge closed during your model pick. It is kept as your pick; automatic control continues.'
        self.live = None
        self.raw = {}
        self.previous = None
        self.idle_since = None
        self.drained_since = None
        self.local = []
        self.list_all = None  # does this CLI accept `models list --all` (0.9.10+)?
        self.catalog = []
        self.discovery_at = 0
        self.discovery_error = None
        self.identity_at = 0
        self.identity_ok = False
        self.identity_session = None
        self.identity_provider = None
        self.reporting_roster = ReportingIdentity()
        self.reporting_scope_seen = None
        self.reporting_recheck_requested = False
        self.reporting_recheck_after = 0
        self.device_identity_ok = False
        self.identity_device = None  # device_id of the daemon state the roster matched
        self.identity_hardware = False
        self.eligible_models = []
        self.proof_lock = threading.Lock()
        self.proof_minutes = {}  # (device, model) -> (checkedAt, served_minutes), ~10 min
        self.runtime_proofs = None  # RUNTIME_PROOFS_KEY, read on first use
        self.proof_seen = {}  # model -> last runtimeProof, logged when it changes
        self.identity_detail = 'Waiting for a fresh match between this Mac and the provider roster.'
        self.next_discovery = 0
        self.next_identity = 0
        self.capacity_at = 0
        self.worker = None
        self.proposal = None
        self.next_switch = None
        self.warmup = {}
        self.warmup_worker = None
        self.update_guard = UpdateGuard(self)
        self.provider_control = ProviderControl(self)
        self.manual_selection = ManualSelection(self)
        self.state.setdefault('account', '')
        self.state.setdefault('device', '')
        if self.demand_auto.interrupt_pending(
            self.state['account'], self.state['device'], time.time()
        ) and not manager.active(self.state):
            self.state['mode'] = 'observe'
            self.detail = 'BloomGauge reopened during an automatic attempt. Its result is unverified; switching is paused and its downtime allowance is retained.'
        self.save()
        self.automatic_control = OptimizerControl(self)
        self.stall = StallControl(self)
        self.network_evidence = None  # collector.NetworkEvidence: public data for excursions
        self.network_health = None  # collector.NetworkHealth: Darkbloom-wide outages
        self.outage_logged = {}  # (account, device) -> outage last written to the activity log
        self.manager = ManagerControl(self)
        self.live_projection = OptimizerLive(self)

    def save(self):
        self.h.cache('optimizer-settings', self.state)

    def start(self):
        with self.lock:
            if getattr(self, '_started', False) or self.stop.is_set():
                return
            self._started = True
            self.refresh_worker = threading.Thread(target=self.refresh_loop, daemon=True)
            self.control_worker = threading.Thread(target=self.loop, daemon=True)
            self.automatic_worker = threading.Thread(target=self.automatic_control.run, daemon=True)
            self.refresh_worker.start()
            self.control_worker.start()
            self.automatic_worker.start()

    def read_state(self):
        d = json.loads((self.home / '.darkbloom/daemon-state.json').read_text())
        if not isinstance(d, dict):
            raise ValueError('Provider status is unavailable.')
        return d

    def read_agent(self):
        return read_launch_agent(self.plist_path, lambda seconds: self.stop.wait(seconds))

    def read_options(self, plist_only=False):
        plist = self.read_agent()
        model, args = launch_options(plist, allow_auto=True)
        if pathlib.Path(plist['ProgramArguments'][0]).resolve() != self.binary.resolve():
            raise ValueError(
                'The installed provider executable has changed. Reopen the dashboard before enabling switching.'
            )
        pinned = None if plist_only else manager.toml_selection(self.home, args)
        if pinned and pinned != model:
            # Darkbloom 0.9.10 follows provider.toml enabled_models, not the launch agent's
            # --model. Use it when the running daemon agrees; otherwise keep the plist.
            try:
                if same_selection(self.read_state(), pinned):
                    model = pinned
            except (OSError, ValueError):
                pass
        if not model:
            # New Darkbloom releases can save an auto-select launch. Use the
            # actual advertised selection, still checked against the live session
            # before commands. Never guess a selection from the downloaded list.
            model = selection_key(self.read_state().get('advertised_models'))
            if not model:
                raise ValueError(
                    'Wait for Darkbloom to select a serving model, then refresh the model controls.'
                )
        return model, args, plist.get('EnvironmentVariables', {})

    def observe(self, account, raw, snapshot):
        now = snapshot['at']
        device = device_id(raw)
        written = raw.get('written_at')
        models = raw.get('advertised_models', [])
        model = selection_key(models)
        stats = raw.get('stats', {})
        current = {
            'at': written,
            'account': account,
            'device': device,
            'model': model,
            'session': (raw.get('started_at'), raw.get('pid')),
            'jobs': stats.get('requests_served'),
            'tokens': stats.get('tokens_generated'),
            'active': raw.get('inference_active', True),
        }
        with self.lock:
            tracking = self.tracking(raw, now)
            current['ready'] = tracking['counting']
            current['verifiedAt'] = tracking.get('verifiedAt')
            published = snapshot['provider'].get('tracking')
            if published is not None:
                current['ready'] = bool(
                    current['ready']
                    and published.get('counting')
                    and published.get('verifiedAt') == current['verifiedAt']
                )
            self.live = {**snapshot, 'account': account, 'device': device}
            self.raw = raw
            recovery = self.state.get('cacheRecovery') or {}
            if (
                recovery.get('status') == 'cleared'
                and recovery.get('session') == session_key(raw)
                and raw.get('warm_models')
                and finite(written)
                and written > recovery.get('at', 0)
            ):
                # The cleanup let the model load. Darkbloom's next idle unload in this session
                # leaves its weights in file cache again, so it may clean up once more (Sep 28).
                recovery.update(status='loaded', detail='macOS file cache cleared; the model loaded.')
                self.save()
            previous = self.previous
            self.previous = current
            pending = bool(self.state.get('pending'))
            good = (
                snapshot['provider']['online']
                and model
                and account
                and device
                and finite(current['at'])
                and activity_counters(raw) is not None
                and isinstance(current['active'], bool)
            )
            attributable = (
                self.identity_ok
                and now - self.identity_at < 180
                and self.identity_session == current['session']
            )
            command = self.state.get('pending') or {}
            # A queued automatic preflight may read its immediately preceding
            # evidence, but cannot extend the clock while collection is paused.
            if not (command.get('kind') == 'demand' and not command.get('autoRunId')):
                self.demand_auto.trials.observe(
                    account,
                    device,
                    raw,
                    now,
                    bool(good and attributable and current['ready'] and not pending),
                    current.get('verifiedAt'),
                )
            if not good or pending or self.warmup.get('status') == 'warming':
                self.idle_since = None
                return
            if previous and current['at'] == previous['at']:
                return
            compatible = (
                previous
                and all(
                    previous[k] == current[k] for k in ('account', 'device', 'model', 'session')
                )
                and all(finite(previous[k]) for k in ('at', 'jobs', 'tokens'))
                and 0 < current['at'] - previous['at'] <= CONTINUITY_SECONDS
            )
            jobs = current['jobs'] - previous['jobs'] if compatible else -1
            tokens = current['tokens'] - previous['tokens'] if compatible else -1
            if not compatible or jobs < 0 or tokens < 0:
                self.idle_since = None
                return
            if current['active'] or jobs or tokens:
                self.idle_since = None
            elif self.idle_since is None:
                self.idle_since = now
            attributable = (
                self.identity_ok
                and now - self.identity_at < 180
                and self.identity_session == current['session']
            )
        if (
            attributable
            and current['ready']
            and previous.get('ready')
            and current['verifiedAt'] == previous.get('verifiedAt')
        ):
            self.store.sample(
                account,
                device,
                previous['at'],
                current['at'],
                model,
                jobs,
                tokens,
                current['active'],
                True,
            )

    def tracking(self, raw, now, cleared=True):
        """Statistics and the status line: a new session counts once the network lets it
        serve (the daemon's trust, or this session's roster row; serving_trust), for at most
        CLEARANCE_GRACE_SECONDS after the start. `cleared=False` skips that wait, for control
        (resume, On, demand following, the stall ladder), which keeps today's rule."""
        with self.lock:
            verified = (
                self.identity_ok
                and now - self.identity_at < 180
                and self.identity_session == (raw.get('started_at'), raw.get('pid'))
            )
            started = raw.get('started_at')
            clearing = bool(
                cleared
                and finite(started)
                and 0 <= now - started < CLEARANCE_GRACE_SECONDS
                and not daemon_authorized(raw)
                and not (verified and self.identity_hardware)
            )
            return readiness(
                raw,
                self.warmup,
                now,
                verified,
                bool(self.state.get('pending')),
                authorized=False if clearing else None,
            )

    def invalidate_reporting_identity(self):
        with self.lock:
            self.reporting_roster.revoke()
            self.reporting_scope_seen = None
            self.reporting_recheck_requested = False

    def reporting_identity(self, raw, now, account=None):
        """Exact read-only mapping; never used to admit a model command."""
        account = self.h.cache('account') if account is None else account
        with self.lock:
            if self.state.get('pending') or self.warmup.get('status') == 'warming':
                self.invalidate_reporting_identity()
                return None
            provider = self.reporting_roster.match(account, raw, now)
            scope = reporting_scope(account, raw, now)
            if scope is None:
                self.reporting_recheck_requested = False
            elif scope != self.reporting_scope_seen and provider is None:
                # A real stale/offline/process/model-set gap revoked the old proof.
                # Ask the existing background loop to revalidate the new scope;
                # never reuse that proof or perform network work on collection.
                self.reporting_recheck_requested = True
            self.reporting_scope_seen = scope
            return provider

    def combo_config_error(self, voluntary=True):
        try:
            _, options, environment = self.read_options()
            return combination_config_error(self.home, options, environment, voluntary=voluntary)
        except (OSError, ValueError, TypeError):
            return 'Provider settings must be readable before pair testing.'

    def config_reserve(self):
        """provider.toml memory_reserve_gb for load budgets (Darkbloom's default if unreadable)."""
        try:
            return configured_reserve_gb(self.home, self.read_options(plist_only=True)[1])
        except (OSError, ValueError, KeyError, TypeError, plistlib.InvalidFileException):
            return DEFAULT_RESERVE_GB

    def purge_credit(self, target, live, now, require_permission=True):
        """File cache that the purge before every switch may free, in GB.

        Darkbloom admits a load against free + inactive pages, and the previous
        model's weight files can sit in active file cache outside them. Counted
        only with the passwordless purge permission and fresh readings; the
        switch purges, re-measures and keeps the current model if it still
        doesn't fit, after which this model gets no credit for an hour.
        """
        hardware = (live or {}).get('hardware') or {}
        cached, at = hardware.get('cachedFilesGB'), hardware.get('at')
        with self.lock:
            shortfall = self.state.get('purgeShortfall') or {}
        if (
            not finite(cached)
            or cached <= 0
            or not finite(at)
            or not -5 < now - at < 15
            or (
                shortfall.get('model') == target
                and finite(shortfall.get('at'))
                and 0 <= now - shortfall['at'] < PURGE_SHORTFALL_HOLD
            )
            or require_permission
            and self.manual_selection.permission_status().get('status') != 'ready'
        ):
            return 0
        return cached

    def selection_budget(self, target, live, raw, manual=False):
        models = members(target)
        rows = {r['id']: r for r in self.candidates({}, {}, live, {}, manual=manual)}
        if not models or any(m not in rows or not rows[m]['available'] for m in models):
            return None
        cache = raw.get('capacity', {}).get('gpu_memory_cache_gb', 0)
        warm = raw.get('warm_models') if isinstance(raw.get('warm_models'), list) else []
        held = sum(
            rows[m]['memoryGB']
            for m in set(warm)
            if m in rows and finite(rows[m].get('memoryGB'))
        )
        reserve = self.config_reserve()
        if len(models) == 1:
            return memory_budget(
                live.get('hardware', {}),
                live.get('provider', {}),
                target,
                rows[target]['memoryGB'],
                cache,
                held,
                reserve,
            )
        # Knobs BloomGauge can't model stop automatic moves (controlError), not restores.
        if self.combo_config_error(voluntary=False):
            return None
        return pair_budget(
            live.get('hardware', {}),
            live.get('provider', {}),
            models,
            [rows[m]['memoryGB'] for m in models],
            cache,
            reserve,
        )

    def confirmation_scope(self, state, raw):
        operation = getattr(self.automatic_control, 'operation', None) or {}
        return confirmation_scope(state, raw, operation.get('id'))

    def demand_decision(self, now, settings=None, live=None, raw=None, preflight=None):
        with self.lock:
            settings = copy.deepcopy(self.state) if settings is None else settings
            live = (copy.deepcopy(self.live) or {}) if live is None else live
            raw = copy.deepcopy(self.raw) if raw is None else raw
            evaluated_scope = self.confirmation_scope(settings, raw)
        admission_activity = None
        # Queuing our own command pauses collection. Retain only the immediately
        # preceding paid/activity proof, never a stale proof or another session.
        proof = preflight or {}
        if (
            settings.get('mode') == 'demand'
            and not settings.get('requestedModel')
            and (settings.get('pending') or {}).get('model') == proof.get('target')
            and proof.get('target')
            and proof.get('session') == session_key(raw)
            and finite(proof.get('at'))
            and 0 <= now - proof['at'] <= 10
            and (proof.get('activity') or {}).get('fresh')
            and finite(proof['activity'].get('observedAt'))
            and 0 <= now - proof['activity']['observedAt'] < 15
            and activity_counters(raw) is not None
            and activity_counters(proof.get('raw') or {}) is not None
            and all(
                new >= old
                for new, old in zip(activity_counters(raw), activity_counters(proof['raw']))
            )
        ):
            live = copy.deepcopy(live)
            live['pulse'] = copy.deepcopy(proof.get('pulse'))
            live['provider'] = {
                **live.get('provider', {}),
                'session': copy.deepcopy(proof.get('providerSession')),
            }
            admission_activity = copy.deepcopy(proof['activity'])
        rules = demand_policy(settings.get('demandPolicy'))
        managed = manager.enabled(rules)
        gathering = data_gathering.status(settings.get('dataGathering'), now)
        if managed:
            gathering = {**gathering, 'active': False}  # no learning boost under the manager
        if gathering['active']:
            rules = data_gathering.relaxed(rules)
        current = selection_key(raw.get('advertised_models'))
        rows = self.candidates({}, {}, live, settings)
        for row in rows:
            if settings['mode'] != 'demand':
                row['selected'] = row['available']
            row['loadBudget'] = (
                self.selection_budget(row['id'], live, raw) if row['available'] else None
            )
            if row['loadBudget'] and row['id'] != current:
                credit = self.purge_credit(row['id'], live, now)
                row['loadBudget']['reclaimableGB'] = credit
                if not credit:
                    # Without the permission: what enabling cache cleanup could add.
                    row['loadBudget']['cleanupCouldFreeGB'] = self.purge_credit(
                        row['id'], live, now, require_permission=False
                    )
        decision = self.demand_auto.evaluate(
            live.get('account', ''),
            live.get('device', ''),
            rows,
            current,
            raw,
            rules,
            now,
            settings.get('lastSwitchAt', 0)
            if settings['mode'] == 'demand'
            else raw.get('started_at', 0),
            live=live,
            admission_activity=admission_activity,
            gathering=gathering,
            stall_escape=settings['mode'] == 'demand' and self.stall.escape_active(now),
        )
        try:
            _, options, environment = self.read_options()
            error = combination_config_error(self.home, options, environment, require_pair=False)
        except Exception:
            error = 'Provider launch settings must be readable before automatic switching.'
        # The manager holds (and restores) a pair without gemma; legacy compares solo models.
        if managed and manager.hold_error(current):
            error = manager.hold_error(current)
        elif not managed and len(members(current)) != 1:
            error = 'Demand following compares solo models. Select one serving model before enabling it.'
        decision['controlError'] = error
        if error:
            decision.update(target=None, reason=error)
            if decision.get('paidAlternative'):
                decision['paidAlternative'] = {
                    **decision['paidAlternative'],
                    'eligible': False,
                    'reason': error,
                }
        if managed:
            decision = self.manager.decide(decision, settings, live, raw, rows, now)
        decision['enabled'] = settings['mode'] == 'demand'
        # 'policy' is what is in effect (data gathering loosens limits); edit the saved one.
        decision['savedPolicy'] = demand_policy(settings.get('demandPolicy'))
        decision['runs'] = self.demand_auto.runs(
            live.get('account', ''), live.get('device', ''), now
        )
        with self.lock:
            captured_at = time.time()
            decision['execution'] = self.decisions.snapshot(
                live.get('account', ''), live.get('device', ''), captured_at
            )
            current_scope = self.confirmation_scope(self.state, self.raw)
            # Evaluation can take time. Read progress after it, from the same
            # current state used for execution reporting, never the input copy.
            decision['confirmation'] = (
                confirmation_view(self.state.get('demandProposal'), current_scope, captured_at)
                if evaluated_scope == current_scope and self.state['mode'] == 'demand'
                else None
            )
        return decision

    def scheduled_trial_hold(self, now, live, raw):
        current = selection_key(raw.get('advertised_models'))
        paid = fresh_paid(
            live.get('pulse'), live.get('provider', {}).get('session'), raw, current, now
        )
        evidence = self.store.evidence(
            live.get('account', ''), live.get('device', ''), now - 1200, now, now
        ).get(current, {})
        protect = demand_policy(self.state.get('demandPolicy'))['protectUsdPerHour']
        return high_earnings(evidence, raw.get('started_at'), now, paid, protect)

    def combo_snapshot(self, settings, live, raw, candidates, evidence):
        rows = [r for r in candidates if r['id'] in settings.get('models', [])]
        pairs = pair_candidates(
            rows,
            live.get('hardware', {}),
            live.get('provider', {}),
            raw.get('capacity', {}).get('gpu_memory_cache_gb', 0),
            self.combo_config_error(),
            manager.SOLO_MODELS if manager.enabled(settings.get('demandPolicy')) else (),
            self.config_reserve(),
        )
        results = [
            {
                'id': key,
                'name': selection_label(key),
                'evidence': {k: v for k, v in value.items() if k != 'minutes'},
            }
            for key, value in evidence.items()
            if len(members(key)) == 2
        ]
        return {
            'plan': copy.deepcopy(settings.get('comboPlan')),
            'candidates': pairs,
            'results': results,
        }

    def refresh(self, now):
        started = time.monotonic()

        def current_time():
            # Preserve the caller's clock (including isolated fixtures), but
            # account for CLI/catalog/roster work before reading fresh daemon data.
            return now + max(0, time.monotonic() - started)

        if now >= self.next_discovery:
            self.next_discovery = now + 300
            try:
                local = self.local_models()
                catalog = self.network.fetch('/v1/models/catalog')['models']
                if not isinstance(local, list) or not isinstance(catalog, list):
                    raise ValueError()
                with self.lock:
                    self.local = local
                    self.catalog = catalog
                    self.discovery_at = now
                    self.discovery_error = None
            except Exception:
                with self.lock:
                    self.discovery_error = (
                        'Could not refresh locally available models and the network catalog.'
                    )
        identity_now = current_time()
        with self.lock:
            refresh_identity = identity_now >= self.next_identity or (
                self.reporting_recheck_requested and identity_now >= self.reporting_recheck_after
            )
            if refresh_identity:
                self.next_identity = identity_now + 60
                self.reporting_recheck_after = identity_now + 15
                self.reporting_recheck_requested = False
        if refresh_identity:
            transport_failure = False
            try:
                raw = self.read_state()
                device = device_id(raw)
                if (
                    not finite(raw.get('written_at'))
                    or not -5 < identity_now - raw['written_at'] < 15
                ):
                    raise ValueError()
                account = self.h.cache('account')
                with self.lock:
                    self.reporting_roster.match(account, raw, identity_now)
                try:
                    rows = self.network.fetch('/v1/providers/attestation')['providers']
                except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
                    # Only a transport failure may retain reporting proof. HTTP,
                    # TLS trust, parsing and authoritative roster failures revoke it.
                    transport_failure = not isinstance(
                        error, (urllib.error.HTTPError, ssl.SSLError)
                    ) and not isinstance(getattr(error, 'reason', None), ssl.SSLError)
                    raise
                proof = roster_identity(raw, rows)
                self.store.identity(device, proof['provider'])
                latest = self.read_state()
                checked_at = current_time()
                latest_account = self.h.cache('account')
                with self.lock:
                    self.identity_at = identity_now
                    self.identity_ok = proof['servingEligible']
                    self.device_identity_ok = True
                    self.identity_device = device
                    self.identity_hardware = proof['hardwareVerified']
                    self.eligible_models = proof['models']
                    self.identity_session = (raw.get('started_at'), raw.get('pid'))
                    self.identity_provider = proof['provider']
                    original_scope = reporting_scope(account, raw, identity_now)
                    if (
                        proof['reportingEligible']
                        and proof['hardwareVerified']
                        and original_scope is not None
                        and original_scope == reporting_scope(latest_account, latest, checked_at)
                    ):
                        # The row's models are the offered ones the network routes here.
                        self.reporting_roster.confirm(
                            latest_account,
                            latest,
                            checked_at,
                            proof['provider'],
                            identity_now,
                            proof['models'],
                        )
                    else:
                        self.reporting_roster.revoke()
                    self.identity_detail = (
                        None
                        if self.identity_ok
                        else 'This Mac is matched, but the coordinator has not confirmed the selected model’s catalog or runtime eligibility. Warm-up is held.'
                    )
            except Exception:
                try:
                    latest = self.read_state()
                    latest_account = self.h.cache('account')
                except Exception:
                    latest = {}
                    latest_account = None
                with self.lock:
                    self.identity_ok = False
                    self.device_identity_ok = False
                    self.identity_device = None
                    self.identity_hardware = False
                    self.eligible_models = []
                    self.identity_provider = None
                    if transport_failure:
                        self.reporting_roster.match(latest_account, latest, current_time())
                    else:
                        self.reporting_roster.revoke()
                    self.identity_detail = (
                        'Waiting for a fresh match between this Mac and the provider roster.'
                    )
        capacity = self.network.snapshot('capacity')
        if capacity.get('status') == 'ok' and capacity.get('updatedAt', 0) > self.capacity_at:
            self.store.network(capacity['updatedAt'], capacity.get('data', {}).get('models', []))
            self.capacity_at = capacity['updatedAt']

    def candidates(self, evidence, demand, live, settings, manual=False):
        current = live.get('provider', {}).get('model')
        total = live.get('hardware', {}).get('memoryTotalGB', 0)
        if not finite(total):
            total = 0
        with self.lock:
            local = copy.deepcopy(self.local)
            catalog = copy.deepcopy(self.catalog)
        by_id = {m['id']: m for m in local if isinstance(m.get('id'), str)}
        result = []
        for m in catalog:
            model = m.get('id')
            if not isinstance(model, str):
                continue
            disk = by_id.get(model)
            reason = None
            runtime_check = False
            proof = None
            if not disk:
                reason = 'Not available in the local CLI model list'
            elif not m.get('active', False):
                reason = 'Not active in the network catalog'
            elif disk.get('template_render_ok') is False and model != current:
                # A switch to it would be refused (verify_local_target) after a purge and
                # catalog fetch; say so up front instead of dispatching it.
                reason = 'Its chat template failed Darkbloom’s check. Refresh models to recheck it.'
            elif not finite(m.get('min_ram_gb')) or m['min_ram_gb'] > total:
                reason = 'Exceeds this Mac’s memory eligibility'
            elif (
                not finite(disk.get('estimated_memory_gb'))
                or disk['estimated_memory_gb'] + 6 > total
            ):
                reason = 'Insufficient model memory headroom'
            elif m.get('required_provider_capabilities') is not None and (
                not isinstance(m['required_provider_capabilities'], list)
                or any(
                    not isinstance(c, str) or not c or c != c.strip()
                    for c in m['required_provider_capabilities']
                )
            ):
                reason = 'The catalog runtime requirements could not be verified.'
            elif m.get('required_provider_capabilities'):
                required = m['required_provider_capabilities']
                proof, why = self.runtime_proof(model, required, disk, live)
                # Never proven on this Mac: only an explicit manual Verify & switch may
                # try it, and that attempt is not automatic eligibility.
                if not proof:
                    runtime_check = bool(
                        manual and self.manual_runtime_allowed(required, disk, live)
                    )
                    if not runtime_check:
                        reason = RUNTIME_UNVERIFIED + ' ' + why
            result.append(
                {
                    'id': model,
                    'name': m.get('display_name') or model,
                    'available': reason is None,
                    'reason': reason,
                    'downloaded': bool(disk),
                    'memoryGB': disk.get('estimated_memory_gb') if disk else m.get('size_gb'),
                    'requiresRuntimeVerification': runtime_check,
                    # Why a model with runtime requirements may be picked automatically:
                    # 'roster', 'history' or None (not allowed, or no requirements).
                    'runtimeProof': proof,
                    'selected': model in settings.get('models', []),
                    'current': model == current,
                    'evidence': {
                        k: v for k, v in evidence.get(model, {}).items() if k != 'minutes'
                    },
                    'demand': demand.get(model),
                }
            )
        return sorted(
            result,
            key=lambda r: (
                not r['available'],
                -r.get('evidence', {}).get('usdPerHour', 0),
                r['name'],
            ),
        )

    def manual_runtime_allowed(self, required, disk, live):
        """Permission to try a known runtime, never proof it is attested or ready."""
        now = time.time()
        with self.lock:
            raw = copy.deepcopy(self.raw)
            trusted = bool(
                self.device_identity_ok
                and self.identity_hardware
                and self.identity_ok
                and 0 <= now - self.identity_at < 180
                and self.identity_session == (raw.get('started_at'), raw.get('pid'))
            )
            warmup = copy.deepcopy(self.warmup)
        h = live.get('hardware', {})
        return bool(
            required
            and set(required) <= {'apple_m5', 'mlx_nax'}
            and re.fullmatch(r'Apple M5(?: Pro| Max| Ultra)?', h.get('chip', ''))
            and disk.get('template_render_ok') is True
            and finite(disk.get('size_bytes'))
            and disk['size_bytes'] > 0
            and trusted
            and finite(live.get('at'))
            and -5 < now - live['at'] < 10
            and finite(h.get('at'))
            and -5 < now - h['at'] < 15
            and len(members(selection_key(raw.get('advertised_models')))) == 1
            and readiness(raw, warmup, now, trusted, False)['counting']
        )

    def capability_verified(self, model):
        with self.lock:
            return bool(
                self.device_identity_ok
                and self.identity_hardware
                and 0 <= time.time() - self.identity_at < 180
                and self.identity_session == (self.raw.get('started_at'), self.raw.get('pid'))
                and model in self.eligible_models
            )

    def runtime_proof(self, model, required, disk, live):
        """Why automatic selection may load a model with catalog runtime requirements.

        Returns ('roster' | 'history', None), or (None, the plain reason it may not).
        'roster': the coordinator lists the model as eligible for the running session.
        'history': this Mac's own Darkbloom runtime reports every required capability, the
        downloaded files and template check out, the roster verified this device, and the
        device has served the model before (RUNTIME_PROOF_MINUTES complete ready minutes in
        RUNTIME_PROOF_DAYS, or a saved roster proof). Neither is readiness: a switch must
        still pass post-start coordinator eligibility and warm-up (verify_started), and one
        that does not is a failed switch that restores the previous model.
        """
        if self.capability_verified(model):
            self.save_runtime_proof(model, live)
            verdict = ('roster', None)
        else:
            verdict = self.history_proof(model, required, disk, live)
        with self.proof_lock:
            changed = self.proof_seen.get(model, False) != verdict[0]
            self.proof_seen[model] = verdict[0]
        if changed:
            log.info(
                'Automatic selection of %s: %s',
                model,
                'allowed (%s proof).' % verdict[0] if verdict[0] else 'not allowed. ' + verdict[1],
            )
        return verdict

    def history_proof(self, model, required, disk, live):
        now = time.time()
        with self.lock:
            reported = self.raw.get('runtime_capabilities')
            device = device_id(self.raw)
            # The fresh roster match is for this daemon's device, the one history is kept under.
            verified = bool(
                device
                and self.device_identity_ok
                and self.identity_hardware
                and 0 <= now - self.identity_at < 180
                and self.identity_device == device
                and live.get('device') == device
            )
        reported = reported if isinstance(reported, list) else []
        missing = [c for c in required if c not in reported]
        if not device:
            return None, 'Waiting to verify this Mac with the provider roster.'
        if missing:
            return None, 'This Mac’s Darkbloom runtime doesn’t report %s.' % ', '.join(missing)
        if not (
            disk.get('template_render_ok') is True
            and finite(disk.get('size_bytes'))
            and disk['size_bytes'] > 0
        ):
            return None, 'Refresh models to verify its downloaded files and template.'
        if not self.served_here(device, model, now):
            return (
                None,
                'Needs one run on this Mac first: pick it from the manual model list to verify it.',
            )
        if not verified:
            return None, 'Waiting to verify this Mac with the provider roster.'
        return 'history', None

    def served_here(self, device, model, now):
        """This device served `model` before: saved roster proof, or enough recent history."""
        return bool(
            model in self.saved_runtime_proofs().get(device, {})
            or self.ready_minutes(device, model, now) >= RUNTIME_PROOF_MINUTES
        )

    def ready_minutes(self, device, model, now):
        """Complete ready minutes in RUNTIME_PROOF_DAYS, cached: candidates() runs often."""
        key = (device, model)
        with self.proof_lock:
            hit = self.proof_minutes.get(key)
        if hit and 0 <= now - hit[0] < RUNTIME_PROOF_CACHE_SECONDS:
            return hit[1]
        try:
            count = self.store.served_minutes(device, model, now - RUNTIME_PROOF_DAYS * 86400)
        except Exception:
            log.exception('Could not read ready minutes for %s', model)
            return 0
        with self.proof_lock:
            self.proof_minutes[key] = (now, count)
        return count

    def saved_runtime_proofs(self):
        """{device: {model: lastProvenAt}}: roster proofs that outlive history rows."""
        with self.proof_lock:
            if self.runtime_proofs is not None:
                return self.runtime_proofs
        try:
            saved = self.h.cache(RUNTIME_PROOFS_KEY)
        except Exception:
            log.exception('Could not read saved runtime proofs')
            return {}
        proofs = {
            device: {m: at for m, at in models.items() if isinstance(m, str) and finite(at)}
            for device, models in (saved.items() if isinstance(saved, dict) else ())
            if isinstance(device, str) and device and isinstance(models, dict)
        }
        with self.proof_lock:
            if self.runtime_proofs is None:
                self.runtime_proofs = proofs
            return self.runtime_proofs

    def save_runtime_proof(self, model, live):
        """The roster verified `model` and this device has served it: keep that proof."""
        now = time.time()
        with self.lock:
            device = device_id(self.raw)
        if not device or live.get('device') != device:
            return
        last = self.saved_runtime_proofs().get(device, {}).get(model)
        if finite(last) and 0 <= now - last < RUNTIME_PROOF_CACHE_SECONDS:
            return
        if self.ready_minutes(device, model, now) < RUNTIME_PROOF_MINUTES:
            return
        with self.proof_lock:
            if self.runtime_proofs is None:
                return  # the saved proofs could not be read: never overwrite them
            proofs = copy.deepcopy(self.runtime_proofs)
            proofs.setdefault(device, {})[model] = now
            try:
                self.h.cache(RUNTIME_PROOFS_KEY, proofs)
            except Exception:
                log.exception('Could not save the runtime proof for %s', model)
                return
            self.runtime_proofs = proofs

    def snapshot(self, start=None, end=None, remote=False):
        now = time.time()
        start = max(0, now - 604800) if start is None else start
        end = now if end is None else end
        with self.lock:
            live = copy.deepcopy(self.live) or {}
            settings = copy.deepcopy(self.state)
            status = self.status
            detail = self.detail
            next_switch = self.next_switch
            discovery_error = self.discovery_error
            discovery_at = self.discovery_at
            identity_fresh = 0 <= now - self.identity_at < 180 and self.identity_session == (
                self.raw.get('started_at'),
                self.raw.get('pid'),
            )
            identity_ok = self.identity_ok and identity_fresh
            control_version = self.control_version()
        account = live.get('account', '')
        device = live.get('device', '')
        evidence, demand, events, coverage = self.store.summary(account, device, start, end, now)
        candidates = self.candidates(evidence, demand, live, settings)
        cap = self.network.snapshot('capacity')
        current = {
            m['id']: m
            for m in cap.get('data', {}).get('models', [])
            if isinstance(m.get('id'), str)
        }
        for m in candidates:
            c = current.get(m['id'])
            m['liveDemand'] = (
                {
                    'active': c.get('active_requests'),
                    'queued': c.get('queued_requests'),
                    'warm': c.get('warm_providers'),
                }
                if c
                else None
            )
        with self.lock:
            raw = copy.deepcopy(self.raw)
        h = live.get('hardware', {})
        gpu_cache = raw.get('capacity', {}).get('gpu_memory_cache_gb', 0)
        reserve = self.config_reserve()
        for m in candidates:
            m['loadBudget'] = memory_budget(
                h, live.get('provider', {}), m['id'], m.get('memoryGB'), gpu_cache, 0, reserve
            )
        try:
            self.read_options()
            control_error = None
        except (OSError, ValueError, KeyError, TypeError, plistlib.InvalidFileException) as e:
            control_error = (
                str(e)
                if isinstance(e, ValueError)
                else 'The installed provider service could not be read.'
            )
        return {
            'at': now,
            'mode': settings['mode'],
            'status': status,
            'detail': detail,
            'lastSwitchResult': settings.get('lastSwitchResult'),
            'models': candidates,
            'selected': settings['models'],
            'blockHours': settings['blockHours'],
            'startedAt': settings.get('startedAt'),
            'endsAt': settings.get('endsAt'),
            'nextSwitchAt': next_switch,
            'currentModel': selection_key(raw.get('advertised_models'))
            or live.get('provider', {}).get('model'),
            'currentModels': observed_models(raw.get('advertised_models')),
            'reporting': live.get('provider', {}).get('multiModelReporting'),
            'combinations': self.combo_snapshot(settings, live, raw, candidates, evidence),
            'originalModel': settings.get('originalModel'),
            'requestedModel': settings.get('requestedModel'),
            'busy': bool(settings.get('pending')),
            'requestedKind': settings.get('requestedKind'),
            'warmup': self.warmup_snapshot(),
            'demandAuto': self.demand_decision(now, settings, live, raw),
            'resumeDemand': self.demand_resume_status(),
            'stallRecovery': self.stall.snapshot(now),
            'memory': {
                'availableGB': h.get('memoryAvailableGB'),
                'cachedFilesGB': h.get('cachedFilesGB'),
                'purgeableGB': h.get('purgeableGB'),
                'providerGB': live.get('provider', {}).get('memoryGB'),
                'providerCacheGB': gpu_cache,
                'automaticPurge': True,
                'cacheRecovery': {
                    k: v for k, v in settings.get('cacheRecovery', {}).items() if k != 'session'
                },
            },
            'canManage': True,
            'remote': remote,
            'controlVersion': control_version,
            'controlError': control_error,
            'discoveryError': discovery_error,
            'discoveryAt': discovery_at,
            'identityVerified': identity_ok,
            'deviceIdentityVerified': self.device_identity_ok and identity_fresh,
            'servingEligibilityVerified': identity_ok,
            'eligibilityDetail': self.identity_detail,
            'networkFresh': cap.get('status') == 'ok' and now - cap.get('updatedAt', 0) < 120,
            'coverageStart': coverage[0],
            'coverageEnd': coverage[1],
            'events': events,
            'scope': 'This Mac only · inference earnings · base rewards excluded',
            'policy': {
                'minimumHours': 6,
                'minimumDays': 2,
                'minimumJobs': 20,
                'improvementPercent': 20,
                'settlementLagSeconds': 120,
            },
        }

    def manual_snapshot(self, remote=False):
        now = time.time()
        with self.lock:
            live = copy.deepcopy(self.live) or {}
            raw = copy.deepcopy(self.raw)
            state = copy.deepcopy(self.state)
            status = self.status
            detail = self.detail
            discovery_at = self.discovery_at
            discovery_error = self.discovery_error
            matched = (
                self.identity_ok
                and 0 <= now - self.identity_at < 180
                and self.identity_session == (raw.get('started_at'), raw.get('pid'))
            )
            idle_since = self.idle_since
        try:
            _, options, _ = self.read_options()
            error = self.endpoint_notice(options)
        except Exception as e:
            error = (
                str(e)
                if isinstance(e, ValueError)
                else 'The provider launch settings could not be read. Check Darkbloom on the Mac.'
            )
        if not error and (
            not live.get('provider', {}).get('online') or now - live.get('at', 0) > 10
        ):
            error = 'Start Darkbloom on the Mac and wait for fresh readings.'
        if not error and not matched:
            error = self.identity_detail or 'Waiting to match this Mac to the provider roster.'
        if not error and (discovery_error or now - discovery_at > 600):
            error = 'Refresh the model list before switching.'
        candidates = self.candidates({}, {}, live, state, manual=True)
        reserve = self.config_reserve()
        for m in candidates:
            m['loadBudget'] = memory_budget(
                live.get('hardware', {}),
                live.get('provider', {}),
                m['id'],
                m.get('memoryGB'),
                raw.get('capacity', {}).get('gpu_memory_cache_gb', 0),
                config_reserve=reserve,
            )
        pending = state.get('pending') or {}
        queued_at = state.get('requestedAt') or state.get('manualResult', {}).get('at')
        fresh = bool(
            live.get('provider', {}).get('online')
            and finite(raw.get('written_at'))
            and -5 < now - raw['written_at'] < 10
            and activity_counters(raw) is not None
            and isinstance(raw.get('inference_active'), bool)
        )
        idle_seconds = (
            min(12, max(0, now - idle_since))
            if fresh and raw.get('inference_active') is False and idle_since is not None
            else 0
        )
        cleanup = state.get('cacheRecovery') or {}
        pause_at = manual_pause_at(state)
        cleanup_event = {
            k: v for k, v in cleanup.items() if k in ('at', 'status', 'model', 'detail')
        }
        if cleanup_event:
            cleanup_event['currentSession'] = cleanup.get('session') == session_key(raw)
        value = {
            'at': now,
            'currentModel': selection_key(raw.get('advertised_models'))
            or live.get('provider', {}).get('model'),
            'session': session_key(raw),
            'mode': state['mode'],
            'models': [
                {
                    k: m.get(k)
                    for k in (
                        'id',
                        'name',
                        'available',
                        'reason',
                        'memoryGB',
                        'loadBudget',
                        'requiresRuntimeVerification',
                        'runtimeProof',
                    )
                }
                for m in candidates
            ],
            'queuedModel': state.get('requestedModel'),
            'requestedKind': state.get('requestedKind'),
            'requestId': state.get('requestId'),
            'switching': bool(pending),
            'switchingModel': pending.get('model'),
            'status': status,
            'detail': detail,
            'warmup': self.warmup_snapshot(),
            'queue': {
                'queuedAt': queued_at
                if state.get('requestedModel') and finite(queued_at)
                else None,
                'ageSeconds': max(0, now - queued_at)
                if state.get('requestedModel') and finite(queued_at)
                else None,
                'pauseAt': pause_at,
                'pauseInSeconds': max(0, pause_at - now) if pause_at is not None else None,
                'pausingForSwitch': bool(pending.get('afterIdleTimeout')),
                'idleSeconds': idle_seconds,
                'requiredIdleSeconds': 12,
                'activity': 'busy'
                if fresh and raw.get('inference_active')
                else 'idle'
                if fresh
                else 'unknown',
                'fresh': fresh,
            },
            'cacheCleanup': {'automatic': True, 'lastAttempt': cleanup_event or None},
            'canCancel': bool(
                state.get('requestedModel')
                and (state.get('requestedKind') or '').startswith('manual')
                and not pending
            ),
            'controlError': error,
            'lastResult': state.get('manualResult'),
            'discoveryAt': discovery_at,
            'remote': remote,
            'providerControl': self.provider_control.snapshot(),
        }

        return self.manual_selection.decorate(value, remote)

    def manual_action(self, data, source='mac'):
        action = data.get('action')
        if action == 'select':
            return self.manual_selection.action(data, source)
        if action in ('provider-start', 'provider-stop', 'provider-endpoint'):
            return self.provider_control.action(data, source)
        if action == 'refresh':
            with self.lock:
                self.next_discovery = 0
                self.next_identity = 0
            with self.manual_selection.permission_lock:
                self.manual_selection.permission = None
            return self.manual_snapshot(remote=source == 'phone')
        if action not in ('switch', 'cancel'):
            raise ValueError('Choose switch, cancel, or refresh.')
        # Reject malformed requests before potentially slow provider observation.
        # Atomic admission revalidates the ID before any state change.
        try:
            uuid.UUID(data.get('requestId', ''))
        except (ValueError, TypeError, AttributeError):
            raise ValueError('A valid switch request ID is required.')
        prepared = None
        if action == 'switch':
            prepared = {
                'snapshot': self.manual_snapshot(remote=source == 'phone'),
                'raw': self.read_state(),
                'options': self.read_options(),
                'disabled': self.service_disabled(),
            }
        self.apply_manual_action(data, source, prepared)
        return self.manual_snapshot(remote=source == 'phone')

    def apply_manual_action(self, data, source, prepared):
        """Atomic state admission; slow service observation and response are outside."""
        action = data.get('action')
        if action not in ('switch', 'cancel'):
            raise ValueError('Choose switch, cancel, or refresh.')
        try:
            request_id = str(uuid.UUID(data.get('requestId', '')))
        except (ValueError, TypeError, AttributeError):
            raise ValueError('A valid switch request ID is required.')
        with self.lock:
            if action == 'cancel':
                if self.state.get('requestId') != request_id or not (
                    self.state.get('requestedKind') or ''
                ).startswith('manual'):
                    raise ValueError('This request is no longer queued. Refresh its status.')
                if self.state.get('pending'):
                    raise ValueError(
                        'The restart has started and must finish before another change.'
                    )
                self.automatic_control.cancel(
                    'Manual model control cancelled the pending On request.'
                )
                model = self.state.get('requestedModel')
                self.state.update(
                    requestedModel=None,
                    requestedKind=None,
                    requestId=None,
                    requestedAt=None,
                    requestedSession=None,
                    requestedLaunchSignature=None,
                    mode='observe',
                )
                self.state['manualResult'] = {
                    'id': request_id,
                    'model': model,
                    'status': 'cancelled',
                    'at': time.time(),
                    'detail': 'Manual switch cancelled. The current model stays running.',
                }
                self.manager.manual_finished(request_id, None, 'cancelled')
                self.status = 'observing'
                self.detail = self.state['manualResult']['detail']
                self.proposal = None
                self.save()
                self.store.event(
                    self.state.get('account', ''),
                    self.state.get('device', ''),
                    time.time(),
                    'manual-cancelled',
                    model,
                    self.detail,
                )
                return None
            self.update_guard.require_available()
            model = data.get('model')
            expected = data.get('expectedSession')
            verify_runtime = data.get('verifyRuntime', False)
            if any(r['id'] == request_id for r in self.state.get('selectionRequests', [])):
                raise ValueError('This request ID was already used for a different command.')
            if not isinstance(verify_runtime, bool):
                raise ValueError('Verify & switch requires an explicit confirmation.')
            if not isinstance(model, str) or not 0 < len(model) <= 200:
                raise ValueError('Select an available model.')
            for saved in self.state.get('manualRequests', []):
                if saved['id'] == request_id:
                    if (
                        saved['model'] != model
                        or saved['session'] != expected
                        or saved.get('verifyRuntime', False) != verify_runtime
                    ):
                        raise ValueError(
                            'This request ID was already used for a different selection.'
                        )
                    return None
            if self.state.get('pending') or self.state.get('requestedModel'):
                raise ValueError(
                    'A switch is already queued or running. Cancel it or wait for it to finish.'
                )
            snapshot = prepared['snapshot']
            if snapshot['controlError']:
                raise ValueError(snapshot['controlError'])
            if not any(m['id'] == model and m['available'] for m in snapshot['models']):
                raise ValueError('Select a supported model from the current model list.')
            if (
                any(
                    m['id'] == model and m.get('requiresRuntimeVerification')
                    for m in snapshot['models']
                )
                and not verify_runtime
            ):
                raise ValueError(
                    'Choose Verify & switch to check this model with Darkbloom. Runtime support has not yet been confirmed.'
                )
            raw = prepared['raw']
            current, options, environment = prepared['options']
            now = time.time()
            live = copy.deepcopy(self.live) or {}
            if endpoint_issue(options):
                raise ValueError(self.endpoint_notice(options))
            if (
                expected != session_key(raw)
                or expected != session_key(self.raw)
                or expected != snapshot['session']
                or not same_selection(raw, current)
                or now - raw.get('written_at', 0) > 10
            ):
                raise ValueError(
                    'The provider changed since this page loaded. Refresh before switching.'
                )
            if prepared['disabled'] is not False:
                raise ValueError(
                    'Darkbloom is stopped or its launch state is unknown. Start it on the Mac first.'
                )
            self.automatic_control.cancel(
                'Manual model selection cancelled the pending On request.'
            )
            record = {
                'id': request_id,
                'model': model,
                'session': expected,
                'verifyRuntime': verify_runtime,
            }
            self.state['manualRequests'] = (self.state.get('manualRequests', []) + [record])[-32:]
            if model == current:
                self.state['manualResult'] = {
                    'id': request_id,
                    'model': model,
                    'status': 'unchanged',
                    'at': now,
                    'detail': 'This model is already serving. No restart was sent.',
                }
                self.manager.manual_queued(request_id, model, unchanged=True)
                self.save()
                return None
            self.manager.manual_queued(request_id, model)
            self.state.update(
                mode='observe',
                requestedModel=model,
                requestedKind='manual',
                requestId=request_id,
                expectedModel=current,
                requestedVerifyRuntime=verify_runtime,
                requestedAt=now,
                requestedSession=session_key(raw),
                requestedLaunchSignature=launch_signature(options, environment),
                account=live.get('account', ''),
                device=live.get('device', ''),
            )
            self.cancel_combo('Manual model selection cancelled the follow-on test.')
            self.state.pop('rollbackModel', None)
            self.state['manualResult'] = {
                'id': request_id,
                'model': model,
                'status': 'queued',
                'at': now,
                'detail': 'Manual switch queued. Automatic switching is paused.',
            }
            self.status = 'waiting'
            self.detail = 'Manual switch queued. Waiting for an idle period and enough memory.'
            self.proposal = None
            self.next_switch = None
            self.save()
            self.store.event(
                live.get('account', ''),
                live.get('device', ''),
                now,
                'manual-requested',
                model,
                'Manual selection from '
                + ('phone' if source == 'phone' else 'Mac')
                + '. Automatic switching paused.',
            )
        return None

    def cancel_combo(self, detail):
        plan = self.state.get('comboPlan')
        if plan and plan.get('status') in ('queued', 'running'):
            plan.update(status='cancelled', detail=detail, endedAt=time.time())

    def combo_action(self, data, source='mac'):
        if data['action'] == 'cancel-combos':
            with self.lock:
                self.require_current_control(data)
                self.cancel_combo('Combination testing cancelled from ' + source + '.')
                if self.state['mode'] == 'combo':
                    self.pause_internal(
                        'Combination testing stopped; the current selection stays running.'
                    )
                self.save()
            return self.snapshot()
        snapshot = self.snapshot()
        with self.lock:
            self.require_current_control(data)
            if (
                self.state['mode'] != 'week'
                or not self.state.get('endsAt')
                or time.time() >= self.state['endsAt']
            ):
                raise ValueError(
                    'Schedule combinations while the single-model week test is still running.'
                )
            if self.state.get('pending') or self.state.get('requestedModel'):
                raise ValueError(
                    'Wait for the current switch to finish before scheduling combinations.'
                )
            if not snapshot['identityVerified'] or snapshot['controlError']:
                raise ValueError(
                    'Wait for fresh, verified provider status before scheduling combinations.'
                )
            allowed = {r['id']: r for r in snapshot['combinations']['candidates'] if r['available']}
            requested = data.get('pairs')
            if requested is None:
                keys = list(allowed)[:6]
            elif not isinstance(requested, list) or not 1 <= len(requested) <= 6:
                raise ValueError('Select one to six compatible model pairs.')
            else:
                keys = [selection_key(pair) for pair in requested]
                if any(not k or len(members(k)) != 2 for k in keys) or len(set(keys)) != len(keys):
                    raise ValueError('Select distinct, two-model combinations.')
            if not keys or any(k not in allowed for k in keys):
                raise ValueError(
                    'No selected pair fits this Mac’s model, configuration, and physical-memory requirements.'
                )
            self.state['comboPlan'] = {
                'status': 'queued',
                'pairs': [members(k) for k in keys],
                'parentStartedAt': self.state['startedAt'],
                'queuedFor': self.state['endsAt'],
                'blockHours': self.state['blockHours'],
                'durationDays': 7,
                'startedAt': None,
                'endsAt': None,
                'detail': 'Queued after the current single-model test. Its schedule and running model are unchanged.',
            }
            self.save()
            self.store.event(
                self.state.get('account', ''),
                self.state.get('device', ''),
                time.time(),
                'combos-queued',
                None,
                'Queued a seven-day combination comparison after the existing test from '
                + source
                + '.',
            )
        return self.snapshot()

    def begin_combos(self, now, settings, live, current):
        plan = settings.get('comboPlan') or {}
        if plan.get('status') != 'queued':
            return False
        if plan.get('parentStartedAt') != settings.get('startedAt') or plan.get(
            'queuedFor'
        ) != settings.get('endsAt'):
            self.pause_internal(
                'The original test changed; its queued combination phase was cancelled.'
            )
            return True
        reason = self.wait_reason(live, now)
        if reason:
            with self.lock:
                self.status = 'waiting'
                self.detail = 'Solo test finished. Combination phase is waiting: ' + reason
            return True
        summary = self.snapshot(now - 604800, now)
        eligible = {r['id'] for r in summary['combinations']['candidates'] if r['available']}
        pairs = [selection_key(pair) for pair in plan['pairs'] if selection_key(pair) in eligible]
        if not pairs:
            self.pause_internal(
                'The solo test finished, but no queued combination is currently eligible. Review pair requirements before starting another test.'
            )
            return True
        # A concurrent solo reference avoids comparing a new week only to an old week.
        solos = [r for r in summary['models'] if r['available'] and r['id'] in settings['models']]
        measured = [r for r in solos if r.get('evidence', {}).get('hours', 0) >= 6]
        reference = (
            max(measured, key=lambda r: r['evidence'].get('usdPerHour', 0))['id']
            if measured
            else current
        )
        configurations = [reference] + pairs
        with self.lock:
            if self.state.get('comboPlan') != plan or self.state['mode'] != 'week':
                return True
            self.state['mode'] = 'combo'
            self.state['comboPlan'].update(
                status='running',
                startedAt=now,
                endsAt=now + 604800,
                configurations=configurations,
                referenceModel=reference,
                detail='Seven-day combination comparison with a solo reference. Both models must pass warm-up.',
            )
            self.status = 'learning'
            self.detail = self.state['comboPlan']['detail']
            self.next_switch = None
            self.save()
        self.store.event(
            live['account'],
            live['device'],
            now,
            'combos-started',
            current,
            'Single-model week finished. Beginning queued combination comparison; memory and readiness checks still apply, with no idle wait before a qualified rotation.',
        )
        return True

    def control_version(self):
        """Reject commands based on a provider or experiment that has since changed."""
        with self.lock:
            fields = (
                'mode',
                'models',
                'blockHours',
                'startedAt',
                'endsAt',
                'expectedModel',
                'requestedModel',
                'requestedKind',
                'requestId',
                'pending',
                'comboPlan',
                'demandPolicy',
                'dataGathering',
            )
            return hashlib.sha256(
                json.dumps(
                    [session_key(self.raw), {k: self.state.get(k) for k in fields}], sort_keys=True
                ).encode()
            ).hexdigest()

    def control_action(self, data, source='mac'):
        action = data.get('action')
        if action not in (
            'start',
            'pause',
            'restore',
            'refresh',
            'schedule-combos',
            'cancel-combos',
            'update-policy',
            'resume-demand',
            'data-gathering',
            'update-plan',
        ):
            raise ValueError('Choose a supported optimizer action.')
        with self.lock:
            if action not in ('pause', 'refresh', 'cancel-combos'):
                self.update_guard.require_available()
            if action != 'refresh' and data.get('expectedControl') != self.control_version():
                raise ValueError(
                    'The provider or test settings changed. Wait for the live status to refresh, then try again.'
                )
            if action in (
                'start',
                'restore',
                'schedule-combos',
                'update-policy',
                'resume-demand',
            ) and session_key(self.read_state()) != session_key(self.raw):
                raise ValueError(
                    'The provider changed. Wait for fresh readings before starting a test or restoring a model.'
                )
        # Each action rechecks its token in the mutation critical section.
        # Historical response construction must not hold up collection.
        return self.action(data, source)

    def require_current_control(self, data):
        if 'expectedControl' in data and data['expectedControl'] != self.control_version():
            raise ValueError(
                'The provider or test settings changed. Wait for the live status to refresh, then try again.'
            )

    def demand_resume_context(self, saved, reviewed_model=None):
        """Read-only admission shared by the visible resume status and the command."""
        now = time.time()
        raw = self.read_state()
        live = self.live or {}
        if (
            saved['mode'] != 'observe'
            or saved.get('pending')
            or saved.get('requestedModel')
            or self.worker
            and self.worker.is_alive()
        ):
            raise ValueError('Wait for the paused plan to have no queued or active switch.')
        if not finite(saved.get('startedAt')) or saved.get('endsAt') is not None:
            raise ValueError(
                'Review strategy and model selection to start a plan. Recorded history is retained.'
            )
        managed = manager.enabled(saved.get('demandPolicy'))
        # Running but drained, its launch agent still disabled: the manager's watchdog restarts
        # it (manager.restore), so the manager may turn on; Manual says what happened.
        drained = drained_idle(raw) and matching_process(process_identity(raw)) is True
        if drained and not managed:
            raise ValueError(DRAINED)
        if (
            self.service_disabled() is not False and not (managed and drained)
        ) or not live.get('provider', {}).get('online'):
            raise ValueError(
                'Open the manual model controls in Optimizer → Overview to start Darkbloom, then wait for Warm and ready before resuming.'
            )
        current, options, environment = self.read_options()
        expected = saved.get('expectedModel') if reviewed_model is None else reviewed_model
        if (
            not isinstance(expected, str)
            or current != expected
            or not same_selection(raw, current)
            or session_key(raw) != session_key(self.raw)
            or matching_process(process_identity(raw)) is not True
            or any(not live.get(k) or live[k] != saved.get(k) for k in ('account', 'device'))
            or device_id(raw) != saved.get('device')
        ):
            raise ValueError(
                'The saved plan, provider or account changed. Review it before resuming.'
            )
        # The manager turns on with a dark or cold model: its watchdog restores home. Turning
        # it On is the person's own choice, like a manual pick: battery power and the pay
        # feeds don't hold it (Sep 28 22:50, on battery). Its own later moves still check.
        reason = self.wait_reason(
            live, now, serving=not managed, move='manual' if managed else None
        )
        if reason:
            raise ValueError(reason)
        if not managed and not self.tracking(raw, now, cleared=False)['counting']:
            raise ValueError(
                'Wait for the current model to be Warm and ready before resuming. Open Optimizer → Overview to check progress.'
            )
        config_error = combination_config_error(self.home, options, environment, require_pair=False)
        # The manager turns on and holds: that error stops only its own moves, while its
        # watchdog and restores keep the Mac serving.
        if config_error and not managed:
            raise ValueError(config_error)
        if managed and manager.hold_error(current):
            raise ValueError(manager.hold_error(current))
        if self.discovery_error or now - self.discovery_at > 600:
            raise ValueError('Refresh the model catalog before resuming.')
        candidates = self.candidates({}, {}, live, saved)
        serving = next((r for r in candidates if r['id'] == current), None)
        if serving and serving['selected'] and not serving['available'] and serving.get('reason'):
            raise ValueError(serving['reason'])
        allowed = {r['id'] for r in candidates if r['available'] and r['selected']}
        pool = saved.get('models', [])
        if managed and len(members(current)) == 2 and manager.in_pool(current, pool):
            allowed.add(current)  # a held pair
        # The manager can hold the serving model alone; excursions only need alternatives.
        if current not in allowed or (len(allowed) < 2 and not managed):
            raise ValueError(
                'Review model selection: include the serving model and at least one available alternative.'
            )
        return current, live, raw, allowed

    def demand_resume_status(self):
        with self.lock:
            saved = copy.deepcopy(self.state)
            current = selection_key(self.raw.get('advertised_models'))
        result = {
            'hasSavedPlan': bool(finite(saved.get('startedAt')) and saved.get('endsAt') is None),
            'available': False,
            'currentModel': current,
            'selectedCount': len(saved.get('models', [])),
            'reason': None,
        }
        if saved.get('mode') != 'observe':
            return result
        try:
            self.update_guard.require_available()
            _, _, _, allowed = self.demand_resume_context(saved, current)
            result.update(available=True, availableCount=len(allowed))
        except ValueError as e:
            result['reason'] = str(e)
        except Exception:
            result['reason'] = (
                'Provider controls are still refreshing. Open the manual model controls in Optimizer → Overview to check the service.'
            )
        return result

    def resume_demand(self, data, source, return_snapshot=True):
        """Explicit protected action; never infer resumption from a recovered model."""
        if set(data) - {'action', 'expectedControl', 'currentModel'}:
            raise ValueError('Resume the saved plan without replacing its settings.')
        if 'currentModel' in data and (
            not isinstance(data['currentModel'], str) or not data['currentModel']
        ):
            raise ValueError('Review the current serving model before resuming.')
        now = time.time()
        with self.lock:
            saved = copy.deepcopy(self.state)
        current, live, raw, _ = self.demand_resume_context(saved, data.get('currentModel'))
        with self.lock:
            self.require_current_control(data)
            if self.state != saved or session_key(self.raw) != session_key(raw):
                raise ValueError(
                    'The saved plan or provider changed during verification. Review it before resuming.'
                )
            if current != saved.get('expectedModel'):
                # Explicitly acknowledge an outside change, without restoring the old model.
                self.state['expectedModel'] = current
                self.state['lastSwitchAt'] = now
                self.state.pop('rollbackModel', None)
            self.state['mode'] = 'demand'
            self.state.pop('demandProposal', None)
            try:
                self.save()
            except Exception:
                self.state = saved
                raise
            self.proposal = None
            self.status = 'optimizing'
            self.detail = 'Demand following resumed. The existing plan and history are preserved.'
        self.store.event(
            live['account'],
            live['device'],
            now,
            'resumed',
            current,
            'Saved demand plan resumed from '
            + source
            + ' after reviewing the serving model; plan dates, selected models, policy and history retained.',
        )
        return self.snapshot() if return_snapshot else None

    def update_demand_policy(self, data, source):
        """Tune an active plan through the same protected controls, without starting over."""
        changes = data.get('demandPolicy')
        if (
            set(data) - {'action', 'expectedControl', 'demandPolicy'}
            or not isinstance(changes, dict)
            or not changes
        ):
            raise ValueError('Provide only the demand settings to update.')
        now = time.time()
        with self.lock:
            self.require_current_control(data)
            if self.state.get('mode') != 'demand':
                raise ValueError(
                    'Settings updates require active demand following; paused experiments stay paused.'
                )
            if (
                self.state.get('pending')
                or self.state.get('requestedModel')
                or (self.worker and self.worker.is_alive())
            ):
                raise ValueError(
                    'Wait for the queued or active model switch to finish before updating settings.'
                )
            live = self.live or {}
            if (
                not live.get('account')
                or not live.get('device')
                or any(live.get(k) != self.state.get(k) for k in ('account', 'device'))
            ):
                raise ValueError(
                    'Wait for the current account and device to match the active plan.'
                )
            if (
                not live.get('provider', {}).get('online')
                or not finite(self.raw.get('written_at'))
                or not -5 < now - self.raw['written_at'] < 15
            ):
                raise ValueError(
                    'Wait for fresh online provider readings before updating settings.'
                )
            previous = demand_policy(self.state.get('demandPolicy'))
            rules = demand_policy({**previous, **changes})
            changed = {k: (previous[k], rules[k]) for k in rules if previous[k] != rules[k]}
            if changed:
                saved = copy.deepcopy(self.state)
                self.state['demandPolicy'] = rules
                self.state.pop('demandProposal', None)
                manager.policy_saved(self.state, previous, rules, now)
                try:
                    self.save()
                except Exception:
                    self.state = saved
                    raise
                self.proposal = None
                self.status = 'optimizing'
                self.detail = (
                    'Demand settings updated. The active model, plan and saved history continue.'
                )
                detail = ', '.join(
                    '%s: %s -> %s' % (k, *values) for k, values in sorted(changed.items())
                )
                self.store.event(
                    live['account'],
                    live['device'],
                    now,
                    'demand-policy-updated',
                    self.state.get('expectedModel'),
                    detail
                    + '. Active plan retained; updated from '
                    + ('phone.' if source == 'phone' else 'Mac.'),
                )
        return self.snapshot()

    def update_plan(self, data, source):
        """Save Follow demand's models and rules: live while on, stored for the next On while Manual.

        Never switches a model. Scheduled tests and an On that is still starting keep their plan.
        """
        if set(data) - {'action', 'expectedControl', 'models', 'demandPolicy'} or not set(data) & {
            'models',
            'demandPolicy',
        }:
            raise ValueError('Provide the models or demand settings to save.')
        now = time.time()
        with self.lock:
            self.require_current_control(data)
            mode = self.state.get('mode')
            if mode not in ('observe', 'demand'):
                raise ValueError('Switch to Manual before changing a scheduled test.')
            if (
                self.state.get('pending')
                or self.state.get('requestedModel')
                or (self.worker and self.worker.is_alive())
            ):
                raise ValueError(
                    'Wait for the queued or active model switch to finish before saving.'
                )
            if (getattr(getattr(self, 'automatic_control', None), 'operation', None) or {}).get(
                'status'
            ) in ('pending', 'starting', 'waiting'):
                raise ValueError('Wait for the optimizer to finish turning on before saving.')
            live = self.live or {}
            if not live.get('account') or not live.get('device'):
                raise ValueError('Wait for this Mac and account to be verified before saving.')
            models = data.get('models', self.state.get('models') or [])
            # The manager can hold one model; legacy demand following compares two or more.
            edits = data.get('demandPolicy')
            policy = dict(self.state.get('demandPolicy') or {})
            policy.update(edits if isinstance(edits, dict) else {})
            fewest = 1 if manager.enabled(policy) else 2
            if 'models' in data and (
                not isinstance(models, list)
                or not fewest <= len(models) <= 16
                or len(set(models)) != len(models)
                or any(not isinstance(m, str) or not m for m in models)
            ):
                raise ValueError('Choose %s to sixteen models.' % ('one' if fewest == 1 else 'two'))
            current = selection_key(self.raw.get('advertised_models'))
            if mode == 'demand' and 'models' in data and not manager.in_pool(current, models):
                raise ValueError('Keep the serving model selected while the optimizer is on.')
            previous = demand_policy(self.state.get('demandPolicy'))
            changes = data.get('demandPolicy', {})
            if not isinstance(changes, dict):
                raise ValueError('Choose supported demand-switching controls.')
            rules = demand_policy({**previous, **changes})
            saved = copy.deepcopy(self.state)
            self.state['models'] = list(models)
            self.state['demandPolicy'] = rules
            self.state.pop('demandProposal', None)
            ended = mode == 'observe' and finite(self.state.get('startedAt')) and (
                self.state.get('endsAt') is not None
            )
            if ended:
                # A finished or paused seven-day test: On blocks on it ("needs a new plan.
                # Review its settings"), and these reviewed settings are that new plan. Clearing
                # the old dates makes the next On a first On with them; without it On asked
                # for the same review forever.
                self.state.update(startedAt=None, endsAt=None)
            manager.policy_saved(self.state, previous, rules, now)
            try:
                self.save()
            except Exception:
                self.state = saved
                raise
            self.proposal = None
            detail = (
                'Plan updated while the optimizer is on; the current model keeps serving.'
                if mode == 'demand'
                else 'Plan saved for the next time the optimizer is turned on.'
            )
            self.store.event(
                live['account'],
                live['device'],
                now,
                'plan-updated',
                current,
                detail
                + ' %d models. Changed from %s.'
                % (len(models), 'phone' if source == 'phone' else 'Mac'),
            )
        return self.snapshot()

    def set_data_gathering(self, data, source):
        """Start (24h/3d/7d) or stop the timed trial-allowance increase. Never switches a model."""
        if set(data) - {'action', 'expectedControl', 'seconds'} or 'seconds' not in data:
            raise ValueError('Choose a data-gathering duration, or stop it.')
        now = time.time()
        with self.lock:
            self.require_current_control(data)
            live = self.live or {}
            if not live.get('account') or not live.get('device'):
                raise ValueError(
                    'Wait for this Mac and account to be verified before changing learning boost.'
                )
            saved = copy.deepcopy(self.state)
            if data['seconds'] and manager.enabled(self.state.get('demandPolicy')):
                raise ValueError(
                    'Learning boost is off while BloomGauge holds a home model (manager strategy).'
                )
            if data['seconds'] == 0:
                if not data_gathering.status(self.state.get('dataGathering'), now)['active']:
                    raise ValueError('Learning boost is not running.')
                self.state.pop('dataGathering', None)
                detail = 'Learning boost stopped. Normal trial limits apply.'
            else:
                self.state['dataGathering'] = data_gathering.start(data['seconds'], now)
                detail = (
                    'Learning boost started for %s. More learning runs are allowed; safety checks are unchanged.'
                    % ({86400: '24 hours', 259200: '3 days', 604800: '7 days'}[data['seconds']])
                )
            try:
                self.save()
            except Exception:
                self.state = saved
                raise
            self.store.event(
                live['account'],
                live['device'],
                now,
                'data-gathering',
                self.state.get('expectedModel'),
                detail + ' Changed from ' + ('phone.' if source == 'phone' else 'Mac.'),
            )
        return self.snapshot()

    def action(self, data, source='mac'):
        action = data.get('action')
        if action == 'resume-demand':
            return self.resume_demand(data, source)
        if action == 'update-policy':
            return self.update_demand_policy(data, source)
        if action == 'data-gathering':
            return self.set_data_gathering(data, source)
        if action == 'update-plan':
            return self.update_plan(data, source)
        if action in ('schedule-combos', 'cancel-combos'):
            return self.combo_action(data, source)
        if action == 'pause':
            self.pause_automatic(source, data)
            return self.snapshot()
        if action == 'refresh':
            with self.lock:
                self.next_discovery = 0
                self.next_identity = 0
            return self.snapshot()
        if action not in ('start', 'restore'):
            raise ValueError('Unknown optimizer action.')
        snapshot = self.snapshot()
        now = time.time()
        with self.lock:
            self.require_current_control(data)
            if self.state.get('pending'):
                raise ValueError('A model switch is already in progress.')
            if self.state.get('requestedModel'):
                raise ValueError('A switch is already queued. Cancel it or wait for it to finish.')
            live = copy.deepcopy(self.live) or {}
            if self.state['mode'] != 'observe':
                raise ValueError('Pause automatic switching before changing the experiment.')
        if snapshot['controlError']:
            raise ValueError(snapshot['controlError'])
        if not snapshot['identityVerified']:
            raise ValueError('Wait for this Mac to be matched to the public provider roster.')
        if not live.get('provider', {}).get('online'):
            raise ValueError('Start Darkbloom on this Mac first.')
        current, _, _ = self.read_options()
        if action == 'restore':
            original = self.state.get('originalModel')
            if not original:
                raise ValueError('No previous model has been saved.')
            if not any(m['id'] == original and m['available'] for m in snapshot['models']):
                raise ValueError('The previous model is no longer available locally.')
            with self.lock:
                self.require_current_control(data)
                if (
                    self.state.get('pending')
                    or self.state.get('requestedModel')
                    or self.state['mode'] != 'observe'
                ):
                    raise ValueError('Switch settings changed. Refresh and try again.')
                self.state.update(
                    mode='observe',
                    requestedModel=original,
                    requestedKind='restore',
                    requestedAt=time.time(),
                    expectedModel=current,
                )
                self.state['account'] = live['account']
                self.state['device'] = live['device']
                self.status = 'waiting'
                self.detail = 'Restore requested. Waiting for fresh readings and an idle period.'
                self.save()
            return self.snapshot()
        mode = data.get('mode')
        models = data.get('models')
        hours = data.get('blockHours', 2)
        if mode not in ('week', 'optimize', 'demand'):
            raise ValueError('Choose a week test, historical optimization, or demand following.')
        if type(hours) is not int or hours not in (2, 4):
            raise ValueError('Choose two- or four-hour model runs.')
        maximum = 16 if mode == 'demand' else 6
        if (
            not isinstance(models, list)
            or not 2 <= len(models) <= maximum
            or any(not isinstance(m, str) for m in models)
            or len(set(models)) != len(models)
        ):
            raise ValueError(
                'Select two to ' + str(maximum) + ' distinct, locally available models.'
            )
        rules = demand_policy(data.get('demandPolicy', self.state.get('demandPolicy')))
        if mode != 'demand' and manager.enabled(rules):
            # Legacy strategies, hidden under the Manager: they'd pause on any failed switch.
            raise ValueError(
                'Seven-day tests and historical optimization need the legacy strategy. The Manager replaces them; choose the legacy strategy first.'
            )
        if mode == 'demand' and snapshot.get('demandAuto', {}).get('controlError'):
            raise ValueError(snapshot['demandAuto']['controlError'])
        allowed = {m['id'] for m in snapshot['models'] if m['available']}
        if any(m not in allowed for m in models):
            raise ValueError('Only supported models already available on this Mac can be tested.')
        if current not in models:
            raise ValueError('Include your current model as the comparison baseline.')
        if self.discovery_error or now - self.discovery_at > 600:
            raise ValueError('Refresh the model catalog before starting.')
        models = [current] + [m for m in models if m != current]
        with self.lock:
            self.require_current_control(data)
            if (
                self.state.get('pending')
                or self.state.get('requestedModel')
                or self.state['mode'] != 'observe'
            ):
                raise ValueError('Optimizer settings changed. Pause switching and try again.')
            self.state.update(
                mode=mode,
                models=models,
                blockHours=hours,
                startedAt=now,
                endsAt=now + 604800 if mode == 'week' else None,
                originalModel=current,
                expectedModel=current,
                lastSwitchAt=now,
                requestedModel=None,
                account=live['account'],
                device=live['device'],
            )
            self.state['demandPolicy'] = rules
            self.state.pop('demandProposal', None)
            if (
                mode == 'demand'
                and finite(self.raw.get('started_at'))
                and 0 < self.raw['started_at'] <= now
            ):
                self.state['lastSwitchAt'] = self.raw['started_at']
            self.status = 'learning' if mode == 'week' else 'optimizing'
            self.detail = 'Keeping the current model for the first run.'
            self.proposal = None
            self.save()
        self.store.event(
            live['account'],
            live['device'],
            now,
            'started',
            current,
            (
                'Seven-day test started'
                if mode == 'week'
                else 'Demand following enabled'
                if mode == 'demand'
                else 'Automatic optimization enabled'
            )
            + (' from phone.' if source == 'phone' else ' from Mac.'),
        )
        return self.snapshot()

    def refresh_loop(self):
        """Slow CLI/network discovery must not delay a ready queued selection."""
        statistics_at = time.time()
        while not self.stop.is_set():
            try:
                self.refresh(time.time())
            except Exception:
                log.exception('Optimizer refresh failed')
                pass  # Individual discovery/identity reads expose their own failures.
            if time.time() - statistics_at >= STATISTICS_SECONDS:
                statistics_at = time.time()
                try:
                    self.store.refresh_statistics()
                except Exception:
                    pass  # Stale planner statistics only slow queries down.
            self.stop.wait(15)

    def control_interval(self):
        with self.lock:
            return 1 if self.state.get('requestedModel') and not self.state.get('pending') else 15

    def loop(self):
        # Interval on the monotonic clock: a backward wall-clock jump must not
        # stop ticks until the clock catches up.
        last_tick = -math.inf
        while not self.stop.is_set():
            if time.monotonic() - last_tick < self.control_interval():
                self.stop.wait(1)
                continue
            last_tick = time.monotonic()
            now = time.time()
            try:
                self.tick(now)
            except Exception:
                log.exception('Optimizer tick failed')
                with self.lock:
                    self.status = 'waiting'
                    self.detail = (
                        'Could not evaluate the optimizer. Keeping the current model and retrying.'
                    )
            self.stop.wait(1)

    def wait_reason(self, live, now, manual=False, serving=True, move=None, target=None):
        """`serving=False` accepts this Mac's roster match without serving eligibility,
        which a cold on-demand model may not have yet (manager recovery only). `move` and
        `target`: see environment_reason."""
        if not live or now - live.get('at', 0) > 10:
            return 'Waiting for fresh local readings.'
        if not live.get('provider', {}).get('online'):
            return 'Provider is offline. Start it in Darkbloom to resume; the optimizer will not start a stopped service.'
        if self.identity_session != (self.raw.get('started_at'), self.raw.get('pid')):
            return 'Waiting to verify the new provider session.'
        if not 0 <= now - self.identity_at < 180:
            return 'Waiting for a fresh match between this Mac and the provider roster.'
        if not self.identity_ok and (serving or not self.device_identity_ok):
            return (
                self.identity_detail
                or 'Waiting for a fresh match between this Mac and the provider roster.'
            )
        try:
            _, options, _ = self.read_options()
            if endpoint_issue(options) and not (
                manual and self.manual_selection.endpoint_setup_allowed(options)
            ):
                return self.endpoint_notice(options)
        except (OSError, ValueError, KeyError, TypeError, plistlib.InvalidFileException):
            return 'Provider launch settings need review in the manual model controls in Optimizer → Overview on the Mac.'
        return self.environment_reason(live, now, manual, move, target)

    def environment_reason(self, live, now, manual=False, move=None, target=None):
        """Shared resource/source checks; callers must independently prove identity.

        `move` names a move that restores or follows a choice instead of chasing pay:
        'restore' (watchdog or failed-switch recovery), 'home' (the manager's return home,
        including the end of an excursion) or 'manual' (an explicit pick). Those skip the
        earnings and network feeds, the 95 °C line and battery power: Darkbloom serves on
        battery, and macOS thermal state Serious or Critical still holds every move. A
        'restore' or 'home' `target` this Mac has served before also skips the catalog age.
        """
        voluntary = move is None
        if not live or not finite(live.get('at')) or not -5 < now - live['at'] < 10:
            return 'Waiting for fresh local readings.'
        if not live.get('provider', {}).get('online'):
            return 'Provider is offline. Start it in Darkbloom to resume; the optimizer will not start a stopped service.'
        if (
            not manual
            and voluntary
            and (
                live.get('earnings', {}).get('status') != 'ok'
                or now - (live.get('earnings', {}).get('updatedAt') or 0) > 180
            )
        ):
            return 'Waiting for current earnings before changing models.'
        cap = self.network.snapshot('capacity')
        if (
            not manual
            and voluntary
            and (cap.get('status') != 'ok' or now - cap.get('updatedAt', 0) > 120)
        ):
            return 'Network demand is stale. Keeping the current model.'
        if (self.discovery_error or now - self.discovery_at > 600) and not self.known_target(
            move, target, live, now
        ):
            return 'Waiting for a fresh model catalog.'
        h = live.get('hardware', {})
        if any(
            not finite(h.get(k))
            for k in ('memoryUsedGB', 'memoryTotalGB', 'memoryAvailableGB', 'cpuTemp', 'gpuTemp')
        ):
            return 'Waiting for current hardware readings.'
        if h.get('thermal') in ('Serious', 'Critical') or (
            voluntary and max(h['cpuTemp'], h['gpuTemp']) >= 95
        ):
            return 'The Mac is hot. Holding the current model until temperatures settle.'
        if voluntary and self.on_ac_power() is False:
            return 'Model switching waits while the Mac is on battery power.'
        return None

    def known_target(self, move, target, live, now):
        """A return-home or restore target this Mac has served before: its files and catalog
        entry were verified then, so an overdue catalog refresh does not hold it."""
        device = (live or {}).get('device')
        models = members(target) if move in ('home', 'restore') and device else []
        return bool(models) and all(self.served_here(device, m, now) for m in models)

    def on_ac_power(self):
        """True on AC power, False on battery; None when pmset fails or times out (unknown,
        which never counts as battery)."""
        try:
            r = self.runner(
                ['/usr/bin/pmset', '-g', 'batt'],
                capture_output=True,
                text=True,
                timeout=3,
                check=True,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return "'AC Power'" in r.stdout if isinstance(r.stdout, str) else None

    def record_decision(self, now):
        with self.lock:
            state = copy.deepcopy(self.state)
            live = copy.deepcopy(self.live) or {}
            raw = copy.deepcopy(self.raw)
            status, reason = self.status, self.detail
            decision = copy.deepcopy(self.last_demand_decision) or {}
        if now - decision.get('at', 0) > 90 or decision.get('currentModel') != selection_key(
            raw.get('advertised_models')
        ):
            decision = {}
        pending = state.get('pending') or {}
        confirmation = state.get('demandProposal') or {}
        target = (
            pending.get('model')
            or state.get('requestedModel')
            or confirmation.get('model')
            or decision.get('target')
        )
        if state['mode'] == 'observe' and not target:
            phase = 'off'
        elif pending:
            phase = 'switching'
        elif reason_code(reason) == 'confirmation':
            phase = 'confirming'
        elif status == 'waiting':
            phase = 'waiting'
        else:
            phase = 'watching'
        if (
            phase == 'watching'
            and state['mode'] == 'demand'
            and not target
            and not decision.get('explorationTrigger')
            and not decision.get('spikeReview')
            and not (decision.get('trial') or {}).get('current')
        ):
            goal = decision.get('earningsTarget') or {}
            if goal.get('status') in ('learning', 'stale'):
                reason = goal['reason']
                phase = 'waiting'
        self.decisions.record(
            live.get('account', ''),
            live.get('device', ''),
            now,
            state['mode'],
            selection_key(raw.get('advertised_models')),
            target,
            phase,
            reason,
            decision,
            confirmation,
        )

    def tick(self, now):
        try:
            self._tick(now)
        except Exception:
            with self.lock:
                self.status = 'waiting'
                self.detail = (
                    'Could not evaluate the optimizer. Keeping the current model and retrying.'
                )
            raise
        finally:
            self.record_decision(now)

    def log_network_outage(self, account, device, now):
        """One activity-log line when a Darkbloom outage that concerns this Mac starts, and
        one when it ends (network_health.py; the stall ladder waits it out)."""
        health = self.network_health
        if health is None:
            return
        try:
            outage = health.outage(now)
            signal = health.signal(now) if outage else None
        except Exception:
            log.exception('Network health unavailable to the activity log')
            return
        logged = self.outage_logged.get((account, device))
        key = outage and (outage['scope'], outage['key'], outage['since'])
        if key and key != (logged or {}).get('key'):
            self.store.event(
                account,
                device,
                now,
                'network-outage',
                None,
                network_health.describe(outage, signal)
                + (
                    ' BloomGauge won’t restart the provider for it; moving to another model is still allowed.'
                    if outage['scope'] == 'model'
                    else ' Nothing to fix on this Mac: BloomGauge won’t restart the provider or switch models because of it.'
                ),
            )
            self.outage_logged[(account, device)] = {'key': key, 'since': outage['since']}
        elif not outage and logged:
            # "Is over" only on a real recovery. A problem that only went quiet (no fresh
            # readings on it, or its model left the list) or became the new normal says so;
            # one still open that no longer concerns this Mac waits for its end.
            scope, key, since = logged['key']
            try:
                status = getattr(health, 'status_of', lambda *a: 'recovered')(scope, key, since)
            except Exception:
                log.exception('Network health unavailable to the activity log')
                return
            if status == 'open':
                return
            began = network_health.clock_time(logged['since'])
            if status == 'recovered':
                kind, text = 'network-recovered', (
                    'The Darkbloom network problem that began at %s is over.' % began
                )
            elif status == 'expired':
                kind, text = 'network-untracked', (
                    'The Darkbloom network problem that began at %s has lasted %d hours: '
                    'BloomGauge takes it as the new normal and no longer holds restarts or '
                    'model switches for it.' % (began, network_health.MAX_SECONDS // 3600)
                )
            else:
                kind, text = 'network-untracked', (
                    'BloomGauge stopped tracking the Darkbloom network problem that began at %s: '
                    'there are no fresh readings on it, so it may not be over. BloomGauge no '
                    'longer holds restarts or model switches for it.' % began
                )
            self.store.event(account, device, now, kind, None, text)
            del self.outage_logged[(account, device)]

    def restore_drained(self, settings, live, raw, now):
        """Restart a provider that a failed start left drained, even while paused.

        Only after BloomGauge's own start failed, only once the drain has finished and
        stayed finished (a running start would have restarted within seconds), and
        only on the selection the provider is already configured for.
        """
        failure = settings.get('lastSwitchFailure') or {}
        eligible = bool(
            failure.get('stage') == 'start'
            and failure.get('recovery') != 'restored'
            and not failure.get('drainedRestartAt')
            and finite(failure.get('at'))
            and 0 <= now - failure['at'] < DRAINED_RESTORE_WINDOW
            and not settings.get('pending')
            and live.get('account')
            and live.get('device')
            and drained_idle(raw)
        )
        with self.lock:
            if not eligible:
                self.drained_since = None
                return False
            if self.drained_since is None:
                self.drained_since = now
            if now - self.drained_since < DRAINED_RESTORE_SECONDS:
                return False
        if not self.command_lock.acquire(blocking=False):
            return False
        try:
            selection, options, environment = self.read_options()
            latest = self.read_state()
            if not drained_idle(latest) or not same_selection(latest, selection):
                return False
            with self.lock:
                if self.state.get('pending') or self.state.get('lastSwitchFailure') != failure:
                    return False
                # One attempt per failure, recorded before it runs.
                failure = self.state['lastSwitchFailure'] = {**failure, 'drainedRestartAt': now}
                self.drained_since = None
                self.save()
            try:
                self.command(selection, options, environment)
                restarted = True
            except Exception:
                log.exception('Could not restart a drained provider')
                restarted = False
            detail = (
                'Darkbloom was left drained after the failed switch and served no work. Restarted '
                + selection_label(selection)
                + (
                    '. Automatic control continues.'
                    if manager.active(settings)
                    else '. Automatic switching stays paused.'
                )
                if restarted
                else 'Darkbloom was left drained after the failed switch, and restarting '
                + selection_label(selection)
                + ' failed. Choose a model in Manual control or run `darkbloom restart`.'
            )
            with self.lock:
                if restarted and self.state.get('lastSwitchFailure') == failure:
                    # Existing values only: diagnostics and the report site validate them.
                    self.state['lastSwitchFailure'] = {
                        **failure,
                        'recovery': 'restored',
                        'recoveryCode': 'restored',
                    }
                    self.save()
                self.detail = detail
            self.store.event(
                live['account'],
                live['device'],
                now,
                'recovered' if restarted else 'failed',
                selection,
                detail,
            )
            return True
        finally:
            self.command_lock.release()

    def _tick(self, now):
        self.manager.clock_tick(now)
        with self.lock:
            if self.update_guard.active():
                return
        warming = self.tick_prewarm(now)
        with self.lock:
            settings = copy.deepcopy(self.state)
            live = copy.deepcopy(self.live) or {}
            raw = copy.deepcopy(self.raw)
            watch_manual = (
                settings['mode'] == 'observe'
                and settings.get('manualResult', {}).get('status') == 'completed'
                and now - settings.get('lastSwitchAt', 0) < 600
            )
        if self.restore_drained(settings, live, raw, now):
            return
        if live.get('account') and live.get('device'):
            self.log_network_outage(live['account'], live['device'], now)
        if live.get('account') and live.get('device'):
            self.demand_auto.trials.review(
                live['account'],
                live['device'],
                self.demand_auto.runs(live['account'], live['device'], now, 100),
                raw,
                now,
            )
        with self.lock:
            if settings.get('pending') or (
                settings['mode'] == 'observe'
                and not settings.get('requestedModel')
                and not watch_manual
            ):
                return
        account = live.get('account', '')
        device = live.get('device', '')
        if not live or not raw or not account or not device:
            with self.lock:
                self.status = 'waiting'
                self.detail = 'Waiting for the first complete account and provider readings. The saved test schedule is preserved.'
            return
        if account != settings.get('account') or device != settings.get('device'):
            if account == settings.get('account') and manager.active(settings):
                # Same account, new Darkbloom device key: hold, re-verify, then continue.
                return self.manager.device_changed(now, settings, account, device)
            if account == settings.get('account') and settings['mode'] == 'observe':
                # Manual, watching a pick that restarted Darkbloom with a new device key: there
                # is nothing to pause (it was paused every tick, with an event each time). Keep
                # the new key once the roster matches it, as the manager does.
                with self.lock:
                    if (
                        self.device_identity_ok
                        and self.identity_device == device
                        and 0 <= now - self.identity_at < 180
                        and self.state.get('device') == settings.get('device')
                    ):
                        self.state['device'] = device
                        self.save()
                return
            detail = (
                'This Mac is signed in to a different Darkbloom account, so automatic control is off. Turn it on again to run it for this account.'
                if account != settings.get('account')
                else 'The account or device identity changed. Automatic switching is paused.'
            )
            self.pause_internal(detail)
            # The event is what the Off card shows as the reason, after a restart too.
            self.store.event(account, device, now, 'paused', settings.get('expectedModel'), detail)
            return
        managed = manager.active(settings)
        try:
            current, current_options, current_environment = self.read_options()
        except Exception:
            if managed:
                with self.lock:
                    self.status = 'waiting'
                    self.detail = 'The provider launch settings are unreadable or unsupported right now. The manager holds and rechecks; nothing is switched.'
                if live.get('provider', {}).get('online'):
                    # The watchdog keeps watching readiness: it reports a dark Mac, and its
                    # restore (which reads the settings again) waits until they can be read.
                    self.manager.tick(
                        now,
                        settings,
                        live,
                        raw,
                        selection_key(raw.get('advertised_models'))
                        or settings.get('expectedModel'),
                        [],
                        {},
                    )
                return
            return self.pause_internal(
                'The provider launch settings changed or are unavailable. Automatic switching is paused.'
            )
        if current != settings.get('expectedModel') or (
            live.get('provider', {}).get('online') and not same_selection(raw, current)
        ):
            if managed:
                # Adopt what serves; a stopped or unreadable selection still gets the watchdog.
                if not self.manager.external(now, settings, live, raw, current):
                    self.manager.tick(
                        now, settings, live, raw, current, current_options, current_environment
                    )
                return
            return self.pause_internal(
                'You changed the serving models outside the dashboard. Automatic switching is paused.'
            )
        if (
            settings.get('requestedModel')
            and settings.get('requestedKind') == 'manual'
            and (
                (
                    settings.get('requestedSession')
                    and settings['requestedSession'] != session_key(raw)
                )
                or (
                    settings.get('requestedLaunchSignature')
                    and settings['requestedLaunchSignature']
                    != launch_signature(current_options, current_environment)
                )
            )
        ):
            resume = (settings.get('manager') or {}).get('resume') or {}
            self.pause_internal(
                'The provider session or launch settings changed while the switch was queued. Refresh and select the model again.'
            )
            if resume and resume.get('id') == settings.get('requestId'):
                with self.lock:
                    self.state['mode'] = 'demand'  # the pick was cancelled; the manager resumes
                    self.save()
            return
        if warming:
            return
        failure = raw.get('last_model_load_error') or {}
        if (
            (settings['mode'] != 'observe' or watch_manual)
            and settings.get('rollbackModel')
            and failure.get('model') in members(current)
            and finite(failure.get('at'))
            and failure['at'] >= settings.get('lastSwitchAt', now)
            and now - settings.get('lastSwitchAt', 0) < 600
            and failure.get('model') not in raw.get('warm_models', [])
        ):
            if managed:
                self.store.event(
                    account,
                    device,
                    now,
                    'load-failed',
                    current,
                    'Darkbloom reported a model-load failure after reconnecting. The manager restores the previous model unless it recovers.',
                )
                return self.manager.load_failed(now, settings, current)
            self.store.event(
                account,
                device,
                now,
                'load-failed',
                current,
                'Darkbloom reported a model-load failure after reconnecting. Pausing the experiment and requesting the previous model.',
            )
            with self.lock:
                self.state.update(
                    mode='observe',
                    requestedModel=settings['rollbackModel'],
                    requestedKind='manual-recovery' if watch_manual else 'restore',
                    requestedAt=now,
                )
                self.state.pop('rollbackModel', None)
                self.status = 'waiting'
                self.detail = 'The model failed to load after reconnecting. Restore requested; check memory and cache if recovery also fails.'
                self.cancel_combo(self.detail)
                if watch_manual:
                    self.state['requestId'] = settings['manualResult']['id']
                    self.state['manualResult'] = {
                        **settings['manualResult'],
                        'status': 'recovering',
                        'detail': self.detail,
                    }
                self.save()
            return
        if settings['mode'] == 'observe' and not settings.get('requestedModel'):
            return
        if managed and self.manager.tick(
            now, settings, live, raw, current, current_options, current_environment
        ):
            return
        if settings['mode'] in ('week', 'combo') and not settings.get('requestedModel'):
            held = self.scheduled_trial_hold(now, live, raw)
            if held['active']:
                with self.lock:
                    self.status = 'learning'
                    self.detail = held['reason']
                    self.next_switch = None
                return
        if settings['mode'] == 'week' and now >= settings['endsAt']:
            if self.begin_combos(now, settings, live, current):
                return
            self.store.event(
                account,
                device,
                now,
                'completed',
                current,
                'Seven-day test completed. Review measured coverage, then enable optimization.',
            )
            return self.pause_internal(
                'The seven-day test is complete. Review the results; the current model stays running.'
            )
        if settings['mode'] == 'combo' and now >= (settings.get('comboPlan') or {}).get(
            'endsAt', 0
        ):
            with self.lock:
                self.state['comboPlan'].update(
                    status='completed',
                    endedAt=now,
                    detail='Combination comparison complete. Review observed earnings and both-warm coverage.',
                )
                self.save()
            self.store.event(
                account,
                device,
                now,
                'combos-completed',
                current,
                'Combination comparison completed; current selection stays running.',
            )
            return self.pause_internal(
                'Combination comparison complete. Automatic switching is paused.'
            )
        manual_pick = (settings.get('requestedKind') or '').startswith('manual')
        reason = self.wait_reason(live, now, manual_pick, move='manual' if manual_pick else None)
        home = ((settings.get('manager') or {}).get('home') or {}).get('model')
        if (
            reason
            and managed
            and not settings.get('requestedModel')
            and not self.wait_reason(live, now, move='home', target=home)
        ):
            # Only a check for voluntary moves failed: the manager may still return home.
            return self.tick_demand(
                now, settings, live, raw, current, current_options, current_environment, reason
            )
        if reason:
            return self.wait_for(reason, settings, raw, now)
        if settings['mode'] == 'demand':
            return self.tick_demand(
                now, settings, live, raw, current, current_options, current_environment
            )
        target = settings.get('requestedModel')
        verify_runtime = bool(
            target
            and settings.get('requestedKind') == 'manual'
            and settings.get('requestedVerifyRuntime') is True
        )
        if target:
            # A deliberate target does not need the expensive historical ranking.
            # Keep the same supported-model and pair configuration screening.
            candidates = self.candidates({}, {}, live, settings, manual=verify_runtime)
            available = {m['id']: m for m in candidates if m['available']}
            if len(members(target)) == 2 and self.selection_budget(target, live, raw):
                available[target] = {'id': target}
        else:
            summary = self.snapshot(now - 604800, now)
            candidates = summary['models']
            available = {m['id']: m for m in candidates if m['available']}
            available.update(
                {
                    m['id']: m
                    for m in summary.get('combinations', {}).get('candidates', [])
                    if m['available']
                }
            )
        if target:
            detail = (
                'Following your manual model selection.'
                if settings.get('requestedKind') == 'manual'
                else 'Restoring the previous model.'
            )
        elif settings['mode'] == 'week':
            target, next_at = planned_model(
                settings['models'], settings['startedAt'], now, settings['blockHours']
            )
            detail = 'Following the seven-day rotation. Each day shifts models across time slots.'
            with self.lock:
                self.next_switch = next_at
        elif settings['mode'] == 'combo':
            plan = settings['comboPlan']
            target, next_at = planned_model(
                plan['configurations'], plan['startedAt'], now, plan['blockHours']
            )
            detail = 'Comparing model pairs with a solo reference across changing time slots.'
            with self.lock:
                self.next_switch = next_at
        else:
            target, detail = best_candidate(candidates, current)
        with self.lock:
            self.detail = detail
            self.status = 'learning' if settings['mode'] in ('week', 'combo') else 'optimizing'
        if (
            not target
            or target == current
            and not self.manual_selection.endpoint_setup_allowed(current_options)
        ):
            with self.lock:
                self.proposal = None
                if settings.get('requestedModel'):
                    self.state.update(requestedModel=None, requestedKind=None, requestId=None)
                    self.save()
                    self.status = 'observing'
            return
        if target not in available:
            with self.lock:
                self.status = 'waiting'
                self.detail = (
                    'The scheduled model is no longer eligible. Keeping the current model.'
                )
            return
        if (
            not settings.get('requestedModel')
            and now - settings.get('lastSwitchAt', 0) < settings['blockHours'] * 3600
        ):
            with self.lock:
                self.detail = 'Keeping the current model until its minimum run time is complete.'
            return
        if settings['mode'] == 'optimize':
            if not self.proposal or self.proposal[0] != target:
                self.proposal = (target, now)
            if now - self.proposal[1] < 900:
                with self.lock:
                    self.detail = 'Checking that the better earnings rate persists for 15 minutes before switching.'
                return
        budget = (
            self.selection_budget(target, live, raw, manual=True)
            if verify_runtime
            else self.selection_budget(target, live, raw)
        )
        if not budget or budget['afterUnloadGB'] < budget['requiredGB']:
            with self.lock:
                self.status = 'waiting'
                self.detail = 'Not enough available memory for the next model, including memory released by unloading. Waiting for memory to free up; see Memory & cache below.'
                self.proposal = None
            return
        with self.lock:
            if self.update_guard.active():
                return
            idle_ready = self.idle_since is not None and now - self.idle_since >= 12
            timeout_due = manual_pause_due(settings, now)
            automatic = not settings.get('requestedModel') and settings['mode'] in (
                'week',
                'combo',
                'optimize',
            )
            # A draining start finishes accepted requests itself, so there is no
            # need to wait for an idle moment that may not come.
            if not automatic and not idle_ready and not timeout_due and not graceful_drain(raw):
                self.status = 'waiting'
                self.detail = (
                    'Waiting for an idle period. After five minutes queued, BloomGauge will pause the provider and switch; active requests may be interrupted.'
                    if manual_pause_at(settings) is not None
                    else 'Waiting for an idle period before restarting with the next model.'
                )
                return
            if any(
                self.state.get(k) != settings.get(k)
                for k in (
                    'mode',
                    'requestedModel',
                    'requestedKind',
                    'requestId',
                    'requestedAt',
                    'requestedVerifyRuntime',
                )
            ):
                return
            self.state['pending'] = {
                'model': target,
                'previous': current,
                'at': now,
                'kind': 'automatic' if automatic else settings.get('requestedKind'),
                'automaticMode': settings['mode'] if automatic else None,
                'requestId': settings.get('requestId'),
                'requestedAt': settings.get('requestedAt'),
                'afterIdleTimeout': timeout_due,
                'session': session_key(raw),
                'launchSignature': launch_signature(current_options, current_environment),
                'verifyRuntime': verify_runtime,
                'selectionAction': (settings.get('selectionRequest') or {}).get('id')
                == settings.get('requestId')
                and bool(settings.get('requestId')),
            }
            self.save()
            self.status = 'switching'
            self.detail = (
                'Five-minute idle wait reached. Pausing the provider, switching models, then pre-warming and checking readiness.'
                if timeout_due
                else 'Switching models, pre-warming the selected model, and checking readiness.'
            )
            self.worker = threading.Thread(
                target=self.switch, args=(current, target, account, device), daemon=False
            )
            self.worker.start()

    def wait_for(self, reason, settings, raw, now):
        """Hold with `reason`. A passing freshness gap keeps the demand confirmation."""
        with self.lock:
            self.status = 'waiting'
            self.detail = reason
            self.proposal = None
            previous = self.state.get('demandProposal')
            transient = reason in (
                'Waiting for fresh local readings.',
                'Waiting for current earnings before changing models.',
                'Network demand is stale. Keeping the current model.',
                'Waiting for current hardware readings.',
            )
            retained = (
                pause_confirmation(previous, self.confirmation_scope(self.state, raw), now, reason)
                if settings['mode'] == 'demand' and transient
                else None
            )
            if retained != previous:
                if retained:
                    self.state['demandProposal'] = retained
                else:
                    self.state.pop('demandProposal', None)
                self.save()

    def tick_demand(self, now, settings, live, raw, current, options, environment, blocked=None):
        """Confirm successive source observations, then use the guarded switch path.

        `blocked`: a check only voluntary moves need failed (earnings or network feed, catalog
        age, the 95 °C line, battery power). The manager's return home still goes ahead; any
        other decision waits with that reason."""
        if not blocked:
            try:
                if self.stall.tick(now, settings, live, raw, current, options, environment):
                    return
            except Exception:
                log.exception('Stall recovery tick failed')
                # Stall recovery is an extra; a failure there never blocks demand following.
                pass
        decision = self.demand_decision(now, settings, live, raw)
        target = decision['target']
        rules = decision['policy']
        managed = bool((decision.get('manager') or {}).get('active'))
        # A return home (or the end of an excursion) uses no demand data: no confirmation,
        # fresh feeds or minimum run, and the model it leaves need not count (it may be
        # resting after Darkbloom's idle unload). Measuring and leaving home still need it.
        home_return = managed and decision.get('kind') == 'home' and bool(target)
        if blocked and home_return:
            blocked = self.wait_reason(live, now, move='home', target=target)
        if blocked:
            return self.wait_for(blocked, settings, raw, now)
        with self.lock:
            self.last_demand_decision = copy.deepcopy(decision)
        self.live_projection.record(decision, settings, live, raw)
        if managed and self.tracking(raw, now, cleared=False)['counting']:
            self.manager.remember(decision, now)
        if not self.tracking(raw, now, cleared=False)['counting'] and not home_return:
            target = None
            decision['reason'] = (
                managed and self.manager.resting_note(raw, current, options, now)
            ) or 'Waiting for verified warm readiness before following demand.'
        with self.lock:
            if self.update_guard.active():
                return
            if (
                self.state['mode'] != 'demand'
                or self.state.get('pending')
                or self.state.get('demandPolicy') != settings.get('demandPolicy')
            ):
                return
            if self.tracking(raw, now, cleared=False)['counting']:
                self.demand_auto.record_spike_review(
                    live['account'], live['device'], decision.get('spikeReview')
                )
            self.detail = decision['reason']
            self.status = 'optimizing'
            if home_return:
                self.state.pop('demandProposal', None)
                self.next_switch = now
                return self.dispatch_demand(
                    now, target, decision, live, raw, current, options, environment
                )
            previous = self.state.get('demandProposal') or {}
            ready = self.tracking(raw, now, cleared=False)['counting']
            economic_row = None
            if ready and not decision.get('controlError'):
                if managed:
                    # Manager moves confirm on steady ticks, not paid-upgrade evidence.
                    economic_row = (
                        {'model': target, 'reason': decision['reason']} if target else None
                    )
                elif target and decision.get('kind') != 'explore':
                    economic_row = next(
                        (r for r in decision['opportunities'] if r['model'] == target),
                        {'model': target},
                    )
                elif not target:
                    economic_row = max(
                        (r for r in decision['opportunities'] if r.get('confirmationEligible')),
                        key=lambda r: (r.get('netGainUsd') or 0, r['model']),
                        default=None,
                    )
            if not target and not economic_row:
                row = next(
                    (r for r in decision['opportunities'] if r['model'] == previous.get('model')),
                    None,
                )
                source = (row or {}).get('signal', {}).get('observedAt')
                retain = (
                    ready
                    and not decision.get('controlError')
                    and row
                    and row.get('confirmationMissing')
                    and (
                        source is None
                        or finite(source)
                        and source <= now
                        and source >= previous.get('sourceAt', source)
                    )
                )
                proposal = (
                    pause_confirmation(
                        previous,
                        self.confirmation_scope(self.state, raw),
                        now,
                        row.get('reason') or 'Waiting for fresh economic evidence.',
                    )
                    if retain
                    else None
                )
                if proposal != previous:
                    if proposal:
                        self.state['demandProposal'] = proposal
                    else:
                        self.state.pop('demandProposal', None)
                    self.save()
                if proposal:
                    self.status = 'waiting'
                    self.detail = proposal['reason']
                return
            exploring = decision.get('kind') == 'explore' and bool(target)
            trial_exit = bool(decision.get('completedTrialExit'))
            source = (
                decision['sourceAt']
                if target
                else (economic_row.get('signal') or {}).get('observedAt')
            )
            if exploring:
                # Discovery already has independently observed sustained windows.
                sustained = next(
                    r.get('sustained', {})
                    for r in decision['opportunities']
                    if r['model'] == target
                )
                proposal = {
                    'model': target,
                    'kind': 'explore',
                    'since': now - 600,
                    'firstSourceAt': source - 600,
                    'sourceAt': source,
                    'checkedAt': now,
                    'samples': sustained.get('samples', 0),
                    'seconds': 600,
                    'requiredSeconds': 600,
                }
            else:
                proposal = advance_confirmation(
                    previous,
                    economic_row['model'],
                    source,
                    self.confirmation_scope(self.state, raw),
                    now,
                    rules['confirmationMinutes'] * 60,
                    economic_row.get('reason'),
                )
                if not proposal:
                    if self.state.pop('demandProposal', None):
                        self.save()
                    self.status = 'waiting'
                    self.detail = 'Waiting for a fresh qualified demand observation.'
                    return
            if proposal != previous:
                self.state['demandProposal'] = proposal
                self.save()
            if not target:
                self.status = 'waiting' if proposal.get('status') == 'ready' else 'optimizing'
                self.detail = proposal['reason']
                self.next_switch = None
                return
            if exploring:
                self.next_switch = (
                    now
                    if decision.get('escapeReady')
                    else now
                    + max(0, rules['idleEscapeMinutes'] * 60 - decision['activity']['idleSeconds'])
                )
                if not decision.get('escapeReady'):
                    self.detail = decision['reason']
                    return
            if not exploring:
                self.next_switch = max(
                    0
                    if trial_exit or (managed and decision.get('kind') == 'home')
                    else settings.get('lastSwitchAt', 0) + rules['minRunMinutes'] * 60,
                    now + max(0, rules['confirmationMinutes'] * 60 - proposal['seconds']),
                )
            if not exploring and (
                proposal['seconds'] < rules['confirmationMinutes'] * 60
                or proposal['samples'] < rules['confirmationMinutes'] + 1
            ):
                self.detail = (
                    decision['reason']
                    + ' Confirming for %d minutes before switching.' % rules['confirmationMinutes']
                    if managed
                    else 'Confirming '
                    + selection_label(target)
                    + ' for '
                    + str(rules['confirmationMinutes'])
                    + ' minutes using fresh demand and paid-work evidence.'
                )
                return
            if (
                not exploring
                and not trial_exit
                and not (managed and decision.get('kind') == 'home')
                and now - settings.get('lastSwitchAt', 0) < rules['minRunMinutes'] * 60
            ):
                self.detail = (
                    decision['reason']
                    + ' Waiting for the %d-minute minimum run to finish.' % rules['minRunMinutes']
                    if managed
                    else 'A better net opportunity is confirmed. Keeping the current model until its minimum run is complete.'
                )
                self.state['demandProposal']['reason'] = self.detail
                self.save()
                return
            self.dispatch_demand(now, target, decision, live, raw, current, options, environment)

    def dispatch_demand(self, now, target, decision, live, raw, current, options, environment):
        """Start a demand move in the worker (caller holds the lock)."""
        home_return = decision.get('kind') == 'home' and bool(
            (decision.get('manager') or {}).get('active')
        )
        self.demand_preflight = {
            'at': now,
            'target': target,
            'session': session_key(raw),
            'raw': copy.deepcopy(raw),
            'pulse': copy.deepcopy(live.get('pulse')),
            'providerSession': copy.deepcopy(live.get('provider', {}).get('session')),
            'activity': copy.deepcopy(decision.get('activity')),
        }
        self.state['pending'] = {
            'model': target,
            'previous': current,
            'at': now,
            'kind': 'demand',
            'afterIdleTimeout': False,
            'automaticMode': 'demand',
            'demandKind': decision.get('kind', 'earnings'),
            'session': session_key(raw),
            'launchSignature': launch_signature(options, environment),
        }
        self.save()
        self.status = 'switching'
        self.detail = (
            decision['reason'] + ' Rechecking readings and memory before switching.'
            if home_return
            else 'Rechecking the confirmed demand opportunity and memory before switching and pre-warming.'
        )
        self.worker = threading.Thread(
            target=self.switch,
            args=(current, target, live['account'], live['device']),
            daemon=False,
        )
        self.worker.start()

    def dispatch_stall_restart(self, now, live, raw, current, options, environment, result):
        """Restart the provider on the same model through the guarded switch path."""
        with self.lock:
            if (
                self.update_guard.active()
                or self.state['mode'] != 'demand'
                or self.state.get('pending')
                or self.state.get('requestedModel')
                or len(members(current)) != 1
            ):
                return False
            self.demand_preflight = {
                'at': now,
                'target': current,
                'session': session_key(raw),
                'raw': copy.deepcopy(raw),
                'pulse': copy.deepcopy(live.get('pulse')),
                'providerSession': copy.deepcopy(live.get('provider', {}).get('session')),
                'activity': None,
            }
            self.state['pending'] = {
                'model': current,
                'previous': current,
                'at': now,
                'kind': 'demand',
                'afterIdleTimeout': False,
                'automaticMode': 'demand',
                'demandKind': 'stall-restart',
                'session': session_key(raw),
                'launchSignature': launch_signature(options, environment),
            }
            self.state.pop('demandProposal', None)
            self.save()
            self.status = 'switching'
            self.detail = result['reason']
            self.worker = threading.Thread(
                target=self.switch,
                args=(current, current, live['account'], live['device']),
                daemon=False,
            )
            self.worker.start()
        return True

    def warmup_snapshot(self):
        with self.lock:
            raw = copy.deepcopy(self.raw)
            result = copy.deepcopy(self.warmup)
        if result.get('session') != session_key(raw):
            return {
                'status': 'waiting',
                'detail': 'Waiting to check the serving model’s warm-up.',
                'model': raw.get('current_model'),
            }
        result.pop('session', None)
        if result.get('status') == 'ready' and not all(
            m in raw.get('warm_models', []) for m in members(result.get('model'))
        ):
            result.update(
                status='cold',
                detail='Darkbloom unloaded a model after warm-up. Its idle-memory policy is preserved.',
            )
        if time.time() - raw.get('written_at', 0) > 15:
            result.update(status='waiting', detail='Waiting for fresh provider readiness readings.')
        return result

    def cache_recovery_needed(self, raw):
        with self.lock:
            live = copy.deepcopy(self.live) or {}
        target = selection_key(raw.get('advertised_models'))
        h = live.get('hardware') or {}
        budget = self.selection_budget(target, live, raw)
        return bool(budget and file_cache_blocked(raw, h, budget['requiredGB']))

    def prewarm_reason(self, raw, now, allow_cache_recovery=False, move=None):
        """`move`: the restore, return home or manual pick this warm-up completes (see
        environment_reason); None for a synthetic pre-warm of Darkbloom's own start."""
        with self.lock:
            live = copy.deepcopy(self.live) or {}
            observed = copy.deepcopy(self.raw)
        if self.stop.is_set():
            return 'BloomGauge is closing; warm-up is stopped.'
        if session_key(raw) != session_key(observed):
            return 'Waiting for the new provider session to be observed.'
        reason = self.wait_reason(
            live, now, manual=True, move=move, target=selection_key(raw.get('advertised_models'))
        )
        if reason:
            return reason
        if self.identity_session != (raw.get('started_at'), raw.get('pid')):
            return 'Waiting to verify the new provider session.'
        if not finite(raw.get('written_at')) or not -5 < now - raw['written_at'] < 15:
            return 'Waiting for fresh provider readings.'
        if activity_counters(raw) is None:
            return 'Waiting for valid provider request and token counters.'
        if raw.get('inference_active') is not False:
            return 'Waiting for idle capacity before pre-warming.'
        models = raw.get('advertised_models') or []
        target = selection_key(models)
        if not target:
            return 'Automatic pre-warming supports one or two distinct selected models.'
        if len(models) == 2:
            # Knobs BloomGauge can't model hold only its own voluntary moves (move None).
            error = self.combo_config_error(voluntary=move is None)
            if error:
                return error
        rows = self.candidates({}, {}, live, {})
        selected = [r for r in rows if r['id'] in models and r['available']]
        if len(selected) != len(models):
            return 'Waiting for supported, locally available serving models.'
        if not all(m in raw.get('warm_models', []) for m in models):
            budget = self.selection_budget(target, live, raw)
            required = budget['requiredGB'] if budget else math.inf
            if len(models) == 2:
                required -= sum(
                    r['memoryGB'] for r in selected if r['id'] in raw.get('warm_models', [])
                )
            # At this stage the new process already exists: do not count a
            # hypothetical unload or disk cache twice when admitting a load.
            if not budget or live['hardware']['memoryAvailableGB'] < required:
                if not (allow_cache_recovery and self.cache_recovery_needed(raw)):
                    return 'Waiting for enough available memory to pre-warm. Cache recovery only runs for a cold model blocked by file cache.'
        return None

    def served_warm(self, raw, now):
        """Completed work in this exact session already proves the decode path.

        Do not make a busy, working model wait for a synthetic request.
        """
        models = raw.get('advertised_models') or []
        stats = raw.get('stats') or {}
        with self.lock:
            if (
                len(models) != 1
                or raw.get('current_model') != models[0]
                or models[0] not in raw.get('warm_models', [])
                or raw.get('trust', {}).get('status') != 'online'
                or not finite(raw.get('written_at'))
                or not -5 < now - raw['written_at'] < 15
                or not self.identity_ok
                or now - self.identity_at >= 180
                or self.identity_session != (raw.get('started_at'), raw.get('pid'))
                or session_key(raw) != session_key(self.raw)
                or any(
                    not finite(stats.get(k)) or stats[k] < 1
                    for k in ('requests_served', 'tokens_generated')
                )
            ):
                return False
            self.warmup = {
                'session': session_key(raw),
                'model': models[0],
                'status': 'ready',
                'detail': 'Warm and ready · serving output verified.',
                'verifiedAt': now,
                'attempts': 0,
            }
        return True

    def tick_prewarm(self, now):
        """Also follows external selections and provider restarts in Observe mode.

        One successful check per session; do not fight a later idle unload.
        Failed checks get one delayed retry, never a restart/purge from here.
        """
        with self.lock:
            if self.update_guard.active():
                return False
            if self.warmup_worker and self.warmup_worker.is_alive():
                return True
            if self.state.get('pending') or self.state.get('requestedModel'):
                return False
            raw = copy.deepcopy(self.raw)
            record = copy.deepcopy(self.warmup)
        if not raw:
            return False
        key = session_key(raw)
        target = selection_key(raw.get('advertised_models'))
        if not target:
            return False
        if record.get('session') != key:
            record = {'session': key, 'model': target, 'attempts': 0}
        if record.get('status') == 'ready':
            return False
        if self.served_warm(raw, now):
            return False
        if record.get('attempts', 0) >= 2:
            return False
        if now - record.get('attemptedAt', 0) < 60:
            return True
        reason = self.prewarm_reason(raw, now, allow_cache_recovery=True)
        try:
            selection, options, _ = self.read_options()
            if selection != target:
                reason = 'Waiting for a stable model selection before pre-warming.'
            local_request(self.home, raw, options)
        except (OSError, ValueError, WarmupError):
            reason = 'Waiting for this provider’s authenticated loopback endpoint. Check Darkbloom on the Mac.'
        if not reason and (self.idle_since is None or now - self.idle_since < 12):
            reason = 'Waiting for an idle period before pre-warming.'
        with self.lock:
            if self.update_guard.active():
                return False
            if self.state.get('pending') or self.state.get('requestedModel'):
                return False
            self.warmup = {
                **record,
                'status': 'waiting',
                'detail': reason or 'Pre-warming the selected model.',
            }
            if reason:
                return False
            self.warmup_worker = threading.Thread(
                target=self.warm_current, args=(target, raw, options), daemon=True
            )
            self.warmup_worker.start()
        return True

    def warm_current(self, target, raw, options):
        with self.command_lock:
            try:
                self.perform_prewarm(target, raw, options)
            except (ExternalChange, WarmupError):
                pass  # Status is recorded, never override an external selection.

    def perform_prewarm(self, target, raw, options, deadline=None, move=None):
        """One local warm-up. A readiness recheck that fails (a stale reading, memory still
        being freed, a roster refresh) raises WarmupDeferred: nothing was sent, so the caller
        keeps waiting; verify_started still fails on a load error, its deadline or memory
        short past the manager's wait. `move`: see prewarm_reason."""
        key = session_key(raw)
        now = time.time()
        with self.lock:
            attempts = self.warmup.get('attempts', 0) if self.warmup.get('session') == key else 0
            self.warmup = {
                'session': key,
                'model': target,
                'status': 'warming',
                'detail': 'Loading the model and running a one-token local warm-up.',
                'attempts': attempts + 1,
                'attemptedAt': now,
            }
            self.previous = None
            self.idle_since = None
        try:
            current = self.read_state()
            selection, current_options, _ = self.read_options()
            if (
                self.stop.is_set()
                or session_key(current) != key
                or selection != target
                or current_options != options
                or self.service_disabled() is not False
            ):
                raise NothingSent('Provider changed before warm-up.')
            if self.served_warm(current, time.time()):
                return True
            reason = self.prewarm_reason(current, time.time(), allow_cache_recovery=True, move=move)
            if current.get('inference_active') is True or not same_activity(current, raw):
                raise WarmupDeferred(
                    'Paid work arrived before warm-up. Waiting for verified serving output or another idle window.',
                    code='work-arrived',
                )
            if reason:
                raise WarmupDeferred(reason, code='readiness-changed')
            if self.cache_recovery_needed(current):
                self.recover_file_cache(current, target, options, move=move)
                current = self.read_state()
                selection, current_options, _ = self.read_options()
                if (
                    self.stop.is_set()
                    or session_key(current) != key
                    or selection != target
                    or current_options != options
                    or self.service_disabled() is not False
                ):
                    raise ExternalChange('Provider changed after cache recovery.')
                if self.served_warm(current, time.time()):
                    return True
                reason = self.prewarm_reason(current, time.time(), move=move)
                if current.get('inference_active') is True:
                    raise WarmupDeferred(
                        'Paid work arrived after cache recovery. Waiting for verified serving output.',
                        code='work-arrived',
                    )
                if reason:
                    raise WarmupDeferred(reason, code='readiness-changed')
            targets = members(target)
            for index, member in enumerate(targets):
                if index:
                    # A fresh idle sample after the first decode prevents using
                    # its stale pre-load memory/idle state to admit the second.
                    after = time.time()
                    member_deadline = (
                        min(after + 60, deadline) if deadline is not None else after + 60
                    )
                    idle_at = None
                    last_stats = None
                    while not self.stop.is_set() and time.time() < member_deadline:
                        current = self.read_state()
                        selection, current_options, _ = self.read_options()
                        if (
                            session_key(current) != key
                            or selection != target
                            or current_options != options
                            or self.service_disabled() is not False
                        ):
                            raise ExternalChange('Provider changed between model warm-ups.')
                        reason = self.prewarm_reason(current, time.time(), move=move)
                        if (
                            reason
                            or current.get('written_at', 0) < after
                            or activity_counters(current) != last_stats
                        ):
                            idle_at = None
                        elif idle_at is None:
                            idle_at = time.time()
                        last_stats = activity_counters(current)
                        if idle_at is not None and time.time() - idle_at >= 12:
                            break
                        self.stop.wait(2)
                    else:
                        raise WarmupError(
                            'Could not confirm fresh idle capacity for the second model.'
                        )
                current = self.read_state()
                selection, current_options, _ = self.read_options()
                if (
                    self.stop.is_set()
                    or session_key(current) != key
                    or selection != target
                    or current_options != options
                    or self.service_disabled() is not False
                ):
                    raise ExternalChange('Provider changed between model warm-ups.')
                reason = self.prewarm_reason(current, time.time(), move=move)
                if current.get('inference_active') is True:
                    raise WarmupDeferred(
                        'Paid work arrived before the local request. Waiting for verified serving output.',
                        code='work-arrived',
                    )
                if reason:
                    raise WarmupDeferred(reason, code='readiness-changed')
                with self.lock:
                    self.warmup.update(
                        detail='Pre-warming '
                        + member
                        + '; every selected model must remain loaded.'
                    )
                remaining = 180 if deadline is None else min(180, deadline - time.time())
                if remaining <= 0:
                    raise WarmupError(
                        'The selected model did not become ready before the verification deadline.',
                        code='readiness-timeout',
                    )
                prewarm(self.home, current, options, member, timeout=remaining)
            completed_at = time.time()
            for _ in range(10):
                if deadline is not None and time.time() >= deadline:
                    break
                current = self.read_state()
                selection, _, _ = self.read_options()
                if (
                    self.stop.is_set()
                    or session_key(current) != key
                    or selection != target
                    or self.service_disabled() is not False
                ):
                    raise ExternalChange('Provider changed during warm-up.')
                fresh = -5 < time.time() - current.get('written_at', 0) < 15 and (
                    len(targets) == 1 or current.get('written_at', 0) >= completed_at
                )
                if (
                    all(m in current.get('warm_models', []) for m in targets)
                    and fresh
                    and current.get('trust', {}).get('status') == 'online'
                ):
                    with self.lock:
                        self.warmup.update(
                            status='ready',
                            detail='Both models are warm · local decode verified.'
                            if len(targets) == 2
                            else 'Warm and ready · local decode verified.',
                            verifiedAt=time.time(),
                        )
                    return True
                self.stop.wait(2)
            raise WarmupError(
                'Warm-up responded, but Darkbloom has not confirmed every selected model is loaded together.',
                code='loaded-model-unconfirmed',
            )
        except WarmupDeferred as e:
            with self.lock:
                self.warmup.update(status='waiting', detail=str(e), failureCode=e.code)
            raise
        except Exception as e:
            detail = (
                str(e)
                if isinstance(e, WarmupError)
                else 'Provider stopped or changed during warm-up; no restart was sent.'
                if isinstance(e, ExternalChange)
                else 'Could not verify model warm-up. Check Darkbloom on the Mac.'
            )
            with self.lock:
                self.warmup.update(
                    status='failed',
                    detail=detail,
                    failureCode=e.code if isinstance(e, WarmupError) else 'unknown',
                )
            if isinstance(e, ExternalChange):
                raise
            if isinstance(e, WarmupError):
                raise
            raise WarmupError(detail) from None
        finally:
            with self.lock:
                self.previous = None
                self.idle_since = None

    def recover_file_cache(self, raw, target, options, move=None):
        """`move`: see prewarm_reason (a restore, return home or pick may purge on battery)."""
        key = session_key(raw)
        now = time.time()
        with self.lock:
            last = self.state.get('cacheRecovery') or {}
            if last.get('session') == key and last.get('status') != 'loaded':
                raise WarmupError(
                    'Cache recovery was already attempted for this provider session. Check the Mac before retrying.',
                    code='cache-recovery-limited',
                )
            if now - last.get('at', 0) < 600:
                raise WarmupError(
                    'Cache recovery is cooling down; it runs at most once every ten minutes.',
                    code='cache-recovery-limited',
                )
        # Revalidate immediately before the privileged command; never trust a
        # queued request's old idle/memory/session observations.
        current = self.read_state()
        selection, current_options, _ = self.read_options()
        if (
            self.stop.is_set()
            or session_key(current) != key
            or selection != target
            or current_options != options
            or self.service_disabled() is not False
        ):
            raise NothingSent('Provider changed before cache recovery.')
        reason = self.prewarm_reason(current, time.time(), allow_cache_recovery=True, move=move)
        if reason or not same_activity(current, raw) or not self.cache_recovery_needed(current):
            # Nothing was cleared or sent: the warm-up keeps waiting (its deadline still holds).
            raise WarmupDeferred(
                reason or 'Memory or workload changed; cache recovery was not run.',
                code='readiness-changed',
            )
        with self.lock:
            self.state['cacheRecovery'] = {
                'session': key,
                'at': now,
                'status': 'running',
                'model': target,
                'detail': 'Clearing macOS file cache for a cold, memory-blocked model.',
            }
            self.save()
            self.warmup.update(
                status='warming',
                detail='Clearing macOS file cache, then rechecking memory before warm-up.',
            )
        try:
            clear_file_cache(self.runner)
        except CacheRecoveryError as e:
            with self.lock:
                self.state['cacheRecovery'].update(status='failed', detail=str(e))
                self.save()
            raise WarmupError(str(e), code=e.code) from None
        cleared_at = time.time()
        with self.lock:
            self.state['cacheRecovery'].update(
                status='cleared',
                detail='macOS file cache cleared. Waiting for fresh memory readings.',
            )
            self.save()
        for _ in range(10):
            if self.stop.wait(2):
                raise ExternalChange('BloomGauge closed during memory recovery.')
            current = self.read_state()
            if session_key(current) != key:
                raise ExternalChange('Provider changed during memory recovery.')
            with self.lock:
                fresh = self.live and self.live.get('at', 0) > cleared_at
            if fresh:
                return
        raise WarmupError(
            'Cache cleared, but fresh memory readings have not arrived. Warm-up is waiting.',
            code='cache-readings',
        )

    def purge_before_load(self, target):
        """Clear the macOS file cache before every switch BloomGauge sends.

        Darkbloom admits a load only if free + inactive memory covers it, so the
        previous model's weight files left in the file cache can block the next
        large model. Runs only with the exact passwordless purge permission, at
        most once per SWITCH_PURGE_GAP. A failure never blocks the switch.
        Returns True when a purge ran and a post-purge memory reading arrived.
        """
        with self.lock:
            last = self.state.get('switchPurge') or {}
        if finite(last.get('at')) and 0 <= time.time() - last['at'] < SWITCH_PURGE_GAP:
            return False
        if self.manual_selection.permission_status().get('status') != 'ready':
            return False
        purged_at = time.time()
        with self.lock:
            self.state['switchPurge'] = {
                'at': purged_at,
                'model': target,
                'status': 'running',
                'detail': 'Clearing macOS file cache before loading the next model.',
            }
            self.save()
        try:
            clear_file_cache(self.runner)
        except CacheRecoveryError as error:
            with self.lock:
                self.state['switchPurge'].update(
                    status='failed', detail=str(error) + ' Switching without cleanup.'
                )
                self.save()
            with self.manual_selection.permission_lock:
                self.manual_selection.permission = None
            return False
        for _ in range(10):
            if self.stop.wait(2):
                raise ExternalChange('BloomGauge closed during file-cache cleanup.')
            with self.lock:
                measured = ((self.live or {}).get('hardware') or {}).get('at')
            fresh = finite(measured) and measured > purged_at
            if fresh:
                with self.lock:
                    self.state['switchPurge'].update(
                        status='cleared', detail='macOS file cache cleared before the switch.'
                    )
                    self.save()
                return True
        with self.lock:
            self.state['switchPurge'].update(
                status='cleared',
                detail='macOS file cache cleared, but no fresh memory reading arrived; using the latest reading.',
            )
            self.save()
        return True

    def pause_automatic(self, source='mac', data=None):
        with self.lock:
            if data is not None:
                self.require_current_control(data)
            if hasattr(self, 'automatic_control'):
                self.automatic_control.cancel(
                    'Manual mode selected. The current model keeps serving.'
                )
            before = copy.deepcopy(self.state)
            self.cancel_combo('Automatic switching was paused.')
            if (self.state.get('requestedKind') or '').startswith('manual') and not self.state.get(
                'pending'
            ):
                self.state['manualResult'] = {
                    **self.state.get('manualResult', {}),
                    'status': 'cancelled',
                    'detail': 'Queued manual switch cancelled from '
                    + ('phone.' if source == 'phone' else 'the Mac.'),
                }
                self.state.update(requestedKind=None, requestId=None)
            self.state['mode'] = 'observe'
            self.state['requestedModel'] = None
            self.state.pop('demandProposal', None)
            manager.release(self.state)
            try:
                self.save()
            except Exception:
                self.state = before
                raise
            self.proposal = None
            self.status = 'observing'
            self.detail = 'Automatic switching is paused. Recording continues; any switch already started will finish.'

    def pause_internal(self, detail, keep_automatic_intent=None):
        with self.lock:
            if hasattr(self, 'automatic_control'):
                self.automatic_control.cancel(detail, keep=keep_automatic_intent)
            self.cancel_combo(detail)
            if (self.state.get('requestedKind') or '').startswith('manual'):
                self.state['manualResult'] = {
                    **self.state.get('manualResult', {}),
                    'status': 'cancelled',
                    'detail': detail,
                }
            self.state.update(
                requestedKind=None,
                requestId=None,
                requestedAt=None,
                requestedSession=None,
                requestedLaunchSignature=None,
            )
            self.state['mode'] = 'observe'
            self.state['requestedModel'] = None
            (self.state.get('manager') or {}).pop('resume', None)
            self.status = 'observing'
            self.detail = detail
            self.proposal = None
            self.save()

    def command(self, target, options, environment):
        models = members(target)
        if not models:
            raise ValueError('Invalid serving-model selection.')
        with self.lock:
            graceful = graceful_drain(self.raw)
        self.runner(
            [
                str(self.binary),
                'start',
                *manager.start_options(self.home, options),
                *[flag for model in models for flag in ('--model', model)],
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=DRAIN_SECONDS + START_SECONDS if graceful else START_SECONDS,
            check=True,
            env={**os.environ, **environment},
        )

    def local_models(self, config=()):
        """Every downloaded model. Since 0.9.10 `models list` shows only provider.toml's
        enabled_models unless given --all, which older CLIs reject as an unknown option."""

        def run(flags):
            result = self.runner(
                [str(self.binary), 'models', 'list', '--json', *flags, *config],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=25,
                check=True,
            )
            return json.loads(result.stdout)['models']

        if self.list_all is False:
            return run([])
        try:
            rows = run(['--all'])
        except subprocess.CalledProcessError as error:
            if self.list_all or '--all' not in f'{error.stderr or ""}{error.stdout or ""}':
                raise
            self.list_all = False
            return run([])
        self.list_all = True
        return rows

    def verify_local_target(self, target, options, catalog_optional=False):
        """Read-only disk/catalog refresh; never start a model to discover it is missing.

        `catalog_optional`: a return home to a model this Mac has served before; its files
        must still check out, but a failed catalog fetch keeps the last catalog."""
        config = [
            options[i + j]
            for i, v in enumerate(options[:-1])
            if v in ('--config', '-c')
            for j in (0, 1)
        ]
        try:
            rows = self.local_models(config)
            try:
                catalog = self.network.fetch('/v1/models/catalog')['models']
                if not isinstance(catalog, list):
                    raise ValueError()
            except Exception:
                if not catalog_optional:
                    raise
                catalog = None
            if not members(target):
                raise ValueError()
            for model in members(target):
                matches = [r for r in rows if isinstance(r, dict) and r.get('id') == model]
                if (
                    len(matches) != 1
                    or not finite(matches[0].get('size_bytes'))
                    or matches[0]['size_bytes'] <= 0
                    or matches[0].get('template_render_ok') is not True
                ):
                    raise ValueError()
            with self.lock:
                self.local = rows
                if catalog is not None:
                    self.catalog = catalog
                    self.discovery_at = time.time()
                    self.discovery_error = None
        except Exception:
            raise DemandDeferred(
                'The selected model’s local files, working template and fresh catalog could not be verified. Keeping the current model.'
            ) from None

    def service_disabled(self):
        try:
            result = self.runner(
                ['/bin/launchctl', 'print-disabled', 'gui/' + str(os.getuid())],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            )
            if not isinstance(result.stdout, str):
                return None
            return bool(re.search(r'"io\.darkbloom\.provider"\s*=>\s*true', result.stdout))
        except (OSError, subprocess.SubprocessError):
            return None

    def service_loaded(self):
        """True when launchd has the provider's launch agent loaded, False when it doesn't
        ("Could not find service"), None when launchctl can't tell."""
        try:
            result = self.runner(
                ['/bin/launchctl', 'print', 'gui/%d/io.darkbloom.provider' % os.getuid()],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode == 0:
            return True
        text = '%s %s' % (result.stdout or '', result.stderr or '')
        return False if 'Could not find service' in text else None

    def bootstrap_service(self):
        """Load the provider's launch agent, as launchd does at login. True when it loaded."""
        try:
            result = self.runner(
                ['/bin/launchctl', 'bootstrap', 'gui/%d' % os.getuid(), str(self.plist_path)],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0

    def log_memory_projection(self, target, budget):
        """For calibrating memory admission later: the projected free memory after the unload
        against the first reading of the new session (free + inactive, and what it holds)."""
        reading = getattr(self, 'verification_memory', None)
        projected = (budget or {}).get('afterUnloadGB')
        if not reading or not finite(projected) or not all(finite(v) for v in reading):
            return
        log.info(
            'Memory after switching to %s: projected %.2f GB free after the unload; the new session saw %.2f GB free with %.2f GB loaded.',
            target,
            projected,
            *reading,
        )

    def endpoint_notice(self, options):
        """endpoint_issue, except that once ENDPOINT_FAILURES setups in a row failed for the same
        reason, the request to set it up again gives way to that reason and the manual fix."""
        issue = endpoint_issue(options)
        if issue != ENDPOINT_SETUP:
            return issue
        with self.lock:
            failure = copy.deepcopy(self.state.get('endpointSetupFailure')) or {}
        fix = endpoint_fix(failure)
        return failure['cause'] + ' ' + fix if fix else issue

    def setup_result(self, model, success, cause, detail):
        """After an attempt that was to set up the local endpoint. A failure counts while the
        launch agent still lacks the endpoint; the same cause twice in a row adds the manual fix
        to `detail`. A success, or an endpoint that is now set up, clears the count."""
        try:
            missing = endpoint_issue(self.read_options(plist_only=True)[1]) == ENDPOINT_SETUP
        except Exception:
            missing = True
        with self.lock:
            last = self.state.get('endpointSetupFailure') or {}
            if success or not missing:
                self.state.pop('endpointSetupFailure', None)
                return detail
            if not cause:
                return detail
            same = last.get('cause') == cause and last.get('model') == model
            failure = {
                'model': model,
                'cause': cause,
                'count': (last.get('count', 0) if same else 0) + 1,
                'at': time.time(),
            }
            self.state['endpointSetupFailure'] = failure
        fix = endpoint_fix(failure)
        return detail + ' ' + fix if fix else detail

    def note_dropped_environment(self, passed, saved):
        """Darkbloom rebuilds the launch agent on every start and keeps only its own allowlisted
        variables (LaunchAgent.passthroughEnvKeys), exactly as a start from Terminal does. Any
        other variable in the old agent is gone after the start. That used to happen silently;
        remember which ones, so the model controls can say so."""
        if not isinstance(passed, dict) or not isinstance(saved, dict):
            return
        # Darkbloom forwards only non-empty values (LaunchAgent.passthroughEnvironment).
        dropped = sorted(
            k for k, v in passed.items() if isinstance(k, str) and v and k not in saved
        )
        if not dropped:
            with self.lock:
                if self.state.pop('environmentDropped', None) is not None:
                    self.save()
            return
        log.warning('Darkbloom did not keep launch-agent variables after start: %s', ', '.join(dropped))
        with self.lock:
            self.state['environmentDropped'] = {'keys': dropped[:8], 'at': time.time()}
            self.save()

    def started_launch(self, requested, environment):
        """Our `darkbloom start` just returned: the launch it saved, as the baseline that
        verification then watches for outside changes.

        Darkbloom doesn't save the argv it was given. It rebuilds the launch agent from its
        parsed options (0.9.10 LaunchAgent.serviceProgramArguments): `--local-endpoint` always
        gains `--port` and `--bind` (8000 and 127.0.0.1 by default), `--coordinator-url` is
        always written, `--idle-timeout` goes to provider.toml instead and the environment keeps
        only its passthrough variables. A digest of our argv never matched that, so every
        endpoint setup read as 'Provider settings changed' and was asked for again. What it saved
        must still carry the `--local-endpoint` BloomGauge passed; verify_started checks the
        selection on every reading. `start` holds Darkbloom's provider-lifecycle lease until the
        agent is written, so another `start` can't write in between. An unreadable agent keeps
        the requested launch as the baseline."""
        for attempt in range(3):
            try:
                _, options, saved = self.read_options()
            except (OSError, ValueError, TypeError, KeyError, plistlib.InvalidFileException):
                if attempt == 2 or self.stop.wait(1):
                    return launch_signature(requested, environment)
                continue
            if '--local-endpoint' in requested and '--local-endpoint' not in options:
                raise ExternalChange('Provider settings changed during the switch.')
            self.note_dropped_environment(environment, saved)
            return launch_signature(options, saved)

    def verify_started(
        self,
        target,
        previous_session,
        timeout=360,
        expected_launch=None,
        memory_seconds=None,
        move=None,
    ):
        """`memory_seconds` bounds a pre-warm memory wait (manager); the restore path follows.
        `move`: see prewarm_reason. Fails only on Darkbloom's load error, a failed local
        warm-up, the deadline, or memory still short after `memory_seconds`; any other
        pre-warm reason, including one that appears just before the local request, waits."""
        if len(members(target)) == 2 and timeout == 360:
            timeout = 600
        deadline = time.time() + timeout
        memory_since = None
        idle_at = None
        previous_stats = None
        attempts = 0
        last_wait = None
        self.verification_failure = None
        self.verification_session = None
        self.verification_memory = None
        with self.lock:
            expected_device = device_id(self.raw)
            account = (self.live or {}).get('account')
        rekeyed = False
        trust, trust_at = None, None  # the new session's latest trust reading, for the text
        while not self.stop.is_set() and time.time() < deadline:
            try:
                selection, options, environment = self.read_options()
                if (
                    selection != target
                    or self.service_disabled() is True
                    or (
                        expected_launch is not None
                        and launch_signature(options, environment) != expected_launch
                    )
                ):
                    raise ExternalChange('Provider settings changed during the switch.')
                d = self.read_state()
                if (
                    -5 < time.time() - d.get('written_at', 0) < 15
                    and d.get('started_at') != previous_session
                ):
                    trust = d.get('trust') if isinstance(d.get('trust'), dict) else None
                    trust_at = time.time()
                device = device_id(d)
                if device != expected_device:
                    # Darkbloom makes a new attestation key at a start when it can't use its
                    # keychain key, so the session our own command started may carry a new
                    # device id. Accept that once, for the same account, after the provider
                    # roster matches it (refresh; the manager then adopts it: device_changed).
                    # Any other identity change fails the switch.
                    if (
                        rekeyed
                        or not device
                        or d.get('started_at') == previous_session
                        or (self.live or {}).get('account') != account
                    ):
                        raise ExternalChange('Provider identity changed during the switch.')
                    with self.lock:
                        matched = bool(
                            self.device_identity_ok
                            and self.identity_device == device
                            and 0 <= time.time() - self.identity_at < 180
                            and self.identity_session == (d.get('started_at'), d.get('pid'))
                        )
                        if not matched:
                            self.next_identity = min(self.next_identity, time.time())
                            self.status = 'warming'
                            self.detail = 'Darkbloom started with a new device key. Waiting for the provider roster to match it to this Mac.'
                    if not matched:
                        self.stop.wait(2)
                        continue
                    expected_device, rekeyed = device, True
                if (
                    -5 < time.time() - d.get('written_at', 0) < 15
                    and d.get('started_at') != previous_session
                    and same_selection(d, target)
                    and d.get('trust', {}).get('status') == 'online'
                ):
                    key = session_key(d)
                    if self.verification_session is not None and self.verification_session != key:
                        raise ExternalChange('Provider restarted again during switch verification.')
                    if self.verification_session is None:
                        # First reading of the new session, for calibrating the memory
                        # projection: free + inactive now, and what the new process holds.
                        with self.lock:
                            hardware = (self.live or {}).get('hardware') or {}
                        self.verification_memory = (
                            hardware.get('memoryAvailableGB'),
                            (d.get('capacity') or {}).get('gpu_memory_active_gb'),
                        )
                    self.verification_session = key
                    failure = d.get('last_model_load_error') or {}
                    if (
                        failure.get('model') in members(target)
                        and finite(failure.get('at'))
                        and failure['at'] >= d.get('started_at', 0)
                        and failure.get('model') not in d.get('warm_models', [])
                        and not self.cache_recovery_needed(d)
                    ):
                        self.verification_failure = WarmupError(
                            'Darkbloom reported a load error for the selected model.',
                            code='model-load-error',
                        )
                        return False
                    if self.served_warm(d, time.time()):
                        return True
                    reason = self.prewarm_reason(
                        d, time.time(), allow_cache_recovery=True, move=move
                    )
                    short = bool(reason) and reason.startswith('Waiting for enough available memory')
                    memory_since = (memory_since or time.time()) if short else None
                    if (
                        memory_seconds is not None
                        and memory_since is not None
                        and time.time() - memory_since >= memory_seconds
                    ):
                        self.verification_failure = WarmupError(reason, code='readiness-timeout')
                        return False
                    if reason or activity_counters(d) != previous_stats:
                        idle_at = None
                    elif idle_at is None:
                        idle_at = time.time()
                    previous_stats = activity_counters(d)
                    with self.lock:
                        self.status = 'warming'
                        self.detail = (
                            reason
                            or last_wait
                            or 'Waiting for idle capacity to pre-warm and verify the selected model.'
                        )
                    if idle_at is not None and time.time() - idle_at >= 12 and attempts < 3:
                        attempts += 1
                        try:
                            return self.perform_prewarm(
                                target, d, options, deadline=deadline, move=move
                            )
                        except WarmupDeferred as error:
                            # The selected model may be serving real work now.
                            # Reset idle proof, retain the original deadline and
                            # bound local requests; never restart/purge for this race.
                            idle_at = None
                            previous_stats = None
                            last_wait = str(error)
                            if error.code == 'readiness-changed':
                                # A recheck just before the request: nothing was sent.
                                attempts -= 1
                                if last_wait.startswith('Waiting for enough available memory'):
                                    memory_since = memory_since or time.time()
                            with self.lock:
                                self.warmup.update(
                                    status='waiting', detail=last_wait, failureCode=error.code
                                )
                        except WarmupError as error:
                            self.verification_failure = error
                            return False
                else:
                    idle_at = None
            except (OSError, ValueError, TypeError):
                pass
            self.stop.wait(2)
        if (
            trust is not None
            and trust.get('status') == 'untrusted'
            and time.time() - trust_at < 30
        ):
            # Failing attestation challenges (e.g. "no response") up to the deadline: Darkbloom
            # runs, but the network doesn't accept this session, so nothing could verify.
            reason = trust.get('reason')
            text = (
                'Darkbloom started the selected model, but the Darkbloom network had not '
                'accepted this Mac by the verification deadline'
                + (' (Darkbloom: “%s”).' % reason if isinstance(reason, str) and reason else '.')
            )
        else:
            text = 'The selected model did not become ready before the verification deadline.'
        self.verification_failure = WarmupError(
            text + (' ' + last_wait if last_wait else ''),
            code='readiness-timeout',
        )
        return False

    def clearance_note(self):
        """After a start the network verifies the new session before it sends work (serving_trust
        daemon_verifying); a pick completes on a local warm-up meanwhile. Say so."""
        try:
            waiting = daemon_verifying(self.read_state())
        except (OSError, ValueError, TypeError):
            waiting = False
        return (
            ' The Darkbloom network is still clearing this Mac to serve (it checks each new '
            'session); work arrives after that.'
            if waiting
            else ''
        )

    def target_serving_after_wait(self, target, previous_session, device, expected_launch):
        """Read-only late proof; never accept another session, stop or selection."""
        try:
            selection, options, environment = self.read_options()
            raw = self.read_state()
            return bool(
                not self.stop.is_set()
                and self.service_disabled() is False
                and selection == target
                and launch_signature(options, environment) == expected_launch
                and device_id(raw) == device
                and raw.get('started_at') != previous_session
                and same_selection(raw, target)
                and getattr(self, 'verification_session', None) == session_key(raw)
                and self.served_warm(raw, time.time())
            )
        except (OSError, ValueError, TypeError):
            return False

    def recovery_ready(self, previous, target, device, timeout=60, expected_launch=None):
        """Never recover by forcibly restarting work that arrived during warm-up."""
        deadline = time.time() + timeout
        idle_at = None
        reference = None
        self.recovery_block_code = 'idle-not-verified'
        while not self.stop.is_set() and time.time() < deadline:
            try:
                selection, options, environment = self.read_options()
                d = self.read_state()
                if (
                    selection not in (previous, target)
                    or device_id(d) != device
                    or self.service_disabled() is not False
                    or expected_launch is not None
                    and launch_signature(options, environment) != expected_launch
                ):
                    self.recovery_block_code = 'provider-changed'
                    return None
                sample = (session_key(d), activity_counters(d))
                if (
                    not same_selection(d, selection)
                    or sample[1] is None
                    or d.get('inference_active') is not False
                    or not -5 < time.time() - d.get('written_at', 0) < 15
                ):
                    idle_at = None
                elif sample != reference:
                    idle_at = time.time()
                elif idle_at is not None and time.time() - idle_at >= 12:
                    return d
                reference = sample
            except (OSError, ValueError, TypeError):
                idle_at = None
                self.recovery_block_code = 'resource-or-identity'
            self.stop.wait(2)
        if self.stop.is_set():
            self.recovery_block_code = 'provider-changed'
        return None

    def recovery_identity(self, raw, account, device, now, move=None):
        """Fresh hardware proof permits restoration, never serving eligibility. `move`: see
        environment_reason."""
        with self.lock:
            live = copy.deepcopy(self.live) or {}
            observed = copy.deepcopy(self.raw)
        hardware_at = live.get('hardware', {}).get('at')
        if (
            live.get('account') != account
            or live.get('device') != device
            or device_id(raw) != device
            or session_key(raw) != session_key(observed)
            or not finite(raw.get('written_at'))
            or not -5 < now - raw['written_at'] < 15
            or not finite(hardware_at)
            or not -5 < now - hardware_at < 15
            or not daemon_authorized(raw)
            or matching_process(process_identity(raw)) is not True
            or self.environment_reason(live, now, manual=True, move=move)
        ):
            return False
        try:
            return roster_identity(
                raw, self.network.fetch('/v1/providers/attestation')['providers']
            )['hardwareVerified']
        except (OSError, ValueError, TypeError, KeyError):
            return False

    def switch(self, previous, target, account, device):
        start = time.time()
        command_started = None
        command_returned = None
        attempted = False
        success = False
        recovered = False
        deferred = False
        detail = ''
        options = []
        environment = {}
        auto_run = None
        decision = {}
        known_working = False
        failure_stage = 'preflight'
        failure_record = None
        written = None  # the launch Darkbloom saved for our start (started_launch)
        cause = None  # why it failed, for repeated endpoint-setup failures
        external = False
        with self.lock:
            request_kind = self.state.get('requestedKind') or ''
            request_id = self.state.get('requestId')
            pending = copy.deepcopy(self.state.get('pending')) or {}
            request_kind = request_kind or pending.get('kind') or ''
            # The manager never pauses after a failed automatic move (manager.switch_failed).
            managed = request_kind == 'demand' and manager.active(self.state)
            # Which voluntary-move checks apply (environment_reason `move`). A return home
            # also skips the readiness preflight; an excursion start meets the daily limit.
            move = (
                'manual'
                if request_kind.startswith('manual')
                else 'home'
                if managed and pending.get('demandKind') == 'home'
                else None
            )
            excursion = managed and pending.get('demandKind') == 'excursion'
            saved_policy = copy.deepcopy(self.state.get('demandPolicy'))
            # A manual pick made under the manager: automatic control resumes after it.
            picked = bool(
                request_id
                and ((self.state.get('manager') or {}).get('resume') or {}).get('id') == request_id
            )
            automatic = request_kind in ('demand', 'automatic')
            automatic_mode = 'demand' if request_kind == 'demand' else pending.get('automaticMode')
            verify_runtime = bool(
                request_kind == 'manual'
                and self.state.get('mode') == 'observe'
                and pending.get('kind') == 'manual'
                and pending.get('verifyRuntime') is True
                and self.state.get('requestedVerifyRuntime') is True
                and pending.get('requestId') == request_id
                and request_id
                and pending.get('model') == target == self.state.get('requestedModel')
            )
            selected_operation = bool(
                pending.get('selectionAction')
                and (self.state.get('selectionRequest') or {}).get('id') == request_id
                and request_id
                and request_kind == 'manual'
            )
            setup_requested = bool(
                selected_operation
                and (self.state.get('selectionRequest') or {}).get('setupEndpoint') is True
            )
        with self.command_lock:
            try:
                model, options, environment = self.read_options()
                raw = self.read_state()
                with self.lock:
                    reference = copy.deepcopy(self.raw)
                    identity_matches = bool(
                        self.live
                        and self.live.get('account') == account
                        and self.identity_session == (raw.get('started_at'), raw.get('pid'))
                    )
                    timed_pause = bool(
                        pending.get('afterIdleTimeout') is True
                        and manual_pause_due(self.state, time.time())
                        and pending.get('kind') == 'manual'
                        and pending.get('model') == target == self.state.get('requestedModel')
                        and pending.get('requestId') == request_id
                        and pending.get('requestedAt') == self.state.get('requestedAt')
                    )
                    if automatic and (
                        self.state['mode'] != automatic_mode
                        or pending.get('model') != target
                        or self.state.get('requestedModel')
                    ):
                        raise ExternalChange(
                            'Automatic switching was paused or replaced before its command started.'
                        )
                if (
                    self.stop.is_set()
                    or self.service_disabled() is not False
                    or model != previous
                    or not same_selection(raw, previous)
                    or session_key(raw) != session_key(reference)
                    or device_id(raw) != device
                    or not identity_matches
                    or (pending.get('session') and pending['session'] != session_key(raw))
                    or (
                        pending.get('launchSignature')
                        and pending['launchSignature'] != launch_signature(options, environment)
                    )
                ):
                    raise ExternalChange(
                        'The provider session or launch settings changed before the switch.'
                    )
                if excursion:
                    # The daily limit counts excursion starts. Check it before the cleanup,
                    # CLI listing and catalog fetch that a refused move would waste.
                    try:
                        limit = self.demand_auto.excursion_limit(
                            account, device, saved_policy, time.time()
                        )
                    except ValueError as error:
                        raise DemandDeferred(str(error)) from None
                    if limit:
                        raise DemandDeferred(limit)
                if target != previous and self.purge_before_load(target):
                    # Every later check, including memory, uses post-purge readings.
                    purged = self.read_state()
                    if (
                        session_key(purged) != session_key(raw)
                        or device_id(purged) != device
                        or not same_selection(purged, previous)
                    ):
                        raise ExternalChange(
                            'The provider session changed during file-cache cleanup.'
                        )
                    raw = purged
                if (
                    not finite(raw.get('written_at'))
                    or not -5 < time.time() - raw['written_at'] < 10
                    or activity_counters(raw) is None
                    or activity_counters(reference) is None
                ):
                    raise ValueError(
                        'Current work counters or provider readings could not be verified.'
                    )
                if any(
                    new < old
                    for new, old in zip(activity_counters(raw), activity_counters(reference))
                ):
                    raise ValueError('Provider work counters reset before the switch.')
                graceful = graceful_drain(raw)
                if (
                    raw.get('inference_active') is True or not same_activity(raw, reference)
                ) and not (timed_pause or automatic or graceful):
                    raise WorkResumed()
                if not isinstance(raw.get('inference_active'), bool):
                    raise ValueError('Provider activity could not be verified.')
                known_working = readiness(
                    raw,
                    self.warmup,
                    time.time(),
                    self.identity_ok and 0 <= time.time() - self.identity_at < 180,
                    False,
                )['counting']
                if automatic and automatic_mode in ('week', 'combo'):
                    held = self.scheduled_trial_hold(time.time(), self.live or {}, raw)
                    if held['active']:
                        raise DemandDeferred(held['reason'])
                if automatic or verify_runtime or selected_operation:
                    known = move == 'home' and self.known_target(
                        move, target, self.live, time.time()
                    )
                    self.verify_local_target(
                        target, options, **({'catalog_optional': True} if known else {})
                    )
                budget = self.selection_budget(target, self.live or {}, raw, manual=verify_runtime)
                if (
                    not budget
                    or budget['afterUnloadGB'] < budget['requiredGB']
                    or self.wait_reason(
                        self.live,
                        time.time(),
                        request_kind.startswith('manual'),
                        move=move,
                        target=target,
                    )
                ):
                    if request_kind == 'demand':
                        if budget and budget['afterUnloadGB'] < budget['requiredGB']:
                            with self.lock:
                                self.state['purgeShortfall'] = {'model': target, 'at': time.time()}
                                self.save()
                        raise DemandDeferred(
                            'Memory, power or fresh source readings changed; keeping the current model.'
                        )
                    raise ValueError(
                        'Memory, power, hardware or source readings changed before restart.'
                    )
                if request_kind == 'demand':
                    with self.lock:
                        if self.state['mode'] != 'demand':
                            raise ExternalChange(
                                'Demand following was paused before its command started.'
                            )
                    verified = (
                        self.identity_ok
                        and time.time() - self.identity_at < 180
                        and self.identity_session == (raw.get('started_at'), raw.get('pid'))
                    )
                    # Leaving a model that is not counting is fine when going home.
                    if (
                        move != 'home'
                        and not readiness(raw, self.warmup, time.time(), verified, False)[
                            'counting'
                        ]
                    ):
                        raise DemandDeferred(
                            'The current model is no longer freshly warm and routable; holding the switch.'
                        )
                    stall_restart = pending.get('demandKind') == 'stall-restart'
                    if stall_restart:
                        with self.lock:
                            rules = demand_policy(self.state.get('demandPolicy'))
                        decision = (
                            self.stall.confirm_restart(
                                account, device, raw, target, rules, time.time()
                            )
                            if target == previous
                            else None
                        )
                        if not decision:
                            raise DemandDeferred(
                                'Work resumed or the stall changed before the restart. Keeping the current session.'
                            )
                    else:
                        decision = self.demand_decision(
                            time.time(), raw=raw, preflight=getattr(self, 'demand_preflight', None)
                        )
                    if not stall_restart and (
                        decision.get('target') != target
                        or (
                            pending.get('demandKind')
                            and decision.get('kind') != pending['demandKind']
                        )
                        or (decision.get('kind') == 'explore' and not decision.get('escapeReady'))
                    ):
                        raise DemandDeferred(
                            'The net earnings or demand opportunity changed before restart. Keeping the current model and scanning again.'
                        )
                    with self.lock:
                        minimum_run_at = (
                            self.state.get('lastSwitchAt', 0)
                            + decision['policy']['minRunMinutes'] * 60
                        )
                    if (
                        decision.get('kind') == 'earnings'
                        and time.time() < minimum_run_at
                        and not decision.get('completedTrialExit')
                    ):
                        raise DemandDeferred(
                            'The minimum run is not complete and a fresh completed-trial exit is no longer supported. Keeping the current model and rechecking.'
                        )
                    # Work may advance during preflight. Identity, monotonic
                    # counters, settings and readiness still must be verified.
                    final = self.read_state()
                    selection, final_options, final_environment = self.read_options()
                    if (
                        selection != previous
                        or session_key(final) != session_key(raw)
                        or device_id(final) != device
                        or not same_selection(final, previous)
                        or final_options != options
                        or final_environment != environment
                        or self.service_disabled() is not False
                    ):
                        raise ExternalChange('Provider changed during the automatic preflight.')
                    if (
                        not finite(final.get('written_at'))
                        or not -5 < time.time() - final['written_at'] < 10
                        or not isinstance(final.get('inference_active'), bool)
                        or activity_counters(final) is None
                        or any(
                            new < old
                            for new, old in zip(activity_counters(final), activity_counters(raw))
                        )
                    ):
                        raise DemandDeferred(
                            'Fresh monotonic provider activity could not be verified before restart.'
                        )
                    final_budget = self.selection_budget(target, self.live or {}, final)
                    if (
                        not final_budget
                        or final_budget['afterUnloadGB']
                        < final_budget['requiredGB'] + decision['policy']['memoryHeadroomGB']
                        or self.wait_reason(self.live, time.time(), move=move, target=target)
                        or (
                            move != 'home'
                            and not readiness(final, self.warmup, time.time(), verified, False)[
                                'counting'
                            ]
                        )
                    ):
                        raise DemandDeferred(
                            'Memory or source freshness changed during preflight; keeping the current model.'
                        )
                    with self.lock:
                        if self.stop.is_set() or self.state['mode'] != 'demand':
                            raise ExternalChange('Demand following was paused.')
                        decision['activeAtDispatch'] = final['inference_active']
                        decision['workAdvancedDuringPreflight'] = not same_activity(final, raw)
                        try:
                            auto_run = (
                                self.demand_auto.begin_recovery
                                if stall_restart
                                else self.demand_auto.begin_manager
                                if decision.get('kind') in ('home', 'excursion')
                                else self.demand_auto.begin
                            )(account, device, decision, time.time())
                        except ValueError as error:
                            raise DemandDeferred(str(error)) from None
                        self.state['pending']['autoRunId'] = auto_run
                        self.save()
                if automatic and request_kind != 'demand':
                    final = self.read_state()
                    selection, final_options, final_environment = self.read_options()
                    with self.lock:
                        if (
                            self.stop.is_set()
                            or self.state['mode'] != automatic_mode
                            or self.state.get('requestedModel')
                            or selection != previous
                            or session_key(final) != session_key(raw)
                            or device_id(final) != device
                            or not same_selection(final, previous)
                            or final_options != options
                            or final_environment != environment
                            or self.service_disabled() is not False
                        ):
                            raise ExternalChange(
                                'Provider or automatic plan changed during preflight.'
                            )
                    if (
                        not finite(final.get('written_at'))
                        or not -5 < time.time() - final['written_at'] < 10
                        or not isinstance(final.get('inference_active'), bool)
                        or activity_counters(final) is None
                        or any(
                            new < old
                            for new, old in zip(activity_counters(final), activity_counters(raw))
                        )
                    ):
                        raise ValueError('Current monotonic provider work could not be verified.')
                    final_budget = self.selection_budget(target, self.live or {}, final)
                    if (
                        not final_budget
                        or final_budget['afterUnloadGB'] < final_budget['requiredGB']
                        or self.wait_reason(self.live, time.time())
                    ):
                        raise ValueError(
                            'Memory, power or source readings changed during preflight.'
                        )
                if verify_runtime or selected_operation:
                    # Discovery may take seconds. Recheck the exact acknowledged
                    # request and current work immediately before changing service.
                    final = self.read_state()
                    selection, final_options, final_environment = self.read_options()
                    with self.lock:
                        if (
                            self.stop.is_set()
                            or self.state['mode'] != 'observe'
                            or self.state.get('requestedKind') != 'manual'
                            or self.state.get('requestedModel') != target
                            or self.state.get('requestId') != request_id
                            or (
                                verify_runtime
                                and self.state.get('requestedVerifyRuntime') is not True
                            )
                            or self.state.get('pending') != pending
                            or selection != previous
                            or session_key(final) != session_key(raw)
                            or device_id(final) != device
                            or not same_selection(final, previous)
                            or final_options != options
                            or final_environment != environment
                            or self.service_disabled() is not False
                        ):
                            raise ExternalChange(
                                'Provider or manual verification request changed during preflight.'
                            )
                    if (
                        not finite(final.get('written_at'))
                        or not -5 < time.time() - final['written_at'] < 10
                        or not isinstance(final.get('inference_active'), bool)
                        or activity_counters(final) is None
                        or any(
                            new < old
                            for new, old in zip(activity_counters(final), activity_counters(raw))
                        )
                    ):
                        raise ValueError(
                            'Fresh provider work could not be verified before the manual switch.'
                        )
                    if (final['inference_active'] or not same_activity(final, raw)) and not (
                        timed_pause or graceful
                    ):
                        raise WorkResumed()
                    final_budget = self.selection_budget(
                        target, self.live or {}, final, manual=verify_runtime
                    )
                    if (
                        (verify_runtime and not known_working)
                        or not final_budget
                        or final_budget['afterUnloadGB'] < final_budget['requiredGB']
                        or self.wait_reason(self.live, time.time(), manual=True, move='manual')
                    ):
                        raise ValueError(
                            'Readiness, memory, power or verification eligibility changed. Keeping the current model.'
                        )
                self.store.event(
                    account,
                    device,
                    start,
                    'switching',
                    target,
                    'Restarting '
                    + selection_label(target)
                    + ' on the same model because work stopped while network demand held.'
                    if target == previous and pending.get('demandKind') == 'stall-restart'
                    else 'Restarting from '
                    + selection_label(previous)
                    + (
                        ' for a qualified automatic switch. Darkbloom refuses new work and finishes accepted requests first.'
                        if automatic and graceful
                        else ' for a qualified automatic switch, without waiting for idle time. Active requests may be interrupted.'
                        if automatic
                        else ' now. Darkbloom refuses new work and finishes accepted requests first.'
                        if graceful
                        else ' after the authorized five-minute manual idle timeout. Active requests may be interrupted.'
                        if timed_pause
                        else ' after an observed idle period.'
                    ),
                )
                if selected_operation and self.manual_selection.endpoint_setup_allowed(options):
                    options = list(options) + ['--local-endpoint']
                with self.lock:
                    if selected_operation:
                        self.warmup = {}
                command_started = time.time()
                attempted = True
                failure_stage = 'start'
                self.manager.commanded(target, command_started)
                self.command(target, options, environment)
                command_returned = time.time()  # after a >= 0.9.9 drain: the load starts
                failure_stage = 'verify'
                written = self.started_launch(options, environment)
                success = self.verify_started(
                    target,
                    raw.get('started_at'),
                    360,
                    written,
                    # The manager bounds a pre-warm memory wait, then restores (manager.py).
                    **({'memory_seconds': manager.MEMORY_WAIT_SECONDS} if managed else {}),
                    **({'move': move} if move else {}),
                )
                self.log_memory_projection(target, budget)
                if not success:
                    raise getattr(self, 'verification_failure', None) or WarmupError(
                        'The selected model did not become ready before the verification deadline.',
                        code='readiness-timeout',
                    )
                detail = (
                    selection_label(target)
                    + ' is warm and ready. Successful decode and loaded-model status verified.'
                    + self.clearance_note()
                )
            except WorkResumed:
                with self.lock:
                    deferred = bool(
                        (
                            request_kind.startswith('manual')
                            and self.state.get('requestedModel') == target
                            and self.state.get('requestId') == request_id
                        )
                        or (request_kind == 'demand' and self.state['mode'] == 'demand')
                    )
                detail = (
                    'Work arrived before restart. Demand following will recheck the opportunity after a new idle period.'
                    if request_kind == 'demand' and deferred
                    else 'Work arrived before the restart. Your selection is still queued; waiting for a new 12-second idle period.'
                    if deferred
                    else 'Work arrived before the restart. No restart was sent; automatic switching is paused.'
                )
            except DemandDeferred as error:
                with self.lock:
                    deferred = (
                        self.state['mode'] == 'demand'
                        or automatic_mode in ('week', 'combo')
                        and self.state['mode'] == automatic_mode
                        and not self.state.get('requestedModel')
                    )
                    self.state.pop('demandProposal', None)
                detail = str(error)
            except ExternalChange as error:
                external = True
                with self.lock:
                    # The result text follows the state now: the user may have turned it off.
                    managed = managed and manager.active(self.state)
                taken = (
                    self.manager.not_taken(target) if attempted and (managed or picked) else None
                )
                cause = taken or str(error).strip() or None
                if taken:
                    # BloomGauge's own command did not take: a failed switch, never a user change.
                    failure_record = {
                        'model': target,
                        'stage': failure_stage,
                        'code': 'unknown',
                        'recovery': 'blocked',
                        'recoveryCode': 'provider-changed',
                    }
                    detail = taken + (
                        '. The manager restores a working model.'
                        if managed
                        else '. Your pick is kept; the manager starts or restores it.'
                    )
                else:
                    # Name the check that fired (there are many); the generic text is the fallback.
                    cause = str(error).strip() or (
                        'The provider was stopped or its model or launch settings changed during the switch.'
                    )
                    detail = cause + (
                        ' Automatic control continues and keeps that change.'
                        if managed or picked
                        else ' Automatic switching is paused; your manual choice is preserved.'
                    )
            except Exception as error:
                with self.lock:
                    managed = managed and manager.active(self.state)
                code = (
                    error.code
                    if isinstance(error, WarmupError)
                    else 'startup-timeout'
                    if failure_stage == 'start' and isinstance(error, subprocess.TimeoutExpired)
                    else 'startup-command'
                    if failure_stage == 'start'
                    else 'unknown'
                )
                primary = (
                    str(error)
                    if isinstance(error, WarmupError)
                    else 'Darkbloom’s start command timed out.'
                    if code == 'startup-timeout'
                    else 'Darkbloom’s start command failed.'
                    if code == 'startup-command'
                    else 'The switch could not be verified from current provider readings.'
                )
                cause = primary
                failure_record = {
                    'model': target,
                    'stage': failure_stage,
                    'code': code,
                    'recovery': 'not-attempted',
                    'recoveryCode': 'none',
                }
                late_allowed = failure_stage == 'verify' and code in (
                    'readiness-timeout',
                    'warmup-capacity',
                    'warmup-transport',
                )
                expected_launch = written or launch_signature(options, environment)
                if (
                    attempted
                    and late_allowed
                    and self.target_serving_after_wait(
                        target, raw.get('started_at'), device, expected_launch
                    )
                ):
                    success = True
                    detail = (
                        selection_label(target)
                        + ' is warm and ready. Serving output verified after a delayed warm-up.'
                    )
                elif (
                    attempted
                    and known_working
                    and not self.stop.is_set()
                    and self.service_disabled() is False
                ):
                    try:
                        before = self.recovery_ready(previous, target, device, 60, expected_launch)
                        if late_allowed and self.target_serving_after_wait(
                            target, raw.get('started_at'), device, expected_launch
                        ):
                            success = True
                        if not success:
                            if before is None:
                                failure_record.update(
                                    recovery='blocked',
                                    recoveryCode=getattr(
                                        self, 'recovery_block_code', 'idle-not-verified'
                                    ),
                                )
                                raise ExternalChange('No safe idle recovery window.')
                            failure_record.update(
                                recovery='blocked', recoveryCode='resource-or-identity'
                            )
                            self.verify_local_target(previous, options)
                            recovery_budget = self.selection_budget(
                                previous, self.live or {}, before or {}
                            )
                            if (
                                recovery_budget
                                and recovery_budget['afterUnloadGB']
                                >= recovery_budget['requiredGB']
                                and self.recovery_identity(
                                    before, account, device, time.time(), move='restore'
                                )
                            ):
                                selection, latest_options, latest_environment = self.read_options()
                                latest = self.read_state()
                                if (
                                    self.stop.is_set()
                                    or self.service_disabled() is not False
                                    or selection not in (previous, target)
                                    or launch_signature(latest_options, latest_environment)
                                    != expected_launch
                                    or session_key(latest) != session_key(before)
                                    or device_id(latest) != device
                                    or not same_selection(latest, selection)
                                    or not same_activity(latest, before)
                                    or latest.get('inference_active') is not False
                                    or not finite(latest.get('written_at'))
                                    or not -5 < time.time() - latest['written_at'] < 15
                                    or not self.recovery_identity(
                                        latest, account, device, time.time(), move='restore'
                                    )
                                ):
                                    raise ExternalChange(
                                        'Provider or launch settings changed before recovery.'
                                    )
                                # Network proof can take time. Re-read local work and
                                # settings afterwards; do not restart work that arrived.
                                final = self.read_state()
                                selection, latest_options, latest_environment = self.read_options()
                                final_budget = self.selection_budget(
                                    previous, self.live or {}, final
                                )
                                if (
                                    self.stop.is_set()
                                    or self.service_disabled() is not False
                                    or selection not in (previous, target)
                                    or launch_signature(latest_options, latest_environment)
                                    != expected_launch
                                    or session_key(final) != session_key(before)
                                    or device_id(final) != device
                                    or not same_selection(final, selection)
                                    or not same_activity(final, before)
                                    or final.get('inference_active') is not False
                                    or not finite(final.get('written_at'))
                                    or not -5 < time.time() - final['written_at'] < 15
                                    or not final_budget
                                    or final_budget['afterUnloadGB'] < final_budget['requiredGB']
                                    or self.environment_reason(
                                        self.live, time.time(), manual=True, move='restore'
                                    )
                                ):
                                    raise ExternalChange(
                                        'Work, settings or resources changed after recovery verification.'
                                    )
                                failure_record.update(
                                    recovery='failed', recoveryCode='restore-not-ready'
                                )
                                self.manager.commanded(previous, time.time())
                                self.command(previous, options, environment)
                                expected_launch = self.started_launch(options, environment)
                                recovered = self.verify_started(
                                    previous,
                                    before.get('started_at'),
                                    360,
                                    expected_launch,
                                    move='restore',
                                )
                                if recovered:
                                    failure_record.update(
                                        recovery='restored', recoveryCode='restored'
                                    )
                    except Exception:
                        recovered = False
                    if success:
                        detail = (
                            selection_label(target)
                            + ' is warm and ready. Serving output verified while waiting; no recovery restart was needed.'
                        )
                    elif recovered:
                        # A pick made under the manager resumes it (manager.resume_manual), so
                        # its failure says automatic control continues, as the manager's own do.
                        detail = (
                            primary
                            + ' Restored and pre-warmed '
                            + selection_label(previous)
                            + (
                                '. Automatic control continues.'
                                if managed or picked
                                else '. Automatic switching is paused.'
                            )
                        )
                    else:
                        recovery_detail = (
                            'A safe idle window for restoration was not available; no recovery restart was sent.'
                            if failure_record['recoveryCode'] == 'idle-not-verified'
                            else 'The provider stopped or its settings changed; that change was preserved.'
                            if failure_record['recoveryCode'] == 'provider-changed'
                            else 'Safe restoration could not be verified; no forced recovery restart was sent.'
                            if failure_record['recovery'] == 'blocked'
                            else 'The previous model could not be confirmed ready after restoration.'
                        )
                        detail = (
                            primary
                            + ' '
                            + recovery_detail
                            + (
                                ' The manager checks whether it becomes ready, then restores the previous model.'
                                if managed or picked
                                else ' Automatic switching is paused. Review Help & feedback on the Mac.'
                            )
                        )
                elif attempted:
                    failure_record.update(recovery='blocked', recoveryCode='provider-changed')
                    detail = primary + (
                        ' The provider stopped or its launch state is unknown. Automatic control continues; nothing is restarted while it is stopped.'
                        if managed or picked
                        else ' The provider stopped or its launch state is unknown. Automatic switching is paused; check Darkbloom on the Mac.'
                    )
                else:
                    detail = (
                        'The provider became busy or its settings changed before the switch. No restart was sent; the manager retries later.'
                        if managed or picked
                        else 'The provider became busy or its settings changed before the switch. No restart was sent; automatic switching is paused.'
                    )
            finally:
                if setup_requested and not deferred:
                    detail = self.setup_result(target, success, cause, detail)
                if not success and not deferred and (attempted or external):
                    # Darkbloom may follow provider.toml's list, or restore it after a failed start.
                    detail = multi_model_notice(self.home, options) or detail
                end = time.time()
                duration = max(0, end - command_started) if attempted else 0
                with self.lock:
                    if failure_record is not None and not success:
                        self.state['lastSwitchFailure'] = {
                            **failure_record,
                            'at': end,
                            'elapsedSeconds': duration,
                        }
                    self.state['lastSwitchResult'] = {
                        'at': end,
                        'outcome': 'deferred'
                        if deferred
                        else 'switched'
                        if success
                        else 'recovered'
                        if recovered
                        else 'failed',
                    }
                # Recording failures (disk full, I/O) must never leave 'pending' set:
                # that would block every later automatic and manual switch.
                try:
                    self.demand_auto.finish(
                        auto_run,
                        account,
                        device,
                        end,
                        'switched' if success else 'recovered' if recovered else 'failed',
                        duration,
                        self.warmup.get('session') if success else None,
                    )
                except Exception:
                    log.exception('Could not record the demand run result')
                try:
                    self.store.event(
                        account,
                        device,
                        end,
                        'switch-deferred'
                        if deferred
                        else 'switched'
                        if success
                        else 'recovered'
                        if recovered
                        else 'failed',
                        target,
                        detail,
                        duration,
                    )
                except Exception:
                    log.exception('Could not record the switch event')
                if success:
                    try:
                        self.switch_alerts.record(
                            account,
                            device,
                            self.warmup.get('session'),
                            previous,
                            target,
                            switch_reason(request_kind, automatic_mode, decision),
                            end,
                        )
                    except Exception:
                        # Notification storage must never change a switch result.
                        pass
                with self.lock:
                    self.state.pop('pending', None)
                    self.previous = None
                    self.idle_since = None
                    self.demand_preflight = None
                    if deferred:
                        self.manager.switch_deferred(request_kind, target, end)
                        if request_kind.startswith('manual'):
                            self.state['manualResult'] = {
                                **self.state.get('manualResult', {}),
                                'id': request_id,
                                'model': target,
                                'status': 'queued',
                                'detail': detail,
                            }
                        self.status = 'waiting'
                        self.detail = detail
                        self.save()
                        return
                    self.state['requestedModel'] = None
                    self.state.update(
                        requestedKind=None,
                        requestId=None,
                        requestedAt=None,
                        requestedSession=None,
                        requestedLaunchSignature=None,
                    )
                    if request_kind.startswith('manual'):
                        self.state['manualResult'] = {
                            'id': request_id,
                            'model': target,
                            'at': end,
                            'status': 'recovered'
                            if recovered or (success and request_kind == 'manual-recovery')
                            else 'completed'
                            if success
                            else 'failed',
                            'detail': detail,
                            'session': getattr(self, 'verification_session', None)
                            or self.warmup.get('session')
                            if success
                            else None,
                        }
                    if success:
                        self.state['expectedModel'] = target
                        self.state['lastSwitchAt'] = end
                        self.state['rollbackModel'] = previous
                        if command_returned is not None:
                            self.manager.load_timed(target, end - command_returned, end)
                    elif not self.manager.switch_failed(
                        request_kind, previous, target, recovered, attempted, failure_record, end
                    ):
                        self.state['mode'] = 'observe'
                        self.cancel_combo(detail)
                    self.manager.switch_done(
                        request_kind, request_id, pending, previous, target, success, decision, end
                    )
                    self.status = (
                        'observing'
                        if self.state['mode'] == 'observe'
                        else 'learning'
                        if self.state['mode'] in ('week', 'combo')
                        else 'optimizing'
                    )
                    self.detail = detail
                    self.save()
                    self.next_identity = 0
                    self.proposal = None
                    if self.state.pop('demandProposal', None):
                        self.save()
