export type OptimizerConfirmation = {
  model: string;
  seconds: number;
  requiredSeconds: number;
  status?: 'confirming' | 'paused' | 'ready';
  reason?: string | null;
  expiresAt?: number | null;
};
export type OptimizerCandidate = {
  model: string;
  name: string;
  group: 'comparison_target' | 'paid_alternative' | 'held' | 'unknown';
  eligible: boolean;
  firstBlocker: string | null;
  kind: 'earnings' | 'explore' | null;
  selectionReason: string | null;
  meanUsdPerWarmHour: number | null;
  lowerUsdPerWarmHour: number | null;
  upperUsdPerWarmHour: number | null;
  netGainUsd: number | null;
  load: number | null;
};
export type OptimizerLive = {
  schemaVersion: 1;
  at: number;
  mode: string;
  currentModel: string | null;
  phase:
    | 'off'
    | 'waiting'
    | 'watching'
    | 'confirming'
    | 'switching'
    | 'measuring';
  reason: string;
  fresh: boolean;
  lastComparisonAt: number | null;
  sourceAt: number | null;
  comparisonTarget: string | null;
  proposalTarget: string | null;
  pendingTarget: string | null;
  earliestEligibleAt: number | null;
  planningMinutes: number | null;
  currentPaidBasis: {
    meanUsdPerWarmHour: number | null;
    lowerUsdPerWarmHour: number | null;
    upperUsdPerWarmHour: number | null;
    liveGuardUsdPerHour: number | null;
    recentGuardUsdPerHour: number | null;
    asOf: number | null;
    basis: 'recent_paid' | 'matched_history' | 'limited_history' | 'unknown';
    detail: string;
  } | null;
  candidates: OptimizerCandidate[];
  measurement?: {
    model: string;
    status: string;
    warmSeconds: number;
    trialMinutes: number;
  } | null;
  confirmation?: OptimizerConfirmation | null;
};
const object = (v: any) => v && typeof v === 'object' && !Array.isArray(v);
const string = (v: any) => typeof v === 'string' && v.length <= 2000;
const finite = (v: any) => typeof v === 'number' && Number.isFinite(v);
const number = (v: any) => v === null || finite(v);
const nullableString = (v: any) => v === null || string(v);
export function readOptimizerLive(value: unknown): OptimizerLive {
  const v = value as any;
  const fail = () => {
    throw Error('Waiting for a complete optimizer reading.');
  };
  if (
    !object(v) ||
    v.schemaVersion !== 1 ||
    !finite(v.at) ||
    v.at <= 0 ||
    !string(v.mode) ||
    !nullableString(v.currentModel) ||
    ![
      'off',
      'waiting',
      'watching',
      'confirming',
      'switching',
      'measuring',
    ].includes(v.phase) ||
    !string(v.reason) ||
    typeof v.fresh !== 'boolean' ||
    ![
      'lastComparisonAt',
      'sourceAt',
      'earliestEligibleAt',
      'planningMinutes',
    ].every((k) => number(v[k]) && (v[k] === null || v[k] >= 0)) ||
    !['comparisonTarget', 'proposalTarget', 'pendingTarget'].every((k) =>
      nullableString(v[k]),
    ) ||
    !Array.isArray(v.candidates) ||
    v.candidates.length > 16
  )
    return fail();
  const ids = new Set<string>();
  for (const r of v.candidates) {
    if (
      !object(r) ||
      !string(r.model) ||
      !r.model ||
      r.model === v.currentModel ||
      ids.has(r.model) ||
      !string(r.name) ||
      !['comparison_target', 'paid_alternative', 'held', 'unknown'].includes(
        r.group,
      ) ||
      typeof r.eligible !== 'boolean' ||
      !nullableString(r.firstBlocker) ||
      !nullableString(r.selectionReason) ||
      !['earnings', 'explore', null].includes(r.kind) ||
      ![
        'meanUsdPerWarmHour',
        'lowerUsdPerWarmHour',
        'upperUsdPerWarmHour',
        'netGainUsd',
        'load',
      ].every((k) => number(r[k])) ||
      (r.group === 'comparison_target' && r.model !== v.comparisonTarget) ||
      (r.group === 'paid_alternative' &&
        (!r.eligible || r.kind !== 'earnings' || r.netGainUsd === null))
    )
      return fail();
    ids.add(r.model);
  }
  const p = v.currentPaidBasis;
  if (
    p !== null &&
    (!object(p) ||
      ![
        'meanUsdPerWarmHour',
        'lowerUsdPerWarmHour',
        'upperUsdPerWarmHour',
        'liveGuardUsdPerHour',
        'recentGuardUsdPerHour',
        'asOf',
      ].every((k) => number(p[k])) ||
      ![
        'recent_paid',
        'matched_history',
        'limited_history',
        'unknown',
      ].includes(p.basis) ||
      !string(p.detail))
  )
    return fail();
  if (
    !v.fresh &&
    (v.candidates.length || p !== null || v.comparisonTarget !== null)
  )
    return fail();
  if (
    v.measurement != null &&
    (!object(v.measurement) ||
      !string(v.measurement.model) ||
      v.measurement.model !== v.currentModel ||
      !['running', 'settling'].includes(v.measurement.status) ||
      !finite(v.measurement.warmSeconds) ||
      v.measurement.warmSeconds < 0 ||
      !finite(v.measurement.trialMinutes) ||
      v.measurement.trialMinutes <= 0)
  )
    return fail();
  if (
    v.confirmation != null &&
    (!object(v.confirmation) ||
      !string(v.confirmation.model) ||
      v.confirmation.model !== v.proposalTarget ||
      !finite(v.confirmation.seconds) ||
      v.confirmation.seconds < 0 ||
      !finite(v.confirmation.requiredSeconds) ||
      v.confirmation.requiredSeconds <= 0 ||
      !validConfirmationState(v.confirmation))
  )
    return fail();
  return v;
}
export function optimizerLiveGroups(rows: OptimizerCandidate[]) {
  return {
    target: rows.filter((r) => r.group === 'comparison_target'),
    paid: rows
      .filter((r) => r.group === 'paid_alternative')
      .sort((a, b) => b.netGainUsd! - a.netGainUsd!),
    held: rows.filter((r) => r.group === 'held'),
    unknown: rows.filter((r) => r.group === 'unknown'),
  };
}

/** Optional fields preserve compatibility with the previous personal build. */
function validConfirmationState(value: OptimizerConfirmation): boolean {
  if (
    value.status != null &&
    !['confirming', 'paused', 'ready'].includes(value.status)
  )
    return false;
  if (
    value.reason != null &&
    (typeof value.reason !== 'string' || value.reason.length > 2000)
  )
    return false;
  if (
    value.expiresAt != null &&
    (!Number.isFinite(value.expiresAt) || value.expiresAt <= 0)
  )
    return false;
  if (value.status === 'paused' && !value.reason?.trim()) return false;
  if (value.status === 'ready' && value.seconds < value.requiredSeconds)
    return false;
  return true;
}
export function confirmationPresentation(
  value: OptimizerConfirmation,
  now: number,
) {
  const expired = value.expiresAt != null && now >= value.expiresAt;
  const clock = (seconds: number) =>
    `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, '0')}`;
  const seconds = Math.max(0, Math.min(value.seconds, value.requiredSeconds));
  const state = expired ? 'expired' : (value.status ?? 'confirming');
  return {
    state,
    seconds,
    label:
      state === 'expired'
        ? 'Confirmation needs a fresh check'
        : state === 'paused'
          ? 'Confirmation paused'
          : state === 'ready'
            ? 'Earnings confirmed'
            : 'Confirming earnings',
    progress: `${clock(seconds)} / ${clock(value.requiredSeconds)}`,
    detail: expired
      ? 'Saved progress has expired. Waiting for the controller’s next reading.'
      : value.reason ||
        'Fresh earnings, demand and safety checks are still required before switching.',
  };
}
