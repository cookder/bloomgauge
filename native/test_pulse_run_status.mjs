import test from 'node:test';
import assert from 'node:assert/strict';
import { pulseRunStatus } from '../lib/pulse-run-status.ts';
const now = 20000;
const session = {
  id: 83,
  models: ['nemotron'],
  status: 'active',
  startedAt: 18000,
};
const base = () => ({
  at: now,
  mode: 'demand',
  currentModel: 'nemotron',
  currentModels: ['nemotron'],
  busy: false,
  detail: 'Keeping the current model.',
  warmup: { status: 'ready' },
  demandAuto: {
    enabled: true,
    trial: {
      runId: 29,
      model: 'nemotron',
      current: true,
      status: 'running',
      warmSeconds: 600,
      trialMinutes: 20,
    },
    runs: [
      {
        id: 29,
        model: 'nemotron',
        decision: {
          explorationTrigger: 'demand_spike',
          reason: 'Sustained exceptional demand.',
        },
      },
    ],
  },
});
test('shows actual trial type, paid-data purpose and covered warm progress', () => {
  const d = base();
  let badge = pulseRunStatus(d, session, now);
  assert.equal(badge.label, 'Demand-spike trial');
  assert.equal(badge.progress, '10.0 / 20 warm min');
  assert.equal(
    badge.reason,
    'Why this trial started: Sustained exceptional demand.',
  );
  d.demandAuto.runs[0].decision.explorationTrigger = 'baseline_learning';
  assert.equal(pulseRunStatus(d, session, now).label, 'Baseline trial');
});
test('a completed trial is normal, but an unresolved return comparison stays visible', () => {
  const d = base();
  d.demandAuto.trial.status = 'productive';
  assert.equal(pulseRunStatus(d, session, now).label, 'Normal run');
  assert.equal(pulseRunStatus(d, session, now).progress, 'Trial finished');
  d.demandAuto.spikeReview = {
    runId: 29,
    status: 'return',
    reason: 'Checking the incumbent.',
  };
  assert.equal(pulseRunStatus(d, session, now).progress, 'Evaluating return');
  d.demandAuto.spikeReview.status = 'keep';
  assert.equal(pulseRunStatus(d, session, now).label, 'Normal run');
});
test('settling is distinct from collecting warm minutes', () => {
  const d = base();
  d.demandAuto.trial.status = 'settling';
  assert.equal(pulseRunStatus(d, session, now).progress, 'Settling credits');
});
test('old trial, preview opportunity and unrelated review cannot label a new run', () => {
  const d = base();
  d.demandAuto.trial.current = false;
  d.demandAuto.kind = 'explore';
  d.demandAuto.target = 'other';
  assert.equal(pulseRunStatus(d, session, now).label, 'Normal run');
  d.demandAuto.trial.current = true;
  d.demandAuto.trial.status = 'productive';
  d.demandAuto.spikeReview = { runId: 28, status: 'return' };
  assert.equal(pulseRunStatus(d, session, now).label, 'Normal run');
});
test('paused automation and explicit scheduled modes have independent labels', () => {
  const d = base();
  d.mode = 'observe';
  assert.equal(pulseRunStatus(d, session, now).label, 'Manual / observe');
  d.mode = 'week';
  assert.equal(pulseRunStatus(d, session, now).label, 'Scheduled model test');
  d.mode = 'combo';
  d.currentModels = ['nemotron', 'gemma'];
  assert.equal(
    pulseRunStatus(d, { ...session, models: ['gemma', 'nemotron'] }, now).label,
    'Model-pair test',
  );
});
test('queued, switching, recovery and warming override old trial progress', () => {
  const d = base();
  d.requestedModel = 'gemma';
  assert.equal(pulseRunStatus(d, session, now).label, 'Switch queued');
  d.busy = true;
  assert.equal(pulseRunStatus(d, session, now).label, 'Switching model');
  d.requestedKind = 'manual-recovery';
  assert.equal(pulseRunStatus(d, session, now).label, 'Restoring model');
  d.busy = false;
  d.requestedModel = null;
  d.warmup.status = 'warming';
  assert.equal(
    pulseRunStatus(d, session, now).label,
    'Model warming / not ready',
  );
});
test('missing, stale or future readings never claim a normal or active trial', () => {
  for (const value of [
    null,
    {},
    { at: NaN },
    { ...base(), at: now - 46 },
    { ...base(), at: now + 6 },
  ])
    assert.equal(
      pulseRunStatus(value, session, now).label,
      'Run status unavailable',
    );
});
test('session transition cannot inherit the last model run classification', () => {
  assert.equal(
    pulseRunStatus(base(), { ...session, models: ['gemma'] }, now).label,
    'Updating run status',
  );
  assert.equal(
    pulseRunStatus(base(), { ...session, startedAt: now + 1 }, now).label,
    'Updating run status',
  );
  assert.equal(
    pulseRunStatus(base(), { ...session, status: 'ended' }, now).label,
    'No active run',
  );
});

test('three-model reporting does not need the optimizer response to describe the run', () => {
  const s = { ...session, models: ['a', 'b', 'c'] };
  const reporting = {
    at: now,
    sessionId: s.id,
    models: s.models,
    managedBy: 'darkbloom',
    automationSupported: false,
    counting: true,
    detail: 'All models loaded; aggregate serving output observed.',
  };
  const badge = pulseRunStatus(null, s, now, reporting);
  assert.equal(badge.label, '3-model monitoring');
  assert.equal(badge.progress, 'Managed by Darkbloom');
  assert.equal(badge.tone, 'normal');
  assert.match(badge.reason, /currently selects one model/);
  assert.equal(
    pulseRunStatus(null, s, now, { ...reporting, counting: false }).label,
    '3-model monitoring paused',
  );
  for (const bad of [
    { at: now - 16 },
    { at: now + 6 },
    { sessionId: s.id + 1 },
    { models: ['a', 'a', 'c'] },
    { models: ['a', 'b', 'd'] },
    { counting: 'yes' },
    { managedBy: 'bloom' },
    { automationSupported: true },
  ])
    assert.equal(
      pulseRunStatus(null, s, now, { ...reporting, ...bad }).label,
      'Run status unavailable',
    );
  assert.equal(
    pulseRunStatus(null, { ...s, status: 'ended' }, now, reporting).label,
    'Run status unavailable',
  );
});

test('ordinary completed trial waiting for fresh evidence remains on hold until resolved', () => {
  const d = base();
  d.demandAuto.runs[0].decision.explorationTrigger = 'idle_escape';
  d.demandAuto.trial.status = 'productive';
  d.demandAuto.spikeReview = {
    runId: 29,
    status: 'waiting',
    ordinary: true,
    reason: 'Missing fresh earnings',
  };
  assert.equal(pulseRunStatus(d, session, now).label, 'Model trial');
  assert.equal(pulseRunStatus(d, session, now).progress, 'Review on hold');
  assert.equal(
    pulseRunStatus(d, session, now).detail,
    'Missing fresh earnings',
  );
  d.demandAuto.spikeReview.status = 'keep';
  d.demandAuto.runs[0].decision.trialResolution = {
    reason: 'Covered elapsed earnings remain competitive',
  };
  assert.equal(pulseRunStatus(d, session, now).label, 'Normal run');
  assert.equal(
    pulseRunStatus(d, session, now).detail,
    'Covered elapsed earnings remain competitive',
  );
});

test('terminal inconclusive comparison does not stay an active trial or imply an improvement', () => {
  const d = base();
  d.demandAuto.trial.status = 'productive';
  d.demandAuto.runs[0].decision.trialResolution = {
    status: 'inconclusive',
    reason:
      'Entry comparison was unavailable. Continue checking safe alternatives.',
  };
  d.demandAuto.spikeReview = {
    runId: 29,
    status: 'waiting',
    reason: 'Old review state',
  };
  d.demandAuto.paidAlternative = {
    model: 'gemma',
    eligible: false,
    reason: 'Memory safety margin is not available.',
    at: now,
  };
  const r = pulseRunStatus(d, session, now);
  assert.equal(r.label, 'Trial ended');
  assert.equal(r.progress, 'Inconclusive');
  assert.equal(r.tone, 'notice');
  assert.match(r.detail, /Entry comparison was unavailable/);
  assert.equal(r.alternative.model, 'gemma');
  assert.match(r.alternative.reason, /Memory/);
  assert.match(r.reason, /Why this trial started/);
});

test('alternative blocker is omitted when stale, malformed, unrelated or automation is paused', () => {
  const d = base();
  const a = {
    model: 'gemma',
    eligible: false,
    reason: 'Memory safety margin is not available.',
    at: now,
  };
  for (const change of [
    { at: now - 46 },
    { at: now + 6 },
    { at: NaN },
    { model: null },
    { eligible: 'false' },
    { reason: [] },
  ]) {
    d.demandAuto.paidAlternative = { ...a, ...change };
    assert.equal(pulseRunStatus(d, session, now).alternative, undefined);
  }
  d.demandAuto.paidAlternative = a;
  d.mode = 'observe';
  assert.equal(pulseRunStatus(d, session, now).alternative, undefined);
  d.mode = 'demand';
  d.demandAuto.trial.current = false;
  assert.equal(pulseRunStatus(d, session, now).alternative, undefined);
  d.demandAuto.trial.current = true;
  assert.equal(
    pulseRunStatus(d, { ...session, models: ['another'] }, now).alternative,
    undefined,
  );
});

test('persisted terminal resolution outranks incomplete raw observation status', () => {
  for (const raw of ['running', 'settling'])
    for (const status of ['keep', 'inconclusive']) {
      const d = base();
      d.demandAuto.trial.status = raw;
      d.demandAuto.runs[0].decision.trialResolution = {
        status,
        reason: 'Persisted terminal review',
      };
      const r = pulseRunStatus(d, session, now);
      assert.equal(
        r.progress,
        status === 'keep' ? 'Trial finished' : 'Inconclusive',
      );
      assert.notEqual(r.tone, 'trial');
    }
});
