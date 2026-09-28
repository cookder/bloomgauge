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
// An ordinary review's rate: settled inference per warm hour after the first paid work
// when the trial has a steady rate, else per elapsed selection hour
// (native/trial_economics.py ordinary_review).
const settledBases = [
  'settled_inference_per_elapsed_selection_hour',
  'settled_inference_per_warm_hour_after_first_work',
];
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
        ['warm_hour', ...settledBases].includes(String(value.rateBasis))) &&
      (value.ordinary == null || typeof value.ordinary === 'boolean') &&
      (value.ordinary !== true ||
        settledBases.includes(String(value.rateBasis))) &&
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
function paidAlternative(value: unknown): boolean {
  return (
    value == null ||
    (record(value) &&
      typeof value.model === 'string' &&
      !!value.model &&
      typeof value.eligible === 'boolean' &&
      typeof value.reason === 'string' &&
      !!value.reason &&
      finite(value.at))
  );
}
function activity(value: unknown): boolean {
  return (
    value == null ||
    (record(value) &&
      typeof value.fresh === 'boolean' &&
      finite(value.idleSeconds))
  );
}
// Manager strategy switches (native/demand_optimizer.py CHOICES): absent on older
// backends, 0 or 1 otherwise. The manager view itself is read defensively at
// render (lib/optimizer-manager.ts), so a new or odd field never blanks the page.
const managerSwitches = (policy: Record<string, unknown>) =>
  !['managerStrategy', 'managerExcursions'].some(
    (k) => policy[k] != null && policy[k] !== 0 && policy[k] !== 1,
  );
// The saved plan, when present, has the same shape as the effective policy.
function savedPolicy(value: unknown): boolean {
  return (
    value == null ||
    (record(value) &&
      Object.values(value).every(
        (v) => typeof v === 'number' && Number.isFinite(v) && v >= 0,
      ) &&
      managerSwitches(value))
  );
}
function baselineLearning(value: unknown): boolean {
  return (
    value == null ||
    (record(value) &&
      typeof value.enabled === 'boolean' &&
      finite(value.trialsUsed))
  );
}
function dataGathering(value: unknown): boolean {
  return (
    value == null ||
    (record(value) &&
      typeof value.active === 'boolean' &&
      (!value.active ||
        ['startedAt', 'endsAt', 'remainingSeconds'].every((k) =>
          finite(value[k]),
        )))
  );
}
function confirmation(value: unknown): boolean {
  if (value == null) return true;
  return !(
    !record(value) ||
    typeof value.model !== 'string' ||
    !finite(value.seconds) ||
    Number(value.seconds) < 0 ||
    !finite(value.samples) ||
    !numeric(value.requiredSeconds) ||
    (value.status != null &&
      !['confirming', 'paused', 'ready'].includes(String(value.status))) ||
    !optionalString(value.reason) ||
    !numeric(value.expiresAt) ||
    (value.expiresAt != null && Number(value.expiresAt) <= 0) ||
    (value.status === 'paused' &&
      !(typeof value.reason === 'string' && value.reason.trim())) ||
    (value.status === 'ready' &&
      (!finite(value.requiredSeconds) ||
        Number(value.requiredSeconds) <= 0 ||
        Number(value.seconds) < Number(value.requiredSeconds)))
  );
}
function policyRules(rules: Record<string, unknown>): boolean {
  return (
    [rules.baselineLearningEnabled, rules.fallbackEnabled].every(
      (v) => v == null || v === 0 || v === 1,
    ) &&
    managerSwitches(rules) &&
    (rules.targetUsdPerHour == null ||
      (typeof rules.targetUsdPerHour === 'number' &&
        [0.08, 0.1, 0.12, 0.15, 0.2, 0.25].includes(rules.targetUsdPerHour))) &&
    [
      'minRunMinutes',
      'confirmationMinutes',
      'improvementPercent',
      'planningMinutes',
      'minimumNetUsd',
      'maxSwitchesPerDay',
      'maxDowntimeMinutes',
      'memoryHeadroomGB',
    ].every((k) => finite(rules[k]) && Number(rules[k]) > 0) &&
    ['idleEscapeMinutes', 'trialMinutes', 'trialCooldownMinutes'].every(
      (k) => rules[k] == null || (finite(rules[k]) && Number(rules[k]) > 0),
    )
  );
}
/** What the plan cannot render without: its policy, limits and scan status. */
function demandAutoCore(value: unknown): boolean {
  return (
    record(value) &&
    record(value.policy) &&
    record(value.limits) &&
    Array.isArray(value.opportunities) &&
    Array.isArray(value.runs) &&
    demandEstimate(value.baseline) &&
    typeof value.reason === 'string' &&
    typeof value.enabled === 'boolean' &&
    typeof value.scanStatus === 'string' &&
    finite(value.at) &&
    finite(value.scanAt) &&
    finite(value.planningMinutes) &&
    optionalString(value.controlError) &&
    optionalString(value.target) &&
    optionalString(value.currentModel) &&
    [
      'switchesUsed',
      'switchLimit',
      'downtimeMinutesUsed',
      'downtimeMinutesLimit',
      'nextRunAt',
    ].every((k) => finite((value.limits as Record<string, unknown>)[k])) &&
    policyRules(value.policy)
  );
}
// Optional parts of demandAuto: one that fails is dropped and named ("Couldn't read …").
const demandSections: [string, (value: unknown) => boolean, string][] = [
  ['paidAlternative', paidAlternative, 'the paid alternative'],
  ['trial', trialOutcome, 'the trial'],
  ['fallback', fallback, 'the fallback model'],
  ['spikeReview', spikeReview, 'the trial review'],
  ['execution', execution, 'recent decisions'],
  ['earningsTarget', earningsTarget, 'the earnings target'],
  ['preferredReturn', preferredReturn, 'the preferred return'],
  ['activity', activity, 'the activity reading'],
  ['savedPolicy', savedPolicy, 'the saved plan'],
  ['baselineLearning', baselineLearning, 'learning status'],
  ['dataGathering', dataGathering, 'the learning boost'],
  ['confirmation', confirmation, 'the switch check'],
  ['economicRevision', optionalString, 'plan details'],
  ['kind', optionalString, 'plan details'],
  ['explorationTrigger', optionalString, 'plan details'],
];
function demandAuto(value: unknown): boolean {
  return (
    demandAutoCore(value) &&
    record(value) &&
    demandSections.every(([key, check]) => check(value[key])) &&
    samplingBudget((value.limits as Record<string, unknown>).sampling) &&
    (value.opportunities as unknown[]).every(opportunity) &&
    (value.runs as unknown[]).every(run)
  );
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

/**
 * The Overview run chip's reader for `/api/optimizer?view=run` (native/collector.py
 * `run_view`). That view sends only the live fields and a slice of demandAuto
 * (enabled, trial, paidAlternative, spikeReview, the current trial's run), so the
 * full-page validator can never accept it. pulseRunStatus reads every field
 * defensively; only what it cannot work without is required here.
 */
export function readRunView(value: unknown): Record<string, unknown> | null {
  return record(value) && finite(value.at) && typeof value.mode === 'string'
    ? value
    : null;
}

/** The response's own fields: without these nothing on the Optimizer page renders. */
function coreResponse(value: unknown): value is Record<string, unknown> & {
  models: unknown[];
  events: unknown[];
} {
  return (
    record(value) &&
    finite(value.at) &&
    ['observe', 'week', 'optimize', 'combo', 'demand'].includes(
      String(value.mode),
    ) &&
    typeof value.detail === 'string' &&
    typeof value.status === 'string' &&
    strings(value.selected) &&
    Array.isArray(value.models) &&
    Array.isArray(value.events) &&
    typeof value.controlVersion === 'string' &&
    typeof value.canManage === 'boolean' &&
    typeof value.busy === 'boolean' &&
    finite(value.blockHours) &&
    Number(value.blockHours) > 0 &&
    [
      'currentModel',
      'originalModel',
      'requestedModel',
      'requestedKind',
      'controlError',
      'discoveryError',
    ].every((key) => optionalString(value[key]))
  );
}
function modelRow(model: unknown): boolean {
  return (
    record(model) &&
    typeof model.id === 'string' &&
    typeof model.name === 'string' &&
    typeof model.available === 'boolean' &&
    optionalString(model.reason) &&
    evidence(model.evidence)
  );
}
function warmup(value: unknown): boolean {
  return (
    value == null ||
    (record(value) &&
      typeof value.status === 'string' &&
      typeof value.detail === 'string')
  );
}
function resumeDemand(resume: unknown): boolean {
  return (
    resume == null ||
    (record(resume) &&
      typeof resume.hasSavedPlan === 'boolean' &&
      typeof resume.available === 'boolean' &&
      optionalString(resume.currentModel) &&
      optionalString(resume.reason) &&
      Number.isInteger(resume.selectedCount) &&
      Number(resume.selectedCount) >= 0 &&
      // A paused manager can hold the serving model alone, so one available model
      // is enough (native/optimizer.py demand_resume_context).
      (!resume.available ||
        (resume.hasSavedPlan &&
          typeof resume.currentModel === 'string' &&
          !!resume.currentModel &&
          Number.isInteger(resume.availableCount) &&
          Number(resume.availableCount) >= 1)))
  );
}
function reporting(r: unknown): boolean {
  return (
    r == null ||
    (record(r) &&
      finite(r.at) &&
      numeric(r.sessionId) &&
      strings(r.models) &&
      r.models.length >= 3 &&
      r.models.length <= 64 &&
      new Set(r.models).size === r.models.length &&
      !r.models.some((m) => !m || m !== m.trim()) &&
      r.managedBy === 'darkbloom' &&
      r.automationSupported === false &&
      typeof r.counting === 'boolean' &&
      typeof r.detail === 'string')
  );
}
function combinations(combo: unknown): boolean {
  return (
    combo == null ||
    (record(combo) &&
      Array.isArray(combo.candidates) &&
      Array.isArray(combo.results) &&
      combo.candidates.every(
        (pair) =>
          record(pair) &&
          typeof pair.id === 'string' &&
          strings(pair.models) &&
          optionalString(pair.reason),
      ) &&
      combo.results.every(
        (pair) =>
          record(pair) &&
          typeof pair.id === 'string' &&
          typeof pair.name === 'string' &&
          evidence(pair.evidence),
      ) &&
      (combo.plan == null ||
        (record(combo.plan) &&
          Array.isArray(combo.plan.pairs) &&
          combo.plan.pairs.every(strings) &&
          optionalString(combo.plan.detail))))
  );
}
function memory(value: unknown): boolean {
  if (value == null) return true;
  if (!record(value)) return false;
  const recovery = value.cacheRecovery;
  return (
    recovery == null ||
    (record(recovery) &&
      ['status', 'detail', 'model'].every((key) =>
        optionalString(recovery[key]),
      ))
  );
}
// Optional parts of the response: one that fails is dropped and named.
const responseSections: [string, (value: unknown) => boolean, string][] = [
  ['warmup', warmup, 'warm-up status'],
  ['resumeDemand', resumeDemand, 'the resume status'],
  ['reporting', reporting, 'multi-model reporting'],
  ['combinations', combinations, 'model pairs'],
  ['memory', memory, 'memory status'],
];

/** Validate the containers used during render before publishing an API result. */
export function validOptimizerResponse(value: unknown): boolean {
  return (
    coreResponse(value) &&
    value.models.every(modelRow) &&
    value.events.every(event) &&
    responseSections.every(([key, check]) => check(value[key])) &&
    (value.demandAuto == null || demandAuto(value.demandAuto))
  );
}

export type OptimizerReading<T> = {
  value: T;
  /** Short names of the parts that were dropped, for "Couldn't read: …". */
  unreadable: string[];
  /**
   * Whether a dropped part counts toward a validation report. Old-format history rows
   * (switch runs, activity events) are tolerated, as before (6bf6a75, Sep 26 run 131):
   * they are named in "Couldn't read" but never reported.
   */
  reportable: boolean;
};
const HISTORY_LABELS = ['some activity', 'some switch history'];
// Lists the backend caps at these lengths today (decision_journal.py history,
// demand_targets.py high_earnings rates). Longer lists are clamped, not rejected.
const HISTORY_LIMIT = 30;
const RATES_LIMIT = 3;

/**
 * Read /api/optimizer section by section. Only the response's own fields are
 * required (null otherwise); a malformed optional section, row or plan part is
 * dropped and named, so one odd value never blanks the whole Optimizer page.
 * Whatever is returned passes validOptimizerResponse.
 */
export function readOptimizerResponse<T = Record<string, unknown>>(
  input: unknown,
): OptimizerReading<T> | null {
  if (!coreResponse(input)) return null;
  const value: Record<string, unknown> = { ...input };
  const unreadable: string[] = [];
  const drop = (label: string) => {
    if (!unreadable.includes(label)) unreadable.push(label);
  };
  const rows = (
    items: unknown[],
    check: (item: unknown) => boolean,
    label: string,
  ) => {
    const kept = items.filter(check);
    if (kept.length !== items.length) drop(label);
    return kept;
  };
  value.models = rows(input.models, modelRow, 'some model evidence');
  value.events = rows(input.events, event, 'some activity');
  for (const [key, check, label] of responseSections)
    if (value[key] != null && !check(value[key])) {
      delete value[key];
      drop(label);
    }
  if (value.demandAuto != null) {
    const auto = readDemandAuto(value.demandAuto, rows, drop);
    if (auto) value.demandAuto = auto;
    else {
      delete value.demandAuto;
      drop('the automatic plan');
    }
  }
  return {
    value: value as T,
    unreadable,
    reportable: unreadable.some((label) => !HISTORY_LABELS.includes(label)),
  };
}

/** The saved plan couldn't be read, so saving could store something else in its place. */
export const savedPlanUnreadable = (unreadable: readonly string[]) =>
  unreadable.includes('the automatic plan') ||
  unreadable.includes('the saved plan');

/**
 * Keep the last readable automatic plan, or its saved plan, when this reading dropped
 * it. Without it a manager Mac would show the legacy controls, and the plan editor
 * would start from the live policy instead of the saved one.
 */
export function keepReadablePlan<T extends { demandAuto?: unknown }>(
  next: T,
  unreadable: readonly string[],
  previous?: { demandAuto?: unknown } | null,
): T {
  const last = previous?.demandAuto;
  if (!record(last)) return next;
  if (next.demandAuto == null)
    return unreadable.includes('the automatic plan')
      ? { ...next, demandAuto: last }
      : next;
  if (
    record(next.demandAuto) &&
    next.demandAuto.savedPolicy == null &&
    last.savedPolicy != null &&
    unreadable.includes('the saved plan')
  )
    return {
      ...next,
      demandAuto: { ...next.demandAuto, savedPolicy: last.savedPolicy },
    };
  return next;
}

function readDemandAuto(
  input: unknown,
  rows: (
    items: unknown[],
    check: (item: unknown) => boolean,
    label: string,
  ) => unknown[],
  drop: (label: string) => void,
): Record<string, unknown> | null {
  if (!demandAutoCore(input) || !record(input)) return null;
  const auto: Record<string, unknown> = { ...input };
  auto.runs = rows(input.runs as unknown[], run, 'some switch history');
  auto.opportunities = rows(
    input.opportunities as unknown[],
    opportunity,
    'some model comparisons',
  );
  const decisions = auto.execution;
  if (
    record(decisions) &&
    Array.isArray(decisions.history) &&
    decisions.history.length > HISTORY_LIMIT
  )
    // Newest first.
    auto.execution = {
      ...decisions,
      history: decisions.history.slice(0, HISTORY_LIMIT),
    };
  const target = auto.earningsTarget;
  const high = record(target) ? target.highEarnings : null;
  if (
    record(target) &&
    record(high) &&
    Array.isArray(high.rates) &&
    high.rates.length > RATES_LIMIT
  )
    // Oldest first: keep the latest windows.
    auto.earningsTarget = {
      ...target,
      highEarnings: { ...high, rates: high.rates.slice(-RATES_LIMIT) },
    };
  for (const [key, check, label] of demandSections)
    if (auto[key] != null && !check(auto[key])) {
      delete auto[key];
      drop(label);
    }
  const limits = auto.limits as Record<string, unknown>;
  if (!samplingBudget(limits.sampling)) {
    auto.limits = { ...limits, sampling: null };
    drop('learning time');
  }
  return demandAuto(auto) ? auto : null;
}
