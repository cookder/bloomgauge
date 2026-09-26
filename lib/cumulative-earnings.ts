export type CumulativePoint = {
  at: number;
  cumulative: number | null;
  predicted?: number | null;
};

export type ModelProjection = {
  status: string;
  models: string[];
  asOf: number;
  hours: number;
  days: number;
  ratePerHour: number | null;
  detail: string;
  points: { at: number; additional: number }[];
};

// Join at the confirmed total. Forecast values only populate their own series.
export function projectedCumulative(
  actual: CumulativePoint[],
  forecast: ModelProjection | undefined,
  end: number,
): CumulativePoint[] {
  const last = actual.at(-1);
  if (
    !last ||
    last.cumulative == null ||
    forecast?.status !== 'ready' ||
    Math.abs(last.at - forecast.asOf) > 180 ||
    end <= last.at ||
    forecast.points.length < 2
  )
    return actual;
  const points = actual.map((p) => ({ ...p }));
  const start = forecast.points[0].at;
  // Interpolate at the exact horizon (including custom future end dates).
  const additionalAt = (at: number) => {
    const right = forecast.points.findIndex((p) => p.at >= at);
    if (right <= 0) return forecast.points[Math.max(0, right)].additional;
    const a = forecast.points[right - 1],
      b = forecast.points[right];
    return (
      a.additional +
      ((b.additional - a.additional) * (at - a.at)) / (b.at - a.at)
    );
  };
  const stop = Math.min(end, forecast.points.at(-1)!.at);
  if (stop <= last.at || last.at < start) return actual;
  const offset = additionalAt(last.at);
  points[points.length - 1].predicted = last.cumulative;
  for (const p of forecast.points) {
    if (p.at > last.at && p.at < stop)
      points.push({
        at: p.at,
        cumulative: null,
        predicted: last.cumulative + p.additional - offset,
      });
  }
  points.push({
    at: stop,
    cumulative: null,
    predicted: last.cumulative + additionalAt(stop) - offset,
  });
  return points;
}

// The caller supplies the same selected hourly records used by the earnings
// summary. Never add forecasts or a lifetime balance to the running total.
export function cumulativeEarnings(
  hours: { at: number; usd: number }[],
  observedAt: number,
  coverageStartedAt?: number | null,
): CumulativePoint[] {
  if (!hours.length) return [];
  const sorted = [...hours].sort((a, b) => a.at - b.at);
  const start = Math.max(sorted[0].at, coverageStartedAt ?? sorted[0].at);
  const points: CumulativePoint[] = [{ at: start, cumulative: 0 }];
  let microUSD = 0;
  let previousEnd = start;
  for (const hour of sorted) {
    if (hour.at > previousEnd) {
      // A missing hour is unknown, not an observed hour of zero earnings.
      points.push({ at: (previousEnd + hour.at) / 2, cumulative: null });
      points.push({ at: hour.at, cumulative: microUSD / 1e6 });
    }
    microUSD += Math.round(hour.usd * 1e6);
    const end = Math.max(hour.at, Math.min(hour.at + 3600, observedAt));
    points.push({ at: end, cumulative: microUSD / 1e6 });
    previousEnd = end;
  }
  return points;
}
