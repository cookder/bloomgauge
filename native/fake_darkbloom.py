"""A fake Darkbloom 0.9.10 and a simulated Mac for end-to-end manager tests (test only).

Bloomkeeper's real Optimizer, manager, manual controls and On/Off control run unchanged
against a temporary HOME: the launch agent plist, provider.toml, daemon-state.json and
local.json are real files, and every `darkbloom`/`launchctl`/`pmset`/`sudo` call goes
through `FakeDarkbloom.run` (the Optimizer's `runner`). Time is virtual (`Clock`): the
app loop, the provider and every wait in a worker (`stop.wait`) advance it, so hours of
provider behaviour run in seconds.

What the fake does, from the 0.9.10 RC source (Layr-Labs/d-inference provider-swift):
- `darkbloom start` (StartCommand+Daemon.launchDaemon): keeps only downloaded, runtime-
  eligible `--model`s ("No models selected." otherwise); `--idle-timeout N` is written to
  provider.toml (setIdleUnloadMinutes); stages `[backend] enabled_models` and restores the
  exact previous file if the drain setup fails (ProviderModelSelection.withReplacement);
  drains the running provider (ServiceDrain: 600 s deadline + 10 s; a killed CLI leaves it
  drained and disabled), stops it and rewrites the plist with its own argv
  (LaunchAgent.serviceProgramArguments: --coordinator-url, --config only when given,
  --model..., then --local-endpoint --port --bind [--no-auth] with defaults 8000 and
  127.0.0.1) and only allow-listed environment variables (passthroughEnvKeys).
- The launchd child (`start --foreground`) serves provider.toml's enabled_models when that
  key exists (usesPinnedModelSelection), else the plist's --model list, else every local
  model; a legacy config path is copied to ~/.config/darkbloom (migrateConfigIfNeeded).
- Startup preload (ProviderLoop+StartupPreload): preload_models, else the previously
  loaded set plus the selection; registration waits up to startup_preload_timeout_secs
  (120) while daemon-state is written every 30 s; afterwards every 5 s.
- `models list` without `--all` lists only enabled_models (ModelsCommand.List).
- Models load on demand: the coordinator routes work only to warm models; a cold model is
  loaded by a local-endpoint request (Bloomkeeper's pre-warm) and unloaded again after
  idle_timeout_mins (60 by default; 0 keeps it loaded).
- Trust: the provider registers after the preload gate and reports trust 'online' after
  `trust_delay` seconds (App Attest pending while it waits).
"""

import copy
import io
import json
import os
import pathlib
import plistlib
import re
import subprocess
import threading
import urllib.error
import urllib.parse

COORDINATOR = 'wss://api.darkbloom.dev/ws/provider'
LABEL = 'io.darkbloom.provider'
CANONICAL = '.config/darkbloom/provider.toml'
CONFIG_PATHS = (
    CANONICAL,
    'Library/Application Support/darkbloom/provider.toml',
    '.config/eigeninference/provider.toml',
    'Library/Application Support/eigeninference/provider.toml',
)
# LaunchAgent.passthroughEnvKeys (+ the two inference keys): everything else is dropped.
PASSTHROUGH_ENV = (
    'DARKBLOOM_DRAIN_TIMEOUT_SECONDS',
    'DARKBLOOM_PREFIX_CACHE',
    'DARKBLOOM_PREFIX_CACHE_MEMORY',
    'DARKBLOOM_MLX_RESOURCE_DEBUG',
    'DARKBLOOM_CBV2_PAGED_KV',
    'DARKBLOOM_CBV2_MTP',
    'DARKBLOOM_MTP_MAX_RECTANGULAR_TOKENS',
    'DARKBLOOM_KV_BACKEND_GUARD',
    'DARKBLOOM_MLX_CACHE_LIMIT_GB',
    'DARKBLOOM_MLX_MEMORY_RESERVE_GB',
    'DARKBLOOM_CBV2_MAX_PARTIAL_PREFILLS',
    'DARKBLOOM_PREFILL_DEADLINE_MODE',
)
OS_RESERVE_GB = 4  # HardwareDetector: memoryAvailableGb = total - OS reserve (model scan)
LOAD_HEADROOM_GB = 4  # ModelLoadAdmission: free memory needed beyond the weights
STATE_SECONDS = 5  # heartbeat / 2
PRELOAD_STATE_SECONDS = 30  # preloadLivenessRefreshInterval
PRELOAD_GATE_SECONDS = 120  # startup_preload_timeout_secs default


def version_tuple(text):
    return tuple(int(v) for v in re.findall(r'\d+', text)[:3])


# ---- virtual time -----------------------------------------------------------------------


class Clock:
    def __init__(self, start):
        self.now = float(start)

    def time(self):
        return self.now


class Stop:
    """Replaces Optimizer.stop: every wait in the app advances virtual time."""

    def __init__(self, mac):
        self.mac = mac
        self.flag = False

    def is_set(self):
        return self.flag

    def set(self):
        self.flag = True

    def clear(self):
        self.flag = False

    def wait(self, timeout=None):
        if not self.flag:
            self.mac.sleep(timeout if timeout is not None else 1)
        return self.flag


# ---- provider.toml (toml++ output: literal strings, `[ 'a' ]` arrays) -------------------


def toml_value(value):
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return "'%s'" % value if "'" not in value else json.dumps(value)
    if isinstance(value, list):
        if not value:
            return '[]'
        items = [toml_value(v) for v in value]
        # toml++ breaks an array over lines once its inline width reaches 120 columns.
        if 3 + sum(len(i) + 2 for i in items) >= 120:
            return '[\n' + ',\n'.join('    ' + i for i in items) + '\n]'
        return '[ ' + ', '.join(items) + ' ]'
    raise TypeError(value)


def dump_toml(table):
    lines = ['%s = %s' % (k, toml_value(v)) for k, v in table.items() if not isinstance(v, dict)]
    for name, section in table.items():
        if isinstance(section, dict):
            if lines:
                lines.append('')
            lines.append('[%s]' % name)
            lines += ['%s = %s' % (k, toml_value(v)) for k, v in section.items()]
    return '\n'.join(lines) + '\n'


def parse_toml(text):
    table = {}
    section = table
    lines = iter(text.splitlines())
    for line in lines:
        line = line.split('#', 1)[0].strip()
        if not line:
            continue
        if line.startswith('['):
            section = table.setdefault(line.strip('[] '), {})
            continue
        key, _, value = (part.strip() for part in line.partition('='))
        if value.startswith('[') and not value.endswith(']'):
            for more in lines:
                value += more.split('#', 1)[0].strip()
                if value.endswith(']'):
                    break
        section[key] = parse_value(value)
    return table


def parse_value(value):
    if value.startswith('['):
        inner = value[1:-1].strip().rstrip(',')
        return [parse_value(v.strip()) for v in inner.split(',') if v.strip()]
    if value[:1] in '\'"':
        return value[1:-1]
    if value in ('true', 'false'):
        return value == 'true'
    return int(value) if re.fullmatch(r'-?\d+', value) else float(value)


# ---- models -----------------------------------------------------------------------------


class Model:
    def __init__(self, id, gb, min_ram=24, load=30, caps=None, template=True, active=True):
        self.id, self.gb, self.min_ram, self.load = id, gb, min_ram, load
        self.caps, self.template, self.active = caps or [], template, active

    def catalog(self):
        row = {'id': self.id, 'active': self.active, 'min_ram_gb': self.min_ram, 'size_gb': self.gb}
        if self.caps:
            row['required_provider_capabilities'] = list(self.caps)
        return row

    def local(self):
        return {
            'id': self.id,
            'estimated_memory_gb': self.gb,
            'size_bytes': int(self.gb * 1e9),
            'template_render_ok': self.template,
        }


GEMMA = 'gemma-4-26b-qat-4bit'
QWEN = 'qwen3.5-35b-a3b'
GPT = 'gpt-oss-20b'
QWEN38 = 'qwen3.8-flash-next'  # runtime-gated (apple_m5 + mlx_nax)
BIG = 'qwen3-235b-a22b-4bit'
MODELS = {
    GEMMA: Model(GEMMA, 15.6, 36, 35),
    QWEN: Model(QWEN, 20.9, 36, 45),
    GPT: Model(GPT, 12.1, 24, 25),
    QWEN38: Model(QWEN38, 18.2, 36, 40, caps=['apple_m5', 'mlx_nax']),
    BIG: Model(BIG, 132.0, 192, 150),
}


# ---- the provider -----------------------------------------------------------------------


class Process:
    """One `darkbloom start --foreground` launched by launchd."""

    def __init__(self, pid, at, models, endpoint, idle_minutes, version):
        self.pid, self.started_at, self.models = pid, at, sorted(models)
        self.endpoint, self.idle_minutes, self.version = endpoint, idle_minutes, version
        self.warm = {}  # model -> last use (load or request)
        self.loading = None  # (model, done_at, via)
        self.preload = []  # startup plan still to load
        self.gate_until = None  # registration waits for the preload until then
        self.registered_at = None
        self.trusted_at = None
        self.requests = self.tokens = 0
        self.active_until = 0
        self.lifecycle = {'outcome': 'serving', 'remaining': 0}
        self.written_at = None
        self.load_error = None
        self.current = None


class FakeDarkbloom:
    """The provider side of one Mac. `mac` supplies the clock and the memory readings."""

    def __init__(
        self,
        mac,
        version='0.9.10',
        runtime_caps=(),
        load_scale=1.0,
        trust_delay=5,
        drain_seconds=4,
        traffic_seconds=60,
        downloaded=(GEMMA, QWEN, GPT),
        rotating_key=False,
    ):
        self.mac = mac
        # Darkbloom can't use its keychain key (e.g. a locked keychain): a new temporary
        # attestation key on every start, so the device id changes with each process.
        self.rotating_key = rotating_key
        self.home = mac.home
        self.version = version
        self.runtime_caps = sorted(runtime_caps)
        self.load_scale = load_scale
        self.trust_delay = trust_delay
        self.drain_seconds = drain_seconds
        self.traffic_seconds = traffic_seconds  # None: no network work at all
        self.downloaded = [MODELS[m] for m in downloaded]
        self.proc = None
        self.pids = 400
        self.disabled = False
        self.loaded_store = []  # LoadedModelsStore: the persisted previously-served set
        self.key = 'se-key-' + mac.name
        self.api_key = 'local-key'
        self.commands = []  # every CLI invocation: (at, argv)
        self.starts = []  # (at, argv) of `start` commands that reached the plist rewrite
        self.exits = []  # (at, reason) of provider processes that ended on their own
        self.bin = self.home / '.darkbloom/bin/darkbloom'
        self.bin.parent.mkdir(parents=True, exist_ok=True)
        self.bin.write_text('#!/bin/sh\n')
        self.state_path = self.home / '.darkbloom/daemon-state.json'
        self.plist_path = self.home / 'Library/LaunchAgents' / (LABEL + '.plist')
        self.plist_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    # -- files ------------------------------------------------------------------------

    def write_plist(self, args, env=None):
        plist = {
            'Label': LABEL,
            'ProgramArguments': [str(self.bin), 'start', '--foreground', *args],
            'KeepAlive': False,
            'RunAtLoad': True,
            'ExitTimeOut': 3660,
        }
        if env:
            plist['EnvironmentVariables'] = dict(env)
        self.plist_path.write_bytes(plistlib.dumps(plist))

    def plist(self):
        return plistlib.loads(self.plist_path.read_bytes())

    def plist_args(self):
        return self.plist()['ProgramArguments'][3:]

    def config_path(self, explicit=None):
        if explicit:
            return pathlib.Path(explicit).expanduser()
        return next(
            (self.home / p for p in CONFIG_PATHS if (self.home / p).exists()),
            self.home / CANONICAL,
        )

    def read_config(self, explicit=None):
        path = self.config_path(explicit)
        return parse_toml(path.read_text()) if path.exists() else {}

    def write_config(self, table, path=None):
        path = path or self.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dump_toml(table))

    def migrate(self, explicit=None):
        """Default-path discovery copies a legacy config to the canonical path."""
        if explicit:
            return
        path = self.config_path()
        canonical = self.home / CANONICAL
        if path != canonical and path.exists() and not canonical.exists():
            canonical.parent.mkdir(parents=True, exist_ok=True)
            canonical.write_text(path.read_text())

    # -- CLI --------------------------------------------------------------------------

    def run(self, argv, **kwargs):
        """subprocess.run for every command Bloomkeeper sends."""
        argv = [str(a) for a in argv]
        with self.lock:
            self.commands.append((self.mac.clock.time(), argv))
        if argv[0] == '/bin/launchctl':
            text = '\t"%s" => %s\n' % (LABEL, 'true' if self.disabled else 'false')
            return subprocess.CompletedProcess(argv, 0, stdout='disabled services = {\n%s}\n' % text)
        if argv[0] == '/usr/bin/pmset':
            return subprocess.CompletedProcess(argv, 0, stdout="Now drawing from 'AC Power'\n")
        if argv[0] in ('/usr/bin/sudo', '/usr/bin/security'):
            return subprocess.CompletedProcess(argv, 1, stdout='', stderr='')
        if argv[0] != str(self.bin):
            raise FileNotFoundError(argv[0])
        command = argv[1:]
        if command[:2] == ['models', 'list']:
            return self.models_list(argv, command[2:], kwargs)
        if command[:1] == ['start']:
            return self.start_command(argv, command[1:], kwargs)
        if command[:1] == ['stop']:
            return self.stop_command(argv, kwargs)
        raise subprocess.CalledProcessError(64, argv, stderr='Error: Unknown command')

    def parse_start(self, argv, args):
        values = {'--model': [], '--config': None, '--coordinator-url': None, '--port': '8000',
                  '--bind': '127.0.0.1', '--idle-timeout': None, '--timeout': '600'}
        flags = set()
        i = 0
        while i < len(args):
            a = args[i]
            if a in ('--local-endpoint', '--no-auth', '--all', '--force', '--foreground'):
                flags.add(a)
                i += 1
            elif a in values and i + 1 < len(args):
                if a == '--model':
                    values[a].append(args[i + 1])
                else:
                    values[a] = args[i + 1]
                i += 2
            else:
                raise subprocess.CalledProcessError(
                    64, argv, stderr="Error: Unknown option '%s'" % a
                )
        return values, flags

    def eligible(self, model_id):
        model = MODELS.get(model_id)
        return bool(
            model
            and model in self.downloaded
            and model.gb <= self.mac.memory_gb - OS_RESERVE_GB
            and set(model.caps) <= set(self.runtime_caps)
        )

    def models_list(self, argv, args, kwargs):
        config = args[args.index('--config') + 1] if '--config' in args else None
        self.migrate(config)
        enabled = (self.read_config(config).get('backend') or {}).get('enabled_models') or []
        if '--all' in args and version_tuple(self.version) < (0, 9, 10):
            raise subprocess.CalledProcessError(64, argv, stderr="Error: Unknown option '--all'")
        rows = [m.local() for m in self.downloaded if m.gb <= self.mac.memory_gb - OS_RESERVE_GB]
        if '--all' not in args and enabled:
            rows = [r for r in rows if r['id'] in enabled]
        body = {'cacheDirectory': str(self.home / '.cache/huggingface/hub'),
                'filteredByConfig': '--all' not in args and bool(enabled), 'models': rows}
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(body), stderr='')

    def elapse(self, seconds, deadline, argv, kwargs):
        """The CLI blocks for `seconds`; Bloomkeeper kills it at its subprocess timeout."""
        limit = kwargs.get('timeout')
        if limit is not None and self.mac.clock.time() + seconds > deadline:
            self.mac.sleep(max(0, deadline - self.mac.clock.time()))
            raise subprocess.TimeoutExpired(argv, limit)
        self.mac.sleep(seconds)

    def drain(self, argv, kwargs, deadline, timeout):
        """ServiceDrain.prepare + wait: fence recovery, publish the request, wait for drained."""
        proc = self.proc
        self.disabled = True  # LaunchAgent.disableAutomaticStartup
        if not proc:
            return
        proc.lifecycle = {'outcome': 'draining', 'remaining': 1 if self.busy(proc) else 0}
        wait = self.drain_seconds + (proc.active_until - self.mac.clock.time() if self.busy(proc) else 0)
        if wait > timeout + 10:
            self.elapse(timeout + 10, deadline, argv, kwargs)
            raise subprocess.CalledProcessError(1, argv, stderr='Drain did not complete')
        self.elapse(wait, deadline, argv, kwargs)
        proc.lifecycle = {'outcome': 'drained', 'remaining': 0}
        proc.warm = {}
        self.write_state()

    def start_command(self, argv, args, kwargs):
        values, flags = self.parse_start(argv, args)
        start = self.mac.clock.time()
        limit = kwargs.get('timeout')
        deadline = start + limit if limit is not None else float('inf')
        env = kwargs.get('env') or {}
        explicit = values['--config']
        self.migrate(explicit)
        config_path = self.config_path(explicit)
        if values['--idle-timeout'] is not None:
            table = self.read_config(explicit)
            table.setdefault('backend', {})['idle_timeout_mins'] = int(values['--idle-timeout'])
            self.write_config(table, config_path)
        selected = [m for m in values['--model'] if self.eligible(m)]
        if '--all' in flags:
            selected = [m.id for m in self.downloaded if self.eligible(m.id)]
        if not selected:
            raise subprocess.CalledProcessError(1, argv, stderr='No models selected.')
        # withReplacement: stage enabled_models, restore the exact file if the setup fails.
        original = config_path.read_text() if config_path.exists() else None
        table = self.read_config(explicit)
        table.setdefault('backend', {})['enabled_models'] = selected
        self.write_config(table, config_path)
        try:
            self.drain(argv, kwargs, deadline, int(values['--timeout']))
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            raise  # a published drain keeps its selection
        except Exception:
            if original is None:
                config_path.unlink()
            else:
                config_path.write_text(original)
            raise
        # stopDrainedProvider + installAndStart
        self.end_process('stopped for a start')
        coordinator = values['--coordinator-url'] or (self.read_config(explicit).get('coordinator') or {}).get('url') or COORDINATOR
        out = ['--coordinator-url', coordinator]
        if explicit:
            out += ['--config', os.path.normpath(str(config_path))]
        for m in selected:
            out += ['--model', m]
        if '--local-endpoint' in flags:
            out += ['--local-endpoint', '--port', values['--port'], '--bind', values['--bind']]
            if '--no-auth' in flags:
                out.append('--no-auth')
        self.write_plist(out, {k: env[k] for k in PASSTHROUGH_ENV if env.get(k)})
        self.starts.append((self.mac.clock.time(), argv))
        self.disabled = False
        self.elapse(1, deadline, argv, kwargs)
        self.spawn()
        return subprocess.CompletedProcess(argv, 0, stdout='Provider started as background service.\n', stderr='')

    def stop_command(self, argv, kwargs):
        start = self.mac.clock.time()
        limit = kwargs.get('timeout')
        deadline = start + limit if limit is not None else float('inf')
        self.drain(argv, kwargs, deadline, 600)
        self.end_process('stopped')
        return subprocess.CompletedProcess(argv, 0, stdout='', stderr='')

    # -- user and Darkbloom actions outside Bloomkeeper ------------------------------

    def restart(self, version=None):
        """`darkbloom restart` or a self-update: drain, relaunch from the same plist."""
        if self.proc:
            self.proc.lifecycle = {'outcome': 'draining', 'remaining': 0}
            self.mac.sleep(self.drain_seconds)
            self.end_process('restart')
        if version:
            self.version = version
        self.disabled = False
        self.spawn()

    def crash(self):
        self.end_process('crashed')

    def set_idle(self, minutes):
        """`darkbloom idle N`: provider.toml's idle policy; applies at the next restart."""
        table = self.read_config()
        table.setdefault('backend', {})['idle_timeout_mins'] = minutes
        self.write_config(table)

    # -- process life -----------------------------------------------------------------

    def end_process(self, reason):
        if self.proc:
            self.exits.append((self.mac.clock.time(), reason))
        self.proc = None

    def spawn(self):
        """launchd runs the plist: `start --foreground` with its args."""
        now = self.mac.clock.time()
        args = self.plist_args()
        explicit = args[args.index('--config') + 1] if '--config' in args else None
        self.migrate(explicit)
        config = self.read_config(explicit)
        backend = config.get('backend') or {}
        if 'enabled_models' in backend:
            chosen = backend['enabled_models']
        else:
            chosen = [args[i + 1] for i, a in enumerate(args[:-1]) if a == '--model']
            if not chosen:
                chosen = [m.id for m in self.downloaded]
        models = [m for m in dict.fromkeys(chosen) if self.eligible(m)]
        if not models:
            self.exits.append((now, 'No models selected.'))
            self.proc = None
            return
        endpoint = None
        if '--local-endpoint' in args:
            endpoint = {
                'port': int(args[args.index('--port') + 1]) if '--port' in args else 8000,
                'bind': args[args.index('--bind') + 1] if '--bind' in args else '127.0.0.1',
                'auth': '--no-auth' not in args,
            }
        idle = backend.get('idle_timeout_mins')
        if idle is None and '--idle-timeout' in args:
            idle = int(args[args.index('--idle-timeout') + 1])
        self.pids += 1
        if self.rotating_key:
            self.key = 'se-key-%s-%d' % (self.mac.name, self.pids)
        proc = Process(self.pids, now, models, endpoint, 60 if idle is None else idle, self.version)
        if backend.get('startup_preload', True):
            plan = backend.get('preload_models') or (
                sorted(self.loaded_store, key=lambda m: -MODELS[m].gb) + models
            )
            proc.preload = [m for m in dict.fromkeys(plan) if m in models]
        proc.gate_until = now + int(backend.get('startup_preload_timeout_secs', PRELOAD_GATE_SECONDS))
        if not proc.preload:
            self.register(proc, now)
        self.proc = proc
        if endpoint:
            path = self.home / '.darkbloom/local.json'
            path.write_text(json.dumps({
                'base_url': 'http://%s:%d/v1' % ('127.0.0.1', endpoint['port']),
                'port': endpoint['port'],
                'pid': proc.pid,
                'api_key': self.api_key,
            }))
            os.chmod(path, 0o600)
        self.write_state()

    def register(self, proc, now):
        proc.registered_at = now
        proc.trusted_at = now + self.trust_delay

    def busy(self, proc):
        return proc.active_until > self.mac.clock.time()

    def load_seconds(self, model):
        return MODELS[model].load * self.load_scale

    def free_gb(self):
        return self.mac.memory_available()

    def begin_load(self, proc, model, now, via):
        need = MODELS[model].gb + LOAD_HEADROOM_GB
        if self.free_gb() < need:
            proc.load_error = {'model': model, 'at': now, 'reason': 'insufficient memory'}
            return False
        proc.loading = (model, now + self.load_seconds(model), via)
        return True

    def step(self, now):
        """Advance the provider to `now` (called in small steps by the Mac)."""
        proc = self.proc
        if not proc:
            return
        # loads (startup preload runs one model after another)
        if proc.loading and now >= proc.loading[1]:
            model = proc.loading[0]
            proc.warm[model] = now
            proc.current = model
            proc.loading = None
            self.loaded_store = sorted(proc.warm)
        if proc.preload and not proc.loading and proc.lifecycle['outcome'] == 'serving':
            model = proc.preload.pop(0)
            if model not in proc.warm and not self.begin_load(proc, model, now, 'preload'):
                pass  # skipped for memory; the next candidate may fit
        preloading = bool(proc.preload or (proc.loading and proc.loading[2] == 'preload'))
        if proc.registered_at is None and (not preloading or now >= proc.gate_until):
            self.register(proc, now)
        serving = proc.lifecycle['outcome'] == 'serving'
        # network work goes only to warm models of a trusted, serving provider
        if (
            serving
            and self.traffic_seconds
            and proc.trusted_at is not None
            and now >= proc.trusted_at
            and proc.warm
            and not self.busy(proc)
            and now - max(proc.warm.values()) >= self.traffic_seconds
        ):
            model = min(proc.warm, key=lambda m: proc.warm[m])
            proc.warm[model] = now
            proc.requests += 1
            proc.tokens += 180
            proc.active_until = now + 2
        # idle unload
        if proc.idle_minutes:
            for model, used in list(proc.warm.items()):
                if now - used >= proc.idle_minutes * 60 and not self.busy(proc):
                    del proc.warm[model]
                    self.loaded_store = sorted(proc.warm)
        cadence = PRELOAD_STATE_SECONDS if proc.registered_at is None else STATE_SECONDS
        if proc.written_at is None or now - proc.written_at >= cadence:
            self.write_state()

    def write_state(self):
        proc = self.proc
        if not proc:
            return
        now = self.mac.clock.time()
        proc.written_at = now
        state = {
            'pid': proc.pid,
            'process_identity': {'pid': proc.pid, 'start_time_micros': int(proc.started_at * 1e6)},
            'version': proc.version,
            'written_at': now,
            'started_at': proc.started_at,
            'attestation_public_key': self.key,
            'coordinator_url': COORDINATOR,
            'current_model': proc.current,
            'warm_models': sorted(proc.warm),
            'advertised_models': proc.models,
            'startup_preload_pending_models': (
                ([proc.loading[0]] if proc.loading and proc.loading[2] == 'preload' else [])
                + list(proc.preload)
            ),
            'inference_active': self.busy(proc),
            'lifecycle': dict(proc.lifecycle),
            'runtime_capabilities': list(self.runtime_caps),
            'stats': {'requests_served': proc.requests, 'tokens_generated': proc.tokens, 'usage_gaps': 0},
            'capacity': {
                'total_memory_gb': self.mac.memory_gb,
                'gpu_memory_active_gb': self.resident_gb(),
                'gpu_memory_cache_gb': 0,
            },
        }
        if proc.trusted_at is not None and now >= proc.trusted_at:
            state['trust'] = {'trust_level': 'hardware', 'status': 'online', 'reason': '', 'received_at': proc.trusted_at}
        elif proc.registered_at is not None:
            state['trust'] = {'trust_level': 'none', 'status': 'untrusted', 'reason': 'App Attest pending', 'received_at': proc.registered_at}
        if proc.load_error:
            state['last_model_load_error'] = dict(proc.load_error)
        tmp = self.state_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(state))
        tmp.replace(self.state_path)

    def resident_gb(self):
        proc = self.proc
        if not proc:
            return 0
        loading = [proc.loading[0]] if proc.loading else []
        return round(sum(MODELS[m].gb for m in list(proc.warm) + loading), 2)

    def running(self, pid=None):
        return bool(self.proc and (pid is None or self.proc.pid == pid))

    def roster_row(self):
        """The coordinator's /v1/providers/attestation row for this Mac."""
        proc = self.proc
        now = self.mac.clock.time()
        if not proc or proc.trusted_at is None or now < proc.trusted_at:
            return None
        catalog = {m['id']: m for m in self.mac.network.catalog}
        routable = [
            m for m in proc.models
            if catalog.get(m, {}).get('active') and set(MODELS[m].caps) <= set(self.runtime_caps)
        ]
        return {
            'se_public_key': self.key,
            'provider_id': 'provider-' + self.mac.name,
            'models': routable,
            'status': 'serving' if proc.warm else 'online',
            'trust_level': 'hardware',
        }

    # -- the local endpoint (Bloomkeeper's pre-warm) -----------------------------------

    def opener(self):
        fake = self

        class Opener:
            def open(self, request, timeout=None):
                return fake.local_completion(request, timeout)

        return Opener()

    def local_completion(self, request, timeout):
        url = urllib.parse.urlsplit(request.full_url)
        proc = self.proc
        if not proc or not proc.endpoint or proc.endpoint['port'] != url.port:
            raise urllib.error.URLError('connection refused')
        body = json.loads(request.data)
        model = body.get('model')
        if request.get_header('Authorization') != 'Bearer ' + self.api_key:
            raise urllib.error.HTTPError(request.full_url, 401, 'unauthorized', {}, None)
        if model not in proc.models:
            raise urllib.error.HTTPError(request.full_url, 404, 'unknown model', {}, None)
        if proc.lifecycle['outcome'] != 'serving':
            raise urllib.error.HTTPError(request.full_url, 503, 'draining', {}, None)
        if model not in proc.warm:
            if proc.loading and proc.loading[0] != model:
                self.mac.sleep(max(0, proc.loading[1] - self.mac.clock.time()))
                proc = self.proc
                if not proc:
                    raise urllib.error.URLError('connection reset')
            if model not in proc.warm:
                if not proc.loading and not self.begin_load(proc, model, self.mac.clock.time(), 'request'):
                    raise urllib.error.HTTPError(request.full_url, 503, 'insufficient memory', {}, None)
                wait = proc.loading[1] - self.mac.clock.time()
                if timeout is not None and wait > timeout:
                    self.mac.sleep(timeout)
                    raise urllib.error.URLError('timed out')
                self.mac.sleep(max(0, wait) + 1)
                if not self.proc or model not in self.proc.warm:
                    raise urllib.error.URLError('connection reset')
        self.proc.warm[model] = self.mac.clock.time()
        payload = json.dumps({
            'model': model,
            'choices': [{'message': {'content': ''}}],
            'usage': {'completion_tokens': 1},
        }).encode()

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return Response(payload)


# ---- the network and the Mac ------------------------------------------------------------


class FakeNetwork:
    """The public Darkbloom API Bloomkeeper reads (catalog, roster, capacity)."""

    def __init__(self, mac, models=None):
        self.mac = mac
        self.catalog = [MODELS[m].catalog() for m in (models or MODELS)]
        self.listeners = []
        self.error_listeners = []

    def fetch(self, path):
        if path == '/v1/models/catalog':
            return {'models': copy.deepcopy(self.catalog)}
        if path == '/v1/providers/attestation':
            row = self.mac.darkbloom.roster_row()
            others = [{'se_public_key': 'other', 'provider_id': 'p2', 'models': [GEMMA],
                       'status': 'online', 'trust_level': 'hardware'}]
            return {'providers': others + ([row] if row else [])}
        raise OSError('unexpected fetch ' + path)

    def snapshot(self, key=None):
        now = self.mac.clock.time()
        value = {'capacity': {'status': 'ok', 'updatedAt': now - 5, 'data': {'models': []}}}
        return value if key is None else value.get(key) or {}


class Mac:
    """A Mac running Bloomkeeper and a fake Darkbloom on a virtual clock.

    `start()` installs the clock and process patches; `stop()` removes them. `launch()`
    opens Bloomkeeper (a new Optimizer on the Mac's history database); `run()` is the app's
    loops (collector every 3 s, discovery/identity refresh, control tick every 15 s, the On
    control and any worker it starts). The `ui_*` methods do what the app's buttons send.
    """

    def __init__(self, name='mac', memory_gb=48, chip='Apple M5 Pro', start=1790607600, **kwargs):
        import tempfile

        self.name = name
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name)
        self.clock = Clock(start)
        self.memory_gb = memory_gb
        self.chip = chip
        self.base_used_gb = 6
        self.network = FakeNetwork(self)
        self.darkbloom = FakeDarkbloom(self, **kwargs)
        self.app = None
        self.history = None
        self.patches = []
        self.collect_at = self.refresh_at = -1e18
        self.errors = []  # exceptions from the app's loops
        self.account = 'acct'  # the Darkbloom account Bloomkeeper is signed in to
        self.log = []  # (at, kind, detail): UI results and status changes
        self.trace = []  # (at, advertised models) while a selection is loaded and trusted
        self.seen = None

    # -- set-up -----------------------------------------------------------------------

    def start(self):
        from unittest.mock import patch

        alive = lambda identity: None if not identity else self.darkbloom.running(identity['pid'])

        def prewarm(home, raw, options, model, timeout=180, opener=None):
            import prewarm as real

            return real.prewarm(home, raw, options, model, timeout, opener=self.darkbloom.opener())

        for target, value in (
            ('time.time', self.clock.time),
            ('time.monotonic', self.clock.time),
            ('optimizer.matching_process', alive),
            ('manager.matching_process', alive),
            ('provider_control.matching_process', alive),
            ('optimizer.prewarm', prewarm),
        ):
            p = patch(target, value)
            p.start()
            self.patches.append(p)
        return self

    def stop(self):
        if self.app:
            self.app.stop.set()
        for p in reversed(self.patches):
            p.stop()
        self.patches = []
        if self.history:
            self.history.close()
        self.tmp.cleanup()

    def launch(self):
        """Open Bloomkeeper (again): a new Optimizer over the same history database."""
        from unittest.mock import Mock

        from history import History
        from optimizer import Optimizer
        from test_demand_optimizer import decision as legacy_decision

        if self.app:
            self.app.stop.set()
            self.history.close()
        self.history = History(self.home / 'history.sqlite3')
        self.history.cache('account', 'acct')
        self.app = Optimizer(self.history, self.network, self.home, Stop(self), runner=self.darkbloom.run)

        def legacy(account, device, rows, current, raw, rules, now, *args, **kwargs):
            d = legacy_decision()
            d.update(at=now, currentModel=current, policy=rules, target=None, kind=None)
            return d

        # The legacy demand evaluation is display-only under the manager; stub it.
        self.app.demand_auto.evaluate = Mock(side_effect=legacy)
        self.collect_at = self.refresh_at = -1e18
        self.background()
        return self.app

    # -- time -------------------------------------------------------------------------

    def sleep(self, seconds):
        end = self.clock.now + max(0, seconds)
        while self.clock.now < end - 1e-9:
            self.clock.now = min(end, self.clock.now + 1)
            self.darkbloom.step(self.clock.now)
            self.background()

    def background(self):
        """The collector (every 3 s) and the refresh loop (every 15 s)."""
        if not self.app or self.app.stop.is_set():
            return
        now = self.clock.now
        if now - self.collect_at >= 3:
            self.collect_at = now
            self.collect(now)
        if now - self.refresh_at >= 15:
            self.refresh_at = now
            try:
                self.app.refresh(now)
            except Exception as error:  # the real loop logs and carries on
                self.errors.append((now, 'refresh', repr(error)))

    def collect(self, now):
        from provider_reporting import preloading, state_fresh

        o = self.app
        try:
            raw = json.loads(self.darkbloom.state_path.read_text())
        except (OSError, ValueError):
            raw = {}
        online = bool(raw) and state_fresh(raw, now)
        tracking = o.tracking(raw, now)
        provider = {
            'online': online,
            'starting': online and preloading(raw) and (raw.get('trust') or {}).get('status') != 'online',
            'active': online and bool(raw.get('inference_active')),
            'model': raw.get('current_model') or '',
            'version': raw.get('version', ''),
            'memoryGB': (raw.get('capacity') or {}).get('gpu_memory_active_gb'),
            'session': None,
            'tracking': tracking,
        }
        available = self.memory_available()
        snapshot = {
            'at': now,
            'provider': provider,
            'hardware': {
                'chip': self.chip,
                'memoryTotalGB': self.memory_gb,
                'memoryUsedGB': self.memory_gb - available,
                'memoryAvailableGB': available,
                'cachedFilesGB': 0,
                'cpuTemp': 50,
                'gpuTemp': 55,
                'thermal': 'Nominal',
                'at': now,
            },
            'earnings': {'status': 'ok', 'updatedAt': now},
            'pulse': None,
        }
        try:
            o.observe(self.account, raw, snapshot)
        except Exception as error:
            self.errors.append((now, 'observe', repr(error)))

    def memory_available(self):
        return round(self.memory_gb - self.base_used_gb - self.darkbloom.resident_gb(), 2)

    # -- the app's loops --------------------------------------------------------------

    def join(self):
        o = self.app
        for name in ('worker', 'warmup_worker'):
            worker = getattr(o, name)
            if worker and worker.is_alive():
                worker.join(120)
                if worker.is_alive():
                    raise AssertionError('a Bloomkeeper worker did not finish (deadlock?)')

    def tick(self):
        o = self.app
        now = self.clock.now
        try:
            o.tick(now)
        except Exception as error:
            self.errors.append((now, 'tick', repr(error)))
        self.join()
        try:
            o.automatic_control.tick(self.clock.now)
        except Exception as error:
            self.errors.append((now, 'control', repr(error)))
        self.join()
        self.note()

    def run(self, seconds, until=None, step=15):
        end = self.clock.now + seconds
        while self.clock.now < end:
            self.sleep(min(step, end - self.clock.now))
            self.tick()
            if until and until():
                return True
        return bool(until and until())

    def note(self):
        """Record each distinct status the app shows (for loop and message checks) and what
        Darkbloom serves (`trace`: when each selection was loaded and trusted)."""
        import manager

        o = self.app
        with o.lock:
            seen = (o.state.get('mode'), o.status, o.detail)
        if seen != self.seen:
            self.seen = seen
            self.log.append((self.clock.now, 'status', seen))
        raw = self.raw()
        if manager.loaded(raw, self.clock.now)[0]:
            self.trace.append((self.clock.now, tuple(raw.get('advertised_models') or ())))

    # -- what the UI sends ------------------------------------------------------------

    def refresh_ui(self):
        """What a refresh of the Optimizer page does before the user acts."""
        o = self.app
        o.next_discovery = o.next_identity = 0
        self.refresh_at = -1e18
        self.background()
        o.automatic_control.tick(self.clock.now)

    def act(self, kind, call):
        """A UI action: None once accepted (its worker finished), else the refusal text."""
        try:
            call()
        except ValueError as error:
            self.log.append((self.clock.now, kind + '-refused', str(error)))
            return str(error)
        self.join()
        self.log.append((self.clock.now, kind, 'accepted'))
        return None

    def ui_select(self, model, verify=False):
        """Prepare / Start / Switch on one model in the manual model controls."""
        import uuid

        o = self.app
        self.refresh_ui()
        view = o.manual_snapshot()
        body = {
            'action': 'select',
            'requestId': str(uuid.uuid4()),
            'model': model,
            'expectedProvider': view['providerControl'].get('version') or '',
            'expectedSession': view.get('session') or '',
            'verifyRuntime': verify,
        }
        return self.act('select', lambda: o.manual_action(body, 'mac'))

    def ui_provider(self, action):
        """provider-start / provider-stop / provider-endpoint in the model controls."""
        import uuid

        o = self.app
        self.refresh_ui()
        view = o.provider_control.snapshot()
        body = {'action': action, 'requestId': str(uuid.uuid4()), 'expectedProvider': view.get('version') or ''}
        return self.act(action, lambda: o.manual_action(body, 'mac'))

    def control_view(self):
        self.app.automatic_control.tick(self.clock.now)
        return self.app.automatic_control.snapshot()

    def ui_on(self):
        """Manager on (the Optimizer's On), with the UI's first-plan model list."""
        import uuid

        o = self.app
        self.refresh_ui()
        view = o.automatic_control.snapshot()
        if not view['automatic'].get('canEnable'):
            self.log.append((self.clock.now, 'on-refused', view['automatic'].get('detail')))
            return view['automatic'].get('detail') or 'On is unavailable.'
        body = {
            'action': 'set-automatic',
            'enabled': True,
            'requestId': str(uuid.uuid4()),
            'expectedControl': view['controlVersion'],
        }
        if view.get('providerVersion'):
            body['expectedProvider'] = view['providerVersion']
        if view.get('firstPlan'):
            available = [m['id'] for m in view['models'] if m.get('available')]
            saved = [m for m in view.get('selected') or [] if m in available]
            body['models'] = list(dict.fromkeys([view['currentModel']] + (saved or available)))[:16]
            body['demandPolicy'] = dict(view['demandPolicy'])
        return self.act('on', lambda: o.automatic_control.action(body, 'mac'))

    def ui_off(self):
        import uuid

        o = self.app
        self.refresh_ui()
        view = o.automatic_control.snapshot()
        body = {'action': 'set-automatic', 'enabled': False, 'requestId': str(uuid.uuid4()),
                'expectedControl': view['controlVersion']}
        return self.act('off', lambda: o.automatic_control.action(body, 'mac'))

    def ui_save_plan(self):
        """The plan settings panel's Save ("Review settings"): the saved models and rules."""
        o = self.app
        self.refresh_ui()
        with o.lock:
            models = list(o.state.get('models') or [])
        body = {'action': 'update-plan', 'expectedControl': o.control_version(), 'models': models,
                'demandPolicy': {}}
        return self.act('save-plan', lambda: o.control_action(body, 'mac'))

    def ui_release_pin(self):
        import uuid

        o = self.app
        self.refresh_ui()
        view = o.automatic_control.snapshot()
        body = {'action': 'release-pin', 'requestId': str(uuid.uuid4()), 'expectedControl': view['controlVersion']}
        return self.act('release-pin', lambda: o.automatic_control.action(body, 'mac'))

    # -- readings for the invariants ------------------------------------------------------

    def raw(self):
        try:
            return json.loads(self.darkbloom.state_path.read_text())
        except (OSError, ValueError):
            return {}

    def holding(self):
        """The manager is on and a model it can hold is warm, trusted and counting."""
        import manager

        o = self.app
        raw = self.raw()
        now = self.clock.now
        with o.lock:
            on = manager.active(o.state)
        return bool(on and manager.loaded(raw, now)[0] and o.tracking(raw, now)['counting'])

    def serving(self):
        """Any model is warm on a trusted provider (earning), manager or not."""
        import manager

        return manager.loaded(self.raw(), self.clock.now)[0]

    def events(self, kinds=None):
        with self.history.lock:
            rows = self.history.db.execute('SELECT at, kind, model, detail FROM opt_events ORDER BY id').fetchall()
        return [tuple(r) for r in rows if kinds is None or r[1] in kinds]

    def settings(self):
        """User-chosen provider settings Bloomkeeper must never change on its own."""
        plist = self.darkbloom.plist()
        args = plist['ProgramArguments'][3:]
        flags = []
        i = 0
        while i < len(args):
            if args[i] == '--model':
                i += 2
                continue
            flags.append(args[i])
            i += 1
        config = {}
        for path in CONFIG_PATHS:
            full = self.home / path
            if full.exists():
                table = parse_toml(full.read_text())
                config[path] = {
                    s: {k: v for k, v in (t.items() if isinstance(t, dict) else ()) if k != 'enabled_models'}
                    for s, t in table.items() if isinstance(t, dict)
                }
        return {'flags': flags, 'env': plist.get('EnvironmentVariables') or {}, 'config': config}


def user_mac(
    models=(GEMMA,),
    endpoint=True,
    memory_gb=48,
    chip='Apple M5 Pro',
    caps=('apple_m5', 'mlx_nax'),
    downloaded=None,
    plist_models=None,
    auto=False,
    enabled=True,
    config=CANONICAL,
    explicit_config=False,
    idle=None,
    preload=None,
    reserve=None,
    env=None,
    endpoint_args=('--local-endpoint', '--port', '8000', '--bind', '127.0.0.1'),
    extra_args=(),
    initial='running',
    version='0.9.10',
    **fake,
):
    """A Mac as a field user has it, before Bloomkeeper opens: Darkbloom set up with `models`
    (provider.toml enabled_models and the launch agent's --model list), running for 5 min.
    `initial`: 'running', 'stopped' (`darkbloom stop` ran) or 'drained' (a start's drain
    finished but nothing restarted it: running, serving nothing, launch agent disabled)."""
    if downloaded is None:
        downloaded = (GEMMA, QWEN, GPT) + ((QWEN38,) if 'apple_m5' in caps else ())
        downloaded += tuple(m for m in models if m not in downloaded)
    mac = Mac(memory_gb=memory_gb, chip=chip, runtime_caps=list(caps), downloaded=downloaded, version=version, **fake)
    mac.start()
    db = mac.darkbloom
    table = {'config_version': 3, 'backend': {}}
    if enabled:
        table['backend']['enabled_models'] = list(models)
    if idle is not None:
        table['backend']['idle_timeout_mins'] = idle
    if preload:
        table['backend']['preload_models'] = list(preload)
    if reserve is not None:
        table['provider'] = {'memory_reserve_gb': reserve}
    path = mac.home / config
    db.write_config(table, path)
    args = ['--coordinator-url', COORDINATOR]
    if explicit_config:
        args += ['--config', str(path)]
    if not auto:
        for m in plist_models or models:
            args += ['--model', m]
    if endpoint:
        args += list(endpoint_args)
    args += list(extra_args)
    db.write_plist(args, env)
    db.spawn()
    mac.sleep(300)
    if initial == 'stopped':
        db.run([str(db.bin), 'stop'])
        mac.sleep(60)
    elif initial == 'drained':
        # `darkbloom start` killed after its drain: running, drained, recovery fenced off
        db.proc.lifecycle = {'outcome': 'drained', 'remaining': 0}
        db.proc.warm = {}
        db.disabled = True
        mac.sleep(30)
    return mac
