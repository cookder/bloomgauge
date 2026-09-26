import test from 'node:test';
import assert from 'node:assert/strict';
import {
  readOptimizerLive,
  optimizerLiveGroups,
  confirmationPresentation,
} from '../lib/optimizer-live.ts';
const candidate = (model, group = 'held', netGainUsd = -0.1) => ({
  model,
  name: model,
  group,
  eligible: group === 'paid_alternative' || group === 'comparison_target',
  firstBlocker: group === 'held' ? 'Gain is insufficient' : null,
  kind: 'earnings',
  selectionReason: null,
  meanUsdPerWarmHour: 0.1,
  lowerUsdPerWarmHour: 0.05,
  upperUsdPerWarmHour: 0.15,
  netGainUsd,
  load: 20,
});
const report = () => ({
  schemaVersion: 1,
  at: 1000,
  mode: 'demand',
  currentModel: 'qwen',
  phase: 'waiting',
  reason: 'Waiting for fresh local readings.',
  fresh: true,
  lastComparisonAt: 990,
  sourceAt: 985,
  comparisonTarget: null,
  proposalTarget: null,
  pendingTarget: null,
  earliestEligibleAt: 500,
  planningMinutes: 60,
  currentPaidBasis: null,
  candidates: [candidate('gemma')],
});
test('actual waiting remains waiting alongside a fresh but held earnings comparison', () => {
  const v = report();
  assert.equal(readOptimizerLive(v).phase, 'waiting');
  assert.equal(v.candidates[0].netGainUsd, -0.1);
});
test('stale comparisons cannot carry current rates, targets or candidate rankings', () => {
  for (const change of [
    (v) => {},
    (v) => {
      v.candidates = [];
      v.comparisonTarget = 'gemma';
    },
    (v) => {
      v.candidates = [];
      v.currentPaidBasis = {};
    },
  ]) {
    const v = report();
    v.fresh = false;
    change(v);
    assert.throws(() => readOptimizerLive(v));
  }
  const v = report();
  v.fresh = false;
  v.candidates = [];
  v.pendingTarget = 'gemma';
  v.phase = 'switching';
  assert.equal(readOptimizerLive(v), v);
});
test('unknown gains remain separate; explicit controller target is not inferred by sorting', () => {
  const rows = [
    candidate('held', 'held', -0.1),
    candidate('unknown', 'unknown', null),
    candidate('second', 'paid_alternative', 0.02),
    candidate('first', 'paid_alternative', 0.04),
    candidate('chosen', 'comparison_target', 0.01),
  ];
  const groups = optimizerLiveGroups(rows);
  assert.deepEqual(
    groups.paid.map((r) => r.model),
    ['first', 'second'],
  );
  assert.equal(groups.target[0].model, 'chosen');
  assert.equal(groups.unknown[0].netGainUsd, null);
  assert.equal(groups.held[0].model, 'held');
});
test('rejects conflicting candidate targets, duplicate models and unearned ranked eligibility', () => {
  for (const change of [
    (v) => v.candidates.push(v.candidates[0]),
    (v) => (v.candidates[0].model = 'qwen'),
    (v) => (v.candidates[0].group = 'comparison_target'),
    (v) => (v.candidates[0].group = 'paid_alternative'),
    (v) => (v.candidates[0].netGainUsd = NaN),
  ]) {
    const v = report();
    change(v);
    assert.throws(() => readOptimizerLive(v));
  }
});
test('trial and confirmation progress must belong to the current selection and actual proposal', () => {
  const v = report();
  v.measurement = {
    model: 'old-model',
    status: 'running',
    warmSeconds: 300,
    trialMinutes: 20,
  };
  assert.throws(() => readOptimizerLive(v));
  v.measurement.model = 'qwen';
  assert.equal(readOptimizerLive(v), v);
  v.confirmation = { model: 'gemma', seconds: 120, requiredSeconds: 300 };
  assert.throws(() => readOptimizerLive(v));
  v.proposalTarget = 'gemma';
  assert.equal(readOptimizerLive(v), v);
});

const confirmation = () => ({
  model: 'gemma',
  seconds: 160,
  requiredSeconds: 300,
  status: 'paused',
  reason: 'Waiting for fresh demand.',
  expiresAt: 1100,
});
test('retained confirmation is valid during a short data gap without current comparisons', () => {
  const v = {
    ...report(),
    fresh: false,
    candidates: [],
    phase: 'waiting',
    proposalTarget: 'gemma',
    confirmation: confirmation(),
  };
  assert.equal(readOptimizerLive(v), v);
  const first = confirmationPresentation(v.confirmation, 1000),
    later = confirmationPresentation(v.confirmation, 1050);
  assert.equal(first.progress, '2:40 / 5:00');
  assert.equal(later.progress, first.progress);
  assert.equal(later.state, 'paused');
  assert.equal(confirmationPresentation(v.confirmation, 1100).state, 'expired');
});
test('ready earnings with a memory hold is distinct from an executing switch', () => {
  const v = {
    ...report(),
    proposalTarget: 'gemma',
    confirmation: {
      ...confirmation(),
      status: 'ready',
      seconds: 300,
      reason: 'Needs 40 MB more memory.',
      expiresAt: null,
    },
  };
  assert.equal(readOptimizerLive(v).phase, 'waiting');
  const display = confirmationPresentation(v.confirmation, 1000);
  assert.equal(display.label, 'Earnings confirmed');
  assert.equal(display.detail, 'Needs 40 MB more memory.');
});
test('confirmation cannot claim ready early or accept malformed pause state', () => {
  for (const change of [
    { status: 'ready' },
    { status: 'switched' },
    { reason: {} },
    { reason: '' },
    { expiresAt: -1 },
    { expiresAt: NaN },
    { seconds: -1 },
    { requiredSeconds: 0 },
  ]) {
    const v = {
      ...report(),
      proposalTarget: 'gemma',
      confirmation: { ...confirmation(), ...change },
    };
    assert.throws(() => readOptimizerLive(v), JSON.stringify(change));
  }
});
test('older confirmations still display without inventing completion or a countdown', () => {
  const value = { model: 'gemma', seconds: 300, requiredSeconds: 300 };
  assert.equal(
    confirmationPresentation(value, 1000).label,
    'Confirming earnings',
  );
  assert.equal(
    confirmationPresentation({ ...value, seconds: 999 }, 1000).progress,
    '5:00 / 5:00',
  );
});
