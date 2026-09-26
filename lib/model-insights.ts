export type ObservedModelRate = {
  status: 'observed' | 'unknown';
  usdPerWarmHour: number | null;
  confirmedInferenceUsd: number | null;
  warmHours: number;
  days: number;
  creditedRequests: number;
  adjustments: number;
  asOf: number | null;
  reason: string;
};
export type ModelRequestSize = {
  status: 'measured' | 'unknown';
  meanOutputTokens: number | null;
  outputSamples: number;
  creditedRequests: number;
  outputCoverage: number | null;
  asOf: number | null;
  reason: string;
};
export type ModelDemandOutlook = {
  scope: 'whole_network_model';
  status: 'historical_pattern' | 'partial' | 'unknown';
  meanConcurrentRequests: number | null;
  supportedSeconds: number;
  minimumDates: number;
  qualifiedHistoryHours: number;
  basis: 'weekday_hour' | 'daytype_hour' | 'mixed' | null;
  asOf: number | null;
  reason: string;
};
export type ModelIncomeOutlook = {
  status: 'conditional' | 'partial' | 'unknown';
  usdPerWarmHour: number | null;
  supportedSeconds: number;
  matchedWarmHours: number;
  matchedCreditedRequests: number;
  days: number;
  blocks: number;
  minimumSlotCoverage: number | null;
  basis: 'weekday_time' | 'daytype_time' | 'mixed' | null;
  asOf: number | null;
  reason: string;
};
export type ModelInsight = {
  id: string;
  observed: ObservedModelRate;
  requestSize: ModelRequestSize;
  demandNext8h: ModelDemandOutlook;
  incomeNext8h: ModelIncomeOutlook;
};
export type ModelInsights = {
  schemaVersion: 1;
  at: number;
  timezone: string;
  history: {
    from: number;
    to: number;
    settledThrough: number;
    lookbackDays: 28;
  };
  horizon: { from: number; to: number; seconds: 28800 };
  scope: 'this_mac_solo_inference';
  models: ModelInsight[];
};

/** Analytics is optional. Reject incomplete or wrong-scope reports rather than
 * presenting a partial-horizon prediction or missing tokens as measured zero. */
export function readModelInsights(
  value: unknown,
  timezone: string,
  models: string[],
): ModelInsights {
  const fail = () => {
    throw new Error(
      'Model statistics returned an incomplete report. You can still choose a model.',
    );
  };
  const obj = (x: any) => x && typeof x === 'object' && !Array.isArray(x);
  const finite = (x: any) => typeof x === 'number' && Number.isFinite(x);
  const nonnegative = (x: any) => finite(x) && x >= 0;
  const nullable = (x: any) => x === null || finite(x);
  const ratio = (x: any) => x === null || (nonnegative(x) && x <= 1);
  const reason = (x: any) => typeof x === 'string' && x.length <= 2000;
  const count = (x: any) => Number.isSafeInteger(x) && x >= 0;
  const v = value as any;
  if (
    !obj(v) ||
    v.schemaVersion !== 1 ||
    v.scope !== 'this_mac_solo_inference' ||
    v.timezone !== timezone ||
    !nonnegative(v.at) ||
    !obj(v.history) ||
    !obj(v.horizon) ||
    v.history.lookbackDays !== 28 ||
    !['from', 'to', 'settledThrough'].every((k) => nonnegative(v.history[k])) ||
    v.history.from > v.history.settledThrough ||
    v.history.settledThrough > v.history.to ||
    v.history.to > v.at ||
    v.horizon.seconds !== 28800 ||
    !nonnegative(v.horizon.from) ||
    !nonnegative(v.horizon.to) ||
    Math.abs(v.horizon.to - v.horizon.from - 28800) > 1 ||
    Math.abs(v.horizon.from - v.at) > 1 ||
    !Array.isArray(v.models) ||
    v.models.length !== models.length ||
    v.models.length > 32
  )
    return fail();
  const ids = new Set<string>();
  for (const m of v.models) {
    if (
      !obj(m) ||
      typeof m.id !== 'string' ||
      !models.includes(m.id) ||
      ids.has(m.id)
    )
      return fail();
    ids.add(m.id);
    const o = m.observed,
      r = m.requestSize,
      d = m.demandNext8h,
      i = m.incomeNext8h;
    if (
      ![o, r, d, i].every(
        (x) =>
          obj(x) &&
          (x.asOf === null || (nonnegative(x.asOf) && x.asOf <= v.at)) &&
          reason(x.reason),
      )
    )
      return fail();
    if (
      !['observed', 'unknown'].includes(o.status) ||
      !nullable(o.usdPerWarmHour) ||
      !nullable(o.confirmedInferenceUsd) ||
      !nonnegative(o.warmHours) ||
      !['days', 'creditedRequests', 'adjustments'].every((k) => count(o[k])) ||
      (o.status === 'observed') !== (o.usdPerWarmHour !== null)
    )
      return fail();
    if (
      !['measured', 'unknown'].includes(r.status) ||
      !(r.meanOutputTokens === null || nonnegative(r.meanOutputTokens)) ||
      !count(r.outputSamples) ||
      !count(r.creditedRequests) ||
      r.outputSamples > r.creditedRequests ||
      !ratio(r.outputCoverage) ||
      (r.status === 'measured') !== (r.meanOutputTokens !== null) ||
      (r.status === 'measured' && r.outputSamples === 0)
    )
      return fail();
    if (
      d.scope !== 'whole_network_model' ||
      !['historical_pattern', 'partial', 'unknown'].includes(d.status) ||
      !(
        d.meanConcurrentRequests === null ||
        nonnegative(d.meanConcurrentRequests)
      ) ||
      !nonnegative(d.supportedSeconds) ||
      d.supportedSeconds > 28800 ||
      !count(d.minimumDates) ||
      !nonnegative(d.qualifiedHistoryHours) ||
      !['weekday_hour', 'daytype_hour', 'mixed', null].includes(d.basis) ||
      (d.status === 'historical_pattern') !==
        (d.meanConcurrentRequests !== null) ||
      (d.meanConcurrentRequests !== null && d.supportedSeconds !== 28800)
    )
      return fail();
    if (
      !['conditional', 'partial', 'unknown'].includes(i.status) ||
      !nullable(i.usdPerWarmHour) ||
      !nonnegative(i.supportedSeconds) ||
      i.supportedSeconds > 28800 ||
      !nonnegative(i.matchedWarmHours) ||
      !['matchedCreditedRequests', 'days', 'blocks'].every((k) =>
        count(i[k]),
      ) ||
      !ratio(i.minimumSlotCoverage) ||
      !['weekday_time', 'daytype_time', 'mixed', null].includes(i.basis) ||
      (i.status === 'conditional') !== (i.usdPerWarmHour !== null) ||
      (i.usdPerWarmHour !== null && i.supportedSeconds !== 28800)
    )
      return fail();
  }
  return v;
}
