export type PulsePoint = {
  at: number;
  rate60: number | null;
  rate300: number | null;
  sessionId?: number;
};
export type PulseSession = {
  id: number;
  models: string[];
  firstAt: number;
  lastAt: number;
};
export type PulseHistory = {
  sessionId: number | null;
  scope?: 'models';
  sessions?: PulseSession[];
  samples: PulsePoint[];
  coverageStart: number | null;
  bucketSeconds: number;
};

/** The meter sample owns the right edge, even when a history bucket has the
 * same timestamp or a later HTTP response contains newer observations. */
export function pulseChartRows(
  samples: PulsePoint[],
  observation: PulsePoint | null,
  start: number,
  end: number,
  maxGap: number,
): PulsePoint[] {
  const anchor =
    observation &&
    Number.isFinite(observation.at) &&
    observation.at >= start &&
    observation.at <= end
      ? observation
      : null;
  const rows = samples.filter(
    (point) =>
      Number.isFinite(point.at) &&
      point.at >= start &&
      point.at <= end &&
      (!anchor || point.at < anchor.at),
  );
  if (anchor) {
    const last = rows.at(-1);
    if (last && anchor.at - last.at > maxGap)
      rows.push({ at: (last.at + anchor.at) / 2, rate60: null, rate300: null });
    rows.push({ ...anchor });
  }
  return rows;
}

/** A malformed/changed response must not replace a usable chart or crash it. */
export function readPulseHistory(
  value: unknown,
  session: number,
  acrossModels = false,
): PulseHistory {
  if (!value || typeof value !== 'object')
    throw new Error('History unavailable');
  const history = value as PulseHistory;
  const reading = (rate: unknown) =>
    rate === null || (typeof rate === 'number' && Number.isFinite(rate));
  if (
    history.sessionId !== session ||
    !Array.isArray(history.samples) ||
    !Number.isFinite(history.bucketSeconds) ||
    history.bucketSeconds <= 0 ||
    !reading(history.coverageStart) ||
    history.samples.some(
      (point) =>
        !point ||
        !Number.isFinite(point.at) ||
        !reading(point.rate60) ||
        !reading(point.rate300),
    )
  ) {
    throw new Error('History unavailable for this session');
  }
  if (acrossModels) {
    if (
      history.scope !== 'models' ||
      !Array.isArray(history.sessions) ||
      history.sessions.some(
        (s) =>
          !s ||
          !Number.isSafeInteger(s.id) ||
          s.id < 1 ||
          !Array.isArray(s.models) ||
          s.models.some((m) => typeof m !== 'string' || !m || m.length > 512) ||
          !Number.isFinite(s.firstAt) ||
          !Number.isFinite(s.lastAt) ||
          s.lastAt < s.firstAt,
      )
    )
      throw new Error('Model history unavailable');
    const ids = new Set(history.sessions.map((s) => s.id));
    if (
      ids.size !== history.sessions.length ||
      history.samples.some(
        (p, i) =>
          !ids.has(p.sessionId as number) ||
          (i > 0 && p.at < history.samples[i - 1].at),
      )
    )
      throw new Error('Model history unavailable');
  }
  return history;
}

const sameSession = (a: { sessionId?: number }, b: { sessionId?: number }) =>
  a.sessionId === b.sessionId;

/** Display-only exponential smoothing. Missing samples reset the trend.
 * An optional exact tail keeps recent observations and the endpoint unaltered. */
export function smoothPulse<T extends { at: number; sessionId?: number }>(
  rows: T[],
  field: keyof T,
  seconds: number,
  maxGap: number,
  exactTailSeconds = 0,
): T[] {
  let previous: { at: number; value: number; sessionId?: number } | null = null;
  const exactFrom =
    exactTailSeconds > 0
      ? (rows.at(-1)?.at ?? Infinity) - exactTailSeconds
      : Infinity;
  return rows.map((row) => {
    const value = row[field];
    if (typeof value !== 'number' || !Number.isFinite(value)) {
      previous = null;
      return { ...row };
    }
    const elapsed = previous ? row.at - previous.at : 0;
    const smoothed =
      previous && sameSession(previous, row) && elapsed > 0 && elapsed <= maxGap
        ? previous.value +
          (1 - Math.exp(-elapsed / seconds)) * (value - previous.value)
        : value;
    previous = { at: row.at, value: smoothed, sessionId: row.sessionId };
    return { ...row, [field]: row.at >= exactFrom ? value : smoothed };
  });
}

/** Separate runs so a chart never draws a line across an unobserved interval. */
export function pulseSegments<T extends { at: number; sessionId?: number }>(
  rows: T[],
  field: keyof T,
  maxGap: number,
): T[][] {
  const segments: T[][] = [];
  let segment: T[] = [];
  let previous: number | null = null;
  for (const row of rows) {
    const value = row[field];
    if (
      !Number.isFinite(row.at) ||
      typeof value !== 'number' ||
      !Number.isFinite(value)
    ) {
      segment = [];
      previous = null;
      continue;
    }
    if (
      previous == null ||
      row.at <= previous ||
      row.at - previous > maxGap ||
      (segment.length > 0 && !sameSession(segment[segment.length - 1], row))
    ) {
      segment = [];
    }
    if (!segment.length) segments.push(segment);
    segment.push(row);
    previous = row.at;
  }
  return segments;
}
