export type WeeklyMetric = 'requestsPerMinute' | 'tokensPerSecond';
export type WeeklyCell = {
  day: number;
  hour: number;
  days: number;
  possibleDays: number;
  observedSeconds: number;
  expectedSeconds: number;
  qualifiedSeconds: number;
  requests: number;
  tokens: number;
  requestsPerMinute: number | null;
  tokensPerSecond: number | null;
  dates: {
    date: string;
    coverage: number;
    requestsPerMinute: number | null;
    tokensPerSecond: number | null;
  }[];
};
export type WeeklyNetwork = {
  from: number;
  to: number;
  at: number;
  timezone: string;
  cells: WeeklyCell[];
  coverageStart: number | null;
  coverageEnd: number | null;
  hourlyHistoryStart: number | null;
  qualifiedDates: number;
  observedHours: number;
  expectedHours: number;
  minimumCoverage: number;
  coarseBuckets: number;
  boundaryBuckets: number;
  invalidBuckets: number;
};
const nonnegative = (v: unknown): v is number =>
  typeof v === 'number' && Number.isFinite(v) && v >= 0;
const nullable = (v: unknown) => v === null || nonnegative(v);
export function validWeeklyNetwork(d: WeeklyNetwork): boolean {
  return (
    !!d &&
    nonnegative(d.from) &&
    nonnegative(d.to) &&
    nonnegative(d.at) &&
    typeof d.timezone === 'string' &&
    [d.coverageStart, d.coverageEnd, d.hourlyHistoryStart].every(nullable) &&
    [
      d.qualifiedDates,
      d.observedHours,
      d.expectedHours,
      d.minimumCoverage,
      d.coarseBuckets,
      d.boundaryBuckets,
      d.invalidBuckets,
    ].every(nonnegative) &&
    Array.isArray(d.cells) &&
    d.cells.length === 168 &&
    d.cells.every(
      (c, i) =>
        !!c &&
        c.day === Math.floor(i / 24) &&
        c.hour === i % 24 &&
        [
          c.days,
          c.possibleDays,
          c.observedSeconds,
          c.expectedSeconds,
          c.qualifiedSeconds,
          c.requests,
          c.tokens,
        ].every(nonnegative) &&
        [c.requestsPerMinute, c.tokensPerSecond].every(nullable) &&
        c.days <= c.possibleDays &&
        Array.isArray(c.dates) &&
        c.dates.every(
          (r) =>
            r &&
            /^\d{4}-\d{2}-\d{2}$/.test(r.date) &&
            nonnegative(r.coverage) &&
            r.coverage <= 1 &&
            nullable(r.requestsPerMinute) &&
            nullable(r.tokensPerSecond),
        ),
    )
  );
}
export function busiestCell(cells: WeeklyCell[], metric: WeeklyMetric) {
  return cells
    .filter((c) => c[metric] !== null)
    .sort(
      (a, b) =>
        b[metric]! - a[metric]! ||
        b.days - a.days ||
        a.day - b.day ||
        a.hour - b.hour,
    )[0];
}
export function heatLevel(
  value: number | null,
  maximum: number,
): number | null {
  return value === null
    ? null
    : maximum <= 0
      ? 0
      : Math.min(5, Math.floor((value / maximum) * 5));
}

// Use the same timezone as the historical cells, including weekday rollover and DST.
export function currentWeekHour(at: number, timezone: string) {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: timezone,
    weekday: 'short',
    hour: 'numeric',
    hourCycle: 'h23',
  }).formatToParts(new Date(at));
  const day = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].indexOf(
    parts.find((p) => p.type === 'weekday')!.value,
  );
  const hour = Number(parts.find((p) => p.type === 'hour')!.value);
  return { day, hour, index: day * 24 + hour };
}
