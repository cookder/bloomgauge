import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import {
  keepReadablePlan,
  readOptimizerResponse,
  readRunView,
  savedPlanUnreadable,
  validOptimizerResponse,
  withoutInvalidHistory,
} from '../lib/optimizer-response.ts';
import { pulseRunStatus } from '../lib/pulse-run-status.ts';

const snapshot = () => ({
  at: 100,
  mode: 'observe',
  status: 'ready',
  detail: 'Recording observations',
  selected: ['model-a'],
  blockHours: 2,
  busy: false,
  canManage: true,
  controlVersion: 'revision',
  models: [
    {
      id: 'model-a',
      name: 'Model A',
      available: true,
      evidence: {
        hours: 4,
        usd: 0.6,
        usdPerHour: 0.15,
        timeSlots: [{ weekend: false, hour: 12, hours: 4, usdPerHour: 0.15 }],
      },
    },
  ],
  combinations: {
    candidates: [{ id: 'pair', models: ['model-a', 'model-b'] }],
    results: [],
    plan: null,
  },
  events: [{ at: 99, detail: 'Provider became busy' }],
  warmup: { status: 'ready', detail: 'Ready for work' },
});

test('optimizer response preserves legitimate empty and unknown evidence', () => {
  assert.equal(validOptimizerResponse(snapshot()), true);
  const value = snapshot();
  value.models[0].evidence = { usdPerHour: null, hours: 0 };
  assert.equal(validOptimizerResponse(value), true);
  value.models = [];
  assert.equal(validOptimizerResponse(value), true);
});

test('incomplete success payloads cannot replace the ready optimizer', () => {
  for (const value of [
    null,
    {},
    { ...snapshot(), models: null },
    { ...snapshot(), selected: 'model-a' },
    { ...snapshot(), events: [null] },
    { ...snapshot(), blockHours: null },
  ]) {
    assert.equal(validOptimizerResponse(value), false);
  }
});

test('malformed nested model and combination evidence is rejected before rendering', () => {
  const malformed = [
    { models: [{ id: 'a', name: 'A', available: true, evidence: null }] },
    {
      models: [
        { id: 'a', name: 'A', available: true, evidence: { timeSlots: {} } },
      ],
    },
    {
      combinations: {
        candidates: [{ id: 'pair', models: 'a+b' }],
        results: [],
      },
    },
    {
      combinations: {
        candidates: [],
        results: [
          { id: 'pair', name: 'Pair', evidence: { perModel: { a: null } } },
        ],
      },
    },
  ];
  for (const changed of malformed)
    assert.equal(validOptimizerResponse({ ...snapshot(), ...changed }), false);
});

const demand = () => ({
  at: 100,
  enabled: true,
  scanAt: 100,
  scanStatus: 'ready',
  reason: 'Comparing',
  currentModel: 'a',
  target: null,
  policy: {
    minRunMinutes: 60,
    confirmationMinutes: 10,
    improvementPercent: 20,
    planningMinutes: 60,
    minimumNetUsd: 0.02,
    maxSwitchesPerDay: 4,
    maxDowntimeMinutes: 20,
    memoryHeadroomGB: 2,
  },
  planningMinutes: 60,
  baseline: null,
  limits: {
    switchesUsed: 0,
    switchLimit: 4,
    downtimeMinutesUsed: 0,
    downtimeMinutesLimit: 20,
    nextRunAt: 100,
  },
  opportunities: [],
  runs: [],
  confirmation: null,
});
test('baseline learning validates policy and sample coverage without requiring old responses to have it', () => {
  const base = {
    ...snapshot(),
    mode: 'demand',
    demandAuto: {
      ...demand(),
      baselineLearning: { enabled: true, trialsUsed: 1 },
    },
  };
  assert.equal(validOptimizerResponse(base), true);
  for (const value of [
    { enabled: 'true', trialsUsed: 1 },
    { enabled: true, trialsUsed: null },
  ])
    assert.equal(
      validOptimizerResponse({
        ...base,
        demandAuto: { ...base.demandAuto, baselineLearning: value },
      }),
      false,
    );
  assert.equal(
    validOptimizerResponse({
      ...base,
      demandAuto: {
        ...base.demandAuto,
        policy: { ...demand().policy, baselineLearningEnabled: 5 },
      },
    }),
    false,
  );
});
test('data gathering status is optional and rejects malformed windows', () => {
  const base = { ...snapshot(), mode: 'demand', demandAuto: { ...demand() } };
  for (const value of [
    undefined,
    { active: false },
    {
      active: true,
      startedAt: 1,
      endsAt: 86401,
      seconds: 86400,
      remainingSeconds: 3600,
    },
  ])
    assert.equal(
      validOptimizerResponse({
        ...base,
        demandAuto: { ...base.demandAuto, dataGathering: value },
      }),
      true,
    );
  for (const value of [
    {},
    { active: 'yes' },
    { active: true, startedAt: 1, endsAt: null, remainingSeconds: 5 },
    [],
  ])
    assert.equal(
      validOptimizerResponse({
        ...base,
        demandAuto: { ...base.demandAuto, dataGathering: value },
      }),
      false,
    );
});
test('recorded decision history and partial trial fields reject malformed containers', () => {
  const entry = {
    id: 1,
    at: 90,
    updated: 100,
    observedSeconds: 10,
    mode: 'demand',
    model: 'a',
    target: 'b',
    phase: 'waiting',
    code: 'memory',
    reason: 'Waiting for memory',
    sourceAt: 95,
    confirmationSeconds: 300,
    requiredSeconds: 300,
  };
  const execution = {
    at: 100,
    fresh: true,
    interruptBusy: true,
    windowHours: 24,
    current: entry,
    history: [entry],
    totals: [{ code: 'memory', seconds: 10 }],
  };
  const base = {
    ...snapshot(),
    mode: 'demand',
    demandAuto: { ...demand(), execution },
  };
  assert.equal(validOptimizerResponse(base), true);
  for (const change of [
    { history: [null] },
    { fresh: 'true' },
    { current: { ...entry, reason: [] } },
    { totals: [{ code: 'memory', seconds: NaN }] },
  ])
    assert.equal(
      validOptimizerResponse({
        ...base,
        demandAuto: {
          ...base.demandAuto,
          execution: { ...execution, ...change },
        },
      }),
      false,
    );
});
test('demand mode accepts empty evidence and rejects malformed live decision containers', () => {
  const valid = { ...snapshot(), mode: 'demand', demandAuto: demand() };
  assert.equal(validOptimizerResponse(valid), true);
  for (const changed of [
    { limits: { ...demand().limits, switchesUsed: {} } },
    { limits: {} },
    { planningMinutes: null },
    { scanAt: null },
    { target: {} },
    { policy: { ...demand().policy, memoryHeadroomGB: 0 } },
    { confirmation: { model: 'b', seconds: {}, samples: 2 } },
    { opportunities: [{}] },
    { runs: [{}] },
  ])
    assert.equal(
      validOptimizerResponse({
        ...valid,
        demandAuto: { ...demand(), ...changed },
      }),
      false,
    );
});

test('trial progress and unknown money render safely while malformed metrics are rejected', () => {
  const trial = {
    runId: 1,
    model: 'nemotron',
    current: true,
    status: 'running',
    warmSeconds: 300,
    trialMinutes: 20,
    requests: 0,
    tokens: 0,
    paidWarmSeconds: 180,
    usd: 0,
    usdPerHour: 0,
    firstTrafficSeconds: null,
    idlePercent: 100,
  };
  const base = {
    ...snapshot(),
    mode: 'demand',
    demandAuto: {
      ...demand(),
      activity: { fresh: true, idleSeconds: 300 },
      trial,
    },
  };
  assert.equal(validOptimizerResponse(base), true);
  for (const changed of [
    { warmSeconds: null },
    { usd: {} },
    { firstTrafficSeconds: NaN },
    { status: {} },
    { model: [] },
    { paidWarmSeconds: undefined },
  ]) {
    assert.equal(
      validOptimizerResponse({
        ...base,
        demandAuto: { ...base.demandAuto, trial: { ...trial, ...changed } },
      }),
      false,
    );
  }
  assert.equal(
    validOptimizerResponse({
      ...base,
      demandAuto: {
        ...base.demandAuto,
        activity: { fresh: true, idleSeconds: {} },
      },
    }),
    false,
  );
  assert.equal(
    validOptimizerResponse({
      ...base,
      demandAuto: {
        ...base.demandAuto,
        policy: { ...demand().policy, idleEscapeMinutes: NaN },
      },
    }),
    false,
  );
});

test('fallback preference accepts old responses and validates live reliability metrics and opt-out', () => {
  const f = {
    model: 'gpt-oss-20b',
    enabled: true,
    qualified: true,
    current: false,
    eligible: true,
    status: 'ready',
    reason: 'Recent paid traffic',
    holdReason: null,
    selection: null,
    warmMinutes: 60,
    windows: 12,
    trafficWindows: 11,
    trafficPercent: 91.67,
    usdPerHour: 0.04,
    paidJobs: 50,
    asOf: 99,
    expiresAt: 86499,
  };
  const base = {
    ...snapshot(),
    mode: 'demand',
    demandAuto: {
      ...demand(),
      policy: { ...demand().policy, fallbackEnabled: 0 },
      fallback: f,
    },
  };
  assert.equal(validOptimizerResponse(base), true);
  for (const change of [
    { qualified: 1 },
    { trafficWindows: undefined },
    { warmMinutes: null },
    { usdPerHour: {} },
    { model: [] },
    { holdReason: {} },
    { status: null },
  ])
    assert.equal(
      validOptimizerResponse({
        ...base,
        demandAuto: { ...base.demandAuto, fallback: { ...f, ...change } },
      }),
      false,
    );
  for (const v of [true, -1, 2, '1'])
    assert.equal(
      validOptimizerResponse({
        ...base,
        demandAuto: {
          ...base.demandAuto,
          policy: { ...base.demandAuto.policy, fallbackEnabled: v },
        },
      }),
      false,
    );
});

test('earnings target and preferred return validate unknown money, policy and malformed data', () => {
  const target = {
    usdPerHour: 0.12,
    dailyUsd: 2.88,
    rate: null,
    fastRate: null,
    warmMinutes: 0,
    asOf: null,
    ready: false,
    belowTarget: false,
    status: 'learning',
    reason: 'Learning',
    nextCheckAt: null,
  };
  const preferred = {
    model: 'gemma',
    qualified: false,
    eligible: false,
    rate: null,
    hours: 0,
    asOf: null,
    scope: 'all_observed_hours',
    reason: 'Learning',
    selection: null,
  };
  const a = {
    ...demand(),
    policy: { ...demand().policy, targetUsdPerHour: 0.12 },
    earningsTarget: target,
    preferredReturn: preferred,
  };
  const valid = { ...snapshot(), mode: 'demand', demandAuto: a };
  assert.equal(validOptimizerResponse(valid), true);
  for (const change of [
    { rate: {} },
    { warmMinutes: null },
    { ready: 1 },
    { reason: [] },
    { dailyUsd: NaN },
    { nextCheckAt: {} },
  ])
    assert.equal(
      validOptimizerResponse({
        ...valid,
        demandAuto: { ...a, earningsTarget: { ...target, ...change } },
      }),
      false,
    );
  for (const change of [
    { model: null },
    { qualified: 1 },
    { rate: {} },
    { hours: undefined },
    { reason: [] },
  ])
    assert.equal(
      validOptimizerResponse({
        ...valid,
        demandAuto: { ...a, preferredReturn: { ...preferred, ...change } },
      }),
      false,
    );
  for (const value of [0, 0.121, '0.12', true, NaN])
    assert.equal(
      validOptimizerResponse({
        ...valid,
        demandAuto: { ...a, policy: { ...a.policy, targetUsdPerHour: value } },
      }),
      false,
    );
});

test('exceptional spike experiment safely validates unknown and malformed comparisons', () => {
  const spikeReview = {
    runId: 1,
    incumbent: 'gemma',
    referenceRate: 0.12,
    trialRate: null,
    liveRate: null,
    status: 'measuring',
    reason: 'Measuring',
    at: 100,
    deadline: 2500,
  };
  const base = {
    ...snapshot(),
    mode: 'demand',
    demandAuto: { ...demand(), spikeReview },
  };
  assert.equal(validOptimizerResponse(base), true);
  for (const change of [
    { referenceRate: {} },
    { incumbent: [] },
    { trialRate: NaN },
    { reason: null },
    { deadline: null },
    { status: 1 },
  ])
    assert.equal(
      validOptimizerResponse({
        ...base,
        demandAuto: {
          ...base.demandAuto,
          spikeReview: { ...spikeReview, ...change },
        },
      }),
      false,
    );
  assert.equal(
    validOptimizerResponse({
      ...base,
      demandAuto: { ...base.demandAuto, fallback: {} },
    }),
    false,
  );
});

test('live spike retention accepts missing legacy data but rejects malformed new fields', () => {
  const evidence = {
    status: 'faded',
    reason: 'Demand faded',
    sourceAt: 99,
    load: 100,
    pressure: 1.2,
    referenceLoad: 275,
    referencePressure: 3.4,
  };
  const spikeReview = {
    runId: 1,
    incumbent: 'gemma',
    referenceRate: 0.12,
    trialRate: 0,
    liveRate: 0,
    status: 'return',
    reason: 'Early return',
    at: 100,
    deadline: 2500,
    learning: false,
    demand: evidence,
  };
  const base = {
    ...snapshot(),
    mode: 'demand',
    demandAuto: { ...demand(), spikeReview },
  };
  assert.equal(validOptimizerResponse(base), true);
  assert.equal(
    validOptimizerResponse({
      ...base,
      demandAuto: {
        ...base.demandAuto,
        spikeReview: {
          ...spikeReview,
          demand: {
            ...evidence,
            status: 'unknown',
            load: null,
            pressure: null,
            sourceAt: null,
          },
        },
      },
    }),
    true,
  );
  for (const value of [
    [],
    { ...evidence, reason: null },
    { ...evidence, pressure: NaN },
    { ...evidence, referencePressure: {} },
    { ...evidence, sourceAt: '99' },
  ])
    assert.equal(
      validOptimizerResponse({
        ...base,
        demandAuto: {
          ...base.demandAuto,
          spikeReview: { ...spikeReview, demand: value },
        },
      }),
      false,
    );
});

test('high earnings hold accepts observed rates and rejects malformed states', () => {
  const highEarnings = {
    active: true,
    threshold: 0.2,
    windowMinutes: 15,
    coveredMinutes: 15,
    rates: [0.21, 0.23, 0.22],
    reason: 'Holding trials',
  };
  const earningsTarget = {
    usdPerHour: 0.12,
    dailyUsd: 2.88,
    warmMinutes: 30,
    ready: false,
    belowTarget: false,
    status: 'high_earnings',
    reason: 'Holding trials',
    highEarnings,
  };
  const base = {
    ...snapshot(),
    mode: 'demand',
    demandAuto: { ...demand(), earningsTarget },
  };
  assert.equal(validOptimizerResponse(base), true);
  for (const change of [
    { active: 'true' },
    { threshold: {} },
    { rates: [{}] },
    { rates: null },
    { coveredMinutes: null },
    { reason: [] },
  ])
    assert.equal(
      validOptimizerResponse({
        ...base,
        demandAuto: {
          ...base.demandAuto,
          earningsTarget: {
            ...earningsTarget,
            highEarnings: { ...highEarnings, ...change },
          },
        },
      }),
      false,
    );
});

test('resume controls reject malformed reasons and incomplete available states', () => {
  const resume = {
    hasSavedPlan: true,
    available: true,
    currentModel: 'model-a',
    selectedCount: 2,
    availableCount: 2,
    reason: null,
  };
  assert.equal(
    validOptimizerResponse({ ...snapshot(), resumeDemand: resume }),
    true,
  );
  assert.equal(
    validOptimizerResponse({
      ...snapshot(),
      resumeDemand: { ...resume, available: false, currentModel: null },
    }),
    true,
  );
  // A paused manager holding one model can resume with it alone (optimizer.py).
  assert.equal(
    validOptimizerResponse({
      ...snapshot(),
      resumeDemand: { ...resume, selectedCount: 1, availableCount: 1 },
    }),
    true,
  );
  for (const changed of [
    { reason: {} },
    { currentModel: null },
    { available: 'yes' },
    { selectedCount: -1 },
    { availableCount: 0 },
    { availableCount: 1.5 },
    { hasSavedPlan: false },
  ])
    assert.equal(
      validOptimizerResponse({
        ...snapshot(),
        resumeDemand: { ...resume, ...changed },
      }),
      false,
    );
});

test('multi-model reporting has its own validated aggregate status', () => {
  const reporting = {
    at: 100,
    sessionId: 7,
    models: ['a', 'b', 'c'],
    managedBy: 'darkbloom',
    automationSupported: false,
    counting: true,
    detail: 'Aggregate work',
  };
  assert.equal(validOptimizerResponse({ ...snapshot(), reporting }), true);
  for (const bad of [
    { models: ['a', 'a', 'c'] },
    { models: ['a'] },
    { managedBy: 'bloom' },
    { counting: 1 },
    { automationSupported: true },
    { at: NaN },
  ])
    assert.equal(
      validOptimizerResponse({
        ...snapshot(),
        reporting: { ...reporting, ...bad },
      }),
      false,
    );
});

const economicTrial = () => ({
  runId: 1,
  model: 'a',
  current: true,
  status: 'productive',
  warmSeconds: 1200,
  trialMinutes: 20,
  requests: 1,
  tokens: 3000,
  paidWarmSeconds: 1200,
  settled: true,
  paymentSeen: true,
  clock: {
    start: 100,
    end: 1420,
    seconds: 1320,
    usd: 0.029333,
    usdPerHour: 0.08,
    coveragePercent: 100,
    qualified: true,
  },
  occupancy: { start: 130, end: 1720, ended: false },
  competitive: {
    outcome: 'win',
    rateBasis: 'settled_inference_per_elapsed_selection_hour',
    comparison: {
      qualified: true,
      lower: 0.057,
      upper: 0.063,
      observedAt: 0,
      capturedAt: 100,
      warmMinutes: 40,
      liveRate: 0.06,
    },
    regime: { providerVersion: 'fixture', model: 'a' },
    revision: 'paid-trials-v1',
  },
});
const economicResponse = (trial) => ({
  ...snapshot(),
  mode: 'demand',
  demandAuto: { ...demand(), trial },
});
test('paid work can be economically worse, while legacy and incomplete coverage stay uncertain', () => {
  const trial = economicTrial();
  assert.equal(validOptimizerResponse(economicResponse(trial)), true);
  assert.equal(
    validOptimizerResponse(
      economicResponse({
        ...trial,
        competitive: { ...trial.competitive, outcome: 'loss' },
        clock: { ...trial.clock, usd: 0.007333, usdPerHour: 0.02 },
      }),
    ),
    true,
  );
  assert.equal(
    validOptimizerResponse(
      economicResponse({
        ...trial,
        clock: {
          ...trial.clock,
          qualified: false,
          usd: null,
          usdPerHour: null,
          coveragePercent: 99,
        },
        competitive: { ...trial.competitive, outcome: 'uncertain' },
      }),
    ),
    true,
  );
  assert.equal(
    validOptimizerResponse(
      economicResponse({
        ...trial,
        clock: {},
        occupancy: null,
        competitive: {
          ...trial.competitive,
          outcome: 'uncertain',
          comparison: {},
          regime: null,
          revision: null,
        },
      }),
    ),
    true,
  );
  for (const change of [
    { settled: false },
    { paymentSeen: 'yes' },
    { clock: { ...trial.clock, qualified: false } },
    { clock: { ...trial.clock, end: 99 } },
    { clock: { ...trial.clock, usdPerHour: NaN } },
    { competitive: { ...trial.competitive, comparison: { qualified: false } } },
    { occupancy: { start: 10, end: 5, ended: true } },
  ])
    assert.equal(
      validOptimizerResponse(economicResponse({ ...trial, ...change })),
      false,
    );
});
test('ordinary review allows unknown incumbent money and validates elapsed sampling holds', () => {
  const spikeReview = {
    runId: 1,
    incumbent: 'b',
    referenceRate: null,
    trialRate: null,
    liveRate: null,
    status: 'waiting',
    reason: 'Missing comparison',
    at: 100,
    deadline: 2500,
    ordinary: true,
    rateBasis: 'settled_inference_per_elapsed_selection_hour',
  };
  const sampling = {
    minutesUsed: 100,
    minutesLimit: 120,
    reservationMinutes: 40,
    unknownLifecycles: 1,
    qualified: false,
    cohort: 'paid-trials-v1',
  };
  const a = {
    ...demand(),
    spikeReview,
    limits: { ...demand().limits, sampling },
  };
  const valid = { ...snapshot(), mode: 'demand', demandAuto: a };
  assert.equal(validOptimizerResponse(valid), true);
  // With a steady trial rate the backend measures per warm hour after the first
  // paid work (trial_economics.py ordinary_review).
  const warm = {
    ...spikeReview,
    rateBasis: 'settled_inference_per_warm_hour_after_first_work',
    rampSeconds: 61,
  };
  assert.equal(
    validOptimizerResponse({
      ...valid,
      demandAuto: { ...a, spikeReview: warm },
    }),
    true,
  );
  assert.equal(
    validOptimizerResponse({
      ...valid,
      demandAuto: {
        ...a,
        spikeReview: { ...warm, ordinary: false, referenceRate: 0.05 },
      },
    }),
    true,
  );
  for (const change of [
    { referenceRate: {} },
    { ordinary: 'true' },
    { rateBasis: 'warm_hour' },
    { rateBasis: 'per_request' },
  ])
    assert.equal(
      validOptimizerResponse({
        ...valid,
        demandAuto: { ...a, spikeReview: { ...spikeReview, ...change } },
      }),
      false,
    );
  for (const change of [
    { minutesUsed: -1 },
    { reservationMinutes: NaN },
    { qualified: 1 },
    { unknownLifecycles: 0.5 },
    { availableAt: 'soon' },
  ])
    assert.equal(
      validOptimizerResponse({
        ...valid,
        demandAuto: {
          ...a,
          limits: { ...a.limits, sampling: { ...sampling, ...change } },
        },
      }),
      false,
    );
  // Learning time off (limit 0) and a projected next start are both valid.
  for (const change of [
    { minutesLimit: 0, minutesUsed: 0 },
    { availableAt: 1790650800 },
    { availableAt: null },
  ])
    assert.equal(
      validOptimizerResponse({
        ...valid,
        demandAuto: {
          ...a,
          limits: { ...a.limits, sampling: { ...sampling, ...change } },
        },
      }),
      true,
    );
});

test('current paid alternative validates its blocker, eligibility and timestamp', () => {
  const alternative = {
    model: 'gemma',
    eligible: false,
    reason: 'Memory safety margin',
    at: 100,
  };
  const a = {
    ...snapshot(),
    mode: 'demand',
    demandAuto: { ...demand(), paidAlternative: alternative },
  };
  assert.ok(validOptimizerResponse(a));
  assert.ok(
    validOptimizerResponse({
      ...a,
      demandAuto: { ...a.demandAuto, paidAlternative: null },
    }),
  );
  for (const change of [
    { model: '' },
    { model: null },
    { eligible: 'false' },
    { reason: [] },
    { reason: '' },
    { at: NaN },
    { at: null },
  ])
    assert.equal(
      validOptimizerResponse({
        ...a,
        demandAuto: {
          ...a.demandAuto,
          paidAlternative: { ...alternative, ...change },
        },
      }),
      false,
    );
});

test('retained economic confirmation validates paused and ready response fields', () => {
  const confirmation = {
    model: 'b',
    seconds: 160,
    samples: 4,
    since: 10,
    requiredSeconds: 300,
    status: 'paused',
    reason: 'Waiting for fresh demand.',
    expiresAt: 150,
  };
  const value = {
    ...snapshot(),
    mode: 'demand',
    demandAuto: { ...demand(), confirmation },
  };
  assert.ok(validOptimizerResponse(value));
  assert.ok(
    validOptimizerResponse({
      ...value,
      demandAuto: {
        ...value.demandAuto,
        confirmation: {
          ...confirmation,
          status: 'ready',
          seconds: 300,
          reason: 'Needs 40 MB more memory.',
          expiresAt: null,
        },
      },
    }),
  );
  for (const change of [
    { status: 'ready' },
    { status: 'switched' },
    { reason: [] },
    { reason: '' },
    { expiresAt: -1 },
    { expiresAt: NaN },
    { seconds: -1 },
  ])
    assert.equal(
      validOptimizerResponse({
        ...value,
        demandAuto: {
          ...value.demandAuto,
          confirmation: { ...confirmation, ...change },
        },
      }),
      false,
      JSON.stringify(change),
    );
});
test('a stall-recovery restart in the run history does not block the optimizer page', () => {
  const restart = {
    id: 131,
    at: 100,
    model: 'a',
    result: 'switched',
    downtime: 72,
    decision: {
      kind: 'recovery',
      reason: 'Restarting the provider on the same model for a fresh session.',
      stall: { model: 'a', silenceSeconds: 480, demandHeld: true },
    },
  };
  const base = {
    ...snapshot(),
    mode: 'demand',
    demandAuto: { ...demand(), runs: [restart] },
  };
  assert.equal(validOptimizerResponse(base), true);
  for (const decision of [
    { ...restart.decision, planningMinutes: 'soon' },
    { ...restart.decision, candidate: {} },
    { ...restart.decision, candidate: {}, planningMinutes: 60 },
  ])
    assert.equal(
      validOptimizerResponse({
        ...base,
        demandAuto: { ...base.demandAuto, runs: [{ ...restart, decision }] },
      }),
      false,
    );
});

test('one malformed history row is dropped instead of blanking the optimizer page', () => {
  const good = {
    id: 1,
    at: 100,
    model: 'a',
    result: 'switched',
    downtime: 5,
    decision: { kind: 'recovery', reason: 'Restart' },
  };
  const bad = { ...good, id: 'two', decision: null };
  const base = snapshot();
  const value = {
    ...base,
    mode: 'demand',
    events: [...base.events, { at: 1, detail: 'ok' }, { at: 'soon' }],
    demandAuto: { ...demand(), runs: [good, bad] },
  };
  assert.equal(validOptimizerResponse(value), false);
  const cleaned = withoutInvalidHistory(value);
  assert.equal(validOptimizerResponse(cleaned), true);
  assert.deepEqual(cleaned.demandAuto.runs, [good]);
  assert.equal(cleaned.events.length, base.events.length + 1);
  assert.equal(withoutInvalidHistory(null), null);
});

test('the run chip reads the real view=run payload from collector.run_view', () => {
  // Generated by native/test_collector.py (run_view_outputs): the manager holding its
  // home model, and a legacy trial with a paid alternative and a spike review.
  const [hold, trial] = JSON.parse(
    readFileSync(
      new URL('./optimizer_run_view_fixture.json', import.meta.url),
      'utf8',
    ),
  );
  for (const value of [hold, trial]) {
    // The view sends a slice of demandAuto, which the full-page validator refuses.
    assert.equal(validOptimizerResponse(value), false);
    assert.equal(readRunView(value), value);
  }
  const session = {
    id: 7,
    models: trial.currentModels,
    status: 'active',
    startedAt: trial.at - 3600,
  };
  const now = trial.at + 1;
  const held = pulseRunStatus(readRunView(hold), session, now);
  assert.equal(held.label, 'Normal run');
  assert.equal(held.detail, hold.detail);
  const measuring = pulseRunStatus(readRunView(trial), session, now);
  assert.equal(measuring.label, 'Demand-spike trial');
  assert.equal(measuring.alternative.model, 'gpt-oss-20b');
  assert.notEqual(measuring.label, 'Run status unavailable');
  for (const bad of [
    null,
    [],
    {},
    { ...trial, at: 'now' },
    { ...trial, mode: 1 },
  ])
    assert.equal(readRunView(bad), null);
});

test('one malformed optional section is dropped and named; the rest of the page renders', () => {
  const base = { ...snapshot(), mode: 'demand', demandAuto: demand() };
  const whole = readOptimizerResponse(base);
  assert.deepEqual(whole.unreadable, []);
  assert.equal(validOptimizerResponse(whole.value), true);
  const read = readOptimizerResponse({
    ...base,
    warmup: { status: 3 },
    reporting: { at: 1 },
    models: [...base.models, { id: 'odd', evidence: null }],
    demandAuto: {
      ...demand(),
      trial: { runId: 'x' },
      spikeReview: { runId: 1, rateBasis: 'per_request' },
      savedPolicy: { managerStrategy: 2 },
      limits: { ...demand().limits, sampling: { minutesUsed: -1 } },
      opportunities: [{ model: 'b', signal: {} }, {}],
    },
  });
  assert.deepEqual(read.unreadable.sort(), [
    'learning time',
    'multi-model reporting',
    'some model comparisons',
    'some model evidence',
    'the saved plan',
    'the trial',
    'the trial review',
    'warm-up status',
  ]);
  assert.equal(validOptimizerResponse(read.value), true);
  assert.deepEqual(
    read.value.models.map((m) => m.id),
    ['model-a'],
  );
  assert.equal(read.value.warmup, undefined);
  assert.equal(read.value.demandAuto.trial, undefined);
  assert.equal(read.value.demandAuto.policy.minRunMinutes, 60);
  assert.equal(read.value.demandAuto.limits.switchLimit, 4);
});

test('a plan that cannot render is dropped alone; missing response fields still fail', () => {
  const base = { ...snapshot(), mode: 'demand' };
  const read = readOptimizerResponse({
    ...base,
    demandAuto: {
      ...demand(),
      policy: { ...demand().policy, memoryHeadroomGB: 0 },
    },
  });
  assert.deepEqual(read.unreadable, ['the automatic plan']);
  assert.equal(read.value.demandAuto, undefined);
  assert.equal(read.value.selected[0], 'model-a');
  for (const value of [
    null,
    {},
    { ...base, models: null },
    { ...base, mode: 'sometimes' },
    { ...base, blockHours: 0 },
  ])
    assert.equal(readOptimizerResponse(value), null);
});

test('values the backend sends today are read: a paused manager with one model, the warm-hour rate basis', () => {
  const read = readOptimizerResponse({
    ...snapshot(),
    mode: 'observe',
    resumeDemand: {
      hasSavedPlan: true,
      available: true,
      currentModel: 'model-a',
      selectedCount: 1,
      availableCount: 1,
      reason: null,
    },
    demandAuto: {
      ...demand(),
      spikeReview: {
        runId: 1,
        incumbent: 'b',
        referenceRate: 0.05,
        trialRate: 0.08,
        liveRate: null,
        status: 'measuring',
        reason: 'Measuring the ordinary trial.',
        at: 100,
        deadline: 2500,
        ordinary: true,
        rampSeconds: 61,
        rateBasis: 'settled_inference_per_warm_hour_after_first_work',
      },
    },
  });
  assert.deepEqual(read.unreadable, []);
  assert.equal(read.value.resumeDemand.availableCount, 1);
  assert.equal(read.value.demandAuto.spikeReview.trialRate, 0.08);
});

test('lists at or over the backend limits are clamped, not rejected', () => {
  const entry = (id) => ({
    id,
    at: 90,
    updated: 100,
    observedSeconds: 10,
    mode: 'demand',
    model: 'a',
    target: null,
    phase: 'waiting',
    code: 'memory',
    reason: 'Waiting for memory',
    sourceAt: 95,
    confirmationSeconds: 0,
    requiredSeconds: 300,
  });
  const history = Array.from({ length: 31 }, (_, i) => entry(31 - i));
  const execution = {
    at: 100,
    fresh: true,
    interruptBusy: true,
    windowHours: 24,
    current: history[0],
    history,
    totals: [],
  };
  const highEarnings = {
    active: false,
    reason: 'Learning',
    threshold: 0.2,
    windowMinutes: 15,
    coveredMinutes: 20,
    rates: [0.1, 0.2, 0.3, 0.4],
  };
  const earningsTarget = {
    usdPerHour: 0.12,
    dailyUsd: 2.88,
    warmMinutes: 60,
    ready: true,
    belowTarget: false,
    status: 'ok',
    reason: 'On target',
    highEarnings,
  };
  const value = {
    ...snapshot(),
    mode: 'demand',
    demandAuto: { ...demand(), execution, earningsTarget },
  };
  assert.equal(validOptimizerResponse(value), false);
  const read = readOptimizerResponse(value);
  assert.deepEqual(read.unreadable, []);
  assert.equal(validOptimizerResponse(read.value), true);
  const kept = read.value.demandAuto.execution.history;
  assert.equal(kept.length, 30);
  assert.equal(kept[0].id, 31); // newest first
  assert.deepEqual(
    read.value.demandAuto.earningsTarget.highEarnings.rates,
    [0.2, 0.3, 0.4],
  );
  // The input is not modified.
  assert.equal(value.demandAuto.execution.history.length, 31);
});

test('dropped history rows are named but never count toward a validation report', () => {
  const base = { ...snapshot(), mode: 'demand', demandAuto: demand() };
  assert.equal(readOptimizerResponse(base).reportable, false);
  // Old-format switch runs and activity events were tolerated before (Sep 26 run 131).
  const history = readOptimizerResponse({
    ...base,
    events: [...base.events, { kind: 1 }],
    demandAuto: { ...demand(), runs: [{ id: 131, model: 'a' }] },
  });
  assert.deepEqual(history.unreadable.sort(), [
    'some activity',
    'some switch history',
  ]);
  assert.equal(history.reportable, false);
  // Any other dropped part still counts.
  const plan = readOptimizerResponse({
    ...base,
    events: [...base.events, { kind: 1 }],
    warmup: { status: 3 },
  });
  assert.equal(plan.reportable, true);
});

test('a dropped plan section keeps its last readable value and locks saving', () => {
  const saved = { ...demand().policy, managerStrategy: 1 };
  const good = readOptimizerResponse({
    ...snapshot(),
    mode: 'demand',
    demandAuto: { ...demand(), savedPolicy: saved, manager: { active: true } },
  });
  assert.deepEqual(good.unreadable, []);
  assert.equal(savedPlanUnreadable(good.unreadable), false);
  // The whole plan drops: the manager view and the saved plan stay as last read.
  const whole = readOptimizerResponse({
    ...snapshot(),
    mode: 'demand',
    demandAuto: {
      ...demand(),
      policy: { ...demand().policy, memoryHeadroomGB: 0 },
    },
  });
  assert.equal(whole.value.demandAuto, undefined);
  const kept = keepReadablePlan(whole.value, whole.unreadable, good.value);
  assert.equal(kept.demandAuto, good.value.demandAuto);
  assert.equal(savedPlanUnreadable(whole.unreadable), true);
  // Only the saved plan drops: the editor must not fall back to the live policy.
  const part = readOptimizerResponse({
    ...snapshot(),
    mode: 'demand',
    demandAuto: {
      ...demand(),
      reason: 'Newer',
      savedPolicy: { managerStrategy: 2 },
    },
  });
  assert.deepEqual(part.unreadable, ['the saved plan']);
  const merged = keepReadablePlan(part.value, part.unreadable, good.value);
  assert.deepEqual(merged.demandAuto.savedPolicy, saved);
  assert.equal(merged.demandAuto.reason, 'Newer');
  assert.equal(validOptimizerResponse(merged), true);
  assert.equal(savedPlanUnreadable(part.unreadable), true);
  // A plan that is simply absent (not dropped) is not brought back.
  const none = readOptimizerResponse({ ...snapshot(), mode: 'observe' });
  assert.equal(
    keepReadablePlan(none.value, none.unreadable, good.value).demandAuto,
    undefined,
  );
  // Nothing to keep before the first readable plan.
  assert.equal(
    keepReadablePlan(part.value, part.unreadable, null),
    part.value,
  );
});
