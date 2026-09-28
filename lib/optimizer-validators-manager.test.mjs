// The optimizer response and control validators accept the manager strategy's
// fields (native/manager.py view(), managerStrategy / managerExcursions).
import test from 'node:test';
import assert from 'node:assert/strict';
import { validOptimizerResponse } from './optimizer-response.ts';
import {
  validOptimizerControl,
  automaticRequest,
  readAutomaticRequest,
  releasePinRequest,
  keepCurrentRequest,
} from './optimizer-control.ts';

const snapshot = () => ({
  at: 100,
  mode: 'demand',
  status: 'optimizing',
  detail: 'Automatic control is on.',
  selected: ['a', 'b'],
  blockHours: 2,
  busy: false,
  canManage: true,
  controlVersion: 'revision',
  models: [{ id: 'a', name: 'A', available: true, evidence: {} }],
  events: [],
});
const policy = {
  minRunMinutes: 30,
  confirmationMinutes: 5,
  improvementPercent: 20,
  planningMinutes: 60,
  minimumNetUsd: 0.02,
  maxSwitchesPerDay: 12,
  maxDowntimeMinutes: 30,
  memoryHeadroomGB: 1,
  managerStrategy: 1,
  managerExcursions: 0,
};
const demand = (over = {}) => ({
  at: 100,
  enabled: true,
  scanAt: 100,
  scanStatus: 'ready',
  reason: 'Holding home model a.',
  currentModel: 'a',
  target: null,
  policy,
  savedPolicy: policy,
  planningMinutes: 60,
  baseline: null,
  limits: {
    switchesUsed: 0,
    switchLimit: 12,
    downtimeMinutesUsed: 0,
    downtimeMinutesLimit: 30,
    nextRunAt: 100,
  },
  opportunities: [],
  runs: [],
  confirmation: null,
  ...over,
});
const manager = {
  strategy: 'manager',
  active: true,
  action: 'hold',
  reason: 'Holding home model a.',
  home: { model: 'a', source: 'history', at: 90, usdPerHour: 0.21, hours: 40 },
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
  blocked: [{ model: 'b', until: 200 }],
  lastGood: { model: 'a', at: 99 },
  lastAction: null,
  evidence: {
    cell: 'M5 Pro|48',
    updatedAt: 99,
    home: 'a',
    homeUsdPerHour: 0.2,
    rows: [
      {
        model: 'b',
        usdPerHour: 0.36,
        low: 0.3,
        high: 0.4,
        ratio: 1.8,
        ratioLow: 1.4,
        providers: 5,
        source: 'cell',
        eligible: true,
        why: null,
      },
    ],
  },
  arming: { model: 'b', since: 50, checks: 1, needed: 2 },
  ledger: {
    days: 14,
    count: 0,
    gainUsd: 0,
    predictedUsd: 0,
    enabled: true,
    disabledReason: null,
  },
};
// A manager move in the switch history (demand_optimizer.begin_manager payload).
const run = {
  id: 7,
  at: 95,
  model: 'b',
  result: 'switched',
  downtime: 40,
  decision: {
    kind: 'excursion',
    reason: 'Excursion to b: evidence.',
    manager: { action: 'excursion' },
  },
};

test('manager view, excursion policy key and manager runs pass the response validator', () => {
  const valid = { ...snapshot(), demandAuto: demand({ manager, runs: [run] }) };
  assert.equal(validOptimizerResponse(valid), true);
  // Older backends have no manager keys at all.
  const { managerStrategy, managerExcursions, ...legacy } = policy;
  assert.ok(managerStrategy === 1 && managerExcursions === 0);
  assert.equal(
    validOptimizerResponse({
      ...snapshot(),
      demandAuto: demand({ policy: legacy, savedPolicy: legacy }),
    }),
    true,
  );
  // An odd manager shape is ignored when rendering, never a reason to blank the page.
  assert.equal(
    validOptimizerResponse({
      ...snapshot(),
      demandAuto: demand({ manager: 'x' }),
    }),
    true,
  );
});

test('manager policy switches outside 0/1 are rejected like other choices', () => {
  for (const bad of [
    { policy: { ...policy, managerExcursions: 2 } },
    { savedPolicy: { ...policy, managerStrategy: 0.5 } },
  ])
    assert.equal(
      validOptimizerResponse({ ...snapshot(), demandAuto: demand(bad) }),
      false,
    );
});

test('manager policy keys pass through the control status and a first On', () => {
  const state = {
    at: 100,
    controlVersion: 'control-one',
    providerVersion: 'provider-one',
    currentModel: 'a',
    providerRunning: true,
    actualMode: 'observe',
    hasSavedPlan: false,
    firstPlan: true,
    selected: [],
    demandPolicy: {
      targetUsdPerHour: 0.12,
      managerStrategy: 1,
      managerExcursions: 0,
    },
    models: [
      { id: 'a', name: 'A', available: true },
      { id: 'b', name: 'B', available: true },
    ],
    warmup: { status: 'cold', detail: 'Loads on the first request' },
    automatic: {
      mode: 'manual',
      phase: 'manual',
      detail: 'Manual',
      canEnable: true,
    },
    operation: null,
    lastRequestId: null,
  };
  assert.equal(validOptimizerControl(state), true);
  const id = 'd9bd3d3e-91bd-485b-b470-0ed00daab0af';
  const request = automaticRequest(state, true, id);
  assert.deepEqual(request.demandPolicy, state.demandPolicy);
  assert.deepEqual(readAutomaticRequest(JSON.stringify(request)), request);
  assert.equal(
    validOptimizerControl({
      ...state,
      demandPolicy: { managerExcursions: 'on' },
    }),
    false,
  );
  // The compact manager summary: an object or null; its fields are read defensively.
  const summary = { at: 100, active: true, home: 'a', pinned: true, extra: [1] };
  assert.equal(validOptimizerControl({ ...state, manager: summary }), true);
  assert.equal(validOptimizerControl({ ...state, manager: null }), true);
  assert.equal(validOptimizerControl({ ...state, manager: 'on' }), false);
  assert.equal(validOptimizerControl({ ...state, manager: [summary] }), false);
  assert.deepEqual(releasePinRequest(state, id), {
    action: 'release-pin',
    requestId: id,
    expectedControl: 'control-one',
  });
  // "Keep current" on a planned home change names the model it keeps (native keep_current).
  assert.deepEqual(keepCurrentRequest(state, 'gpt-oss-20b', id), {
    action: 'keep-current',
    requestId: id,
    expectedControl: 'control-one',
    model: 'gpt-oss-20b',
  });
});
