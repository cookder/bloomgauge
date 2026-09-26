import type { NetworkContributions } from './network-contributions';

export const pulseGraphStyles = ['earnings', 'line', 'range'] as const;
export type PulseGraphStyle = (typeof pulseGraphStyles)[number];
export function readPulseGraphStyle(value: unknown): PulseGraphStyle {
  return pulseGraphStyles.includes(value as PulseGraphStyle)
    ? (value as PulseGraphStyle)
    : 'earnings';
}

/** Merge only whole source intervals. Credit rates are weighted by elapsed
 * time, not warm time or sample count. Unknown money is never filled with zero. */
export function pulseEarningsBars(report: NetworkContributions) {
  const span = report.to - report.from;
  const wanted = Math.max(report.bucketSeconds, span / 30);
  const step = Math.ceil(wanted / report.bucketSeconds) * report.bucketSeconds;
  const low = Math.floor(report.from / step) * step;
  const count = Math.ceil((report.to - low) / step);
  const bins = Array.from({ length: count }, (_, i) => ({
    from: Math.max(report.from, low + i * step),
    to: Math.min(report.to, low + (i + 1) * step),
    values: report.series.map(() => 0 as number | null),
    seconds: 0,
    observed: 0,
  }));
  for (const point of report.points) {
    const i = Math.floor((point.from - low) / step),
      bin = bins[i];
    if (!bin || point.from < bin.from || point.to > bin.to + 0.001) continue;
    const duration = point.to - point.from;
    bin.seconds += duration;
    bin.observed += duration * point.coverageFraction;
    point.values.forEach((value, k) => {
      if (value === null) bin.values[k] = null;
      else if (bin.values[k] !== null) bin.values[k]! += value * duration;
    });
  }
  return {
    bucketSeconds: step,
    points: bins.map((bin) => ({
      from: bin.from,
      to: bin.to,
      coverageFraction: bin.observed / (bin.to - bin.from),
      values: bin.values.map((value) =>
        value !== null && Math.abs(bin.seconds - (bin.to - bin.from)) < 0.001
          ? value / (bin.to - bin.from)
          : null,
      ),
    })),
  };
}
