import type { PulsePoint } from './pulse-smoothing';
export type PaceField = 'rate60' | 'rate300';
export type PacePoint = {
  at: number;
  value: number;
  low: number;
  high: number;
  from: number;
  to: number;
  sessionId?: number;
  kind: 'average' | 'reading';
  observedSeconds?: number;
  coverage?: number;
};
const steps = [
  1, 2, 3, 5, 10, 15, 20, 30, 60, 120, 180, 300, 600, 900, 1200, 1800, 3600,
  7200, 10800, 14400, 21600, 28800, 43200, 86400, 172800, 604800,
];
/** A consistent time grid, sized to the actual plotting width. Buckets do not
 * slide with each poll, and a narrow phone does not draw hundreds of vertices. */
export function paceBucketSeconds(
  span: number,
  width: number,
  sourceBucket = 1,
) {
  const target = Math.max(
    16,
    Math.min(200, Math.floor(Math.max(1, width) / 3)),
  );
  const needed = Math.max(1, sourceBucket, Math.max(1, span) / target);
  return steps.find((s) => s >= needed) ?? Math.ceil(needed / 86400) * 86400;
}
export function paceReading(p: PulsePoint, field: PaceField): PacePoint {
  return {
    at: p.at,
    value: p[field] as number,
    low: p[field] as number,
    high: p[field] as number,
    from: p.at,
    to: p.at,
    sessionId: p.sessionId,
    kind: 'reading',
  };
}
/** Integrate only within a previously verified continuous run. Linear
 * interpolation follows the recorded line; it never invents values across a
 * missing observation or a model/session boundary. Time, not row count, weights
 * irregular sampling. No EMA, extrapolated tail or curve overshoot. */
export function averagePaceRun(
  run: PulsePoint[],
  field: PaceField,
  step: number,
): PacePoint[] {
  if (run.length < 2) return run.map((p) => paceReading(p, field));
  const bins = new Map<
    number,
    {
      from: number;
      to: number;
      area: number;
      seconds: number;
      low: number;
      high: number;
    }
  >();
  for (let i = 1; i < run.length; i++) {
    const a = run[i - 1],
      b = run[i],
      duration = b.at - a.at;
    if (
      !(duration > 0) ||
      a.sessionId !== b.sessionId ||
      a[field] == null ||
      b[field] == null
    )
      continue;
    for (let from = a.at; from < b.at;) {
      const bucket = Math.floor(from / step),
        to = Math.min(b.at, (bucket + 1) * step);
      if (to <= from) break;
      // Clamp roundoff at an endpoint (for example 0 - epsilon) so a positive
      // series cannot acquire a negative axis or an invented extreme.
      const interpolate = (at: number) =>
        Math.max(
          Math.min(a[field]!, b[field]!),
          Math.min(
            Math.max(a[field]!, b[field]!),
            a[field]! + ((b[field]! - a[field]!) * (at - a.at)) / duration,
          ),
        );
      const av = interpolate(from),
        bv = interpolate(to);
      const bin = bins.get(bucket) ?? {
        from,
        to,
        area: 0,
        seconds: 0,
        low: Infinity,
        high: -Infinity,
      };
      bin.to = to;
      bin.area += ((av + bv) / 2) * (to - from);
      bin.seconds += to - from;
      bin.low = Math.min(bin.low, av, bv);
      bin.high = Math.max(bin.high, av, bv);
      bins.set(bucket, bin);
      from = to;
    }
  }
  return [...bins.values()].map((b) => ({
    at: (b.from + b.to) / 2,
    value: b.area / b.seconds,
    low: b.low,
    high: b.high,
    from: b.from,
    to: b.to,
    sessionId: run[0].sessionId,
    kind: 'average',
  }));
}
/** In detail view retain the first, low, high and last recorded value in each
 * horizontal pixel column. This is selection, not averaging or smoothing. */
export function detailPaceRun(
  run: PulsePoint[],
  field: PaceField,
  start: number,
  span: number,
  width: number,
): PacePoint[] {
  const bins = new Map<number, PulsePoint[]>();
  for (const p of run) {
    const key = Math.floor(
      ((p.at - start) / Math.max(1, span)) * Math.max(1, width),
    );
    const group = bins.get(key) ?? [];
    group.push(p);
    bins.set(key, group);
  }
  return [...bins.values()].flatMap((group) => {
    const low = group.reduce((a, b) =>
      (a[field] as number) <= (b[field] as number) ? a : b,
    );
    const high = group.reduce((a, b) =>
      (a[field] as number) >= (b[field] as number) ? a : b,
    );
    return [...new Set([group[0], low, high, group[group.length - 1]])]
      .sort((a, b) => a.at - b.at)
      .map((p) => paceReading(p, field));
  });
}
export function displayPace(
  segments: PulsePoint[][],
  field: PaceField,
  options: {
    start: number;
    end: number;
    width: number;
    sourceBucket: number;
    detail: boolean;
  },
) {
  const span = Math.max(1, options.end - options.start);
  const bucketSeconds = paceBucketSeconds(
    span,
    options.width,
    options.sourceBucket,
  );
  return {
    bucketSeconds,
    segments: segments.map((run) =>
      options.detail
        ? detailPaceRun(run, field, options.start, span, options.width)
        : averagePaceRun(run, field, bucketSeconds),
    ),
  };
}
/** Interval bars summarize only observed runs. Missing seconds never contribute
 * zero or a connecting line; the coverage strip retains their exact locations.
 * Different sessions stay separate even inside one display interval. */
export function intervalPace(
  segments: PulsePoint[][],
  field: PaceField,
  options: { start: number; end: number; width: number; sourceBucket: number },
) {
  const { start, end } = options;
  // Wider marks than the line overview: roughly 30–60 bars for an hour.
  const bucketSeconds = paceBucketSeconds(
    end - start,
    options.width / 3,
    options.sourceBucket,
  );
  const spans = new Map<number | undefined, { from: number; to: number }>();
  for (const run of segments)
    for (const p of run) {
      const span = spans.get(p.sessionId) ?? { from: p.at, to: p.at };
      span.from = Math.min(span.from, p.at);
      span.to = Math.max(span.to, p.at);
      spans.set(p.sessionId, span);
    }
  const bins = new Map<
    string,
    { parts: PacePoint[]; bucket: number; sessionId?: number }
  >();
  const coverage: { from: number; to: number; sessionId?: number }[] = [];
  for (const run of segments) {
    if (run.length > 1)
      coverage.push({
        from: Math.max(start, run[0].at),
        to: Math.min(end, run.at(-1)!.at),
        sessionId: run[0].sessionId,
      });
    for (const p of averagePaceRun(run, field, bucketSeconds)) {
      const bucket = Math.floor(p.at / bucketSeconds),
        key = `${p.sessionId}:${bucket}`;
      const bin = bins.get(key) ?? {
        parts: [],
        bucket,
        sessionId: p.sessionId,
      };
      bin.parts.push(p);
      bins.set(key, bin);
    }
  }
  const points = [...bins.values()]
    .map<PacePoint>(({ parts, bucket, sessionId }) => {
      const span = spans.get(sessionId)!;
      const from = Math.max(start, bucket * bucketSeconds, span.from),
        to = Math.min(end, (bucket + 1) * bucketSeconds, span.to);
      const observedSeconds = parts.reduce((n, p) => n + p.to - p.from, 0);
      const value =
        observedSeconds > 0
          ? parts.reduce((n, p) => n + p.value * (p.to - p.from), 0) /
            observedSeconds
          : parts.at(-1)!.value;
      return {
        at: (from + to) / 2,
        from,
        to,
        value,
        low: Math.min(...parts.map((p) => p.low)),
        high: Math.max(...parts.map((p) => p.high)),
        sessionId,
        kind: observedSeconds > 0 ? 'average' : 'reading',
        observedSeconds,
        coverage: Math.min(1, observedSeconds / Math.max(1, to - from)),
      };
    })
    .sort((a, b) => a.at - b.at);
  return {
    bucketSeconds,
    points,
    coverage,
    observedSeconds: coverage.reduce(
      (n, p) => n + Math.max(0, p.to - p.from),
      0,
    ),
  };
}
export function paceInterval(seconds: number) {
  if (seconds < 1) return '<1s';
  if (seconds < 60) return `${Number(seconds.toFixed(1))}s`;
  if (seconds < 3600) return `${Number((seconds / 60).toFixed(1))}m`;
  if (seconds < 86400) return `${Number((seconds / 3600).toFixed(1))}h`;
  return `${Number((seconds / 86400).toFixed(1))}d`;
}
