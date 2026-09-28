export type TargetHour = {
  at: number;
  end: number;
  from: number;
  to: number;
  usd: number;
  inferenceUsd: number;
  baseUsd: number;
  /** 'complete': a full, covered hour when no goal is set. */
  status: 'met' | 'below' | 'complete' | 'unknown' | 'partial' | 'settling';
  covered: boolean;
};
export type TargetReport = {
  at: number;
  from: number;
  to: number;
  requestedFrom: number;
  historyStart: number | null;
  model: string | null;
  models: string[];
  /** The user's goal; null until they choose one (no built-in target). */
  targetUsdPerHour: number | null;
  dailyTargetUsd: number | null;
  usd: number;
  inferenceUsd: number;
  accountBaseUsd: number;
  includesBase: boolean;
  coveredSeconds: number;
  rangeSeconds: number;
  completeCoverage: boolean;
  clockUsdPerHour: number | null;
  completeHours: number;
  metHours: number | null;
  unknownHours: number;
  metPercent: number | null;
  completeHourAverageUsd: number | null;
  longestBelowHours: number | null;
  switchSeconds: number;
  switchCount: number;
  hourly: TargetHour[];
  chartTruncated: boolean;
  scope: string;
  method: string;
};
const object = (v: unknown): v is Record<string, unknown> =>
  !!v && typeof v === 'object' && !Array.isArray(v);
const finite = (v: unknown) => typeof v === 'number' && Number.isFinite(v);
export function validTargetReport(v: unknown): v is TargetReport {
  if (
    !object(v) ||
    ![
      'at',
      'from',
      'to',
      'requestedFrom',
      'usd',
      'inferenceUsd',
      'accountBaseUsd',
      'coveredSeconds',
      'rangeSeconds',
      'completeHours',
      'unknownHours',
      'switchSeconds',
      'switchCount',
    ].every((k) => finite(v[k])) ||
    ![
      'historyStart',
      'clockUsdPerHour',
      'metPercent',
      'completeHourAverageUsd',
    ].every((k) => v[k] === null || finite(v[k])) ||
    // Goal fields are all numbers with a goal, all null without one.
    ![
      'targetUsdPerHour',
      'dailyTargetUsd',
      'metHours',
      'longestBelowHours',
    ].every((k) =>
      v.targetUsdPerHour === null ? v[k] === null : finite(v[k]),
    ) ||
    !['includesBase', 'completeCoverage', 'chartTruncated'].every(
      (k) => typeof v[k] === 'boolean',
    ) ||
    !(v.model === null || typeof v.model === 'string') ||
    typeof v.scope !== 'string' ||
    typeof v.method !== 'string' ||
    !Array.isArray(v.models) ||
    !v.models.every((m) => typeof m === 'string') ||
    !Array.isArray(v.hourly) ||
    v.hourly.length > 745
  )
    return false;
  return (
    Number(v.to) > Number(v.from) &&
    (v.targetUsdPerHour === null ||
      (Number(v.targetUsdPerHour) > 0 &&
        Number(v.metHours) >= 0 &&
        Number(v.metHours) <= Number(v.completeHours))) &&
    (v.metPercent === null ||
      (v.targetUsdPerHour !== null &&
        Number(v.metPercent) >= 0 &&
        Number(v.metPercent) <= 100)) &&
    v.hourly.every(
      (h) =>
        object(h) &&
        ['at', 'end', 'from', 'to', 'usd', 'inferenceUsd', 'baseUsd'].every(
          (k) => finite(h[k]),
        ) &&
        Number(h.end) - Number(h.at) === 3600 &&
        Number(h.to) > Number(h.from) &&
        typeof h.covered === 'boolean' &&
        [
          ...(v.targetUsdPerHour === null ? ['complete'] : ['met', 'below']),
          'unknown',
          'partial',
          'settling',
        ].includes(String(h.status)),
    )
  );
}
