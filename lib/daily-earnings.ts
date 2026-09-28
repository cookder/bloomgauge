export type DailyEarning = {
  date: string;
  at: number;
  end: number;
  from: number;
  to: number;
  usd: number;
  inferenceUsd: number;
  baseUsd: number;
  covered: boolean;
  status: 'complete' | 'today' | 'partial' | 'settling' | 'unknown';
};
export type DailyEarnings = {
  at: number;
  from: number;
  to: number;
  timezone: string;
  model: string | null;
  includesBase: boolean;
  historyStart: number | null;
  days: DailyEarning[];
  chartTruncated: boolean;
  models: string[];
  usd: number;
  inferenceUsd: number;
  baseUsd: number;
  completeDays: number;
  averageDayUsd: number | null;
  scope: string;
};
export function validDailyEarnings(v: unknown): v is DailyEarnings {
  if (!v || typeof v !== 'object') return false;
  const d = v as DailyEarnings;
  return (
    [
      'at',
      'from',
      'to',
      'usd',
      'inferenceUsd',
      'baseUsd',
      'completeDays',
    ].every((k) => Number.isFinite(d[k as keyof DailyEarnings])) &&
    d.to > d.from &&
    typeof d.timezone === 'string' &&
    typeof d.scope === 'string' &&
    typeof d.includesBase === 'boolean' &&
    typeof d.chartTruncated === 'boolean' &&
    (d.averageDayUsd === null || Number.isFinite(d.averageDayUsd)) &&
    (d.historyStart === null || Number.isFinite(d.historyStart)) &&
    (d.model === null || typeof d.model === 'string') &&
    Array.isArray(d.models) &&
    d.models.every((m) => typeof m === 'string') &&
    Array.isArray(d.days) &&
    d.days.length <= 366 &&
    d.days.every(
      (h) =>
        h &&
        /^\d{4}-\d{2}-\d{2}$/.test(h.date) &&
        ['at', 'end', 'from', 'to', 'usd', 'inferenceUsd', 'baseUsd'].every(
          (k) => Number.isFinite(h[k as keyof DailyEarning]),
        ) &&
        h.end > h.at &&
        h.to > h.from &&
        h.from >= h.at &&
        h.to <= h.end &&
        typeof h.covered === 'boolean' &&
        ['complete', 'today', 'partial', 'settling', 'unknown'].includes(
          h.status,
        ),
    )
  );
}
export type EarningsTone =
  | 'quiet'
  | 'steady'
  | 'green'
  | 'purple'
  | 'gold'
  | 'neutral'
  | 'unknown';
/** This Mac's own day scale: the 25th, 50th, 75th and 90th percentiles of its
 * last 30 complete days. Macs earn very different amounts, so no fixed dollar
 * tiers; with fewer than 7 complete days (or no spread) there is no scale. */
export type EarningsTiers = {
  steady: number;
  green: number;
  purple: number;
  gold: number;
  days: number;
};
export const TIER_MIN_DAYS = 7;
export function earningsTiers(
  days: DailyEarning[] | null | undefined,
): EarningsTiers | null {
  const values = (days ?? [])
    .filter((d) => d.status === 'complete' && Number.isFinite(d.usd))
    .sort((a, b) => a.at - b.at)
    .slice(-30)
    .map((d) => d.usd)
    .sort((a, b) => a - b);
  if (values.length < TIER_MIN_DAYS) return null;
  const at = (p: number) => {
    const i = (values.length - 1) * p,
      low = Math.floor(i);
    return values[low] + (values[Math.ceil(i)] - values[low]) * (i - low);
  };
  const tiers = {
    steady: at(0.25),
    green: at(0.5),
    purple: at(0.75),
    gold: at(0.9),
    days: values.length,
  };
  return tiers.gold > tiers.steady ? tiers : null;
}
export function earningsTone(
  usd: number | null | undefined,
  hours = 24,
  tiers: EarningsTiers | null = null,
): EarningsTone {
  if (
    usd == null ||
    !Number.isFinite(usd) ||
    !Number.isFinite(hours) ||
    hours <= 0
  )
    return 'unknown';
  if (!tiers) return 'neutral';
  const daily = (usd * 24) / hours;
  if (daily <= 0) return 'quiet';
  if (daily >= tiers.gold) return 'gold';
  if (daily >= tiers.purple) return 'purple';
  if (daily >= tiers.green) return 'green';
  if (daily >= tiers.steady) return 'steady';
  return 'quiet';
}
export const earningsToneLabel: Record<EarningsTone, string> = {
  quiet: 'Quiet',
  steady: 'Steady',
  green: 'Good',
  purple: 'Great',
  gold: 'Outstanding',
  neutral: 'Not rated yet',
  unknown: 'Unavailable',
};
export function dayTone(
  day: DailyEarning,
  tiers: EarningsTiers | null = null,
): EarningsTone {
  if (day.status === 'unknown') return 'unknown';
  return earningsTone(day.usd, 24, tiers);
}
// Whole local calendar dates, including today, rather than rolling 24h bins.
export function dailyBounds(
  range: { preset: string; start?: number; end?: number },
  now = new Date(),
) {
  const end =
    range.preset === 'custom'
      ? (range.end ?? now.getTime() / 1000)
      : now.getTime() / 1000;
  if (range.preset === 'custom')
    return { start: range.start ?? end - 86400, end };
  if (range.preset === 'all') return { start: 0, end };
  const day = new Date(now);
  day.setHours(0, 0, 0, 0);
  day.setDate(day.getDate() - (Number.parseInt(range.preset) || 30) + 1);
  return { start: day.getTime() / 1000, end };
}
