import test from 'node:test';
import assert from 'node:assert/strict';
import {
  automaticRequest,
  readAutomaticRequest,
  optimizerRequestObserved,
  validOptimizerControl,
  optimizerControlFresh,
  optimizerPhaseLabel,
} from '../lib/optimizer-control.ts';
const id = '66d782ba-2d0e-48fc-8060-599c5f5af1b4';
const state = () => ({
  at: 100,
  controlVersion: 'saved-plan',
  providerVersion: 'stopped-gemma',
  currentModel: 'gemma',
  actualMode: 'observe',
  providerRunning: false,
  hasSavedPlan: true,
  firstPlan: false,
  selected: ['gemma', 'qwen'],
  models: [],
  demandPolicy: {},
  automatic: {
    mode: 'on',
    phase: 'starting',
    detail: 'Clearing file cache',
    canEnable: false,
    intentId: id,
  },
  operation: { id, status: 'starting', detail: 'Clearing file cache' },
});
test('On intent is representable while provider remains stopped and actual mode is Observe', () => {
  const s = state();
  assert.ok(validOptimizerControl(s));
  assert.equal(s.automatic.mode, 'on');
  assert.equal(optimizerPhaseLabel(s), 'Preparing to start');
  s.automatic.phase = 'waiting';
  s.operation.status = 'waiting';
  assert.ok(validOptimizerControl(s));
  assert.equal(s.automatic.mode, 'on');
});
test('exact On request survives changed plan/provider and only a matching receipt resolves it', () => {
  const s = state(),
    r = automaticRequest(s, true, id);
  s.controlVersion = 'later-plan';
  s.providerVersion = 'later-provider';
  assert.deepEqual(readAutomaticRequest(JSON.stringify(r)), r);
  assert.equal(r.expectedControl, 'saved-plan');
  assert.equal(r.expectedProvider, 'stopped-gemma');
  assert.equal(optimizerRequestObserved(s, r), true);
  s.automatic.intentId = 'other';
  s.operation.id = 'other';
  assert.equal(optimizerRequestObserved(s, r), false);
});
test('Manual cancellation carries no provider or startup authority', () => {
  const r = automaticRequest(state(), false, id);
  assert.deepEqual(r, {
    action: 'set-automatic',
    enabled: false,
    requestId: id,
    expectedControl: 'saved-plan',
  });
  assert.equal(
    readAutomaticRequest(
      JSON.stringify({ ...r, expectedProvider: 'stopped-gemma' }),
    ),
    null,
  );
});
test('future/stale status cannot authorize On, and malformed progress fails validation', () => {
  assert.equal(optimizerControlFresh(state(), 94), false);
  assert.equal(optimizerControlFresh(state(), 115), false);
  const s = state();
  s.operation.status = 'purge-whenever';
  assert.equal(validOptimizerControl(s), false);
});
