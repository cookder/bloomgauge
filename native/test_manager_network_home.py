"""Starting home from Macs like yours (roadmap item 3): the network pick for a Mac without its own
history, the notice and "Keep current" before a change, the handover to own history, and the
fallback when public evidence is thin or stale."""

import copy
import unittest
import uuid
from unittest.mock import Mock, patch

import manager
import test_manager as tm
from demand_optimizer import policy
from demand_targets import GEMMA
from network_evidence import NetworkEvidence
from optimizer import Optimizer
from test_demand_optimizer import NOW, decision as legacy_upgrade
from test_network_evidence import GPT, HOUR, PRICING, Base

CELL = 'M5 Pro|48'
QWEN = 'qwen3.6-35b-a3b-vl-mtp-mxfp8'


def est(usd, low=None, providers=6, windows=288, hours=24, source='cell', dedicated=True):
    """A NetworkEvidence estimate as dedicated_table returns it (only the fields used)."""
    return {
        'usd_per_h': usd,
        'low': usd * 0.4 if low is None else low,
        'providers': providers,
        'windows': windows,
        'hours_seen': hours,
        'source': source,
        'dedicated_only': dedicated,
    }


def net(models, neighbours=None, updated=NOW - 120, cell=CELL):
    return {'cell': cell, 'updatedAt': updated, 'models': models, 'neighbours': neighbours or {}}


def own(model, rate, hours=10, end=NOW):
    return {model: {'minutes': tm.minutes(rate, hours, end)}}


# Sep 27-28 on an M5 Pro 48 (poll copy by day + this app's rows by night): dedicated gemma about
# $0.048/h over 24 h, a gpt-oss box about $0.011/h.
MODELS = {GEMMA: est(0.048, 0.010), GPT: est(0.011, 0.001, providers=5)}
ALLOWED = {GEMMA, GPT, QWEN}


def home(network='fresh', current=GPT, m=None, earned=None, allowed=ALLOWED, now=NOW):
    network = net(MODELS, updated=now - 120) if network == 'fresh' else network
    return manager.choose_home(
        m or {}, earned or {}, lambda: [], current, set(allowed), now, 0, network
    )


class NetworkPickTests(unittest.TestCase):
    def test_new_user_with_thin_or_stale_evidence_keeps_the_current_model(self):
        current = {'model': GPT, 'source': 'current', 'at': NOW}
        self.assertEqual(home(None), current)  # no network evidence yet
        cases = {
            'stale': net({GEMMA: est(0.048)}, updated=NOW - 3601),
            'never updated': net({GEMMA: est(0.048)}, updated=None),
            'four providers': net({GEMMA: est(0.048, providers=4)}),
            'under two hours of windows': net({GEMMA: est(0.048, windows=23)}),
            'no lower bound': net({GEMMA: est(0.048, low=0.0)}),
            'neighbour cell': net({GEMMA: est(0.048, source='neighbour')}),
            'mixed boxes': net({GEMMA: est(0.048, dedicated=False)}),
        }
        for name, network in cases.items():
            with self.subTest(name):
                self.assertEqual(home(network, earned=own(GPT, 0.005)), current)

    def test_pick_is_the_best_lower_bound_among_allowed_unblocked_solo_models(self):
        pair = manager.selection_key([QWEN, GPT])
        network = net(
            {
                GEMMA: est(0.048, 0.010),
                GPT: est(0.011, 0.001),
                QWEN: est(0.09, 0.03),  # better, but not downloaded/allowed here
                pair: est(0.2, 0.1),  # pairs never
                'blocked': est(0.2, 0.1),  # failed to load: held back
            }
        )
        m = {'blocked': {'blocked': NOW + 3600}}
        got = home(network, GEMMA, m, allowed={GEMMA, GPT, pair, 'blocked'})
        self.assertEqual(
            got,
            {
                'model': GEMMA,
                'source': 'network',
                'at': NOW,
                'usdPerHour': 0.048,
                'low': 0.010,
                'providers': 6,
                'cell': CELL,
            },
        )
        # Without gemma allowed the pick is gpt-oss, and labels it where it already serves.
        self.assertEqual(home(network, GPT, m, allowed={GPT, 'blocked'})['source'], 'network')

    def test_a_better_model_next_door_without_cell_evidence_leaves_no_pick(self):
        # M3 Ultra 96, Sep 27-28: qwen3.6 $0.039 [0.007] in the cell; gemma only at 256-512 GB.
        network = net({QWEN: est(0.039, 0.007)}, {GEMMA: est(0.095, 0.029, source='neighbour')})
        self.assertEqual(home(network, GEMMA)['source'], 'current')
        self.assertEqual(home(network, QWEN, allowed={QWEN})['source'], 'network')

    def test_change_away_from_the_serving_model_is_announced_then_made(self):
        earned = own(GPT, 0.011)
        first = home(earned=earned)
        notice = first.pop('notice')
        self.assertEqual(first, {'model': GPT, 'source': 'current', 'at': NOW})
        self.assertAlmostEqual(notice['ownUsdPerHour'], 0.011)
        self.assertEqual(
            {**notice, 'ownUsdPerHour': 0.011},
            {
                'model': GEMMA,
                'from': GPT,
                'at': NOW,
                'until': NOW + manager.NETWORK_HOME_NOTICE_SECONDS,
                'usdPerHour': 0.048,
                'low': 0.010,
                'currentUsdPerHour': 0.011,
                'ownUsdPerHour': 0.011,
                'providers': 6,
                'cell': CELL,
            },
        )
        self.assertEqual(manager.NETWORK_HOME_NOTICE_SECONDS, 2 * 3600)
        m = {'home': first, 'homeNotice': notice}
        later = home(m=m, earned=earned, now=NOW + 3600)
        self.assertEqual((later['model'], later['notice']['until']), (GPT, notice['until']))
        due = home(m=m, earned=earned, now=notice['until'])
        self.assertEqual((due['model'], due['source']), (GEMMA, 'network'))
        self.assertNotIn('notice', due)
        # Saved as home, it stays home without a notice, even away from it (a failed return).
        m = {'home': {**due}}
        self.assertEqual(home(m=m, earned=earned, now=NOW + 9000)['model'], GEMMA)

    def test_no_change_without_a_day_of_evidence_or_while_this_mac_earns_well(self):
        cases = {
            'under 20 of 24 hours': (net({GEMMA: est(0.048, 0.01, hours=19)}), own(GPT, 0.005)),
            'earning half the pick here': (net({GEMMA: est(0.048, 0.01)}), own(GPT, 0.024)),
            'Macs like yours earn half': (
                net({GEMMA: est(0.048, 0.01), GPT: est(0.024, 0.001)}),
                own(GPT, 0.001),
            ),
            'nothing known about the serving model': (net({GEMMA: est(0.048, 0.01)}), {}),
            'under an hour served': (net({GEMMA: est(0.048, 0.01)}), own(GPT, 0.001, 0.9)),
        }
        for name, (network, earned) in cases.items():
            with self.subTest(name):
                got = home(network, earned=earned)
                self.assertEqual((got['model'], got['source']), (GPT, 'current'))
                self.assertNotIn('notice', got)
        # Just under half on this Mac and on Macs like it: announced.
        got = home(net({GEMMA: est(0.048, 0.01), GPT: est(0.0239)}), earned=own(GPT, 0.0239))
        self.assertEqual(got['notice']['model'], GEMMA)

    def test_a_different_pick_or_serving_model_restarts_the_notice(self):
        old = {'model': QWEN, 'from': GPT, 'at': NOW - 7000, 'until': NOW + 200}
        got = home(m={'homeNotice': old}, earned=own(GPT, 0.005))
        self.assertEqual((got['notice']['model'], got['notice']['at']), (GEMMA, NOW))
        old = {**old, 'model': GEMMA, 'from': 'Qwen3.5-9B'}
        got = home(m={'homeNotice': old}, earned=own(GPT, 0.005))
        self.assertEqual((got['notice']['from'], got['notice']['until']), (GPT, NOW + 7200))

    def test_pin_wins_and_own_history_takes_over_once_it_has_a_lower_bound(self):
        earned = own(GPT, 0.005)
        self.assertEqual(home(m={'home': manager.pin(GPT, NOW)}, earned=earned)['source'], 'manual')
        saved = {'model': GEMMA, 'source': 'network', 'at': NOW - 86400}
        # Three separate days with a ready hour on gpt-oss: its lower bound decides, as before.
        history = {GPT: {'minutes': tm.HomeTests.days([0.02, 0.022, 0.018], 4)}}
        got = home(m={'home': saved}, earned=history)
        self.assertEqual((got['model'], got['source']), (GPT, 'history'))
        # Two days are not enough: the network home stays.
        history = {GPT: {'minutes': tm.HomeTests.days([0.02, 0.022], 4)}}
        got = home(m={'home': saved}, earned=history, current=GEMMA)
        self.assertEqual(got['source'], 'network')

    def test_a_saved_network_home_stays_while_evidence_is_missing(self):
        saved = {'model': GEMMA, 'source': 'network', 'at': NOW - 600, 'usdPerHour': 0.048}
        self.assertEqual(home(None, GEMMA, {'home': saved}), saved)
        self.assertEqual(home(net({}, updated=NOW - 7200), GPT, {'home': saved}), saved)


class DecisionTests(unittest.TestCase):
    def context(self, home, current=GPT):
        return {
            'current': current,
            'home': home,
            'rows': {m: tm.row(m) for m in ALLOWED},
            'rules': policy(),
            'blocked': {},
        }

    def test_hold_says_why_and_no_excursion_starts_during_a_notice(self):
        state = {'mode': 'demand', 'demandPolicy': policy()}
        pending = home(earned=own(GPT, 0.005))
        with patch('manager.manager_excursion') as hook:
            d = manager.decide(legacy_upgrade(), state, self.context(pending), NOW)
        hook.assert_not_called()
        self.assertIsNone(d['target'])
        self.assertTrue(
            d['reason'].startswith('Holding home model %s. Switching to %s at ' % (GPT, GEMMA)),
            d['reason'],
        )
        self.assertEqual(d['manager']['homeNotice']['model'], GEMMA)
        self.assertNotIn('notice', d['manager']['home'])
        # The network home, once due: a return home (no confirmation), with its reason.
        due = home(m={'homeNotice': pending['notice']}, earned=own(GPT, 0.005), now=NOW + 7200)
        d = manager.decide(legacy_upgrade(), state, self.context(due), NOW + 7200)
        self.assertEqual((d['target'], d['kind']), (GEMMA, 'home'))
        self.assertEqual(
            d['reason'],
            'Moving to home model %s, chosen from Macs like yours: about $0.048/h on M5 Pro 48 GB '
            'Macs serving it alone.' % GEMMA,
        )
        self.assertIsNone(d['manager']['homeNotice'])
        d = manager.decide(legacy_upgrade(), state, self.context(due, GEMMA), NOW + 7200)
        held = 'Holding home model %s (chosen from Macs like yours' % GEMMA
        self.assertTrue(d['reason'].startswith(held), d['reason'])


class DedicatedTableTests(Base):
    def test_exact_cell_dedicated_boxes_only_with_neighbours_apart(self):
        now = HOUR + 1800
        self.network_row(HOUR)
        for hour in (HOUR - 3600, HOUR):
            self.put(hour, 'M5 Pro|48', GEMMA, providers=6, mean=300)
            self.put(hour, 'M5 Pro|48', GPT, providers=4, mean=500)  # too few here ...
            self.put(hour, 'M5 Pro|64', GPT, providers=3, mean=500)  # ... with the 64 GB boxes: 7
            self.put(hour, 'M5 Pro|48', QWEN, dedicated=0, providers=9, mean=900)  # mixed: never
        self.e.last_window = {'at': now - 60}
        t = self.e.dedicated_table([GEMMA, GPT, QWEN], 'M5 Pro|48', hours=24, now=now)
        self.assertEqual((t['cell'], t['hours'], t['updatedAt']), ('M5 Pro|48', 24, now - 60))
        self.assertEqual(list(t['models']), [GEMMA])
        g = t['models'][GEMMA]
        facts = (g['source'], g['dedicated_only'], g['providers'], g['hours_seen'])
        self.assertEqual(facts, ('cell', True, 6, 2))
        self.assertEqual(g['windows'], 24)
        self.assertEqual(list(t['neighbours']), [GPT])
        near = t['neighbours'][GPT]
        self.assertEqual((near['source'], near['providers']), ('neighbour', 7))
        same = self.e.estimate(GEMMA, 'M5 Pro|48', hours=24, now=now)
        self.assertEqual(g, {**same, 'hours_seen': 2})
        self.assertEqual(self.e.dedicated_table([GEMMA], None, now=now)['models'], {})  # no cell


class Flow(tm.Harness):
    """The whole path: public evidence -> decision -> notice saved -> Keep current or the move."""

    def setUp(self):
        super().setUp()
        self.o.state['manager'] = {}  # a new user: nothing learned yet
        self.o.state['models'] = ['a', 'b']
        self.ne = NetworkEvidence(
            self.h, lambda: set(), lambda: {'chip': 'Apple M5 Pro', 'memoryTotalGB': 48.0}
        )
        self.ne.set_pricing(PRICING)  # a and b at the fallback list price
        self.o.network_evidence = self.ne
        hour0 = int(self.now // 3600) * 3600
        with self.h.lock:
            for hour in range(hour0 - 23 * 3600, hour0 + 3600, 3600):
                for model, mean in (('a', 50.0), ('b', 500.0)):
                    self.h.db.execute(
                        'INSERT INTO network_cell_rates VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (hour, CELL, model, 1, 6, 12, 72, mean, mean, mean / 10, 0.0, None, 100.0),
                    )
                self.h.db.execute(
                    'INSERT INTO network_cell_rates VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (hour, '*', '*', 0, 500, 12, 12, 1e5, 1e5, 0.0, 0.0, 2000.0, 150.0),
                )
            self.h.db.commit()
        self.ne.last_window = {'at': self.now - 60}
        legacy = legacy_upgrade()
        legacy['currentModel'] = 'a'
        self.o.demand_auto.evaluate = Mock(return_value=legacy)
        self.o.demand_auto.evidence = Mock(return_value=(own('a', 0.004, 10, self.now), {}))
        self.provider_status = 'running'
        self.o.provider_control.inspect = Mock(side_effect=self.provider)
        self.control = self.o.automatic_control

    def provider(self):
        model, options, environment = self.o.read_options()
        return {
            'status': self.provider_status,
            'version': '0.9.10',
            'model': model,
            'options': options,
            'environment': environment,
            'raw': copy.deepcopy(self.o.raw),
            'disabled': False,
        }

    def decide(self, offset=0):
        now = self.now + offset
        d = Optimizer.demand_decision(self.o, now)
        self.o.last_demand_decision = copy.deepcopy(d)
        self.o.manager.remember(d, now)
        return d

    def keep(self, model='a', **changes):
        view = self.control.snapshot()
        data = {
            'action': 'keep-current',
            'requestId': str(uuid.uuid4()),
            'expectedControl': view['controlVersion'],
            'model': model,
            **changes,
        }
        return self.control.action(data)

    def test_notice_then_keep_current_pins_the_serving_model(self):
        d = self.decide()
        self.assertIsNone(d['target'])
        notice = self.o.state['manager']['homeNotice']
        self.assertEqual((notice['model'], notice['from']), ('b', 'a'))
        self.assertEqual(notice['until'], self.now + 7200)
        self.assertIn('Bloomkeeper will switch to b at', self.events('manager')[-1])
        summary = self.control.projection(self.now)['manager']
        shown = summary['homeNotice']
        self.assertEqual((shown['model'], shown['from']), ('b', 'a'))
        self.assertEqual(summary['home'], 'a')
        with self.assertRaisesRegex(ValueError, 'No switch away from b'):
            self.keep('b')
        with self.assertRaisesRegex(ValueError, 'takes the model'):
            self.keep(extra=1)
        result = self.keep()
        self.assertEqual(self.o.state['manager']['home']['model'], 'a')
        self.assertEqual(self.o.state['manager']['home']['source'], 'manual')
        self.assertNotIn('homeNotice', self.o.state['manager'])
        self.assertTrue(result['manager']['pinned'])
        self.assertIsNone(result['manager']['homeNotice'])
        self.assertIn('You kept a', self.events('manager')[-1])
        # The pin holds after the notice would have ended; no move, no new notice.
        self.ne.last_window = {'at': self.now + 7300 - 60}
        d = self.decide(7300)
        self.assertIsNone(d['target'])
        self.assertIsNone(d['manager']['homeNotice'])
        self.assertTrue(d['manager']['pinned'])

    def test_without_an_answer_the_move_follows_the_notice(self):
        self.decide()
        self.ne.last_window = {'at': self.now + 3600 - 60}
        self.assertIsNone(self.decide(3600)['target'])  # still announced
        self.assertEqual(self.o.state['manager']['homeNotice']['until'], self.now + 7200)
        self.ne.last_window = {'at': self.now + 7200 - 60}
        d = self.decide(7200)
        self.assertEqual((d['target'], d['kind']), ('b', 'home'))
        saved = self.o.state['manager']['home']
        self.assertEqual((saved['model'], saved['source'], saved['cell']), ('b', 'network', CELL))
        self.assertNotIn('homeNotice', self.o.state['manager'])
        changed = self.events('manager')[-1]
        self.assertIn('Home model is now b (chosen from Macs like yours', changed)

    def test_stale_network_evidence_falls_back_to_the_current_model(self):
        self.ne.last_window = {'at': self.now - 3700}
        d = self.decide()
        self.assertEqual(d['manager']['home']['source'], 'current')
        self.assertIsNone(d['manager']['homeNotice'])
        self.assertNotIn('homeNotice', self.o.state['manager'])
        self.o.network_evidence = None
        self.assertEqual(self.decide(60)['manager']['home']['source'], 'current')

    def test_turning_the_manager_off_drops_the_notice(self):
        self.decide()
        manager.release(self.o.state)
        self.assertNotIn('homeNotice', self.o.state['manager'])


if __name__ == '__main__':
    unittest.main()
