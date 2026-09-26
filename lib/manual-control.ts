export type CacheAuthorization = {
  status: 'ready' | 'required' | 'unknown';
  detail: string;
  checkedAt: number;
};
export type ManualModel = {
  id: string;
  name: string;
  available: boolean;
  reason?: string | null;
  canStart: boolean;
  startReason: string | null;
  canSwitch: boolean;
  switchReason: string | null;
  requiresRuntimeVerification: boolean;
  memoryGB?: number | null;
  loadBudget?: { afterUnloadGB: number; requiredGB: number } | null;
  cacheRecovery?: {
    needed: boolean;
    canAttempt: boolean;
    detail: string;
    retryAt: number | null;
    authorization: CacheAuthorization['status'];
  };
};
export type ManualControl = {
  at: number;
  remote?: boolean;
  currentModel?: string | null;
  session: string;
  mode: string;
  models: ManualModel[];
  queuedModel?: string | null;
  switching: boolean;
  switchingModel?: string | null;
  requestId?: string | null;
  canCancel: boolean;
  status: string;
  detail: string;
  controlError?: string | null;
  providerControl: {
    status: string;
    version: string;
    model?: string | null;
    detail: string;
    canStart: boolean;
    canStop: boolean;
    canEnableEndpoint: boolean;
    selectionActionSupported?: boolean;
    endpointSetupRequired?: boolean;
    endpoint?: string;
    endpointDetail?: string;
    operationPending?: boolean;
    configurationIssue?: string | null;
    lastResult?: { id: string; status: string; detail: string } | null;
    cacheRecoveryAuthorization?: CacheAuthorization;
  };
  selectionResult?: {
    id: string;
    model: string;
    status: string;
    detail: string;
    at: number;
    kind: 'start' | 'switch';
  } | null;
  warmup?: { status: string; detail: string; model?: string } | null;
  queue?: {
    ageSeconds?: number | null;
    idleSeconds: number;
    requiredIdleSeconds: number;
    activity: 'busy' | 'idle' | 'unknown';
    fresh: boolean;
    pauseInSeconds?: number | null;
    pausingForSwitch?: boolean;
  } | null;
};
const record = (v: unknown): v is Record<string, unknown> =>
  !!v && typeof v === 'object' && !Array.isArray(v);
const string = (v: unknown): v is string => typeof v === 'string';
const number = (v: unknown): v is number =>
  typeof v === 'number' && Number.isFinite(v);
const optionalString = (v: unknown) => v == null || string(v);
const optionalNumber = (v: unknown) => v == null || number(v);
const optionalBool = (v: unknown) => v == null || typeof v === 'boolean';
const receipt = (v: unknown) =>
  record(v) && string(v.id) && !!v.id && string(v.status) && string(v.detail);
const cacheAuthorizationStatus = (v: unknown) =>
  ['ready', 'required', 'unknown'].includes(String(v));

export function validManualControl(v: unknown): v is ManualControl {
  if (
    !record(v) ||
    !number(v.at) ||
    v.at <= 0 ||
    !string(v.session) ||
    !string(v.mode) ||
    !string(v.status) ||
    !string(v.detail) ||
    typeof v.switching !== 'boolean' ||
    typeof v.canCancel !== 'boolean' ||
    !optionalBool(v.remote) ||
    ![
      'currentModel',
      'queuedModel',
      'switchingModel',
      'requestId',
      'controlError',
    ].every((k) => optionalString(v[k])) ||
    !Array.isArray(v.models) ||
    v.models.length > 256 ||
    new Set(v.models.filter(record).map((m) => m.id)).size !== v.models.length
  )
    return false;
  const p = v.providerControl;
  if (
    !record(p) ||
    !string(p.version) ||
    !p.version ||
    !string(p.status) ||
    !string(p.detail) ||
    !optionalString(p.model) ||
    !['canStart', 'canStop', 'canEnableEndpoint'].every(
      (k) => typeof p[k] === 'boolean',
    ) ||
    ![
      'selectionActionSupported',
      'endpointSetupRequired',
      'operationPending',
    ].every((k) => optionalBool(p[k])) ||
    !['endpoint', 'endpointDetail', 'configurationIssue'].every((k) =>
      optionalString(p[k]),
    ) ||
    (p.lastResult != null && !receipt(p.lastResult))
  )
    return false;
  const auth = p.cacheRecoveryAuthorization;
  if (
    auth != null &&
    (!record(auth) ||
      !cacheAuthorizationStatus(auth.status) ||
      !string(auth.detail) ||
      !number(auth.checkedAt) ||
      auth.checkedAt <= 0)
  )
    return false;
  for (const m of v.models) {
    if (
      !record(m) ||
      !string(m.id) ||
      !m.id ||
      !string(m.name) ||
      typeof m.available !== 'boolean' ||
      !optionalString(m.reason) ||
      !optionalNumber(m.memoryGB)
    )
      return false;
    // Older backends remain readable, but the new action is never enabled without its contract.
    if (
      p.selectionActionSupported === true &&
      (!['canStart', 'canSwitch', 'requiresRuntimeVerification'].every(
        (k) => typeof m[k] === 'boolean',
      ) ||
        !optionalString(m.startReason) ||
        !optionalString(m.switchReason))
    )
      return false;
    if (
      m.loadBudget != null &&
      (!record(m.loadBudget) ||
        !number(m.loadBudget.afterUnloadGB) ||
        !number(m.loadBudget.requiredGB))
    )
      return false;
    const cache = m.cacheRecovery;
    if (
      cache != null &&
      (!record(cache) ||
        typeof cache.needed !== 'boolean' ||
        typeof cache.canAttempt !== 'boolean' ||
        !string(cache.detail) ||
        !optionalNumber(cache.retryAt) ||
        !cacheAuthorizationStatus(cache.authorization) ||
        (cache.canAttempt &&
          (!cache.needed || cache.authorization !== 'ready')))
    )
      return false;
  }
  const r = v.selectionResult;
  if (
    r != null &&
    (!receipt(r) ||
      !record(r) ||
      !string(r.model) ||
      !r.model ||
      !number(r.at) ||
      !['start', 'switch'].includes(String(r.kind)) ||
      ![
        'queued',
        'working',
        'completed',
        'failed',
        'cancelled',
        'unchanged',
      ].includes(String(r.status)))
  )
    return false;
  const w = v.warmup;
  if (
    w != null &&
    (!record(w) ||
      !string(w.status) ||
      !string(w.detail) ||
      !optionalString(w.model))
  )
    return false;
  const q = v.queue;
  return (
    q == null ||
    (record(q) &&
      number(q.idleSeconds) &&
      q.idleSeconds >= 0 &&
      number(q.requiredIdleSeconds) &&
      q.requiredIdleSeconds > 0 &&
      ['busy', 'idle', 'unknown'].includes(String(q.activity)) &&
      typeof q.fresh === 'boolean' &&
      optionalNumber(q.ageSeconds) &&
      optionalNumber(q.pauseInSeconds) &&
      optionalBool(q.pausingForSwitch))
  );
}

export type ManualRequest =
  | {
      action: 'select';
      requestId: string;
      model: string;
      expectedProvider: string;
      expectedSession: string;
      verifyRuntime: boolean;
    }
  | { action: 'provider-stop'; requestId: string; expectedProvider: string };
export const manualRequestKey = 'bloom-manual-pending-v1';
export function readManualRequest(raw: string | null): ManualRequest | null {
  try {
    const v: unknown = JSON.parse(raw || 'null');
    if (
      !record(v) ||
      !string(v.requestId) ||
      !/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(v.requestId) ||
      !string(v.expectedProvider) ||
      !v.expectedProvider
    )
      return null;
    const keys =
      v.action === 'select'
        ? [
            'action',
            'requestId',
            'model',
            'expectedProvider',
            'expectedSession',
            'verifyRuntime',
          ]
        : ['action', 'requestId', 'expectedProvider'];
    if (Object.keys(v).some((k) => !keys.includes(k))) return null;
    if (v.action === 'provider-stop') return v as ManualRequest;
    return v.action === 'select' &&
      string(v.model) &&
      !!v.model &&
      string(v.expectedSession) &&
      typeof v.verifyRuntime === 'boolean'
      ? (v as ManualRequest)
      : null;
  } catch {
    return null;
  }
}
export function manualRequest(
  state: ManualControl,
  model: string,
  requestId: string,
): ManualRequest {
  return {
    action: 'select',
    requestId,
    model,
    expectedProvider: state.providerControl.version,
    expectedSession: state.session,
    verifyRuntime: !!state.models.find((m) => m.id === model)
      ?.requiresRuntimeVerification,
  };
}
export function manualRequestObserved(
  state: ManualControl,
  request: ManualRequest,
): boolean {
  if (request.action === 'provider-stop')
    return state.providerControl.lastResult?.id === request.requestId;
  return (
    state.selectionResult?.id === request.requestId &&
    state.selectionResult.model === request.model
  );
}
export function manualControlFresh(
  state: ManualControl | null,
  now = Date.now() / 1000,
): boolean {
  return !!state && now - state.at < 15 && now - state.at >= -5;
}
export function currentManualWarmup(state: ManualControl | null) {
  return state?.providerControl.status === 'running' &&
    !state.switching &&
    !state.providerControl.operationPending &&
    state.warmup?.model &&
    state.warmup.model === state.currentModel
    ? state.warmup
    : undefined;
}
export function manualBlocker(
  state: ManualControl | null,
  model: string,
  now = Date.now() / 1000,
): string | null {
  if (!manualControlFresh(state, now))
    return 'Waiting for fresh model status. Retrying automatically.';
  if (!state!.providerControl.selectionActionSupported)
    return 'These model controls need the updated Bloomkeeper app on this Mac.';
  const p = state!.providerControl,
    target = state!.models.find((m) => m.id === model);
  if (
    p.operationPending ||
    state!.queuedModel ||
    state!.switching ||
    (state!.selectionResult &&
      ['queued', 'working'].includes(state!.selectionResult.status))
  )
    return 'Waiting for the current model command to finish.';
  if (!['stopped', 'running'].includes(p.status))
    return 'Checking whether Darkbloom is running. Your selection is saved here.';
  if (!target) return 'Choose a downloaded model to continue.';
  const stopped = p.status === 'stopped';
  if (
    stopped &&
    target.cacheRecovery?.needed &&
    target.canStart &&
    (!target.cacheRecovery.canAttempt ||
      target.cacheRecovery.authorization !== 'ready' ||
      p.cacheRecoveryAuthorization?.status !== 'ready' ||
      now - p.cacheRecoveryAuthorization.checkedAt > 45 ||
      now - p.cacheRecoveryAuthorization.checkedAt < -5)
  )
    return 'Checking cache cleanup permission before starting this model. Refresh status to check again.';
  if (!(stopped ? target.canStart : target.canSwitch))
    return (
      (stopped ? target.startReason : target.switchReason) ||
      'This model is unavailable on this Mac right now.'
    );
  if (
    !stopped &&
    model === state!.currentModel &&
    state!.mode === 'observe' &&
    !p.endpointSetupRequired
  )
    return 'This model is already running.';
  return null;
}
