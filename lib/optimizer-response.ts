const record = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === 'object' && !Array.isArray(value);
const strings = (value: unknown): value is string[] =>
  Array.isArray(value) && value.every((item) => typeof item === 'string');
const numeric = (value: unknown) =>
  value == null || (typeof value === 'number' && Number.isFinite(value));
const finite = (value: unknown) =>
  typeof value === 'number' && Number.isFinite(value);
const optionalString = (value: unknown) =>
  value == null || typeof value === 'string';

export type DemandObservation = {
  hours: number;
  days: number;
  minutes: number;
  asOf: number | null;
  usd: number;
  paidJobs: number;
  active: number | null;
  queued: number | null;
  warm: number | null;
  load: number | null;
  pressure: number | null;
  usdPerHour: number | null;
  requestsPerMinute: number | null;
  tokensPerSecond: number | null;
  busyPercent: number | null;
};
export type ConditionalBaseline = DemandObservation & {
  scope: string;
  usable: boolean;
  weight: number;
  reason: string;
  blocks: number;
  coverage: number;
  overlapHours: number;
  totalHours: number;
  lower: number | null;
  upper: number | null;
  current: {
    pressure: number;
    active: number;
    warm: number;
    load: number;
  } | null;
  forecastUsable?: boolean;
  matchingCoverage?: number;
  otherDemandHours?: number | null;
  otherContextHours?: number | null;
  unknownDemandHours?: number;
};
export type BaselineReport = {
  at: number;
  from: number;
  to: number;
  model: string | null;
  models: { id: string; hours: number; overlapHours: number }[];
  summary: DemandObservation;
  baseline: ConditionalBaseline;
  periods: (DemandObservation & { at: number; end: number })[];
  bands: (DemandObservation & { low: number; high: number | null })[];
  periodSeconds: number;
  scope: string;
  method: string;
};
function observation(v: unknown): boolean {
  return (
    record(v) &&
    ['hours', 'days', 'minutes', 'paidJobs'].every(
      (k) => finite(v[k]) && Number(v[k]) >= 0,
    ) &&
    finite(v.usd) &&
    (v.usdPerHour === null || finite(v.usdPerHour)) &&
    [
      'asOf',
      'active',
      'queued',
      'warm',
      'load',
      'pressure',
      'requestsPerMinute',
      'tokensPerSecond',
      'busyPercent',
    ].every((k) => v[k] === null || (finite(v[k]) && Number(v[k]) >= 0))
  );
}
export function validConditionalBaseline(v: unknown): v is ConditionalBaseline {
  return (
    record(v) &&
    observation(v) &&
    typeof v.usable === 'boolean' &&
    typeof v.scope === 'string' &&
    typeof v.reason === 'string' &&
    ['weight', 'blocks', 'coverage', 'overlapHours', 'totalHours'].every(
      (k) => finite(v[k]) && Number(v[k]) >= 0,
    ) &&
    ['lower', 'upper'].every((k) => v[k] === null || finite(v[k])) &&
    (v.forecastUsable === undefined || typeof v.forecastUsable === 'boolean') &&
    [
      'matchingCoverage',
      'otherDemandHours',
      'otherContextHours',
      'unknownDemandHours',
    ].every((k) => v[k] == null || (finite(v[k]) && Number(v[k]) >= 0)) &&
    (v.current === null ||
      (record(v.current) &&
        ['pressure', 'active', 'warm', 'load'].every((k) =>
          finite((v.current as Record<string, unknown>)[k]),
        )))
  );
}
export function validBaselineReport(v: unknown): v is BaselineReport {
  return (
    record(v) &&
    ['at', 'from', 'to', 'periodSeconds'].every((k) => finite(v[k])) &&
    Number(v.periodSeconds) > 0 &&
    (v.model === null || typeof v.model === 'string') &&
    typeof v.scope === 'string' &&
    typeof v.method === 'string' &&
    observation(v.summary) &&
    validConditionalBaseline(v.baseline) &&
    Array.isArray(v.models) &&
    v.models.every(
      (r) =>
        record(r) &&
        typeof r.id === 'string' &&
        finite(r.hours) &&
        finite(r.overlapHours),
    ) &&
    Array.isArray(v.periods) &&
    v.periods.length <= 602 &&
    v.periods.every(
      (r) => record(r) && finite(r.at) && finite(r.end) && observation(r),
    ) &&
    Array.isArray(v.bands) &&
    v.bands.length <= 10 &&
    v.bands.every(
      (r) =>
        record(r) &&
        finite(r.low) &&
        (r.high === null || finite(r.high)) &&
        observation(r),
    )
  );
}

function evidence(value: unknown): boolean {
  if (!record(value)) return false;
  if (
    value.timeSlots != null &&
    (!Array.isArray(value.timeSlots) ||
      value.timeSlots.some(
        (slot) =>
          !record(slot) ||
          typeof slot.weekend !== 'boolean' ||
          !numeric(slot.hour) ||
          !numeric(slot.hours) ||
          !numeric(slot.usdPerHour),
      ))
  )
    return false;
  if (
    value.perModel != null &&
    (!record(value.perModel) ||
      Object.values(value.perModel).some(
        (model) =>
          !record(model) || !numeric(model.usd) || !numeric(model.jobs),
      ))
  )
    return false;
  return [
    'hours',
    'usd',
    'jobs',
    'usdPerHour',
    'jobsPerHour',
    'days',
    'switchMinutes',
    'bothWarmPercent',
  ].every((key) => numeric(value[key]));
}

function demandEstimate(value: unknown): boolean {
  return (
    value == null ||
    (record(value) &&
      typeof value.scope === 'string' &&
      (value.forecastUsable == null ||
        typeof value.forecastUsable === 'boolean') &&
      ['rate', 'lower', 'upper', 'hours', 'days'].every(
        (k) => typeof value[k] === 'number' && Number.isFinite(value[k]),
      ))
  );
}
function trialClock(value: unknown): boolean {
  if (value == null) return true;
  if (!record(value)) return false;
  if (!Object.keys(value).length) return true; // Legacy trials have no clock evidence.
  return (
    finite(value.start) &&
    ['end', 'seconds', 'usd', 'usdPerHour'].every((k) => numeric(value[k])) &&
    finite(value.coveragePercent) &&
    Number(value.coveragePercent) >= 0 &&
    Number(value.coveragePercent) <= 100 &&
    typeof value.qualified === 'boolean' &&
    (!value.qualified ||
      (finite(value.end) &&
        Number(value.end) > Number(value.start) &&
        finite(value.seconds) &&
        Number(value.seconds) > 0 &&
        finite(value.usd) &&
        finite(value.usdPerHour)))
  );
}
function trialCompetitive(value: unknown): boolean {
  if (value == null) return true;
  if (
    !record(value) ||
    !['win', 'loss', 'uncertain'].includes(String(value.outcome)) ||
    value.rateBasis !== 'settled_inference_per_elapsed_selection_hour' ||
    !optionalString(value.revision) ||
    !record(value.comparison)
  )
    return false;
  const c = value.comparison;
  if (
    Object.keys(c).length &&
    (typeof c.qualified !== 'boolean' ||
      ![
        'lower',
        'upper',
        'observedAt',
        'capturedAt',
        'warmMinutes',
        'liveRate',
      ].every((k) => numeric(c[k])) ||
      (c.qualified &&
        (!finite(c.lower) ||
          !finite(c.upper) ||
          Number(c.lower) > Number(c.upper))))
  )
    return false;
  if (value.outcome !== 'uncertain' && c.qualified !== true) return false;
  return (
    value.regime == null ||
    (record(value.regime) &&
      typeof value.regime.providerVersion === 'string' &&
      typeof value.regime.model === 'string')
  );
}
function trialOccupancy(value: unknown): boolean {
  return (
    value == null ||
    (record(value) &&
      finite(value.start) &&
      finite(value.end) &&
      Number(value.end) >= Number(value.start) &&
      typeof value.ended === 'boolean')
  );
}
function samplingBudget(value: unknown): boolean {
  // A limit of 0 means learning time is off.
  return (
    value == null ||
    (record(value) &&
      [
        'minutesUsed',
        'minutesLimit',
        'reservationMinutes',
        'unknownLifecycles',
      ].every((k) => finite(value[k]) && Number(value[k]) >= 0) &&
      Number.isInteger(value.unknownLifecycles) &&
      typeof value.qualified === 'boolean' &&
      typeof value.cohort === 'string' &&
      (value.availableAt == null || finite(value.availableAt)))
  );
}
function trialOutcome(value: unknown): boolean {
  return (
    value == null ||
    (record(value) &&
      typeof value.model === 'string' &&
      typeof value.status === 'string' &&
      (value.paymentSeen == null || typeof value.paymentSeen === 'boolean') &&
      trialClock(value.clock) &&
      trialCompetitive(value.competitive) &&
      trialOccupancy(value.occupancy) &&
      (!record(value.competitive) ||
        value.competitive.outcome === 'uncertain' ||
        (record(value.clock) &&
          value.clock.qualified === true &&
          value.settled === true)) &&
      (value.learning == null ||
        (record(value.learning) &&
          [
            'completeTokenSamples',
            'jobMinutes',
            'sampleGoal',
            'jobMinutesGoal',
          ].every((k) =>
            finite(
              value.learning && (value.learning as Record<string, unknown>)[k],
            ),
          ) &&
          typeof value.learning.targetReached === 'boolean' &&
          numeric(value.learning.usdPerRequest) &&
          numeric(value.learning.usdPerMillionTokens))) &&
      typeof value.current === 'boolean' &&
      [
        'runId',
        'warmSeconds',
        'trialMinutes',
        'requests',
        'tokens',
        'paidWarmSeconds',
      ].every((k) => finite(value[k])) &&
      [
        'firstTrafficSeconds',
        'idlePercent',
        'usd',
        'usdPerHour',
        'cooldownUntil',
        'coveragePercent',
        'settlementDeadline',
      ].every((k) => numeric(value[k])) &&
      (value.partial == null || typeof value.partial === 'boolean') &&
      (value.settled == null || typeof value.settled === 'boolean'))
  );
}
export type DecisionExecution = {
  at: number;
  fresh: boolean;
  interruptBusy: boolean;
  windowHours: number;
  current: DecisionEntry | null;
  history: DecisionEntry[];
  totals: { code: string; seconds: number }[];
};
export type DecisionEntry = {
  id: number;
  at: number;
  updated: number;
  observedSeconds: number;
  mode: string;
  model: string | null;
  target: string | null;
  phase: string;
  code: string;
  reason: string;
  sourceAt: number | null;
  confirmationSeconds: number;
  requiredSeconds: number;
};
function execution(v: unknown): boolean {
  const entry = (x: unknown) => objectEntry(x);
  function objectEntry(x: unknown): boolean {
    return (
      record(x) &&
      [
        'id',
        'at',
        'updated',
        'observedSeconds',
        'confirmationSeconds',
        'requiredSeconds',
      ].every((k) => finite(x[k])) &&
      ['mode', 'phase', 'code', 'reason'].every(
        (k) => typeof x[k] === 'string',
      ) &&
      optionalString(x.model) &&
      optionalString(x.target) &&
      numeric(x.sourceAt)
    );
  }
  return (
    v == null ||
    (record(v) &&
      finite(v.at) &&
      finite(v.windowHours) &&
      typeof v.fresh === 'boolean' &&
      typeof v.interruptBusy === 'boolean' &&
      (v.current === null || entry(v.current)) &&
      Array.isArray(v.history) &&
      v.history.length <= 30 &&
      v.history.every(entry) &&
      Array.isArray(v.totals) &&
      v.totals.every(
        (t) =>
          record(t) &&
          typeof t.code === 'string' &&
          finite(t.seconds) &&
          Number(t.seconds) >= 0,
      ))
  );
}
function spikeReview(value: unknown): boolean {
  return (
    value == null ||
    (record(value) &&
      typeof value.incumbent === 'string' &&
      typeof value.reason === 'string' &&
      typeof value.status === 'string' &&
      ['runId', 'at', 'deadline'].every((k) => finite(value[k])) &&
      (value.ordinary === true
        ? numeric(value.referenceRate)
        : finite(value.referenceRate)) &&
      ['trialRate', 'liveRate'].every((k) => numeric(value[k])) &&
      (value.rateBasis == null ||
        ['warm_hour', 'settled_inference_per_elapsed_selection_hour'].includes(
          String(value.rateBasis),
        )) &&
      (value.ordinary == null || typeof value.ordinary === 'boolean') &&
      (value.ordinary !== true ||
        value.rateBasis === 'settled_inference_per_elapsed_selection_hour') &&
      (value.learning == null || typeof value.learning === 'boolean') &&
      (value.demand == null ||
        (record(value.demand) &&
          typeof value.demand.status === 'string' &&
          typeof value.demand.reason === 'string' &&
          [
            'load',
            'pressure',
            'referenceLoad',
            'referencePressure',
            'sourceAt',
          ].every((k) =>
            numeric((value.demand as Record<string, unknown>)[k]),
          ))))
  );
}
function spikeOpportunity(value: unknown): boolean {
  return (
    value == null ||
    (record(value) &&
      typeof value.qualified === 'boolean' &&
      typeof value.reason === 'string' &&
      ['trialsUsed', 'trialLimit', 'maxClockMinutes'].every((k) =>
        finite(value[k]),
      ) &&
      ['loadRatio', 'pressureRatio'].every((k) => numeric(value[k])))
  );
}
function opportunity(value: unknown): boolean {
  if (!record(value) || !record(value.signal)) return false;
  const signal = value.signal;
  return (
    record(value) &&
    typeof value.model === 'string' &&
    record(value.signal) &&
    demandEstimate(value.estimate) &&
    spikeOpportunity(value.spike) &&
    (value.learning == null ||
      (record(value.learning) &&
        typeof value.learning.qualified === 'boolean' &&
        typeof value.learning.reason === 'string' &&
        (value.learning.evidence == null ||
          (record(value.learning.evidence) &&
            finite(value.learning.evidence.completeTokenSamples) &&
            finite(value.learning.evidence.jobMinutes) &&
            typeof value.learning.evidence.fresh === 'boolean' &&
            typeof value.learning.evidence.needsSamples === 'boolean')))) &&
    (value.conditional == null ||
      validConditionalBaseline(value.conditional)) &&
    numeric(value.historyWeight) &&
    (value.fallbackPreferred == null ||
      typeof value.fallbackPreferred === 'boolean') &&
    optionalString(value.selectionReason) &&
    optionalString(value.kind) &&
    ['load', 'pressure', 'loadRatio', 'pressureRatio'].every((k) =>
      numeric(signal[k]),
    ) &&
    (value.sustained == null ||
      (record(value.sustained) &&
        (value.sustained.qualified == null ||
          typeof value.sustained.qualified === 'boolean'))) &&
    typeof value.selected === 'boolean' &&
    typeof value.current === 'boolean' &&
    typeof value.eligible === 'boolean' &&
    optionalString(value.reason) &&
    [
      'netGainUsd',
      'paybackMinutes',
      'switchCostUsd',
      'outboundSeconds',
      'returnSeconds',
      'extraMemoryGB',
      'timingSamples',
      'failureCooldownUntil',
      'trialCooldownUntil',
    ].every((k) => numeric(value[k])) &&
    (value.loadBudget == null ||
      (record(value.loadBudget) &&
        numeric(value.loadBudget.afterUnloadGB) &&
        numeric(value.loadBudget.requiredGB) &&
        numeric(value.loadBudget.reclaimableGB)))
  );
}
function fallback(value: unknown): boolean {
  return (
    value == null ||
    (record(value) &&
      typeof value.model === 'string' &&
      typeof value.status === 'string' &&
      typeof value.reason === 'string' &&
      ['enabled', 'qualified', 'current', 'eligible'].every(
        (k) => typeof value[k] === 'boolean',
      ) &&
      ['warmMinutes', 'windows', 'trafficWindows', 'paidJobs'].every(
        (k) => finite(value[k]) && Number(value[k]) >= 0,
      ) &&
      ['trafficPercent', 'usdPerHour', 'asOf', 'expiresAt'].every((k) =>
        numeric(value[k]),
      ) &&
      optionalString(value.holdReason) &&
      optionalString(value.selection))
  );
}
function highEarnings(value: unknown): boolean {
  return (
    value == null ||
    (record(value) &&
      typeof value.active === 'boolean' &&
      typeof value.reason === 'string' &&
      ['threshold', 'windowMinutes', 'coveredMinutes'].every((k) =>
        finite(value[k]),
      ) &&
      Array.isArray(value.rates) &&
      value.rates.length <= 3 &&
      value.rates.every(numeric))
  );
}
function earningsTarget(value: unknown): boolean {
  return (
    value == null ||
    (record(value) &&
      ['usdPerHour', 'dailyUsd', 'warmMinutes'].every(
        (k) => finite(value[k]) && Number(value[k]) >= 0,
      ) &&
      [
        'rate',
        'fastRate',
        'asOf',
        'nextCheckAt',
        'coveragePercent',
        'productiveFloor',
      ].every((k) => numeric(value[k])) &&
      ['ready', 'belowTarget'].every((k) => typeof value[k] === 'boolean') &&
      (value.livePaid == null ||
        (record(value.livePaid) &&
          typeof value.livePaid.fresh === 'boolean' &&
          ['rate', 'asOf', 'seconds'].every((k) =>
            numeric(
              value.livePaid && (value.livePaid as Record<string, unknown>)[k],
            ),
          ))) &&
      typeof value.status === 'string' &&
      typeof value.reason === 'string' &&
      highEarnings(value.highEarnings))
  );
}
function preferredReturn(value: unknown): boolean {
  return (
    value == null ||
    (record(value) &&
      ['model', 'scope', 'reason'].every((k) => typeof value[k] === 'string') &&
      ['qualified', 'eligible'].every((k) => typeof value[k] === 'boolean') &&
      finite(value.hours) &&
      numeric(value.rate) &&
      numeric(value.asOf) &&
      optionalString(value.selection))
  );
}
function demandAuto(value: unknown): boolean {
  if (
    !record(value) ||
    !record(value.policy) ||
    !record(value.limits) ||
    !Array.isArray(value.opportunities) ||
    !Array.isArray(value.runs) ||
    !demandEstimate(value.baseline) ||
    typeof value.reason !== 'string' ||
    typeof value.enabled !== 'boolean' ||
    typeof value.scanStatus !== 'string' ||
    !finite(value.at) ||
    !finite(value.scanAt) ||
    !finite(value.planningMinutes) ||
    !optionalString(value.controlError) ||
    !optionalString(value.target) ||
    !optionalString(value.currentModel)
  )
    return false;
  const limits = value.limits;
  const alternative = value.paidAlternative;
  if (
    alternative != null &&
    (!record(alternative) ||
      typeof alternative.model !== 'string' ||
      !alternative.model ||
      typeof alternative.eligible !== 'boolean' ||
      typeof alternative.reason !== 'string' ||
      !alternative.reason ||
      !finite(alternative.at))
  )
    return false;
  if (
    !optionalString(value.economicRevision) ||
    !samplingBudget(limits.sampling) ||
    !optionalString(value.kind) ||
    !trialOutcome(value.trial) ||
    !fallback(value.fallback) ||
    !spikeReview(value.spikeReview) ||
    !execution(value.execution) ||
    !earningsTarget(value.earningsTarget) ||
    !preferredReturn(value.preferredReturn) ||
    !optionalString(value.explorationTrigger) ||
    (value.activity != null &&
      (!record(value.activity) ||
        typeof value.activity.fresh !== 'boolean' ||
        !finite(value.activity.idleSeconds)))
  )
    return false;
  if (
    ![
      'switchesUsed',
      'switchLimit',
      'downtimeMinutesUsed',
      'downtimeMinutesLimit',
      'nextRunAt',
    ].every((k) => finite(limits[k]))
  )
    return false;
  const rules = value.policy;
  // The saved plan, when present, has the same shape as the effective policy.
  const saved = value.savedPolicy;
  if (
    saved != null &&
    (!record(saved) ||
      !Object.values(saved).every(
        (v) => typeof v === 'number' && Number.isFinite(v) && v >= 0,
      ))
  )
    return false;
  if (
    value.baselineLearning != null &&
    (!record(value.baselineLearning) ||
      typeof value.baselineLearning.enabled !== 'boolean' ||
      !finite(value.baselineLearning.trialsUsed))
  )
    return false;
  if (
    rules.baselineLearningEnabled != null &&
    rules.baselineLearningEnabled !== 0 &&
    rules.baselineLearningEnabled !== 1
  )
    return false;
  const gathering = value.dataGathering;
  if (
    gathering != null &&
    (!record(gathering) ||
      typeof gathering.active !== 'boolean' ||
      (gathering.active &&
        !['startedAt', 'endsAt', 'remainingSeconds'].every((k) =>
          finite(gathering[k]),
        )))
  )
    return false;
  if (
    rules.fallbackEnabled != null &&
    rules.fallbackEnabled !== 0 &&
    rules.fallbackEnabled !== 1
  )
    return false;
  if (
    rules.targetUsdPerHour != null &&
    ![0.08, 0.1, 0.12, 0.15, 0.2, 0.25].includes(Number(rules.targetUsdPerHour))
  )
    return false;
  if (
    rules.targetUsdPerHour != null &&
    typeof rules.targetUsdPerHour !== 'number'
  )
    return false;
  if (
    ![
      'minRunMinutes',
      'confirmationMinutes',
      'improvementPercent',
      'planningMinutes',
      'minimumNetUsd',
      'maxSwitchesPerDay',
      'maxDowntimeMinutes',
      'memoryHeadroomGB',
    ].every(
      (k) =>
        typeof rules[k] === 'number' &&
        Number.isFinite(rules[k]) &&
        rules[k] > 0,
    )
  )
    return false;
  if (
    !['idleEscapeMinutes', 'trialMinutes', 'trialCooldownMinutes'].every(
      (k) => rules[k] == null || (finite(rules[k]) && Number(rules[k]) > 0),
    )
  )
    return false;
  if (
    value.confirmation != null &&
    (!record(value.confirmation) ||
      typeof value.confirmation.model !== 'string' ||
      !finite(value.confirmation.seconds) ||
      Number(value.confirmation.seconds) < 0 ||
      !finite(value.confirmation.samples) ||
      !numeric(value.confirmation.requiredSeconds) ||
      (value.confirmation.status != null &&
        !['confirming', 'paused', 'ready'].includes(
          String(value.confirmation.status),
        )) ||
      !optionalString(value.confirmation.reason) ||
      !numeric(value.confirmation.expiresAt) ||
      (value.confirmation.expiresAt != null &&
        Number(value.confirmation.expiresAt) <= 0) ||
      (value.confirmation.status === 'paused' &&
        !(
          typeof value.confirmation.reason === 'string' &&
          value.confirmation.reason.trim()
        )) ||
      (value.confirmation.status === 'ready' &&
        (!finite(value.confirmation.requiredSeconds) ||
          Number(value.confirmation.requiredSeconds) <= 0 ||
          Number(value.confirmation.seconds) <
            Number(value.confirmation.requiredSeconds))))
  )
    return false;
  return value.opportunities.every(opportunity) && value.runs.every(run);
}

function run(r: unknown): boolean {
  return (
    record(r) &&
    typeof r.model === 'string' &&
    typeof r.result === 'string' &&
    finite(r.id) &&
    finite(r.at) &&
    numeric(r.downtime) &&
    record(r.decision) &&
    optionalString(r.decision.kind) &&
    optionalString(r.decision.reason) &&
    trialOutcome(r.decision.outcome) &&
    // Stall-recovery restarts record no switching estimate.
    (r.decision.candidate == null
      ? r.decision.planningMinutes == null || finite(r.decision.planningMinutes)
      : opportunity(r.decision.candidate) && finite(r.decision.planningMinutes))
  );
}

function event(e: unknown): boolean {
  return record(e) && typeof e.detail === 'string' && numeric(e.at);
}

/**
 * Drop malformed history rows (switch runs, activity events) before
 * validating: one odd record from an older version or a new kind of run must
 * not blank the whole optimizer page, as run 131 did on Sep 26.
 */
export function withoutInvalidHistory<T>(value: T): T {
  if (!record(value)) return value;
  const out: Record<string, unknown> = { ...value };
  if (Array.isArray(value.events)) out.events = value.events.filter(event);
  const auto = value.demandAuto;
  if (record(auto) && Array.isArray(auto.runs))
    out.demandAuto = { ...auto, runs: auto.runs.filter(run) };
  return out as T;
}

/** Validate the containers used during render before publishing an API result. */
export function validOptimizerResponse(value: unknown): boolean {
  if (
    !record(value) ||
    typeof value.at !== 'number' ||
    !Number.isFinite(value.at) ||
    !['observe', 'week', 'optimize', 'combo', 'demand'].includes(
      String(value.mode),
    ) ||
    typeof value.detail !== 'string' ||
    typeof value.status !== 'string' ||
    !strings(value.selected) ||
    !Array.isArray(value.models) ||
    !Array.isArray(value.events) ||
    typeof value.controlVersion !== 'string' ||
    typeof value.canManage !== 'boolean' ||
    typeof value.busy !== 'boolean' ||
    typeof value.blockHours !== 'number' ||
    !Number.isFinite(value.blockHours) ||
    value.blockHours <= 0
  )
    return false;
  if (
    value.models.some(
      (model) =>
        !record(model) ||
        typeof model.id !== 'string' ||
        typeof model.name !== 'string' ||
        typeof model.available !== 'boolean' ||
        !optionalString(model.reason) ||
        !evidence(model.evidence),
    )
  )
    return false;
  if (
    ![
      'currentModel',
      'originalModel',
      'requestedModel',
      'requestedKind',
      'controlError',
      'discoveryError',
    ].every((key) => optionalString(value[key]))
  )
    return false;
  if (!value.events.every(event)) return false;
  if (
    value.warmup != null &&
    (!record(value.warmup) ||
      typeof value.warmup.status !== 'string' ||
      typeof value.warmup.detail !== 'string')
  )
    return false;
  if (value.resumeDemand != null) {
    const resume = value.resumeDemand;
    if (
      !record(resume) ||
      typeof resume.hasSavedPlan !== 'boolean' ||
      typeof resume.available !== 'boolean' ||
      !optionalString(resume.currentModel) ||
      !optionalString(resume.reason) ||
      !Number.isInteger(resume.selectedCount) ||
      (resume.selectedCount as number) < 0 ||
      (resume.available &&
        (!resume.hasSavedPlan ||
          typeof resume.currentModel !== 'string' ||
          !resume.currentModel ||
          !Number.isInteger(resume.availableCount) ||
          (resume.availableCount as number) < 2))
    )
      return false;
  }
  if (value.reporting != null) {
    const r = value.reporting;
    if (
      !record(r) ||
      !finite(r.at) ||
      !numeric(r.sessionId) ||
      !strings(r.models) ||
      r.models.length < 3 ||
      r.models.length > 64 ||
      new Set(r.models).size !== r.models.length ||
      r.models.some((m) => !m || m !== m.trim()) ||
      r.managedBy !== 'darkbloom' ||
      r.automationSupported !== false ||
      typeof r.counting !== 'boolean' ||
      typeof r.detail !== 'string'
    )
      return false;
  }
  if (value.combinations != null) {
    const combo = value.combinations;
    if (
      !record(combo) ||
      !Array.isArray(combo.candidates) ||
      !Array.isArray(combo.results)
    )
      return false;
    if (
      combo.candidates.some(
        (pair) =>
          !record(pair) ||
          typeof pair.id !== 'string' ||
          !strings(pair.models) ||
          !optionalString(pair.reason),
      )
    )
      return false;
    if (
      combo.results.some(
        (pair) =>
          !record(pair) ||
          typeof pair.id !== 'string' ||
          typeof pair.name !== 'string' ||
          !evidence(pair.evidence),
      )
    )
      return false;
    if (
      combo.plan != null &&
      (!record(combo.plan) ||
        !Array.isArray(combo.plan.pairs) ||
        !combo.plan.pairs.every(strings) ||
        !optionalString(combo.plan.detail))
    )
      return false;
  }
  if (value.memory != null) {
    if (!record(value.memory)) return false;
    const recovery = value.memory.cacheRecovery;
    if (
      recovery != null &&
      (!record(recovery) ||
        !['status', 'detail', 'model'].every((key) =>
          optionalString(recovery[key]),
        ))
    )
      return false;
  }
  if (value.demandAuto != null && !demandAuto(value.demandAuto)) return false;
  return true;
}
