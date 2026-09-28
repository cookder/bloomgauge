// Stall recovery as shown on the optimizer page: GET /api/optimizer `stallRecovery`
// (native/stall_control.py snapshot) plus its `stall-*` rows in `events`.
// A malformed snapshot hides the panel; it never rejects the optimizer response.

const record = (v: unknown): v is Record<string, unknown> =>
  !!v && typeof v === 'object' && !Array.isArray(v);
const finite = (v: unknown): v is number =>
  typeof v === 'number' && Number.isFinite(v);
const optional = (v: unknown, check: (x: unknown) => boolean) =>
  v == null || check(v);
const text = (v: unknown) => typeof v === 'string';

const STALL_STEPS = ['probe', 'restart', 'escape', 'hold'] as const;
export type StallStep = (typeof STALL_STEPS)[number];
const LABELS: Record<StallStep, string> = {
  probe: 'Test request',
  restart: 'Restart on the same model',
  escape: 'Try another model',
  hold: 'Stop and notify you',
};
// The manager never escapes to another model: it holds the home model instead.
const MANAGER_LABELS: Record<StallStep, string> = {
  ...LABELS,
  hold: 'Hold the home model and notify you',
};
// A model became ready (or automatic control restarted) after the stall: a new ready
// period began, so the stall card is history, not news.
const READY_KINDS = new Set(['switched', 'recovered', 'resumed', 'started']);
// native/stall_recovery.py EPISODE_LIMIT_SECONDS: longer silences are ordinary quiet.
const EPISODE_SECONDS = 3 * 3600;
const RECENT_SECONDS = 86400;
const RECENT_CARD_SECONDS = 3600;

export type StallSnapshot = {
  status?: string;
  step?: string | null;
  reason?: string | null;
  model?: string | null;
  silenceSeconds?: number | null;
  requiredSeconds?: number | null;
  baselineJobsPerMinute?: number | null;
  demandHeld?: boolean | null;
  episodeStart?: number | null;
  taken?: string[];
  nudge?: { kind: string; ok: boolean; code: string } | null;
  at: number;
  fresh: boolean;
};
export type StallEvent = {
  at: number;
  kind: string;
  model?: string | null;
  detail: string;
};

export function validStallSnapshot(v: unknown): v is StallSnapshot {
  if (!record(v) || !finite(v.at) || typeof v.fresh !== 'boolean') return false;
  if (!['status', 'step', 'reason', 'model'].every((k) => optional(v[k], text)))
    return false;
  if (
    ![
      'silenceSeconds',
      'requiredSeconds',
      'baselineJobsPerMinute',
      'episodeStart',
    ].every((k) => optional(v[k], finite))
  )
    return false;
  if (!optional(v.demandHeld, (x) => typeof x === 'boolean')) return false;
  if (!optional(v.taken, (x) => Array.isArray(x) && x.every(text)))
    return false;
  return optional(
    v.nudge,
    (x) =>
      record(x) && text(x.kind) && typeof x.ok === 'boolean' && text(x.code),
  );
}

export type StallStepView = {
  step: StallStep;
  label: string;
  state: 'done' | 'skipped' | 'upcoming';
  at?: number;
  note?: string;
};
export type StallView = {
  state: 'active' | 'recent';
  model: string | null;
  since: number | null;
  headline: string;
  detail: string | null;
  steps: StallStepView[];
};

const minutes = (seconds: number) => Math.max(1, Math.round(seconds / 60));

/**
 * What to show, or null when there is nothing to report: no stall now, and the last
 * one ended (work came back, or a model became ready again after it) or is over a
 * day old. Under the manager (`manager: true`) the "Try another model" step is not
 * part of the ladder unless an older episode actually took it.
 */
export function stallView(
  snapshot: unknown,
  events: unknown,
  now: number,
  { manager = false }: { manager?: boolean } = {},
): StallView | null {
  const labels = manager ? MANAGER_LABELS : LABELS;
  const s = validStallSnapshot(snapshot) ? snapshot : null;
  const rows: StallEvent[] = (Array.isArray(events) ? events : []).filter(
    (e): e is StallEvent =>
      record(e) &&
      finite(e.at) &&
      text(e.kind) &&
      e.kind.startsWith('stall-') &&
      text(e.detail) &&
      optional(e.model, text),
  );
  const active =
    !!s && s.fresh && s.status === 'stalled' && finite(s.episodeStart);
  let episode: StallEvent[];
  if (active) {
    episode = rows.filter((e) => e.at >= (s!.episodeStart as number));
  } else {
    // The latest episode only. Steps never repeat within one, and run in ladder
    // order, so walking back in time a step at or after the earliest one already
    // seen (or a gap longer than an episode) belongs to an earlier episode.
    const recent = rows
      .filter((e) => now - e.at <= RECENT_SECONDS)
      .sort((a, b) => b.at - a.at);
    const ladder = recent.filter((e) =>
      STALL_STEPS.includes(e.kind.slice(6) as StallStep),
    );
    if (!ladder.length) return null;
    const steps: StallEvent[] = [ladder[0]];
    let lowest = STALL_STEPS.indexOf(ladder[0].kind.slice(6) as StallStep);
    for (const e of ladder.slice(1)) {
      const index = STALL_STEPS.indexOf(e.kind.slice(6) as StallStep);
      if (
        index >= lowest ||
        steps[steps.length - 1].at - e.at > EPISODE_SECONDS
      )
        break;
      steps.push(e);
      lowest = index;
    }
    const start = steps[steps.length - 1].at;
    // Nudge replies (and other notes) arrive after their step.
    episode = recent.filter((e) => e.at >= start);
  }
  const done = new Map<StallStep, StallEvent>();
  for (const e of [...episode].sort((a, b) => a.at - b.at)) {
    const step = e.kind.slice(6) as StallStep;
    if (STALL_STEPS.includes(step) && !done.has(step)) done.set(step, e);
  }
  for (const step of active ? (s!.taken ?? []) : []) {
    if (STALL_STEPS.includes(step as StallStep) && !done.has(step as StallStep))
      done.set(step as StallStep, {
        at: s!.at,
        kind: 'stall-' + step,
        detail: '',
      });
  }
  const nudge = [...episode]
    .sort((a, b) => b.at - a.at)
    .find((e) => e.kind === 'stall-nudge');
  const last = Math.max(
    -1,
    ...STALL_STEPS.map((step, i) => (done.has(step) ? i : -1)),
  );
  const steps: StallStepView[] = STALL_STEPS.filter(
    (step) => !manager || step !== 'escape' || done.has(step),
  ).map((step) => {
    const i = STALL_STEPS.indexOf(step);
    const event = done.get(step);
    if (event)
      return {
        step,
        label: labels[step],
        state: 'done',
        at: event.at,
        note:
          step === 'probe'
            ? nudge?.detail ||
              (active && s!.nudge
                ? s!.nudge.ok
                  ? 'Served.'
                  : `Failed (${s!.nudge.code}).`
                : 'Waiting for the reply.')
            : undefined,
      };
    return {
      step,
      label: labels[step],
      state: i < last ? 'skipped' : 'upcoming',
    };
  });
  const model =
    (active ? s!.model : null) ?? episode.find((e) => e.model)?.model ?? null;
  if (active) {
    const silence = finite(s!.silenceSeconds)
      ? `${minutes(s!.silenceSeconds)} min`
      : 'several minutes';
    const rate = finite(s!.baselineJobsPerMinute)
      ? ` after about ${Math.round(s!.baselineJobsPerMinute)} jobs a minute`
      : '';
    // Bloomkeeper's reason often says this already; don't repeat it.
    const demand = /demand/i.test(s!.reason ?? '')
      ? ''
      : s!.demandHeld === true
        ? ' Network demand for it held.'
        : s!.demandHeld === false
          ? ' Network demand for it fell too.'
          : '';
    return {
      state: 'active',
      model,
      since: s!.episodeStart ?? null,
      steps,
      headline: `No work for ${silence}${rate}.${demand}`,
      detail: s!.reason ?? null,
    };
  }
  // Work came back ('quiet' with a short silence is trickling work, not a stall):
  // the episode is over, so the card goes away.
  const flowing =
    !!s &&
    s.fresh &&
    (finite(s.silenceSeconds)
      ? s.silenceSeconds < 300
      : s.status === 'ok' || s.status === undefined);
  if (flowing) return null;
  const ended = Math.max(...episode.map((e) => e.at));
  // A finished episode is news for an hour at most; after that it is history.
  if (now - ended > RECENT_CARD_SECONDS) return null;
  const readyAgain = (Array.isArray(events) ? events : []).some(
    (e) =>
      record(e) &&
      finite(e.at) &&
      e.at > ended &&
      text(e.kind) &&
      (READY_KINDS.has(e.kind) ||
        (e.kind === 'manager' &&
          text(e.detail) &&
          /\b(is ready again|became ready)\b/i.test(e.detail))),
  );
  if (readyAgain) return null;
  const gaveUp = done.has('hold');
  return {
    state: 'recent',
    model,
    since: Math.min(...episode.map((e) => e.at)),
    steps,
    headline: gaveUp
      ? manager
        ? 'Bloomkeeper held the home model after these steps.'
        : 'Bloomkeeper stopped trying after these steps.'
      : 'No steady work right now.',
    detail: null,
  };
}
