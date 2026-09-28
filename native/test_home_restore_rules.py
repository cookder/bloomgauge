"""Home choice on multi-day evidence, and the stop, restart and restore rules (audit B11-B12)."""

import plistlib
import unittest
from unittest.mock import Mock, patch

import manager
import stall_recovery as sr
import test_manager as tm
import test_provider_control as pc
import test_stall_recovery as stall
from demand_targets import GEMMA
from optimizer import launch_options
from prewarm import WarmupError

NOW = tm.NOW
QWEN = 'qwen3.5-35b-a3b'


def day(rate, hours, end):
    return tm.minutes(rate, hours, end)


class HomeChoiceTests(unittest.TestCase):
    def home(self, earned, saved=None, current='x'):
        m = {'home': saved} if saved else {}
        return manager.choose_home(m, earned, lambda: [], current, set(earned) | {current}, NOW, 0)

    def gemma(self, days=10, end=NOW):
        # ~$0.105 per ready hour, 12 ready hours a day, the usual day-to-day spread
        return [
            x
            for i in range(days)
            for x in day((0.09, 0.12, 0.10, 0.11)[i % 4], 12, end - i * 86400)
        ]

    def test_a_two_day_regime_never_becomes_home(self):
        """Sep 9-10: qwen3.5-35b earned $0.30/h and $0.12/h on two days, then ~$0.007/h."""
        earned = {
            GEMMA: {'minutes': self.gemma()},
            QWEN: {
                'minutes': day(0.302, 10.5, NOW - 5 * 86400)
                + day(0.119, 8.6, NOW - 4 * 86400)
                + day(0.0068, 1.2, NOW - 86400)
            },
        }
        rates = manager.home_rates(earned, NOW)
        self.assertEqual(rates[QWEN]['days'], 3)
        self.assertGreater(rates[QWEN]['usdPerHour'], rates[GEMMA]['usdPerHour'])
        self.assertLess(rates[QWEN]['low'], 0)  # three unlike days: no evidence it pays more
        self.assertEqual(self.home(earned)['model'], GEMMA)
        saved = {'model': GEMMA, 'source': 'history', 'at': NOW - 3 * 86400}
        self.assertEqual(self.home(earned, saved)['model'], GEMMA)
        # Two regime days alone are not enough days to count at all.
        earned[QWEN]['minutes'] = earned[QWEN]['minutes'][:-72]
        self.assertIsNone(manager.home_rates(earned, NOW)[QWEN]['low'])
        self.assertEqual(self.home(earned, saved)['model'], GEMMA)

    def test_old_evidence_is_kept_for_thirty_days_so_a_wrong_home_is_left(self):
        # Gemma served 16-25 days ago; the saved home has paid little since.
        earned = {
            GEMMA: {'minutes': self.gemma(10, NOW - 16 * 86400)},
            'x': {'minutes': [m for i in range(4) for m in day(0.03, 12, NOW - i * 86400)]},
        }
        saved = {'model': 'x', 'source': 'history', 'at': NOW - 2 * 86400}
        home = self.home(earned, saved)
        self.assertEqual((home['model'], home['source']), (GEMMA, 'history'))
        self.assertEqual(home['at'], NOW)
        # Past 30 days the old days no longer count.
        earned[GEMMA] = {'minutes': self.gemma(3, NOW - 31 * 86400)}
        self.assertEqual(self.home(earned, saved)['model'], 'x')

    def test_thin_evidence_keeps_the_current_model(self):
        earned = {'q': {'minutes': day(0.2, 10, NOW) + day(0.2, 10, NOW - 86400)}}
        self.assertEqual(
            self.home(earned, current='q'), {'model': 'q', 'source': 'current', 'at': NOW}
        )

    def test_short_days_and_excursion_minutes_do_not_count(self):
        minutes = [m for i in range(3) for m in day(0.1, 2, NOW - i * 86400)]
        minutes += day(0.9, 0.5, NOW - 3 * 86400)  # half an hour: not a day of evidence
        rates = manager.home_rates({'b': {'minutes': minutes}}, NOW)['b']
        self.assertEqual((rates['days'], round(rates['usdPerHour'], 3)), (3, 0.1))
        # The first day falls inside an excursion to b and is dropped.
        excluded = manager.home_rates(
            {'b': {'minutes': minutes}}, NOW, [['b', NOW - 3 * 3600, NOW]]
        )
        self.assertEqual(excluded['b']['days'], 2)
        self.assertIsNone(excluded['b']['low'])

    def test_lower_bound_uses_student_t(self):
        minutes = [m for i, r in enumerate((0.1, 0.2, 0.3)) for m in day(r, 2, NOW - i * 86400)]
        rates = manager.home_rates({'b': {'minutes': minutes}}, NOW)['b']
        self.assertAlmostEqual(rates['low'], 0.2 - 1.886 * 0.1 / 3**0.5, places=6)

    def test_one_continuous_stint_is_not_three_days(self):
        """A 26-36 h regime or pin used to count as three rolling 24 h blocks for part of the
        day: calendar days, and one stint counts one day per full 24 h it spans."""
        for hours in (26, 28, 30, 36, 47):
            with self.subTest(hours=hours):
                stint = day(0.30, hours, NOW - 2 * 86400 + 3600 * (hours - 26))
                for k in range(0, 24 * 12, 6):  # every 30 minutes over a day
                    now = NOW + k * 300
                    q = manager.home_rates({QWEN: {'minutes': stint}}, now)[QWEN]
                    self.assertIsNone(q['low'])
                    earned = {GEMMA: {'minutes': self.gemma(end=now)}, QWEN: {'minutes': stint}}
                    self.assertEqual(
                        manager.choose_home({}, earned, lambda: [], GEMMA, {GEMMA, QWEN}, now)['model'],
                        GEMMA,
                    )
        self.assertEqual(manager.stint_days([NOW + i * 60 for i in range(72 * 60)]), 3)
        # Three separate stints on three days are three days.
        three = [m for i in range(3) for m in day(0.30, 8, NOW - i * 86400)]
        rates = manager.home_rates({QWEN: {'minutes': three}}, NOW)[QWEN]
        self.assertEqual(rates['days'], 3)
        self.assertAlmostEqual(rates['low'], 0.30)

    def test_a_saved_home_with_thin_evidence_is_not_protected(self):
        # One $0.20 hour used to hold off a better challenger (the saved home needed one day).
        earned = {GEMMA: {'minutes': self.gemma()}, 'b': {'minutes': day(0.20, 1, NOW - 3600)}}
        for age in (3600, 2 * 86400):  # within the day-long hold and after it
            saved = {'model': 'b', 'source': 'history', 'at': NOW - age}
            home = self.home(earned, saved)
            self.assertEqual((home['model'], home['at']), (GEMMA, NOW))
        # With the minimum days behind it the saved home keeps its day-long hold.
        earned['b'] = {'minutes': [m for i in range(3) for m in day(0.20, 2, NOW - i * 86400)]}
        self.assertEqual(self.home(earned, {'model': 'b', 'source': 'history', 'at': NOW - 3600})['model'], 'b')

    def test_a_saved_home_no_longer_allowed_is_dropped(self):
        earned = {GEMMA: {'minutes': self.gemma()}, 'b': {'minutes': self.gemma()[: 12 * 60 * 4]}}
        saved = {'model': GEMMA, 'source': 'history', 'at': NOW - 3600}  # within its hold
        self.assertEqual(self.home(earned, saved)['model'], GEMMA)
        # Deselected or unavailable: the best allowed model, even during the hold...
        home = manager.choose_home({'home': saved}, earned, lambda: [], 'b', {'b'}, NOW)
        self.assertEqual((home['model'], home['source']), ('b', 'history'))
        # ...else the current model, not the saved one.
        home = manager.choose_home({'home': saved}, {}, lambda: [], 'c', {'c'}, NOW)
        self.assertEqual((home['model'], home['source']), ('c', 'current'))


class RestoreRuleTests(tm.Harness):
    def failed_round(self):
        """A cold home: a failed load at 600 s, then a failed restart at 780 s -> back-off."""
        self.o.perform_prewarm = Mock(side_effect=WarmupError('no', code='readiness-timeout'))
        self.o.verify_started = Mock(return_value=False)
        self.dark()
        self.ticks(0, 780)
        self.assertEqual((self.restores(), self.loads()), (['a'], ['a']))
        return self.o.state['manager']['watchdog']

    def test_back_off_is_capped_at_two_hours(self):
        wd = manager.back_off({'backoff': 2 * 3600}, NOW, {'hardware': {'memoryAvailableGB': 20}})
        self.assertEqual((wd['nextAt'] - NOW, wd['backoff']), (7200, 7200))
        self.assertEqual((wd['since'], wd['freeGB'], wd['attempts']), (NOW, 20, 0))
        wd = manager.back_off({}, NOW, {})
        self.assertEqual((wd['nextAt'] - NOW, wd['backoff'], wd['freeGB']), (1800, 3600, None))
        wd = manager.back_off({'backoff': 8 * 3600}, NOW, {})  # saved by the old 8 h cap
        self.assertEqual(wd['nextAt'] - NOW, 7200)

    def test_back_off_waits_without_a_state_change(self):
        wd = self.failed_round()
        self.assertEqual(wd['nextAt'], self.now + 780 + 1800)
        self.ticks(840, 2520)
        self.assertEqual(len(self.restores() + self.loads()), 2)
        self.assertIn('tries again at', self.o.detail)
        self.at(2580)
        self.assertEqual(len(self.restores() + self.loads()), 3)

    def test_a_new_provider_session_ends_the_back_off_early(self):
        wd = self.failed_round()
        next_at = wd['nextAt']
        self.dark(session=900)  # Darkbloom restarted after the back-off began
        self.ticks(840, 1320)
        self.assertEqual(len(self.restores() + self.loads()), 2)  # its own window first
        self.at(1380)
        self.assertEqual(len(self.restores() + self.loads()), 3)
        # That early retry failed: the back-off keeps its time and allows no second one.
        wd = self.o.state['manager']['watchdog']
        self.assertEqual((wd['nextAt'], wd['early']), (next_at, True))

    def test_only_one_early_retry_per_back_off_step(self):
        """Darkbloom relaunching a crashing provider (a new session every 15 min) must not cut
        every back-off short: at most one early retry per step (was 24 restores in 3 h)."""
        self.o.perform_prewarm = Mock(side_effect=WarmupError('no', code='readiness-timeout'))
        self.o.verify_started = Mock(return_value=False)
        self.dark()
        times = []
        for offset in range(0, 3 * 3600, 60):
            before = len(self.restores() + self.loads())
            if offset and offset % 900 == 0:
                self.o.raw['started_at'] = self.now + offset - 5
                self.o.identity_session = (self.o.raw['started_at'], self.o.raw['pid'])
            self.at(offset)
            if len(self.restores() + self.loads()) > before:
                times.append(offset)
        # Two per step (30 min, 1 h, 2 h) plus one early retry in each step.
        self.assertLessEqual(len(times), 9)
        wd = self.o.state['manager']['watchdog']
        self.assertEqual(wd['backoff'], manager.WATCHDOG_BACKOFF_MAX)

    def test_freed_memory_ends_the_back_off_early(self):
        self.failed_round()
        self.assertEqual(self.o.state['manager']['watchdog']['freeGB'], 24)
        self.o.live['hardware']['memoryAvailableGB'] = 29  # less than model a's 10 GB
        self.ticks(840, 1380)
        self.assertEqual(len(self.restores() + self.loads()), 2)
        self.o.live['hardware']['memoryAvailableGB'] = 34  # room for the whole model
        self.at(1440)
        self.assertEqual(len(self.restores() + self.loads()), 3)

    def test_recovery_that_fails_twice_hands_over_and_is_retried_later(self):
        self.o.state['manager']['recovery'] = {
            'failedTarget': 'b',
            'previous': 'a',
            'at': self.now - 600,
            'attempts': manager.RESTORE_ATTEMPTS,
        }
        self.dark(-900)
        self.at(0)
        m = self.o.state['manager']
        self.assertEqual(self.o.state['mode'], 'demand')
        self.assertNotIn('recovery', m)
        self.assertEqual(m['watchdog']['nextAt'], self.now + 1800)
        notice = self.events('manager-notice')[-1]
        self.assertIn('did not work twice. Automatic control stays on.', notice)
        self.assertEqual(self.events('paused'), [])
        self.ticks(60, 1740)
        self.assertEqual(self.restores() + self.loads(), [])
        self.at(1800)
        self.assertEqual(self.loads(), ['a'])  # the watchdog restores the cold home

    def switched(self, model, loads):
        """Successful switches to `model` that loaded in `loads` seconds after the command."""
        with self.o.lock:
            for i, seconds in enumerate(loads):
                self.o.manager.load_timed(model, seconds, self.now - 3600 - len(loads) + i)

    def test_failed_target_gets_its_own_p90_load_time(self):
        self.switched('b', [100] * 8 + [400, 420])
        self.o.state['manager']['recovery'] = {
            'failedTarget': 'b',
            'previous': 'a',
            'at': self.now,
            'attempts': 0,
        }
        self.dark()
        self.at(360)
        self.assertEqual(self.loads(), [])
        self.assertIn('Checking until', self.o.detail)
        self.at(420)
        self.assertEqual(self.loads(), ['a'])

    def test_grace_needs_ten_loads_and_never_drops_below_three_minutes(self):
        self.switched('b', [100] * 7 + [400, 420])
        self.switched('c', [60] * 12)
        self.assertEqual(self.o.manager.grace('b'), 180)  # nine loads: the floor
        self.assertEqual(self.o.manager.grace('c'), 180)
        self.switched('d', [100] * 8 + [400, 420])
        self.assertEqual(self.o.manager.grace('d'), 400)
        self.assertEqual(self.o.manager.grace(None), 180)

    def test_watchdog_retry_waits_for_the_failed_models_load_time(self):
        self.switched('a', [100] * 8 + [400, 420])
        self.o.perform_prewarm = Mock(side_effect=WarmupError('no', code='readiness-timeout'))
        self.dark()
        self.ticks(0, 600)
        self.assertEqual(self.loads(), ['a'])
        self.ticks(660, 960)
        self.assertEqual(self.restores(), [])
        self.at(1020)
        self.assertEqual(self.restores(), ['a'])

    def test_repeat_failures_of_an_automatic_target_are_blocked_longer(self):
        m = self.o.state['manager']
        for hours in (24, 48, 96, 96):
            self.o.manager.hold_target(m, 'b', self.now)
            self.assertEqual(m['blocked']['b'] - self.now, hours * 3600)
        self.o.manager.hold_target(m, 'a', self.now)  # the home: a retry hold, never a block
        self.assertNotIn('a', m['blocked'])
        # b loads and serves: its next failure starts at 24 h again.
        self.o.manager.settle(self.now, m, self.o.raw, 'b')
        self.assertNotIn('blockCounts', self.o.state['manager'])
        self.o.manager.hold_target(self.o.state['manager'], 'b', self.now)
        self.assertEqual(self.o.state['manager']['blocked']['b'] - self.now, 86400)

    def test_unreadable_launch_settings_keep_the_watchdog_running(self):
        self.o.read_options = Mock(side_effect=ValueError('unsupported'))
        self.dark()
        self.ticks(0, 540)
        self.assertIn('a has not been ready since', self.o.detail)
        self.assertIn('Bloomkeeper restores a', self.o.detail)
        self.at(600)
        self.assertEqual(self.restores() + self.loads(), [])  # nothing runs without settings
        self.assertIn('could not be read; nothing was sent', self.o.detail)
        self.assertEqual(self.o.state['mode'], 'demand')
        self.o.read_options = Mock(return_value=('a', ['--local-endpoint', '--port', '8000'], {}))
        self.at(660)
        self.assertEqual(self.loads(), ['a'])


class ProviderCommandTests(unittest.TestCase):
    setUp = pc.ProviderTests.setUp
    tearDown = pc.ProviderTests.tearDown
    write_plist = pc.ProviderTests.write_plist
    run_cli = pc.ProviderTests.run_cli
    payload = pc.ProviderTests.payload
    dispatch = pc.ProviderTests.dispatch
    provider_calls = pc.ProviderTests.provider_calls

    def stop_timeout(self, version):
        self.raw['version'] = version
        self.o.state['mode'] = 'demand'
        self.assertEqual(self.dispatch(self.payload('provider-stop'))['status'], 'completed')
        call = next(c for c in self.o.runner.call_args_list if c.args[0][1] == 'stop')
        return call.kwargs['timeout']

    def test_stop_waits_out_the_drain_on_099_and_later(self):
        self.assertEqual(self.stop_timeout('0.9.10'), 660)
        self.assertEqual(self.o.state['mode'], 'observe')  # Stop still pauses

    def test_stop_keeps_sixty_seconds_before_099(self):
        self.assertEqual(self.stop_timeout('0.9.8'), 60)

    def stopped(self):
        self.process.return_value = False
        self.o.service_disabled.return_value = True
        self.o.live['provider']['online'] = False

    def test_start_leaves_the_manager_on(self):
        self.o.state['mode'] = 'demand'
        self.stopped()
        self.assertIn('automatic control stays on', self.p.snapshot()['detail'])
        result = self.dispatch(self.payload('provider-start'))
        self.assertEqual(result['status'], 'completed')
        self.assertIn('automatic control continues', result['detail'])
        self.assertEqual(self.o.state['mode'], 'demand')
        self.assertEqual(self.o.status, 'optimizing')

    def test_endpoint_setup_leaves_the_manager_on(self):
        self.o.state['mode'] = 'demand'
        self.args.remove('--local-endpoint')
        self.write_plist()
        with patch(
            'provider_control.local_request',
            return_value=('http://127.0.0.1:8000/v1/chat/completions', 'private'),
        ):
            result = self.dispatch(self.payload('provider-endpoint'), 'mac')
        self.assertEqual(result['status'], 'completed')
        self.assertIn('--local-endpoint', self.provider_calls()[0])
        self.assertEqual(self.o.state['mode'], 'demand')

    def test_legacy_strategy_start_still_pauses(self):
        legacy = {**self.o.state['demandPolicy'], 'managerStrategy': 0}
        self.o.state.update(mode='demand', demandPolicy=legacy)
        self.stopped()
        result = self.dispatch(self.payload('provider-start'))
        self.assertEqual(result['status'], 'completed')
        self.assertIn('remains paused', result['detail'])
        self.assertEqual(self.o.state['mode'], 'observe')

    def test_unknown_launch_flags_survive_a_restart_unchanged(self):
        self.args = [
            '--model', 'a', '--future-knob', 'fast', '--local-endpoint', '--beta', '--limit=3',
        ]  # fmt: skip
        self.write_plist()
        self.stopped()
        self.assertEqual(self.dispatch(self.payload('provider-start'))['status'], 'completed')
        command = self.provider_calls()[0]
        self.assertEqual(
            command[2:],
            ['--future-knob', 'fast', '--local-endpoint', '--beta', '--limit=3', '--model', 'a'],
        )
        saved = plistlib.loads(self.o.plist_path.read_bytes())['ProgramArguments']
        self.assertEqual(launch_options({'ProgramArguments': saved})[1], command[2:-2])


class LaunchFlagTests(unittest.TestCase):
    def parse(self, *args):
        return launch_options({'ProgramArguments': ['darkbloom', 'start', *args]}, allow_auto=True)

    def test_unknown_flags_are_kept_verbatim_with_their_values(self):
        model, options = self.parse(
            '--model', 'a', '--x', '5', '--y', '-2', '--local-endpoint', '--z', '--port', '8000',
            '--w=1', '-q', '--mode', 'auto',
        )  # fmt: skip
        self.assertEqual(model, 'a')
        self.assertEqual(
            options,
            [
                '--x', '5', '--y', '-2', '--local-endpoint', '--z', '--port', '8000', '--w=1',
                '-q', '--mode', 'auto',
            ],
        )  # fmt: skip

    def test_a_flag_after_an_unknown_switch_is_still_read(self):
        self.assertEqual(self.parse('--z', '--model', 'a'), ('a', ['--z']))
        parsed = self.parse('--z', '-c', '/tmp/p.toml', '--model', 'a')
        self.assertEqual(parsed, ('a', ['--z', '-c', '/tmp/p.toml']))
        self.assertEqual(self.parse('--model', 'a', '--z'), ('a', ['--z']))

    def test_known_flags_in_another_form_and_stray_words_are_refused(self):
        for arg in ('--model=a', '--bind=0.0.0.0', '--local-endpoint=1', '-', '--', 'word'):
            with self.subTest(arg=arg), self.assertRaises(ValueError):
                self.parse('--model', 'a', arg)


class StallRestartGateTests(unittest.TestCase):
    probe = [{'at': stall.T0 + 300, 'step': 'probe'}]

    def test_restart_needs_absolute_pressure(self):
        low = stall.network(active=20, warm=100)  # pressure 0.2, held relative to the baseline
        self.assertEqual(stall.check(stall.T0 + 300, net=low)['step'], 'probe')
        # The gate skips the restart; the ladder goes on to the escape.
        r = stall.check(stall.T0 + 480, self.probe, net=low)
        self.assertEqual(r['step'], 'escape')
        self.assertTrue(r['demandHeld'])
        self.assertIn('demand for this model is low (0.20', r['reason'])
        self.assertEqual(
            stall.check(stall.T0 + 480, self.probe, net=stall.network(active=33, warm=100))['step'],
            'restart',
        )
        # Without a stored API key the ladder starts at the restart: the same gate applies.
        self.assertEqual(stall.check(stall.T0 + 480, net=low, probe=False)['step'], 'escape')

    def test_restart_without_fresh_demand_readings_escapes(self):
        stale = stall.network(end=stall.T0 + 300)
        r = stall.check(stall.T0 + 480, self.probe, net=stale)
        self.assertEqual(r['step'], 'escape')
        self.assertIn('no fresh demand readings', r['reason'])
        self.assertEqual(sr.RESTART_MIN_PRESSURE, 0.33)


if __name__ == '__main__':
    unittest.main()
