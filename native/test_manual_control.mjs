import test from 'node:test';
import assert from 'node:assert/strict';
import {
  validManualControl,
  manualBlocker,
  manualControlFresh,
  currentManualWarmup,
  manualRequest,
  manualRequestObserved,
  readManualRequest,
} from '../lib/manual-control.ts';
import {
  cachePermissionResult,
  cachePermissionMessage,
} from '../lib/cache-permission.ts';
const id = 'aabbeeff-1111-2222-3333-123456789abc';
const snapshot = () => ({
  at: 100,
  session: 'old-gemma-session',
  mode: 'observe',
  currentModel: 'gemma',
  switching: false,
  canCancel: false,
  status: 'stopped',
  detail: 'Stopped',
  models: [
    {
      id: 'qwen',
      name: 'Qwen',
      available: false,
      reason: 'Automatic support held',
      canStart: true,
      startReason: null,
      canSwitch: true,
      switchReason: null,
      requiresRuntimeVerification: true,
    },
  ],
  providerControl: {
    status: 'stopped',
    version: 'stopped-version',
    model: 'gemma',
    detail: 'Stopped',
    canStart: true,
    canStop: false,
    canEnableEndpoint: true,
    selectionActionSupported: true,
    endpointSetupRequired: true,
  },
  warmup: { status: 'ready', detail: 'Old Gemma warm', model: 'gemma' },
});
test('stopped selection uses Qwen directly despite old Gemma session and automatic hold', () => {
  const s = snapshot();
  assert.ok(validManualControl(s));
  assert.equal(manualBlocker(s, 'qwen', 100), null);
  assert.deepEqual(manualRequest(s, 'qwen', id), {
    action: 'select',
    requestId: id,
    model: 'qwen',
    expectedProvider: 'stopped-version',
    expectedSession: 'old-gemma-session',
    verifyRuntime: true,
  });
  assert.equal(currentManualWarmup(s), undefined);
});
test('separate running eligibility and saved-model startup are respected', () => {
  const s = snapshot();
  s.models[0].canSwitch = false;
  s.models[0].switchReason = 'Waiting for matching identity';
  assert.equal(manualBlocker(s, 'qwen', 100), null);
  s.providerControl.status = 'running';
  assert.equal(manualBlocker(s, 'qwen', 100), 'Waiting for matching identity');
  s.models[0].canSwitch = true;
  assert.equal(manualBlocker(s, 'qwen', 100), null);
});
test('incomplete data, nonboolean availability, malformed queue and receipts fail closed', () => {
  for (const patch of [
    { at: NaN },
    { models: [null] },
    { models: [{ ...snapshot().models[0], canStart: 'yes' }] },
    {
      providerControl: {
        ...snapshot().providerControl,
        selectionActionSupported: 'yes',
      },
    },
    {
      queue: {
        idleSeconds: 1,
        requiredIdleSeconds: 0,
        fresh: true,
        activity: 'idle',
      },
    },
    {
      selectionResult: {
        id,
        model: 'qwen',
        status: 'completed',
        kind: 'start',
        at: null,
        detail: 'Done',
      },
    },
  ])
    assert.equal(validManualControl({ ...snapshot(), ...patch }), false);
});
test('old backend stays readable but cannot receive a target-aware start', () => {
  const s = snapshot();
  delete s.providerControl.selectionActionSupported;
  delete s.models[0].canStart;
  delete s.models[0].canSwitch;
  assert.ok(validManualControl(s));
  assert.match(manualBlocker(s, 'qwen', 100), /updated Bloomkeeper/);
});
test('stale, future, unknown and busy status block commands', () => {
  assert.equal(manualControlFresh(snapshot(), 115), false);
  assert.equal(manualControlFresh(snapshot(), 94), false);
  assert.equal(manualControlFresh(snapshot(), 114), true);
  const s = snapshot();
  s.providerControl.status = 'unknown';
  assert.match(manualBlocker(s, 'qwen', 100), /Checking whether/);
  s.providerControl.status = 'stopped';
  s.providerControl.operationPending = true;
  assert.match(manualBlocker(s, 'qwen', 100), /current model command/);
});
test('readiness needs the current running target, never stopped or a different model', () => {
  const s = snapshot();
  assert.equal(currentManualWarmup(s), undefined);
  s.providerControl.status = 'running';
  assert.equal(currentManualWarmup(s), s.warmup);
  s.currentModel = 'qwen';
  assert.equal(currentManualWarmup(s), undefined);
  s.warmup.model = 'qwen';
  s.switching = true;
  assert.equal(currentManualWarmup(s), undefined);
});
test('exact request survives reload without adopting new provider state or new target', () => {
  const s = snapshot(),
    request = manualRequest(s, 'qwen', id);
  const raw = JSON.stringify(request);
  s.session = 'replacement';
  s.providerControl.version = 'replacement';
  assert.deepEqual(readManualRequest(raw), request);
  assert.notEqual(request.expectedSession, s.session);
  for (const patch of [
    { action: 'provider-start' },
    { requestId: 'wrong' },
    { expectedProvider: '' },
    { verifyRuntime: 'yes' },
    { extra: true },
    { model: '' },
  ])
    assert.equal(
      readManualRequest(JSON.stringify({ ...request, ...patch })),
      null,
    );
});
test('only matching target and UUID acknowledge a selected operation', () => {
  const s = snapshot(),
    r = manualRequest(s, 'qwen', id);
  s.requestId = id;
  assert.equal(manualRequestObserved(s, r), false);
  s.selectionResult = {
    id,
    model: 'gemma',
    kind: 'start',
    status: 'completed',
    detail: 'Wrong target',
    at: 100,
  };
  assert.equal(manualRequestObserved(s, r), false);
  s.selectionResult.model = 'qwen';
  assert.equal(manualRequestObserved(s, r), true);
});
test('phone setup refusal uses an actionable model-specific reason', () => {
  const s = snapshot();
  s.remote = true;
  s.models[0].canStart = false;
  s.models[0].startReason = 'Open Bloomkeeper on the Mac once to set up pre-warming.';
  assert.equal(manualBlocker(s, 'qwen', 100), s.models[0].startReason);
});

test('preparing the same running model and choosing Manual remain explicit usable actions', () => {
  const s = snapshot();
  s.providerControl.status = 'running';
  s.currentModel = 'qwen';
  assert.equal(manualBlocker(s, 'qwen', 100), null);
  s.providerControl.endpointSetupRequired = false;
  assert.equal(manualBlocker(s, 'qwen', 100), 'This model is already running.');
  s.mode = 'demand';
  assert.equal(manualBlocker(s, 'qwen', 100), null);
});
test('a cleanup-ready row requires a current independent permission check before Start', () => {
  const s = snapshot();
  s.models[0].cacheRecovery = {
    needed: true,
    canAttempt: true,
    detail: 'Cleanup then measure',
    retryAt: null,
    authorization: 'ready',
  };
  s.providerControl.cacheRecoveryAuthorization = {
    status: 'ready',
    detail: 'Exact rule verified',
    checkedAt: 100,
  };
  assert.ok(validManualControl(s));
  assert.equal(manualBlocker(s, 'qwen', 100), null);
  for (const auth of [
    { status: 'required', checkedAt: 100 },
    { status: 'ready', checkedAt: 20 },
    { status: 'ready', checkedAt: 110 },
  ]) {
    s.providerControl.cacheRecoveryAuthorization = {
      ...auth,
      detail: 'Checking',
    };
    assert.match(manualBlocker(s, 'qwen', 100), /permission/);
  }
});
test('inconsistent or malformed cache recovery and permission data fail closed', () => {
  for (const cache of [
    {
      needed: true,
      canAttempt: true,
      authorization: 'required',
      detail: 'No grant',
      retryAt: null,
    },
    {
      needed: false,
      canAttempt: true,
      authorization: 'ready',
      detail: 'Wrong',
      retryAt: null,
    },
    {
      needed: true,
      canAttempt: 'yes',
      authorization: 'ready',
      detail: 'Wrong',
      retryAt: null,
    },
    {
      needed: true,
      canAttempt: false,
      authorization: 'ready',
      detail: 'Wrong',
      retryAt: NaN,
    },
  ]) {
    const s = snapshot();
    s.models[0].cacheRecovery = cache;
    assert.equal(validManualControl(s), false);
  }
  const s = snapshot();
  s.providerControl.cacheRecoveryAuthorization = {
    status: 'ready',
    detail: 'Wrong',
    checkedAt: NaN,
  };
  assert.equal(validManualControl(s), false);
});
test('permission replies correlate with the active request and never represent start authority', () => {
  assert.equal(
    cachePermissionResult({ requestId: 'different', status: 'completed' }, id),
    null,
  );
  assert.equal(
    cachePermissionResult({ requestId: id, status: 'ready' }, id),
    null,
  );
  assert.equal(
    cachePermissionResult({ requestId: id, status: 'completed' }, id).status,
    'completed',
  );
  assert.match(cachePermissionMessage('completed'), /Checking/);
  assert.match(cachePermissionMessage('cancelled'), /unchanged/);
});
