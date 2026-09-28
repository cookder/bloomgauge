export type OptimizerControlState = {
  at: number;
  controlVersion: string;
  providerVersion?: string | null;
  currentModel?: string | null;
  actualMode: string;
  lastRequestId?: string | null;
  operation?: { id: string; status: string; detail: string } | null;
  providerRunning: boolean;
  hasSavedPlan: boolean;
  firstPlan: boolean;
  selected: string[];
  demandPolicy: Record<string, number>;
  models: {
    id: string;
    name: string;
    available: boolean;
    reason?: string | null;
    selected?: boolean;
  }[];
  automatic: {
    mode: 'on' | 'manual';
    phase: 'manual' | 'starting' | 'waiting' | 'active' | 'blocked';
    detail: string;
    intentId?: string;
    canEnable: boolean;
    blocker?: {
      code: string;
      action: 'retry' | 'configure' | 'manual' | 'controller' | 'upgrade';
    };
  };
  warmup?: { status?: string; detail?: string };
  /** Compact manager status (lib/optimizer-manager.ts readManagerSummary); null under legacy. */
  manager?: Record<string, unknown> | null;
  /** The effective strategy (native/optimizer_control.py); absent on older backends. */
  strategy?: 'manager' | 'legacy' | null;
};

export function validOptimizerControl(
  value: unknown,
): value is OptimizerControlState {
  if (!record(value)) return false;
  const v = value as OptimizerControlState;
  const a = v.automatic;
  return (
    Number.isFinite(v.at) &&
    v.at > 0 &&
    typeof v.controlVersion === 'string' &&
    !!v.controlVersion &&
    (v.providerVersion == null || typeof v.providerVersion === 'string') &&
    (v.currentModel == null || typeof v.currentModel === 'string') &&
    typeof v.actualMode === 'string' &&
    record(v.demandPolicy) &&
    (v.lastRequestId == null || typeof v.lastRequestId === 'string') &&
    (v.operation == null ||
      (record(v.operation) &&
        typeof v.operation.id === 'string' &&
        !!v.operation.id &&
        [
          'pending',
          'starting',
          'waiting',
          'active',
          'blocked',
          'cancelled',
        ].includes(v.operation.status) &&
        typeof v.operation.detail === 'string')) &&
    Object.values(v.demandPolicy).every(
      (n) => typeof n === 'number' && Number.isFinite(n),
    ) &&
    typeof v.providerRunning === 'boolean' &&
    typeof v.hasSavedPlan === 'boolean' &&
    typeof v.firstPlan === 'boolean' &&
    modelIds(v.selected, 256) &&
    Array.isArray(v.models) &&
    v.models.length <= 256 &&
    v.models.every(
      (m) =>
        record(m) &&
        typeof m.id === 'string' &&
        !!m.id &&
        typeof m.name === 'string' &&
        typeof m.available === 'boolean' &&
        (m.reason == null || typeof m.reason === 'string') &&
        (m.selected == null || typeof m.selected === 'boolean'),
    ) &&
    record(a) &&
    ['on', 'manual'].includes(a.mode) &&
    ['manual', 'starting', 'waiting', 'active', 'blocked'].includes(a.phase) &&
    typeof a.detail === 'string' &&
    typeof a.canEnable === 'boolean' &&
    (a.blocker == null ||
      (record(a.blocker) &&
        typeof a.blocker.code === 'string' &&
        ['retry', 'configure', 'manual', 'controller', 'upgrade'].includes(
          a.blocker.action,
        ))) &&
    (a.intentId == null || typeof a.intentId === 'string') &&
    (v.warmup == null ||
      (record(v.warmup) &&
        (v.warmup.status == null || typeof v.warmup.status === 'string') &&
        (v.warmup.detail == null ||
          typeof v.warmup.detail === 'string'))) &&
    // Read field by field later (readManagerSummary): only its shape is checked here.
    (v.manager == null || record(v.manager)) &&
    (v.strategy == null || v.strategy === 'manager' || v.strategy === 'legacy')
  );
}

const record = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === 'object' && !Array.isArray(value);
const modelIds = (value: unknown, limit: number): value is string[] =>
  Array.isArray(value) &&
  value.length <= limit &&
  value.every((id) => typeof id === 'string' && !!id) &&
  new Set(value).size === value.length;

export type AutomaticRequest = {
  action: 'set-automatic';
  enabled: boolean;
  requestId: string;
  expectedControl: string;
  expectedProvider?: string;
  models?: string[];
  demandPolicy?: Record<string, number>;
};
export const automaticRequestKey = 'bloom-optimizer-pending-v1';

/** Manager: release the pinned model; the manager chooses the home model again. */
export type ReleasePinRequest = {
  action: 'release-pin';
  requestId: string;
  expectedControl: string;
};
export function releasePinRequest(
  state: OptimizerControlState,
  requestId: string,
): ReleasePinRequest {
  return {
    action: 'release-pin',
    requestId,
    expectedControl: state.controlVersion,
  };
}

/** Manager: "Keep current" on a planned home change; `model` becomes the pin. */
export type KeepCurrentRequest = {
  action: 'keep-current';
  requestId: string;
  expectedControl: string;
  model: string;
};
export function keepCurrentRequest(
  state: OptimizerControlState,
  model: string,
  requestId: string,
): KeepCurrentRequest {
  return {
    action: 'keep-current',
    requestId,
    expectedControl: state.controlVersion,
    model,
  };
}

export function initialOptimizerModels(state: OptimizerControlState): string[] {
  // The stopped/warming current model remains part of the reviewed first plan.
  // Its runtime eligibility is checked by the guarded backend before enabling.
  // A selection saved from the plan settings wins over "every available model".
  const available = state.models.filter((m) => m.available).map((m) => m.id);
  const saved = state.selected.filter((id) => available.includes(id));
  return [
    ...new Set([state.currentModel, ...(saved.length ? saved : available)]),
  ]
    .filter((id): id is string => !!id)
    .slice(0, 16);
}

export function automaticRequest(
  state: OptimizerControlState,
  enabled: boolean,
  requestId: string,
): AutomaticRequest {
  return {
    action: 'set-automatic',
    enabled,
    requestId,
    expectedControl: state.controlVersion,
    ...(enabled && state.providerVersion
      ? { expectedProvider: state.providerVersion }
      : {}),
    ...(enabled && state.firstPlan
      ? {
          models: initialOptimizerModels(state),
          demandPolicy: { ...state.demandPolicy },
        }
      : {}),
  };
}

export function readAutomaticRequest(
  raw: string | null,
): AutomaticRequest | null {
  try {
    const v: unknown = JSON.parse(raw || 'null');
    if (
      !record(v) ||
      v.action !== 'set-automatic' ||
      typeof v.enabled !== 'boolean' ||
      typeof v.requestId !== 'string' ||
      !/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(v.requestId) ||
      typeof v.expectedControl !== 'string' ||
      !v.expectedControl ||
      Object.keys(v).some(
        (k) =>
          ![
            'action',
            'enabled',
            'requestId',
            'expectedControl',
            'expectedProvider',
            'models',
            'demandPolicy',
          ].includes(k),
      ) ||
      (v.expectedProvider !== undefined &&
        typeof v.expectedProvider !== 'string') ||
      // The manager can hold one model; the backend enforces two for legacy plans.
      (v.models !== undefined &&
        (!modelIds(v.models, 16) || v.models.length < 1)) ||
      (v.demandPolicy !== undefined &&
        (!record(v.demandPolicy) ||
          !Object.values(v.demandPolicy).every(
            (n) => typeof n === 'number' && Number.isFinite(n),
          ))) ||
      (!v.enabled &&
        ['expectedProvider', 'models', 'demandPolicy'].some((k) => k in v))
    )
      return null;
    return v as AutomaticRequest;
  } catch {
    return null;
  }
}

export function optimizerRequestObserved(
  state: OptimizerControlState,
  request: AutomaticRequest,
): boolean {
  return (
    state.lastRequestId === request.requestId ||
    state.automatic.intentId === request.requestId ||
    state.operation?.id === request.requestId
  );
}

export function optimizerControlFresh(
  state: OptimizerControlState | null,
  now = Date.now() / 1000,
): boolean {
  return !!state && now - state.at < 15 && now - state.at >= -5;
}

export function optimizerPhaseLabel(state: OptimizerControlState): string {
  if (state.automatic.phase === 'starting') return 'Preparing to start';
  if (state.automatic.phase === 'waiting') return 'Getting ready';
  if (state.automatic.phase === 'blocked')
    return state.providerRunning
      ? 'Needs attention'
      : 'Optimizer could not start';
  if (state.automatic.phase === 'active') return 'Optimizer on';
  return state.providerRunning ? 'Manual · model running' : 'Darkbloom stopped';
}
