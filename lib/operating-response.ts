export type HistoryPoint = { at: number; [key: string]: number | null };
export type Tariff = {
  id: number;
  at: number;
  rate: number | null;
  label: string;
};
export type SmoothingData = {
  passive: true;
  method: string;
  variants: {
    halfLifeSeconds: number;
    observations: number;
    leaderChanges: number;
    reversals: number;
    agreementPercent: number | null;
    confirmedLeaders: number;
    unresolvedLeaders: number;
    meanFollowSeconds: number | null;
    resets: number;
    leader: string | null;
  }[];
};
type HistoryBase = {
  at: number;
  from: number;
  to: number;
  coverageStart: number | null;
  coverageEnd: number | null;
  bucketSeconds: number;
  samples: HistoryPoint[];
  method: string;
};
export type EnergyData = HistoryBase & {
  source: string;
  tariff: Tariff;
  tariffs: Tariff[];
  latest: { at: number; watts: number; powerSource: string } | null;
  totals: {
    seconds: number;
    acSeconds: number;
    kwh: number;
    costUsd: number | null;
  };
  comparison: {
    seconds: number;
    inferenceUsd: number;
    accountBaseUsd: number;
    costUsd: number;
    afterCostUsd: number | null;
  };
};
export type ConcurrencyData = HistoryBase & {
  latest: {
    at: number;
    session: number;
    models: string[];
    pending: number | null;
    providerLimit: number | null;
    running: number | null;
    waiting: number | null;
    slotLimit: number | null;
    score: number | null;
    responseTimeMs: number | null;
    failedJobs: number | null;
  } | null;
  fresh: boolean;
  sessions: { id: number; models: string[] }[];
  models: string[];
  observations: number;
  truncated: boolean;
  failureIncrements: number | null;
};
const object = (d: unknown): d is Record<string, unknown> =>
  !!d && typeof d === 'object' && !Array.isArray(d);
const finite = (d: unknown): d is number =>
  typeof d === 'number' && Number.isFinite(d);
const maybe = (d: unknown) => d === null || finite(d);
const strings = (d: unknown): d is string[] =>
  Array.isArray(d) && d.every((s) => typeof s === 'string');
export const validSmoothing = (d: unknown): d is SmoothingData =>
  object(d) &&
  d.passive === true &&
  typeof d.method === 'string' &&
  Array.isArray(d.variants) &&
  d.variants.every(
    (v) =>
      object(v) &&
      [
        'halfLifeSeconds',
        'observations',
        'leaderChanges',
        'reversals',
        'confirmedLeaders',
        'unresolvedLeaders',
        'resets',
      ].every((k) => finite(v[k])) &&
      maybe(v.agreementPercent) &&
      maybe(v.meanFollowSeconds) &&
      (v.leader === null || typeof v.leader === 'string'),
  );
const base = (d: unknown): boolean =>
  object(d) &&
  ['at', 'from', 'to', 'bucketSeconds'].every((k) => finite(d[k])) &&
  maybe(d.coverageStart) &&
  maybe(d.coverageEnd) &&
  typeof d.method === 'string' &&
  Array.isArray(d.samples) &&
  d.samples.every(
    (p) => object(p) && finite(p.at) && Object.values(p).every(maybe),
  );
export const validTariff = (d: unknown): d is Tariff =>
  object(d) &&
  Number.isInteger(d.id) &&
  finite(d.at) &&
  (d.rate === null || (finite(d.rate) && d.rate >= 0 && d.rate <= 10)) &&
  typeof d.label === 'string';
export function validEnergy(d: unknown): d is EnergyData {
  if (!object(d) || !base(d)) return false;
  const { totals, comparison, latest } = d;
  return (
    typeof d.source === 'string' &&
    validTariff(d.tariff) &&
    Array.isArray(d.tariffs) &&
    d.tariffs.every(validTariff) &&
    object(totals) &&
    ['seconds', 'acSeconds', 'kwh'].every((k) => finite(totals[k])) &&
    maybe(totals.costUsd) &&
    object(comparison) &&
    ['seconds', 'inferenceUsd', 'accountBaseUsd', 'costUsd'].every((k) =>
      finite(comparison[k]),
    ) &&
    maybe(comparison.afterCostUsd) &&
    (latest === null ||
      (object(latest) &&
        finite(latest.at) &&
        finite(latest.watts) &&
        typeof latest.powerSource === 'string'))
  );
}
export function validConcurrencyHistory(d: unknown): d is ConcurrencyData {
  if (!object(d) || !base(d)) return false;
  const { latest } = d;
  return (
    typeof d.fresh === 'boolean' &&
    strings(d.models) &&
    finite(d.observations) &&
    typeof d.truncated === 'boolean' &&
    maybe(d.failureIncrements) &&
    Array.isArray(d.sessions) &&
    d.sessions.every(
      (s) => object(s) && Number.isInteger(s.id) && strings(s.models),
    ) &&
    (latest === null ||
      (object(latest) &&
        finite(latest.at) &&
        Number.isInteger(latest.session) &&
        strings(latest.models) &&
        [
          'pending',
          'providerLimit',
          'running',
          'waiting',
          'slotLimit',
          'score',
          'responseTimeMs',
          'failedJobs',
        ].every((k) => maybe(latest[k]))))
  );
}
