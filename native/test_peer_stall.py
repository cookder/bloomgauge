"""Peer-based stall detection ("Macs like yours are getting work; this one isn't") and the
"Macs like yours" benchmark: synthetic cases, plus replays of the public /v1/stats poll of
Sep 27-28, 2026 (10-minute request counts; the poller kept no provider ids)."""

import pathlib
import tempfile
import threading
import unittest
from types import SimpleNamespace

import stall_recovery as sr
from history import History
from network_evidence import NetworkEvidence
from optimizer_store import OptimizerStore
from stall_control import StallControl
from test_network_evidence import GEMMA, GPT, HOUR, PRICING, prov, stats

T0 = 1_800_000_000
STEP = 300


def window(end, requests=0, peers=(300,) * 6, model=GEMMA, trusted=True, seconds=STEP):
    return {
        'at': end,
        'start': end - seconds,
        'model': model,
        'cell': 'M5 Pro|48',
        'dedicated': True,
        'requests': requests,
        'trusted': trusted,
        'peers': len(peers),
        'peer_rates': tuple(sorted(peers)),
    }


def windows(first_zero, until, before=3, **kw):
    """Windows ending every 5 min: `before` with work, then none from `first_zero` on."""
    out = [window(first_zero - i * STEP, requests=20) for i in range(before, 0, -1)]
    at = first_zero + STEP
    while at <= until:
        out.append(window(at, **kw))
        at += STEP
    return out


def trickle(end=T0, minutes=30, every=3):
    """Slow work: a job every few minutes, well under the own trigger's steady bar."""
    return [
        {'at': end - (minutes - i) * 60, 'model': GEMMA, 'seconds': 60, 'jobs': 1 if i % every == 0 else 0}
        for i in range(minutes)
    ]


def silent(start=T0, minutes=90, model=GEMMA):
    return [{'at': start + i * 60, 'model': model, 'seconds': 60, 'jobs': 0} for i in range(minutes)]


def busy_network(start=T0 - 1800, end=T0 + 7200, pressure=0.5, drop_at=None):
    return {
        GEMMA: [
            {
                'at': at,
                'active': 100 * pressure / (4 if drop_at and at >= drop_at else 1),
                'queued': 0,
                'warm': 100,
            }
            for at in range(start, end, 30)
        ]
    }


def check(now, peers, minutes=None, attempts=(), net=None, **kw):
    rows = minutes if minutes is not None else trickle() + silent()
    return sr.assess(
        [m for m in rows if m['at'] + 60 <= now],
        net or busy_network(),
        list(attempts),
        [],
        None,
        now,
        peers=[w for w in peers if w['at'] <= now] if peers is not None else None,
        **kw,
    )




class PeerTriggerTests(unittest.TestCase):
    def test_fires_when_peers_get_work_and_this_mac_gets_none(self):
        peers = windows(T0, T0 + 3600)
        # The own trigger alone never calls slow work a stall.
        self.assertEqual(check(T0 + 1800, None)['status'], 'quiet')
        early = check(T0 + 540, peers)  # 9 minutes: not yet
        self.assertIsNone(early['step'])
        r = check(T0 + 660, peers)
        self.assertEqual((r['status'], r['trigger'], r['step']), ('stalled', 'peers', 'probe'))
        self.assertTrue(r['reason'].startswith(sr.PEER_NOTE))
        self.assertIn('6 similar Macs got a median of 300 requests an hour', r['reason'])
        self.assertTrue(r['peers']['busy'])
        self.assertEqual(r['requiredSeconds'], sr.PEER_SILENCE_SECONDS)
        # A restart only after 20 minutes without work (four windows), not 3 min after the probe.
        probe = [{'at': T0 + 660, 'step': 'probe'}]
        waiting = check(T0 + 900, peers, attempts=probe)
        self.assertIsNone(waiting['step'])
        self.assertIn('20 minutes', waiting['reason'])
        r = check(T0 + 1260, peers, attempts=probe)
        self.assertEqual(r['step'], 'restart')
        self.assertTrue(r['reason'].startswith(sr.PEER_NOTE))
        # The probe's own request reaching this Mac doesn't end the run.
        nudged = [dict(w, requests=1) if w['start'] <= T0 + 700 < w['at'] else w for w in peers]
        self.assertEqual(check(T0 + 1260, nudged, attempts=probe)['step'], 'restart')

    def test_fewer_than_five_peers_is_not_enough(self):
        peers = windows(T0, T0 + 3600, peers=(600,) * 4)
        r = check(T0 + 1800, peers)
        self.assertEqual(r['status'], 'quiet')
        self.assertFalse(r['peers']['busy'])

    def test_peers_idle_too_is_ordinary_quiet(self):
        for rates in ((0,) * 6, (0, 0, 0, 36, 48, 60), (30,) * 8):
            r = check(T0 + 1800, windows(T0, T0 + 3600, peers=rates))
            self.assertEqual(r['status'], 'quiet', rates)
            self.assertIsNone(r['step'])
        # Busy earlier but nothing for anyone in the latest window: demand stopped, not this Mac.
        peers = windows(T0, T0 + 900) + [window(T0 + 1200, peers=(0,) * 6)]
        self.assertEqual(check(T0 + 1260, peers)['status'], 'quiet')

    def test_untrusted_other_model_stale_or_broken_runs_do_not_count(self):
        cases = {
            'untrusted': windows(T0, T0 + 3600, trusted=False),
            'other model': windows(T0, T0 + 3600, model=GPT),
            'stale': windows(T0, T0 + 600),
        }
        for name, peers in cases.items():
            self.assertEqual(check(T0 + 1800, peers)['status'], 'quiet', name)
        # A missing window (the Mac left the stats, or its session restarted) restarts the run.
        gap = [w for w in windows(T0, T0 + 1200) if w['at'] != T0 + 900]
        run = sr.peer_stall(gap, GEMMA, T0 + 1200)
        self.assertEqual((run['seconds'], run['windows'], run['since']), (300, 1, T0 + 900))
        self.assertEqual(check(T0 + 1260, gap)['status'], 'quiet')
        # A missing trust field is unknown, not untrusted.
        unknown = windows(T0, T0 + 3600, trusted=None)
        self.assertEqual(check(T0 + 660, unknown)['trigger'], 'peers')

    def test_peers_overrule_a_fall_in_demand_readings(self):
        # Own steady work stops and the model's demand readings fall: normally a straight
        # escape. Macs like this one still getting work say demand for them held.
        steady = [{'at': T0 - (30 - i) * 60, 'model': GEMMA, 'seconds': 60, 'jobs': 10} for i in range(30)]
        rows = steady + silent()
        net = busy_network(drop_at=T0)
        self.assertEqual(check(T0 + 300, None, minutes=rows, net=net)['step'], 'escape')
        r = check(T0 + 660, windows(T0, T0 + 3600), minutes=rows, net=net)
        self.assertEqual((r['trigger'], r['step'], r['demandHeld']), ('own', 'probe', False))
        self.assertTrue(r['reason'].startswith(sr.PEER_NOTE))

    def test_budgets_and_pressure_gate_still_apply(self):
        peers = windows(T0, T0 + 3600)
        probe = [{'at': T0 + 660, 'step': 'probe'}]
        low = check(T0 + 1260, peers, attempts=probe, net=busy_network(pressure=0.2))
        self.assertEqual(low['step'], 'escape')
        self.assertIn('Not restarting', low['reason'])
        self.assertTrue(low['reason'].startswith(sr.PEER_NOTE))
        spent = [{'at': T0 - 7200 - i * 4000, 'step': 'restart'} for i in range(3)]
        self.assertEqual(check(T0 + 1260, peers, attempts=spent + probe)['step'], 'escape')


class FakeStore:
    def __init__(self, h):
        self.h = h
        self.events = []

    def event(self, account, device, at, kind, model, detail, downtime=0):
        self.events.append((kind, model, detail))
        OptimizerStore.event(self, account, device, at, kind, model, detail, downtime)


class ControlTests(unittest.TestCase):
    """StallControl feeds the evidence windows in, and stays quiet during an outage."""

    def setUp(self):
        self.h = History(':memory:')
        OptimizerStore(self.h)
        self.store = FakeStore(self.h)
        self.windows = windows(T0, T0 + 3600)
        self.o = SimpleNamespace(
            store=self.store,
            home='/tmp',
            lock=threading.RLock(),
            detail='',
            last_demand_decision=None,
            tracking=lambda raw, now: {'counting': True},
            network_evidence=SimpleNamespace(
                own_windows=lambda now: [w for w in self.windows if w['at'] <= now]
            ),
        )
        with self.h.lock:
            for m in trickle() + silent(minutes=30):
                self.h.db.execute(
                    'INSERT INTO opt_ready_minutes VALUES(?,?,?,?,?,?,?,?)',
                    ('acct', 'mac', m['at'], m['model'], 60, m['jobs'], 0, 0),
                )
            for row in busy_network(end=T0 + 1800)[GEMMA]:
                self.h.db.execute(
                    'INSERT OR REPLACE INTO opt_network VALUES(?,?,?,?,?,?)',
                    (row['at'], GEMMA, row['active'], 0, row['warm'], 100),
                )
            self.h.db.commit()
        self.control = StallControl(
            self.o, keychain=lambda: None, route=lambda *a: None, local=lambda *a, **k: None
        )
        self.raw = {'started_at': T0 - 9000, 'pid': 1}

    def tearDown(self):
        self.h.close()

    def tick(self, now):
        settings = {'mode': 'demand', 'demandPolicy': {'managerStrategy': 1}}
        live = {'account': 'acct', 'device': 'mac'}
        self.control.tick(now, settings, live, self.raw, GEMMA, [], {})
        for _ in range(50):
            if not self.control.nudging:
                break
            threading.Event().wait(0.02)
        return self.control.snapshot(now)

    def test_probe_is_logged_in_plain_words(self):
        s = self.tick(T0 + 660)
        self.assertEqual((s['status'], s['trigger']), ('stalled', 'peers'))
        kinds = [e[0] for e in self.store.events]
        self.assertEqual(kinds[0], 'stall-probe')
        self.assertTrue(self.store.events[0][2].startswith(sr.PEER_NOTE))
        self.assertTrue(self.o.detail.startswith(sr.PEER_NOTE))

    def test_quiet_during_a_network_wide_outage(self):
        outage = {'since': T0, 'scope': 'network', 'key': '*', 'dropPct': 60, 'detail': 'x'}
        self.o.network_health = SimpleNamespace(outage=lambda now: outage)
        s = self.tick(T0 + 1800)
        self.assertEqual(s['status'], 'quiet')
        self.assertEqual(self.store.events, [])
        # No outage, or no outage information (an error), leaves the peer trigger on.
        for health in (lambda now: None, lambda now: 1 / 0):
            self.o.network_health = SimpleNamespace(outage=health)
            self.assertEqual(self.control.evaluate('acct', 'mac', self.raw, T0 + 660)['step'], 'probe')

    def test_restart_decision_carries_the_trigger(self):
        self.store.event('acct', 'mac', T0 + 660, 'stall-probe', GEMMA, 'probe')
        decision = self.control.confirm_restart('acct', 'mac', self.raw, GEMMA, {}, T0 + 1260)
        self.assertEqual(decision['stall']['trigger'], 'peers')
        self.assertTrue(decision['stall']['peers']['busy'])
        self.assertTrue(decision['reason'].startswith(sr.PEER_NOTE))

    def test_manager_hold_keeps_the_plain_words(self):
        for at, step in ((T0 + 660, 'probe'), (T0 + 1260, 'restart')):
            self.store.event('acct', 'mac', at, 'stall-' + step, GEMMA, step)
        self.tick(T0 + 1260 + 600)
        hold = [e for e in self.store.events if e[0] == 'stall-hold']
        self.assertEqual(len(hold), 1)
        self.assertTrue(hold[0][2].startswith(sr.PEER_NOTE))
        self.assertIn('Holding the home model', hold[0][2])


def spread(count, start):
    """`count` jobs over the 10 minutes from `start`, as whole jobs per minute."""
    return [
        {
            'at': start + i * 60,
            'model': GEMMA,
            'seconds': 60,
            'jobs': (i + 1) * count // 10 - i * count // 10,
        }
        for i in range(10)
    ]


# Public /v1/stats poll, Sep 28 04:44-07:04 CDT, 10-minute request counts of dedicated gemma
# boxes in Andrew's cell (M5 Pro 48 GB). 'me' is a Mac whose work stopped after 05:14 and never
# came back while the others got 80-150 requests per 10 minutes (it was still at zero at 08:44).
# Its own history was 1.8-4.2 jobs a minute: too slow for the own trigger.
STALLED_PEER = [
    {'p0': 29, 'p1': 56, 'p2': 47, 'p3': 55, 'p4': 38, 'p5': 31, 'me': 29, 'p7': 40},
    {'p0': 26, 'p1': 57, 'p2': 48, 'p3': 59, 'p4': 34, 'p5': 27, 'me': 18},
    {'p0': 57, 'p1': 73, 'p2': 64, 'p3': 69, 'p4': 24, 'p5': 40, 'me': 42},
    {'p0': 0, 'p1': 72, 'p2': 69, 'p3': 48, 'p4': 68, 'p5': 138, 'me': 13, 'p8': 15},
    {'p0': 0, 'p1': 107, 'p2': 151, 'p3': 112, 'p4': 121, 'p5': 149, 'me': 0},
    {'p0': 0, 'p1': 122, 'p2': 105, 'p3': 0, 'p4': 81, 'p5': 98, 'me': 0, 'p9': 0},
    {'p0': 0, 'p1': 100, 'p2': 117, 'p3': 0, 'p4': 83, 'p5': 96, 'me': 0, 'p9': 0, 'p10': 38},
    {'p0': 0, 'p1': 115, 'p2': 113, 'p3': 0, 'p4': 117, 'p5': 79, 'me': 0, 'p9': 0, 'p10': 51},
    {'p0': 0, 'p1': 115, 'p2': 117, 'p3': 0, 'p4': 91, 'p5': 76, 'me': 0},
    {'p0': 0, 'p1': 122, 'p2': 94, 'p3': 0, 'p4': 88, 'p5': 72, 'me': 0},
    {'p1': 95, 'p3': 0, 'p4': 50, 'p5': 60, 'me': 0, 'p11': 40},
    {'p1': 139, 'p3': 0, 'p4': 149, 'p5': 121, 'me': 0},
    {'p1': 176, 'p3': 117, 'p4': 151, 'p5': 153, 'me': 0},
]

# The same poll from Andrew's side, Sep 27 09:14 to Sep 28 08:34 CDT, one line per 10 minutes:
# his count on dedicated gemma ('-' while his Mac wasn't on it: the 15:38-17:18 cold qwen period,
# and a session change at 22:54), then the counts of his cell's other dedicated gemma boxes.
ANDREW_DAY = """199:125 137 140 151 182 237 269
256:104 157 165 183 187 207 215
259:139 158 165 179 187 189 207 207
142:41 130 133 135 154 180 275
188:87 93 108 121 122 122 129 139
254:80 130 147 155 174 180 239
222:57 148 153 169 174 182 196 272
190:39 69 136 143 157 168 208
239:141 150 155 166 166 221 273
179:88 96 132 146 154 180 190 208
223:74 78 81 106 111 150 153 309
123:33 81 90 105 106 110 115 123
149:62 72 120 124 128 137 154
230:8 71 119 152 157 161 180
188:107 109 123 139 150 153 238
158:75 98 103 104 109 112 140
141:66 72 80 98 114 115 116
242:93 94 111 148 150 159 211
147:41 82 85 88 95 117 135 145
41:10 23 26 30 35 36 51 66
46:30 38 48 56 61 89 102 111
84:24 45 52 72 87 94 106 116
45:4 21 28 37 42 53 55 80
60:31 61 70 75 75 90 153
81:33 40 53 58 63 72 93 112
158:103 105 107 116 125 126 136 154
62:34 43 43 49 50 65 105 115
53:32 44 45 45 46 53 94 101
21:9 9 11 13 15 28 33 38
57:42 43 54 68 99 133 139
63:34 43 49 50 70 116
33:15 20 25 50 54 57 80 131
89:68 71 94 108 119 143 162 170
61:51 81 89 90 117 138 165
32:29 34 37 44 55 63 126
44:24 34 37 60 66 72 124
38:26 29 30 36 53 55 87
141:86 116 124 130 133 145 152
156:94 118 132 138 140 140 151
-:0 2 17 41 43 55
-:1 2 5 44 56 58
-:26 34 37 41 60 68
-:2 18 18 20 26 35
-:2 4 6 6 11 11
-:11 15 18 23 27 29
-:41 42 44 47 51 61 69
-:4 13 19 19 25 32 36
-:24 31 55 58 69 77
-:19 37 50 51 59 70
-:52 57 68 77 77 91
38:26 30 35 45 60 69
46:25 29 33 42 58 90
7:2 5 5 9 11 12
28:2 14 16 24 30 33 38
47:2 19 22 35 47 51 52
21:2 5 10 10 15 16 45
47:2 12 27 42 44 47 49
39:2 21 23 30 36 39 52
6:3 9 11 12 12 13 14
35:1 35 40 45 53 57 59
32:4 23 30 53 54 55 55
131:19 121 154 155 174 177 189
69:55 60 69 79 85 91 102
57:48 50 51 73 74 87 90
15:18 18 20 21 31 35 42
27:21 21 26 27 45 54 58
26:18 21 29 36 56 57 79
9:4 6 8 11 12 14 15
26:24 27 27 35 49 55 59
10:10 12 15 19 41 44 55
7:6 6 8 10 16 17 29
22:18 20 21 30 37 53 59
33:26 28 31 47 67 75 89
13:10 10 14 18 22 24 30
14:9 10 18 19 24 24 30
5:5 5 5 6 6 7 14
15:12 12 13 14 31 34 38
12:9 12 15 15 17 26 29
2:3 3 4 5 5 8 11
5:8 14 14 14 19 19 26
15:11 14 19 25 36 36
-:2 6 6 6 20
20:0 35 38 40 41 52 65
55:0 52 53 65 70 72 77 102
102:0 56 69 80 94 112 128 137
10:0 0 0 5 14 14 20 34
58:0 13 15 43 47 48 52 75
38:16 21 31 32 38 41 57
12:11 14 18 19 24 26 36 40
13:0 6 17 20 21 25 38 54
20:0 16 18 33 33 35 38 45
8:0 3 6 7 10 10 14 15
17:0 9 18 24 27 29 31 44
25:2 19 33 36 39 45 46 53
151:115 132 139 144 156 157 171 193
53:11 27 38 56 68 68 75 77
1:0 0 2 5 21 24 25 35
20:0 0 10 14 24 31 37 57
29:0 6 16 42 46 48 51 64
21:0 12 14 26 29 32 42 54
29:0 4 21 27 31 54
33:0 1 18 31 35 46 50 57
21:0 9 11 14 25 34 38 47
7:0 2 3 3 6 14 16 31
25:0 13 16 16 32 35 38 41
19:0 8 13 20 23 28 41 46
124:0 96 101 118 122 181 188
184:0 89 115 166 190 195 237
158:45 68 137 168 178 200 209
204:67 98 198 205 206 207 225
86:82 85 90 94 98 99 100
15:9 28 30 34 36 36 40
14:10 19 20 20 20 25 33
21:15 31 32 38 41 43 47
31:29 29 38 40 47 55 56
27:18 26 34 48 57 59
40:24 42 57 64 69 73
138:0 13 15 48 68 69 72
149:0 0 107 112 121 151
98:0 0 0 0 81 105 122
96:0 0 0 0 38 83 100 117
79:0 0 0 0 51 113 115 117
76:0 0 0 91 115 117
72:0 0 0 88 94 122
60:0 0 40 50 95
121:0 0 139 149
153:0 117 151 176
98:0 101 125 131 150
109:0 133 146 147
128:0 109 115 124 134
91:0 84 98 103 110 116
127:0 64 116 128 159
125:0 91 95 105 121 121
92:0 52 104 123 124
106:0 88 89 93 125
161:0 124 126 141 152 174
136:0 121 128 154 164
187:0 0 147 171 175 190 199 206
121:0 0 96 119 125 126 133 149"""


class ReplayTests(unittest.TestCase):
    def test_a_real_stall_in_andrews_cell_through_the_evidence_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = History(pathlib.Path(tmp) / 'h.sqlite3')
            self.addCleanup(h.close)
            evidence = NetworkEvidence(h, lambda: {'me'}, lambda: None)
            evidence.set_pricing(PRICING)
            start = HOUR + 600
            totals, minutes, seen = {}, [], {}
            for i, counts in enumerate([{k: 0 for k in STALLED_PEER[0]}] + STALLED_PEER):
                at = start + i * 600
                present = set(counts) | (set(STALLED_PEER[i]) if i < len(STALLED_PEER) else set())
                for k in counts:
                    totals[k] = totals.get(k, 0) + counts[k]
                providers = [
                    dict(prov(k, req=totals.get(k, 0), tok=totals.get(k, 0) * 200), trust_level='hardware')
                    for k in sorted(present)
                ]
                evidence.observe(stats(at, providers), at)
                if i:
                    minutes += spread(counts['me'], at - 600)
                seen[at] = sr.assess(
                    minutes,
                    busy_network(start=start - 1800, end=at + 60),
                    [],
                    [],
                    None,
                    at + 30,
                    peers=evidence.own_windows(at + 30),
                )
            ends = sorted(seen)
            # Work stopped after the 4th window (05:14); the own trigger never qualifies.
            for at in ends[:5]:
                self.assertNotEqual(seen[at]['trigger'], 'peers')
            r = seen[ends[5]]  # 05:24: ten minutes without work while five peers got 107-151
            self.assertEqual((r['status'], r['trigger'], r['step']), ('stalled', 'peers', 'probe'))
            self.assertLess(r['baselineJobsPerMinute'], sr.MIN_JOBS_PER_MINUTE)
            self.assertTrue(r['reason'].startswith(sr.PEER_NOTE))
            r = seen[ends[6]]  # 05:34: twenty minutes, 3 of 7 peers idle too, the rest busy
            self.assertEqual(r['trigger'], 'peers')
            self.assertGreaterEqual(r['peers']['peerReqPerHour'], sr.PEER_MIN_REQ_H)
            probe = [{'at': ends[5] + 30, 'step': 'probe'}]
            later = sr.assess(
                minutes,
                busy_network(start=start - 1800, end=ends[6] + 60),
                probe,
                [],
                None,
                ends[6] + 30,
                peers=evidence.own_windows(ends[6] + 30),
            )
            self.assertEqual(later['step'], 'restart')
            # Its benchmark over the last 2 hours shows it far behind Macs like it.
            bench = evidence.benchmark(ends[-1] + 30)
            self.assertLess(bench['reqPerHour'], bench['peerMedianReqPerHour'] / 10)
            self.assertLess(bench['percentile'], 0.2)
            self.assertGreaterEqual(bench['peers'], 5)

    def test_andrews_own_day_never_trips_the_peer_trigger(self):
        start = T0
        minutes, peers, fired, zero_windows = [], [], [], 0
        for i, line in enumerate(ANDREW_DAY.splitlines()):
            own, _, rest = line.partition(':')
            end = start + (i + 1) * 600
            if own == '-':
                continue
            count = int(own)
            zero_windows += count == 0
            minutes += spread(count, end - 600)
            rates = tuple(int(x) * 6 for x in rest.split())
            peers.append(window(end, requests=count, peers=rates, seconds=600))
            for now in range(end, end + 600, 60):
                p = sr.peer_stall(peers, GEMMA, now)
                if p and p['busy'] and p['seconds'] >= sr.PEER_SILENCE_SECONDS:
                    fired.append(now)
        self.assertEqual(len(peers), 127)
        self.assertEqual((zero_windows, fired), (0, []))
        # Its second-quietest window (Sep 27 22:24: 2 jobs while peers got 3-11) would not have
        # counted even with no work at all: the peers' median was under the 60 req/h bar.
        quiet = window(T0, peers=tuple(x * 6 for x in (3, 3, 4, 5, 5, 8, 11)), seconds=600)
        self.assertFalse(sr.peer_stall([quiet], GEMMA, T0)['busy'])


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.h = History(pathlib.Path(self.tmp.name) / 'h.sqlite3')
        self.addCleanup(self.h.close)
        self.e = NetworkEvidence(self.h, lambda: {'me'}, lambda: None)
        self.e.set_pricing(PRICING)

    def feed(self, t0, rounds, mine=(10, 20), peers=((5, 10, 20, 30, 40, 60),)):
        at = t0
        totals = {}
        for r in range(rounds):
            own = mine[r % len(mine)]
            rates = peers[r % len(peers)]
            counts = {'me': own, **{'p%d' % i: x for i, x in enumerate(rates)}}
            providers = []
            for k, c in counts.items():
                totals[k] = totals.get(k, 0) + (c if r else 0)
                providers.append(dict(prov(k, req=totals[k], tok=totals[k] * 100), trust_level='hardware'))
            self.e.observe(stats(at, providers), at)
            at += 300
        return at - 300

    def test_shape_and_cache(self):
        self.assertIsNone(self.e.benchmark(HOUR))
        end = self.feed(HOUR + 600, 5)
        b = self.e.benchmark(end + 10)
        self.assertEqual((b['cell'], b['model'], b['dedicated']), ('M5 Pro|48', GEMMA, True))
        self.assertEqual((b['windows'], b['peers'], b['hours']), (4, 6, 2))
        # Own windows alternate 20 and 10 requests per 5 minutes: 180 req/h on average.
        self.assertAlmostEqual(b['reqPerHour'], 180)
        # Peers 60-720 req/h: median of (120, 240, 360, 480, ...) pooled over the windows.
        self.assertAlmostEqual(b['peerMedianReqPerHour'], 300)
        # At 240 req/h it beats 2 of 6 peers and ties one; at 120, 1 and a tie.
        self.assertAlmostEqual(b['percentile'], (2.5 / 6 + 1.5 / 6) / 2)
        self.assertEqual(b['usdBasis'], 'list')
        self.assertAlmostEqual(b['usdPerHour'] / b['peerUsdPerHour'], 180 / 300)
        self.assertEqual(b['at'], end)
        self.assertNotIn('me', repr(b))
        # Cached for a minute; a new window refreshes it.
        self.assertIs(self.e.benchmark(end + 30), b)
        later = self.feed(end + 300, 2)
        self.assertIsNot(self.e.benchmark(later + 10), b)

    def test_own_windows_are_numbers_only(self):
        end = self.feed(HOUR + 600, 3, mine=(0,))
        w = self.e.own_windows(end + 10)
        self.assertEqual([x['requests'] for x in w], [0, 0])
        self.assertEqual({x['trusted'] for x in w}, {True})
        self.assertEqual(w[-1]['peers'], 6)
        self.assertEqual(w[-1]['start'], w[-2]['at'])
        self.assertFalse(any(isinstance(v, str) and v.startswith('p') for x in w for v in x.values()))


if __name__ == '__main__':
    unittest.main()
