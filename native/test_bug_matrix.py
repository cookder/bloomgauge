"""Bug matrix (Sep 28 2026): the manager against a fake Darkbloom 0.9.10 on field setups.

Every provider command, file and wait is simulated by fake_darkbloom (a temporary HOME, a
virtual clock); Bloomkeeper's Optimizer, manager, model controls and On/Off run unchanged.
Setups come from the field reports (1, 2 or 3+ enabled models, endpoint off or on, an
auto-select launch, an older config path, idle policies, a stopped or drained provider,
launch-agent variables, a runtime-gated model, 48/128/192 GB). Each runs the journey a user
takes: open Bloomkeeper -> set up pre-warming -> Manager on -> pin -> release the pin ->
Off -> Manager on again, with a Darkbloom restart and an update. The invariants:

- no failure loop: the same refusal or failure again after the action it asked for;
- the manager holds a working model within HOLD_SECONDS wherever the setup allows it;
- every blocked state shows a specific, actionable message (never a generic retry);
- no restart storm (at most STARTS_PER_HOUR `darkbloom start`s an hour on its own);
- a setting the user chose (launch-agent flags and variables, provider.toml values other
  than enabled_models) is never changed by Bloomkeeper's commands.

Set BLOOM_MATRIX=full for every setup combination (slower); the default runs the
representative subset below. Regression tests for the bugs found follow the matrix.
"""

import itertools
import os
import unittest

import manager
from fake_darkbloom import BIG, GEMMA, GPT, QWEN, QWEN38, parse_toml, user_mac

HOLD_SECONDS = 20 * 60
STARTS_PER_HOUR = 4
FALSE_FAILURE_SECONDS = 120
GENERIC = (
    'Set up Darkbloom on this Mac, then refresh its status.',
    'Waiting for a fresh provider status. Refresh before turning On.',
    'Provider status could not be verified. Refresh.',
    'Refresh the provider state before selecting a model.',
    'Waiting for fresh readings for the current provider session.',
    'On timed out while waiting for verified readiness. The saved plan is kept; review the status and try again.',
    'The request could not be verified. Automatic switching stays Manual; review the status and try again.',
    'Could not evaluate the optimizer. Keeping the current model and retrying.',
    'The provider stopped or changed before the restore.',
)
DEFAULTS = {'--port': '8000', '--bind': '127.0.0.1'}
JOURNEY_KEYS = ('pin', 'setup_model', 'verify', 'plan')
DAY = 86400


def saved_plan(mac, kind):
    """Bloomkeeper's saved optimizer settings from an earlier version, before it opens."""
    from history import History
    from optimizer_store import device_id

    now = mac.clock.now
    device = device_id({'attestation_public_key': mac.darkbloom.key})
    plans = {
        # The seven-day test (1.36.4x) finished: the Optimizer went back to Manual.
        'week-finished': dict(mode='observe', startedAt=now - 10 * DAY, endsAt=now - 3 * DAY),
        # The user chose Manual halfway through the seven-day test.
        'week-paused': dict(mode='observe', startedAt=now - 3 * DAY, endsAt=now + 4 * DAY),
    }
    history = History(mac.home / 'history.sqlite3')
    history.cache(
        'optimizer-settings',
        {
            'models': [GEMMA, QWEN],
            'blockHours': 2,
            'lastSwitchAt': now - 3 * DAY,
            'originalModel': GEMMA,
            'expectedModel': GEMMA,
            'account': 'acct',
            'device': device,
            'demandPolicyRevision': 2,
            'demandPolicy': {'minRunMinutes': 30},
            **plans[kind],
        },
    )
    history.close()


def launch_flags(settings):
    """Launch-agent flags as the user set them, with Darkbloom's defaults filled in."""
    flags, i = {}, 0
    args = settings['flags']
    while i < len(args):
        flag = args[i]
        if i + 1 < len(args) and not args[i + 1].startswith('--'):
            flags[flag] = args[i + 1]
            i += 2
        else:
            flags[flag] = True
            i += 1
    if '--local-endpoint' in flags:
        for key, value in DEFAULTS.items():
            flags.setdefault(key, value)
    return flags


class Journey:
    """One setup through the user's journey, with what the invariants need."""

    def __init__(self, test, spec):
        self.test = test
        self.spec = spec
        self.mac = user_mac(**{k: v for k, v in spec.items() if k not in JOURNEY_KEYS})
        test.addCleanup(self.mac.stop)
        self.notes = []  # (step, refusal or failure text)
        self.steps = []
        self.user_starts = set()

    def step(self, name, result=None):
        self.steps.append((name, self.mac.clock.now, result))
        if result:
            self.notes.append((name, result))

    def user(self, name, action):
        before = len(self.mac.darkbloom.starts)
        result = action()
        self.user_starts.update(range(before, len(self.mac.darkbloom.starts)))
        self.step(name, result)
        return result

    def settle(self, seconds, hold=False):
        mac = self.mac
        count = len(mac.darkbloom.starts)
        mac.run(seconds, until=mac.holding if hold else None)
        # A start that the user's action queued (the switch runs on a later tick) is theirs.
        if self.steps and self.steps[-1][0] in ('setup', 'pin', 'on', 'on-again', 'start'):
            self.user_starts.update(range(count, len(mac.darkbloom.starts)))
        return mac.holding()

    def run(self):
        mac = self.mac
        spec = self.spec
        self.before = mac.settings()
        if spec.get('plan'):
            saved_plan(mac, spec['plan'])
        mac.launch()
        self.settle(120)
        current = mac.raw().get('advertised_models') or []
        if spec.get('plan'):
            # The On card asks to review the plan's settings: save them as the UI does.
            self.user('on', mac.ui_on)
            self.user('review-plan', mac.ui_save_plan)
            self.settle(60)
        if spec.get('endpoint') is False or len(current) > 2 or spec.get('initial') == 'drained':
            # Set up pre-warming (or pick one model) in the model controls.
            target = spec.get('setup_model') or (current[0] if len(current) == 1 else GEMMA)
            self.user('setup', lambda: mac.ui_select(target))
            self.settle(900)
        self.user('on', mac.ui_on)
        self.held_at = mac.clock.now if self.settle(HOLD_SECONDS, hold=True) else None
        self.settle(900)
        self.holding_before_pin = mac.holding()
        serving = mac.raw().get('advertised_models') or []
        pin = spec.get('pin') or (QWEN if serving != [QWEN] else GEMMA)
        self.user('pin', lambda: mac.ui_select(pin, verify=spec.get('verify', False)))
        self.settle(1200)
        self.pinned = (mac.raw().get('advertised_models') == [pin], mac.holding())
        self.user('release', mac.ui_release_pin)
        self.settle(1200)
        self.user('off', mac.ui_off)
        self.settle(300)
        self.user('on-again', mac.ui_on)
        self.again = self.settle(HOLD_SECONDS, hold=True)
        mac.darkbloom.restart()
        self.step('darkbloom-restart')
        self.after_restart = self.settle(1200) or self.settle(600, hold=True)
        mac.darkbloom.restart(version='0.9.11')
        self.step('darkbloom-update')
        self.after_update = self.settle(1200) or self.settle(600, hold=True)
        self.after = mac.settings()
        return self

    # ---- invariants ------------------------------------------------------------------

    def check(self, expect_hold=True):
        t = self.test
        mac = self.mac
        t.assertEqual(mac.errors, [], 'the app loops raised')
        if expect_hold:
            t.assertIsNotNone(self.held_at, 'Manager on never held a working model')
            t.assertTrue(self.holding_before_pin, 'the manager stopped holding a working model')
            t.assertEqual(self.pinned, (True, True), 'the pin did not become the held model')
            t.assertTrue(self.again, 'Manager on again never held a working model')
            t.assertTrue(self.after_restart, 'no working model after a Darkbloom restart')
            t.assertTrue(self.after_update, 'no working model after a Darkbloom update')
        self.check_messages()
        self.check_false_failures()
        self.check_loops()
        self.check_storms()
        self.check_settings()

    def check_messages(self):
        mac = self.mac
        for _, kind, (mode, _, detail) in [e for e in mac.log if e[1] == 'status']:
            if mode == 'demand':
                self.test.assertNotIn(
                    'Automatic switching is paused', detail or '', 'says paused while on'
                )
        shown = [text for _, text in self.notes] + [
            e[3] for e in mac.events(('failed', 'manager-notice'))
        ]
        shown += [detail for _, kind, detail in mac.log if kind == 'status' for detail in [detail[2]]]
        for text in shown:
            for generic in GENERIC:
                self.test.assertNotIn(generic, text or '', 'a generic message: %r' % text)
            self.test.assertNotIn('@combo:', text or '', 'a raw selection key: %r' % text)

    def check_false_failures(self):
        """A switch or restore reported failed while its model was loaded and trusted within
        FALSE_FAILURE_SECONDS: the user is told something broke that didn't."""
        for at, _, model, detail in self.mac.events(('failed',)):
            served = [
                t for t, models in self.mac.trace
                if model in models and at - 5 <= t <= at + FALSE_FAILURE_SECONDS
            ]
            self.test.assertEqual(served, [], 'a false failure: %r' % detail)

    def check_loops(self):
        """The same refusal for the same kind of action twice means the action it asked for
        could not help; the same failure event twice without a success in between is a loop."""
        seen = {}
        for step, text in self.notes:
            kind = 'on' if step.startswith('on') else step
            self.test.assertNotEqual(seen.get(kind), text, 'repeated refusal: %r' % text)
            seen[kind] = text
        last = None
        for _, kind, _, detail in self.mac.events(('failed', 'switched', 'recovered')):
            if kind == 'failed':
                self.test.assertNotEqual(last, detail, 'repeated failure: %r' % detail)
                last = detail
            else:
                last = None

    def check_storms(self):
        starts = [at for i, (at, _) in enumerate(self.mac.darkbloom.starts) if i not in self.user_starts]
        for at in starts:
            window = [x for x in starts if at <= x < at + 3600]
            self.test.assertLessEqual(len(window), STARTS_PER_HOUR, 'restart storm')

    def check_settings(self):
        before, after = self.before, self.after
        flags = launch_flags(before)
        if any(step == 'setup' and not result for step, _, result in self.steps):
            flags.setdefault('--local-endpoint', True)
            for key, value in DEFAULTS.items():
                flags.setdefault(key, value)
        flags.pop('--idle-timeout', None)  # Darkbloom moves it into provider.toml
        self.test.assertEqual(launch_flags(after), flags, 'launch-agent flags changed')
        self.test.assertEqual(after['env'], before['env'], 'launch-agent variables changed')
        for path, table in before['config'].items():
            self.test.assertEqual(after['config'].get(path), table, 'provider.toml changed')


class QuietJourney(Journey):
    """Manager on, then no network work for three hours: Darkbloom unloads idle models."""

    def run(self):
        mac = self.mac
        self.before = mac.settings()
        mac.launch()
        self.settle(120)
        if self.spec.get('endpoint') is False:
            self.user('setup', lambda: mac.ui_select(self.spec['models'][0]))
            self.settle(900)
        self.user('on', mac.ui_on)
        self.held_at = mac.clock.now if self.settle(HOLD_SECONDS, hold=True) else None
        mac.darkbloom.traffic_seconds = None
        self.step('quiet')
        self.empty = 0
        for _ in range(36):
            self.settle(300)
            if not mac.holding():
                self.empty += 300
        self.after = mac.settings()
        return self

    def check(self, expect_hold=True):
        t = self.test
        t.assertEqual(self.mac.errors, [], 'the app loops raised')
        t.assertIsNotNone(self.held_at, 'Manager on never held a working model')
        if expect_hold:
            # One 5-min reading may fall on a reload; the Mac must not sit empty.
            t.assertLessEqual(self.empty, 600, 'the Mac sat empty after an idle unload')
        self.check_messages()
        self.check_false_failures()
        self.check_loops()
        self.check_storms()
        self.check_settings()


# ---- the matrix ------------------------------------------------------------------------

MODELS = {'one': [GEMMA], 'pair': [GPT, QWEN], 'three': [GEMMA, QWEN, GPT]}
HARDWARE = {
    48: dict(memory_gb=48, chip='Apple M5 Pro', caps=('apple_m5', 'mlx_nax')),
    128: dict(memory_gb=128, chip='Apple M1 Ultra', caps=()),
    192: dict(memory_gb=192, chip='Apple M3 Ultra', caps=()),
}
SETUPS = {
    'andrew': dict(models=[GEMMA], preload=[GEMMA]),
    'endpoint-off': dict(models=[GEMMA], endpoint=False),
    'pair': dict(models=[GPT, QWEN], pin=GEMMA, **HARDWARE[128]),
    'three-models': dict(models=[GEMMA, QWEN, GPT]),
    'three-enabled-one-launch': dict(models=[GEMMA, QWEN, GPT], plist_models=[GEMMA], endpoint=False),
    'jason': dict(
        models=[GEMMA, QWEN38], plist_models=[GEMMA], endpoint=False, **HARDWARE[128],
        downloaded=(GEMMA, QWEN, GPT, QWEN38),
    ),
    'auto-select': dict(models=[GEMMA], auto=True),
    'app-support-config': dict(models=[GEMMA], config='Library/Application Support/darkbloom/provider.toml'),
    'eigeninference-config': dict(models=[GEMMA], config='.config/eigeninference/provider.toml'),
    'explicit-config': dict(models=[GEMMA], explicit_config=True),
    'idle-30': dict(models=[GEMMA], idle=30),
    'idle-0': dict(models=[GEMMA], idle=0),
    'legacy-idle-flag': dict(models=[GEMMA], idle=0, extra_args=['--idle-timeout', '30']),
    'stopped': dict(models=[GEMMA], initial='stopped'),
    'drained': dict(models=[GEMMA], initial='drained'),
    'mlx-cache-variable': dict(models=[GEMMA], env={'DARKBLOOM_MLX_CACHE_LIMIT_GB': '12'}),
    'memory-reserve': dict(models=[GEMMA], reserve=8),
    'runtime-gated-home': dict(models=[QWEN38], pin=GEMMA),
    'm1-ultra-128': dict(models=[GEMMA], **HARDWARE[128]),
    'm3-ultra-192': dict(models=[GEMMA], **HARDWARE[192]),
    'endpoint-without-port': dict(models=[GEMMA], endpoint_args=['--local-endpoint']),
    'rotating-device-key': dict(models=[GEMMA], rotating_key=True),
    'app-attest-slow': dict(models=[GEMMA], trust_delay=420),
    'm3-pro-36': dict(models=[GEMMA], memory_gb=36, chip='Apple M3 Pro', caps=(), pin=GPT),
    'm3-ultra-192-big-pin': dict(
        models=[GEMMA], pin=BIG, downloaded=(GEMMA, QWEN, GPT, BIG), **HARDWARE[192]
    ),
    'week-test-finished': dict(models=[GEMMA], plan='week-finished'),
    'week-test-paused': dict(models=[GEMMA], plan='week-paused'),
}
QUIET = {
    'quiet-default-idle': dict(models=[GEMMA]),
    'quiet-idle-0': dict(models=[GEMMA], idle=0),
    'quiet-idle-30': dict(models=[GEMMA], idle=30),
    'quiet-endpoint-off': dict(models=[GEMMA], endpoint=False),
    'quiet-pair': dict(models=[GPT, QWEN], **HARDWARE[128]),
}
# Setups where the manager can't hold a model on its own (and why the journey still runs).
NO_HOLD = {'quiet-idle-30': 'the user chose to unload after 30 idle minutes'}
# Setups that still break an invariant: the bug, until its fix lands (expected failures).
KNOWN = {
    # Bloomkeeper's pair budget (weights + reserve + activation + KV: 45 GB) refuses to start the
    # gpt-oss + qwen pair on 48 GB although the fake Darkbloom serves it; unverified on a Mac.
    'full-pair-ep-48-None-stopped': 'the stopped pair start is refused by the pair budget',
    'full-pair-ep-48-30-stopped': 'the stopped pair start is refused by the pair budget',
    'app-attest-slow': 'a pick fails at the 6-min deadline while App Attest is pending (false failure)',
}


def full_matrix():
    for (count, models), endpoint, memory, idle, initial in itertools.product(
        MODELS.items(), (True, False), HARDWARE, (None, 30), ('running', 'stopped', 'drained')
    ):
        name = 'full-%s-%s-%d-%s-%s' % (count, 'ep' if endpoint else 'noep', memory, idle, initial)
        yield name, dict(models=models, endpoint=endpoint, idle=idle, initial=initial, **HARDWARE[memory])


class MatrixTests(unittest.TestCase):
    maxDiff = None


def add(name, spec, journey=Journey):
    def test(self):
        journey(self, spec).run().check(expect_hold=name not in NO_HOLD)

    test.__name__ = 'test_' + name.replace('-', '_')
    test.__doc__ = 'Journey on the %s setup.' % name
    if name in KNOWN:
        test = unittest.expectedFailure(test)
    setattr(MatrixTests, test.__name__, test)


for name, spec in SETUPS.items():
    add(name, spec)
for name, spec in QUIET.items():
    add(name, spec, QuietJourney)
if os.environ.get('BLOOM_MATRIX') == 'full':
    for name, spec in full_matrix():
        add(name, spec)


# ---- regressions for the bugs the matrix found ------------------------------------------


class Regression(unittest.TestCase):
    def mac(self, **spec):
        plan = spec.pop('plan', None)
        mac = user_mac(**spec)
        self.addCleanup(mac.stop)
        if plan:
            saved_plan(mac, plan)
        mac.launch()
        mac.run(120)
        return mac

    def card(self, mac):
        return mac.control_view()['automatic']



class FinishedWeekTestTests(Regression):
    """A user who ran the 1.36.4x seven-day test (finished, or paused with Manual) could never
    turn the manager on: On asked to review the plan's settings, and saving them left the old
    plan's end date, so On asked again, forever."""

    def test_reviewing_a_finished_week_test_lets_manager_on_start_a_new_plan(self):
        mac = self.mac(plan='week-finished')
        self.assertIn('Review its settings', mac.ui_on())
        self.assertEqual(self.card(mac)['blocker'], {'code': 'completed-plan', 'action': 'configure'})
        self.assertIsNone(mac.ui_save_plan())
        self.assertTrue(mac.control_view()['firstPlan'])
        self.assertIsNone(mac.ui_on())
        self.assertTrue(mac.run(HOLD_SECONDS, until=mac.holding))
        with mac.app.lock:
            state = mac.app.state
            self.assertEqual((state['mode'], state['endsAt']), ('demand', None))
            self.assertEqual(state['models'][0], GEMMA)
            self.assertIn(QWEN, state['models'])  # the reviewed models are kept

    def test_a_paused_week_test_is_reviewed_the_same_way(self):
        mac = self.mac(plan='week-paused')
        self.assertIn('Review its settings', mac.ui_on())
        self.assertIsNone(mac.ui_save_plan())
        self.assertIsNone(mac.ui_on())
        self.assertTrue(mac.run(HOLD_SECONDS, until=mac.holding))

    def test_saving_a_paused_manager_plan_keeps_it(self):
        mac = self.mac()
        self.assertIsNone(mac.ui_on())
        self.assertTrue(mac.run(HOLD_SECONDS, until=mac.holding))
        self.assertIsNone(mac.ui_off())
        with mac.app.lock:
            started = mac.app.state['startedAt']
        self.assertIsNone(mac.ui_save_plan())
        with mac.app.lock:
            self.assertEqual(mac.app.state['startedAt'], started)
        self.assertFalse(mac.control_view()['firstPlan'])



class StoppedProviderOnTests(Regression):
    """On with Darkbloom stopped starts its saved model. 0.9.10 then preloads that model before
    it registers, writing daemon-state only every 30 s; the provider controls read a state older
    than 15 s as "unknown", so On failed with "The provider is not running after Start" while
    the model was loading."""

    def test_on_starts_a_stopped_provider_and_holds_its_model(self):
        mac = self.mac(initial='stopped')
        self.assertIn('Turning On starts its saved model', self.card(mac)['detail'])
        self.assertIsNone(mac.ui_on())
        self.assertTrue(mac.run(HOLD_SECONDS, until=mac.holding))
        self.assertEqual(len(mac.darkbloom.starts), 1)
        details = [d[2] for _, kind, d in mac.log if kind == 'status']
        self.assertFalse(any('not running after Start' in d for d in details), details)

    def test_a_preloading_provider_counts_as_running(self):
        mac = self.mac(initial='stopped', load_scale=3)  # a 105-s preload
        self.assertIsNone(mac.ui_on())
        for _ in range(300):
            raw = mac.raw()
            if raw.get('startup_preload_pending_models') and mac.clock.now - raw['written_at'] > 16:
                break
            mac.run(1, step=1)
        else:
            self.fail('no preload reading older than 16 s')
        self.assertEqual(mac.app.provider_control.inspect()['status'], 'running')
        self.assertTrue(mac.run(HOLD_SECONDS, until=mac.holding))
        self.assertNotIn('blocker', self.card(mac))



def drain_stuck(mac):
    """`darkbloom start` killed after its drain (or its drain ended with nothing restarting
    it): the provider runs, serves nothing, and its launch agent is still disabled."""
    proc = mac.darkbloom.proc
    proc.lifecycle = {'outcome': 'drained', 'remaining': 0}
    proc.warm = {}
    mac.darkbloom.disabled = True


class DrainedProviderTests(Regression):
    """A drain fences off launchd recovery (`launchctl disable`) until the start that follows
    it re-enables the service. When nothing followed, Darkbloom kept running, drained and
    serving nothing, and the manager's restore refused every minute because the launch agent
    was disabled ("The provider stopped or changed before the restore."): a dark Mac for good.
    On waited on "start Darkbloom" and timed out after 10 minutes."""

    def test_the_watchdog_restarts_a_drained_provider(self):
        mac = self.mac()
        self.assertIsNone(mac.ui_on())
        self.assertTrue(mac.run(HOLD_SECONDS, until=mac.holding))
        drain_stuck(mac)
        mac.run(120)
        self.assertFalse(mac.holding())
        self.assertIn('drained and serving nothing', mac.app.detail)
        self.assertTrue(mac.run(1800, until=mac.holding))
        self.assertEqual(len(mac.darkbloom.starts), 1)  # one restart, no local-endpoint reload
        self.assertFalse(mac.darkbloom.disabled)
        self.assertIn('Watchdog restored', mac.events(('recovered',))[-1][3])

    def test_manager_on_with_a_drained_provider_restarts_it(self):
        mac = self.mac(initial='drained')
        self.assertIsNone(mac.ui_on())
        mac.run(60)
        with mac.app.lock:
            self.assertEqual(mac.app.state['mode'], 'demand')
        self.assertTrue(mac.run(1800, until=mac.holding))
        self.assertEqual(len(mac.darkbloom.starts), 1)

    def test_model_controls_say_what_happened_and_a_pick_starts_it(self):
        mac = self.mac(initial='drained')
        view = mac.app.manual_snapshot()
        self.assertIn('finished draining', view['providerControl']['detail'])
        row = next(r for r in view['models'] if r['id'] == QWEN)
        self.assertTrue(row['canSwitch'], row['switchReason'])
        self.assertIsNone(mac.ui_select(QWEN))
        mac.run(600)
        self.assertEqual(mac.app.state['manualResult']['status'], 'completed')
        self.assertEqual(mac.raw()['advertised_models'], [QWEN])
        self.assertFalse(mac.darkbloom.disabled)

    def test_without_the_local_endpoint_a_pick_sets_it_up(self):
        # DRAINED said "turn the manager on", On said "set up pre-warming in the model
        # controls", and the model controls said DRAINED: a circle.
        mac = self.mac(initial='drained', endpoint=False)
        self.assertIsNone(mac.ui_select(GEMMA))
        mac.run(600)
        self.assertIn('--local-endpoint', mac.darkbloom.plist_args())
        self.assertIsNone(mac.ui_on())
        self.assertTrue(mac.run(HOLD_SECONDS, until=mac.holding))

    def test_three_drained_models_are_replaced_by_the_pick(self):
        mac = self.mac(initial='drained', models=[GEMMA, QWEN, GPT], **HARDWARE[128])
        self.assertIsNone(mac.ui_select(GEMMA))
        mac.run(600)
        self.assertEqual(mac.raw()['advertised_models'], [GEMMA])
        self.assertIsNone(mac.ui_on())
        self.assertTrue(mac.run(HOLD_SECONDS, until=mac.holding))

    def test_a_user_stop_is_still_respected(self):
        mac = self.mac()
        self.assertIsNone(mac.ui_on())
        self.assertTrue(mac.run(HOLD_SECONDS, until=mac.holding))
        mac.darkbloom.run([str(mac.darkbloom.bin), 'stop'])
        mac.run(3600)
        self.assertEqual(mac.darkbloom.starts, [])
        self.assertIsNone(mac.darkbloom.proc)



class LegacyIdleFlagTests(Regression):
    """Launch agents written before Darkbloom 0.8.14 carry `--idle-timeout N`. Darkbloom's
    launchd child ignores it once provider.toml sets idle_timeout_mins (`darkbloom idle`), but
    `darkbloom start --idle-timeout N` writes N into provider.toml. Bloomkeeper passed the
    launch agent's flags to every start, so a switch replaced the user's idle choice (here
    "always ready", 0) with the stale 30: the model then unloaded after 30 idle minutes and the
    manager, reading 30 as the user's choice, left the Mac empty."""

    def toml(self, mac):
        return parse_toml(mac.darkbloom.config_path().read_text())['backend']

    def test_a_switch_keeps_the_idle_setting_in_provider_toml(self):
        mac = self.mac(idle=0, extra_args=['--idle-timeout', '30'])
        self.assertIsNone(mac.ui_on())
        self.assertTrue(mac.run(HOLD_SECONDS, until=mac.holding))
        self.assertIsNone(mac.ui_select(QWEN))
        self.assertTrue(mac.run(HOLD_SECONDS, until=mac.holding))
        self.assertEqual(mac.raw()['advertised_models'], [QWEN])
        self.assertEqual(self.toml(mac)['idle_timeout_mins'], 0)
        argv = mac.darkbloom.starts[-1][1]
        self.assertNotIn('--idle-timeout', argv)
        # Still always ready: two quiet hours later the model is loaded.
        mac.darkbloom.traffic_seconds = None
        mac.run(7200)
        self.assertEqual(mac.raw()['warm_models'], [QWEN])

    def test_without_a_provider_toml_value_the_flag_is_kept(self):
        mac = self.mac(extra_args=['--idle-timeout', '30'])
        self.assertNotIn('idle_timeout_mins', self.toml(mac))
        self.assertIsNone(mac.ui_on())
        self.assertTrue(mac.run(HOLD_SECONDS, until=mac.holding))
        self.assertIsNone(mac.ui_select(QWEN))
        self.assertTrue(mac.run(HOLD_SECONDS, until=mac.holding))
        # Darkbloom moved the launch agent's value into provider.toml: still 30 minutes.
        self.assertIn('--idle-timeout', mac.darkbloom.starts[-1][1])
        self.assertEqual(self.toml(mac)['idle_timeout_mins'], 30)



class ThreeModelTests(Regression):
    """Darkbloom set to serve three or more models (the start picker saves every pick; the
    Darkbloom app and `darkbloom switch` save provider.toml's list). The manager holds one
    model or a pair, and its On card says "Darkbloom is set to serve N models. Run `darkbloom
    start` in Terminal and pick one model, or use Bloomkeeper's model controls." The model
    controls refused every pick: with three --model flags they could not read the launch
    agent ("Provider status could not be verified. Refresh."), and with Darkbloom serving the
    list they showed that same notice. Picking one model in them now replaces the list."""

    def pick_one_then_manager_on(self, mac):
        card = self.card(mac)
        self.assertEqual(card['blocker'], {'code': 'multi-model', 'action': 'controller'})
        self.assertTrue(card['detail'].startswith('Darkbloom is set to serve 3 models.'))
        view = mac.app.manual_snapshot()
        row = next(r for r in view['models'] if r['id'] == GEMMA)
        self.assertTrue(row['canSwitch'], row['switchReason'])
        self.assertIsNone(mac.ui_select(GEMMA))
        mac.run(600)
        self.assertEqual(mac.raw()['advertised_models'], [GEMMA])
        backend = parse_toml(mac.darkbloom.config_path().read_text())['backend']
        self.assertEqual(backend['enabled_models'], [GEMMA])
        self.assertEqual(mac.app.state['manualResult']['status'], 'completed')
        self.assertNotIn('blocker', self.card(mac))
        self.assertIsNone(mac.ui_on())
        self.assertTrue(mac.run(HOLD_SECONDS, until=mac.holding))

    def test_three_launch_agent_models_are_replaced_by_the_pick(self):
        mac = self.mac(models=[GEMMA, QWEN, GPT])
        self.assertEqual(len(mac.raw()['advertised_models']), 3)
        self.pick_one_then_manager_on(mac)

    def test_the_list_darkbloom_serves_is_replaced_by_the_pick(self):
        mac = self.mac(models=[GEMMA, QWEN, GPT], plist_models=[GEMMA], endpoint=False)
        self.assertEqual(len(mac.raw()['advertised_models']), 3)
        self.pick_one_then_manager_on(mac)
        self.assertIn('--local-endpoint', mac.darkbloom.plist_args())

    def test_a_busy_list_is_drained_not_interrupted(self):
        mac = self.mac(models=[GEMMA, QWEN, GPT], traffic_seconds=5)
        self.assertIsNone(mac.ui_select(GEMMA))
        mac.run(600)
        self.assertEqual(mac.raw()['advertised_models'], [GEMMA])
        self.assertNotIn('--force', mac.darkbloom.starts[-1][1])



class RotatingDeviceKeyTests(Regression):
    """When Darkbloom can't use its keychain key (a locked keychain) it makes a new temporary
    attestation key on every start, and the device id is that key's hash. Every start
    Bloomkeeper sent then failed its verification with "Provider identity changed during the
    switch", although the model came up: picks read as failed, restores counted as failed
    (holding home back for an hour) and automatic moves blocked their target for a day. The
    session Bloomkeeper's own command started may now carry a new key, once, after the provider
    roster matches it; the manager then adopts it (device_changed)."""

    def failures(self, mac):
        return [e[3] for e in mac.events(('failed',))]

    def test_a_pick_verifies_on_the_new_key(self):
        mac = self.mac(rotating_key=True)
        self.assertIsNone(mac.ui_on())
        self.assertTrue(mac.run(HOLD_SECONDS, until=mac.holding))
        old = mac.app.state['device']
        self.assertIsNone(mac.ui_select(QWEN))
        self.assertTrue(mac.run(HOLD_SECONDS, until=mac.holding))
        result = mac.app.state['manualResult']
        self.assertEqual(result['status'], 'completed', result['detail'])
        self.assertEqual(self.failures(mac), [])
        mac.run(120)
        self.assertNotEqual(mac.app.state['device'], old)  # adopted
        self.assertEqual(mac.app.state['mode'], 'demand')

    def test_a_watchdog_restore_verifies_on_the_new_key(self):
        mac = self.mac(rotating_key=True)
        self.assertIsNone(mac.ui_on())
        self.assertTrue(mac.run(HOLD_SECONDS, until=mac.holding))
        drain_stuck(mac)
        mac.run(120)
        self.assertTrue(mac.run(1800, until=mac.holding))
        self.assertEqual(self.failures(mac), [])
        self.assertIn('Watchdog restored', mac.events(('recovered',))[-1][3])
        self.assertNotIn('homeRetry', mac.app.state['manager'])

    def test_a_pick_in_manual_pauses_nothing(self):
        mac = self.mac(rotating_key=True)
        old = mac.app.state.get('device')
        self.assertIsNone(mac.ui_select(QWEN))
        mac.run(900)
        self.assertEqual(mac.app.state['manualResult']['status'], 'completed')
        self.assertEqual(mac.events(('paused',)), [])
        self.assertNotIn('paused', mac.app.detail)
        self.assertNotEqual(mac.app.state['device'], old)
        self.assertIsNone(mac.ui_on())
        self.assertTrue(mac.run(HOLD_SECONDS, until=mac.holding))

    def test_a_new_key_under_another_account_still_fails(self):
        mac = self.mac(rotating_key=True)
        self.assertIsNone(mac.ui_on())
        self.assertTrue(mac.run(HOLD_SECONDS, until=mac.holding))
        real = mac.darkbloom.spawn

        def spawn():  # the Mac was signed in to another account meanwhile
            real()
            mac.account = 'someone-else'

        mac.darkbloom.spawn = spawn
        self.assertIsNone(mac.ui_select(QWEN))
        mac.run(900)
        result = mac.app.state['manualResult']
        self.assertEqual(result['status'], 'failed')
        self.assertIn('Provider identity changed during the switch', result['detail'])



class FailedPickMessageTests(Regression):
    """A pick under the manager that didn't verify (here App Attest kept the coordinator from
    trusting the Mac past the 6-minute deadline) said "Automatic switching is paused. Review
    Help & feedback on the Mac." while the manager had resumed with the pick and went on to
    hold it."""

    def pick_while_attest_pends(self, mac):
        mac.darkbloom.trust_delay = 420  # from the next start on
        self.assertIsNone(mac.ui_select(QWEN))
        mac.run(900, until=lambda: mac.app.state['manualResult']['status'] == 'failed')
        return mac.app.state['manualResult']

    def test_a_failed_pick_under_the_manager_says_automatic_control_continues(self):
        mac = self.mac()
        self.assertIsNone(mac.ui_on())
        self.assertTrue(mac.run(HOLD_SECONDS, until=mac.holding))
        result = self.pick_while_attest_pends(mac)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(mac.app.state['mode'], 'demand')
        self.assertNotIn('Automatic switching is paused', result['detail'])
        self.assertIn('The manager checks whether it becomes ready', result['detail'])
        self.assertTrue(mac.run(HOLD_SECONDS, until=mac.holding))
        self.assertEqual(mac.raw()['advertised_models'], [QWEN])

    def test_a_failed_pick_in_manual_still_says_paused(self):
        mac = self.mac()
        result = self.pick_while_attest_pends(mac)
        self.assertEqual((result['status'], mac.app.state['mode']), ('failed', 'observe'))
        self.assertIn('Automatic switching is paused', result['detail'])


class PairTextTests(unittest.TestCase):
    """Watchdog, reload and restore texts named a pair by its internal key: "Loaded
    @combo:["gpt-oss-20b","qwen3.5-35b-a3b"] again after Darkbloom unloaded it while idle"."""

    def test_pair_keys_read_as_their_models(self):
        from model_combinations import selection_key

        pair = selection_key([GPT, QWEN])
        text = manager.readable('Loaded %s again; restoring %s.' % (pair, GEMMA))
        self.assertEqual(text, 'Loaded %s + %s again; restoring %s.' % (GPT, QWEN, GEMMA))
        self.assertIsNone(manager.readable(None))



class ChosenIdleUnloadTests(Regression):
    """With the user's own idle setting (here 30 minutes) the manager lets Darkbloom unload an
    idle model, as Andrew decided. The status then read "Waiting for verified warm readiness
    before following demand." for hours: nothing said the Mac earns no base reward, why, or
    what setting changes it."""

    def test_the_status_says_why_nothing_is_loaded_and_how_to_change_it(self):
        mac = self.mac(idle=30)
        self.assertIsNone(mac.ui_on())
        self.assertTrue(mac.run(HOLD_SECONDS, until=mac.holding))
        mac.darkbloom.traffic_seconds = None
        mac.run(2700)
        self.assertEqual(mac.raw()['warm_models'], [])
        detail = mac.app.detail
        self.assertIn('unloaded %s after it sat idle' % GEMMA, detail)
        self.assertIn('darkbloom idle keep-loaded', detail)
        self.assertEqual(mac.darkbloom.starts, [])  # the choice is respected



class StoppedPairOnTests(Regression):
    """On with Darkbloom stopped on a pair (the manager holds a pair without gemma) looked the
    pair's selection key up among single models and refused: "The saved model is no longer
    available for automatic selection." """

    def test_on_starts_a_stopped_pair_and_holds_it(self):
        mac = self.mac(models=[GPT, QWEN], initial='stopped', **HARDWARE[128])
        self.assertIn('Turning On starts its saved model', self.card(mac)['detail'])
        self.assertIsNone(mac.ui_on())
        self.assertTrue(mac.run(HOLD_SECONDS, until=mac.holding))
        self.assertEqual(mac.raw()['advertised_models'], [GPT, QWEN])
        self.assertEqual(len(mac.darkbloom.starts), 1)

    def test_a_pair_with_an_unavailable_model_names_it(self):
        mac = self.mac(models=[GPT, QWEN], initial='stopped', **HARDWARE[128])
        mac.network.catalog = [m for m in mac.network.catalog if m['id'] != QWEN]
        mac.app.next_discovery = 0
        mac.run(30)
        self.assertIsNone(mac.ui_on())
        mac.run(120)
        card = self.card(mac)
        self.assertEqual(card['phase'], 'blocked')
        self.assertIn(QWEN + ' is not available for automatic selection', card['detail'])
        self.assertEqual(mac.darkbloom.starts, [])


if __name__ == '__main__':
    unittest.main()
