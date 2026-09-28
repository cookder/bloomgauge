import {
  earningsMetrics,
  modelEarnings,
  type TrackedEarningsHour,
} from './model-earnings';

export type HourlyMetric = ReturnType<typeof earningsMetrics>[number];
export type HourlyBarRow = HourlyMetric & { projected: number | null };

/** The fields of the snapshot's hour-end forecast the bars use. */
export type HourEndForecast = {
  hourStart: number;
  hourEnd: number;
  earnings: {
    status: string;
    actual: number | null;
    additional: number | null;
  };
};

/**
 * Add the current hour's estimated remainder as its own `projected` value, on
 * top of the confirmed amount. Shared by the Charts tab's Earnings bars and the
 * Overview's hourly bars so both show the same end-of-hour prediction. With no
 * recorded row for the current hour yet, one is added at the forecast's
 * confirmed amount so the estimate still has a bar to sit on.
 */
export function withHourProjection(
  hours: HourlyMetric[],
  forecast: HourEndForecast | undefined,
  show: boolean,
): HourlyBarRow[] {
  const ready = show && forecast?.earnings.status === 'ready';
  const rows: HourlyBarRow[] = hours.map((h) => ({
    ...h,
    projected:
      ready && h.at === forecast.hourStart
        ? forecast.earnings.additional
        : null,
  }));
  if (ready && !rows.some((h) => h.at === forecast.hourStart))
    rows.push({
      at: forecast.hourStart,
      usd: forecast.earnings.actual ?? 0,
      jobs: 0,
      categories: {},
      seconds: 0,
      rateComplete: false,
      perMinute: 0,
      perHour: 0,
      perJob: null,
      projected: forecast.earnings.additional,
    });
  return rows;
}

export const overviewHourlyKey = 'bloom.overview-hourly.v1';
export type OverviewHourlyWindow = 12 | 24;
export function readOverviewHourlyWindow(
  raw: string | null,
): OverviewHourlyWindow {
  return raw === '24' ? 24 : 12;
}

type MonitorHours = {
  hours: TrackedEarningsHour[];
  observedAt?: number;
  updatedAt: number | null;
  coverageStartedAt: number | null;
  coverageIntervals?: { start: number; end: number }[];
  gaps: number;
};

/**
 * Dollars per clock hour for the last `windowHours`, stacked by model, exactly
 * as the Charts tab's Earnings view (All models + rewards, By model) shapes
 * them, with the current hour's estimate stacked on top.
 */
export function overviewHourlyBars(
  monitor: MonitorHours | undefined,
  forecast: HourEndForecast | undefined,
  at: number,
  windowHours: OverviewHourlyWindow,
) {
  const start = at - windowHours * 3600;
  const observedAt = monitor?.observedAt ?? monitor?.updatedAt ?? at;
  const hours = earningsMetrics(
    monitor?.hours.filter((h) => h.at + 3600 > start && h.at < at) ?? [],
    observedAt,
    monitor?.coverageStartedAt,
    monitor?.coverageIntervals,
    (monitor?.gaps ?? 0) > (monitor?.coverageIntervals?.length ?? 0),
  );
  const show =
    !!forecast && forecast.hourEnd > start && forecast.hourStart < at;
  const perModel = modelEarnings(hours, observedAt, monitor?.coverageStartedAt);
  const series = [...perModel.series].sort((a, b) => b.usd - a.usd);
  const empty = Object.fromEntries(series.map((s) => [s.key, null]));
  const rows = withHourProjection(hours, forecast, show).map((h) => ({
    ...h,
    byModel:
      perModel.hourly.find((point) => point.at === h.at)?.byModel ?? empty,
  }));
  const current = rows.find((h) => h.projected != null);
  const firstAt = rows[0]?.at ?? at;
  const lastAt = rows.at(-1)?.at ?? at;
  const axisMaximum = rows.reduce((maximum, row) => {
    const amounts = [...Object.values(row.byModel), row.projected].filter(
      (v): v is number => typeof v === 'number' && Number.isFinite(v),
    );
    const positive = amounts.reduce((sum, v) => sum + Math.max(0, v), 0);
    const negative = amounts.reduce((sum, v) => sum + Math.min(0, v), 0);
    return Math.max(maximum, positive, Math.abs(negative));
  }, 0);
  return {
    rows,
    series,
    // Half-hour padding keeps the first and last bars whole, as in Charts.
    domain: [firstAt - 1800, lastAt + 1800] as [number, number],
    axisMaximum,
    total: hours.reduce((sum, h) => sum + h.usd, 0),
    current: current
      ? {
          at: current.at,
          confirmed: current.usd,
          remaining: current.projected!,
          projected: current.usd + current.projected!,
        }
      : null,
  };
}
