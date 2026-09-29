"""Evidence-armed excursions under the manager: arming, caps, exits, ledger and kill switch."""

import copy
import math
import statistics
import threading
import unittest
import uuid
from unittest.mock import Mock

import excursions as ex
import manager
import test_manager as tm
from demand_optimizer import policy
from demand_targets import GEMMA
from history import History
from model_combinations import selection_key
from network_evidence import NetworkEvidence
from optimizer import Optimizer, session_key
from optimizer_store import OptimizerStore
from test_demand_optimizer import NOW, decision as legacy_upgrade
from test_network_evidence import PRICING

QWEN = 'qwen3.5-35b-a3b'
BIG = 'Qwen3.8-27B-MTP'
HOME_RATE = 0.108


def row(model, available=True, after=40, required=30, selected=True):
    return {
        'id': model,
        'available': available,
        'selected': selected,
        'memoryGB': required * 0.7,
        'reason': None if available else 'Not active in the network catalog',
        'loadBudget': {'afterUnloadGB': after, 'requiredGB': required},
    }


def evidence(model=QWEN, ratio=2.4, low=1.3, latest=2.4, providers=6, source='cell', **kw):
    return {
        'model': model,
        'ratio': ratio,
        'low': low,
        'high': ratio * 1.5 if ratio else None,
        'latestRatio': latest,
        'providers': providers,
        'source': source,
        'dedicated': True,
        'priorSuccess': True,
        **kw,
    }


def context(
    current=GEMMA,
    candidates=None,
    rows=None,
    rules=None,
    source='history',
    blocked=None,
    active=None,
    **data,
):
    rows = rows or [row(GEMMA, required=22), row(QWEN), row(BIG)]
    return {
        'current': current,
        'home': {'model': GEMMA, 'source': source, 'at': NOW - 90000, 'usdPerHour': HOME_RATE},
        'rows': {r['id']: r for r in rows},
        'rules': rules or policy(),
        'blocked': blocked or {},
        'excursions': {
            'cell': 'M5 Pro|48',
            'updatedAt': NOW - 60,
            'homeUsdPerHour': HOME_RATE,
            'homeBasis': '72h',
            'homeNetwork': {
                'usdPerHour': 0.09, 'spread': 0.3, 'source': 'cell', 'providers': 9, 'dedicated': True
            },
            'memoryGB': 48,
            'today': 0,
            'lastSwitchAt': NOW - 7200,
            'environment': None,
            'candidates': [evidence()] if candidates is None else candidates,
            'active': active,
            'ledger': {},
            **data,
        },
    }


def state(**m):
    return {'mode': 'demand', 'demandPolicy': policy(), 'manager': m}


def decide(s, ctx, now):
    return manager.decide(legacy_upgrade(), s, ctx, now)


def persist(s, d):
    """What ManagerControl.remember does with the arming the pure decision produced."""
    arming = d['manager']['arming']
    m = s.setdefault('manager', {})
    if arming:
        m['arming'] = {k: arming[k] for k in ('model', 'since', 'checks', 'lastCheckAt')}
    else:
        m.pop('arming', None)


def watch(test, s, ctx_at, times):
    """Run decisions at `times`, persisting arming; returns the decisions."""
    out = []
    for t in times:
        d = decide(s, ctx_at(t), t)
        persist(s, d)
        out.append(d)
    return out


def rows_by_model(d):
    return {r['model']: r for r in d['manager']['evidence']['rows']}


class ArmingTests(unittest.TestCase):
    def test_a_move_needs_two_passing_checks_an_hour_apart(self):
        s = state()
        first, mid, second = watch(self, s, lambda t: context(), (NOW, NOW + 1800, NOW + 3600))
        self.assertIsNone(first['target'])
        self.assertEqual(
            {k: first['manager']['arming'][k] for k in ('model', 'checks', 'needed', 'since')},
            {'model': QWEN, 'checks': 1, 'needed': 2, 'since': NOW},
        )
        self.assertEqual(first['manager']['arming']['nextCheckAt'], NOW + 3600)
        self.assertEqual(
            (first['manager']['arming']['checkSeconds'], first['manager']['arming']['neededSeconds']),
            (3600, 3600),
        )
        self.assertEqual(first['manager']['arming']['ratio'], 2.4)
        self.assertIn('Public data favours %s; watching it (check 1 of 2' % QWEN, first['reason'])
        self.assertTrue(first['reason'].startswith('Holding home model ' + GEMMA))
        self.assertIsNone(mid['target'])
        self.assertEqual(mid['manager']['arming']['checks'], 1)
        self.assertEqual((second['target'], second['kind']), (QWEN, 'excursion'))
        self.assertEqual(second['manager']['action'], 'excursion')
        p = second['manager']['proposal']
        self.assertAlmostEqual(p['predictedUsdPerHour'], HOME_RATE * 2.4, places=4)
        self.assertEqual(p['maxMinutes'], 24 * 60)  # a runaway cap; evidence ends it
        self.assertEqual((p['evidence']['checks'], p['evidence']['homeUsdPerHour']), (2, 0.108))
        self.assertEqual(
            {k: p['evidence'][k] for k in ('homeNetworkSource', 'homeNetworkDedicated', 'memoryGB')},
            {'homeNetworkSource': 'cell', 'homeNetworkDedicated': True, 'memoryGB': 48},
        )
        self.assertIn('M5 Pro, 48 GB', second['reason'])
        self.assertIn('2.4x %s (90%% low 1.30x)' % GEMMA, second['reason'])
        view = rows_by_model(second)
        self.assertEqual(view[GEMMA]['why'], 'home')
        self.assertTrue(view[QWEN]['eligible'])
        self.assertIsNone(view[QWEN]['why'])
        self.assertAlmostEqual(view[QWEN]['usdPerHour'], HOME_RATE * 2.4, places=4)

    def test_a_failed_check_restarts_arming_and_a_stale_one_does_not_count(self):
        s = state()
        weak = context(candidates=[evidence(low=0.95)])
        d = watch(self, s, lambda t: context() if t != NOW + 3600 else weak, (NOW, NOW + 3600))
        self.assertIsNone(d[1]['target'])
        self.assertIsNone(d[1]['manager']['arming'])
        self.assertEqual(rows_by_model(d[1])[QWEN]['why'], 'weak evidence')
        d = watch(self, s, lambda t: context(), (NOW + 3700, NOW + 3700 + 3 * 3600))
        self.assertEqual(d[0]['manager']['arming']['checks'], 1)
        # Three hours later (asleep): the old check is void, this is check 1 again.
        self.assertIsNone(d[1]['target'])
        self.assertEqual(d[1]['manager']['arming']['since'], NOW + 3700 + 3 * 3600)

    def test_between_checks_a_dip_does_not_reset_but_blocks_the_move(self):
        s = state()
        dip = context(candidates=[evidence(latest=1.05)])
        d = watch(self, s, lambda t: dip if t == NOW + 3600 else context(), (NOW, NOW + 1200))
        self.assertEqual(d[1]['manager']['arming']['checks'], 1)
        d = decide(s, dip, NOW + 3600)  # the second check fails: fading
        self.assertIsNone(d['target'])
        self.assertIsNone(d['manager']['arming'])
        self.assertEqual(rows_by_model(d)[QWEN]['why'], 'fading')

    def test_no_arming_without_five_providers_a_source_or_dedicated_gemma_evidence(self):
        self.assertEqual(ex.MIN_PROVIDERS, 5)
        cases = {
            'no evidence': evidence(source='none', ratio=None, low=None, providers=0),
            'four providers': evidence(providers=4),
            'mixed boxes': evidence(dedicated=False),
        }
        for name, candidate in cases.items():
            with self.subTest(name):
                s = state()
                d = watch(self, s, lambda t: context(candidates=[candidate]), (NOW, NOW + 3600))
                self.assertIsNone(d[1]['target'])
                self.assertIsNone(d[1]['manager']['arming'])
                why = rows_by_model(d[1])[QWEN]['why']
                self.assertEqual(why, 'mixed boxes' if name == 'mixed boxes' else 'no evidence')

    def test_gain_must_clear_home_and_the_amortized_round_trip_cost(self):
        s = state()
        dead = {QWEN: 179, GEMMA: 115}  # this Mac's command -> first paid job
        ctx = context(candidates=[evidence(ratio=1.45, low=1.1, latest=1.45)], deadSeconds=dead)
        r = rows_by_model(decide(s, ctx, NOW))[QWEN]
        self.assertEqual(r['why'], 'gain too small')
        self.assertAlmostEqual(r['gainUsdPerHour'], HOME_RATE * 0.45, places=4)
        # Out: 1.45 x 0.108 x (179 s + 25 min ramp); back: 0.108 x (115 s + 10 min ramp);
        # 2 x 1.1 base epochs; (4.7% + 1%) x 9 min dark.
        cost = (
            0.1566 * (179 / 60 + 25) / 60
            + 0.108 * (115 / 60 + 10) / 60
            + 2 * 1.1 * 16 / 8640
            + (0.047 + 0.01) * 9 / 60 * (0.108 + 16 / 720)
        )
        self.assertAlmostEqual(r['costUsd'], cost, places=4)
        # Home's own $/h binds here: max(1.0 x 0.108, cost / 2 + $0.01).
        self.assertAlmostEqual(r['needUsdPerHour'], max(HOME_RATE, cost / 2 + 0.01), places=4)
        self.assertAlmostEqual(r['needUsdPerHour'], HOME_RATE, places=4)
        # On a slow home the amortized round trip plus the $0.01 floor binds instead.
        self.assertAlmostEqual(ex.required_gain(0.01, 0.04), 0.04 / 2 + 0.01)
        # Without switch history both legs assume 5 minutes to the first paid job.
        plain = context(candidates=[evidence(ratio=1.45, low=1.1, latest=1.45)])
        r = rows_by_model(decide(s, plain, NOW))[QWEN]
        self.assertAlmostEqual(
            r['costUsd'] - cost,
            0.1566 * (300 - 179) / 3600 + 0.108 * (300 - 115) / 3600,
            places=4,
        )
        # A 1.85x regime (the old fixture) no longer clears a round trip from a $0.108 home.
        old = context(candidates=[evidence(ratio=1.85, low=1.25, latest=1.85)], deadSeconds=dead)
        r = rows_by_model(decide(s, old, NOW))[QWEN]
        self.assertEqual(r['why'], 'gain too small')

    def test_gain_bar_is_one_out_of_sample_error_of_the_1h_gain(self):
        """calibration-2026-09-28.md (a), public /v1/stats poll Sep 26-28: where a candidate looked
        better (1.1 <= ratio < 2), the next hour's gain missed its prediction by 0.96 x home $/h on
        average with a gemma home (paying no more than home in 51% of those hours) and 0.69 x with
        a gpt-oss home; in $/h by $0.011-0.031 on homes under $0.04/h. So the bar is home's own
        $/h, with a $0.01/h floor over the amortized round trip (was 0.5 x home and $0.04/h)."""
        bar = (ex.MIN_GAIN_SHARE, ex.TAU_USD_PER_HOUR, ex.AMORTIZE_HOURS)
        self.assertEqual(bar, (1.0, 0.01, 2))
        # Andrew's $0.108 gemma home: the gain must reach home's own rate (a ratio of 2).
        self.assertAlmostEqual(ex.required_gain(0.108, 0.10), 0.108)
        # A $0.015/h home with a $0.022 round trip needs $0.021/h (the measured error at that
        # level is $0.020/h), not the old max(0.0075, 0.011 + 0.04) = $0.051/h.
        self.assertAlmostEqual(ex.required_gain(0.015, 0.022), 0.021)

    def test_switch_cost_parameters(self):
        self.assertEqual(ex.failure_chance(GEMMA, 40, 48, False), 0.01)
        self.assertEqual(ex.failure_chance('gpt-oss-20b', 40, 48, False), 0.01)
        self.assertEqual(ex.failure_chance('Qwen3.5-9B', 40, 48, False), 0.01)
        self.assertEqual(ex.failure_chance(QWEN, 32, 48, False), 0.25)  # > 65% of RAM, never ran
        self.assertEqual(ex.failure_chance(QWEN, 32, 48, True), 0.047)
        self.assertEqual(ex.failure_chance(QWEN, 30, 48, False), 0.047)
        self.assertEqual(ex.floor_usd_per_month(48), 16)
        self.assertEqual(ex.floor_usd_per_month(36), 12)
        self.assertEqual(ex.floor_usd_per_month(128), 26)
        self.assertEqual((ex.ramp_minutes(GEMMA), ex.ramp_minutes(QWEN)), (10, 25))
        # Pay is judged after this Mac's command -> first paid job plus the ramp (>= 10 min).
        dead = {GEMMA: 115, 'gpt-oss-20b': 212, 'qwen3.6-35b-a3b-vl-mtp-mxfp8': 732}
        self.assertEqual(ex.ramp_seconds(GEMMA, dead), 115 + 600)
        self.assertEqual(ex.ramp_seconds('gpt-oss-20b', dead), 600)  # 212 + 300: the floor
        self.assertEqual(ex.ramp_seconds('qwen3.6-35b-a3b-vl-mtp-mxfp8', dead), 732 + 1500)
        self.assertEqual(ex.ramp_seconds(QWEN, dead), 300 + 1500)  # no history: 5 min
        self.assertEqual((ex.dead_seconds(QWEN, None), ex.dead_seconds(GEMMA, dead)), (300, 115))


class GateTests(unittest.TestCase):
    def armed(self, **m):
        return state(arming={'model': QWEN, 'since': NOW - 3600, 'checks': 2, 'lastCheckAt': NOW - 60}, **m)

    def test_armed_model_moves_only_when_every_gate_is_open(self):
        self.assertEqual(decide(self.armed(), context(), NOW)['target'], QWEN)
        for gate, ctx in (
            ('daily limit', context(today=3)),
            ('dwell', context(lastSwitchAt=NOW - 1200)),
            ('environment', context(environment='Model switching waits while the Mac is on battery power.')),
        ):
            with self.subTest(gate):
                d = decide(self.armed(), ctx, NOW)
                self.assertIsNone(d['target'])
                self.assertEqual(d['manager']['evidence']['gate'], gate)
                self.assertEqual(rows_by_model(d)[QWEN]['why'], gate)
                # A passing gate pauses arming; it does not reset it.
                self.assertEqual(d['manager']['arming']['checks'], 2)
        d = decide(self.armed(), context(environment='The Mac is hot.'), NOW)
        self.assertEqual(d['manager']['evidence']['environment'], 'The Mac is hot.')

    def test_setting_off_or_pinned_home_never_proposes_and_clears_arming(self):
        for name, ctx in (
            ('off', context(rules=policy({'managerExcursions': 0}))),
            ('pinned', context(source='manual')),
        ):
            with self.subTest(name):
                d = decide(self.armed(), ctx, NOW)
                self.assertIsNone(d['target'])
                self.assertIsNone(d['manager']['arming'])
                self.assertEqual(d['manager']['evidence']['gate'], name)
                self.assertEqual(rows_by_model(d)[QWEN]['why'], name)
        ledger = decide(self.armed(), context(rules=policy({'managerExcursions': 0})), NOW)
        self.assertEqual(ledger['manager']['ledger']['enabled'], False)
        self.assertEqual(
            ledger['manager']['ledger']['disabledReason'], 'Turned off in the optimizer settings.'
        )

    def test_never_a_gemma_pair_a_model_that_does_not_fit_or_one_not_selected(self):
        pair = selection_key([GEMMA, QWEN])
        rows = [
            row(GEMMA, required=22),
            row(QWEN, after=20),  # 30 GB needed, 20 available after unloading
            row(BIG, selected=False),
            row(pair),
            row('blocked-model'),
        ]
        candidates = [
            evidence(QWEN, ratio=3.0, low=2.0, latest=3.0),
            evidence(BIG, ratio=3.0, low=2.0, latest=3.0),
            evidence(pair, ratio=3.0, low=2.0, latest=3.0),
            evidence('blocked-model', ratio=3.0, low=2.0, latest=3.0),
        ]
        ctx = context(candidates=candidates, rows=rows, blocked={'blocked-model': NOW + 60})
        for model in (QWEN, BIG, pair, 'blocked-model'):
            s = state(arming={'model': model, 'since': NOW - 3600, 'checks': 2, 'lastCheckAt': NOW - 60})
            d = decide(s, ctx, NOW)
            self.assertIsNone(d['target'], model)
        why = rows_by_model(d)
        self.assertEqual(
            {m: why[m]['why'] for m in (QWEN, BIG, pair, 'blocked-model')},
            {QWEN: 'memory', BIG: 'not selected', pair: 'gemma pair', 'blocked-model': 'blocked'},
        )

    def test_evidence_view_is_small_and_home_first(self):
        candidates = [evidence('m%02d' % i, ratio=1 + i / 10) for i in range(15)]
        rows = [row(GEMMA)] + [row('m%02d' % i) for i in range(15)]
        d = decide(state(), context(candidates=candidates, rows=rows), NOW)
        view = d['manager']['evidence']
        self.assertEqual(len(view['rows']), ex.MAX_ROWS)
        self.assertEqual(view['rows'][0]['model'], GEMMA)
        self.assertEqual((view['cell'], view['home'], view['homeUsdPerHour']), ('M5 Pro|48', GEMMA, 0.108))
        self.assertEqual(d['manager']['ledger']['count'], 0)
        # Without evidence data the fields are null, not errors.
        plain = context()
        plain.pop('excursions')
        d = decide(state(), plain, NOW)
        self.assertEqual(
            (d['manager']['evidence'], d['manager']['arming'], d['manager']['ledger']),
            (None, None, None),
        )


def uncalibrated(ratio=3.0, low=0.8, trial=1.25, latest=3.0, **kw):
    """A model this Mac has not calibrated: ln 4 fails, the ln 2 trial bound passes."""
    return evidence(ratio=ratio, low=low, latest=latest, calibrated=False, trialLow=trial, **kw)


def reading(*ratios, load=10.0):
    """ExcursionData.demand: each of the last two hours' median load over its usual level."""
    return {
        'ratio': min(ratios),
        'hours': [{'ratio': r, 'load': load, 'usualLoad': load / r} for r in ratios],
    }


class TrialTests(unittest.TestCase):
    """Andrew, Sep 28: Macs that can hold large models try them when demand is higher than usual
    and sustained, then learn per model."""

    def test_trial_constants(self):
        from network_evidence import USD_ERROR_TRIAL

        self.assertAlmostEqual(USD_ERROR_TRIAL, math.log(2))
        self.assertEqual(
            (ex.TRIAL_DEMAND_RATIO, ex.TRIAL_DEMAND_HOURS, ex.TRIAL_EVERY_SECONDS),
            (1.5, 2, 7 * 86400),
        )

    def test_an_uncalibrated_model_that_fits_arms_on_its_trial_bound_with_sustained_demand(self):
        s = state()
        ctx = lambda t: context(candidates=[uncalibrated()], demand={QWEN: reading(2.0, 1.8)})
        first, second = watch(self, s, ctx, (NOW, NOW + 3600))
        self.assertIsNone(first['target'])
        self.assertEqual(first['manager']['arming']['checks'], 1)
        row = rows_by_model(first)[QWEN]
        self.assertEqual((row['why'], row['trial'], row['trialLow'], row['demandRatio']), (None, True, 1.25, 1.8))
        self.assertEqual((second['target'], second['kind']), (QWEN, 'excursion'))
        p = second['manager']['proposal']
        self.assertEqual(
            {k: p['evidence'][k] for k in ('trial', 'trialLow', 'demandRatio', 'lessonFactor')},
            {'trial': True, 'trialLow': 1.25, 'demandRatio': 1.8, 'lessonFactor': None},
        )
        self.assertAlmostEqual(p['predictedUsdPerHour'], HOME_RATE * 3.0, places=4)
        self.assertIn('a trial of a model this Mac has not served lately', second['reason'])
        self.assertIn('3.0x %s (90%% low 1.25x at the trial margin)' % GEMMA, second['reason'])
        self.assertIn('demand at 1.8x its usual level', second['reason'])
        # A normal pass needs no demand gate and is no trial.
        d = decide(state(), context(candidates=[evidence(calibrated=False, trialLow=1.9)]), NOW)
        self.assertEqual((rows_by_model(d)[QWEN]['why'], rows_by_model(d)[QWEN]['trial']), (None, False))

    def test_unsustained_demand_a_calibrated_model_one_that_does_not_fit_or_a_recent_try_stay_home(self):
        recent = [[QWEN, NOW - 3 * 86400, NOW - 3 * 86400 + 7200]]
        cases = {
            'one high hour': ({}, {QWEN: reading(2.0, 1.2)}, [uncalibrated()], None, 'demand not high enough'),
            'no reading': ({}, {}, [uncalibrated()], None, 'demand not high enough'),
            'under one request': ({}, {QWEN: reading(3.0, 3.0, load=0.5)}, [uncalibrated()], None, 'demand not high enough'),
            'calibrated': ({}, {QWEN: reading(2, 2)}, [evidence(ratio=3.0, low=0.8, latest=3.0, calibrated=True, trialLow=None)], None, 'weak evidence'),
            'trial bound too low': ({}, {QWEN: reading(2, 2)}, [uncalibrated(trial=0.95)], None, 'weak evidence'),
            'does not fit': ({}, {QWEN: reading(2, 2)}, [uncalibrated()], [row(GEMMA, required=22), row(QWEN, after=20)], 'memory'),
            'tried 3 days ago': ({'excursionWindows': recent}, {QWEN: reading(2, 2)}, [uncalibrated()], None, 'tried this week'),
        }
        for name, (m, demand, candidates, rows, why) in cases.items():
            with self.subTest(name):
                s = state(**copy.deepcopy(m))
                ctx = lambda t: context(candidates=candidates, rows=rows, demand=demand)
                d = watch(self, s, ctx, (NOW, NOW + 3600))
                self.assertIsNone(d[1]['target'])
                self.assertIsNone(d[1]['manager']['arming'])
                self.assertEqual(rows_by_model(d[1])[QWEN]['why'], why)
        # A week later it may try again.
        old = [[QWEN, NOW - 8 * 86400, NOW - 8 * 86400 + 7200]]
        d = decide(state(excursionWindows=old), context(candidates=[uncalibrated()], demand={QWEN: reading(2, 2)}), NOW)
        self.assertTrue(rows_by_model(d)[QWEN]['trial'])

    def test_demand_gate_needs_every_hour_at_one_and_a_half_times_usual(self):
        self.assertTrue(ex.demand_high(reading(1.5, 4.0)))
        self.assertFalse(ex.demand_high(reading(1.49, 4.0)))
        self.assertFalse(ex.demand_high(reading(2.0)))  # one hour is not both checks
        self.assertFalse(ex.demand_high({'ratio': None, 'hours': [{'ratio': None, 'load': 3}] * 2}))
        self.assertFalse(ex.demand_high(None))


def ledger_row(ended, realized=0.05, cf=0.108, code='faded', model=QWEN, started=None):
    return {
        'model': model,
        'from': GEMMA,
        'startedAt': ended - 3 * 3600 if started is None else started,
        'endedAt': ended,
        'realizedUsdPerHour': realized,
        'homeCounterfactualUsdPerHour': cf,
        'endCode': code,
    }


class LessonTests(unittest.TestCase):
    def test_a_losing_row_raises_the_bar_by_its_shortfall_up_to_x2_fading_over_fourteen_days(self):
        self.assertEqual((ex.LESSON_SECONDS, ex.LESSON_MAX), (14 * 86400, math.log(2)))
        end = NOW - 3600
        # Paid $0.05/h against $0.108/h: the shortfall (x2.16) is capped at one ledger noise, x2.
        self.assertAlmostEqual(ex.lessons([ledger_row(end)], end)[QWEN]['factor'], 2.0)
        self.assertAlmostEqual(ex.lessons([ledger_row(end)], end + 7 * 86400)[QWEN]['factor'], 2 ** 0.5)
        self.assertEqual(ex.lessons([ledger_row(end)], end + 14 * 86400), {})
        # A small shortfall moves the bar a little; an early exit always the full x2.
        self.assertAlmostEqual(ex.lessons([ledger_row(end, 0.09, 0.10)], end)[QWEN]['factor'], 0.10 / 0.09)
        self.assertAlmostEqual(ex.lessons([ledger_row(end, 0.2, 0.1, 'early-exit')], end)[QWEN]['factor'], 2.0)
        # No lesson from a win or from an excursion the user, an outside change or a failed load ended.
        for r in (
            ledger_row(end, 0.2, 0.1),
            ledger_row(end, code='manual'),
            ledger_row(end, code='external'),
            ledger_row(end, code='changed'),
            ledger_row(end, code='off'),
            ledger_row(end, cf=None),
        ):
            self.assertEqual(ex.lessons([r], end), {}, r['endCode'])
        # Losses add up; the latest one describes the lesson; other models are untouched.
        two = ex.lessons([ledger_row(end - 86400), ledger_row(end), ledger_row(end, model=BIG, realized=0.2)], end)
        self.assertEqual(set(two), {QWEN})
        self.assertAlmostEqual(two[QWEN]['factor'], 2 * 2 ** (1 - 1 / 14))
        self.assertEqual((two[QWEN]['endedAt'], two[QWEN]['until']), (end, end + 14 * 86400))

    def test_a_lesson_holds_back_a_model_that_would_pass_and_says_why(self):
        end = NOW - 3600
        taught = ex.lessons([ledger_row(end)], NOW)
        d = watch(self, state(), lambda t: context(lessons=taught), (NOW, NOW + 3600))
        self.assertIsNone(d[1]['target'])
        row = rows_by_model(d[1])[QWEN]
        self.assertEqual(row['why'], 'paid less before')
        self.assertAlmostEqual(row['lessonFactor'], 2 ** (1 - 1 / 24 / 14), places=3)
        self.assertEqual(row['ratio'], 2.4)  # the public figures are shown as they are
        # A small lesson (x1.1) only discounts the prediction: 2.4x still clears the bar.
        small = ex.lessons([ledger_row(NOW, 0.1, 0.11)], NOW)
        d = watch(self, state(), lambda t: context(lessons=small), (NOW, NOW + 3600))
        self.assertEqual(d[1]['target'], QWEN)
        p = d[1]['manager']['proposal']
        self.assertAlmostEqual(p['predictedUsdPerHour'], HOME_RATE * 2.4 / 1.1, places=3)
        self.assertAlmostEqual(p['evidence']['lessonFactor'], 1.1, places=2)
        # A trial too: after a loss, a 3x regime at the trial margin (low 1.25) is not enough.
        ctx = context(candidates=[uncalibrated()], demand={QWEN: reading(2, 2)}, lessons=taught)
        self.assertEqual(rows_by_model(decide(state(), ctx, NOW))[QWEN]['why'], 'paid less before')

    def test_the_evidence_view_names_each_lesson_in_plain_words(self):
        from datetime import datetime

        started = datetime(2026, 9, 28, 10, 0).timestamp()
        rows = [ledger_row(started + 3 * 3600, started=started)]
        now = started + 5 * 3600
        view = decide(state(), context(lessons=ex.lessons(rows, now)), now)['manager']['evidence']
        self.assertEqual(view['trialDemandRatio'], 1.5)
        (lesson,) = view['lessons']
        self.assertEqual(
            lesson['text'],
            '%s: tried Sep 28, paid less than %s; needs stronger evidence until Oct 12.' % (QWEN, GEMMA),
        )
        self.assertEqual(
            {k: lesson[k] for k in ('model', 'home', 'code', 'realizedUsdPerHour', 'homeUsdPerHour')},
            {'model': QWEN, 'home': GEMMA, 'code': 'faded', 'realizedUsdPerHour': 0.05, 'homeUsdPerHour': 0.108},
        )
        self.assertEqual((lesson['triedAt'], lesson['until']), (started, started + 3 * 3600 + 14 * 86400))
        early = [ledger_row(started + 3 * 3600, started=started, code='early-exit')]
        view = decide(state(), context(lessons=ex.lessons(early, now)), now)['manager']['evidence']
        self.assertIn('tried Sep 28, ended early, paying less than %s;' % GEMMA, view['lessons'][0]['text'])
        self.assertEqual(decide(state(), context(), now)['manager']['evidence']['lessons'], [])


def away(started, predicted=HOME_RATE * 2.4, **changes):
    excursion = {
        'target': QWEN,
        'from': GEMMA,
        'startedAt': started,
        'leftAt': started - 120,
        'predictedUsdPerHour': predicted,
        'reason': 'public data',
        'maxMinutes': 24 * 60,
        'evidence': {'homeUsdPerHour': HOME_RATE, 'homeNetworkUsdPerHour': 0.09},
        **changes,
    }
    return state(excursion=excursion)


def active(realized=0.19, latest=1.8, network=True, low=0.1, dedicated=True, high=None, ready=3600):
    """ExcursionData.active: pay per ready hour since the ramp (with its 90% upper bound) and
    what home would have paid meanwhile."""
    return {
        'model': QWEN,
        'realizedUsdPerHour': realized,
        'realizedHigh': realized if high is None else high,
        'realizedSeconds': ready + 300,
        'readySeconds': ready,
        'network': network,
        'latestRatio': latest,
        'latestDedicated': dedicated,
        'home': {'usdPerHour': HOME_RATE, 'low': low, 'high': 0.117, 'basis': 'network-scaled'},
    }


class EndTests(unittest.TestCase):
    def end(self, s, act, now):
        return decide(s, context(current=QWEN, active=act), now)

    def test_early_exit_after_an_hour_below_the_home_counterfactual_and_a_day_hold(self):
        s = away(NOW - 3000)
        held = self.end(s, active(realized=0.05), NOW)
        self.assertIsNone(held['target'])  # 50 min: too early
        self.assertEqual(held['manager']['excursion']['realizedUsdPerHour'], 0.05)  # running figure
        self.assertNotIn('realizedUsdPerHour', s['manager']['excursion'])  # the input is untouched
        d = self.end(s, active(realized=0.05), NOW + 600)
        self.assertEqual((d['target'], d['kind'], d['manager']['action']), (GEMMA, 'home', 'end-excursion'))
        self.assertEqual(d['manager']['excursionEnd']['code'], 'early-exit')
        self.assertIn(
            'it paid $0.050/h after warming up; home would likely have paid at least $0.100/h',
            d['reason'],
        )
        # Judged against home's lower bound ($0.100/h), not half the prediction ($0.130/h):
        # $0.11/h stays although the old rule ended it; just under the bound goes.
        self.assertIsNone(self.end(s, active(realized=0.11), NOW + 600)['target'])
        self.assertEqual(self.end(s, active(realized=0.099), NOW + 600)['target'], GEMMA)
        self.assertIsNone(self.end(s, active(realized=0.05, low=0.04), NOW + 600)['target'])
        # Realized pay's own noise counts: its 90% upper bound must be below home's lower one.
        self.assertIsNone(self.end(s, active(realized=0.05, high=0.12), NOW + 600)['target'])
        self.assertEqual(self.end(s, active(realized=0.05, high=0.099), NOW + 600)['target'], GEMMA)
        # An hour of data after the ramp, not an hour since the switch (qwen3.6: ~11 min).
        self.assertIsNone(self.end(s, active(realized=0.05, ready=660), NOW + 600)['target'])
        self.assertIsNone(self.end(s, active(realized=0.05, ready=3540), NOW + 600)['target'])
        no_home = active(realized=0.0)
        no_home['home'] = None  # no home rate to compare with: nothing to judge
        self.assertIsNone(self.end(s, no_home, NOW + 600)['target'])
        m = s['manager']
        manager.end_excursion(s, d['manager']['excursionEnd']['reason'], NOW + 600, 'early-exit')
        manager.finish_excursion(m, NOW + 900, 'returned home')
        self.assertEqual(m['lastExcursion']['endCode'], 'early-exit')
        self.assertEqual(m['excursionHolds'][QWEN]['until'], NOW + 900 + 86400)
        self.assertEqual(m['unbooked'][0]['homeUsdPerHour'], HOME_RATE)
        home = decide(state(**copy.deepcopy(m)), context(), NOW + 3600)
        self.assertEqual(rows_by_model(home)[QWEN]['why'], 'cooling down')
        later = decide(state(**copy.deepcopy(m)), context(), NOW + 900 + 86401)
        self.assertIsNone(rows_by_model(later)[QWEN]['why'])

    def test_faded_evidence_ends_after_the_dwell_and_cools_down_for_an_hour(self):
        s = away(NOW - 1200)
        self.assertIsNone(self.end(s, active(latest=1.05), NOW)['target'])  # 20 min: dwell
        d = self.end(s, active(latest=1.05), NOW + 600)
        self.assertEqual(d['manager']['excursionEnd']['code'], 'faded')
        self.assertIn('latest hour 1.05x home', d['reason'])
        d = self.end(s, active(latest=None), NOW + 600)
        self.assertEqual(d['manager']['excursionEnd']['code'], 'faded')
        # With a gemma home, a ratio from mixed boxes (0.46x dedicated gemma) is no evidence.
        d = self.end(s, active(latest=1.8, dedicated=False), NOW + 600)
        self.assertEqual(
            d['manager']['excursionEnd'],
            {'code': 'faded', 'reason': 'public evidence for it is no longer visible'},
        )
        # Without the network collector there is nothing to fade; maxMinutes still ends it.
        self.assertIsNone(self.end(s, active(latest=None, network=False), NOW + 600)['target'])
        manager.finish_excursion(s['manager'], NOW + 900, 'returned home', 'faded')
        self.assertEqual(s['manager']['excursionHolds'][QWEN]['until'], NOW + 900 + 3600)

    def test_no_four_hour_clock_while_the_evidence_and_pay_hold(self):
        for hours in (4, 8, 23.9):
            with self.subTest(hours=hours):
                self.assertIsNone(self.end(away(NOW - hours * 3600), active(), NOW)['target'])
        self.assertEqual((ex.MAX_MINUTES, manager.EXCURSION_MAX_MINUTES), (1440, 1440))

    def test_max_duration_ends_it_and_leaves_it_free_to_arm_again(self):
        s = away(NOW - 24 * 3600)
        d = self.end(s, active(), NOW)
        self.assertEqual(
            d['manager']['excursionEnd'],
            {'code': 'max-duration', 'reason': 'it reached its 24-hour safety limit'},
        )
        self.assertEqual(d['target'], GEMMA)
        # An excursion saved with the old 4-hour limit keeps it.
        d = self.end(away(NOW - 240 * 60, maxMinutes=240), active(), NOW)
        self.assertEqual(d['manager']['excursionEnd']['reason'], 'it reached its 4-hour safety limit')
        manager.finish_excursion(s['manager'], NOW + 300, 'returned home', 'max-duration')
        self.assertNotIn('excursionHolds', s['manager'])
        home = decide(state(**copy.deepcopy(s['manager'])), context(lastSwitchAt=NOW - 3600), NOW + 3600)
        self.assertEqual(home['manager']['arming']['model'], QWEN)

    def test_turning_excursions_off_brings_it_home(self):
        d = decide(
            away(NOW - 600),
            context(current=QWEN, active=active(), rules=policy({'managerExcursions': 0})),
            NOW,
        )
        self.assertEqual((d['target'], d['manager']['excursionEnd']['code']), (GEMMA, 'off'))

    def test_a_target_that_stops_serving_is_the_watchdogs_and_is_held_an_hour(self):
        s = away(NOW - 600)
        d = self.end(s, None, NOW)  # no realized data: nothing to judge, no competing move
        self.assertIsNone(d['target'])
        manager.finish_excursion(s['manager'], NOW, 'the serving model changed', 'changed')
        self.assertEqual(s['manager']['excursionHolds'][QWEN]['until'], NOW + 3600)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.h = History(':memory:')
        self.addCleanup(self.h.close)
        self.store = OptimizerStore(self.h)
        self.store.identity('mac', 'p')
        self.ledger = ex.Ledger(self.h)
        self.o = type('O', (), {'network_evidence': None})()

    def credits(self, start, end, usd_per_hour):
        with self.h.lock:
            for i, at in enumerate(range(int(start), int(end), 60)):
                self.h.db.execute(
                    'INSERT INTO opt_credits VALUES(?,?,?,?,?,?,?)',
                    ('acct', int(at), 'p', at + 1, QWEN, round(usd_per_hour / 60 * 1e6), 10),
                )
            self.h.db.commit()

    def excursion(self, start, hours, realized, m, **changes):
        entry = {
            'target': QWEN,
            'from': GEMMA,
            'startedAt': start + 120,
            'leftAt': start,
            'endedAt': start + hours * 3600,
            'predictedUsdPerHour': 0.2,
            'endCode': 'faded',
            'endReason': 'public evidence faded',
            'homeUsdPerHour': HOME_RATE,
            'homeNetworkUsdPerHour': 0.09,
            **changes,
        }
        self.credits(start, start + hours * 3600, realized)
        return ex.book(self.o, self.ledger, entry, 'acct', 'mac')

    # Not in the credits: base periods lost by both switches (base rewards are left out on
    # both sides) and home's 10-minute ramp after the return.
    UNSEEN = 2 * 1.1 * 16 / 8640 + HOME_RATE * 10 / 60

    def test_booking_compares_realized_pay_with_the_home_counterfactual(self):
        r = self.excursion(NOW, 2, 0.19, {})
        self.assertAlmostEqual(r['realizedUsdPerHour'], 0.19, places=3)
        self.assertEqual(r['homeCounterfactualUsdPerHour'], HOME_RATE)
        self.assertEqual(r['counterfactualBasis'], 'trailing')
        self.assertAlmostEqual(r['unseenCostUsd'], self.UNSEEN)
        self.assertAlmostEqual(r['gainUsd'], (0.19 - HOME_RATE) * 2 - self.UNSEEN, places=3)
        self.assertAlmostEqual(r['predictedGainUsd'], (0.2 - HOME_RATE) * 2, places=4)
        s = self.ledger.summary('acct', 'mac', NOW + 3 * 3600)
        self.assertEqual(s['count'], 1)
        self.assertAlmostEqual(s['gainUsd'], r['gainUsd'])
        self.assertIn('+$0.142 against staying home', ex.booked_text(r))
        # A move the user made ends it without a return trip: one switch's base periods only.
        r = self.excursion(NOW + 3 * 3600, 1, 0.19, {}, endCode='manual')
        self.assertAlmostEqual(r['unseenCostUsd'], 1.1 * 16 / 8640)

    def test_counterfactual_follows_the_network_without_a_clamp_and_keeps_its_interval(self):
        # Saturday night to Sunday morning moved dedicated gemma 5.3x in Andrew's cell (gap-2);
        # the old 0.5-2x clamp would have said 2x.
        during = {'usd_per_h': 0.09 * 5.3, 'req_spread': 0.2, 'source': 'cell', 'dedicated_only': True}
        self.o.network_evidence = type('NE', (), {'estimate': lambda self, *a, **k: during})()
        r = self.excursion(
            NOW, 1, 0.19, {}, homeNetworkSpread=0.1, homeNetworkSource='cell', homeNetworkDedicated=True
        )
        self.assertEqual(r['counterfactualBasis'], 'network-scaled')
        self.assertAlmostEqual(r['homeCounterfactualUsdPerHour'], HOME_RATE * 5.3)
        spread = math.hypot(0.1, 0.2)
        self.assertAlmostEqual(r['homeCounterfactualLow'], HOME_RATE * 5.3 * math.exp(-spread))
        self.assertAlmostEqual(r['homeCounterfactualHigh'], HOME_RATE * 5.3 * math.exp(spread))
        saved = self.ledger.records('acct', 'mac', 0)[-1]
        self.assertAlmostEqual(saved['homeCounterfactualLow'], r['homeCounterfactualLow'])
        self.assertAlmostEqual(saved['homeCounterfactualHigh'], r['homeCounterfactualHigh'])
        # A different evidence level (mixed boxes now) is not comparable: trailing only.
        during.update(dedicated_only=False)
        r = self.excursion(
            NOW + 7200, 1, 0.19, {}, homeNetworkSpread=0.1, homeNetworkSource='cell', homeNetworkDedicated=True
        )
        self.assertEqual(
            (r['counterfactualBasis'], r['homeCounterfactualUsdPerHour']), ('trailing', HOME_RATE)
        )
        self.assertEqual(ex.counterfactual(None, {}, {}), None)

    def test_home_counterfactual_in_the_ledger_is_per_clock_hour_like_realized_pay(self):
        # Home's own rate is per ready hour; the ledger's realized pay is every credit per clock
        # hour. Home's minutes around restarts and dark spells paid nothing: $0.108 per ready
        # hour is $0.099 per clock hour over its stints (the 4 h between them do not count).
        data = ex.ExcursionData(type('O', (), {'h': self.h})())
        span = [NOW - 20 * 3600 + i * 60 for i in range(600)]  # a 10-hour stint
        ready = [at for i, at in enumerate(span) if i % 10 != 5]  # 540 ready minutes
        minutes = [{'at': at, 'seconds': 60, 'usd': 0.108 / 60} for at in ready]
        minutes += [{'at': NOW - 5 * 3600 + i * 60, 'seconds': 60, 'usd': 0.108 / 60} for i in range(120)]
        with self.h.lock:
            for i, x in enumerate(minutes):
                self.h.db.execute(
                    'INSERT INTO opt_credits VALUES(?,?,?,?,?,?,?)',
                    ('acct', 50_000_000 + i, 'p', x['at'] + 1, GEMMA, round(x['usd'] * 1e6), 10),
                )
            self.h.db.commit()
        earned = {GEMMA: {'minutes': minutes}}
        wall = data.wall_rate('acct', 'mac', earned, GEMMA, '72h', NOW)
        self.assertAlmostEqual(wall, 0.108 * (540 + 120) / (600 + 120), places=4)
        self.assertIsNone(data.wall_rate('acct', 'mac', earned, GEMMA, 'saved', NOW))
        # Credits never below those of its ready minutes (a gap in the credit history).
        self.assertAlmostEqual(data.wall_rate('other', 'mac', earned, GEMMA, '72h', NOW), wall, places=4)
        r = self.excursion(NOW + 3600, 2, 0.19, {}, homeWallUsdPerHour=0.0972)
        self.assertEqual(r['homeCounterfactualUsdPerHour'], 0.0972)
        self.assertAlmostEqual(r['gainUsd'], (0.19 - 0.0972) * 2 - (2 * 1.1 * 16 / 8640 + 0.0972 / 6), places=3)
        self.assertAlmostEqual(r['predictedGainUsd'], (0.2 - HOME_RATE) * 2, places=4)  # ready basis
        m = {}
        ex.finished(m, {'evidence': {'homeUsdPerHour': HOME_RATE, 'homeWallUsdPerHour': 0.0972}},
                    {'target': QWEN, 'startedAt': NOW, 'endedAt': NOW + 60}, NOW + 60)  # fmt: skip
        self.assertEqual(m['unbooked'][0]['homeWallUsdPerHour'], 0.0972)

    def test_ledger_rows_from_before_the_interval_columns_still_load(self):
        h = History(':memory:')
        self.addCleanup(h.close)
        with h.lock:
            h.db.execute(
                'CREATE TABLE manager_excursions(account TEXT,device TEXT,'
                'started_at REAL,ended_at REAL,model TEXT,home TEXT,predicted REAL,realized REAL,'
                'counterfactual REAL,gain REAL,predicted_gain REAL,end_code TEXT,end_reason TEXT,'
                'PRIMARY KEY(account,device,started_at))'
            )
            h.db.execute(
                'INSERT INTO manager_excursions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ('acct', 'mac', 1.0, 2.0, QWEN, GEMMA, 0.2, 0.1, 0.108, -0.01, 0.1, 'faded', 'x'),
            )
            h.db.commit()
        rows = ex.Ledger(h).records('acct', 'mac', 0)
        self.assertEqual(
            (rows[0]['gainUsd'], rows[0]['homeCounterfactualLow'], rows[0]['homeCounterfactualHigh']),
            (-0.01, None, None),
        )

    def test_kill_switch_after_three_losing_excursions_in_fourteen_days(self):
        m = {}
        self.excursion(NOW - 10 * 86400, 2, 0.30, m)  # +0.36
        self.excursion(NOW - 5 * 86400, 2, 0.02, m)  # -0.20
        self.assertIsNone(ex.kill(self.ledger, 'acct', 'mac', m, NOW))  # two only
        self.excursion(NOW - 86400, 3, 0.0, m)  # -0.35: total -0.18
        off = ex.kill(self.ledger, 'acct', 'mac', m, NOW)
        self.assertNotIn('until', off)
        self.assertEqual(
            off['reason'],
            'The last 3 excursions earned $0.18 less than staying home. '
            'To try again, turn excursions off and back on.',
        )
        m['excursionsOff'] = off
        d = decide(state(**m, arming={'model': QWEN, 'since': NOW - 3600, 'checks': 2, 'lastCheckAt': NOW}), context(), NOW)
        self.assertIsNone(d['target'])
        self.assertEqual(d['manager']['evidence']['gate'], 'paused')
        ledger = d['manager']['ledger']
        self.assertEqual(
            (ledger['enabled'], ledger['disabledBy'], ledger['disabledUntil']), (False, 'ledger', None)
        )
        self.assertEqual(ledger['disabledReason'], off['reason'])
        # It never lifts by itself, not after 7 days, not after 14...
        for later in (7 * 86400 + 1, 30 * 86400):
            self.assertEqual(ex.paused(m, NOW + later), off)
            later_d = decide(state(**m), context(), NOW + later)
            self.assertEqual(later_d['manager']['ledger']['disabledBy'], 'ledger')
            self.assertIsNone(later_d['target'])
        # ...only when the user turns excursions off and on; old losses no longer count.
        self.assertFalse(manager.policy_saved({'manager': m}, policy(), policy(), NOW + 60))
        self.assertTrue(
            manager.policy_saved({'manager': m}, policy({'managerExcursions': 0}), policy(), NOW + 60)
        )
        self.assertNotIn('excursionsOff', m)
        self.assertIsNone(ex.kill(self.ledger, 'acct', 'mac', m, NOW + 120))
        # Turned off by the user: the ledger says so.
        d = decide(state(), context(rules=policy({'managerExcursions': 0})), NOW)
        self.assertEqual(d['manager']['ledger']['disabledBy'], 'setting')
        self.assertIsNone(decide(state(), context(), NOW)['manager']['ledger']['disabledBy'])

    def test_kill_switch_needs_a_lower_bound_at_or_above_zero(self):
        # Gains net of the round trip. Mostly winning but noisy: the mean is +$0.12 per
        # excursion, the one-sided 90% lower bound (t with 2 df x SD / sqrt 3) is -$0.07.
        gains = [0.30, 0.10, -0.05]
        mean, low = ex.ledger_bound(gains)
        self.assertAlmostEqual(mean, 0.35 / 3)
        self.assertAlmostEqual(low, 0.35 / 3 - 1.886 * statistics.stdev(gains) / math.sqrt(3))
        self.assertLess(low, 0)
        m = {}
        for i, gain in enumerate(gains):
            self.excursion(NOW - (i + 1) * 86400, 1, HOME_RATE + gain + self.UNSEEN, m)
        off = ex.kill(self.ledger, 'acct', 'mac', m, NOW)
        self.assertEqual(
            off['reason'],
            'The last 3 excursions earned only $0.35 more than staying home, not clearly enough. '
            'To try again, turn excursions off and back on.',
        )
        self.assertAlmostEqual(off['gainUsd'], 0.35, places=2)
        self.assertLess(off['lowUsd'], 0)
        # Rows older than 14 days, or from before excursions were turned back on, do not count.
        self.assertIsNone(ex.kill(self.ledger, 'acct', 'mac', {}, NOW + 12 * 86400))
        since = {'excursionLedgerSince': NOW - 1.5 * 86400}
        self.assertIsNone(ex.kill(self.ledger, 'acct', 'mac', since, NOW))
        # One-sided 90%: $0.30, $0.10 and $0.05 stay on (a one-sided 95% bound, 2.920, turned
        # them off).
        self.assertGreaterEqual(ex.ledger_bound([0.30, 0.10, 0.05])[1], 0)
        self.assertLess(0.15 - 2.920 * statistics.stdev([0.30, 0.10, 0.05]) / math.sqrt(3), 0)
        # Clearly winning excursions stay on.
        h = History(':memory:')
        self.addCleanup(h.close)
        OptimizerStore(h).identity('mac', 'p')
        self.h, self.ledger = h, ex.Ledger(h)
        for i, gain in enumerate((0.20, 0.25, 0.30)):
            self.excursion(NOW - (i + 1) * 86400, 1, HOME_RATE + gain + self.UNSEEN, m)
        self.assertGreater(ex.ledger_bound([0.20, 0.25, 0.30])[1], 0)
        self.assertIsNone(ex.kill(self.ledger, 'acct', 'mac', m, NOW))

    def test_dead_times_come_from_this_macs_switch_history(self):
        self.store.identity('other-mac', 'q')
        data = ex.ExcursionData(type('O', (), {'h': self.h})())
        now = NOW

        def event(at, kind, model, device='mac'):
            self.store.event('acct', device, now - 86400 + at, kind, model, 'x')

        def paid(at, model, provider='p'):
            with self.h.lock:
                self.h.db.execute(
                    'INSERT INTO opt_credits VALUES(?,?,?,?,?,?,?)',
                    ('acct', int(at * 10) + len(provider), provider, now - 86400 + at, model, 90, 10),
                )
                self.h.db.commit()

        def switch(command, ready, first, model):
            event(command, 'switching', model)
            event(ready, 'switched', model)
            if first:
                paid(first, model)

        switch(1000, 1072, 1115, GEMMA)  # 115 s
        switch(5000, 5080, 5179, QWEN)  # 179 s
        switch(9000, 9070, 9100, GEMMA)  # 100 s
        paid(9010, GEMMA, provider='q')  # another Mac on the account: not this Mac's job
        switch(12000, 12080, None, QWEN)  # not paid before the next command: >= 1000 s
        switch(13000, 13070, 13200, GEMMA)  # 200 s
        # A restart of the model already serving: its drain tail is no first paid job.
        switch(13500, 13560, 13505, GEMMA)
        # A deferred switch leaves gemma serving, so this is a restart too.
        event(14000, 'switching', QWEN)
        event(14005, 'switch-deferred', QWEN)
        switch(14500, 14560, 14520, GEMMA)
        # A failed switch is no dead time; what serves after it is unknown.
        event(16000, 'switching', BIG)
        event(16200, 'failed', BIG)
        switch(17000, 17070, 17130, GEMMA)  # 130 s
        switch(20000, 20080, 20250, QWEN)  # 250 s
        switch(22000, 22070, 22110, GEMMA)  # 110 s
        switch(30000, 30080, None, QWEN)  # open and not paid yet: not counted
        # Another Mac's switches and anything older than 30 days do not count.
        event(24000, 'switching', 'gpt-oss-20b', device='other')
        event(24070, 'switched', 'gpt-oss-20b', device='other')
        self.store.event('acct', 'mac', now - 31 * 86400, 'switching', 'gpt-oss-20b', 'x')
        self.store.event('acct', 'mac', now - 31 * 86400 + 60, 'switched', 'gpt-oss-20b', 'x')
        dead = data.dead_seconds('acct', 'mac', now)
        self.assertEqual(dead, {GEMMA: 115, QWEN: 250})  # medians of 5 and of 3
        # Cached for an hour; then the open episode counts once paid.
        paid(30500, QWEN)
        self.assertEqual(data.dead_seconds('acct', 'mac', now + 60), dead)
        self.assertEqual(data.dead_seconds('acct', 'mac', now + 3601)[QWEN], (250 + 500) / 2)

    def test_pay_is_judged_after_the_models_ramp_against_what_home_would_have_paid(self):
        left = NOW - 5400
        excursion = {'target': QWEN, 'leftAt': left, 'startedAt': left + 80}
        # Nothing for the first 20 minutes (dead time and ramp), then $0.24/h.
        minutes = [
            {'at': left + i * 60, 'seconds': 60, 'usd': 0.0 if i < 20 else 0.24 / 60}
            for i in range(90)
        ]
        earned = {QWEN: {'minutes': minutes}}
        before = {'usdPerHour': 0.09, 'spread': 0.1, 'source': 'cell', 'dedicated': True}
        ramp = ex.ramp_seconds(QWEN, {QWEN: 300})  # 5 + 25 min
        judge = ex.ExcursionData.active
        a = judge(None, earned, excursion, QWEN, GEMMA, 'M5 Pro|48', NOW, ramp, 0.108, before)
        self.assertEqual(a['rampSeconds'], 1800)
        # Per ready hour, like home's trailing rate: 58 settled ready minutes at $0.24/h.
        self.assertEqual((a['readySeconds'], a['realizedSeconds']), (58 * 60, 5400 - 120 - 1800))
        self.assertAlmostEqual(a['realizedUsdPerHour'], 0.24)
        self.assertAlmostEqual(a['realizedHigh'], 0.24)  # two steady 20-minute blocks
        # Without network evidence, home's counterfactual is its trailing rate, x/÷ 2.
        self.assertEqual(
            a['home'], {'usdPerHour': 0.108, 'low': 0.054, 'high': 0.216, 'basis': 'trailing'}
        )
        # With it, home's network rate doubled since leaving: the bar doubles, with an interval.
        level = {'source': 'cell', 'dedicated_only': True}
        ne = type(
            'NE',
            (),
            {
                'relative': lambda self, *a, **k: {'ratio': 2.5, **level},
                'estimate': lambda self, *a, **k: {'usd_per_h': 0.18, 'req_spread': 0.1, **level},
            },
        )()
        a = judge(ne, earned, excursion, QWEN, GEMMA, 'M5 Pro|48', NOW, ramp, 0.108, before)
        self.assertAlmostEqual(a['home']['usdPerHour'], 0.216)
        self.assertAlmostEqual(a['home']['low'], 0.216 * math.exp(-math.hypot(0.1, 0.1)))
        self.assertEqual((a['latestRatio'], a['latestDedicated']), (2.5, True))
        # Under 10 judged minutes there is no figure yet.
        short = judge(
            None, earned, excursion, QWEN, GEMMA, 'M5 Pro|48', left + 2400, ramp, 0.108, before
        )
        self.assertIsNone(short['realizedUsdPerHour'])

    def test_policy_key_defaults_on_and_accepts_only_zero_or_one(self):
        self.assertEqual(policy()['managerExcursions'], 1)
        saved = policy()
        saved.pop('managerExcursions')  # a revision-3 policy saved before this key existed
        self.assertEqual(policy(saved)['managerExcursions'], 1)
        self.assertEqual(policy({'managerExcursions': 0})['managerExcursions'], 0)
        for bad in (2, True, 'on', 0.5, None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                policy({'managerExcursions': bad})


class HomeBaseTests(unittest.TestCase):
    """Home's network rate is a base only over the own home rate's window and when firm; the
    prediction cannot exceed the candidate's network $/h over that base."""

    def test_home_network_base_is_read_over_the_window_of_the_own_home_rate(self):
        h = History(':memory:')
        self.addCleanup(h.close)
        OptimizerStore(h).identity('mac', 'p')
        calls = []
        point = {'usd_per_h': 0.09, 'req_spread': 0.3, 'requests': 500, 'source': 'cell',
                 'dedicated_only': True, 'providers': 8}  # fmt: skip

        class NE:
            lock = threading.Lock()
            last_window = None

            def own_cell(self):
                return 'M5 Pro|48'

            def estimate(self, model, cell=None, hours=2, now=None, basis=None):
                calls.append((model, hours))
                return point

            def relative(self, *a, **k):
                return {'estimate': point, 'home_estimate': point}

        o = type('O', (), {})()
        o.h, o.network_evidence = h, NE()
        o.environment_reason = Mock(return_value=None)
        o.demand_auto = Mock()
        data = ex.ExcursionData(o)
        live = {'account': 'acct', 'device': 'mac', 'hardware': {'memoryTotalGB': 48}}
        rows = [{'id': GEMMA, 'available': True}]
        cases = (
            ('72h', tm.minutes(0.108, 10, NOW), 72),
            ('14d', tm.minutes(0.108, 10, NOW - 5 * 86400), 14 * 24),
            ('saved', [], None),
        )
        for basis, minutes, hours in cases:
            with self.subTest(basis):
                calls.clear()
                data.invalidate()
                o.demand_auto.evidence = Mock(return_value=({GEMMA: {'minutes': minutes}}, {}))
                home = {'model': GEMMA, 'usdPerHour': 0.1}
                x = data.context({'manager': {}}, live, {}, rows, home, GEMMA, NOW)
                self.assertEqual(x['homeBasis'], basis)
                if hours:
                    self.assertEqual(calls, [(GEMMA, hours)])
                    self.assertEqual(x['homeNetwork']['requests'], 500)
                else:  # no window: no base, so no excursion (evaluate: 'no evidence')
                    self.assertEqual((calls, x['homeNetwork']), ([], {}))

    def test_a_thin_home_base_arms_nothing_and_scales_nothing(self):
        # Review case: six dedicated gemma boxes at 0.4 req/h (a near-silent night), six
        # candidate boxes at 40 req/h. The proposal said "about $36.92/h vs $0.106/h".
        h = History(':memory:')
        self.addCleanup(h.close)
        ne = NetworkEvidence(h, lambda: set(), lambda: {'chip': 'Apple M5 Pro', 'memoryTotalGB': 48})
        ne.set_pricing(
            {**PRICING, 'prices': PRICING['prices'] + [{'model': QWEN, 'input_price': 150000, 'output_price': 700000}]}
        )
        hour, cell = int(NOW // 3600) * 3600, 'M5 Pro|48'
        rows = []
        for hr in (hour - 7200, hour - 3600, hour):
            rows.append((hr, cell, GEMMA, 1, 6, 12, 72, 0.0, 0.4, 2.0, 0.97, None, 220))
            rows.append((hr, cell, QWEN, 1, 6, 12, 72, 38.0, 40.0, 12.0, 0.0, None, 500))
            rows.append((hr, '*', '*', 0, 1000, 12, 12, 3e5, 3e5, 1e4, 0.0, 2557, 400))
        with h.lock:
            h.db.executemany('INSERT INTO network_cell_rates VALUES(%s)' % ','.join('?' * 13), rows)
            h.db.commit()
        base = ne.estimate(GEMMA, cell, hours=72, now=NOW, basis='list')
        self.assertLess(base['requests'], ex.MIN_BASE_REQUESTS)
        self.assertGreater(base['req_spread'], ex.MAX_BASE_SPREAD)
        candidate = {'model': QWEN, 'priorSuccess': True, **ex.ExcursionData.ratio(ne, QWEN, GEMMA, cell, NOW)}
        self.assertGreater(candidate['ratio'], 50)
        self.assertGreater(candidate['low'], 1)  # the ratio's own interval did not stop it
        self.assertIs(candidate['homeUsable'], False)
        s = state(arming={'model': QWEN, 'since': NOW - 3600, 'checks': 2, 'lastCheckAt': NOW - 60})
        network = {**ex.network_point(base), 'providers': base['providers']}
        for home_network, home_usable in ((network, False), ({**network, 'spread': 0.3, 'requests': None}, False),
                                          (network, None)):  # fmt: skip
            with self.subTest(home_usable=home_usable, base=home_network['spread']):
                c = {**candidate, 'homeUsable': home_usable}
                d = decide(s, context(candidates=[c], homeNetwork=home_network), NOW)
                self.assertIsNone(d['target'])
                self.assertEqual(rows_by_model(d)[QWEN]['why'], 'no evidence')
        # A firm 2 h ratio over a thin base is not enough either.
        c = {**candidate, 'homeUsable': True}
        d = decide(s, context(candidates=[c], homeNetwork=network), NOW)
        self.assertEqual((d['target'], rows_by_model(d)[QWEN]['why']), (None, 'no evidence'))
        # Nor does a thin base scale home's counterfactual: the trailing rate, x/÷ 2.
        before = {'usdPerHour': 0.0004, 'spread': 0.9, 'requests': 5, 'source': 'cell', 'dedicated': True}
        during = {'usdPerHour': 0.012, 'spread': 0.3, 'requests': 400, 'source': 'cell', 'dedicated': True}
        cf = ex.counterfactual(0.106, before, during)
        self.assertEqual((cf['basis'], cf['usdPerHour']), ('trailing', 0.106))
        self.assertAlmostEqual(cf['low'], 0.053)
        cf = ex.counterfactual(0.106, {**before, 'requests': 50, 'spread': 1.0}, during)
        self.assertEqual(cf['basis'], 'trailing')  # a 100% error is no base
        cf = ex.counterfactual(0.106, {**before, 'requests': 50, 'spread': 0.5}, during)
        self.assertEqual(cf['basis'], 'network-scaled')
        self.assertAlmostEqual(cf['usdPerHour'], 0.106 * 30)

    def test_prediction_is_capped_by_the_candidates_network_pay_over_homes_base(self):
        """Sep 27 in Andrew's cell: dedicated gemma's 2 h rate fell 942 -> 206 req/h (day mean
        474). A candidate at 2.4x that quiet hour earns ~1.04x home's usual level."""
        base = {'usdPerHour': 0.09, 'spread': 0.3, 'source': 'cell', 'providers': 9, 'dedicated': True}
        quiet = evidence(ratio=2.4, low=1.3, latest=2.4, usdPerHour=0.09 * 2.4 * 206 / 474)
        r = rows_by_model(decide(state(), context(candidates=[quiet], homeNetwork=base), NOW))[QWEN]
        self.assertAlmostEqual(r['usdPerHour'], HOME_RATE * 2.4 * 206 / 474, places=4)
        self.assertAlmostEqual(r['ratio'], 2.4)  # the ratio itself is shown as measured
        self.assertEqual(r['why'], 'gain too small')
        # A candidate really paying more than home's usual level keeps its ratio.
        busy = evidence(ratio=2.4, low=1.3, latest=2.4, usdPerHour=0.09 * 3)
        s = state(arming={'model': QWEN, 'since': NOW - 3600, 'checks': 2, 'lastCheckAt': NOW - 60})
        ctx = context(candidates=[busy], homeNetwork={**base, 'requests': 400}, homeWallUsdPerHour=0.1)
        d = decide(s, ctx, NOW)
        self.assertEqual(d['target'], QWEN)
        p = d['manager']['proposal']
        self.assertAlmostEqual(p['predictedUsdPerHour'], HOME_RATE * 2.4, places=4)
        # What the counterfactual and the ledger need later: the base's firmness, home per clock hour.
        self.assertEqual((p['evidence']['homeNetworkRequests'], p['evidence']['homeWallUsdPerHour']), (400, 0.1))
        # Figures from different levels are not comparable: no cap.
        other = evidence(ratio=2.4, low=1.3, latest=2.4, usdPerHour=0.01, source='neighbour')
        r = rows_by_model(decide(state(), context(candidates=[other], homeNetwork=base), NOW))[QWEN]
        self.assertAlmostEqual(r['usdPerHour'], HOME_RATE * 2.4, places=4)
        # The proposal uses the capped figure.
        capped = evidence(ratio=2.4, low=1.3, latest=2.4, usdPerHour=0.09 * 2.2)
        d = decide(s, context(candidates=[capped], homeNetwork=base), NOW)
        self.assertAlmostEqual(d['manager']['proposal']['predictedUsdPerHour'], HOME_RATE * 2.2, places=4)
        self.assertIn('expected about $%.3f/h here' % (HOME_RATE * 2.2), d['reason'])


class ReviewRuleTests(unittest.TestCase):
    def test_the_policys_lower_daily_switch_limit_gates_excursions(self):
        # Dispatch refuses an excursion past maxSwitchesPerDay (excursion_limit); arming must
        # not confirm one only to have it deferred and retried all day.
        armed = {'model': QWEN, 'since': NOW - 3600, 'checks': 2, 'lastCheckAt': NOW - 60}
        for limit, today, gate in ((1, 1, 'daily limit'), (2, 2, 'daily limit'), (2, 1, None),
                                   (4, 3, 'daily limit'), (4, 2, None)):  # fmt: skip
            with self.subTest(limit=limit, today=today):
                rules = policy({'maxSwitchesPerDay': limit})
                d = decide(state(arming=armed), context(rules=rules, today=today), NOW)
                self.assertEqual(d['manager']['evidence']['gate'], gate)
                self.assertEqual(d['target'], None if gate else QWEN)
        self.assertEqual(ex.daily_limit({}), ex.MAX_PER_DAY)

    def test_excursion_windows_stay_out_of_the_home_choice_for_thirty_days(self):
        # Three old qwen excursions (16-20 days ago, $0.30/h) used to become qwen's home
        # evidence once a later excursion pruned windows older than 14 days.
        day = 86400
        gemma = [x for i in range(30) for x in tm.minutes((0.09, 0.12, 0.10, 0.11)[i % 4], 12, NOW - i * day)]
        trips = [(NOW - k * day - 8 * 3600, NOW - k * day) for k in (16, 18, 20)]
        earned = {GEMMA: {'minutes': gemma}, QWEN: {'minutes': [x for a, b in trips for x in tm.minutes(0.30, 8, b)]}}
        m = {'home': {'model': GEMMA, 'source': 'history', 'at': NOW - 20 * day},
             'excursionWindows': [[QWEN, a, b] for a, b in trips]}  # fmt: skip
        other = {'target': BIG, 'startedAt': NOW - 3 * 3600, 'leftAt': NOW - 3 * 3600 - 100, 'evidence': {}}
        record = {**{k: other[k] for k in ('target', 'startedAt', 'leftAt')}, 'endedAt': NOW, 'endCode': 'faded'}
        ex.finished(m, other, record, NOW)
        self.assertEqual(len(m['excursionWindows']), 4)
        self.assertNotIn(QWEN, manager.home_rates(earned, NOW, ex.windows(m, NOW)))
        self.assertEqual(manager.choose_home(m, earned, lambda: [], GEMMA, {GEMMA, QWEN}, NOW)['model'], GEMMA)
        # Windows go once the home choice no longer reads them (30 days), 90 at most.
        ex.finished(m, other, {**record, 'endedAt': NOW + 11 * day}, NOW + 11 * day)
        self.assertEqual([w[0] for w in m['excursionWindows']], [QWEN, QWEN, BIG, BIG])
        self.assertEqual(ex.HOME_WINDOW_SECONDS, manager.HOME_WINDOW)
        m['excursionWindows'] = [[BIG, NOW - i, NOW - i + 1] for i in range(200, 0, -1)]
        ex.finished(m, other, record, NOW)
        self.assertEqual(len(m['excursionWindows']), ex.MAX_WINDOWS)
        self.assertEqual(m['excursionWindows'][-1][2], NOW)

    def test_realized_pay_bound_comes_from_its_block_rates(self):
        # An hour after the ramp: $0.30/h, then nothing for 40 minutes. The mean is $0.10/h but
        # 20-minute blocks that uneven leave its 90% upper bound at ~$0.29/h.
        minutes = [{'at': NOW + i * 60, 'seconds': 60, 'usd': (0.30 if i < 20 else 0.0) / 60} for i in range(60)]
        high = ex.realized_high(minutes, 0.10)
        self.assertAlmostEqual(high, 0.10 + 1.886 * statistics.stdev([0.3, 0, 0]) / math.sqrt(3))
        self.assertIsNone(ex.realized_high(minutes[:39], 0.10))  # fewer than two blocks
        s = away(NOW - 5400)
        act = active(realized=0.10, high=high, low=0.2)
        self.assertIsNone(decide(s, context(current=QWEN, active=act), NOW)['target'])
        act = active(realized=0.10, high=high, low=0.3)
        self.assertEqual(decide(s, context(current=QWEN, active=act), NOW)['target'], GEMMA)


def fresh_decision(test):
    from optimizer import Optimizer

    test.o.demand_auto.evaluate = Mock(side_effect=test.legacy)
    return Optimizer.demand_decision(test.o, test.now)


class Sep9ReplayTests(tm.Harness):
    """Andrew's M5 Pro 48 GB on gemma (~$0.108 per ready hour). Public data shows dedicated
    qwen3.5-35b-a3b boxes in his cell earning 4.5x dedicated gemma for hours (on Sep 9 his own
    qwen3.5-35b paid 2-2.8x gemma), then the regime ends. Expected: one excursion after two
    hourly checks and the 5-minute confirmation, pay tracked against what home would have
    paid, a return home when the evidence fades, a ledger row, and no second excursion.
    This Mac's gemma jobs paid list x the network's tokens (a calibrated home); it never
    served qwen3.5-35b, so that side carries the uncalibrated ln 4 $/request error of a model
    outside the gemma and gpt-oss families on a normal check: 4.5x clears it, and a 3x regime arms
    only as a trial, when qwen3.5-35b fits memory and its demand has held above 1.5x usual for two
    hours (test_a_sustained_3x_regime_with_high_demand_arms_one_trial)."""

    CELL = 'M5 Pro|48'
    REGIME = 4.5

    def setUp(self):
        super().setUp()
        self.o.local = [
            {'id': m, 'estimated_memory_gb': gb, 'size_bytes': 1, 'template_render_ok': True}
            for m, gb in ((GEMMA, 15.6), (QWEN, 20.9))
        ]
        self.o.catalog = [{'id': m, 'active': True, 'min_ram_gb': 36} for m in (GEMMA, QWEN)]
        self.o.read_options.return_value = (GEMMA, tm.OPTIONS, {})
        self.o.raw.update(
            advertised_models=[GEMMA],
            current_model=GEMMA,
            warm_models=[GEMMA],
            version='0.9.10',
            lifecycle={'outcome': 'serving', 'remaining': 0},
            capacity={'total_memory_gb': 48, 'gpu_memory_active_gb': 17, 'gpu_memory_cache_gb': 0},
            stats={'requests_served': 7946, 'tokens_generated': 900000},
        )
        self.o.live['provider'].update(model=GEMMA, memoryGB=17)
        self.o.live['hardware'].update(memoryTotalGB=48, memoryAvailableGB=24)
        self.o.state.update(expectedModel=GEMMA, models=[GEMMA, QWEN], lastSwitchAt=self.now - 86400)
        self.o.state['manager'] = {
            'home': {'model': GEMMA, 'source': 'history', 'at': self.now - 90000}
        }
        self.o.read_state.return_value = copy.deepcopy(self.o.raw)
        self.o.warmup = {
            'session': session_key(self.o.raw),
            'model': GEMMA,
            'status': 'ready',
            'verifiedAt': self.now - 1,
        }
        self.o.verify_local_target = Mock()
        self.o.tracking = Mock(return_value={'counting': True})
        del self.o.tick_demand  # the real demand tick, confirmation and switch path
        self.commands = []
        self.o.command = Mock(side_effect=self.started)
        self.o.verify_started = Mock(side_effect=self.verified)
        self.o.demand_auto.evaluate = Mock(side_effect=self.legacy)
        self.o.demand_auto.evidence = Mock(side_effect=self.earned)
        # Public evidence: hourly aggregates for this cell, as network_evidence writes them.
        self.ne = NetworkEvidence(
            self.h, lambda: {'p'}, lambda: {'chip': 'Apple M5 Pro', 'memoryTotalGB': 48.0}
        )
        self.ne.set_pricing(
            {
                **PRICING,
                'prices': PRICING['prices']
                + [{'model': QWEN, 'input_price': 42000, 'output_price': 220000}],
            }
        )
        self.o.network_evidence = self.ne
        self.o.store.identity(self.live['device'], 'p')
        self.h.cache('account', 'acct')
        with self.h.lock:  # yesterday's gemma jobs at list x the network's tokens (106 micro-USD)
            self.h.db.executemany(
                'INSERT INTO opt_credits VALUES(?,?,?,?,?,?,?)',
                [('acct', 20_000_000 + i, 'p', self.now - 86400, GEMMA, 106, 10) for i in range(60)],
            )
            self.h.db.commit()
        self.hour0 = int(self.now // 3600) * 3600
        self.regime_ends = self.hour0 + 3 * 3600
        self.regime(self.REGIME)

    def regime(self, ratio):
        for hour in range(self.hour0 - 6 * 3600, self.hour0 + 9 * 3600, 3600):
            self.put(hour, GEMMA, 300.0, providers=8)
            self.put(hour, QWEN, 300.0 * (ratio if hour < self.regime_ends else 0.9), providers=5)
            self.put(hour, '*', 100000.0, providers=500, dedicated=0, prompt=2000.0)

    def put(self, hour, model, mean, providers, dedicated=1, prompt=None):
        cell = '*' if model == '*' else self.CELL
        with self.h.lock:
            self.h.db.execute(
                'INSERT OR REPLACE INTO network_cell_rates VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (hour, cell, model, dedicated, providers, 12, providers * 12, mean, mean, 20.0, 0.0, prompt, 100.0),
            )
            self.h.db.commit()

    def legacy(self, account, device, rows, current, raw, rules, now, *args, **kwargs):
        d = legacy_upgrade()
        d.update(at=now, currentModel=current, policy=rules, target=None, kind=None)
        return d

    def earned(self, account, device, models, now):
        """Gemma's 72 h at $0.108 per ready hour, and qwen at $0.19 while it serves."""
        out = {GEMMA: {'minutes': tm.minutes(0.108, 72, self.now), 'seconds': 72 * 3600}}
        start = self.qwen_since
        if start is not None:
            count = int((min(now, self.qwen_until or now) - start) // 60)
            out[QWEN] = {
                'minutes': [
                    {'at': start + i * 60, 'seconds': 60, 'usd': self.qwen_rate / 60}
                    for i in range(count)
                ],
                'seconds': count * 60,
            }
        return out, {}

    qwen_since = qwen_until = None
    qwen_rate = 0.19

    def started(self, target, options, environment):
        self.commands.append((target, self.o.state.get('pending', {}).get('at')))
        self.o.read_options.return_value = (target, tm.OPTIONS, {})
        self.o.raw.update(
            advertised_models=[target],
            current_model=target,
            warm_models=[],
            started_at=self.o.raw['written_at'] - 1,
            pid=self.o.raw['pid'] + 1,
            stats={'requests_served': 0, 'tokens_generated': 0},
        )
        self.o.live['provider']['model'] = target
        self.o.identity_session = (self.o.raw['started_at'], self.o.raw['pid'])

    def verified(self, target, *args, **kwargs):
        self.o.raw['warm_models'] = [target]
        self.o.raw['stats'] = {'requests_served': 5, 'tokens_generated': 500}
        self.o.read_state.return_value = copy.deepcopy(self.o.raw)
        self.o.warmup = {
            'session': session_key(self.o.raw),
            'model': target,
            'status': 'ready',
            'verifiedAt': self.o.raw['written_at'],
        }
        return True

    def credits(self, start, end, usd_per_hour):
        with self.h.lock:
            for i, at in enumerate(range(int(start), int(end), 60)):
                self.h.db.execute(
                    'INSERT OR REPLACE INTO opt_credits VALUES(?,?,?,?,?,?,?)',
                    ('acct', 10_000_000 + i, 'p', at + 1, QWEN, round(usd_per_hour / 60 * 1e6), 10),
                )
            self.h.db.commit()

    def run_for(self, hours, pay=0.19):
        """Tick every minute; returns when the Mac armed, left and came back."""
        seen = {}
        for offset in range(0, int(hours * 3600), 60):
            now = self.at(offset)
            m = self.o.state['manager']
            view = (self.o.last_demand_decision or {}).get('manager') or {}
            if view.get('arming') and 'armed' not in seen:
                seen['armed'] = (now, view['arming']['checks'])
            if m.get('excursion') and 'left' not in seen:
                seen['left'] = now
                self.qwen_since = m['excursion']['startedAt']
            if m.get('excursion') and now - m['excursion']['startedAt'] >= 5400:
                seen.setdefault('realized', (view.get('excursion') or {}).get('realizedUsdPerHour'))
            if 'left' in seen and not m.get('excursion') and 'back' not in seen:
                seen['back'] = now
                self.qwen_until = m['lastExcursion']['endedAt']
                self.credits(m['lastExcursion']['leftAt'], self.qwen_until, pay)
        return seen

    def test_one_excursion_on_persistent_evidence_then_home_when_it_fades(self):
        seen = self.run_for(6)
        m = self.o.state['manager']
        self.assertEqual([c[0] for c in self.commands], [QWEN, GEMMA])
        self.assertEqual(seen['armed'], (self.now, 1))  # the first check at once
        # Two hourly checks, then the 5-minute confirmation.
        self.assertGreaterEqual(seen['left'] - self.now, 3600 + 300)
        self.assertLess(seen['left'] - self.now, 3600 + 600)
        last = m['lastExcursion']
        self.assertEqual((last['target'], last['from'], last['endCode']), (QWEN, GEMMA, 'faded'))
        self.assertAlmostEqual(last['predictedUsdPerHour'], 0.108 * self.REGIME, places=3)
        # The evidence fades once the latest-hour window holds only post-regime hours.
        self.assertGreaterEqual(last['endedAt'], self.regime_ends + 3600)
        self.assertLess(last['endedAt'], self.regime_ends + 3600 + 900)
        self.assertLess(last['endedAt'] - last['startedAt'], 240 * 60)
        self.assertEqual(last['maxMinutes'], 24 * 60)  # evidence ended it, not a clock
        self.assertEqual(self.o.state['mode'], 'demand')
        self.assertEqual(m['home']['model'], GEMMA)  # excursion pay never moves the home
        self.assertEqual(m['excursionWindows'][-1][0], QWEN)
        # Booked once its credits settled: $0.19/h against gemma's $0.108/h.
        self.assertNotIn('unbooked', m)
        rows = self.o.manager.excursions.ledger.records('acct', self.live['device'], 0)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]['realizedUsdPerHour'], 0.19, places=2)
        self.assertGreater(rows[0]['gainUsd'], 0)
        self.assertTrue(any('Excursion to %s ended' % QWEN in e for e in self.events('manager')))
        self.assertAlmostEqual(seen['realized'], 0.19, places=2)  # the running figure while away
        runs = self.o.demand_auto.runs('acct', self.live['device'], self.now + 6 * 3600)
        self.assertEqual(
            sorted(r['decision']['kind'] for r in runs if r['result'] == 'switched'),
            ['excursion', 'home'],
        )
        view = self.o.last_demand_decision['manager']
        self.assertEqual((view['ledger']['count'], view['ledger']['enabled']), (1, True))
        self.assertIsNone(view['arming'])
        self.assertEqual({r['model']: r['why'] for r in view['evidence']['rows']}[QWEN], 'weak evidence')

    def test_a_low_own_gemma_dollars_per_job_does_not_inflate_the_ratio(self):
        # Equal public req/h in the cell and equal list prices: the models pay the same.
        for hour in range(self.hour0 - 6 * 3600, self.hour0 + 3600, 3600):
            self.put(hour, QWEN, 300.0, providers=5)
        # This Mac's gemma jobs paid 30 micro-USD each (list x network mix: ~106).
        with self.h.lock:
            self.h.db.execute('DELETE FROM opt_credits')
            self.h.db.executemany(
                'INSERT INTO opt_credits VALUES(?,?,?,?,?,?,?)',
                [('acct', i, 'p', self.now - 3600, GEMMA, 30, 10) for i in range(60)],
            )
            self.h.db.commit()
        self.ne.own_at = None
        self.assertEqual(self.ne.estimate(GEMMA, now=self.now)['usd_basis'], 'own')  # display
        # Even by default a ratio never prices one side with own $/job and the other at list.
        one = self.ne.relative(QWEN, GEMMA, now=self.now)
        bases = (one['estimate']['usd_basis'], one['home_estimate']['usd_basis'])
        self.assertEqual(bases, ('list', 'list'))
        self.assertAlmostEqual(one['ratio'], 1.0)
        # The mismatch widens gemma's $/request error instead: |ln(30/106)| + 0.17.
        self.assertAlmostEqual(one['home_estimate']['usd_error'], math.log(106 / 30) + 0.17, places=3)
        view = fresh_decision(self)['manager']
        row = {r['model']: r for r in view['evidence']['rows']}[QWEN]
        self.assertAlmostEqual(row['ratio'], 1.0, places=3)
        self.assertAlmostEqual(row['usdPerHour'], 0.108, places=3)  # own home $/h x ratio
        self.assertEqual(row['why'], 'weak evidence')
        self.assertIsNone(view['arming'])
        self.run_for(2.5)
        self.assertEqual(self.commands, [])

    def test_a_1_85x_regime_priced_at_list_alone_does_not_arm(self):
        # The old fixture: 1.85x public ratio. With an uncalibrated qwen3.5-35b (ln 4) the 90%
        # low is 1.85 x e^-1.4 ~ 0.46, and a gain from a $0.108 home must reach 2x anyway.
        self.regime(1.85)
        view = fresh_decision(self)['manager']
        row = {r['model']: r for r in view['evidence']['rows']}[QWEN]
        self.assertAlmostEqual(row['ratio'], 1.85, places=3)
        self.assertLess(row['ratioLow'], 1)
        self.assertEqual(row['why'], 'weak evidence')
        self.run_for(2.5)
        self.assertEqual(self.commands, [])

    def demand_history(self, high_from, high_to, usual=4.0, high=12.0):
        """Public capacity samples for qwen3.5-35b every 30 s (opt_network): its usual load at
        this time of day on the same weekday 1-3 weeks ago, and today `high` between
        `high_from` and `high_to` seconds from now (else usual)."""
        rows = []
        for days in (7, 14, 21):
            base = int(self.now) - days * 86400
            rows += [(t, QWEN, usual, 0, 20, 20) for t in range(base - 4 * 3600, base + 7 * 3600, 30)]
        start = int(self.now)
        rows += [
            (t, QWEN, high if high_from <= t - start < high_to else usual, 0, 20, 20)
            for t in range(start - 3 * 3600, start + 7 * 3600, 30)
        ]
        with self.h.lock:
            self.h.db.executemany('INSERT OR REPLACE INTO opt_network VALUES(?,?,?,?,?,?)', rows)
            self.h.db.commit()
        self.o.manager.excursions.invalidate()

    def test_a_sustained_3x_regime_with_high_demand_arms_one_trial(self):
        """calibration-2026-09-28.md (b) still holds for a normal check: list x the network mix
        missed Andrew's realized $/job by more than 2x on 5 of 18 model-days outside gemma and
        gpt-oss, so a model this Mac never served carries ln 4 and a 3x public ratio is weak
        evidence there. Andrew (Sep 28): a Mac that can hold it should still try it when demand is
        higher than usual and sustained. qwen3.5-35b fits this 48 GB Mac and its demand has been 3x
        usual for two hours, so the trial bound (ln 2) arms it once; it pays, and a week passes
        before another trial."""
        self.regime(3.0)
        self.demand_history(-2 * 3600, 7 * 3600)
        one = self.ne.relative(QWEN, GEMMA, now=self.now, basis='list')
        self.assertAlmostEqual(one['estimate']['usd_error'], math.log(4))
        self.assertAlmostEqual(one['home_estimate']['usd_error'], 0.17, places=3)  # calibrated home
        self.assertFalse(one['calibrated'])
        self.assertLess(one['low'], 1)
        a, b = one['estimate'], one['home_estimate']
        trial = math.hypot(a['req_spread'], math.log(2), b['req_spread'], b['usd_error'])
        self.assertAlmostEqual(one['trial_low'], one['ratio'] * math.exp(-trial))
        self.assertGreater(one['trial_low'], 1.4)  # the same ratio at the ln 2 margin
        row = {r['model']: r for r in fresh_decision(self)['manager']['evidence']['rows']}[QWEN]
        self.assertAlmostEqual(row['ratio'], 3.0, places=3)
        self.assertLess(row['ratioLow'], 1)
        self.assertEqual((row['why'], row['trial'], row['demandRatio']), (None, True, 3.0))
        self.run_for(6)
        m = self.o.state['manager']
        self.assertEqual([c[0] for c in self.commands], [QWEN, GEMMA])
        last = m['lastExcursion']
        self.assertEqual((last['target'], last['endCode']), (QWEN, 'faded'))
        self.assertTrue(last['reason'].startswith('a trial of a model this Mac has not served lately'))
        rows = self.o.manager.excursions.ledger.records('acct', self.live['device'], 0)
        self.assertEqual(len(rows), 1)
        self.assertGreater(rows[0]['gainUsd'], 0)  # $0.19/h against gemma's $0.108/h: no lesson
        self.assertEqual(fresh_decision(self)['manager']['evidence']['lessons'], [])
        # Replay the same regime and demand without the fade's hour of cooldown. The trial's paid
        # jobs (>= OWN_MIN_JOBS) now calibrate qwen3.5-35b from this Mac's own credits: no trial
        # bound any more.
        m.pop('excursionHolds')
        self.o.manager.excursions.invalidate()
        self.ne.own_at = None
        row = {r['model']: r for r in fresh_decision(self)['manager']['evidence']['rows']}[QWEN]
        self.assertEqual((row['calibrated'], row['trialLow'], row['trial']), (True, None, False))
        # Without those jobs it would still wait a week for another trial.
        with self.h.lock:
            self.h.db.execute('DELETE FROM opt_credits WHERE model=?', (QWEN,))
            self.h.db.commit()
        self.ne.own_at = None
        self.o.manager.excursions.invalidate()
        row = {r['model']: r for r in fresh_decision(self)['manager']['evidence']['rows']}[QWEN]
        self.assertEqual((row['calibrated'], row['why']), (False, 'tried this week'))

    def test_a_3x_regime_without_sustained_demand_does_not_arm(self):
        self.regime(3.0)
        row = {r['model']: r for r in fresh_decision(self)['manager']['evidence']['rows']}[QWEN]
        self.assertEqual((row['why'], row['demandRatio']), ('demand not high enough', None))
        self.demand_history(-3600, 0)  # one hour at 3x usual, then usual again
        row = {r['model']: r for r in fresh_decision(self)['manager']['evidence']['rows']}[QWEN]
        self.assertEqual((row['why'], row['demandRatio']), ('demand not high enough', 1.0))
        self.run_for(2.5)
        self.assertEqual(self.commands, [])

    def test_own_jobs_calibrate_the_model_and_replace_the_trial_bar(self):
        """With OWN_MIN_JOBS (50) own jobs on qwen3.5-35b in 7 days its $/request error comes from
        this Mac's credits (|ln(own / list)| + 0.17), no trial bound is needed or offered, and a
        3x regime arms on a normal check without the demand gate."""
        self.regime(3.0)
        with self.h.lock:
            self.h.db.executemany(
                'INSERT INTO opt_credits VALUES(?,?,?,?,?,?,?)',
                [('acct', 30_000_000 + i, 'p', self.now - 86400, QWEN, 106, 10) for i in range(60)],
            )
            self.h.db.commit()
        self.ne.own_at = None
        one = self.ne.relative(QWEN, GEMMA, now=self.now, basis='list')
        self.assertTrue(one['calibrated'])
        self.assertAlmostEqual(one['estimate']['usd_error'], 0.17, places=3)
        self.assertIsNone(one['trial_low'])
        self.assertGreater(one['low'], 2)
        row = {r['model']: r for r in fresh_decision(self)['manager']['evidence']['rows']}[QWEN]
        self.assertEqual((row['calibrated'], row['trial'], row['trialLow'], row['why']), (True, False, None, None))
        self.run_for(2.5)
        self.assertEqual([c[0] for c in self.commands], [QWEN])
        self.assertTrue(self.o.state['manager']['excursion']['reason'].startswith('public data for Macs'))

    def test_poor_realized_pay_ends_it_after_an_hour_past_its_ramp_and_holds_it_a_day(self):
        self.qwen_rate = 0.05
        seen = self.run_for(3.5, pay=0.05)
        m = self.o.state['manager']
        self.assertEqual([c[0] for c in self.commands], [QWEN, GEMMA])
        last = m['lastExcursion']
        self.assertEqual(last['endCode'], 'early-exit')
        self.assertIn(
            'it paid $0.050/h after warming up; home would likely have paid at least $0.10',
            last['endReason'],
        )
        # An hour of settled pay after the ramp (5 min to the first job + 25 min).
        ramp = ex.ramp_seconds(QWEN, {})
        self.assertGreaterEqual(last['endingAt'] - last['leftAt'], ramp + 3600 + 120)
        self.assertLess(last['endingAt'] - last['leftAt'], ramp + 3600 + 420)
        self.assertAlmostEqual(m['excursionHolds'][QWEN]['until'] - last['endedAt'], 86400)
        view = self.o.last_demand_decision['manager']
        self.assertEqual({r['model']: r['why'] for r in view['evidence']['rows']}[QWEN], 'cooling down')
        self.assertLess(self.o.manager.excursions.ledger.records('acct', self.live['device'], 0)[0]['gainUsd'], 0)
        # Its ledger row teaches a lesson: stronger evidence for two weeks (x2 now, fading).
        later = self.now + 4 * 3600
        view = Optimizer.demand_decision(self.o, later)['manager']['evidence']
        (lesson,) = view['lessons']
        self.assertEqual((lesson['model'], lesson['code'], lesson['home']), (QWEN, 'early-exit', GEMMA))
        self.assertAlmostEqual(lesson['factor'], 2.0, places=1)
        self.assertAlmostEqual(lesson['until'], m['lastExcursion']['endedAt'] + 14 * 86400, delta=300)
        self.assertIn('ended early, paying less than %s; needs stronger evidence until' % GEMMA, lesson['text'])

    def test_kill_switch_pauses_excursions_and_pushes_a_notice(self):
        ledger = self.o.manager.excursions.ledger
        device = self.live['device']
        for day in (9, 4):
            ledger.add(
                'acct',
                device,
                {
                    'startedAt': self.now - day * 86400,
                    'endedAt': self.now - day * 86400 + 7200,
                    'model': QWEN,
                    'from': GEMMA,
                    'predictedUsdPerHour': 0.2,
                    'realizedUsdPerHour': 0.05,
                    'homeCounterfactualUsdPerHour': 0.108,
                    'gainUsd': -0.116,
                    'predictedGainUsd': 0.184,
                    'endCode': 'early-exit',
                    'endReason': 'x',
                },
            )
        self.o.state['manager']['unbooked'] = [
            {
                'target': QWEN,
                'from': GEMMA,
                'startedAt': self.now - 7200,
                'leftAt': self.now - 7300,
                'endedAt': self.now - 3600,
                'predictedUsdPerHour': 0.2,
                'endCode': 'faded',
                'endReason': 'public evidence faded',
                'homeUsdPerHour': 0.108,
                'homeNetworkUsdPerHour': None,
            }
        ]
        self.credits(self.now - 7300, self.now - 3600, 0.15)  # +0.04 $/h: not enough
        self.o.manager.book(self.now)
        m = self.o.state['manager']
        self.assertNotIn('unbooked', m)
        self.assertNotIn('until', m['excursionsOff'])
        self.assertEqual(
            self.events('manager-notice')[-1],
            'The last 3 excursions earned $0.21 less than staying home. '
            'To try again, turn excursions off and back on.',
        )
        view = fresh_decision(self)['manager']
        self.assertEqual(view['ledger']['count'], 3)
        self.assertEqual((view['ledger']['enabled'], view['ledger']['disabledBy']), (False, 'ledger'))
        self.assertEqual(view['evidence']['gate'], 'paused')
        # A week later, and after the background bookkeeping runs, it is still off.
        self.o.manager.remember(fresh_decision(self), self.now + 8 * 86400)
        self.assertIn('excursionsOff', self.o.state['manager'])
        # Saving other settings keeps the pause; turning excursions off and on lifts it.
        for changes in ({'maxSwitchesPerDay': 20}, {'managerExcursions': 0}):
            self.o.control_action(self.policy_request(changes), 'mac')
            self.assertIn('excursionsOff', self.o.state['manager'])
        self.o.control_action(self.policy_request({'managerExcursions': 1}), 'phone')
        m = self.o.state['manager']
        self.assertNotIn('excursionsOff', m)
        self.assertGreaterEqual(m['excursionLedgerSince'], self.now)
        self.assertEqual(self.o.state['demandPolicy']['managerExcursions'], 1)

    def policy_request(self, changes):
        self.at(0)
        return {
            'action': 'update-policy',
            'expectedControl': self.o.control_version(),
            'demandPolicy': changes,
        }


class ControlEndpointTests(tm.Harness):
    """The 3-second control poll: manager summary, pin release and bound warm-up."""

    def setUp(self):
        super().setUp()
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

    def project(self):
        return self.control.projection(self.now)

    def decision(self, at=None, **view):
        self.o.last_demand_decision = {
            'at': self.now if at is None else at,
            'currentModel': 'a',
            'manager': {'action': 'hold', 'reason': 'Holding home model a.', **view},
        }

    def test_summary_carries_what_the_manager_card_needs(self):
        arming = {'model': 'b', 'checks': 1, 'needed': 2, 'checkSeconds': 3600, 'neededSeconds': 3600, 'since': self.now - 60, 'ratio': 1.9}
        self.decision(arming=arming)
        summary = self.project()['manager']
        self.assertEqual(
            summary,
            {
                'at': self.now,
                'active': True,
                'home': 'a',
                'homeSource': 'history',
                'homeNotice': None,
                'pinned': False,
                'action': 'hold',
                'reason': 'Holding home model a.',
                'watchdog': None,
                'recovery': None,
                'excursion': None,
                'arming': {k: v for k, v in arming.items() if k != 'ratio'},
            },
        )
        self.assertEqual(self.control.snapshot()['manager'], summary)
        # A decision more than a minute old says nothing about now.
        self.decision(at=self.now - 120, arming=arming)
        summary = self.project()['manager']
        self.assertEqual((summary['action'], summary['reason'], summary['arming']), (None, None, None))

    def test_summary_reports_the_excursion_with_running_pay_recovery_and_watchdog(self):
        m = self.o.state['manager']
        m['excursion'] = {'target': 'b', 'from': 'a', 'startedAt': self.now - 4000, 'predictedUsdPerHour': 0.2}
        m['recovery'] = {'failedTarget': 'c', 'previous': 'a', 'at': self.now - 30, 'attempts': 0, 'interrupted': True}
        self.decision(excursion={'target': 'b', 'realizedUsdPerHour': 0.17}, action='excursion')
        summary = self.project()['manager']
        self.assertEqual(
            summary['excursion'],
            {'target': 'b', 'startedAt': self.now - 4000, 'predictedUsdPerHour': 0.2, 'realizedUsdPerHour': 0.17, 'endReason': None},
        )
        self.assertEqual(summary['recovery'], {'failedTarget': 'c', 'previous': 'a', 'at': self.now - 30, 'attempts': 0})
        self.o.manager.dark_since = self.now - 700
        self.o.manager.dark_text = 'a has not been ready since 1:00 PM'
        summary = self.project()['manager']
        self.assertEqual(summary['watchdog'], {'darkSince': self.now - 700, 'reason': 'a has not been ready since 1:00 PM'})
        self.assertEqual((summary['action'], summary['reason']), ('recover', 'a has not been ready since 1:00 PM'))
        self.o.state['demandPolicy'] = policy({'managerStrategy': 0})
        self.assertIsNone(self.project()['manager'])

    def test_warm_up_is_shown_only_for_the_serving_model_and_session(self):
        self.assertEqual(self.project()['warmup'], {'status': 'ready'})
        waiting = {'status': 'waiting', 'detail': 'Waiting to check the serving model’s warm-up.'}
        warm = copy.deepcopy(self.o.warmup)
        self.o.warmup = {**warm, 'session': 'an-older-session'}
        self.assertEqual(self.project()['warmup'], waiting)
        self.o.warmup = {**warm, 'model': 'b'}
        self.assertEqual(self.project()['warmup'], waiting)
        self.o.warmup = warm
        self.o.raw['warm_models'] = []  # unloaded after warm-up
        self.assertEqual(self.project()['warmup']['status'], 'cold')
        self.provider_status = 'stopped'
        self.assertEqual(self.project()['warmup'], {})

    def release(self, **changes):
        view = self.control.snapshot()
        data = {'action': 'release-pin', 'requestId': str(uuid.uuid4()), 'expectedControl': view['controlVersion'], **changes}
        return data, self.control.action(data)

    def test_release_pin_returns_control_of_the_home_to_the_manager(self):
        self.o.state['manager']['home'] = manager.pin('b', self.now - 600)
        self.project()
        self.assertTrue(self.control.snapshot()['manager']['pinned'])
        with self.assertRaisesRegex(ValueError, 'status changed'):
            self.release(expectedControl='stale')
        data, result = self.release()
        self.assertNotIn('home', self.o.state['manager'])
        self.assertEqual(self.o.state['mode'], 'demand')
        self.assertEqual(result['lastRequestId'], data['requestId'])
        self.assertFalse(result['manager']['pinned'])
        self.assertIn('Your pick b is released', self.events('manager')[-1])
        self.assertEqual(self.control.action(data)['lastRequestId'], data['requestId'])  # a retry
        with self.assertRaisesRegex(ValueError, 'already used'):
            self.control.action({**data, 'expectedControl': 'other'})
        self.project()
        with self.assertRaisesRegex(ValueError, 'No model is pinned'):
            self.release()
        with self.assertRaisesRegex(ValueError, 'only a request ID'):
            self.release(models=['a'])
        # The next decision picks home from history and returns to it (a normal move).
        legacy = legacy_upgrade()
        legacy['currentModel'] = 'b'
        self.o.demand_auto.evaluate = Mock(return_value=legacy)
        self.o.demand_auto.evidence = Mock(
            # 96 h: 72 h ending late in the evening leave only 2 full-day stints (a flake).
            return_value=({'a': {'minutes': tm.minutes(0.108, 96, self.now)}, 'b': {'minutes': tm.minutes(0.02, 96, self.now)}}, {})
        )
        self.o.raw.update(advertised_models=['b'])
        d = Optimizer.demand_decision(self.o, self.now)
        self.assertEqual((d['target'], d['kind'], d['manager']['home']['source']), ('a', 'home', 'history'))

    def test_release_needs_the_manager_on_and_no_change_in_flight(self):
        self.o.state['manager']['home'] = manager.pin('b', self.now - 600)
        self.o.state['manager']['resume'] = {'id': 'x', 'model': 'b'}
        self.project()
        with self.assertRaisesRegex(ValueError, 'Wait for the current model change'):
            self.release()
        self.o.state['manager'].pop('resume')
        self.o.state['mode'] = 'observe'
        self.project()
        with self.assertRaisesRegex(ValueError, 'Turn the manager on'):
            self.release()
        self.assertEqual(self.o.state['manager']['home']['source'], 'manual')


if __name__ == '__main__':
    unittest.main()
