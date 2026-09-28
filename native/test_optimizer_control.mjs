import test from 'node:test';
import assert from 'node:assert/strict';
import {
  validOptimizerControl,
  automaticRequest,
  readAutomaticRequest,
  initialOptimizerModels,
  optimizerRequestObserved,
  optimizerControlFresh,
  optimizerPhaseLabel,
} from '../lib/optimizer-control.ts';

const id = 'd9bd3d3e-91bd-485b-b470-0ed00daab0af';
const fixture = () => ({
  at: 100,
  controlVersion: 'control-one',
  providerVersion: 'provider-one',
  currentModel: 'current',
  providerRunning: true,
  actualMode: 'observe',
  hasSavedPlan: true,
  firstPlan: false,
  selected: ['current', 'saved-unavailable'],
  demandPolicy: { targetUsdPerHour: 0.12 },
  models: [
    { id: 'current', name: 'Current', available: false, reason: 'Warming' },
    { id: 'other', name: 'Other', available: true },
  ],
  warmup: { status: 'warming', detail: 'Getting ready' },
  automatic: {
    mode: 'manual',
    phase: 'manual',
    detail: 'Manual',
    canEnable: true,
  },
  operation: null,
  lastRequestId: null,
});

test('lightweight status accepts unavailable saved models without requiring analytics', () => {
  assert.equal(validOptimizerControl(fixture()), true);
  assert.equal(
    validOptimizerControl({ ...fixture(), models: [], warmup: {} }),
    true,
  );
});

test('control status may name the effective strategy', () => {
  assert.equal(validOptimizerControl({ ...fixture(), strategy: 'manager' }), true);
  assert.equal(validOptimizerControl({ ...fixture(), strategy: 'legacy' }), true);
  assert.equal(validOptimizerControl({ ...fixture(), strategy: 'boost' }), false);
});

test('malformed nested control data never replaces the last confirmed status', () => {
  const changes = [
    { at: NaN },
    { at: 0 },
    { controlVersion: '' },
    { firstPlan: undefined },
    { selected: ['a', 'a'] },
    { demandPolicy: [] },
    { demandPolicy: { targetUsdPerHour: null } },
    { models: [null] },
    { models: [{ id: 'a', name: 'A', available: 'yes' }] },
    { models: [{ id: 'a', name: 'A', available: true, selected: {} }] },
    { automatic: null },
    { automatic: { ...fixture().automatic, phase: 'earned' } },
    {
      automatic: {
        ...fixture().automatic,
        blocker: { code: 'a', action: 'run-shell' },
      },
    },
    { operation: { id, status: 'success-maybe', detail: 'Unknown' } },
    { operation: [] },
    { warmup: [] },
  ];
  for (const patch of changes)
    assert.equal(
      validOptimizerControl({ ...fixture(), ...patch }),
      false,
      JSON.stringify(patch),
    );
  for (const value of [null, [], {}, 'status'])
    assert.equal(validOptimizerControl(value), false);
});

test('Manual includes no provider or plan replacement fields', () => {
  assert.deepEqual(automaticRequest(fixture(), false, id), {
    action: 'set-automatic',
    enabled: false,
    requestId: id,
    expectedControl: 'control-one',
  });
});

test('saved On preserves the exact plan and does not resend settings', () => {
  const state = fixture(),
    before = structuredClone(state),
    request = automaticRequest(state, true, id);
  assert.equal('models' in request, false);
  assert.equal('demandPolicy' in request, false);
  assert.equal(request.expectedProvider, 'provider-one');
  assert.deepEqual(state, before);
});

test('first-ever On includes the stopped current model even before runtime verification', () => {
  const state = {
    ...fixture(),
    firstPlan: true,
    hasSavedPlan: false,
    providerRunning: false,
  };
  assert.deepEqual(initialOptimizerModels(state), ['current', 'other']);
  const request = automaticRequest(state, true, id);
  assert.deepEqual(request.models, ['current', 'other']);
  assert.deepEqual(request.demandPolicy, state.demandPolicy);
  state.demandPolicy.targetUsdPerHour = 0.5;
  assert.equal(request.demandPolicy.targetUsdPerHour, 0.12);
  const completed = { ...state, firstPlan: false };
  assert.equal('models' in automaticRequest(completed, true, id), false);
});

test('reopening and retrying preserve exact UUID payload despite changing versions/catalogs', () => {
  const request = automaticRequest(
    { ...fixture(), firstPlan: true, hasSavedPlan: false },
    true,
    id,
  );
  const saved = JSON.stringify(request),
    restored = readAutomaticRequest(saved);
  assert.deepEqual(restored, request);
  assert.equal(JSON.stringify(restored), saved);
  for (const bad of [
    '{',
    null,
    'null',
    JSON.stringify({ ...request, requestId: 'bad' }),
    JSON.stringify({ ...request, enabled: false }),
    JSON.stringify({ ...request, models: [] }),
    JSON.stringify({ ...request, unknown: true }),
  ])
    assert.equal(readAutomaticRequest(bad), null);
  // The manager can hold one model (the backend keeps two for legacy plans).
  const one = { ...request, models: ['current'] };
  assert.deepEqual(readAutomaticRequest(JSON.stringify(one)), one);
});

test('only matching receipts acknowledge a lost response, never a healthy or same-mode GET', () => {
  const request = automaticRequest(fixture(), true, id),
    state = fixture();
  state.automatic = { ...state.automatic, mode: 'on', phase: 'active' };
  assert.equal(optimizerRequestObserved(state, request), false);
  for (const status of [
    'pending',
    'starting',
    'waiting',
    'active',
    'blocked',
    'cancelled',
  ]) {
    const observed = {
      ...state,
      operation: { id, status, detail: 'Specific observed outcome' },
    };
    assert.equal(validOptimizerControl(observed), true);
    assert.equal(optimizerRequestObserved(observed, request), true);
  }
  assert.equal(
    optimizerRequestObserved({ ...state, lastRequestId: id }, request),
    true,
  );
  assert.equal(
    optimizerRequestObserved(
      { ...state, automatic: { ...state.automatic, intentId: id } },
      request,
    ),
    true,
  );
  assert.equal(
    optimizerRequestObserved({ ...state, lastRequestId: 'different' }, request),
    false,
  );
});

test('freshness matches the server admission window and phases do not imply earnings', () => {
  const state = fixture();
  assert.equal(optimizerControlFresh(state, 114), true);
  assert.equal(optimizerControlFresh(state, 115), false);
  assert.equal(optimizerControlFresh(state, 90), false);
  assert.equal(optimizerControlFresh(null, 100), false);
  for (const [phase, label] of [
    ['starting', 'Preparing to start'],
    ['waiting', 'Getting ready'],
    ['blocked', 'Needs attention'],
    ['active', 'Optimizer on'],
  ])
    assert.equal(
      optimizerPhaseLabel({
        ...state,
        automatic: { ...state.automatic, phase },
      }),
      label,
    );
  assert.equal(
    optimizerPhaseLabel({ ...state, providerRunning: false }),
    'Darkbloom stopped',
  );
});
