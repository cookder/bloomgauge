"""'It's not you': Darkbloom-wide outage detection, its effects, and real incident replays."""

import json
import math
import pathlib
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from types import SimpleNamespace
from unittest.mock import Mock

import network_health as nh
import optimizer
import retention
import support_reports as reports
import test_stall_recovery as ladder
from history import History
from network import Network
from network_health import NetworkHealth, stall_wait

CENTRAL = ZoneInfo('America/Chicago')

T = 1_790_000_000
FIXTURE = pathlib.Path(__file__).with_name('network_health_fixture.json')
KEYS = {'since', 'scope', 'key', 'dropPct', 'detail'}


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f') + '123Z'


def capacity(warm, active=300.0, models=None):
    """A /v1/models/capacity payload: `warm` split over two busy models unless given."""
    models = models or {'gemma': (warm * 0.6, active * 0.6), 'gpt': (warm * 0.4, active * 0.4)}
    return {
        'models': [
            {'id': m, 'warm_providers': w, 'active_requests': a, 'queued_requests': 0}
            for m, (w, a) in models.items()
        ]
    }


def noise(t, level):
    """Deterministic +-2% wobble, like the steady warm count (0.974-1.03 of its median)."""
    return level * (1 + 0.02 * math.sin(t / 97.0))


def stats(t, cells, requests=0, status='online'):
    """A /v1/stats payload with `n` providers per (family, tier, memory) cell."""
    providers = []
    for (family, tier, memory), n in cells.items():
        providers += [
            {
                'id': 'provider-%s-%s-%d-%d' % (family, tier, memory, i),
                'status': status,
                'chip_family': family,
                'chip_tier': tier,
                'memory_gb': memory,
                'requests_served': 7,
            }
            for i in range(n)
        ]
    return {'snapshot_at': iso(t), 'providers': providers, 'total_requests': requests}


class Base(unittest.TestCase):
    def setUp(self):
        self.h = History(':memory:')
        self.context = ('M5 Pro|48', ['gemma'])
        self.health = NetworkHealth(self.h, context=lambda: self.context, clock=lambda: T)

    def tearDown(self):
        self.h.close()

    def feed(self, start, end, warm, step=30, active=300.0, models=None):
        """Capacity readings every `step` s; `warm(t)` gives the total."""
        for t in range(int(start), int(end), step):
            payload = capacity(warm(t) if warm else 0, active, models(t) if models else None)
            self.health.on_network('capacity', payload, t)

    def rows(self):
        return self.h.db.execute(
            'SELECT start,end,scope,key,before,during,detail FROM network_incidents ORDER BY start'
        ).fetchall()


class DetectionTests(Base):
    def test_too_little_data_is_none(self):
        self.feed(T, T + 600, lambda t: 1000)
        self.feed(T + 600, T + 900, lambda t: 50)
        self.assertIsNone(self.health.outage(T + 900))
        self.assertEqual(self.health.view(T + 900)['status'], 'learning')
        self.assertEqual(self.rows(), [])

    def test_a_collapse_counts_after_two_readings_and_a_blip_does_not(self):
        self.feed(T, T + 7200, lambda t: noise(t, 1000))
        self.health.on_network('capacity', capacity(60), T + 7200)  # one reading: a blip
        self.assertIsNone(self.health.outage(T + 7200))
        self.feed(T + 7230, T + 7290, lambda t: noise(t, 1000))
        self.feed(T + 7290, T + 7350, lambda t: 100)
        outage = self.health.outage(T + 7320)
        self.assertEqual(set(outage), KEYS)
        self.assertEqual(
            (outage['scope'], outage['key'], outage['since']), ('network', None, T + 7290)
        )
        self.assertAlmostEqual(outage['dropPct'], 90, delta=2)
        self.assertIn('Macs ready to serve', outage['detail'])
        view = self.health.view(T + 7320)
        self.assertEqual((view['status'], view['outage']['signal']), ('outage', 'warm'))
        start, end, scope, key, before, during, _ = self.rows()[0]
        self.assertEqual((start, scope, key, during), (T + 7290, 'network', None, 100))
        self.assertAlmostEqual(before, 1000, delta=25)

    def test_a_sustained_partial_drop_counts_after_five_minutes_then_recovers(self):
        self.feed(T, T + 7200, lambda t: noise(t, 1000))
        self.feed(T + 7200, T + 7470, lambda t: 720)
        self.assertIsNone(self.health.outage(T + 7470))  # 4.5 minutes: not yet
        self.feed(T + 7470, T + 9000, lambda t: 720)
        outage = self.health.outage(T + 8990)
        self.assertEqual(outage['since'], T + 7200)
        self.assertAlmostEqual(outage['dropPct'], 28, delta=2)
        # Back to normal: over after two minutes above 0.85 of the level before.
        self.feed(T + 9000, T + 9090, lambda t: 1000)
        self.assertIsNotNone(self.health.outage(T + 9060))
        self.feed(T + 9090, T + 9300, lambda t: 1000)
        self.assertIsNone(self.health.outage(T + 9290))
        (start, end, scope, _, _, during, detail), = self.rows()
        self.assertEqual((start, end, during), (T + 7200, T + 8970, 720))
        # A later drop is a new incident measured against the pre-incident level.
        self.feed(T + 9300, T + 9400, lambda t: 1000)
        self.feed(T + 9400, T + 9460, lambda t: 300)
        self.assertEqual(self.health.outage(T + 9450)['since'], T + 9400)
        self.assertEqual(len(self.rows()), 2)

    def test_normal_nightly_dip_is_not_an_outage(self):
        # Warm Macs drift down a third over 8 hours and requests fall 70% at night:
        # neither is sharp against the last two hours.
        night = 8 * 3600
        self.feed(T, T + night, lambda t: noise(t, 1300 - 430 * (t - T) / night))
        cells = {('M5', 'Pro', 48): 60, ('M4', 'Max', 128): 300}
        for i, t in enumerate(range(T, T + night, 60)):
            share = 1 - 0.3 * (t - T) / night
            now = {c: int(n * share) for c, n in cells.items()}
            self.health.on_network('stats', stats(t, now, requests=int(i * 3000 * share)), t)
            self.assertIsNone(self.health.outage(t))
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.health.view(T + night)['status'], 'ok')

    def test_api_down_needs_darkbloom_replies_on_two_endpoints(self):
        self.feed(T, T + 7200, lambda t: noise(t, 1000))
        # This Mac offline: dropped connections say nothing about Darkbloom.
        for t in range(T + 7200, T + 7400, 30):
            self.health.on_error('capacity', 'Connection unavailable', t)
            self.health.on_error('stats', 'Connection unavailable', t)
        self.assertIsNone(self.health.outage(T + 7400))
        # One endpoint failing alone is not enough either.
        for t in range(T + 7400, T + 7500, 30):
            self.health.on_error('capacity', 'HTTP 503', t)
        self.assertIsNone(self.health.outage(T + 7500))
        self.health.on_error('stats', 'HTTP 503', T + 7500)
        outage = self.health.outage(T + 7500)
        self.assertEqual((outage['since'], outage['dropPct']), (T + 7400, 100.0))
        self.assertIn('HTTP 503', outage['detail'])
        self.assertEqual(self.health.signal(T + 7500), 'api')
        self.assertIn('servers stopped answering', nh.describe(outage, 'api'))
        # Answers again with a normal count: over.
        self.feed(T + 7530, T + 7560, lambda t: 1000)
        self.assertIsNone(self.health.outage(T + 7560))
        (start, end, *_), = self.rows()
        self.assertEqual((start, end), (T + 7400, T + 7500))  # the last 503 seen

    def test_old_evidence_is_not_a_current_outage(self):
        self.feed(T, T + 7200, lambda t: 1000)
        self.feed(T + 7200, T + 7260, lambda t: 100)
        self.assertIsNotNone(self.health.outage(T + 7260))
        self.assertIsNone(self.health.outage(T + 7260 + nh.STALE_SECONDS + 60))

    def test_seeded_from_saved_capacity_readings(self):
        self.h.db.execute(
            'CREATE TABLE opt_network(at INTEGER,model TEXT,active REAL,queued REAL,warm REAL,'
            'routable REAL,PRIMARY KEY(model,at))'
        )
        self.h.db.executemany(
            'INSERT INTO opt_network VALUES(?,?,?,?,?,?)',
            [(t, 'gemma', 200, 0, 1000, 1500) for t in range(T, T + 7200, 30)],
        )
        health = NetworkHealth(self.h, clock=lambda: T + 7200)
        for t in (T + 7230, T + 7260):
            health.on_network('capacity', capacity(0, models={'gemma': (80, 5)}), t)
        self.assertEqual(health.outage(T + 7260)['since'], T + 7230)


class ScopeTests(Base):
    def models(self, gemma):
        return lambda t: {'gemma': (gemma(t), 60), 'gpt': (2000, 240)}

    def test_one_model_collapsing_concerns_macs_serving_it(self):
        self.feed(T, T + 7200, None, models=self.models(lambda t: noise(t, 200)))
        self.feed(T + 7200, T + 7800, None, models=self.models(lambda t: 30))
        outage = self.health.outage(T + 7800)
        self.assertEqual((outage['scope'], outage['key']), ('model', 'gemma'))
        self.assertAlmostEqual(outage['dropPct'], 85, delta=2)
        self.assertIn('Macs serving gemma', nh.describe(outage))
        self.context = ('M5 Pro|48', ['gpt'])
        self.assertIsNone(self.health.outage(T + 7800))

    def test_a_model_without_demand_swings_freely(self):
        # qwen3-vl-30b, Sep 2026: 4-55 warm Macs and no requests at all.
        def models(t):
            idle = 50 if t < T + 7200 else 5
            return {'idle': (idle, 0), 'gpt': (2000, 240)}

        self.context = ('M5 Pro|48', ['idle'])
        self.feed(T, T + 8400, None, models=models)
        self.assertIsNone(self.health.outage(T + 8400))
        self.assertEqual(self.rows(), [])

    def test_one_cell_dropping_concerns_macs_of_that_kind_and_no_ids_are_kept(self):
        cells = {('M5', 'Pro', 48): 40, ('M4', 'Max', 128): 400}
        for t in range(T, T + 7200, 60):
            self.health.on_network('stats', stats(t, cells), t)
        cells[('M5', 'Pro', 48)] = 10
        for t in range(T + 7200, T + 7400, 60):
            self.health.on_network('stats', stats(t, cells), t)
        outage = self.health.outage(T + 7380)
        self.assertEqual((outage['scope'], outage['key']), ('cell', 'M5 Pro|48'))
        self.assertAlmostEqual(outage['dropPct'], 75, delta=1)
        self.assertIn('of M5 Pro 48 GB Macs stopped', nh.describe(outage))
        self.context = ('M4 Max|128', [])
        self.assertIsNone(self.health.outage(T + 7380))
        # Provider ids are reduced to counts on arrival: none in memory or in the table.
        stored = json.dumps([list(r) for r in self.rows()]) + repr(vars(self.health))
        stored += repr([vars(w) for w in self.health.cells.values()])
        self.assertNotIn('provider-', stored)

    def test_macs_marked_untrusted_count_as_a_network_drop(self):
        cells = {('M5', 'Pro', 48): 200, ('M4', 'Max', 128): 400}
        for t in range(T, T + 7200, 60):
            self.health.on_network('stats', stats(t, cells), t)
        for t in range(T + 7200, T + 7400, 60):
            snapshot = stats(t, cells)
            for p in snapshot['providers'][:300]:
                p['status'] = 'untrusted'
            self.health.on_network('stats', snapshot, t)
        outage = self.health.outage(T + 7380)
        self.assertEqual(outage['scope'], 'network')
        self.assertAlmostEqual(outage['dropPct'], 50, delta=1)
        self.assertIn('Online Macs', outage['detail'])


class ReplayTests(unittest.TestCase):
    """Real public capacity readings (totals over models) from Andrew's history."""

    @classmethod
    def setUpClass(cls):
        cls.windows = {w['name']: w for w in json.loads(FIXTURE.read_text())['windows']}

    def replay(self, name, errors=None, checks=()):
        window = self.windows[name]
        h = History(':memory:')
        health = NetworkHealth(h, clock=lambda: window['start'])
        rows = [(window['start'] + dt, warm, active) for dt, warm, active in window['rows']]
        seen = []
        for i, (t, warm, active) in enumerate(rows):
            if errors and i and errors[0] <= t and rows[i - 1][0] < errors[0]:
                # What the app saw in the gap: Darkbloom's API answering 503.
                for e in range(int(errors[0]), int(t), 30):
                    health.on_error('capacity', 'HTTP 503', e)
                    if (e - errors[0]) % 60 == 0:
                        health.on_error('stats', 'HTTP 503', e + 5)
                    seen.append((e, health.outage(e + 5)))
            health.on_network('capacity', capacity(warm, models={'all': (warm, active)}), t)
            seen.append((t, health.outage(t)))
        incidents = h.db.execute(
            'SELECT start,end,scope,before,during FROM network_incidents ORDER BY start'
        ).fetchall()
        h.close()
        return seen, incidents

    @staticmethod
    def at(window, clock):
        """Chicago wall-clock 'HH:MM' on the window's last day -> epoch seconds.

        The fixtures were recorded in Central time; CI runs in UTC."""
        start = datetime.fromtimestamp(window['start'], CENTRAL)
        hour, minute = map(int, clock.split(':'))
        day = start.replace(hour=hour, minute=minute, second=0)
        end = datetime.fromtimestamp(window['start'] + window['rows'][-1][0], CENTRAL)
        if day < start:
            day = end.replace(hour=hour, minute=minute, second=0)
        return day.timestamp()

    def test_sep27_coordinator_down(self):
        w = self.windows['sep27-api-outage']
        gap = next(
            (w['start'] + a[0], w['start'] + b[0])
            for a, b in zip(w['rows'], w['rows'][1:])
            if b[0] - a[0] > 120 and w['start'] + a[0] >= self.at(w, '23:30')
        )
        seen, incidents = self.replay('sep27-api-outage', errors=(gap[0] + 30,))
        during = [o for t, o in seen if gap[0] + 60 <= t <= gap[1]]
        self.assertTrue(during and all(o and o['dropPct'] == 100 for o in during[1:]))
        self.assertIsNone(dict(seen)[max(t for t, _ in seen)])
        (start, end, scope, _, _), = incidents
        self.assertEqual((start, scope), (gap[0] + 30, 'network'))
        self.assertLess(end - start, 15 * 60)  # over within minutes of the API answering

    def test_sep11_partial_drop_this_mac_got_no_work_through(self):
        w = self.windows['sep11-partial-drop']
        seen, incidents = self.replay('sep11-partial-drop')
        (start, end, scope, before, during), = incidents
        self.assertLess(abs(start - self.at(w, '00:56')), 120)
        self.assertLess(abs(end - self.at(w, '01:34')), 120)
        self.assertAlmostEqual(1 - during / before, 0.35, delta=0.03)
        mid = min(seen, key=lambda s: abs(s[0] - self.at(w, '01:15')))[1]
        self.assertEqual(mid['scope'], 'network')

    def test_sep08_collapse(self):
        w = self.windows['sep08-collapse']
        seen, incidents = self.replay('sep08-collapse')
        start, end, scope, before, during = incidents[0]
        self.assertLess(abs(start - self.at(w, '19:32')), 60)
        self.assertLess(during / before, 0.05)
        self.assertGreater(end - start, 30 * 60)  # through the slow reconnection

    def test_sep26_drained_night_and_sep25_app_attest_are_not_network_outages(self):
        for name in ('sep26-drained-night', 'sep25-app-attest'):
            with self.subTest(name=name):
                seen, incidents = self.replay(name)
                self.assertEqual(incidents, [])
                self.assertFalse(any(o for _, o in seen))


class FakeHealth:
    def __init__(self, outage=None, signal='warm'):
        self.value, self.kind = outage, signal

    def outage(self, now=None, model=None):
        return self.value

    def signal(self, now=None, model=None):
        return self.kind if self.value else None


OUTAGE = {'since': T, 'scope': 'network', 'key': None, 'dropPct': 62.0, 'detail': 'x'}


class EffectTests(unittest.TestCase):
    def test_stall_ladder_waits_instead_of_restarting_or_escaping(self):
        health = FakeHealth(OUTAGE)
        for step in ('restart', 'escape'):
            result = stall_wait({'step': step, 'reason': 'r', 'model': 'a'}, health, T)
            self.assertIsNone(result['step'])
            self.assertIn('Darkbloom network problem since', result['reason'])
            self.assertIn('62% of Macs stopped getting work', result['reason'])
            self.assertEqual(result['networkOutage'], OUTAGE)
        for step in ('probe', 'hold', None):
            self.assertEqual(stall_wait({'step': step}, health, T), {'step': step})
        # A single model's problem: no restart, but moving to another model helps.
        model = FakeHealth({**OUTAGE, 'scope': 'model', 'key': 'a'})
        self.assertIsNone(stall_wait({'step': 'restart'}, model, T)['step'])
        self.assertEqual(stall_wait({'step': 'escape'}, model, T), {'step': 'escape'})
        self.assertEqual(stall_wait({'step': 'restart'}, FakeHealth(), T), {'step': 'restart'})
        self.assertEqual(stall_wait({'step': 'restart'}, None, T), {'step': 'restart'})

    def test_support_reports_hold_automatic_ones_only_during_an_outage(self):
        health = FakeHealth(OUTAGE)
        sent = []
        reporter = reports.SupportReports(
            SimpleNamespace(network_health=health),
            transport=lambda *a: (
                sent.append(a),
                (200, {'status': 'sent', 'reportId': json.loads(a[1])['id']}),
            )[1],
            now=lambda: T,
            capture=lambda remote: {},
            store=reports._MemoryStore(),
        )
        reporter.set_auto({'autoSend': True})
        data = {'category': 'connection', 'context': 'models', 'description': '', 'contact': ''}
        with self.assertRaises(reports.SupportError) as refused:
            reporter.preview({**data, 'automatic': True})
        self.assertEqual(refused.exception.response['error'], reports.AUTO_OUTAGE)
        manual = reporter.preview({**data, 'category': 'manual'})
        confirm = {
            'reportId': manual['report']['id'],
            'reviewToken': manual['reviewToken'],
            'confirmed': True,
        }
        self.assertEqual(reporter.send(confirm)['status'], 'sent')
        health.value = None
        automatic = reporter.preview({**data, 'automatic': True})
        self.assertTrue(automatic['report']['id'])

    def test_activity_log_notes_the_start_and_the_end_once(self):
        store = SimpleNamespace(events=[])
        store.event = lambda *a, **k: store.events.append(a)
        o = SimpleNamespace(network_health=FakeHealth(OUTAGE), outage_logged={}, store=store)
        for now in (T, T + 30, T + 60):
            optimizer.Optimizer.log_network_outage(o, 'acct', 'mac', now)
        o.network_health.value = None
        for now in (T + 90, T + 120):
            optimizer.Optimizer.log_network_outage(o, 'acct', 'mac', now)
        kinds = [e[3] for e in store.events]
        self.assertEqual(kinds, ['network-outage', 'network-recovered'])
        self.assertIn('Nothing to fix on this Mac', store.events[0][5])
        self.assertIn('is over', store.events[1][5])


class StallControlTests(unittest.TestCase):
    setUp = ladder.ControlTests.setUp
    tearDown = ladder.ControlTests.tearDown
    control = ladder.ControlTests.control

    def test_no_restart_while_the_network_is_down_and_the_ladder_resumes_after(self):
        self.o.network_health = FakeHealth(OUTAGE)
        c = self.control()
        self.store.event('acct', 'mac', ladder.T0 + 300, 'stall-probe', 'a', 'probe')
        self.assertFalse(c.tick(ladder.T0 + 480, self.settings, self.live, self.raw, 'a', [], {}))
        self.o.dispatch_stall_restart.assert_not_called()
        status = c.snapshot(ladder.T0 + 480)
        self.assertEqual((status['status'], status['step']), ('stalled', None))
        self.assertIn('Not restarting', status['reason'])
        self.assertIsNone(
            c.confirm_restart('acct', 'mac', self.raw, 'a', ladder.policy(), ladder.T0 + 485)
        )
        self.o.network_health.value = None  # recovered
        self.assertTrue(c.tick(ladder.T0 + 490, self.settings, self.live, self.raw, 'a', [], {}))
        decision = c.confirm_restart('acct', 'mac', self.raw, 'a', ladder.policy(), ladder.T0 + 495)
        self.assertEqual(decision['kind'], 'recovery')


class WiringTests(unittest.TestCase):
    def test_network_passes_failures_to_error_listeners(self):
        net = Network(History(':memory:'), SimpleNamespace(is_set=lambda: False))
        calls = []
        net.error_listeners.append(lambda *a: calls.append(a[:2]))
        net.fetch = Mock(side_effect=urllib.error.HTTPError('u', 503, 'x', {}, None))
        stop = SimpleNamespace(n=0)

        def is_set():
            stop.n += 1
            return stop.n > 1

        net.stop = SimpleNamespace(is_set=is_set, wait=lambda s: None)
        net.loop('capacity', '/v1/models/capacity', 30)
        self.assertEqual(calls, [('capacity', 'HTTP 503')])

    def test_collector_feeds_it_shares_it_and_puts_it_in_the_snapshot(self):
        from collector import Collector

        with tempfile.TemporaryDirectory() as root:
            c = Collector(root)
            self.assertIn(c.network_health.on_network, c.network.listeners)
            self.assertIn(c.network_health.on_error, c.network.error_listeners)
            self.assertIs(c.optimizer.network_health, c.network_health)
            c.collect(500)
            self.assertEqual(c.snapshot['networkHealth']['status'], 'learning')
            self.assertIsNone(c.snapshot['networkHealth']['outage'])
            c.optimizer.raw = {'advertised_models': ['gemma']}
            self.assertEqual(c.network_health_context()[1], ['gemma'])

    def test_incidents_are_kept_90_days(self):
        h = History(':memory:')
        NetworkHealth(h)
        now = 200 * 86400
        h.db.executemany(
            'INSERT INTO network_incidents(start,end,scope,key,before,during,detail) '
            'VALUES(?,?,?,?,?,?,?)',
            [(d * 86400, d * 86400 + 600, 'network', None, 1000, 100, 'x') for d in range(200)],
        )
        retention.prune(h, now, pause=0)
        oldest = h.db.execute('SELECT MIN(start) FROM network_incidents').fetchone()[0]
        self.assertEqual(oldest, 110 * 86400)
        h.close()


if __name__ == '__main__':
    unittest.main()
