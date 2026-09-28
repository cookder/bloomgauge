import test from 'node:test';
import assert from 'node:assert/strict';
import {
  readManager,
  optimizerStrategy,
  loadStrategy,
  excursionsSetting,
  withoutCircularHint,
  pausedReason,
  managerHeadline,
  managerSentence,
  homeSource,
  readiness,
  excursionProgress,
  armingText,
  ledgerText,
  cellLabel,
  evidenceTable,
  lastPause,
  usd,
  readManagerSummary,
  withSummary,
  planMinimum,
  excursionsInDay,
  EXCURSIONS_PER_DAY,
} from './optimizer-manager.ts';

const now = 1_790_000_000;
const GEMMA = 'gemma-4-26b-qat-4bit';
const QWEN = 'EigenLabs/Qwen3.8-27B-4bit-mtp';
const label = (id) =>
  ({ [GEMMA]: 'Gemma 4 26B', [QWEN]: 'Qwen 3.8 27B' })[id] ?? id;
// native/manager.py view() today, before excursions arm.
const view = (over = {}) => ({
  strategy: 'manager',
  active: true,
  action: 'hold',
  reason: `Holding home model ${GEMMA} (best paid on this Mac: $0.214 per ready hour over the last 30 days).`,
  home: {
    model: GEMMA,
    source: 'history',
    at: now - 86400,
    usdPerHour: 0.214,
    hours: 61.2,
  },
  pinned: false,
  proposal: null,
  excursion: null,
  lastExcursion: null,
  recovery: null,
  watchdog: {
    darkSince: null,
    windowSeconds: 600,
    attempts: 0,
    nextAt: null,
    reason: null,
  },
  retryAt: null,
  blocked: [],
  lastGood: { model: GEMMA, at: now - 30 },
  lastAction: null,
  ...over,
});

test('the manager view reads today’s fields and every new one as optional', () => {
  const v = readManager(view());
  assert.equal(v.strategy, 'manager');
  assert.equal(v.home.model, GEMMA);
  assert.equal(v.evidence, null);
  assert.equal(v.arming, null);
  assert.equal(v.ledger, null);
  const full = readManager(
    view({
      evidence: {
        cell: 'M5 Pro|48',
        updatedAt: now - 60,
        home: GEMMA,
        homeUsdPerHour: 0.2,
        rows: [
          {
            model: QWEN,
            usdPerHour: 0.36,
            low: 0.3,
            high: 0.42,
            ratio: 1.8,
            ratioLow: 1.4,
            providers: 7,
            source: 'cell',
            eligible: true,
            why: null,
          },
          { model: 'x', usdPerHour: 'bad' },
          null,
        ],
      },
      arming: { model: QWEN, since: now - 3900, checks: 1, needed: 2 },
      ledger: {
        days: 14,
        count: 2,
        gainUsd: 0.31,
        predictedUsd: 0.4,
        enabled: true,
        disabledReason: null,
      },
    }),
  );
  assert.equal(full.evidence.cell, 'M5 Pro|48');
  assert.equal(full.evidence.rows.length, 2);
  assert.equal(full.evidence.rows[1].usdPerHour, null);
  assert.equal(full.arming.checks, 1);
  assert.equal(full.ledger.gainUsd, 0.31);
});

test('malformed parts are dropped, never the whole view', () => {
  const v = readManager(
    view({
      home: { source: 'history' },
      excursion: { target: 3 },
      watchdog: 'dark',
      blocked: [{ model: 'a', until: now + 60 }, { model: 'b' }, 'c'],
      evidence: { rows: 'none' },
      arming: { since: now },
      ledger: [1],
    }),
  );
  assert.equal(v.home, null);
  assert.equal(v.excursion, null);
  assert.equal(v.watchdog, null);
  assert.deepEqual(v.blocked, [{ model: 'a', until: now + 60 }]);
  assert.equal(v.evidence, null);
  assert.equal(v.arming, null);
  assert.equal(v.ledger, null);
  for (const bad of [null, [], 'manager', { strategy: 'legacy' }, {}])
    assert.equal(readManager(bad), null);
});

test('strategy comes from the manager view, else the saved policy', () => {
  assert.equal(optimizerStrategy({ manager: view() }), 'manager');
  assert.equal(
    optimizerStrategy({ savedPolicy: { managerStrategy: 1 } }),
    'manager',
  );
  assert.equal(
    optimizerStrategy({ savedPolicy: { managerStrategy: 0 } }),
    'legacy',
  );
  // A full saved policy without the key comes from a backend before the manager.
  assert.equal(
    optimizerStrategy({ savedPolicy: { minRunMinutes: 30 } }),
    'legacy',
  );
  // The raw control policy defaults a missing key to the manager on the backend.
  assert.equal(
    optimizerStrategy({ controlPolicy: { managerStrategy: 0 } }),
    'legacy',
  );
  assert.equal(optimizerStrategy({ controlPolicy: {} }), null);
  assert.equal(optimizerStrategy({}), null);
});

test('the control status names the strategy before /api/optimizer loads', () => {
  assert.equal(optimizerStrategy({ controlStrategy: 'manager' }), 'manager');
  // The live control answer wins over a 30 s old full view.
  assert.equal(
    optimizerStrategy({ controlStrategy: 'legacy', manager: view() }),
    'legacy',
  );
  assert.equal(
    optimizerStrategy({ controlStrategy: 'other', controlPolicy: {} }),
    null,
  );
});

test('the excursion setting is null until the backend has it', () => {
  assert.equal(excursionsSetting({ managerExcursions: 1 }), true);
  assert.equal(excursionsSetting({ managerExcursions: 0 }), false);
  assert.equal(excursionsSetting({ managerStrategy: 1 }), null);
  assert.equal(excursionsSetting(null), null);
});

test('directions to the page the reader is on are removed', () => {
  assert.equal(
    withoutCircularHint(
      'Wait for the current model to be Warm and ready before resuming. Open Optimizer → Overview to check progress.',
    ),
    'Wait for the current model to be Warm and ready before resuming.',
  );
  assert.equal(
    withoutCircularHint(
      'Darkbloom stopped. Automatic switching is paused. Choose a model and use Start in Optimizer → Overview to resume.',
    ),
    'Darkbloom stopped. Automatic switching is paused. Choose a model and use Start to resume.',
  );
  assert.equal(
    withoutCircularHint(
      'Automatic control is paused; choose a model in Optimizer → Overview or run `darkbloom restart`.',
    ),
    'Automatic control is paused; choose a model or run `darkbloom restart`.',
  );
  // "on the Mac" tells a phone where to go; keep it. Version numbers stay whole.
  const mac =
    'Open Optimizer → Overview on the Mac. With Darkbloom 0.9.9 or later, work finishes first.';
  assert.equal(withoutCircularHint(mac), mac);
  assert.equal(withoutCircularHint(null), '');
});

test('paused statistics give the backend’s concrete reason', () => {
  assert.equal(
    pausedReason(
      'Statistics paused · model switching, loading or pre-warming.',
    ),
    'model switching, loading or pre-warming',
  );
  assert.equal(pausedReason('Statistics paused · '), null);
  assert.equal(pausedReason(undefined), null);
  assert.equal(pausedReason('Provider is draining.'), 'Provider is draining');
});

test('headline and sentence follow the manager state; the reason is verbatim', () => {
  assert.equal(
    managerHeadline(readManager(view()), GEMMA, label, now).title,
    'Holding Gemma 4 26B',
  );
  assert.equal(
    managerHeadline(
      readManager(
        view({ pinned: true, home: { model: GEMMA, source: 'manual' } }),
      ),
      GEMMA,
      label,
      now,
    ).title,
    'Holding your pick, Gemma 4 26B',
  );
  const dark = readManager(
    view({
      reason: 'Holding home model gemma.',
      watchdog: {
        darkSince: now - 1200,
        reason:
          'qwen3.5-35b-a3b has not been ready since 5:44 AM: nothing is loaded and no work has arrived. If it is still not ready at 6:04 AM, Bloomkeeper restores gemma.',
        attempts: 0,
      },
    }),
  );
  assert.equal(
    managerHeadline(dark, 'qwen3.5-35b-a3b', label, now).tone,
    'attention',
  );
  assert.match(managerSentence(dark), /has not been ready since 5:44 AM/);
  assert.equal(managerSentence(readManager(view())), view().reason);
  const away = readManager(
    view({
      action: 'excursion',
      excursion: {
        target: QWEN,
        from: GEMMA,
        startedAt: now - 1800,
        predictedUsdPerHour: 0.36,
        reason: 'x',
        maxMinutes: 240,
      },
    }),
  );
  assert.match(
    managerHeadline(away, QWEN, label, now).title,
    /Trying Qwen 3\.8 27B/,
  );
  const recovering = readManager(
    view({
      recovery: {
        failedTarget: QWEN,
        previous: GEMMA,
        at: now - 60,
        attempts: 0,
      },
    }),
  );
  assert.match(
    managerHeadline(recovering, GEMMA, label, now).title,
    /Recovering after the switch to Qwen 3\.8 27B failed/,
  );
  assert.equal(
    managerHeadline(readManager(view({ active: false })), GEMMA, label, now)
      .title,
    'Manager is off',
  );
});

test('home source explains why this model is home', () => {
  assert.match(
    homeSource(readManager(view()).home, false),
    /Best paid on this Mac · \$0\.21 per ready hour over 61 h/,
  );
  assert.match(
    homeSource({ model: GEMMA, source: 'manual' }, true),
    /Your pick/,
  );
  assert.match(
    homeSource({ model: GEMMA, source: 'external' }, true),
    /outside Bloomkeeper/,
  );
  assert.match(homeSource(null, false), /Not chosen yet/);
});

test('readiness always has a reason and a since time when not ready', () => {
  assert.equal(
    readiness({ providerRunning: true, warmup: { status: 'ready' } }).state,
    'ready',
  );
  const cold = readiness({
    providerRunning: true,
    warmup: {
      status: 'cold',
      detail:
        'Darkbloom unloaded a model after warm-up. Its idle-memory policy is preserved.',
    },
    seen: now - 300,
  });
  assert.equal(cold.state, 'not-ready');
  assert.match(cold.reason, /unloaded a model after warm-up/);
  assert.equal(cold.since, now - 300);
  const dark = readiness({
    providerRunning: true,
    warmup: {
      status: 'waiting',
      detail: 'Waiting for fresh provider readiness readings.',
    },
    watchdog: {
      darkSince: now - 900,
      reason: 'qwen has not been ready since 5:44 AM',
    },
    seen: now - 30,
  });
  assert.equal(dark.since, now - 900);
  assert.equal(dark.reason, 'qwen has not been ready since 5:44 AM');
  const none = readiness({ providerRunning: true, warmup: null, seen: now });
  assert.match(none.reason, /No readiness reading/);
  assert.equal(readiness({ providerRunning: false }).state, 'stopped');
});

test('excursion progress, arming and ledger read as plain sentences', () => {
  const p = excursionProgress(
    {
      target: QWEN,
      startedAt: now - 1800,
      maxMinutes: 240,
      predictedUsdPerHour: 0.36,
      realizedUsdPerHour: 0.3,
    },
    now,
  );
  assert.equal(p.left, 240 * 60 - 1800);
  assert.equal(p.realized, 0.3);
  assert.equal(
    armingText(
      { model: QWEN, since: now - 3900, checks: 1, needed: 2, ratio: null },
      [{ model: QWEN, ratio: 1.8 }],
      label,
      now,
    ),
    'Qwen 3.8 27B has looked 1.8× better for your Mac for 1 h 5 min · 1 of 2 checks before a switch',
  );
  assert.equal(
    armingText(
      {
        model: QWEN,
        since: now - 600,
        checks: 1,
        needed: 2,
        ratio: 1.9,
        checkSeconds: 3600,
        neededSeconds: 3600,
      },
      [],
      label,
      now,
    ),
    'Qwen 3.8 27B has looked 1.9× better for your Mac for 10 min · 1 of 2 hourly checks before a switch',
  );
  assert.equal(
    ledgerText(
      { days: 14, count: 2, gainUsd: 0.31, predictedUsd: null, enabled: true },
      GEMMA,
      label,
    ),
    'Switches in the last 14 days: 2, +$0.31 vs staying on Gemma 4 26B',
  );
  assert.equal(
    ledgerText({ days: 14, count: 0, gainUsd: 0, enabled: true }, GEMMA, label),
    'Switches in the last 14 days: 0',
  );
  assert.equal(
    ledgerText(
      {
        enabled: false,
        disabledReason: 'Two excursions in a row paid less than home.',
      },
      GEMMA,
      label,
    ),
    'Excursions are off: Two excursions in a row paid less than home',
  );
  assert.equal(usd(-0.05), '−$0.050');
});

test('evidence rows are best first with the home model marked', () => {
  assert.equal(cellLabel('M5 Pro|48'), 'M5 Pro · 48 GB');
  assert.equal(cellLabel(null), 'this Mac’s hardware class');
  const rows = evidenceTable(
    readManager(
      view({
        evidence: {
          cell: 'M5 Pro|48',
          rows: [
            { model: GEMMA, usdPerHour: 0.2 },
            { model: 'none', usdPerHour: null },
            { model: QWEN, usdPerHour: 0.36 },
          ],
        },
      }),
    ).evidence,
    GEMMA,
  );
  assert.deepEqual(
    rows.map((r) => [r.model, r.home]),
    [
      [QWEN, false],
      [GEMMA, true],
      ['none', false],
    ],
  );
});

test('the latest pause is reported until automatic control is on again', () => {
  const paused = {
    at: now - 600,
    kind: 'paused',
    model: QWEN,
    detail: 'Restoring after the failed switch did not work twice.',
  };
  assert.equal(lastPause([paused]).detail, paused.detail);
  assert.equal(
    lastPause([paused, { at: now - 60, kind: 'resumed', detail: 'On' }]),
    null,
  );
  assert.equal(
    lastPause([{ at: now - 900, kind: 'resumed', detail: '' }, paused]).at,
    now - 600,
  );
  assert.equal(lastPause(null), null);
});

test('the 3-second control summary refreshes the live facts of the 30-second view', () => {
  const full = readManager(
    view({
      arming: { model: QWEN, since: now - 60, checks: 1, needed: 2, ratio: 1.9 },
      evidence: { cell: 'M5 Pro|48', rows: [{ model: QWEN, ratio: 1.9 }] },
      ledger: { days: 14, count: 1, gainUsd: 0.2, enabled: true },
    }),
  );
  // native/manager.py control_summary(), mid-excursion, a minute later.
  const summary = readManagerSummary({
    at: now + 60,
    active: true,
    home: GEMMA,
    homeSource: 'history',
    pinned: false,
    action: 'excursion',
    reason: `Excursion to ${QWEN}: public data`,
    watchdog: null,
    recovery: null,
    excursion: {
      target: QWEN,
      startedAt: now,
      predictedUsdPerHour: 0.36,
      realizedUsdPerHour: 0.3,
      endReason: null,
    },
    arming: null,
    unknownField: 1,
  });
  const merged = withSummary(full, summary);
  assert.equal(merged.action, 'excursion');
  assert.equal(merged.reason, `Excursion to ${QWEN}: public data`);
  assert.equal(merged.excursion.target, QWEN);
  assert.equal(merged.excursion.realizedUsdPerHour, 0.3);
  assert.equal(merged.arming, null);
  assert.equal(merged.home, full.home); // same home: the full record stays
  assert.equal(merged.evidence, full.evidence);
  assert.equal(merged.ledger, full.ledger);
  // A released pin shows at once; a dark Mac shows the watchdog's time and reason.
  const pinned = readManager(
    view({ pinned: true, home: { model: QWEN, source: 'manual' } }),
  );
  const released = withSummary(pinned, { ...summary, excursion: null });
  assert.equal(released.pinned, false);
  assert.equal(released.home.model, GEMMA);
  const dark = withSummary(full, {
    ...summary,
    action: 'recover',
    reason: 'Gemma has not been ready since 1:00 PM',
    watchdog: { darkSince: now - 700, reason: 'Gemma has not been ready since 1:00 PM' },
    excursion: null,
  });
  assert.equal(dark.watchdog.darkSince, now - 700);
  assert.equal(dark.watchdog.windowSeconds, 600);
  // No summary (legacy, stale control status): the full view as it was.
  assert.equal(withSummary(full, null), full);
  assert.equal(withSummary(null, summary), null);
  assert.equal(readManagerSummary(null), null);
  assert.equal(readManagerSummary({ excursion: { target: '' } }).excursion, null);
});

test('the plan minimum follows the strategy being saved, as the backend checks it', () => {
  // optimizer.py save: manager.enabled(saved policy + edits) decides one or two models.
  assert.equal(planMinimum('legacy', { managerStrategy: 1 }), 1);
  assert.equal(planMinimum('manager', { managerStrategy: 0 }), 2);
  assert.equal(planMinimum('manager', {}), 1);
  assert.equal(planMinimum('legacy', {}), 2);
  assert.equal(planMinimum(null, {}), 2);
});

test('under the manager the daily limit counts only excursions, capped at three', () => {
  const run = (at, kind) => ({ id: at, at, decision: { kind } });
  const runs = [
    run(now - 60, 'excursion'),
    run(now - 120, 'home'),
    run(now - 600, 'stall-recovery'),
    run(now - 3600, 'excursion'),
    run(now - 90000, 'excursion'), // older than 24 h
    run(now - 7200, 'upgrade'), // a legacy run
    { id: 1, at: 'x' },
  ];
  assert.deepEqual(excursionsInDay(runs, 24, now), { used: 2, limit: 3 });
  assert.equal(EXCURSIONS_PER_DAY, 3);
  // A lower "Maximum automatic moves" is the limit.
  assert.deepEqual(excursionsInDay(runs, 1, now), { used: 2, limit: 1 });
  assert.deepEqual(excursionsInDay(undefined, 4, now), { used: 0, limit: 3 });
});

test('the manager daily cap matches the backend', async () => {
  const { readFile } = await import('node:fs/promises');
  const python = await readFile(
    new URL('../native/excursions.py', import.meta.url),
    'utf8',
  );
  assert.equal(
    Number(python.match(/^MAX_PER_DAY = (\d+)/m)[1]),
    EXCURSIONS_PER_DAY,
  );
});

test('cards without /api/optimizer read the strategy from the control status', async () => {
  const get = (value) => async (url) => {
    assert.equal(url, '/api/optimizer/control');
    return value;
  };
  assert.equal(await loadStrategy(get({ strategy: 'manager' })), 'manager');
  assert.equal(
    await loadStrategy(get({ demandPolicy: { managerStrategy: 0 } })),
    'legacy',
  );
  assert.equal(await loadStrategy(get(null)), null);
  assert.equal(
    await loadStrategy(async () => {
      throw Error('offline');
    }),
    null,
  );
});
