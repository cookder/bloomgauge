export type RunSession = {
  id: number;
  status: string;
  models: string[];
  startedAt: number;
};
export type MultiModelReporting = {
  at: number;
  sessionId: number | null;
  models: string[];
  managedBy: 'darkbloom';
  counting: boolean;
  detail: string;
  automationSupported: false;
  /** Offered models no longer downloaded here (`darkbloom models remove`): Darkbloom
   * keeps offering them until it restarts. Empty when none or not yet known. */
  offeredNotDownloaded?: string[];
};
export type PulseRunStatus = {
  label: string;
  tone: 'trial' | 'normal' | 'notice';
  detail: string;
  progress?: string;
  reason?: string;
  alternative?: { model: string; eligible: boolean; reason: string };
};
const obj = (v: unknown): Record<string, unknown> =>
  v && typeof v === 'object' && !Array.isArray(v)
    ? (v as Record<string, unknown>)
    : {};
const str = (v: unknown) => (typeof v === 'string' ? v : '');
const finite = (v: unknown): v is number =>
  typeof v === 'number' && Number.isFinite(v);
const sameModels = (a: unknown, b: string[]) =>
  Array.isArray(a) &&
  a.length === b.length &&
  new Set(a).size === a.length &&
  a.every((v) => typeof v === 'string' && b.includes(v));

export function currentPaidAlternative(
  value: unknown,
  now: number,
): PulseRunStatus['alternative'] {
  const a = obj(value);
  if (
    !str(a.model) ||
    typeof a.eligible !== 'boolean' ||
    !str(a.reason) ||
    !finite(a.at) ||
    now - a.at > 45 ||
    now - a.at < -5
  )
    return undefined;
  return { model: str(a.model), eligible: a.eligible, reason: str(a.reason) };
}

/** Describe the executed run, never an unexecuted opportunity or old trial. */
export function pulseRunStatus(
  value: unknown,
  session: RunSession | null | undefined,
  now: number,
  reporting?: unknown,
): PulseRunStatus {
  const d = obj(value);
  const unknown: PulseRunStatus = {
    label: 'Run status unavailable',
    tone: 'notice',
    detail:
      'Waiting for a fresh optimizer reading. Earnings collection continues independently.',
  };
  // Reporting remains usable without an optimizer plan, subscription or API
  // response. Its proof must belong to the exact fresh collection session.
  const report = obj(reporting ?? d.reporting);
  if (
    session?.status === 'active' &&
    session.models.length > 2 &&
    finite(report.at) &&
    now - report.at < 15 &&
    now - report.at > -5 &&
    report.at >= session.startedAt &&
    report.sessionId === session.id &&
    sameModels(report.models, session.models) &&
    new Set(session.models).size === session.models.length &&
    report.managedBy === 'darkbloom' &&
    report.automationSupported === false &&
    typeof report.counting === 'boolean' &&
    typeof report.detail === 'string'
  ) {
    return {
      label: `${session.models.length}-model monitoring${report.counting ? '' : ' paused'}`,
      tone: report.counting ? 'normal' : 'notice',
      progress: 'Managed by Darkbloom',
      detail: report.detail,
      reason:
        'Darkbloom manages this model set; Bloomkeeper reports on it and never changes it. The Manager runs one model, or a pair without Gemma. To use it, pick one model in Model controls, then turn the Manager on.',
    };
  }
  if (!finite(d.at) || now - d.at > 45 || now - d.at < -5) return unknown;
  const detail = str(d.detail);
  if (d.busy === true || str(d.requestedModel)) {
    const recovery = /recovery|restore/.test(str(d.requestedKind));
    return {
      label: recovery
        ? 'Restoring model'
        : d.busy
          ? 'Switching model'
          : 'Switch queued',
      tone: 'notice',
      detail,
    };
  }
  if (!session || session.status !== 'active')
    return {
      label: 'No active run',
      tone: 'notice',
      detail: 'The provider session has ended or is not yet available.',
    };
  if (
    !sameModels(d.currentModels, session.models) ||
    d.at < session.startedAt
  ) {
    return {
      label: 'Updating run status',
      tone: 'notice',
      detail: 'Matching the optimizer to this provider session.',
    };
  }
  const warm = obj(d.warmup);
  if (warm.status && warm.status !== 'ready')
    return {
      label: 'Model warming / not ready',
      tone: 'notice',
      detail: str(warm.detail) || detail,
    };
  if (d.mode === 'week' || d.mode === 'combo')
    return {
      label: d.mode === 'combo' ? 'Model-pair test' : 'Scheduled model test',
      tone: 'trial',
      detail,
      progress:
        finite(d.nextSwitchAt) && d.nextSwitchAt > now
          ? 'Scheduled rotation'
          : 'Collecting comparison data',
    };
  const auto = obj(d.demandAuto),
    trial = obj(auto.trial);
  const matches =
    d.mode === 'demand' &&
    auto.enabled === true &&
    trial.current === true &&
    trial.model === d.currentModel;
  if (matches) {
    const run = (Array.isArray(auto.runs) ? auto.runs : [])
      .map(obj)
      .find((r) => r.id === trial.runId && r.model === trial.model);
    const decision = obj(run?.decision),
      review = obj(auto.spikeReview);
    const resolution = obj(decision.trialResolution);
    const alternative = currentPaidAlternative(auto.paidAlternative, now);
    const entryReason = str(decision.reason)
      ? `Why this trial started: ${str(decision.reason)}`
      : undefined;
    if (resolution.status === 'inconclusive')
      return {
        label: 'Trial ended',
        tone: 'notice',
        progress: 'Inconclusive',
        detail:
          str(resolution.reason) ||
          'The comparison ended without enough evidence to establish an improvement. This model keeps serving while safe paid alternatives are checked.',
        reason: entryReason,
        alternative,
      };
    if (resolution.status === 'keep')
      return {
        label: 'Normal run',
        tone: 'normal',
        progress: 'Trial finished',
        detail:
          str(resolution.reason) ||
          'The trial review is complete. Keeping this model while checking ordinary paid opportunities.',
        reason: entryReason,
        alternative,
      };
    const reviewing =
      review.runId === trial.runId &&
      ['measuring', 'settling', 'waiting', 'recovering', 'return'].includes(
        str(review.status),
      );
    const measuring = trial.status === 'running',
      settling = trial.status === 'settling';
    if (measuring || settling || reviewing) {
      const name =
        decision.explorationTrigger === 'baseline_learning' ||
        decision.learningTrial
          ? 'Baseline trial'
          : decision.explorationTrigger === 'demand_spike' ||
              decision.spikeTrial
            ? 'Demand-spike trial'
            : 'Model trial';
      const progress =
        reviewing && review.status === 'waiting'
          ? 'Review on hold'
          : review.status === 'return'
            ? 'Evaluating return'
            : settling
              ? 'Settling credits'
              : measuring &&
                  finite(trial.warmSeconds) &&
                  finite(trial.trialMinutes)
                ? `${(trial.warmSeconds / 60).toFixed(1)} / ${trial.trialMinutes} warm min`
                : 'Comparing paid results';
      return {
        label: name,
        tone: 'trial',
        progress,
        detail: reviewing
          ? str(review.reason)
          : settling
            ? 'Measurement finished; waiting for confirmed credits to settle.'
            : 'Measuring this Mac’s paid work. Only verified warm time counts toward the trial.',
        reason: entryReason,
        alternative,
      };
    }
    if (
      [
        'productive',
        'no_traffic',
        'no_paid_work',
        'insufficient_coverage',
      ].includes(str(trial.status))
    ) {
      return {
        label: 'Normal run',
        tone: 'normal',
        progress: 'Trial finished',
        detail:
          str(obj(decision.trialResolution).reason) ||
          str(obj(decision.spikeResolution).reason) ||
          'Trial measurement is complete. The optimizer is now evaluating ordinary opportunities.',
        reason: entryReason,
        alternative,
      };
    }
  }
  if (d.mode === 'observe')
    return {
      // Neutral for both strategies: legacy calls this Manual, the manager Off.
      label: 'Automatic control off',
      tone: 'notice',
      detail:
        'Bloomkeeper isn’t choosing models. This model keeps serving while Bloomkeeper records passive history.',
    };
  return {
    label: 'Normal run',
    tone: 'normal',
    detail:
      detail ||
      'Serving the selected model and passively recording its results.',
  };
}
