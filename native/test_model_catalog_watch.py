"""Network news: new, dropped and capacity-swing detection, the push notice and the view.

Temporary history only; synthetic payloads plus real capacity windows from
model_catalog_fixture.json (aggregates from Andrew's capacity history)."""

import json
import pathlib
import tempfile
import threading
import unittest
from types import SimpleNamespace

import model_catalog_watch as mcw
import retention
from history import History
import test_remote

FIXTURE = pathlib.Path(__file__).with_name('model_catalog_fixture.json')
T0 = 1_790_000_000
BASE = {'gpt-oss-20b': (500, 300), 'gemma-4-26b-qat-4bit': (550, 400), 'Qwen3.5-9B': (200, 120)}


def capacity(models):
    """{model: (routable, warm[, demand])} -> a /v1/models/capacity payload."""
    return {
        'models': [
            {
                'id': m,
                'routable_providers': v[0],
                'warm_providers': v[1],
                'active_requests': v[2] if len(v) > 2 else 0,
                'queued_requests': 0,
            }
            for m, v in models.items()
        ]
    }


def stats(serving=None, offering=None):
    """{model: Macs with it loaded} -> a /v1/stats payload (no ids that matter here)."""
    providers = []
    for m, n in (serving or {'gpt-oss-20b': 1}).items():
        providers += [
            {'id': f'p{m}{i}', 'status': 'serving', 'current_model': m, 'models': [m]}
            for i in range(n)
        ]
    for m, n in (offering or {}).items():
        providers += [
            {'id': f'o{m}{i}', 'status': 'online', 'current_model': None, 'models': [m]}
            for i in range(n)
        ]
    return {'providers': providers}


def fresh_catalog(watch):
    """The optimizer's catalog copy as a removal needs it: fresh, the dropped models inactive."""
    watch.catalog = lambda: ([{'id': 'catalog-model', 'active': True}], watch.capacity_at)
    return watch


class FakeEvidence:
    def __init__(self, rows=None, served=None):
        self.rows = rows or {}
        self.served = served

    def estimate(self, model, cell=None, hours=2, now=None, basis=None):
        r = self.rows.get(model)
        if not r:
            return {'model': model, 'cell': 'M5 Pro|48', 'source': 'none', 'usd_per_h': None}
        return {
            'model': model,
            'cell': 'M5 Pro|48',
            'source': 'cell',
            'usd_per_h': r[0],
            'low': r[1],
            'high': r[0] * 2,
            'providers': 7,
        }

    def table(self, cell=None, hours=2, now=None, basis=None):
        return {'models': [self.estimate(m) for m in self.rows]}

    def self_percentile(self, hours=4, now=None):
        return {'model': self.served} if self.served else None


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.history = History(pathlib.Path(self.tmp.name) / 'history.sqlite')

    def tearDown(self):
        self.history.close()
        self.tmp.cleanup()

    def make(self, **kwargs):
        watch = mcw.ModelCatalogWatch(self.history, **kwargs)
        return watch if 'catalog' in kwargs else fresh_catalog(watch)

    def events(self, kind=None):
        with self.history.lock:
            rows = self.history.db.execute(
                'SELECT at,kind,model,detail FROM model_catalog_events ORDER BY at'
            ).fetchall()
        return [
            (at, k, m, json.loads(d)) for at, k, m, d in rows if kind is None or k == kind
        ]

    def seeded(self, models=BASE, **kwargs):
        watch = self.make(**kwargs)
        watch.on_network('stats', stats(), T0 - 1)
        self.assertEqual(watch.observe_capacity(capacity(models), T0), [])
        return watch

    def run_minutes(self, watch, start, minutes, models, step=30, stats_payload=None):
        t = start
        while t < start + minutes * 60:
            if stats_payload is not None and (t - start) % 60 == 0:
                watch.on_network('stats', stats_payload, t)
            watch.on_network('capacity', capacity(models), t)
            t += step
        return t


class MembershipTests(Base):
    def test_first_run_seeds_without_news_and_survives_a_restart(self):
        watch = self.make()
        # Capacity alone does not seed: /v1/stats must have been seen once too.
        watch.on_network('capacity', capacity(BASE), T0 - 30)
        self.assertIsNone(watch.state['seededAt'])
        watch.on_network('stats', stats({'gpt-oss-20b': 2}), T0 - 20)
        watch.on_network('capacity', capacity(BASE), T0)
        self.assertEqual(watch.state['seededAt'], T0)
        # Without /v1/stats, capacity alone seeds after five minutes.
        memory = History(':memory:')
        self.addCleanup(memory.close)
        other = mcw.ModelCatalogWatch(memory)
        other.on_network('capacity', capacity(BASE), T0)
        other.on_network('capacity', capacity(BASE), T0 + 270)
        self.assertIsNone(other.state['seededAt'])
        other.on_network('capacity', capacity(BASE), T0 + 300)
        self.assertEqual(other.state['seededAt'], T0 + 300)
        self.assertEqual(set(watch.state['known']), set(BASE))
        self.run_minutes(watch, T0 + 30, 30, BASE)
        self.assertEqual(self.events(), [])
        again = self.make()
        self.assertEqual(set(again.state['known']), set(BASE))
        self.assertEqual(again.state['seededAt'], T0)
        saved = json.dumps(self.history.cache(mcw.KEY))
        self.assertNotIn('pgpt', saved)  # no provider ids anywhere
        with self.history.lock:
            rows = self.history.db.execute('SELECT * FROM model_catalog_events').fetchall()
        self.assertNotIn('pgpt', json.dumps([list(r) for r in rows]))

    def test_new_model_needs_three_macs_for_ten_minutes(self):
        watch = self.seeded(evidence=FakeEvidence({'new-model': (0.12, 0.05)}))
        t = T0 + 30
        # Churn: one or two Macs come and go for hours (gemma-4-26b's pattern).
        for n in (1, 2, 1, 2):
            t = self.run_minutes(watch, t, 20, {**BASE, 'new-model': (n, 0)})
            t = self.run_minutes(watch, t, 10, BASE)
        self.assertEqual(self.events(), [])
        # Three Macs, but for less than ten minutes.
        t = self.run_minutes(watch, t, 9, {**BASE, 'new-model': (3, 1)})
        t = self.run_minutes(watch, t, 5, {**BASE, 'new-model': (2, 1)})
        self.assertEqual(self.events(), [])
        t = self.run_minutes(watch, t, 11, {**BASE, 'new-model': (4, 3, 5)})
        [(at, kind, model, detail)] = self.events()
        self.assertEqual((kind, model), ('new', 'new-model'))
        self.assertEqual(
            {k: detail[k] for k in ('macs', 'warm', 'demand', 'returned', 'usdPerHour', 'cell')},
            {
                'macs': 4,
                'warm': 3,
                'demand': 5,
                'returned': False,
                'usdPerHour': 0.12,
                'cell': 'M5 Pro|48',
            },
        )
        self.assertEqual(at - detail['since'], 600)
        self.run_minutes(watch, t, 60, {**BASE, 'new-model': (40, 30)})
        self.assertEqual(len(self.events('new')), 1)

    def test_new_model_seen_only_among_serving_macs(self):
        watch = self.seeded()
        t = T0 + 30
        self.run_minutes(watch, t, 12, BASE, stats_payload=stats({'fresh-model': 3}))
        [(_, kind, model, detail)] = self.events()
        self.assertEqual(
            (kind, model, detail['serving'], detail['routable']), ('new', 'fresh-model', 3, None)
        )

    def test_dropped_model_after_ten_minutes_but_not_a_brief_gap_or_a_small_one(self):
        watch = self.seeded()
        both = {**BASE, 'small': (2, 1)}
        t = self.run_minutes(watch, T0 + 30, 5, both)
        watch.state['known']['small'] = {'first': T0, 'macs': 2, 'cap': True}
        t = self.run_minutes(watch, t, 5, both)
        # One missing poll (Sep 20 19:45) then back.
        without = {m: v for m, v in both.items() if m != 'Qwen3.5-9B'}
        watch.on_network('capacity', capacity(without), t)
        t = self.run_minutes(watch, t + 30, 10, both)
        self.assertEqual(self.events(), [])
        # Gone for good; Macs keep serving it for a while, which doesn't count.
        still = stats({'Qwen3.5-9B': 50})
        two = {m: v for m, v in BASE.items() if m != 'Qwen3.5-9B'}
        t = self.run_minutes(watch, t, 9, two, stats_payload=still)
        self.assertEqual(self.events(), [])
        t = self.run_minutes(watch, t, 2, two, stats_payload=still)
        [(_, kind, model, detail)] = self.events()
        self.assertEqual((kind, model, detail['macs']), ('left', 'Qwen3.5-9B', 200))
        # 'small' (2 Macs) vanished too, without news.
        self.assertIn('small', watch.state['known'])
        self.assertNotIn('Qwen3.5-9B', watch.state['known'])

    def test_a_return_is_news_again(self):
        watch = self.seeded()
        rest = {m: v for m, v in BASE.items() if m != 'Qwen3.5-9B'}
        t = self.run_minutes(watch, T0 + 30, 15, rest)
        t = self.run_minutes(watch, t, 15, BASE)
        kinds = [(k, m, d.get('returned')) for _, k, m, d in self.events()]
        self.assertEqual(kinds, [('left', 'Qwen3.5-9B', None), ('new', 'Qwen3.5-9B', True)])
        self.assertEqual(self.make().state['left'], {})

    def test_a_partial_list_or_a_fresh_catalog_listing_is_not_a_removal(self):
        catalog = [{'id': m, 'active': True} for m in BASE]
        # The optimizer's copy, refreshed during the test (older than 30 minutes is ignored).
        watch = self.seeded(catalog=lambda: (catalog, T0 + 2000))
        one = {'gpt-oss-20b': (500, 300)}
        t = self.run_minutes(watch, T0 + 30, 30, one)  # 2 of 3 missing: a partial list
        self.assertEqual(self.events(), [])
        rest = {m: v for m, v in BASE.items() if m != 'Qwen3.5-9B'}
        t = self.run_minutes(watch, t, 20, rest)  # still active in the fresh catalog
        self.assertEqual(self.events(), [])
        catalog[2]['active'] = False
        self.run_minutes(watch, t, 1, rest)
        self.assertEqual([(k, m) for _, k, m, _ in self.events()], [('left', 'Qwen3.5-9B')])

    def test_a_stale_or_missing_catalog_copy_confirms_no_removal(self):
        # Missing from the list alone is not a removal: the catalog must confirm it.
        catalog = [{'id': m, 'active': True} for m in BASE if m != 'Qwen3.5-9B']
        rest = {m: v for m, v in BASE.items() if m != 'Qwen3.5-9B'}
        for source in (lambda: (catalog, T0 - 3600), None, lambda: ([], T0 + 600)):
            with self.subTest(source=source):
                watch = self.seeded(catalog=source)
                self.run_minutes(watch, T0 + 30, 30, rest)
                self.assertEqual(self.events(), [])

    def test_malformed_payloads_change_nothing(self):
        watch = self.seeded()
        for bad in (
            None,
            {},
            {'models': None},
            {'models': [{'id': 'x y'}]},
            {'models': [{'id': 'm', 'routable_providers': -1, 'warm_providers': 1}]},
        ):
            watch.on_network('capacity', bad, T0 + 60)
        watch.on_network('stats', {'providers': 'no'}, T0 + 60)
        watch.on_network('series', {}, T0 + 60)
        self.assertEqual(self.events(), [])
        self.assertEqual(set(watch.state['known']), set(BASE))


class ReplayTests(Base):
    """Real capacity windows from BloomGauge's history (model_catalog_fixture.json)."""

    def replay(self, name):
        doc = json.loads(FIXTURE.read_text())
        window = next(w for w in doc['windows'] if w['name'] == name)
        watch = self.make()
        watch.on_network('stats', stats(), window['start'] - 1)
        for row in window['rows']:
            models = {m: tuple(v) for m, v in zip(window['models'], row[1:]) if v}
            watch.on_network('capacity', capacity(models), window['start'] + row[0])
        return [(k, m) for _, k, m, _ in self.events()]

    def test_sep17_new_model_then_the_real_removal(self):
        self.assertEqual(
            self.replay('sep17-arrival-and-removal'),
            [
                ('new', 'qwen3.8-flash-next'),
                ('left', 'qwen3-vl-30b-a3b-instruct'),
                ('left', 'qwen3.8-flash-next'),
            ],
        )

    def test_sep20_one_poll_gap_is_quiet(self):
        self.assertEqual(self.replay('sep20-partial-list'), [])

    def test_sep27_gemma_flicker_is_quiet(self):
        self.assertEqual(self.replay('sep27-gemma-flicker'), [])


class SwingTests(Base):
    def steady(self, watch, start, hours, models, noise=0.2):
        """Warm counts wobbling +-noise around their level (normal churn)."""
        t, i = start, 0
        while t < start + hours * 3600:
            f = 1 + noise * (1 if i % 7 < 3 else -1) * ((i % 5) / 4)
            watch.on_network(
                'capacity',
                capacity({m: (r, max(0, round(w * f))) for m, (r, w) in models.items()}),
                t,
            )
            t += 30
            i += 1
        return t

    def test_normal_churn_is_quiet_and_a_halving_is_one_collapse(self):
        watch = self.seeded()
        t = self.steady(watch, T0 + 30, 4, BASE)
        self.assertEqual(self.events(), [])
        lower = {**BASE, 'Qwen3.5-9B': (200, 40)}
        t = self.steady(watch, t, 1, lower, noise=0)
        [(at, kind, model, detail)] = self.events()
        self.assertEqual((kind, model), ('collapse', 'Qwen3.5-9B'))
        self.assertGreater(detail['before'], 100)
        self.assertEqual((detail['after'], detail['minutes']), (40, 15))
        # While it stays low there is no second collapse.
        self.steady(watch, t, 2, lower, noise=0)
        self.assertEqual(len(self.events()), 1)

    def test_a_third_down_or_up_is_ordinary(self):
        # 0.7x / 1.4x would have fired 60 times over Sep 6-28 (10 on Sep 26-28).
        watch = self.seeded()
        t = self.steady(watch, T0 + 30, 3.5, BASE, noise=0)
        t = self.steady(watch, t, 3.5, {**BASE, 'Qwen3.5-9B': (200, 78)}, noise=0)
        self.steady(watch, t, 1, {**BASE, 'Qwen3.5-9B': (200, 115)}, noise=0)
        self.assertEqual(self.events(), [])

    def test_a_doubling_is_a_surge(self):
        watch = self.seeded()
        t = self.steady(watch, T0 + 30, 3.5, BASE, noise=0)
        self.steady(watch, t, 0.5, {**BASE, 'Qwen3.5-9B': (300, 260)}, noise=0)
        self.assertEqual([(k, m) for _, k, m, _ in self.events()], [('surge', 'Qwen3.5-9B')])

    def test_a_network_wide_drop_or_a_small_model_is_not_model_news(self):
        small = {**BASE, 'tiny': (12, 8)}
        watch = self.seeded(small)
        t = self.steady(watch, T0 + 30, 3.5, small, noise=0)
        half = {m: (r, w // 2) for m, (r, w) in small.items()}
        half['tiny'] = (12, 1)  # 8 -> 1: below MIN_CHANGE_MACS
        self.steady(watch, t, 1, half, noise=0)
        self.assertEqual(self.events(), [])


class PushTests(Base):
    def setUp(self):
        super().setUp()
        self.sent = []
        self.ok = True

    def notify(self, key, title, body):
        self.sent.append((key, title, body))
        return self.ok

    def arrived(self, evidence, **kwargs):
        watch = self.seeded(evidence=evidence, notify=self.notify, **kwargs)
        self.run_minutes(watch, T0 + 30, 11, {**BASE, 'new-model': (8, 6)})
        self.assertEqual(len(self.events('new')), 1)
        return watch

    def test_off_by_default(self):
        watch = self.arrived(FakeEvidence({'new-model': (0.2, 0.1), 'gpt-oss-20b': (0.1, 0.05)}))
        self.assertFalse(watch.push_status(T0)['enabled'])
        self.assertIsNone(watch.maybe_push(T0 + 3600))
        self.assertEqual(self.sent, [])

    def test_one_a_day_only_when_it_pays_well_and_mute(self):
        evidence = FakeEvidence(
            {'new-model': (0.08, 0.02), 'gpt-oss-20b': (0.1, 0.05)}, served='gpt-oss-20b'
        )
        watch = self.arrived(evidence)
        watch.set_push({'action': 'push', 'enabled': True}, T0 + 700)
        self.assertIsNone(watch.maybe_push(T0 + 1000))  # 0.8x what this Mac serves
        evidence.rows['new-model'] = (0.45, 0.18)
        self.assertIsNone(watch.maybe_push(T0 + 1100))  # checked under 5 minutes ago
        self.assertEqual(watch.maybe_push(T0 + 1400), 'new-model')
        [(key, title, body)] = self.sent
        self.assertTrue(key.startswith('catalog-new-new-model-'))
        self.assertIn('$0.45/h', body)
        self.assertIn('4.5x what gpt-oss-20b makes', body)
        self.assertLessEqual(len(title), 60)
        self.assertLessEqual(len(body), 240)
        # The same model never again; another model not within a day.
        self.run_minutes(watch, T0 + 1500, 11, {**BASE, 'new-model': (8, 6), 'other': (9, 9)})
        evidence.rows['other'] = (0.5, 0.2)
        self.assertIsNone(watch.maybe_push(T0 + 5000))
        self.assertEqual(watch.maybe_push(T0 + 1400 + 86400), 'other')
        # Muted for a week.
        watch.set_push({'action': 'mute'}, T0 + 2 * 86400)
        self.assertGreater(watch.push_status(T0 + 2 * 86400)['mutedUntil'], T0 + 8 * 86400)
        state = self.make().push_status(T0 + 2 * 86400)
        self.assertTrue(state['enabled'])
        self.assertEqual(state['lastSentAt'], T0 + 1400 + 86400)
        self.assertIsNone(watch.set_push({'action': 'unmute'}, T0 + 3 * 86400)['mutedUntil'])

    def test_without_a_served_model_it_compares_with_the_median(self):
        evidence = FakeEvidence(
            {'new-model': (0.45, 0.18), 'a': (0.1, 0.05), 'b': (0.2, 0.1), 'c': (0.05, 0.01)}
        )
        watch = self.arrived(evidence)
        watch.set_push({'action': 'push', 'enabled': True}, T0 + 700)
        self.assertEqual(watch.maybe_push(T0 + 1000), 'new-model')
        self.assertIn('typical model', self.sent[0][2])

    def test_no_confident_estimate_no_push_and_failed_delivery_retries(self):
        evidence = FakeEvidence({'new-model': (0.3, 0.0), 'gpt-oss-20b': (0.1, 0.05)})
        watch = self.arrived(evidence)
        watch.set_push({'action': 'push', 'enabled': True}, T0 + 700)
        self.assertIsNone(watch.maybe_push(T0 + 1000))  # lower bound 0: not confident
        evidence.rows['new-model'] = (0.5, 0.1)
        self.ok = False
        self.assertIsNone(watch.maybe_push(T0 + 1400))
        self.ok = True
        self.assertEqual(watch.maybe_push(T0 + 1800), 'new-model')

    def test_settings_are_strict(self):
        watch = self.make()
        for bad in (
            None,
            [],
            {'action': 'push'},
            {'action': 'push', 'enabled': 'yes'},
            {'action': 'push', 'enabled': True, 'x': 1},
            {'action': 'mute', 'days': 30},
            {'action': 'other'},
        ):
            with self.assertRaises(ValueError):
                watch.set_push(bad, T0)


class ViewTests(Base):
    def test_items_flags_and_live_estimates(self):
        mac = {'offered': ['old-a'], 'local': ['old-a', 'old-b']}
        evidence = FakeEvidence({'new-model': (0.11, 0.04)})
        watch = self.seeded(evidence=evidence, this_mac=lambda: (mac['offered'], mac['local']))
        for m in ('old-a', 'old-b', 'old-c'):
            watch.state['known'][m] = {'first': T0, 'macs': 20, 'cap': True}
        models = {**BASE, 'old-a': (20, 5), 'old-b': (20, 5), 'old-c': (20, 5)}
        t = self.run_minutes(watch, T0 + 30, 2, models)
        t = self.run_minutes(watch, t, 12, BASE)
        t = self.run_minutes(watch, t, 12, {**BASE, 'new-model': (5, 4, 2)})
        view = watch.view(t + 60)
        self.assertEqual(view['watchingSince'], T0)
        self.assertFalse(view['push']['enabled'])
        items = {i['model']: i for i in view['items']}
        self.assertEqual(view['items'][0]['model'], 'new-model')  # newest first
        self.assertEqual(
            {m: items[m]['here'] for m in ('old-a', 'old-b', 'old-c')},
            {'old-a': 'offered', 'old-b': 'downloaded', 'old-c': None},
        )
        new = items['new-model']
        self.assertEqual(
            (new['kind'], new['warm'], new['macs'], new['current'], new['usdPerHour']),
            ('new', 4, 5, True, 0.11),
        )
        # A week later the row keeps its first-seen counts, without a live estimate.
        later = {i['model']: i for i in watch.view(t + 8 * 86400)['items']}['new-model']
        self.assertFalse(later['current'])
        # A model that came back is not flagged as dropped any more.
        self.run_minutes(watch, t, 12, {**BASE, 'old-a': (9, 9)})
        items = {}
        for i in watch.view(t + 1000)['items']:
            items.setdefault(i['model'], i)
        self.assertEqual(items['old-a']['kind'], 'new')
        dropped = [
            i for i in watch.view(t + 1000)['items'] if (i['model'], i['kind']) == ('old-a', 'left')
        ]
        self.assertIsNone(dropped[0]['here'])

    def test_events_are_kept_ninety_days(self):
        watch = self.make()
        watch._record(T0 - 100 * 86400, 'new', 'a', {})
        watch._record(T0 - 10 * 86400, 'new', 'b', {})
        self.assertEqual(retention.prune(self.history, T0, pause=0), {'model_catalog_events': 1})
        self.assertEqual([m for _, _, m, _ in self.events()], ['b'])

    def test_optimizer_sources_read_the_existing_copies(self):
        optimizer = SimpleNamespace(
            lock=threading.RLock(),
            catalog=[{'id': 'a', 'active': True}],
            discovery_at=T0,
            raw={'advertised_models': ['a', 'b']},
            local=[{'id': 'a'}, {'id': 'c'}, 'junk'],
        )
        catalog, this_mac = mcw.optimizer_sources(optimizer)
        self.assertEqual(catalog(), ([{'id': 'a', 'active': True}], T0))
        self.assertEqual(this_mac(), (['a', 'b'], ['a', 'c']))


class NewsHTTPTests(unittest.TestCase):
    read = test_remote.RemoteHTTPTests.read
    denied = test_remote.RemoteHTTPTests.denied

    def setUp(self):
        test_remote.RemoteHTTPTests.setUp(self)
        self.c = self.local.RequestHandlerClass.collector
        self.write = {'Content-Type': 'application/json', 'X-Bloom-Action': 'network-news'}

    def tearDown(self):
        self.c.close()
        test_remote.RemoteHTTPTests.tearDown(self)
        self.c.history.close()

    def test_news_and_push_setting_on_mac_and_phone(self):
        self.assertIn(self.c.catalog_watch.on_network, self.c.network.listeners)
        with self.read(self.local, '/api/network/news') as response:
            view = json.load(response)
        self.assertEqual((view['items'], view['push']['enabled']), ([], False))
        body = json.dumps({'action': 'push', 'enabled': True}).encode()
        self.denied(self.local, '/api/network/news', {'Content-Type': 'application/json'}, body)
        self.denied(self.phone, '/api/network/news', {**self.headers, **self.write}, body)
        phone = {**self.headers, **self.write, 'Origin': 'https://' + self.headers['Host']}
        with self.read(self.phone, '/api/network/news', phone, body) as response:
            self.assertTrue(json.load(response)['enabled'])
        with self.read(self.phone, '/api/network/news', self.headers) as response:
            self.assertTrue(json.load(response)['push']['enabled'])


if __name__ == '__main__':
    unittest.main()
