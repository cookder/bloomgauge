import {
  earningsMetrics,
  modelColor,
  modelEarnings,
  UNATTRIBUTED,
  type TrackedEarningsHour,
} from './model-earnings';
import type { NetworkContributions } from './network-contributions';

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
/** The Pulse's $/h bars: how far back, and "earned in the last …" over the same
 * span. 1 h and 3 h draw 5-minute bars (see shortEarningsBars); the rest clock hours. */
export const overviewHourlyWindows = [1, 3, 6, 12, 24, 72, 168] as const;
export type OverviewHourlyWindow = (typeof overviewHourlyWindows)[number];
export function readOverviewHourlyWindow(
  raw: string | null,
): OverviewHourlyWindow {
  return overviewHourlyWindows.find((h) => String(h) === raw) ?? 12;
}
/** "1 h", "6 h", "24 h", "3 days", "7 days". */
export function overviewHourlyLabel(
  hours: OverviewHourlyWindow,
  short = false,
) {
  if (hours < 48) return short ? `${hours}h` : `${hours} h`;
  return short ? `${hours / 24}d` : `${hours / 24} days`;
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

/** 1 h and 3 h: hourly bars are too coarse, so these draw 5-minute bars. */
export const shortBucketSeconds = 300;
export type ShortWindow = Extract<OverviewHourlyWindow, 1 | 3>;
export function isShortWindow(
  hours: OverviewHourlyWindow,
): hours is ShortWindow {
  return hours === 1 || hours === 3;
}
const OTHER_MODELS = '__other_models__';
const BUCKET = shortBucketSeconds;

/**
 * The span a short window reads from the recorded-credits report
 * (/api/network/contributions?metric=earnings, 1-minute points up to ~10 h):
 * the 5-minute clock bucket holding `to` and the ones before it, 12 for 1 h and
 * 36 for 3 h. `to` is `at` in whole 30 s steps so repeat polls reuse the
 * backend's 30 s cache.
 */
export function shortWindowSpan(at: number, windowHours: ShortWindow) {
  const to = Math.floor(at / 30) * 30;
  const last = Math.ceil(to / BUCKET) * BUCKET - BUCKET;
  return { from: last - (windowHours * 12 - 1) * BUCKET, to };
}

type CreditReport = Pick<
  NetworkContributions,
  'from' | 'to' | 'series' | 'points'
> &
  Partial<Pick<NetworkContributions, 'status' | 'baseRewards'>>;

/** The report's account-wide base rewards (USD per elapsed hour, one per
 * point), or null when absent or malformed (then the bars are inference only). */
export function baseRewardValues(
  report: CreditReport | null | undefined,
): (number | null)[] | null {
  const base = report?.baseRewards as unknown;
  if (!report || !base || typeof base !== 'object') return null;
  const { attribution, values } = base as Record<string, unknown>;
  return attribution === 'account' &&
    Array.isArray(values) &&
    values.length === report.points.length &&
    values.every(
      (v) => v === null || (typeof v === 'number' && Number.isFinite(v)),
    )
    ? (values as (number | null)[])
    : null;
}

/**
 * 5-minute bars in dollars per hour (the bucket's confirmed credits × 12),
 * stacked by model. The source is the recorded-credits report behind the
 * Network tab's "My earnings" bars (and the Pulse's former 2-minute bars):
 * inference credits per model, plus the report's account-wide base rewards as
 * their own "base_reward" series (as the hourly bars count them).
 *
 * Money is summed in integer microdollars from whole 1-minute points. A finished
 * bucket with a minute below the report's 80% poll coverage, or a point that
 * crosses its edge, stays blank (never zero), and `total` counts only what the
 * bars show, so the headline always equals the bars. Only the bucket in progress
 * holds back its newest, not-yet-polled minutes (it is drawn "so far"). An
 * unavailable report records nothing (`recorded` false: the headline shows "—").
 */
export function shortEarningsBars(
  report: CreditReport | null | undefined,
  windowHours: ShortWindow,
) {
  const count = windowHours * (3600 / BUCKET);
  const end = report?.to ?? 0;
  const last = Math.ceil(end / BUCKET) * BUCKET - BUCKET;
  const first = last - (count - 1) * BUCKET;
  const base = baseRewardValues(report);
  const sources = [
    ...(report?.series ?? []),
    ...(base ? [{ id: 'base_reward', name: 'base_reward' }] : []),
  ];
  const points = (report?.points ?? []).map((p, i) =>
    base ? { ...p, values: [...p.values, base[i]] } : p,
  );
  const width = sources.length;
  // The bucket in progress starts at `last` unless `end` is on a 5-minute mark
  // (then every drawn bucket is finished). Its trailing unpolled minutes are
  // "still recording"; a finished bucket never borrows that allowance.
  const current = last + BUCKET > end ? last : end;
  let recordedTo = end;
  for (let i = points.length - 1; i >= 0; i--) {
    const p = points[i];
    if (p.from < current - 1e-3 || p.values.some((v) => v !== null)) break;
    recordedTo = p.from;
  }
  const unavailable = report?.status === 'unavailable';
  const buckets = Array.from({ length: count }, (_, i) => ({
    from: first + i * BUCKET,
    to: Math.min(first + (i + 1) * BUCKET, recordedTo),
    seconds: 0,
    broken: false,
    micro: Array.from({ length: width }, () => 0 as number | null),
  }));
  for (const p of report ? points : []) {
    if (p.to <= first || p.from >= first + count * BUCKET) continue;
    if (p.from >= recordedTo - 1e-3) continue;
    const bucket = buckets[Math.floor((p.from - first) / BUCKET)];
    if (
      !bucket ||
      p.from < bucket.from - 1e-3 ||
      p.to > bucket.from + BUCKET + 1e-3
    ) {
      for (const b of buckets)
        if (p.from < b.from + BUCKET && p.to > b.from) b.broken = true;
      continue;
    }
    const duration = p.to - p.from;
    bucket.seconds += duration;
    p.values.forEach((v, k) => {
      if (v === null) bucket.micro[k] = null;
      else if (bucket.micro[k] !== null)
        bucket.micro[k]! += Math.round(((v * duration) / 3600) * 1e6);
    });
  }
  // Drawn only when every second is in the report and some model's money is known.
  const known = buckets.map(
    (b) =>
      !unavailable &&
      b.to > b.from &&
      !b.broken &&
      Math.abs(b.seconds - (b.to - b.from)) < 1e-3 &&
      b.micro.some((v) => v !== null),
  );
  const all = sources.map((s, k) => {
    const other = s.id === 'other' || s.id === 'unattributed';
    const micro = buckets.map((b, i) => (known[i] ? b.micro[k] : null));
    return {
      model:
        s.id === 'unattributed'
          ? UNATTRIBUTED
          : s.id === 'other'
            ? OTHER_MODELS
            : s.name,
      label: s.id === 'other' ? s.name : undefined,
      key: `model_${k}`,
      color: other ? 'var(--c-899da8)' : modelColor(s.name),
      micro,
      usd: micro.reduce<number>((sum, v) => sum + (v ?? 0), 0) / 1e6,
    };
  });
  // Series with nothing drawn in this window (e.g. an empty "Unattributed").
  const series = all
    .filter((s) => s.micro.some((v) => v !== null && v !== 0))
    .sort((a, b) => b.usd - a.usd);
  const rows = buckets.map((b, i) => {
    const byModel = Object.fromEntries(
      series.map((s) => [
        s.key,
        s.micro[i] === null ? null : (s.micro[i]! / 1e6) * (3600 / BUCKET),
      ]),
    );
    const micro = series.reduce((sum, s) => sum + (s.micro[i] ?? 0), 0);
    return {
      at: b.from,
      end: b.from + BUCKET,
      /** Recorded through (the in-progress bucket ends early). */
      through: Math.max(b.from, b.to),
      inProgress: b.from + BUCKET > end,
      known: known[i],
      /** Dollars per hour, all drawn models. */
      perHour: known[i] ? (micro / 1e6) * (3600 / BUCKET) : null,
      usd: known[i] ? micro / 1e6 : null,
      byModel,
      projected: null as number | null,
    };
  });
  const axisMaximum = rows.reduce((maximum, row) => {
    const amounts = Object.values(row.byModel).filter(
      (v): v is number => typeof v === 'number' && Number.isFinite(v),
    );
    const positive = amounts.reduce((sum, v) => sum + Math.max(0, v), 0);
    const negative = amounts.reduce((sum, v) => sum + Math.min(0, v), 0);
    return Math.max(maximum, positive, Math.abs(negative));
  }, 0);
  return {
    rows,
    series,
    bucketSeconds: BUCKET,
    domain: [first - BUCKET / 2, last + BUCKET / 2] as [number, number],
    axisMaximum,
    total:
      series.reduce(
        (sum, s) => sum + s.micro.reduce<number>((n, v) => n + (v ?? 0), 0),
        0,
      ) / 1e6,
    recordedTo,
    /** At least one bucket was recorded: the total means something (else "—"). */
    recorded: known.some(Boolean),
    /** Base rewards are in the bars (the report carried them). */
    baseRewards: !!base,
    /** Finished 5-minute buckets left blank (not fully recorded; not counted). */
    blanks: rows.filter((r) => !r.known && !r.inProgress).length,
  };
}

/** Local clock ticks every `minutes` inside `domain`: 10:15, 10:30… Steps real
 * time 5 minutes at a time and keeps local :00/:15/:30/:45 marks, so a repeated
 * DST fall-back hour gets its ticks too. */
export function clockTicks(domain: [number, number], minutes: number) {
  const ticks: number[] = [];
  for (
    let t = Math.ceil(domain[0] / BUCKET) * BUCKET;
    t <= domain[1];
    t += BUCKET
  )
    if (new Date(t * 1000).getMinutes() % minutes === 0) ticks.push(t);
  return ticks;
}

const clockFormat = new Intl.DateTimeFormat([], {
  hour: 'numeric',
  minute: '2-digit',
});
/** A 5-minute bar's tooltip title: "10:15–10:20", "… · so far" while in progress. */
export function shortBarLabel(row: {
  at: number;
  end: number;
  inProgress: boolean;
}) {
  return `${clockTime(row.at)}–${clockTime(row.end)}${row.inProgress ? ' · so far' : ''}`;
}

/** "10:15" (no AM/PM), in the viewer's locale and time zone. */
export function clockTime(at: number) {
  const parts = clockFormat.formatToParts(new Date(at * 1000));
  const hour = parts.findIndex((p) => p.type === 'hour');
  const minute = parts.findIndex((p) => p.type === 'minute');
  if (hour < 0 || minute < 0) return clockFormat.format(new Date(at * 1000));
  return parts
    .slice(Math.min(hour, minute), Math.max(hour, minute) + 1)
    .map((p) => p.value)
    .join('');
}
