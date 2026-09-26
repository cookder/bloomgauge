export type ContributionMetric =
  | 'activity'
  | 'requests'
  | 'tokens'
  | 'earnings';
export type NetworkContributions = {
  schemaVersion: 1;
  metric: ContributionMetric;
  scope: 'network' | 'this_mac';
  status: 'ok' | 'partial' | 'empty' | 'unavailable';
  at: number;
  from: number;
  to: number;
  bucketSeconds: number;
  unit:
    | 'concurrent_requests'
    | 'requests_per_minute'
    | 'tokens_per_second'
    | 'usd_per_hour';
  attribution: 'models' | 'unavailable';
  series: { id: string; name: string }[];
  points: {
    from: number;
    to: number;
    values: (number | null)[];
    coverageFraction: number;
  }[];
  summary: {
    total: number | null;
    values: (number | null)[];
    unit: 'concurrent_requests' | 'requests' | 'tokens' | 'usd';
  };
  coverage: {
    observedSeconds: number;
    requestedSeconds: number;
    fraction: number;
  };
  notes: string[];
  networkMoney: { available: false; reason: string };
};
const obj = (v: unknown): v is Record<string, any> =>
  v !== null && typeof v === 'object' && !Array.isArray(v);
const finite = (v: unknown): v is number =>
  typeof v === 'number' && Number.isFinite(v);
const nonnegative = (v: unknown): v is number => finite(v) && v >= 0;
const fraction = (v: unknown) => nonnegative(v) && v <= 1;
const text = (v: unknown, max = 600): v is string =>
  typeof v === 'string' && v.length > 0 && v.length <= max;
const units = {
  activity: ['concurrent_requests', 'concurrent_requests'],
  requests: ['requests_per_minute', 'requests'],
  tokens: ['tokens_per_second', 'tokens'],
  earnings: ['usd_per_hour', 'usd'],
} as const;

export function readNetworkContributions(
  v: unknown,
  metric: ContributionMetric,
): NetworkContributions {
  const value = (n: unknown) =>
    n === null || (finite(n) && (metric === 'earnings' || n >= 0));
  if (
    !obj(v) ||
    v.schemaVersion !== 1 ||
    v.metric !== metric ||
    v.scope !== (metric === 'earnings' ? 'this_mac' : 'network') ||
    !['ok', 'partial', 'empty', 'unavailable'].includes(v.status) ||
    !nonnegative(v.at) ||
    !nonnegative(v.from) ||
    !nonnegative(v.to) ||
    v.to <= v.from ||
    v.to > v.at + 1 ||
    !nonnegative(v.bucketSeconds) ||
    v.bucketSeconds <= 0 ||
    v.unit !== units[metric][0] ||
    !['models', 'unavailable'].includes(v.attribution) ||
    !Array.isArray(v.series) ||
    v.series.length > 64 ||
    !v.series.every(
      (s: unknown) => obj(s) && text(s.id, 600) && text(s.name, 600),
    ) ||
    new Set(v.series.map((s: any) => s.id)).size !== v.series.length ||
    !Array.isArray(v.points) ||
    v.points.length > 600 ||
    !obj(v.summary) ||
    v.summary.unit !== units[metric][1] ||
    !value(v.summary.total) ||
    !Array.isArray(v.summary.values) ||
    v.summary.values.length !== v.series.length ||
    !v.summary.values.every(value) ||
    !obj(v.coverage) ||
    !nonnegative(v.coverage.observedSeconds) ||
    !nonnegative(v.coverage.requestedSeconds) ||
    !fraction(v.coverage.fraction) ||
    v.coverage.observedSeconds > v.coverage.requestedSeconds + 0.001 ||
    !Array.isArray(v.notes) ||
    v.notes.length > 16 ||
    !v.notes.every((n: unknown) => text(n, 1200)) ||
    !obj(v.networkMoney) ||
    v.networkMoney.available !== false ||
    !text(v.networkMoney.reason, 1200)
  )
    throw Error('The contribution report is incomplete.');
  let end = v.from;
  for (const p of v.points) {
    if (
      !obj(p) ||
      !nonnegative(p.from) ||
      !nonnegative(p.to) ||
      p.from < end - 0.001 ||
      p.to <= p.from ||
      p.to > v.to + 0.001 ||
      !fraction(p.coverageFraction) ||
      !Array.isArray(p.values) ||
      p.values.length !== v.series.length ||
      !p.values.every(value)
    )
      throw Error('The contribution timeline is incomplete.');
    end = p.to;
  }
  if (
    v.summary.total !== null &&
    v.summary.values.some((n: unknown) => n === null)
  )
    throw Error('The contribution total includes unknown models.');
  if (
    v.summary.total !== null &&
    v.summary.values.every((n: unknown) => n !== null)
  ) {
    const sum = (v.summary.values as number[]).reduce((a, n) => a + n, 0);
    if (Math.abs(sum - v.summary.total) > Math.max(1e-7, Math.abs(sum) * 1e-8))
      throw Error('The model contributions do not match the total.');
  }
  return v as NetworkContributions;
}

export type ContributionGroup = { id: string; name: string; indices: number[] };
export function contributionGroups(
  report: NetworkContributions,
): ContributionGroup[] {
  const indices = report.series
    .map((_, i) => i)
    .sort(
      (a, b) =>
        Math.abs(report.summary.values[b] ?? 0) -
          Math.abs(report.summary.values[a] ?? 0) ||
        report.series[a].id.localeCompare(report.series[b].id),
    );
  return indices.map((i) => ({ ...report.series[i], indices: [i] }));
}
export function groupedContributions(
  values: (number | null)[],
  groups: ContributionGroup[],
) {
  return groups.map((g) =>
    g.indices.some((i) => values[i] === null || values[i] === undefined)
      ? null
      : g.indices.reduce((sum, i) => sum + values[i]!, 0),
  );
}
export function contributionShares(
  values: (number | null)[],
): (number | null)[] {
  const total = values.reduce<number>((n, v) => n + (v ?? 0), 0);
  return values.every((v) => v !== null && v >= 0) && total > 0
    ? values.map((v) => (v! * 100) / total)
    : values.map(() => null);
}
