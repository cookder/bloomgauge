'use client';
import { useState } from 'react';
import { ConfirmationProgress } from './optimizer-confirmation';
import type { OptimizerConfirmation } from '@/lib/optimizer-live';
import { comparisonOrder } from '@/lib/optimizer-comparison';
import { currentPaidAlternative } from '@/lib/pulse-run-status';
import { TrendingUp } from 'lucide-react';
import type {
  ConditionalBaseline,
  DecisionExecution,
} from '@/lib/optimizer-response';
import { Choice, money, num, shortModel, age, plural } from './shared';
import { useAppNavigation } from './app-navigation';
import { snap, tuningRanges, type TunedKey } from '@/lib/optimizer-tuning';

export type DemandRules = {
  minRunMinutes: number;
  confirmationMinutes: number;
  improvementPercent: number;
  planningMinutes: number;
  minimumNetUsd: number;
  maxSwitchesPerDay: number;
  maxDowntimeMinutes: number;
  memoryHeadroomGB: number;
  idleEscapeMinutes: number;
  trialMinutes: number;
  trialCooldownMinutes: number;
  fallbackEnabled: number;
  targetUsdPerHour: number;
  baselineLearningEnabled: number;
  protectUsdPerHour: number;
  learningMinutesPerDay: number;
};
export const defaultDemandRules: DemandRules = {
  minRunMinutes: 30,
  confirmationMinutes: 5,
  improvementPercent: 20,
  planningMinutes: 60,
  minimumNetUsd: 0.02,
  maxSwitchesPerDay: 12,
  maxDowntimeMinutes: 30,
  memoryHeadroomGB: 1,
  idleEscapeMinutes: 20,
  trialMinutes: 20,
  trialCooldownMinutes: 30,
  fallbackEnabled: 1,
  targetUsdPerHour: 0.12,
  baselineLearningEnabled: 1,
  protectUsdPerHour: 0.2,
  learningMinutesPerDay: 60,
};
type Estimate = {
  rate: number;
  lower: number;
  upper: number;
  hours: number;
  evidenceHours?: number;
  days: number;
  scope: string;
  demandMatched?: boolean;
  recent?: boolean;
  recentGuardRate?: number;
  forecastUsable?: boolean;
  confidence?: string;
  historyReason?: string;
  asOf?: number;
};
type Opportunity = {
  model: string;
  selected: boolean;
  current: boolean;
  eligible: boolean;
  reason?: string | null;
  estimate?: Estimate | null;
  signal: {
    status?: string;
    load?: number | null;
    pressure?: number | null;
    loadRatio?: number | null;
    pressureRatio?: number | null;
  };
  spike?: {
    qualified: boolean;
    reason: string;
    loadRatio: number | null;
    pressureRatio: number | null;
    trialsUsed: number;
    trialLimit: number;
    maxClockMinutes: number;
  };
  kind?: string;
  sustained?: { qualified?: boolean; pressure?: number | null };
  trialCooldownUntil?: number | null;
  failureCooldownUntil?: number | null;
  netGainUsd?: number | null;
  paybackMinutes?: number | null;
  switchCostUsd?: number | null;
  outboundSeconds: number;
  returnSeconds: number;
  costScope: string;
  timingSamples?: number;
  extraMemoryGB: number;
  loadBudget?: {
    afterUnloadGB: number;
    requiredGB: number;
    reclaimableGB?: number;
  } | null;
  conditional?: ConditionalBaseline | null;
  historyWeight?: number;
  fallbackPreferred?: boolean;
  selectionReason?: string | null;
  learning?: {
    qualified: boolean;
    reason: string;
    evidence?: {
      completeTokenSamples: number;
      jobMinutes: number;
      fresh: boolean;
      needsSamples: boolean;
    } | null;
  };
};
type Fallback = {
  model: string;
  enabled: boolean;
  qualified: boolean;
  current: boolean;
  eligible: boolean;
  status: string;
  reason: string;
  holdReason?: string | null;
  selection?: string | null;
  warmMinutes: number;
  windows: number;
  trafficWindows: number;
  trafficPercent?: number | null;
  usdPerHour?: number | null;
  paidJobs: number;
  asOf?: number | null;
  expiresAt?: number | null;
};
type Trial = {
  runId: number;
  model: string;
  current: boolean;
  status: string;
  warmSeconds: number;
  trialMinutes: number;
  firstTrafficSeconds?: number | null;
  requests: number;
  tokens: number;
  idlePercent?: number | null;
  usd?: number | null;
  usdPerHour?: number | null;
  paidWarmSeconds: number;
  cooldownUntil?: number | null;
  paymentSeen?: boolean;
  clock?: {
    start?: number;
    end?: number | null;
    seconds?: number | null;
    usd?: number | null;
    usdPerHour?: number | null;
    coveragePercent?: number;
    qualified?: boolean;
  };
  competitive?: {
    outcome: 'win' | 'loss' | 'uncertain';
    rateBasis: string;
    comparison: {
      qualified?: boolean;
      lower?: number | null;
      upper?: number | null;
    };
    revision?: string | null;
  };
  partial?: boolean;
  settled?: boolean;
  coveragePercent?: number;
  settlementDeadline?: number | null;
  learning?: {
    completeTokenSamples: number;
    jobMinutes: number;
    sampleGoal: number;
    jobMinutesGoal: number;
    targetReached: boolean;
    usdPerRequest: number | null;
    usdPerMillionTokens: number | null;
  } | null;
};
export type DemandAutoData = {
  at: number;
  enabled: boolean;
  currentModel?: string;
  target?: string | null;
  reason: string;
  policy: DemandRules;
  savedPolicy?: DemandRules;
  planningMinutes: number;
  baseline?: Estimate | null;
  controlError?: string | null;
  opportunities: Opportunity[];
  scanAt: number;
  scanStatus: string;
  kind?: string | null;
  escapeReady?: boolean;
  activity?: { fresh: boolean; idleSeconds: number };
  trial?: Trial | null;
  execution?: DecisionExecution;
  paidAlternative?: {
    model: string;
    eligible: boolean;
    reason: string;
    at: number;
  } | null;
  fallback?: Fallback;
  earningsTarget?: {
    usdPerHour: number;
    dailyUsd: number;
    protectUsdPerHour?: number;
    rate?: number | null;
    fastRate?: number | null;
    productiveFloor?: number;
    highEarnings?: {
      active: boolean;
      threshold: number;
      windowMinutes: number;
      coveredMinutes: number;
      rates: (number | null)[];
      reason: string;
    };
    livePaid?: {
      fresh: boolean;
      rate?: number | null;
      seconds: number;
      asOf?: number | null;
    } | null;
    warmMinutes: number;
    coveragePercent?: number;
    asOf?: number | null;
    ready: boolean;
    belowTarget: boolean;
    status: string;
    reason: string;
    nextCheckAt?: number | null;
  };
  preferredReturn?: {
    model: string;
    qualified: boolean;
    eligible: boolean;
    rate?: number | null;
    hours: number;
    scope: string;
    asOf?: number | null;
    selection?: string | null;
    reason: string;
  };
  explorationTrigger?: string | null;
  baselineLearning?: { enabled: boolean; trialsUsed: number };
  dataGathering?: DataGathering;
  spikeReview?: {
    runId: number;
    incumbent: string;
    referenceRate: number | null;
    ordinary?: boolean;
    rateBasis?: string;
    trialRate: number | null;
    liveRate: number | null;
    status: string;
    reason: string;
    at: number;
    deadline: number;
    learning?: boolean;
    demand?: {
      status: string;
      reason: string;
      load: number | null;
      pressure: number | null;
      referenceLoad: number | null;
      referencePressure: number | null;
      sourceAt: number | null;
    };
  } | null;
  confirmation?:
    | (Omit<OptimizerConfirmation, 'requiredSeconds'> & {
        samples: number;
        since: number;
        kind?: string;
        requiredSeconds?: number;
      })
    | null;
  limits: {
    switchesUsed: number;
    switchLimit: number;
    downtimeMinutesUsed: number;
    downtimeMinutesLimit: number;
    nextRunAt: number;
    sampling?: {
      minutesUsed: number;
      minutesLimit: number;
      reservationMinutes: number;
      unknownLifecycles: number;
      qualified: boolean;
      availableAt?: number | null;
    };
  };
  runs: {
    id: number;
    at: number;
    model: string;
    result: string;
    downtime?: number | null;
    decision: {
      candidate?: Opportunity | null;
      planningMinutes?: number | null;
      kind?: string;
      reason?: string | null;
      outcome?: Trial;
      trialResolution?: { status?: string; reason?: string; at?: number };
    };
  }[];
};
const trialStatus: Record<string, string> = {
  running: 'Measuring trial',
  settling: 'Waiting for credits to settle',
  productive: 'Paid work found',
  no_traffic: 'No traffic found',
  no_paid_work: 'Traffic without paid credits',
  interrupted: 'Trial interrupted',
  insufficient_coverage: 'Finished · partial coverage',
};
function TrialEconomics({ trial }: { trial: Trial }) {
  if (!trial.competitive) return null;
  const clock = trial.clock,
    benchmark = trial.competitive.comparison;
  const result = trial.competitive.outcome;
  return (
    <div className="trial-economics">
      <p>
        <strong>
          {result === 'win'
            ? 'Above entry benchmark'
            : result === 'loss'
              ? 'Below entry benchmark'
              : 'Earnings comparison uncertain'}
        </strong>
        {trial.paymentSeen != null && (
          <span>
            {' '}
            ·{' '}
            {trial.paymentSeen ? 'Payment received' : 'No payment observed yet'}
          </span>
        )}
      </p>
      <p>
        {clock?.qualified
          ? `${money(clock.usdPerHour)} / elapsed hour · ${num((clock.seconds ?? 0) / 60, 1)} min including loading and idle time`
          : !trial.competitive.revision
            ? 'Elapsed-time earnings were not recorded for this earlier trial.'
            : 'Waiting for covered, settled elapsed-time earnings.'}
      </p>
      {benchmark.qualified && (
        <small>
          Entry benchmark: {money(benchmark.lower)}–{money(benchmark.upper)} /
          hr from the previous model’s observed paid work. This is a saved
          comparison, not a forecast of what it would have earned.
        </small>
      )}
      {!benchmark.qualified && (
        <small>
          No qualified entry benchmark was saved. Receiving payment alone does
          not establish an earnings improvement.
        </small>
      )}
    </div>
  );
}
const holdNames: Record<string, string> = {
  protected: 'Holding the current model',
  memory: 'Memory',
  temperature: 'Temperature',
  power: 'Power',
  identity: 'Provider identity',
  readiness: 'Model readiness',
  network: 'Demand evidence',
  daily_limit: 'Daily limits',
  retry: 'Retry wait',
  confirmation: 'Confirming opportunity',
  minimum_run: 'Minimum observation',
  trial: 'Trial observation',
  earnings_evidence: 'Earnings evidence',
  freshness: 'Waiting for fresh readings',
  selection: 'Model selection',
  gain: 'Earnings improvement',
  observation: 'Observing',
  off: 'Automation off',
  switching: 'Switching',
};
function ExecutionHistory({
  value,
  enabled,
}: {
  value?: DecisionExecution;
  enabled: boolean;
}) {
  if (!value) return null;
  const current = value.current;
  return (
    <div className="decision-execution">
      {current && (
        <div
          className={`decision-now ${!value.fresh && enabled ? 'notice' : ''}`}
          role="status"
        >
          <strong>
            {value.fresh
              ? (holdNames[current.code] ?? current.phase)
              : 'Last recorded decision'}
            {current.target ? ` · ${shortModel(current.target)}` : ''}
          </strong>
          <p>{current.reason}</p>
          <small>
            {num(current.observedSeconds / 60, 1)} min observed in this state ·
            checked {age(current.updated)}
            {current.sourceAt
              ? ` · demand source ${age(current.sourceAt)}`
              : ''}
          </small>
          {!value.fresh && enabled && (
            <small>
              The decision loop has not reported recently. This saved status is
              not current.
            </small>
          )}
        </div>
      )}
      <details>
        <summary>Decision history · last 24 hours</summary>
        <p className="footnote">
          Qualified automatic switches do not wait for idle time. Darkbloom
          0.9.9 and later finish accepted requests first; older versions may
          interrupt them. Times below count observed decisions; app downtime is
          not added.
        </p>
        {!!value.totals.length && (
          <div className="decision-totals">
            {value.totals.map((t) => (
              <span key={t.code}>
                {holdNames[t.code] ?? t.code}
                <strong>{num(t.seconds / 60, 1)} min</strong>
              </span>
            ))}
          </div>
        )}
        <div className="decision-history">
          {value.history.slice(0, 20).map((r) => (
            <article key={r.id}>
              <div>
                <strong>
                  {holdNames[r.code] ?? r.phase}
                  {r.target ? ` · ${shortModel(r.target)}` : ''}
                </strong>
                <time>
                  {new Date(r.at * 1000).toLocaleString([], {
                    month: 'short',
                    day: 'numeric',
                    hour: 'numeric',
                    minute: '2-digit',
                  })}
                </time>
              </div>
              <p>{r.reason}</p>
              <small>
                {num(r.observedSeconds / 60, 1)} min observed
                {r.requiredSeconds > 0 && r.phase === 'confirming'
                  ? ` · ${num(r.confirmationSeconds / 60, 1)} / ${num(r.requiredSeconds / 60, 0)} min confirmation`
                  : ''}
              </small>
            </article>
          ))}
        </div>
        {!value.history.length && (
          <p className="muted">
            Decision history begins with this version. Earlier switch events
            remain in the log.
          </p>
        )}
      </details>
    </div>
  );
}
const scope = (estimate?: Pick<Estimate, 'scope'> | null) =>
  ({
    current_session_30m: 'This session · last 30m of settled work',
    weekday_time: 'Matching day of week & time',
    daytype_time: 'Matching weekday/weekend & time',
    all_observed_hours: 'All observed hours · last 30 days',
    similar_demand: 'Similar demand · all times of day',
  })[estimate?.scope ?? ''] ?? 'Waiting for paid-work evidence';
const signalName: Record<string, string> = {
  spike: 'Sustained spike',
  watching: 'Checking spike',
  normal: 'Normal demand',
  stale: 'Stale demand',
  no_headroom: 'No warm capacity',
  learning: 'Learning demand',
};
const timingScope = (row: Opportunity) =>
  row.costScope === 'mac_history'
    ? `Based on ${row.timingSamples ?? 'recent'} switches on this Mac · model not timed yet`
    : row.costScope === 'unmeasured'
      ? 'Initial 2m estimate · no measured timing yet'
      : `${row.timingSamples ?? 'Recent'} successful switch${row.timingSamples === 1 ? '' : 'es'} to this model · includes warm-up`;

export type DataGathering = {
  active: boolean;
  startedAt?: number;
  endsAt?: number;
  seconds?: number;
  remainingSeconds?: number;
};
const gatheringDurations: [number, string][] = [
  [86400, '24 hours'],
  [259200, '3 days'],
  [604800, '7 days'],
];
const timeLeft = (seconds: number) =>
  seconds >= 86400
    ? `${num(seconds / 86400, 1)} days`
    : seconds >= 3600
      ? `${num(seconds / 3600, 1)} hours`
      : `${num(Math.max(1, seconds / 60), 0)} min`;
export function DataGatheringControl({
  value,
  onSet,
  disabled,
  compact = false,
}: {
  value?: DataGathering;
  onSet: (seconds: number) => void;
  disabled: boolean;
  compact?: boolean;
}) {
  const active = !!value?.active;
  if (compact)
    return (
      <div className="data-gathering compact">
        <span className="optimizer-plan-label">
          Learning boost{' '}
          <small>
            {active
              ? `on · ${timeLeft(value?.remainingSeconds ?? 0)} left`
              : 'off'}
          </small>
        </span>
        <div className="data-gathering-actions">
          {active ? (
            <button type="button" disabled={disabled} onClick={() => onSet(0)}>
              Stop
            </button>
          ) : (
            gatheringDurations.map(([seconds, label]) => (
              <button
                key={seconds}
                type="button"
                disabled={disabled}
                onClick={() => onSet(seconds)}
              >
                {label}
              </button>
            ))
          )}
        </div>
        <small>
          {disabled && !active
            ? 'Available while the optimizer is on.'
            : 'At least 3 hours of learning a day, without waiting for low earnings. Your protect level and safety checks stay on.'}
        </small>
      </div>
    );
  return (
    <div className="demand-fallback-readout data-gathering">
      <div>
        <strong>Learning boost</strong>
        <span>
          {active
            ? `On · ${timeLeft(value?.remainingSeconds ?? 0)} left`
            : 'Off'}
        </span>
      </div>
      <p>
        Learns this Mac faster: at least 3 hours of learning time a day, and
        learning runs don’t wait for low earnings. Before Darkbloom 0.9.9, runs
        may interrupt active requests.
      </p>
      <small>
        Your protect level, memory, temperature, power and demand checks still
        apply. It only acts while the optimizer is on, and ends automatically.
      </small>
      <div className="data-gathering-actions">
        {active ? (
          <button
            type="button"
            className="text-link"
            disabled={disabled}
            onClick={() => onSet(0)}
          >
            Stop learning boost
          </button>
        ) : (
          gatheringDurations.map(([seconds, label]) => (
            <button
              key={seconds}
              type="button"
              className="text-link"
              disabled={disabled}
              onClick={() => onSet(seconds)}
            >
              Boost for {label}
            </button>
          ))
        )}
      </div>
    </div>
  );
}

export function DemandSettings({
  value,
  onChange,
  disabled,
  flat = false,
}: {
  value: DemandRules;
  onChange: (next: DemandRules) => void;
  disabled: boolean;
  flat?: boolean;
}) {
  const control = (
    key: keyof DemandRules,
    label: string,
    values: number[],
    format: (v: number) => string,
  ) => (
    <label key={key}>
      {label}
      <Choice
        value={String(value[key])}
        disabled={disabled}
        onChange={(next) => onChange({ ...value, [key]: Number(next) })}
        label={label}
        options={values.map((v) => ({ value: String(v), label: format(v) }))}
      />
    </label>
  );
  const number = (key: TunedKey, label: string, unit: string, hint: string) => (
    <NumberSetting
      key={key}
      id={`tune-${key}`}
      label={label}
      unit={unit}
      hint={hint}
      disabled={disabled}
      value={value[key]}
      range={tuningRanges[key]}
      onChange={(next) => onChange({ ...value, [key]: snap(key, next) })}
    />
  );
  const confirmTooLong = value.confirmationMinutes > value.minRunMinutes;
  const advanced = (
    <div className="demand-rule-grid">
      {number(
        'idleEscapeMinutes',
        'Try another model after warm idle',
        'min',
        'Lower moves on from quiet models sooner.',
      )}
      {number(
        'trialMinutes',
        'Measure each demand trial for',
        'warm min',
        'Shorter trials test more models, with less evidence each.',
      )}
      {control('fallbackEnabled', 'GPT-OSS fallback preference', [1, 0], (v) =>
        v
          ? 'Last resort · recent paid traffic required'
          : 'Off · rank like other models',
      )}
      {number(
        'minRunMinutes',
        'Minimum run for earnings comparisons',
        'min',
        'How long a model runs before its earnings are compared.',
      )}
      {number(
        'confirmationMinutes',
        'Confirm the opportunity for',
        'min',
        confirmTooLong
          ? 'Must be no longer than the minimum run.'
          : 'How long demand must hold before switching.',
      )}
      {number(
        'trialCooldownMinutes',
        'Bloomkeeper retry wait after an unpaid trial',
        'min',
        'Before testing the same model again.',
      )}
      {number(
        'improvementPercent',
        'Minimum rate improvement',
        '%',
        'Gain needed over the current model to switch.',
      )}
      {number(
        'minimumNetUsd',
        'Minimum gain after switch costs',
        'USD',
        'Expected extra earnings after the time spent loading.',
      )}
      {number(
        'planningMinutes',
        'Compare expected earnings over',
        'min',
        'The window Bloomkeeper forecasts earnings for.',
      )}
      {number(
        'memoryHeadroomGB',
        'Extra memory above load requirement',
        'GB',
        'Safety margin. Never below 1 GB.',
      )}
      {number(
        'maxSwitchesPerDay',
        'Maximum automatic attempts in 24 hours',
        'attempts',
        'Includes trials and switches.',
      )}
      {number(
        'maxDowntimeMinutes',
        'Maximum downtime in 24 hours',
        'min',
        'Time spent unloading and loading models.',
      )}
      {number(
        'learningMinutesPerDay',
        'Learning time per day',
        'min',
        'Time Bloomkeeper may spend measuring other models while pace is below your protect level. 0 turns learning off.',
      )}
      {control(
        'targetUsdPerHour',
        'Earnings goal for the Target report',
        [0.08, 0.1, 0.12, 0.15, 0.2, 0.25],
        (v) => `${money(v)} / hour · report only`,
      )}
    </div>
  );
  // Flat: shown inside the optimizer card's single Fine-tune fold, where the target is already inline.
  if (flat)
    return (
      <div className="demand-auto-settings">
        <div className="demand-rule-grid">
          {control(
            'baselineLearningEnabled',
            'Learn payment baselines during low earnings',
            [1, 0],
            (v) =>
              v
                ? 'On · within your learning time'
                : 'Off · ordinary demand trials only',
          )}
        </div>
        {advanced}
        <p className="footnote">
          These are Bloomkeeper controls, not network cooldowns. Daily downtime
          reserves room for a possible recovery; earnings comparisons deduct
          only the next load. A demand trial can leave before the normal minimum
          run. Limits persist across restarts.
        </p>
      </div>
    );
  return (
    <div className="demand-auto-settings">
      <div className="demand-rule-grid">
        {control(
          'targetUsdPerHour',
          'Earnings target per hour',
          [0.08, 0.1, 0.12, 0.15, 0.2, 0.25],
          (v) => `${money(v)} / hour`,
        )}
        {control(
          'baselineLearningEnabled',
          'Learn payment baselines during low earnings',
          [1, 0],
          (v) =>
            v
              ? 'On · within your learning time'
              : 'Off · ordinary demand trials only',
        )}
      </div>
      <p className="footnote">
        The target guides earnings comparisons. Bloomkeeper protects productive runs
        and learns from confirmed paid work.
      </p>
      <details>
        <summary>Advanced timing & safeguards</summary>
        <p className="footnote">
          The target is an aspiration, not a reason to leave productive work.
          Follow demand preserves paid rates within 25% of the target unless a
          supported earnings improvement or an exceptional-demand experiment
          qualifies. Trials continue during substantial shortfalls or quiet
          periods; passive learning and scheduled tests continue too. Qualified
          switches do not wait for idle time.
        </p>
        <p className="footnote">
          No exploratory trials or scheduled test rotations while paid earnings
          stay at or above $0.20/hour across three consecutive five-minute
          windows and the current paid check. Passive learning continues;
          supported earnings upgrades and manual choices remain available.
        </p>
        <p className="footnote">
          Exceptional-demand trials need two five-minute windows above 3× the
          model’s usual demand and load per warm provider, with load/warm of at
          least 1 and no sharp fade. Up to three trials per rolling day; repeat
          testing needs a materially larger spike. Bloomkeeper saves the previous paid
          rate and rechecks a return if the trial is worse. These are learning
          experiments, not predicted upgrades.
        </p>

        {advanced}
        <p className="footnote">
          These are Bloomkeeper controls, not network cooldowns. Daily downtime
          reserves room for a possible recovery; earnings comparisons deduct
          only the next load. A demand trial can leave before the normal minimum
          run. Limits persist across restarts.
        </p>
      </details>
    </div>
  );
}

export function DemandAutoPanel({
  data,
  stale,
}: {
  data?: DemandAutoData;
  stale: boolean;
}) {
  const navigation = useAppNavigation();
  const [comparisonModel, setComparisonModel] = useState('');
  if (!data)
    return (
      <section className="panel demand-auto-panel">
        <h2>Demand auto-switch</h2>
        <p className="muted">Connecting to the opportunity scanner…</p>
      </section>
    );
  const alternatives = comparisonOrder(data.opportunities);
  const best =
    alternatives.find((r) => r.model === comparisonModel) ??
    alternatives.find((r) => r.model === data.target) ??
    alternatives[0];
  const baseline = data.baseline;
  const now = Date.now() / 1000;
  const confirmation = data.confirmation;
  const required =
    confirmation?.requiredSeconds ?? data.policy.confirmationMinutes * 60;
  const scanStale =
    stale ||
    Date.now() / 1000 - data.scanAt > 120 ||
    data.scanStatus === 'stale';
  const trial =
    data.trial?.current && data.trial.model === data.currentModel
      ? data.trial
      : undefined;
  const actual = data.execution?.current;
  const actualFresh =
    !!data.execution?.fresh &&
    !scanStale &&
    actual?.model === data.currentModel &&
    (!data.enabled || actual?.mode === 'demand');
  const controllerFresh =
    !stale &&
    now - data.at <= 45 &&
    now - data.at >= -5 &&
    data.execution?.fresh &&
    actual?.model === data.currentModel &&
    actual?.mode === 'demand';
  const showConfirmation = !!(
    data.enabled &&
    confirmation &&
    controllerFresh &&
    actual?.phase !== 'switching' &&
    (confirmation.status != null ||
      (actual?.phase === 'confirming' && actual.target === confirmation.model))
  );
  const actualTarget =
    actualFresh && actual && ['confirming', 'switching'].includes(actual.phase)
      ? actual.target
      : null;
  const goal = data.earningsTarget;
  const preferred = data.preferredReturn;
  const resolution =
    trial &&
    data.runs.find((run) => run.id === trial.runId && run.model === trial.model)
      ?.decision.trialResolution;
  const trialClosed =
    resolution?.status === 'keep' || resolution?.status === 'inconclusive';
  const alternative =
    actualFresh &&
    currentPaidAlternative(data.paidAlternative, Date.now() / 1000);
  return (
    <section className="panel demand-auto-panel">
      <div className="panel-heading">
        <div>
          <div className="eyebrow">DEMAND + PAID WORK</div>
          <h2>Would switching pay more?</h2>
        </div>
        <TrendingUp size={22} />
      </div>
      <div className="demand-auto-state">
        <span
          className={`status-pill ${data.enabled && actualFresh && actual?.phase !== 'waiting' ? 'good' : 'warn'}`}
        >
          {showConfirmation &&
          confirmation?.status &&
          (confirmation.expiresAt == null || now < confirmation.expiresAt)
            ? `On · ${confirmation.status === 'paused' ? 'confirmation paused' : confirmation.status === 'ready' ? 'earnings confirmed' : 'confirming earnings'}`
            : scanStale || (data.enabled && !actualFresh)
              ? 'Waiting for fresh controller status'
              : data.enabled
                ? `On · ${actual?.phase === 'waiting' ? 'waiting' : actual?.phase === 'confirming' ? 'confirming' : actual?.phase === 'switching' ? 'switching' : trial?.status === 'running' && !trialClosed ? 'measuring' : 'watching'}`
                : 'Demand mode off · preview'}
        </span>
        <span className="small muted">Demand source · {age(data.scanAt)}</span>
      </div>
      <p className="small muted">
        {data.enabled
          ? 'Runs on your Mac, even with this phone app closed. Keep Bloomkeeper running and your Mac awake.'
          : 'Once enabled, switching runs on your Mac. Your phone app can be closed.'}
      </p>
      {(scanStale ||
        data.controlError ||
        !data.enabled ||
        !data.execution?.current) && (
        <p
          className={scanStale || data.controlError ? 'notice' : 'small muted'}
        >
          {scanStale
            ? 'Saved comparisons are not a live switching recommendation.'
            : data.reason}
        </p>
      )}
      {data.controlError && (
        <div className="controller-review-actions">
          <p className="footnote">
            This holds automatic switching; it does not stop a model already
            serving. Your settings have not been changed.
          </p>
          <button
            type="button"
            className="text-link"
            onClick={() => navigation.navigate('switch')}
          >
            Review model controls →
          </button>
          <button
            type="button"
            className="text-link"
            onClick={() => navigation.navigate('support')}
          >
            Help & feedback →
          </button>
        </div>
      )}
      <ExecutionHistory
        value={
          data.execution ? { ...data.execution, fresh: actualFresh } : undefined
        }
        enabled={data.enabled}
      />
      {showConfirmation && confirmation && (
        <ConfirmationProgress
          value={{ ...confirmation, requiredSeconds: required }}
          now={now}
        />
      )}
      {data.enabled &&
        resolution?.status === 'inconclusive' &&
        typeof resolution.reason === 'string' && (
          <div className="demand-trial-readout" role="status">
            <strong>Trial ended · Inconclusive</strong>
            <p>{resolution.reason}</p>
            <small>
              This result does not establish an earnings improvement. Recorded
              payments and observation time remain in history.
            </small>
          </div>
        )}
      {alternative && (
        <div className="demand-trial-readout" role="status">
          <strong>Paid alternative · {shortModel(alternative.model)}</strong>
          <p>{alternative.reason}</p>
          <small>
            Compared using local paid evidence. Current demand, memory and
            switch limits still apply.
          </small>
        </div>
      )}
      {data.baselineLearning && (
        <details className="demand-fallback-readout">
          <summary>
            <strong>Payment baseline learning</strong>
            <span>
              {data.baselineLearning.enabled
                ? `${data.baselineLearning.trialsUsed} in the last 24 h`
                : 'Off'}
            </span>
          </summary>
          <p>
            During sustained low earnings, fill gaps in payment per request and
            input/output token mix. Aim for 50 complete token samples across 10
            paid minutes; stop at {data.policy.trialMinutes} warm minutes or
            review after 40 clock minutes. Then compare with the previous model.
          </p>
          <small>
            Runs share your learning time, each model at most once every 4
            hours, with room for a return. Needs sustained demand; memory and
            paid-work protections still apply. Sample counts measure coverage,
            not certainty about future earnings.
          </small>
          {data.opportunities
            .filter((r) => r.selected)
            .map((r) => (
              <p key={r.model} className="small">
                <strong>{shortModel(r.model)}</strong> ·{' '}
                {r.learning?.evidence
                  ? `${num(r.learning.evidence.completeTokenSamples, 0)} complete jobs / ${num(r.learning.evidence.jobMinutes, 0)} paid minutes in 7d`
                  : 'Coverage unavailable'}
                <br />
                {r.current
                  ? 'Learning passively while serving.'
                  : r.learning?.reason}
                <br />
                {r.reason && !r.current ? r.reason : ''}
              </p>
            ))}
        </details>
      )}
      {goal && (
        <details className="demand-target-readout">
          <summary>
            <strong>Earnings checks</strong>
            <span>
              {money(goal.protectUsdPerHour ?? 0.2)} / hour protect level
            </span>
          </summary>
          <div>
            <strong>
              Protect level · {money(goal.protectUsdPerHour ?? 0.2)} / hour
            </strong>
            <span>
              Above this, Bloomkeeper doesn’t interrupt the current model to learn.
            </span>
          </div>
          <p>
            {goal.rate != null
              ? `${money(goal.rate)} / hr observed · ${num(goal.warmMinutes, 0)} covered warm min`
              : 'Gathering covered earnings from this session'}
          </p>
          <small>
            {scanStale
              ? 'Waiting for fresh readings before checking the protect level.'
              : goal.reason}
          </small>
          {goal.highEarnings?.active && (
            <small className="good-text">
              Protected · learning runs held above{' '}
              {money(goal.highEarnings.threshold)} / hr ·{' '}
              {num(goal.highEarnings.coveredMinutes, 0)} covered minutes in the
              last {goal.highEarnings.windowMinutes}m of settled work
            </small>
          )}
          {goal.livePaid?.fresh && (
            <small>
              Fresh 5m confirmed pace: {money(goal.livePaid.rate)} / hr ·{' '}
              {num(goal.livePaid.seconds / 60, 1)} warm min
            </small>
          )}
          {goal.coveragePercent != null && (
            <small>
              {num(goal.coveragePercent, 0)}% covered in the rolling observation
              window · gaps remain unknown
            </small>
          )}
          {preferred && (
            <div className="preferred-return-readout">
              <strong>Preferred return · {shortModel(preferred.model)}</strong>
              <p>
                {preferred.qualified && preferred.rate != null
                  ? `${money(preferred.rate)} / hr observed · ${scope(preferred)}`
                  : 'Learning repeated paid-work evidence'}
              </p>
              {preferred.qualified && (
                <small>
                  {num(preferred.hours, 1)} total verified hours saved
                </small>
              )}
              <small>
                {scanStale
                  ? 'Current return eligibility is unverified.'
                  : preferred.reason}
              </small>
            </div>
          )}
          <small>
            Comparison uses this Mac’s inference earnings; account base rewards
            stay separate. Passive history keeps growing while this model runs.
          </small>
        </details>
      )}
      {data.spikeReview && (
        <div className="demand-trial-readout" role="status">
          <strong>
            {data.spikeReview.ordinary
              ? 'Ordinary model'
              : data.spikeReview.learning
                ? 'Baseline-learning'
                : 'Exceptional-demand'}{' '}
            trial · {shortModel(data.currentModel ?? '')}
          </strong>
          <p>{data.spikeReview.reason}</p>
          <dl>
            <div>
              <dt>
                Entry benchmark · {shortModel(data.spikeReview.incumbent)}
              </dt>
              <dd>{money(data.spikeReview.referenceRate)} / hr</dd>
            </div>
            <div>
              <dt>
                {data.spikeReview.rateBasis ===
                'settled_inference_per_elapsed_selection_hour'
                  ? 'Trial · settled / elapsed hour'
                  : 'Trial · settled / warm hour'}
              </dt>
              <dd>{money(data.spikeReview.trialRate)} / hr</dd>
            </div>
            <div>
              <dt>Trial · fresh 5m pace</dt>
              <dd>{money(data.spikeReview.liveRate)} / hr</dd>
            </div>
          </dl>
          {data.spikeReview.demand && (
            <>
              <p>{data.spikeReview.demand.reason}</p>
              <dl>
                <div>
                  <dt>Load / warm · latest 2m median</dt>
                  <dd>{num(data.spikeReview.demand.pressure, 2)}</dd>
                </div>
                <div>
                  <dt>Load / warm · at trial entry</dt>
                  <dd>{num(data.spikeReview.demand.referencePressure, 2)}</dd>
                </div>
              </dl>
              <small>
                Demand checked each scan ·{' '}
                {age(data.spikeReview.demand.sourceAt)}
              </small>
            </>
          )}
          <small>
            {data.spikeReview.ordinary ? (
              <>
                Loading and idle time count toward elapsed earnings. Missing
                evidence remains unknown. An unresolved comparison ends as
                inconclusive at its deadline; the model can keep serving while
                safe paid alternatives are checked. Review deadline:{' '}
                {new Date(data.spikeReview.deadline * 1000).toLocaleTimeString(
                  [],
                  { hour: 'numeric', minute: '2-digit' },
                )}
                .
              </>
            ) : (
              <>
                Compare by {data.policy.trialMinutes} verified warm minutes and
                credit settlement.{' '}
                {data.spikeReview.learning
                  ? 'A sample-complete baseline trial may finish sooner.'
                  : 'A fading spike with weak paid results can end after five warm minutes; missing observations never count as zero.'}{' '}
                A recovering paid burst gets up to ten more minutes. Recheck by{' '}
                {new Date(data.spikeReview.deadline * 1000).toLocaleTimeString(
                  [],
                  { hour: 'numeric', minute: '2-digit' },
                )}
                ; returns still need fresh data, demand and available memory.
              </>
            )}
          </small>
        </div>
      )}
      <div className="demand-comparison-heading">
        <div>
          <strong>
            {scanStale || !actualFresh
              ? 'Saved comparison'
              : !data.enabled
                ? 'Opportunity preview · demand mode off'
                : actualTarget
                  ? `${actual?.phase === 'switching' ? 'Switching to' : 'Confirming'} ${shortModel(actualTarget)}`
                  : actual?.phase === 'waiting'
                    ? 'Waiting to evaluate'
                    : `Keeping ${shortModel(data.currentModel ?? 'the current model')}`}
          </strong>
          <p className="small muted">
            {!actualFresh
              ? 'Wait for fresh controller readings before interpreting this as a current decision.'
              : (actual?.reason ?? data.reason)}
          </p>
        </div>
        {!!alternatives.length && (
          <label>
            Compare with
            <Choice
              value={best?.model ?? ''}
              onChange={setComparisonModel}
              label="Compare optimizer alternative"
              options={alternatives.map((row) => ({
                value: row.model,
                label: `${shortModel(row.model)}${!row.selected ? ' · outside your pool' : ''}`,
              }))}
            />
          </label>
        )}
      </div>
      <div className="demand-auto-comparison">
        <div>
          <span>
            Current · {shortModel(data.currentModel ?? 'current model')}
          </span>
          <strong>
            {money(baseline?.recentGuardRate ?? baseline?.rate)}
            <small> / hr</small>
          </strong>
          <small>
            {baseline?.recentGuardRate != null
              ? 'Recent 5m · settled paid pace'
              : 'Observed history · not a live forecast'}
          </small>
          <p>
            {baseline?.recentGuardRate != null
              ? 'Short bursts can make this rate change quickly.'
              : scope(baseline)}
          </p>
          {baseline && (
            <small>
              {baseline.recent ? 'Recent session average' : 'Historical mean'}{' '}
              {money(baseline.rate)} / hr · {scope(baseline).toLowerCase()}
            </small>
          )}
          {data.activity && (
            <small>
              {!scanStale && data.activity.fresh
                ? data.activity.idleSeconds > 0
                  ? `${num(data.activity.idleSeconds / 60, 1)} min without local work`
                  : 'Local work is active'
                : 'Waiting for fresh verified activity'}
            </small>
          )}
        </div>
        <div>
          <span>
            {best ? shortModel(best.model) : 'Alternative model'} ·{' '}
            {best?.model === actualTarget
              ? 'controller candidate'
              : 'comparison only'}
          </span>
          <strong>
            {money(best?.estimate?.rate)}
            <small> / hr</small>
          </strong>
          <small>
            {best?.estimate?.forecastUsable
              ? 'Matched paid history'
              : 'Observed history · insufficient forecast evidence'}
          </small>
          {best?.estimate && (
            <p>
              {plural(best.estimate.hours, 'observed hour', 1)} ·{' '}
              {plural(best.estimate.days, 'date')} ·{' '}
              {scope(best.estimate).toLowerCase()}
              {typeof best.estimate.asOf === 'number' &&
              Number.isFinite(best.estimate.asOf)
                ? ` · last evidence ${age(best.estimate.asOf).toLowerCase()}`
                : ''}
            </p>
          )}
          <small>
            {best
              ? (signalName[best.signal.status ?? ''] ?? 'Waiting for demand')
              : 'Waiting for enough evidence'}{' '}
            · {num(best?.signal.pressure, 2)} requests per warm provider
          </small>
          {best?.reason && (
            <p className="demand-candidate-blocker">{best.reason}</p>
          )}
          {best?.eligible && !scanStale && (
            <small>
              {best.model === data.target
                ? 'Selected for the next eligibility checks.'
                : 'Eligible comparison; the controller may prioritize another opportunity.'}
            </small>
          )}
        </div>
      </div>
      {data.fallback && (
        <details className="demand-fallback-readout">
          <summary>
            <strong>Last resort · {shortModel(data.fallback.model)}</strong>
            <span
              className={
                data.fallback.qualified && !scanStale ? 'good-text' : 'muted'
              }
            >
              {!data.fallback.enabled
                ? 'Off'
                : scanStale
                  ? 'Waiting for fresh data'
                  : data.fallback.selection === 'fallback'
                    ? 'Last resort available'
                    : data.fallback.current && data.fallback.qualified
                      ? 'Current selection'
                      : data.fallback.qualified
                        ? data.fallback.holdReason
                          ? 'On hold'
                          : 'Ready when needed'
                        : 'Checking reliability'}
            </span>
          </summary>
          <p>
            {scanStale
              ? 'Waiting for fresh demand and local-work readings before using fallback preference.'
              : (data.fallback.holdReason ?? data.fallback.reason)}
          </p>
          {data.fallback.warmMinutes > 0 && (
            <small>
              {money(data.fallback.usdPerHour)} / hr observed · traffic in{' '}
              {data.fallback.trafficWindows} / {data.fallback.windows} windows ·{' '}
              {num(data.fallback.warmMinutes, 0)} warm min ·{' '}
              {age(data.fallback.asOf)}
            </small>
          )}
          <small>
            Used when no other trial candidate qualifies. Steady traffic alone
            does not meet the earnings target. Preference expires as recent paid
            traffic or demand fades.
          </small>
        </details>
      )}
      {best?.netGainUsd != null && best.kind !== 'explore' ? (
        <div className="demand-switch-comparison">
          <div className="demand-auto-economics">
            <div>
              <span>
                Switching advantage · next {data.planningMinutes / 60}h
              </span>
              <strong className={best.netGainUsd > 0 ? 'good-text' : ''}>
                {money(best.netGainUsd)}
              </strong>
              <small>
                {best.netGainUsd < 0
                  ? 'Estimated to earn less than staying. This is not a loss from your balance.'
                  : 'Estimated extra earnings compared with staying.'}
              </small>
            </div>
            <div>
              <span>Stay · protected rate</span>
              <strong>
                {money(baseline?.upper)}
                <small> / hr</small>
              </strong>
              <small>
                Upper planning estimate, including recent paid-work protection.
              </small>
            </div>
            <div>
              <span>Switch · cautious rate</span>
              <strong>
                {money(best.estimate?.lower)}
                <small> / hr</small>
              </strong>
              <small>
                Lower planning estimate; {num(best.outboundSeconds, 0)} sec
                deducted for loading.
              </small>
            </div>
          </div>
          <details className="demand-gain-explanation">
            <summary>How this comparison works</summary>
            <p>
              It compares the alternative’s lower estimate with the current
              model’s upper estimate over {data.planningMinutes / 60}h, then
              subtracts earnings missed while loading. These cautious bounds
              differ from the average rates above. Variable or sparse paid work
              can keep this value negative even when an alternative sometimes
              has a higher live pace.
            </p>
            <p>
              To qualify, the alternative must improve the protected rate by at
              least {data.policy.improvementPercent}% and earn at least{' '}
              {money(data.policy.minimumNetUsd)} extra over the window. It also
              requires at least $0.005/hour more. These are thresholds, not
              fees.
            </p>
            <p>
              {timingScope(best)}.{' '}
              {best.paybackMinutes != null
                ? `Loading time is repaid after about ${num(best.paybackMinutes, 1)} min if the compared rates hold.`
                : 'The compared rates do not repay the loading time.'}
            </p>
          </details>
        </div>
      ) : (
        best && (
          <p className="notice">
            {best.kind === 'explore'
              ? 'This is a measurement trial candidate. Its demand is not a prediction of paid earnings.'
              : 'There is not enough comparable paid history to estimate the advantage of switching to this model.'}
          </p>
        )
      )}
      {trial && (
        <div className="demand-trial-readout">
          <div>
            <strong>
              {shortModel(trial.model)} ·{' '}
              {trialClosed
                ? 'Completed trial observations'
                : !actualFresh || actual?.phase === 'waiting'
                  ? 'Saved trial observations'
                  : (trialStatus[trial.status] ?? 'Trial result')}
            </strong>
            <span>
              {num(trial.warmSeconds / 60, 1)} / {trial.trialMinutes} warm min
            </span>
          </div>
          <progress
            aria-label="Observed warm trial time"
            max={trial.trialMinutes * 60}
            value={Math.min(trial.warmSeconds, trial.trialMinutes * 60)}
          />
          {trial.learning && (
            <>
              <p>
                <strong>
                  Baseline samples ·{' '}
                  {num(trial.learning.completeTokenSamples, 0)} /{' '}
                  {trial.learning.sampleGoal}
                </strong>
                <br />
                {trial.learning.jobMinutes} / {trial.learning.jobMinutesGoal}{' '}
                paid minutes ·{' '}
                {trial.learning.targetReached
                  ? 'Sample goal reached'
                  : 'Collecting input, output and payment together'}
              </p>
              <progress
                aria-label="Complete payment samples"
                max={trial.learning.sampleGoal}
                value={Math.min(
                  trial.learning.completeTokenSamples,
                  trial.learning.sampleGoal,
                )}
              />
              <p className="small">
                {money(trial.learning.usdPerRequest)} / paid request ·{' '}
                {money(trial.learning.usdPerMillionTokens)} / million total
                tokens. This trial’s measured workload mix; no assumed future
                hourly earnings.
              </p>
            </>
          )}
          <dl>
            <div>
              <dt>First traffic</dt>
              <dd>
                {trial.firstTrafficSeconds != null
                  ? `${num(trial.firstTrafficSeconds / 60, 1)} warm min`
                  : 'Not observed'}
              </dd>
            </div>
            <div>
              <dt>Local requests / tokens</dt>
              <dd>
                {num(trial.requests, 0)} / {num(trial.tokens, 0)}
              </dd>
            </div>
            <div>
              <dt>Observed idle share</dt>
              <dd>{num(trial.idlePercent, 0)}%</dd>
            </div>
            <div>
              <dt>Settled inference earnings</dt>
              <dd>{money(trial.usd)}</dd>
            </div>
            <div>
              <dt>Observed warm paid pace</dt>
              <dd>{money(trial.usdPerHour)} / hr</dd>
            </div>
          </dl>
          <TrialEconomics trial={trial} />
          <small>
            Warm paid pace uses {num(trial.paidWarmSeconds / 60, 1)} complete,
            settled warm minutes. Switching overhead is tracked separately.
          </small>
          {trial.partial && (
            <small className="notice">
              This trial finished with partial credit coverage. Known earnings
              are retained; missing time is not treated as unpaid. Late credits
              can update the result.
            </small>
          )}
          {!trialClosed &&
            trial.status === 'settling' &&
            trial.settlementDeadline != null && (
              <small>
                Closes with partial coverage by{' '}
                {new Date(trial.settlementDeadline * 1000).toLocaleTimeString(
                  [],
                  { hour: 'numeric', minute: '2-digit' },
                )}{' '}
                if the remaining readings do not arrive.
              </small>
            )}
        </div>
      )}

      <div className="demand-auto-limits small muted">
        <span>
          {data.limits.switchesUsed} / {data.limits.switchLimit} attempts in 24h
        </span>
        <span>
          {num(data.limits.downtimeMinutesUsed, 1)} /{' '}
          {data.limits.downtimeMinutesLimit} min downtime used or reserved
        </span>
      </div>
      {data.limits.sampling && (
        <p className="small muted" role="status">
          Learning time: {num(data.limits.sampling.minutesUsed, 0)} of{' '}
          {data.limits.sampling.minutesLimit} min used in the last 24 h
          {data.limits.sampling.reservationMinutes > 0
            ? ` · each run needs ${data.limits.sampling.reservationMinutes} min free`
            : ''}
          .{' '}
          {!data.limits.sampling.qualified
            ? data.limits.sampling.availableAt
              ? `New learning runs wait until about ${new Date(data.limits.sampling.availableAt * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}. `
              : 'New learning runs are on hold. '
            : ''}
          Returns and independently supported paid upgrades remain eligible.
          {data.limits.sampling.unknownLifecycles > 0
            ? ` ${data.limits.sampling.unknownLifecycles} trial(s) with incomplete timing retain their reserved allowance.`
            : ''}
        </p>
      )}
      <details className="demand-opportunities">
        <summary>What limits a switch?</summary>
        {best && (
          <p>
            Next load: about{' '}
            <strong>{num(best.outboundSeconds, 0)} seconds</strong>.{' '}
            {timingScope(best)}.
          </p>
        )}
        <p>
          <strong>Demand:</strong> ten minutes of sustained load per warm
          provider can qualify a trial during poor earnings or quiet periods.
          Ordinary demand cannot, by itself, replace productive paid work;
          exceptional spikes can qualify for bounded learning trials. Raw
          request counts are concurrent work across the network, not this Mac’s
          arriving jobs or earnings.
        </p>
        <p>
          <strong>Memory:</strong> space after unloading must cover model
          weights, the provider’s OS and inference reserves, plus{' '}
          {data.policy.memoryHeadroomGB} GB of extra Bloomkeeper margin.
        </p>
        <p>
          <strong>Bloomkeeper retry waits:</strong> {data.policy.trialCooldownMinutes}{' '}
          minutes after an unpaid trial; 15 minutes after a load failure.
          Successful loads have no added retry cooldown. The normal observation
          period is {data.policy.minRunMinutes} minutes, and earnings
          comparisons confirm for {data.policy.confirmationMinutes} minutes.
        </p>
        <p>
          <strong>Daily limits:</strong> {data.limits.switchLimit} automatic
          attempts and {data.limits.downtimeMinutesLimit} minutes of downtime
          per rolling 24 hours. A possible return is reserved for recovery, not
          charged as a certain earnings loss.
        </p>
        <p>
          Qualified automatic switches do not wait for idle time. Darkbloom
          0.9.9 and later finish accepted requests first; older versions may
          interrupt them. Fresh identity/readiness checks, AC power, memory and
          safe temperatures still apply. A failed load pauses automation;
          recovery keeps its existing idle check.
        </p>
      </details>
      <p className="footnote">
        Load / warm is active + queued network requests per warm provider. It
        ranks demand trials, not predicted dollars. A short zero-earning sample
        does not exclude a model. Trials measure this Mac’s actual paid work;
        repeated comparable results can refine later choices.
      </p>
      {!data.enabled && (
        <p className="footnote">
          Preview scans available solo models. Your selected models and controls
          take effect when you enable demand following below.
        </p>
      )}
      <details className="demand-opportunities">
        <summary>
          Why each model is held · {data.opportunities.length} checks
        </summary>
        <div className="demand-opportunity-grid">
          {data.opportunities.map((row) => (
            <article key={row.model}>
              <div>
                <strong>{shortModel(row.model)}</strong>
                <span>
                  {row.current
                    ? 'Current selection'
                    : row.eligible && !scanStale
                      ? row.kind === 'explore'
                        ? 'Trial candidate'
                        : 'Clears gain checks'
                      : 'Holding'}
                </span>
              </div>
              <p>
                {signalName[row.signal.status ?? ''] ?? 'Waiting for demand'}
                {row.signal.pressureRatio != null
                  ? ` · ${num(row.signal.pressureRatio, 1)}× pressure`
                  : ''}
              </p>
              <dl>
                <div>
                  <dt>Load / warm</dt>
                  <dd>{num(row.signal.pressure, 2)}</dd>
                </div>
                <div>
                  <dt>Observed historical pace</dt>
                  <dd>{money(row.estimate?.rate)} / hr</dd>
                </div>
                <div>
                  <dt>Earnings forecast</dt>
                  <dd>
                    {row.estimate?.forecastUsable && row.kind !== 'explore'
                      ? 'Matched history'
                      : 'Uncertain'}
                  </dd>
                </div>
                <div>
                  <dt>Available after unload</dt>
                  <dd>{num(row.loadBudget?.afterUnloadGB, 1)} GB</dd>
                </div>
                {!!row.loadBudget?.reclaimableGB && (
                  <div>
                    <dt>File cache cleared before switching</dt>
                    <dd>up to {num(row.loadBudget.reclaimableGB, 1)} GB</dd>
                  </div>
                )}
                <div>
                  <dt>Required + extra margin</dt>
                  <dd>
                    {row.loadBudget
                      ? num(row.loadBudget.requiredGB + row.extraMemoryGB, 1)
                      : '—'}{' '}
                    GB
                  </dd>
                </div>
              </dl>
              <p>
                {row.reason ??
                  'Requires sustained confirmation and fresh model/memory checks before switching; active work does not delay it.'}
              </p>
              {row.trialCooldownUntil != null && (
                <p>
                  Retry after{' '}
                  {new Date(row.trialCooldownUntil * 1000).toLocaleTimeString(
                    [],
                    { hour: 'numeric', minute: '2-digit' },
                  )}
                </p>
              )}
              {row.failureCooldownUntil != null && (
                <p>
                  Load-failure retry after{' '}
                  {new Date(row.failureCooldownUntil * 1000).toLocaleTimeString(
                    [],
                    { hour: 'numeric', minute: '2-digit' },
                  )}
                </p>
              )}
              {row.estimate && (
                <small>
                  {num(row.estimate.hours, 1)} matched hours ·{' '}
                  {scope(row.estimate)}
                  {row.estimate.demandMatched
                    ? ' · Similar observed demand'
                    : ''}
                </small>
              )}
              {row.conditional && (
                <p className="small">
                  {num(row.conditional.hours, 1)}h at comparable load / warm,
                  active requests and provider count · {row.conditional.days}{' '}
                  dates.{' '}
                  {row.conditional.usable
                    ? `${num(row.historyWeight ?? 1, 2)}× trial history weight.`
                    : 'Still learning · no history penalty.'}
                </p>
              )}
            </article>
          ))}
        </div>
      </details>
      {data.runs.length > 0 && (
        <details className="demand-attempts">
          <summary>Recent automatic attempts · {data.runs.length}</summary>
          {data.runs.map((run) => (
            <article key={run.id}>
              <strong>
                {shortModel(run.model)} · {run.result}
              </strong>
              <p>
                {new Date(run.at * 1000).toLocaleString()} ·{' '}
                {run.downtime != null
                  ? `${num(run.downtime, 0)}s measured downtime`
                  : 'Downtime not yet verified'}
              </p>
              <small>
                {run.decision.kind === 'explore'
                  ? `Demand trial · ${run.decision.trialResolution?.status === 'inconclusive' ? 'Ended inconclusively' : run.decision.trialResolution?.status === 'keep' ? 'Review complete · kept model' : run.decision.outcome ? (trialStatus[run.decision.outcome.status] ?? 'Result pending') : 'Waiting for verified warm observations'}`
                  : run.decision.candidate &&
                      run.decision.planningMinutes != null
                    ? `Saved estimate: ${money(run.decision.candidate.netGainUsd)} net over ${run.decision.planningMinutes / 60}h.`
                    : run.decision.kind === 'recovery'
                      ? 'Stall recovery restart'
                      : 'No saved estimate'}
              </small>
              {run.decision.reason && <p>{run.decision.reason}</p>}
              {run.decision.outcome && (
                <TrialEconomics trial={run.decision.outcome} />
              )}
              {run.decision.outcome && (
                <p>
                  {num(run.decision.outcome.warmSeconds / 60, 1)} warm min ·{' '}
                  {money(run.decision.outcome.usd)} paid ·{' '}
                  {num(run.decision.outcome.idlePercent, 0)}% idle · first
                  traffic{' '}
                  {run.decision.outcome.firstTrafficSeconds != null
                    ? `${num(run.decision.outcome.firstTrafficSeconds / 60, 1)} warm min`
                    : 'not observed'}
                </p>
              )}
            </article>
          ))}
        </details>
      )}
    </section>
  );
}

/** A typed value with its range. Commits on blur or Enter so partial typing isn't snapped. */
export function NumberSetting({
  id,
  label,
  unit,
  hint,
  value,
  range,
  disabled,
  onChange,
}: {
  id: string;
  label: string;
  unit: string;
  hint: string;
  value: number;
  range: { min: number; max: number; step: number };
  disabled: boolean;
  onChange: (next: number) => void;
}) {
  const [draft, setDraft] = useState<string | null>(null);
  const commit = () => {
    if (draft === null) return;
    const next = Number(draft);
    setDraft(null);
    if (draft.trim() !== '' && Number.isFinite(next) && next !== value)
      onChange(next);
  };
  const show = (v: number) => String(Number(v.toFixed(3)));
  return (
    <div className="tune-setting">
      <label htmlFor={id}>{label}</label>
      <div className="tune-input">
        <input
          id={id}
          type="number"
          inputMode="decimal"
          min={range.min}
          max={range.max}
          step={range.step}
          disabled={disabled}
          value={draft ?? show(value)}
          onChange={(event) => setDraft(event.target.value)}
          onBlur={commit}
          onKeyDown={(event) => {
            if (event.key === 'Enter') commit();
            if (event.key === 'Escape') setDraft(null);
          }}
        />
        <span>{unit}</span>
      </div>
      <small>
        {hint} {show(range.min)}–{show(range.max)} {unit}.
      </small>
    </div>
  );
}
