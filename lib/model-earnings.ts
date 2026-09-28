import {
  cumulativeEarnings,
  projectedCumulative,
  type CumulativePoint,
  type ModelProjection,
} from './cumulative-earnings';

export const UNATTRIBUTED = '__unattributed_earnings__';
export const ALL_EARNINGS = '__all_earnings__';
const colors = [
  'var(--c-82efb5)',
  'var(--c-87b9ff)',
  'var(--c-f3c57e)',
  'var(--c-f798b5)',
  'var(--c-78d6da)',
  'var(--c-c8adf5)',
  'var(--c-e3df85)',
  'var(--c-eaa07d)',
];
const known: Record<string, string> = {
  'gemma-4-26b-qat-4bit': colors[0],
  'qwen3.6-35b-a3b-vl-mtp-mxfp8': colors[1],
  'EigenLabs/Qwen3.8-27B-4bit-mtp': colors[2],
  'gpt-oss-20b': colors[3],
  'Qwen3.5-9B': colors[6],
  'qwen3.5-35b-a3b': colors[4],
  'qwen3-vl-30b-a3b-instruct': colors[5],
  base_reward: 'var(--c-8493a8)',
  [UNATTRIBUTED]: 'var(--c-b5aa96)',
};

export function modelColor(model: string): string {
  if (Object.hasOwn(known, model)) return known[model];
  let hash = 0;
  for (const character of model)
    hash = (hash * 31 + character.charCodeAt(0)) >>> 0;
  return colors[hash % colors.length];
}

// Signed adjustments and zero totals do not have a meaningful positive share.
// Keep microdollar precision; never clamp a negative amount into a proportion.
export function earningsShares(amounts: number[]): number[] | null {
  if (!amounts.length || amounts.some((value) => !Number.isFinite(value)))
    return null;
  const micro = amounts.map((value) => Math.round(value * 1e6));
  const total = micro.reduce((sum, value) => sum + value, 0);
  return total > 0 && micro.every((value) => value >= 0)
    ? micro.map((value) => (value / total) * 100)
    : null;
}

export type EarningsHour = {
  at: number;
  usd: number;
  categories: Record<string, number>;
};
export type TrackedEarningsHour = EarningsHour & {
  jobs: number | null;
  categoryJobs?: Record<string, number>;
};

// Filter the ledger before calculating any chart, summary, or job average.
// Category counts come from Monitor; older records may not include them.
export function filterEarningsHours(
  hours: TrackedEarningsHour[],
  model: string,
): TrackedEarningsHour[] {
  if (model === ALL_EARNINGS) return hours;
  return hours.map((h) => {
    const amounts = Object.fromEntries(
      Object.entries(h.categories).map(([key, value]) => [
        key,
        Math.round(value * 1e6),
      ]),
    );
    const remainder =
      Math.round(h.usd * 1e6) -
      Object.values(amounts).reduce((a, b) => a + b, 0);
    const usd =
      ((Object.hasOwn(amounts, model) ? amounts[model] : 0) +
        (model === UNATTRIBUTED ? remainder : 0)) /
      1e6;
    const recordedJobs =
      h.categoryJobs && Object.hasOwn(h.categoryJobs, model)
        ? h.categoryJobs[model]
        : undefined;
    const inferenceCategories = Object.keys(h.categories).filter(
      (key) => key !== 'base_reward',
    );
    const knownCount = Object.entries(h.categoryJobs ?? {})
      .filter(
        ([key, count]) =>
          key !== 'base_reward' && Number.isInteger(count) && count >= 0,
      )
      .reduce((sum, [, count]) => sum + count, 0);
    const countsContradict = h.jobs != null && knownCount > h.jobs;
    const countsComplete =
      !remainder &&
      h.categoryJobs &&
      inferenceCategories.every(
        (key) =>
          Object.hasOwn(h.categoryJobs!, key) &&
          Number.isInteger(h.categoryJobs![key]) &&
          h.categoryJobs![key] >= 0,
      ) &&
      inferenceCategories.reduce(
        (sum, key) => sum + h.categoryJobs![key],
        0,
      ) === h.jobs;
    const jobs =
      model === 'base_reward' || model === UNATTRIBUTED
        ? null
        : recordedJobs != null &&
            !countsContradict &&
            Number.isInteger(recordedJobs) &&
            recordedJobs >= 0
          ? recordedJobs
          : !Object.hasOwn(h.categories, model) && countsComplete
            ? 0
            : null;
    return { ...h, usd, jobs, categories: { [model]: usd } };
  });
}

export function earningsMetrics(
  hours: TrackedEarningsHour[],
  observedAt: number,
  coverageStartedAt?: number | null,
  coverageIntervals: { start: number; end: number }[] = [],
  unlocatedGaps = false,
) {
  return [...hours]
    .sort((a, b) => a.at - b.at)
    .map((h) => {
      const seconds = Math.max(
        0,
        Math.min(h.at + 3600, observedAt) -
          Math.max(h.at, coverageStartedAt ?? h.at),
      );
      const work = h.usd - (h.categories.base_reward ?? 0);
      // Hourly money cannot be split around unknown intervals. Preserve it,
      // but do not interpret missing time as observed zero-income runtime.
      const rateComplete =
        !unlocatedGaps &&
        !coverageIntervals.some(
          (gap) =>
            gap.start < Math.min(h.at + 3600, observedAt) &&
            gap.end > Math.max(h.at, coverageStartedAt ?? h.at),
        );
      return {
        ...h,
        seconds,
        rateComplete,
        perMinute: seconds && rateComplete ? h.usd / (seconds / 60) : null,
        perHour: seconds && rateComplete ? h.usd / (seconds / 3600) : null,
        perJob: h.jobs ? work / h.jobs : null,
      };
    });
}

// Joint forecasts cannot be divided into guessed single-model rates.
export function filterModelProjection(
  projection: ModelProjection | undefined,
  model: string,
): ModelProjection | undefined {
  if (model === ALL_EARNINGS) return projection;
  return model !== 'base_reward' &&
    model !== UNATTRIBUTED &&
    projection?.models.length === 1 &&
    projection.models[0] === model
    ? projection
    : undefined;
}
export function projectedEarningsHour(
  hours: EarningsHour[],
  projection: ModelProjection | undefined,
  window: {
    start: number;
    end: number;
    at: number;
    observedAt: number;
    sourceReady: boolean;
  },
): number | null {
  const hour = hours.find((h) => h.at === window.start);
  if (
    !window.sourceReady ||
    !hour ||
    !projection ||
    projection.asOf < window.start ||
    window.observedAt < window.start ||
    projection.asOf >= window.end ||
    window.at - projection.asOf > 180
  )
    return null;
  return (
    projectedCumulative(
      [{ at: window.observedAt, cumulative: hour.usd }],
      projection,
      window.end,
    ).at(-1)?.predicted ?? null
  );
}

export type ModelEarningsRow = {
  at: number;
  byModel: Record<string, number | null>;
};

export function modelEarnings(
  hours: EarningsHour[],
  observedAt: number,
  coverageStartedAt?: number | null,
) {
  // Use integer microdollars, and retain any unlabelled remainder explicitly.
  // Old hours can have a total without a complete model breakdown.
  const normalized = hours
    .map((h) => {
      const categories: Record<string, number> = Object.assign(
        Object.create(null),
        Object.fromEntries(
          Object.entries(h.categories).map(([model, usd]) => [
            model,
            Math.round(usd * 1e6),
          ]),
        ),
      );
      const remaining =
        Math.round(h.usd * 1e6) -
        Object.values(categories).reduce((a, b) => a + b, 0);
      if (remaining)
        categories[UNATTRIBUTED] = (categories[UNATTRIBUTED] ?? 0) + remaining;
      return { ...h, categories };
    })
    .sort((a, b) => a.at - b.at);
  const names = [
    ...new Set(normalized.flatMap((h) => Object.keys(h.categories))),
  ].sort();
  const series = names.map((model, i) => ({
    model,
    key: `model_${i}`,
    color: modelColor(model),
    usd:
      normalized.reduce((total, h) => total + (h.categories[model] ?? 0), 0) /
      1e6,
  }));
  const hourly: ModelEarningsRow[] = normalized.map((h) => ({
    at: h.at,
    byModel: Object.fromEntries(
      series.map((s) => [s.key, (h.categories[s.model] ?? 0) / 1e6]),
    ),
  }));
  const cumulative: ModelEarningsRow[] = cumulativeEarnings(
    normalized,
    observedAt,
    coverageStartedAt,
  ).map((p) => ({ at: p.at, byModel: {} }));
  for (const s of series) {
    const running = cumulativeEarnings(
      normalized.map((h) => ({
        at: h.at,
        usd: (h.categories[s.model] ?? 0) / 1e6,
      })),
      observedAt,
      coverageStartedAt,
    );
    running.forEach((p, i) => {
      cumulative[i].byModel[s.key] = p.cumulative;
    });
  }
  return { series, hourly, cumulative };
}

// Use the same confirmed series the forecast continues on screen. Other
// models, base rewards, and unattributed credits must not raise its anchor.
export function cumulativeForModels(
  earnings: ReturnType<typeof modelEarnings>,
  models: string[],
  actual: CumulativePoint[],
): CumulativePoint[] {
  if (!models.length) return [];
  const keys = earnings.series
    .filter((s) => models.includes(s.model))
    .map((s) => s.key);
  return actual.map((p, i) => ({
    at: p.at,
    cumulative:
      p.cumulative == null
        ? null
        : keys.reduce(
            (sum, key) =>
              sum +
              Math.round((earnings.cumulative[i]?.byModel[key] ?? 0) * 1e6),
            0,
          ) / 1e6,
  }));
}
