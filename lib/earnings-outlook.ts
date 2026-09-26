import {
  validConditionalBaseline,
  type ConditionalBaseline,
  type DemandObservation,
} from './optimizer-response';
export type EarningsBand = DemandObservation & {
  low: number;
  high: number | null;
  blocks: number;
  quality: 'none' | 'limited' | 'repeated' | 'older';
  excludedThinHours: number;
};

export type EarningsForecast = {
  methodVersion: string;
  state: 'estimate' | 'unavailable' | 'held' | 'expired';
  origin: number;
  issuedAt: number;
  targetEnd: number;
  validUntil: number;
  refreshAfter: number;
  horizonSeconds: 3600;
  target: 'inference_usd_next_hour_if_already_warm_and_stays_selected_ready';
  condition: 'already_warm_and_same_model_selected_ready_for_entire_hour';
  assumption: string;
  usd: number | null;
  usdPerHour: number | null;
  basis: 'recent_paid_persistence' | 'completed_hour_history' | 'unavailable';
  reason: string;
  reasonCodes: string[];
  demandAsOf: number | null;
  paidEvidenceThrough: number | null;
  inputStatus: 'fresh' | 'demand_unavailable' | 'held';
  lookbackSeconds: number;
  support: {
    completedHours: number;
    distinctDates: number;
    recentCompleteMinutes: number;
    recentRequiredMinutes: number;
    lastOutcomeAt: number | null;
    lastPaidMinuteEnd: number | null;
    demandAdjusted: boolean;
  };
  uncertainty: {
    kind: 'unavailable' | 'descriptive_historical_spread';
    lower: number | null;
    upper: number | null;
    nominalCoverage: null;
    validationWindows: number;
    empiricalCoverage: null;
    label: string;
  };
  historicalFallback: {
    basis: 'completed_warm_hours';
    usdPerWarmHour: number | null;
    observedHours: number;
    asOf: number | null;
    isForecast: false;
  };
  validation: {
    status: string;
    prospectiveValidated: boolean;
    retrospectiveEvidence: string;
    creditAvailability: string;
  };
};
export type EarningsOutlookModel = {
  model: string;
  serving: boolean;
  eligible: boolean;
  signalAt: number | null;
  totalWarmHours: number;
  pairedWarmHours: number;
  current: ConditionalBaseline;
  bands: EarningsBand[];
  forecast?: EarningsForecast;
};
export type ForecastScore = {
  model: string;
  windows: number;
  censoredWindows: number;
  unavailableForecasts: number;
  pendingWindows: number;
  overlappingWindows: number;
  maeUSDPerHour: number | null;
  biasUSDPerHour: number | null;
};
export type ForecastEvaluation = {
  status: string;
  methodVersion: string;
  at?: number;
  recordedPackets?: number;
  models: ForecastScore[];
  basis?: string;
  creditOutcomeStatus?: string;
  intervalCalibration?: string;
};
export type EarningsOutlook = {
  at: number;
  from: number;
  to: number;
  models: EarningsOutlookModel[];
  scope: string;
  method: string;
  forecastEvaluation?: ForecastEvaluation;
  forecastPersistence?: string;
};
export const demandBands = [
  [0, 0.25],
  [0.25, 0.5],
  [0.5, 1],
  [1, 2],
  [2, 4],
  [4, 8],
  [8, null],
] as const;
export const bandLabel = (low: number, high: number | null) =>
  high == null ? `${low}+` : `${low}–<${high}`;
const finite = (v: unknown): v is number =>
  typeof v === 'number' && Number.isFinite(v);
const record = (v: unknown): v is Record<string, unknown> =>
  !!v && typeof v === 'object' && !Array.isArray(v);
function band(v: unknown, index: number): v is EarningsBand {
  if (
    !record(v) ||
    !demandBands[index] ||
    v.low !== demandBands[index][0] ||
    v.high !== demandBands[index][1]
  )
    return false;
  return (
    [
      'hours',
      'days',
      'minutes',
      'paidJobs',
      'blocks',
      'excludedThinHours',
    ].every((k) => finite(v[k]) && Number(v[k]) >= 0) &&
    finite(v.usd) &&
    [
      'usdPerHour',
      'active',
      'queued',
      'warm',
      'load',
      'pressure',
      'requestsPerMinute',
      'tokensPerSecond',
      'busyPercent',
      'asOf',
    ].every((k) => v[k] === null || finite(v[k])) &&
    ['none', 'limited', 'repeated', 'older'].includes(String(v.quality))
  );
}
export function validEarningsOutlook(v: unknown): v is EarningsOutlook {
  return (
    record(v) &&
    ['at', 'from', 'to'].every((k) => finite(v[k])) &&
    Number(v.to) > Number(v.from) &&
    typeof v.scope === 'string' &&
    typeof v.method === 'string' &&
    (v.forecastEvaluation === undefined ||
      validForecastEvaluation(v.forecastEvaluation)) &&
    Array.isArray(v.models) &&
    v.models.length <= 256 &&
    v.models.every(
      (m) =>
        record(m) &&
        typeof m.model === 'string' &&
        typeof m.serving === 'boolean' &&
        typeof m.eligible === 'boolean' &&
        (m.signalAt === null || finite(m.signalAt)) &&
        ['totalWarmHours', 'pairedWarmHours'].every(
          (k) => finite(m[k]) && Number(m[k]) >= 0,
        ) &&
        validConditionalBaseline(m.current) &&
        (m.forecast === undefined || validEarningsForecast(m.forecast)) &&
        Array.isArray(m.bands) &&
        m.bands.length === 7 &&
        m.bands.every(band),
    ) &&
    new Set(v.models.map((m) => m.model)).size === v.models.length
  );
}
export function outlookReading(
  model: EarningsOutlookModel,
  condition: string,
  now: number,
  stale = false,
) {
  if (condition === 'current') {
    const value = model.current;
    const fresh =
      !stale &&
      model.signalAt != null &&
      now - model.signalAt >= 0 &&
      now - model.signalAt < 90 &&
      value.current != null;
    return {
      value,
      rate: fresh ? value.usdPerHour : null,
      label: !fresh
        ? 'Demand unavailable'
        : value.forecastUsable
          ? 'Supported estimate'
          : value.hours
            ? 'Limited observations'
            : 'No matched history',
      supported: fresh && !!value.forecastUsable,
      reason: !fresh
        ? 'Waiting for fresh demand. Historical bands remain available.'
        : value.reason,
    };
  }
  const value = model.bands[Number(condition)];
  return {
    value,
    rate: value?.usdPerHour ?? null,
    label:
      value?.quality === 'repeated'
        ? 'Repeated observations'
        : value?.quality === 'older'
          ? 'Older observations'
          : value?.hours
            ? 'Limited observations'
            : 'No observations',
    supported: value?.quality === 'repeated',
    reason:
      'Observed at this pressure band; active request volume, provider counts and time of day may differ.',
  };
}

const nullableFinite = (v: unknown) => v === null || finite(v);
const strings = (v: unknown) =>
  Array.isArray(v) && v.length <= 32 && v.every((x) => typeof x === 'string');
const nonnegative = (v: unknown) => finite(v) && v >= 0;
export function validEarningsForecast(v: unknown): v is EarningsForecast {
  if (
    !record(v) ||
    !record(v.support) ||
    !record(v.uncertainty) ||
    !record(v.historicalFallback) ||
    !record(v.validation)
  )
    return false;
  const s = v.support,
    u = v.uncertainty,
    h = v.historicalFallback,
    e = v.validation;
  return (
    ['estimate', 'unavailable', 'held', 'expired'].includes(String(v.state)) &&
    v.horizonSeconds === 3600 &&
    v.target ===
      'inference_usd_next_hour_if_already_warm_and_stays_selected_ready' &&
    v.condition ===
      'already_warm_and_same_model_selected_ready_for_entire_hour' &&
    [
      'origin',
      'issuedAt',
      'targetEnd',
      'validUntil',
      'refreshAfter',
      'lookbackSeconds',
    ].every((k) => nonnegative(v[k])) &&
    Number(v.targetEnd) === Number(v.origin) + 3600 &&
    v.validUntil === v.targetEnd &&
    Number(v.refreshAfter) >= Number(v.origin) &&
    Number(v.refreshAfter) < Number(v.targetEnd) &&
    ['usd', 'usdPerHour', 'demandAsOf', 'paidEvidenceThrough'].every((k) =>
      nullableFinite(v[k]),
    ) &&
    v.usd === v.usdPerHour &&
    Number(v.issuedAt) <= Number(v.origin) &&
    Number(v.origin) - Number(v.issuedAt) < 60 &&
    (v.paidEvidenceThrough === null ||
      Number(v.paidEvidenceThrough) <= Number(v.origin) - 120) &&
    (v.demandAsOf === null || Number(v.demandAsOf) <= Number(v.issuedAt)) &&
    [
      'recent_paid_persistence',
      'completed_hour_history',
      'unavailable',
    ].includes(String(v.basis)) &&
    ['reason', 'methodVersion', 'assumption'].every(
      (k) => typeof v[k] === 'string',
    ) &&
    strings(v.reasonCodes) &&
    ['fresh', 'demand_unavailable', 'held'].includes(String(v.inputStatus)) &&
    [
      'completedHours',
      'distinctDates',
      'recentCompleteMinutes',
      'recentRequiredMinutes',
    ].every((k) => nonnegative(s[k]) && Number.isInteger(s[k])) &&
    ['lastOutcomeAt', 'lastPaidMinuteEnd'].every((k) => nullableFinite(s[k])) &&
    typeof s.demandAdjusted === 'boolean' &&
    ['unavailable', 'descriptive_historical_spread'].includes(String(u.kind)) &&
    ['lower', 'upper'].every((k) => nullableFinite(u[k])) &&
    u.nominalCoverage === null &&
    u.empiricalCoverage === null &&
    nonnegative(u.validationWindows) &&
    typeof u.label === 'string' &&
    (u.kind === 'unavailable'
      ? u.lower === null && u.upper === null
      : finite(u.lower) && finite(u.upper) && u.lower <= u.upper) &&
    h.basis === 'completed_warm_hours' &&
    nullableFinite(h.usdPerWarmHour) &&
    nonnegative(h.observedHours) &&
    nullableFinite(h.asOf) &&
    h.isForecast === false &&
    typeof e.status === 'string' &&
    typeof e.prospectiveValidated === 'boolean' &&
    typeof e.retrospectiveEvidence === 'string' &&
    typeof e.creditAvailability === 'string' &&
    (['estimate', 'held'].includes(String(v.state))
      ? finite(v.usdPerHour) && v.basis !== 'unavailable'
      : v.usdPerHour === null)
  );
}

export function forecastReading(
  model: EarningsOutlookModel,
  now: number,
  refreshFailed = false,
) {
  const f = model.forecast;
  const end = f?.targetEnd ?? null;
  const expired = !!f && (now >= f.validUntil || f.state === 'expired');
  const future = !!f && (now < f.issuedAt || f.origin - now >= 60);
  const upcoming = !!f && !future && now < f.origin;
  const saved =
    refreshFailed || f?.state === 'held' || (!!f && now > f.refreshAfter + 30);
  const rate =
    f && !expired && !future && (f.state === 'estimate' || f.state === 'held')
      ? f.usdPerHour
      : null;
  const limited = f?.reasonCodes.some((c) =>
    /insufficient|limited|no_.*history/.test(c),
  );
  const label = !f
    ? 'Forecast not available'
    : expired
      ? 'Forecast window ended'
      : future
        ? 'Forecast time unavailable'
        : rate != null
          ? saved
            ? 'Saved forecast'
            : f.basis === 'recent_paid_persistence'
              ? 'Recent paid pace'
              : 'Historical hourly baseline'
          : limited
            ? 'More local evidence needed'
            : 'Forecast unavailable';
  return {
    rate,
    label,
    upcoming,
    saved: rate != null && saved,
    expired,
    end,
    forecast: f,
    reason:
      f?.reason ||
      'A next-hour forecast needs complete, recent observations for this model.',
  };
}

export function validForecastEvaluation(v: unknown): v is ForecastEvaluation {
  return (
    record(v) &&
    typeof v.status === 'string' &&
    typeof v.methodVersion === 'string' &&
    (v.at === undefined || finite(v.at)) &&
    (v.recordedPackets === undefined || nonnegative(v.recordedPackets)) &&
    ['basis', 'creditOutcomeStatus', 'intervalCalibration'].every(
      (k) => v[k] === undefined || typeof v[k] === 'string',
    ) &&
    Array.isArray(v.models) &&
    v.models.length <= 256 &&
    v.models.every(
      (m) =>
        record(m) &&
        typeof m.model === 'string' &&
        [
          'windows',
          'censoredWindows',
          'unavailableForecasts',
          'pendingWindows',
          'overlappingWindows',
        ].every((k) => nonnegative(m[k]) && Number.isInteger(m[k])) &&
        nullableFinite(m.maeUSDPerHour) &&
        (m.maeUSDPerHour === null || Number(m.maeUSDPerHour) >= 0) &&
        nullableFinite(m.biasUSDPerHour) &&
        (m.windows === 0
          ? m.maeUSDPerHour === null && m.biasUSDPerHour === null
          : finite(m.maeUSDPerHour) && finite(m.biasUSDPerHour)),
    ) &&
    new Set(v.models.map((m) => m.model)).size === v.models.length
  );
}
