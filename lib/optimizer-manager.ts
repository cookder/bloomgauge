// The optimizer's "manager" strategy as the UI reads it: GET /api/optimizer
// `demandAuto.manager` (native/manager.py view()). Every field is optional and
// read defensively: a malformed part is dropped, never the whole page, so a newer
// or older backend can add fields (evidence, arming, ledger) without breaking it.
import type { sharedGet } from './shared-get';

const record = (v: unknown): v is Record<string, unknown> =>
  !!v && typeof v === 'object' && !Array.isArray(v);
const finite = (v: unknown): v is number =>
  typeof v === 'number' && Number.isFinite(v);
const text = (v: unknown): v is string => typeof v === 'string' && !!v.trim();
const num = (v: unknown): number | null => (finite(v) ? v : null);
const str = (v: unknown): string | null => (text(v) ? v : null);
const bool = (v: unknown): boolean | null =>
  typeof v === 'boolean' ? v : null;

export type ManagerHome = {
  model: string;
  /** manual | external (pins), history | current (chosen by the manager). */
  source: string | null;
  at: number | null;
  usdPerHour: number | null;
  hours: number | null;
  /** Days with ≥ 1 ready hour behind usdPerHour (history homes). */
  days: number | null;
  failures: number | null;
  failedAt: number | null;
};
export type ManagerExcursion = {
  target: string;
  from: string | null;
  startedAt: number | null;
  predictedUsdPerHour: number | null;
  /** Realized $/h while away, when the backend reports it. */
  realizedUsdPerHour: number | null;
  reason: string | null;
  maxMinutes: number | null;
  endReason: string | null;
  endedAt: number | null;
};
export type ManagerRecovery = {
  failedTarget: string | null;
  previous: string | null;
  at: number | null;
  attempts: number | null;
  interrupted: boolean;
};
export type ManagerWatchdog = {
  darkSince: number | null;
  windowSeconds: number | null;
  attempts: number | null;
  nextAt: number | null;
  reason: string | null;
};
export type EvidenceRow = {
  model: string;
  usdPerHour: number | null;
  low: number | null;
  high: number | null;
  ratio: number | null;
  ratioLow: number | null;
  providers: number | null;
  source: string | null;
  eligible: boolean | null;
  why: string | null;
};
export type ManagerEvidence = {
  cell: string | null;
  updatedAt: number | null;
  home: string | null;
  homeUsdPerHour: number | null;
  rows: EvidenceRow[];
};
export type ManagerArming = {
  model: string;
  since: number | null;
  checks: number | null;
  needed: number | null;
  ratio: number | null;
  /** Seconds between checks (3600: hourly). */
  checkSeconds?: number | null;
  /** Seconds from the first passing check to the earliest move. */
  neededSeconds?: number | null;
};
export type ManagerLedger = {
  days: number | null;
  count: number | null;
  gainUsd: number | null;
  predictedUsd: number | null;
  enabled: boolean | null;
  disabledReason: string | null;
};
export type ManagerView = {
  strategy: 'manager';
  active: boolean;
  action: string | null;
  reason: string | null;
  home: ManagerHome | null;
  pinned: boolean;
  proposal: {
    target: string;
    reason: string | null;
    predictedUsdPerHour: number | null;
  } | null;
  excursion: ManagerExcursion | null;
  lastExcursion: ManagerExcursion | null;
  recovery: ManagerRecovery | null;
  watchdog: ManagerWatchdog | null;
  retryAt: number | null;
  blocked: { model: string; until: number }[];
  lastGood: { model: string; at: number | null } | null;
  lastAction: {
    action: string | null;
    model: string | null;
    at: number | null;
    success: boolean | null;
  } | null;
  evidence: ManagerEvidence | null;
  arming: ManagerArming | null;
  ledger: ManagerLedger | null;
};

function home(v: unknown): ManagerHome | null {
  if (!record(v) || !text(v.model)) return null;
  return {
    model: v.model,
    source: str(v.source),
    at: num(v.at),
    usdPerHour: num(v.usdPerHour),
    hours: num(v.hours),
    days: num(v.days),
    failures: num(v.failures),
    failedAt: num(v.failedAt),
  };
}
function excursion(v: unknown): ManagerExcursion | null {
  if (!record(v) || !text(v.target)) return null;
  return {
    target: v.target,
    from: str(v.from),
    startedAt: num(v.startedAt),
    predictedUsdPerHour: num(v.predictedUsdPerHour),
    realizedUsdPerHour: num(v.realizedUsdPerHour),
    reason: str(v.reason),
    maxMinutes: num(v.maxMinutes),
    endReason: str(v.endReason),
    endedAt: num(v.endedAt),
  };
}
function evidenceRow(v: unknown): EvidenceRow | null {
  if (!record(v) || !text(v.model)) return null;
  return {
    model: v.model,
    usdPerHour: num(v.usdPerHour),
    low: num(v.low),
    high: num(v.high),
    ratio: num(v.ratio),
    ratioLow: num(v.ratioLow),
    providers: num(v.providers),
    source: str(v.source),
    eligible: bool(v.eligible),
    why: str(v.why),
  };
}
function evidence(v: unknown): ManagerEvidence | null {
  if (!record(v) || !Array.isArray(v.rows)) return null;
  const rows = v.rows
    .slice(0, 64)
    .map(evidenceRow)
    .filter((r): r is EvidenceRow => !!r);
  return {
    cell: str(v.cell),
    updatedAt: num(v.updatedAt),
    home: str(v.home),
    homeUsdPerHour: num(v.homeUsdPerHour),
    rows,
  };
}

/** The manager view, or null when absent (legacy strategy or an older backend). */
export function readManager(value: unknown): ManagerView | null {
  if (!record(value) || value.strategy !== 'manager') return null;
  const recovery = value.recovery;
  const watchdog = value.watchdog;
  const proposal = value.proposal;
  const good = value.lastGood;
  const last = value.lastAction;
  const arming = value.arming;
  const ledger = value.ledger;
  return {
    strategy: 'manager',
    active: value.active === true,
    action: str(value.action),
    reason: str(value.reason),
    home: home(value.home),
    pinned: value.pinned === true,
    proposal:
      record(proposal) && text(proposal.target)
        ? {
            target: proposal.target,
            reason: str(proposal.reason),
            predictedUsdPerHour: num(proposal.predictedUsdPerHour),
          }
        : null,
    excursion: excursion(value.excursion),
    lastExcursion: excursion(value.lastExcursion),
    recovery: record(recovery)
      ? {
          failedTarget: str(recovery.failedTarget),
          previous: str(recovery.previous),
          at: num(recovery.at),
          attempts: num(recovery.attempts),
          interrupted: recovery.interrupted === true,
        }
      : null,
    watchdog: record(watchdog)
      ? {
          darkSince: num(watchdog.darkSince),
          windowSeconds: num(watchdog.windowSeconds),
          attempts: num(watchdog.attempts),
          nextAt: num(watchdog.nextAt),
          reason: str(watchdog.reason),
        }
      : null,
    retryAt: num(value.retryAt),
    blocked: (Array.isArray(value.blocked) ? value.blocked : [])
      .filter(
        (b): b is { model: string; until: number } =>
          record(b) && text(b.model) && finite(b.until),
      )
      .map((b) => ({ model: b.model, until: b.until })),
    lastGood:
      record(good) && text(good.model)
        ? { model: good.model, at: num(good.at) }
        : null,
    lastAction: record(last)
      ? {
          action: str(last.action),
          model: str(last.model),
          at: num(last.at),
          success: bool(last.success),
        }
      : null,
    evidence: evidence(value.evidence),
    arming:
      record(arming) && text(arming.model)
        ? {
            model: arming.model,
            since: num(arming.since),
            checks: num(arming.checks),
            needed: num(arming.needed),
            ratio: num(arming.ratio),
            checkSeconds: num(arming.checkSeconds),
            neededSeconds: num(arming.neededSeconds),
          }
        : null,
    ledger: record(ledger)
      ? {
          days: num(ledger.days),
          count: num(ledger.count),
          gainUsd: num(ledger.gainUsd),
          predictedUsd: num(ledger.predictedUsd),
          enabled: bool(ledger.enabled),
          disabledReason: str(ledger.disabledReason),
        }
      : null,
  };
}

/**
 * The compact manager status on GET /api/optimizer/control (polled every 3 s;
 * native/manager.py control_summary). Null under the legacy strategy.
 */
export type ManagerSummary = {
  at: number | null;
  active: boolean;
  home: string | null;
  pinned: boolean;
  action: string | null;
  reason: string | null;
  watchdog: { darkSince: number | null; reason: string | null } | null;
  recovery: {
    failedTarget: string | null;
    previous: string | null;
    at: number | null;
    attempts: number | null;
  } | null;
  excursion: {
    target: string;
    startedAt: number | null;
    predictedUsdPerHour: number | null;
    realizedUsdPerHour: number | null;
    endReason: string | null;
  } | null;
  arming: {
    model: string;
    since: number | null;
    checks: number | null;
    needed: number | null;
    checkSeconds: number | null;
    neededSeconds: number | null;
  } | null;
};

export function readManagerSummary(value: unknown): ManagerSummary | null {
  if (!record(value)) return null;
  const { watchdog, recovery, excursion: ex, arming } = value;
  return {
    at: num(value.at),
    active: value.active === true,
    home: str(value.home),
    pinned: value.pinned === true,
    action: str(value.action),
    reason: str(value.reason),
    watchdog: record(watchdog)
      ? { darkSince: num(watchdog.darkSince), reason: str(watchdog.reason) }
      : null,
    recovery: record(recovery)
      ? {
          failedTarget: str(recovery.failedTarget),
          previous: str(recovery.previous),
          at: num(recovery.at),
          attempts: num(recovery.attempts),
        }
      : null,
    excursion:
      record(ex) && text(ex.target)
        ? {
            target: ex.target,
            startedAt: num(ex.startedAt),
            predictedUsdPerHour: num(ex.predictedUsdPerHour),
            realizedUsdPerHour: num(ex.realizedUsdPerHour),
            endReason: str(ex.endReason),
          }
        : null,
    arming:
      record(arming) && text(arming.model)
        ? {
            model: arming.model,
            since: num(arming.since),
            checks: num(arming.checks),
            needed: num(arming.needed),
            checkSeconds: num(arming.checkSeconds),
            neededSeconds: num(arming.neededSeconds),
          }
        : null,
  };
}

/**
 * The 30-second manager view with the 3-second summary's fresher facts laid over it:
 * on/off, pin, home model, what it is doing and why, watchdog, recovery, excursion
 * and arming. Evidence, ledger and the rest stay from the full view.
 */
export function withSummary(
  view: ManagerView | null,
  summary: ManagerSummary | null,
): ManagerView | null {
  if (!view || !summary) return view;
  const homeChanged = summary.home !== (view.home?.model ?? null);
  const home = !summary.home
    ? null
    : homeChanged
      ? {
          model: summary.home,
          source: summary.pinned ? 'manual' : null,
          at: null,
          usdPerHour: null,
          hours: null,
          days: null,
          failures: null,
          failedAt: null,
        }
      : view.home;
  const ex = summary.excursion;
  const armed = summary.arming;
  return {
    ...view,
    active: summary.active,
    pinned: summary.pinned,
    home,
    action: summary.action ?? view.action,
    reason: summary.reason ?? view.reason,
    watchdog: summary.watchdog
      ? {
          darkSince: summary.watchdog.darkSince,
          reason: summary.watchdog.reason,
          windowSeconds: view.watchdog?.windowSeconds ?? null,
          attempts: view.watchdog?.attempts ?? null,
          nextAt: view.watchdog?.nextAt ?? null,
        }
      : view.watchdog && { ...view.watchdog, darkSince: null },
    recovery: summary.recovery
      ? { ...summary.recovery, interrupted: view.recovery?.interrupted ?? false }
      : null,
    excursion: ex
      ? {
          ...(view.excursion?.target === ex.target
            ? view.excursion
            : {
                from: null,
                reason: null,
                maxMinutes: null,
                endedAt: null,
              }),
          target: ex.target,
          startedAt: ex.startedAt,
          predictedUsdPerHour: ex.predictedUsdPerHour,
          realizedUsdPerHour: ex.realizedUsdPerHour,
          endReason: ex.endReason,
        }
      : null,
    // No arming in a summary without a fresh decision: keep the view's.
    arming:
      summary.at == null
        ? view.arming
        : armed
          ? {
              ...armed,
              ratio:
                view.arming?.model === armed.model ? view.arming.ratio : null,
            }
          : null,
  };
}

export type Strategy = 'manager' | 'legacy';

/**
 * Which optimizer this Mac runs. The control status names it (`strategy`, the
 * backend's own answer, read every 3 s). Else the manager view exists only under the
 * manager strategy; otherwise the saved policy decides (managerStrategy 1 or 0, where an
 * absent key in a full saved policy means a backend that predates the manager).
 * The control status carries the raw saved policy, whose missing key defaults to 1.
 * Null while unknown: the On card waits rather than guess.
 */
export function optimizerStrategy({
  controlStrategy,
  manager,
  savedPolicy,
  controlPolicy,
}: {
  controlStrategy?: unknown;
  manager?: unknown;
  savedPolicy?: Record<string, unknown> | null;
  controlPolicy?: Record<string, unknown> | null;
}): Strategy | null {
  if (controlStrategy === 'manager' || controlStrategy === 'legacy')
    return controlStrategy;
  if (readManager(manager)) return 'manager';
  if (record(savedPolicy)) {
    if (savedPolicy.managerStrategy === 1) return 'manager';
    return 'legacy';
  }
  if (record(controlPolicy)) {
    if (controlPolicy.managerStrategy === 0) return 'legacy';
    if (controlPolicy.managerStrategy === 1) return 'manager';
  }
  return null;
}

/**
 * The strategy from the light control status, for cards that don't read /api/optimizer.
 * Shared with other readers for 20 s; null while unknown or unreadable.
 */
export async function loadStrategy(
  get: typeof sharedGet,
  signal?: AbortSignal,
): Promise<Strategy | null> {
  try {
    const value = await get('/api/optimizer/control', 20000, signal, (r) =>
      r.ok ? r.json() : Promise.resolve(null),
    );
    return record(value)
      ? optimizerStrategy({
          controlStrategy: value.strategy,
          controlPolicy: record(value.demandPolicy) ? value.demandPolicy : null,
        })
      : null;
  } catch {
    return null;
  }
}

/**
 * The fewest models a plan may be saved with. The backend checks the strategy being
 * saved (the saved policy plus these edits, optimizer.py save), so an edited
 * managerStrategy decides; otherwise the current strategy does. The manager can hold
 * one model; legacy demand following compares two or more.
 */
export function planMinimum(
  strategy: Strategy | null,
  rules: { managerStrategy?: unknown },
): number {
  const edited =
    rules.managerStrategy === 1
      ? 'manager'
      : rules.managerStrategy === 0
        ? 'legacy'
        : strategy;
  return edited === 'manager' ? 1 : 2;
}

/** The manager starts at most this many excursions in any 24 h (excursions.py MAX_PER_DAY). */
export const EXCURSIONS_PER_DAY = 3;

/**
 * Under the manager only excursion starts count toward the daily move limit
 * (demand_optimizer.py excursion_limit): returns home, restores and older runs don't.
 * `limit` is the lower of "Maximum automatic moves" and the manager's own daily cap.
 */
export function excursionsInDay(
  runs: unknown,
  switchLimit: number,
  now: number,
): { used: number; limit: number } {
  const used = Array.isArray(runs)
    ? runs.filter(
        (run) =>
          record(run) &&
          finite(run.at) &&
          run.at > now - 86400 &&
          run.at <= now &&
          record(run.decision) &&
          run.decision.kind === 'excursion',
      ).length
    : 0;
  return { used, limit: Math.min(switchLimit, EXCURSIONS_PER_DAY) };
}

/** Evidence-armed excursions: true/false, or null when this backend has no such setting yet. */
export function excursionsSetting(
  policy?: Record<string, unknown> | null,
): boolean | null {
  if (!record(policy) || !finite(policy.managerExcursions)) return null;
  return policy.managerExcursions === 1;
}

/**
 * Drop "go to Optimizer → Overview" directions when the reader is already there.
 * "…on the Mac" stays: it tells a phone which device to use.
 */
export function withoutCircularHint(value: string | null | undefined): string {
  if (!value) return '';
  // Sentence ends: punctuation followed by whitespace ("0.9.9" stays whole).
  const sentences: string[] = [];
  const end = /[.!?]+\s+/g;
  let start = 0;
  for (let m = end.exec(value); m; m = end.exec(value)) {
    sentences.push(value.slice(start, m.index + m[0].length));
    start = m.index + m[0].length;
  }
  sentences.push(value.slice(start));
  return sentences
    .filter(
      (s) =>
        !/^\s*(open|see|check|go to)\s+optimizer\s*→\s*overview\b/i.test(s) ||
        /on the mac/i.test(s),
    )
    .map((s) =>
      s.replace(
        /\s+(?:in|on|from|under)\s+optimizer\s*→\s*overview(?!\s+on the mac)/gi,
        '',
      ),
    )
    .join('')
    .trim();
}

/** "Statistics paused · provider is not ready to serve." → "provider is not ready to serve". */
export function pausedReason(detail: string | null | undefined): string | null {
  if (!text(detail)) return null;
  const reason = detail
    .replace(/^\s*statistics paused\s*[·:-]?\s*/i, '')
    .trim()
    .replace(/\.$/, '');
  return reason || null;
}

export const usd = (n: number | null | undefined) =>
  n == null || !Number.isFinite(n)
    ? '—'
    : Math.abs(n) >= 0.1 || n === 0
      ? `${n < 0 ? '−' : ''}$${Math.abs(n).toFixed(2)}`
      : `${n < 0 ? '−' : ''}$${Math.abs(n).toFixed(3)}`;
const signed = (n: number) => `${n >= 0 ? '+' : '−'}${usd(Math.abs(n))}`;

export function duration(seconds: number) {
  const minutes = Math.max(0, Math.round(seconds / 60));
  if (minutes < 60) return `${minutes} min`;
  const h = Math.floor(minutes / 60),
    m = minutes % 60;
  return m ? `${h} h ${m} min` : `${h} h`;
}

export type Tone = 'good' | 'working' | 'attention';
type Label = (id: string) => string;
const same = (id: string) => id;

/** A short title for what the manager is doing now. */
export function managerHeadline(
  view: ManagerView,
  current: string | null | undefined,
  label: Label = same,
  now = Date.now() / 1000,
): { title: string; tone: Tone } {
  const homeName = view.home ? label(view.home.model) : null;
  if (!view.active) return { title: 'Manager is off', tone: 'attention' };
  if (view.recovery)
    return {
      title: `Recovering after the switch to ${label(view.recovery.failedTarget ?? 'a model')} failed`,
      tone: 'working',
    };
  if (view.watchdog?.darkSince != null)
    return {
      title: `No model ready since ${clock(view.watchdog.darkSince)}`,
      tone: 'attention',
    };
  if (view.excursion && (!current || view.excursion.target === current))
    return {
      title: `Trying ${label(view.excursion.target)} while the network favours it`,
      tone: 'good',
    };
  if (view.action === 'end-excursion' || view.action === 'return-home')
    return {
      title: `Returning to ${homeName ?? 'the home model'}`,
      tone: 'working',
    };
  if (view.action === 'excursion' && view.proposal)
    return {
      title: `Moving to ${label(view.proposal.target)} on network evidence`,
      tone: 'working',
    };
  if (view.retryAt != null && view.retryAt > now)
    return {
      title: `Waiting to retry at ${clock(view.retryAt)}`,
      tone: 'working',
    };
  if (view.home?.failedAt != null)
    return {
      title: `Your pick ${homeName} could not be restored`,
      tone: 'attention',
    };
  if (!view.home) return { title: 'Holding the current model', tone: 'good' };
  if (current && view.home.model !== current)
    return { title: `Waiting to return to ${homeName}`, tone: 'working' };
  return {
    title: view.pinned
      ? `Holding your pick, ${homeName}`
      : `Holding ${homeName}`,
    tone: 'good',
  };
}

/** The backend's own sentence for what is happening and why, verbatim. */
export function managerSentence(view: ManagerView): string {
  const dark = view.watchdog?.darkSince != null || !!view.recovery;
  return withoutCircularHint(
    (dark && view.watchdog?.reason) ||
      view.reason ||
      view.watchdog?.reason ||
      '',
  );
}

export function homeSource(home: ManagerHome | null, pinned: boolean): string {
  if (!home)
    return 'Not chosen yet: no model has earned here on 3 separate days in the last 30 days';
  if (home.source === 'manual' || (pinned && home.source !== 'external'))
    return 'Your pick · Bloomkeeper won’t switch away from it';
  if (home.source === 'external')
    return 'Changed outside Bloomkeeper · held as your pick';
  if (home.source === 'history')
    return home.usdPerHour != null
      ? `Best paid on this Mac · ${usd(home.usdPerHour)} per ready hour${home.hours != null ? ` over ${Math.round(home.hours)} h` : ''}${home.days != null ? ` on ${home.days} days` : ''} in 30 days`
      : 'Best paid on this Mac in the last 30 days';
  if (home.source === 'current')
    return 'The model serving when the manager started';
  return 'Home model';
}

export type Readiness = {
  state: 'ready' | 'not-ready' | 'stopped' | 'unknown';
  label: string;
  reason: string | null;
  since: number | null;
};

/**
 * Current-model readiness with a concrete reason. Never "checking" without saying
 * why and since when: the watchdog's dark time wins, else the first time this view
 * saw the model not ready (`seen`).
 */
export function readiness({
  providerRunning,
  warmup,
  watchdog,
  seen,
}: {
  providerRunning: boolean | null | undefined;
  warmup?: { status?: string | null; detail?: string | null } | null;
  watchdog?: ManagerWatchdog | null;
  seen?: number | null;
}): Readiness {
  if (providerRunning === false)
    return {
      state: 'stopped',
      label: 'Stopped',
      reason: 'Darkbloom is not running',
      since: null,
    };
  if (providerRunning == null)
    return {
      state: 'unknown',
      label: 'Waiting for status',
      reason: null,
      since: null,
    };
  if (warmup?.status === 'ready' && watchdog?.darkSince == null)
    return {
      state: 'ready',
      label: 'Warm and ready',
      reason: null,
      since: null,
    };
  const since = watchdog?.darkSince ?? seen ?? null;
  const reason =
    withoutCircularHint(watchdog?.darkSince != null ? watchdog.reason : null) ||
    withoutCircularHint(warmup?.detail) ||
    (warmup?.status
      ? `Readiness check reports “${warmup.status}” with no detail`
      : 'No readiness reading from Darkbloom yet');
  return {
    state: 'not-ready',
    label: warmup?.status === 'warming' ? 'Warming up' : 'Not ready',
    reason: reason.replace(/\.$/, ''),
    since,
  };
}

export function clock(at: number) {
  return new Date(at * 1000).toLocaleTimeString([], {
    hour: 'numeric',
    minute: '2-digit',
  });
}

/** Time on an excursion, its limit and predicted vs realized pay. */
export function excursionProgress(
  ex: ManagerExcursion,
  now = Date.now() / 1000,
) {
  const max = ex.maxMinutes ?? 24 * 60; // the backend's runaway cap (evidence ends it sooner)
  const elapsed = ex.startedAt != null ? Math.max(0, now - ex.startedAt) : null;
  return {
    elapsed,
    maxSeconds: max * 60,
    left: elapsed != null ? Math.max(0, max * 60 - elapsed) : null,
    predicted: ex.predictedUsdPerHour,
    realized: ex.realizedUsdPerHour,
  };
}

/** "Qwen3.8 has looked 1.8× better for your Mac for 1 h 5 min · 1 of 2 checks". */
export function armingText(
  arming: ManagerArming,
  rows: EvidenceRow[] = [],
  label: Label = same,
  now = Date.now() / 1000,
) {
  const ratio =
    arming.ratio ?? rows.find((r) => r.model === arming.model)?.ratio ?? null;
  const better = ratio != null ? `${ratio.toFixed(1)}× better` : 'better';
  const time =
    arming.since != null && now >= arming.since
      ? ` for ${duration(now - arming.since)}`
      : '';
  const unit = arming.checkSeconds === 3600 ? 'hourly checks' : 'checks';
  const checks =
    arming.checks != null && arming.needed != null
      ? ` · ${Math.min(arming.checks, arming.needed)} of ${arming.needed} ${unit} before a switch`
      : '';
  return `${label(arming.model)} has looked ${better} for your Mac${time}${checks}`;
}

/** "Switches in the last 14 days: 2, +$0.31 vs staying on Gemma 4 26B". */
export function ledgerText(
  ledger: ManagerLedger,
  homeModel: string | null,
  label: Label = same,
  pinned = false,
) {
  if (ledger.enabled === false)
    return `Excursions are off${ledger.disabledReason ? `: ${ledger.disabledReason.replace(/\.$/, '')}` : ''}`;
  const days = ledger.days ?? 14;
  const count = ledger.count ?? 0;
  const gain =
    count && ledger.gainUsd != null
      ? `, ${signed(ledger.gainUsd)} vs staying on ${homeModel && !pinned ? label(homeModel) : 'the home model'}`
      : '';
  const predicted =
    count && ledger.predictedUsd != null
      ? ` (predicted ${signed(ledger.predictedUsd)})`
      : '';
  return `Switches in the last ${days} days: ${count}${gain}${predicted}`;
}

/** 'M5 Pro|48' → 'M5 Pro · 48 GB'. */
export function cellLabel(cell: string | null | undefined) {
  if (!cell) return 'this Mac’s hardware class';
  const [name, memory] = cell.split('|');
  return memory ? `${name} · ${memory} GB` : name;
}

/** Evidence rows best first, with the home model marked (and present even without a row). */
export function evidenceTable(
  evidence: ManagerEvidence,
  homeModel?: string | null,
) {
  const home = evidence.home ?? homeModel ?? null;
  const rows = [...evidence.rows].sort(
    (a, b) => (b.usdPerHour ?? -1) - (a.usdPerHour ?? -1),
  );
  return rows.map((row) => ({ ...row, home: row.model === home }));
}

/** The latest time automatic control was paused, if it is still paused. */
export function lastPause(
  events: unknown,
): { at: number; model: string | null; detail: string } | null {
  let pause: { at: number; model: string | null; detail: string } | null = null;
  let resumed = -Infinity;
  for (const e of Array.isArray(events) ? events : []) {
    if (!record(e) || !finite(e.at) || !text(e.kind)) continue;
    if (['resumed', 'started'].includes(e.kind))
      resumed = Math.max(resumed, e.at);
    if (e.kind === 'paused' && text(e.detail) && (!pause || e.at > pause.at))
      pause = { at: e.at, model: str(e.model), detail: e.detail };
  }
  return pause && pause.at > resumed ? pause : null;
}
