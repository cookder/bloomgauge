"""Review of the network features (net-all, Sep 28): each finding's reproduction as a test.

1-3, 5  manager: the network home during an excursion, with no serving model (and the
        watchdog's restore target), the pick's $/request error, and the notice's flapping.
4, 6, 7 network_health: silence counted from an outage's end (plus per-install jitter), a model
        incident whose model left the list, and a model outage on another offered model.
8, 9    model_catalog_watch: the "pays well" push bar, and 'left' only with a fresh catalog and
        no Darkbloom-wide outage.
10      the activity log's "is over" line only on a real recovery (the banner copy per scope is
        tested in lib/network-health.test.mjs).
"""

import copy
import math
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import manager
import model_catalog_watch as mcw
import network_health as nh
import optimizer
import stall_recovery as sr
import test_manager as tm
import test_manager_network_home as tnh
import test_model_catalog_watch as tcw
import test_network_health as th
from demand_optimizer import policy
from demand_targets import GEMMA
from history import History
from network_evidence import uncalibrated_error
from network_health import NetworkHealth, stall_wait
from test_demand_optimizer import NOW, decision as legacy_upgrade

GPT, QWEN, CELL = tnh.GPT, tnh.QWEN, tnh.CELL
est, net, own = tnh.est, tnh.net, tnh.own
ALLOWED = {GEMMA, GPT, QWEN}


def home(m, current, network, earned=None, now=NOW):
    return manager.choose_home(m, earned or {}, lambda: [], current, set(ALLOWED), now, 0, network)


def remember(m, got, now):
    """ManagerControl.remember's notice bookkeeping on a plain dict; returns what it announced."""
    notice, old, last = got.get('notice'), m.get('homeNotice') or {}, manager.ended_notice(m, now)
    if manager.notice_timing(notice) == manager.notice_timing(old):
        return None
    if notice:
        m['homeNotice'] = {k: notice.get(k) for k in manager.NOTICE_KEYS}
        m.pop('homeNoticeLast', None)
        told = old or last
        key = ('model', 'from', 'until')
        return notice if tuple(told.get(k) for k in key) != tuple(notice[k] for k in key) else None
    m.pop('homeNotice')
    m['homeNoticeLast'] = {**{k: old.get(k) for k in manager.NOTICE_KEYS[:4]}, 'endedAt': now}
    return None


class ExcursionHomeTests(unittest.TestCase):
    """1. An excursion onto the network pick must not make it home (review-net/excursion_home.py,
    home_paths.py case 1)."""

    SAVED = {'model': GEMMA, 'source': 'current', 'at': NOW - 86400}

    def test_an_excursion_target_that_is_the_network_pick_does_not_become_home(self):
        network = net({GEMMA: est(0.048, 0.010), QWEN: est(0.12, 0.02, hours=10)})
        m = {'home': dict(self.SAVED), 'excursion': {'target': QWEN, 'from': GEMMA}}
        got = home(m, QWEN, network)
        self.assertEqual(got, self.SAVED)  # before: qwen as a network home, no notice
        self.assertEqual(manager.network_home(m, network, QWEN, ALLOWED, {}, NOW), (None, None))
        # With 20+ hours of evidence the same: no network pick is judged during an excursion.
        network = net({GEMMA: est(0.048, 0.010), QWEN: est(0.12, 0.05)})
        self.assertEqual(home(m, QWEN, network, own(QWEN, 0.01)), self.SAVED)

    def test_the_excursion_ends_by_returning_to_the_saved_home(self):
        network = net({GEMMA: est(0.048, 0.010), QWEN: est(0.12, 0.02, hours=10)})
        excursion = {'target': QWEN, 'from': GEMMA, 'startedAt': NOW - 5 * 3600, 'maxMinutes': 240}
        state = {
            'mode': 'demand',
            'demandPolicy': policy(),
            'manager': {'home': dict(self.SAVED), 'excursion': excursion},
        }
        context = {
            'current': QWEN,
            'home': home(state['manager'], QWEN, network),
            'rows': {m: tm.row(m) for m in ALLOWED},
            'rules': policy(),
            'blocked': {},
        }
        legacy = {**legacy_upgrade(), 'currentModel': QWEN}
        d = manager.decide(legacy, state, context, NOW)
        self.assertEqual((d['target'], d['manager']['action']), (GEMMA, 'end-excursion'))
        self.assertTrue(d['reason'].endswith('Returning to home model %s.' % GEMMA), d['reason'])


class ExcursionFlow(tnh.Flow):
    """1, end to end: the saved home survives a decision (and remember) during the excursion."""

    def test_remember_keeps_the_saved_home_during_an_excursion_to_the_pick(self):
        saved = {'model': 'a', 'source': 'current', 'at': self.now - 86400}
        self.o.state['manager'] = {
            'home': dict(saved),
            'excursion': {'target': 'b', 'from': 'a', 'startedAt': self.now - 1800},
        }
        legacy = {**legacy_upgrade(), 'currentModel': 'b'}
        self.o.demand_auto.evaluate.return_value = legacy
        # 'b' is the network pick here (tnh.Flow): before the fix it became a network home.
        self.assertEqual(self.o.manager.network_evidence({'a', 'b'}, self.now)['cell'], CELL)
        d = self.decide()
        self.assertEqual(d['manager']['home']['model'], 'a')
        self.assertEqual(self.o.state['manager']['home'], saved)
        self.assertIsNone(d['manager']['homeNotice'])
        self.assertEqual(self.events('manager'), [])


class NoServingModelTests(unittest.TestCase):
    """2. No serving model reported (drained, dark): the saved home, never an unannounced pick
    (home_paths.py case 2)."""

    def test_the_saved_home_stands_while_nothing_serving_is_reported(self):
        network = net({GEMMA: est(0.048, 0.010), GPT: est(0.03, 0.004)})
        saved = {'model': GPT, 'source': 'current', 'at': NOW - 86400}
        self.assertEqual(home({'home': saved}, None, network), saved)  # before: gemma, unannounced
        self.assertEqual(manager.network_home({}, network, None, ALLOWED, {}, NOW), (None, None))
        # A new user with nothing saved and nothing serving has no home (the watchdog falls back
        # to the last good model), rather than the pick.
        self.assertIsNone(home({}, None, network))
        # A saved network home stays what it is.
        saved = {'model': GEMMA, 'source': 'network', 'at': NOW - 600}
        self.assertEqual(home({'home': saved}, None, network), saved)


class WatchdogFlow(tnh.Flow):
    """2. The watchdog restores the saved home, not the network pick, when no model is reported."""

    def test_watchdog_restore_target_is_the_saved_home(self):
        saved = {'model': 'a', 'source': 'current', 'at': self.now - 86400}
        self.o.state['manager'] = {'home': dict(saved)}
        settings, live, raw = copy.deepcopy(self.o.state), self.o.live, self.o.raw
        control = self.o.manager
        self.assertEqual(control.home(settings, live, None, self.now), saved)
        control.dark_since = self.now
        with patch('manager.time.time', return_value=self.now):
            self.assertTrue(
                control.watch(self.now, settings, live, raw, None, settings['manager'])
            )
        self.assertIn('BloomGauge restores a.', self.o.detail)
        # While 'a' serves, 'b' is announced first as before.
        self.assertEqual(control.home(settings, live, 'a', self.now)['notice']['model'], 'b')


class PriceErrorTests(unittest.TestCase):
    """3. The pick must beat the serving model at the low end of its $/request error, not only
    at 2x on means (review-net/niche.py)."""

    def test_a_niche_pick_at_2x_on_means_is_not_announced(self):
        g, q = 0.048, 0.11  # qwen3.6 at 2.3x gemma; its list x mix is off by up to 4x
        low = q * math.exp(-uncalibrated_error(QWEN))
        network = net({GEMMA: est(g, g * 0.5), QWEN: est(q, low)})
        got = home({'home': {'model': GEMMA, 'source': 'current', 'at': NOW - 86400}}, GEMMA,
                   network, own(GEMMA, g, 20))  # fmt: skip
        self.assertEqual((got['model'], got.get('notice')), (GEMMA, None))
        # At 4.5x (qwen $0.216/h at its low end: $0.054 > $0.048) it is.
        network['models'][QWEN] = est(0.216, 0.05)
        got = home({}, GEMMA, network, own(GEMMA, g, 20))
        self.assertEqual(got['notice']['model'], QWEN)

    def test_gemma_over_gpt_oss_still_changes_and_a_calibrated_error_is_used(self):
        # Sep 27-28, M5 Pro 48: gemma $0.048/h (ln 2: $0.024 at the low end) vs gpt-oss $0.011.
        got = home({}, GPT, net(tnh.MODELS), own(GPT, 0.011))
        self.assertEqual(got['notice']['model'], GEMMA)
        # This Mac's own jobs on the pick narrow its error: then a 2.3x niche pick qualifies.
        network = net({GEMMA: est(0.048, 0.02), QWEN: {**est(0.11, 0.05), 'usd_error': 0.3}})
        got = home({}, GEMMA, network, own(GEMMA, 0.048))
        self.assertEqual(got['notice']['model'], QWEN)
        # ... and a wide calibrated error blocks even a gemma pick.
        network = net({GEMMA: {**est(0.048, 0.02), 'usd_error': 1.6}, GPT: est(0.011, 0.001)})
        self.assertNotIn('notice', home({}, GPT, network, own(GPT, 0.011)))


class NoticeFlapTests(unittest.TestCase):
    """5. Own pay hovering at half the pick: one announcement, one deadline (home_paths.py
    case 3: three announcements in 70 minutes and a deadline that kept sliding)."""

    def network(self, now):
        return net({GEMMA: est(0.048, 0.03), GPT: est(0.011, 0.001)}, updated=now - 120)

    def simulate(self, rates, gap=None):
        m = {'home': {'model': GPT, 'source': 'current', 'at': NOW - 86400}}
        told, until = [], []
        for step, rate in enumerate(rates):
            now = NOW + step * 600
            network = None if step == gap else self.network(now)
            got = home(m, GPT, network, own(GPT, rate, 20, now), now)
            said = remember(m, got, now)
            if said:
                told.append(said['until'])
            until.append(got['notice']['until'] if 'notice' in got else None)
        return told, until

    def test_hysteresis_keeps_the_notice_and_its_deadline(self):
        told, until = self.simulate([0.0235, 0.0235, 0.0241, 0.0235, 0.0235, 0.0241, 0.0235])
        self.assertEqual(told, [NOW + 7200])
        self.assertEqual(until, [NOW + 7200] * 7)
        # Only below 1.6x (0.048 / 0.031) is it cancelled.
        told, until = self.simulate([0.0235, 0.029, 0.031])
        self.assertEqual(until, [NOW + 7200, NOW + 7200, None])
        # A first announcement still needs 2x.
        self.assertEqual(self.simulate([0.0241, 0.029]), ([], [None, None]))

    def test_a_notice_that_drops_out_comes_back_with_its_deadline_unannounced(self):
        told, until = self.simulate([0.0235] * 5, gap=2)  # network evidence missing for a tick
        self.assertEqual(told, [NOW + 7200])
        self.assertEqual(until, [NOW + 7200, NOW + 7200, None, NOW + 7200, NOW + 7200])
        # Back after its deadline passed: at least 15 more minutes, announced again.
        m = {'home': {'model': GPT, 'source': 'current', 'at': NOW - 86400}}
        m['homeNoticeLast'] = {'model': GEMMA, 'from': GPT, 'at': NOW - 7000, 'until': NOW + 200,
                               'endedAt': NOW - 3600}  # fmt: skip
        got = home(m, GPT, self.network(NOW), own(GPT, 0.0235, 20))
        self.assertEqual((got['notice']['at'], got['notice']['until']), (NOW - 7000, NOW + 900))
        self.assertIsNotNone(remember(m, got, NOW))
        # Over two hours after it dropped out: a new notice.
        m['homeNoticeLast'] = {**m.pop('homeNotice'), 'endedAt': NOW - 7201}
        got = home(m, GPT, self.network(NOW), own(GPT, 0.0235, 20))
        self.assertEqual(got['notice']['until'], NOW + 7200)


class NoticeFlow(tnh.Flow):
    """5, end to end through remember(): the event log gets one announcement."""

    def test_a_dropped_tick_neither_moves_the_deadline_nor_announces_again(self):
        self.decide()
        notice = self.o.state['manager']['homeNotice']
        self.ne.last_window = {'at': self.now + 600 - 3700}  # evidence stale for a tick
        self.decide(600)
        self.assertNotIn('homeNotice', self.o.state['manager'])
        self.assertEqual(self.o.state['manager']['homeNoticeLast']['until'], notice['until'])
        self.ne.last_window = {'at': self.now + 1200 - 60}
        self.decide(1200)
        self.assertEqual(self.o.state['manager']['homeNotice']['until'], notice['until'])
        self.assertNotIn('homeNoticeLast', self.o.state['manager'])
        said = [e for e in self.events('manager') if 'BloomGauge will switch to b' in e]
        self.assertEqual(len(said), 1)
        # Keep current and unpinning forget a dropped notice too.
        self.o.state['manager']['homeNoticeLast'] = {'model': 'b', 'from': 'a', 'endedAt': self.now}
        manager.keep_current(self.o.state, 'a', self.now + 1200)
        self.assertNotIn('homeNoticeLast', self.o.state['manager'])
        self.o.state['manager']['homeNoticeLast'] = {'model': 'b', 'from': 'a', 'endedAt': self.now}
        manager.unpin(self.o.state, self.now + 1300)
        self.assertNotIn('homeNoticeLast', self.o.state['manager'])


M = 'gemma-4-26b-qat-4bit'
T = 1_800_000_000


class AfterOutageTests(unittest.TestCase):
    """4. Silence during an outage no longer counts once it clears (review-net/outage_end.py:
    probe at 5 min, then a restart the minute it cleared, on every Mac at once)."""

    def health(self, jitter):
        h = History(':memory:')
        self.addCleanup(h.close)
        context = lambda: ('M5 Pro|48', [M])  # noqa: E731
        health = NetworkHealth(h, context=context, clock=lambda: T, jitter=jitter)
        self.t = T - 7200
        self.feed(health, T + 60, lambda t: th.noise(t, 500))
        return health

    def feed(self, health, end, warm):
        while self.t < end:
            level = warm(self.t)
            payload = th.capacity(0, models={M: (level, 40), 'gpt': (level, 40)})
            health.on_network('capacity', payload, self.t)
            self.t += 30

    def ladder(self, health, until_minute):
        minutes = [{'at': T - 1200 + i * 60, 'model': M, 'seconds': 60, 'jobs': 8}
                   for i in range(20)]  # fmt: skip
        minutes += [{'at': T + i * 60, 'model': M, 'seconds': 60, 'jobs': 0} for i in range(90)]
        network = {M: [{'at': T - 1500 + i * 30, 'active': 30, 'queued': 5, 'warm': 60}
                       for i in range(400)]}  # fmt: skip
        attempts, results = [], []
        for minute in range(1, until_minute):
            now = T + minute * 60 + 5
            self.feed(health, now + 1, lambda t: 100 if t < T + 24 * 60 else 500)
            seen = [x for x in minutes if x['at'] + 60 <= now]
            r = stall_wait(sr.assess(seen, network, attempts, [], T - 7200, now), health, now)
            results.append((minute, r))
            if r['step']:
                attempts.append({'at': now, 'step': r['step'], 'model': M})
                if r['step'] == 'restart':
                    break
        return attempts, results

    def test_restart_waits_for_silence_counted_from_the_end_plus_jitter(self):
        for jitter, restart in ((0, 32), (240, 36)):
            with self.subTest(jitter=jitter):
                health = self.health(jitter)
                attempts, results = self.ladder(health, 60)
                ended = health.ended_outage(T + 3600)
                self.assertEqual((ended['scope'], ended['how']), ('network', 'recovered'))
                self.assertEqual(ended['end'], T + 23.5 * 60)
                steps = [(int((a['at'] - T) // 60), a['step']) for a in attempts]
                self.assertEqual(steps, [(5, 'probe'), (restart, 'restart')])  # before: 25
                waiting = [r for minute, r in results if 25 <= minute < restart]
                self.assertTrue(all(r['step'] is None for r in waiting))
                after = [r for r in waiting if 'afterOutage' in r]
                self.assertEqual(len(after), restart - 26)  # back for 2 minutes at 26 min
                for r in after:
                    self.assertIn('ended at', r['reason'])
                    self.assertIn('Not restarting yet', r['reason'])
                    self.assertEqual(r['afterOutage']['end'], ended['end'])

    def test_each_install_draws_its_own_jitter_and_silence_after_the_end_is_ordinary(self):
        h = History(':memory:')
        self.addCleanup(h.close)
        draws = {NetworkHealth(h).jitter for _ in range(20)}
        self.assertGreater(len(draws), 1)
        self.assertTrue(all(0 <= j <= nh.AFTER_JITTER_SECONDS for j in draws))
        ended = {'scope': 'network', 'key': None, 'since': T - 3000, 'end': T - 1200,
                 'how': 'recovered'}  # fmt: skip
        health = SimpleNamespace(outage=lambda now, model=None: None, jitter=100,
                                 ended_outage=lambda now, model=None: ended)  # fmt: skip
        after = {'step': 'restart', 'model': M, 'episodeStart': T - 600, 'trigger': 'own'}
        self.assertEqual(stall_wait(after, health, T), after)  # work stopped after it ended
        during = {**after, 'episodeStart': T - 2000}
        self.assertIsNone(stall_wait(during, health, T - 1200 + 480 + 99)['step'])
        self.assertEqual(stall_wait(during, health, T - 1200 + 480 + 100), during)
        peers = {**during, 'trigger': 'peers'}
        self.assertIsNone(stall_wait(peers, health, T - 1200 + 480 + 100)['step'])
        probe = {**during, 'step': 'probe', 'requiredSeconds': 300}
        self.assertIsNone(stall_wait(probe, health, T - 1200 + 399)['step'])
        self.assertEqual(stall_wait(probe, health, T - 1200 + 400), probe)


class LeftModelTests(th.Base):
    """6. A model incident whose model leaves the capacity list (review-net/left_model.py: never
    closed, its row re-written every minute)."""

    def cap(self, t, vl):
        models = [{'id': 'gemma', 'warm_providers': 600, 'active_requests': 40}]
        if vl is not None:
            models.append({'id': 'qwen3-vl-30b', 'warm_providers': vl, 'active_requests': 5})
        self.health.on_network('capacity', {'models': models}, t)

    def test_it_closes_as_stale_and_is_written_only_on_change(self):
        self.context = ('M5 Pro|48', ['qwen3-vl-30b'])
        writes = []

        def trace(sql):
            if sql.split()[0] in ('INSERT', 'UPDATE') and 'network_incidents' in sql:
                writes.append(sql)

        self.h.db.set_trace_callback(trace)
        t = T
        for _ in range(80):
            self.cap(t, 110)
            t += 30
        for _ in range(14):
            self.cap(t, 20)
            t += 30
        self.assertEqual(self.health.outage(t)['key'], 'qwen3-vl-30b')
        before = len(writes)
        for _ in range(240):  # two hours off the list
            self.cap(t, None)
            t += 30
        self.assertEqual(self.health.incidents, {})  # before: tracked for ever
        self.assertLessEqual(len(writes) - before, 1)  # before: 120 rewrites
        [ended] = self.health.ended
        self.assertEqual((ended['scope'], ended['how']), ('model', 'stale'))
        self.assertEqual(self.health.ended_outage(ended['end'] + 60, model='qwen3-vl-30b'), ended)
        self.assertEqual(self.health.status_of('model', 'qwen3-vl-30b', ended['since']), 'stale')
        [(start, end, scope, key, *_)] = self.rows()
        self.assertEqual((scope, key, end), ('model', 'qwen3-vl-30b', ended['end']))


class OtherModelOutageTests(th.Base):
    """7. A model outage on another offered model no longer holds a restart for this stall."""

    def test_only_the_stalled_models_outage_holds_its_restart(self):
        self.context = ('M5 Pro|48', ['gemma', 'gpt'])
        models = th.ScopeTests.models(self, lambda t: th.noise(t, 200))
        self.feed(T, T + 7200, None, models=models)
        self.feed(T + 7200, T + 7800, None, models=th.ScopeTests.models(self, lambda t: 30))
        now = T + 7800
        self.assertEqual(self.health.outage(now)['key'], 'gemma')  # the banner still shows it
        restart = {'step': 'restart', 'model': 'gpt', 'reason': 'r'}
        self.assertEqual(stall_wait(restart, self.health, now), restart)  # before: held
        held = stall_wait({**restart, 'model': 'gemma'}, self.health, now)
        self.assertIsNone(held['step'])
        self.assertIn('Macs serving gemma', held['reason'])
        pair = manager.selection_key(['gemma', 'gpt'])
        self.assertIsNone(stall_wait({**restart, 'model': pair}, self.health, now)['step'])
        self.assertIsNone(self.health.outage(now, model='gpt'))


class PaysWellTests(tcw.Base):
    """8. "A new model pays well" needs 2x the reference and the low end of its $/request error
    above it (was: parity on list-price means)."""

    def watch(self, rows):
        evidence = tcw.FakeEvidence(rows, served='gpt-oss-20b')
        return self.make(evidence=evidence), evidence

    def test_parity_or_a_niche_2x_is_not_news_to_push(self):
        watch, evidence = self.watch({'new-model': (0.1, 0.05), 'gpt-oss-20b': (0.1, 0.05)})
        self.assertEqual(watch.pays_well('new-model', T), (False, None))  # before: pushed
        evidence.rows['new-model'] = (0.23, 0.1)  # 2.3x, but $0.058 at its ln 4 low end
        self.assertEqual(watch.pays_well('new-model', T), (False, None))
        evidence.rows['new-model'] = (0.45, 0.2)
        good, detail = watch.pays_well('new-model', T)
        self.assertTrue(good)
        self.assertAlmostEqual(detail['ratio'], 4.5)
        self.assertEqual(mcw.PAYS_WELL_RATIO, 2.0)

    def test_a_calibrated_error_counts(self):
        watch, evidence = self.watch({'new-model': (0.23, 0.1), 'gpt-oss-20b': (0.1, 0.05)})
        estimate = evidence.estimate
        evidence.estimate = lambda m, **k: {**estimate(m, **k), 'usd_error': 0.3}
        self.assertTrue(watch.pays_well('new-model', T)[0])


class LeftNewsTests(tcw.Base):
    """9. Missing from the capacity list confirms 'left' only with a fresh catalog, and never
    during a Darkbloom-wide outage."""

    REST = {m: v for m, v in tcw.BASE.items() if m != 'Qwen3.5-9B'}

    def test_no_left_during_a_network_outage(self):
        watch = self.seeded()
        watch.health = SimpleNamespace(outage=lambda now: {'scope': 'network'})
        t = self.run_minutes(watch, tcw.T0 + 30, 30, self.REST)
        self.assertEqual(self.events(), [])  # before: 'left' after 10 minutes
        watch.health = SimpleNamespace(outage=lambda now: {'scope': 'model', 'key': 'x'})
        self.run_minutes(watch, t, 1, self.REST)
        self.assertEqual([(k, m) for _, k, m, _ in self.events()], [('left', 'Qwen3.5-9B')])

    def test_no_left_without_a_catalog(self):
        watch = self.seeded(catalog=None)
        self.run_minutes(watch, tcw.T0 + 30, 30, self.REST)
        self.assertEqual(self.events(), [])  # before: missing alone confirmed it

    def test_the_collector_shares_its_network_health(self):
        import tempfile
        from collector import Collector

        with tempfile.TemporaryDirectory() as root:
            c = Collector(root)
            self.assertIs(c.catalog_watch.health, c.network_health)


class ActivityLogTests(unittest.TestCase):
    """10. "Is over" only when the problem really recovered."""

    OUTAGE = {'since': T, 'scope': 'model', 'key': 'm', 'dropPct': 80.0, 'detail': 'x'}

    def log(self, statuses):
        store = SimpleNamespace(events=[])
        store.event = lambda *a, **k: store.events.append(a)
        health = th.FakeHealth(self.OUTAGE)
        health.status_of = lambda scope, key, since: statuses.pop(0)
        o = SimpleNamespace(network_health=health, outage_logged={}, store=store)
        optimizer.Optimizer.log_network_outage(o, 'acct', 'mac', T)
        health.value = None
        for i in range(3):
            optimizer.Optimizer.log_network_outage(o, 'acct', 'mac', T + 60 * (i + 1))
        return [(e[3], e[5]) for e in store.events[1:]]

    def test_the_end_line_says_what_happened(self):
        [(kind, text)] = self.log(['open', 'recovered'])
        self.assertEqual(kind, 'network-recovered')
        self.assertIn('is over', text)
        [(kind, text)] = self.log(['stale'])  # its model left the list: no evidence either way
        self.assertEqual(kind, 'network-untracked')
        self.assertNotIn('is over', text)
        self.assertIn('stopped tracking', text)
        [(kind, text)] = self.log([None])
        self.assertEqual(kind, 'network-untracked')
        [(kind, text)] = self.log(['expired'])
        self.assertIn('lasted 6 hours', text)
        self.assertEqual(self.log(['open', 'open', 'open']), [])  # still going somewhere

    def test_with_the_real_health_a_stale_incident_is_not_over(self):
        h = History(':memory:')
        self.addCleanup(h.close)
        health = NetworkHealth(h, context=lambda: ('M5 Pro|48', ['vl']), clock=lambda: T)
        store = SimpleNamespace(events=[])
        store.event = lambda *a, **k: store.events.append(a)
        o = SimpleNamespace(network_health=health, outage_logged={}, store=store)

        def cap(t, vl):
            models = [{'id': 'gemma', 'warm_providers': 600, 'active_requests': 40}]
            if vl is not None:
                models.append({'id': 'vl', 'warm_providers': vl, 'active_requests': 5})
            health.on_network('capacity', {'models': models}, t)
            optimizer.Optimizer.log_network_outage(o, 'acct', 'mac', t)

        t = T
        for level, n in ((110, 80), (20, 14), (None, 60)):
            for _ in range(n):
                cap(t, level)
                t += 30
        kinds = [e[3] for e in store.events]
        self.assertEqual(kinds, ['network-outage', 'network-untracked'])


if __name__ == '__main__':
    unittest.main()
