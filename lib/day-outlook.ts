import type { DailyEarnings } from './daily-earnings';
import type { ModelProjection } from './cumulative-earnings';

export type DayOutlook = {
  status: 'ready' | 'learning' | 'unavailable' | 'stale';
  detail: string;
  total: number | null;
  confirmed: number | null;
  additionalInference: number | null;
  additionalBase: number | null;
  includesBase: boolean;
  baseEstimated: boolean;
  baseDays: number;
  end: number | null;
  at: number | null;
  hours: number;
  days: number;
  models: string[];
};

// This combines a confirmed ledger total with a separate, conditional forecast.
// It never modifies daily cells, creates credits, or feeds optimizer decisions.
export function dayOutlook(
  history: DailyEarnings | null | undefined,
  projection: ModelProjection | null | undefined,
  now: number,
  connected = true,
  paused = false,
): DayOutlook {
  const empty: DayOutlook = {
    status: 'unavailable',
    detail: 'Waiting for today’s confirmed earnings.',
    total: null,
    confirmed: null,
    additionalInference: null,
    additionalBase: null,
    includesBase: history?.includesBase === true,
    baseEstimated: false,
    baseDays: 0,
    end: null,
    at: history?.at ?? null,
    hours: 0,
    days: 0,
    models: [],
  };
  if (!history || !Number.isFinite(now)) return empty;
  const today = history.days.find(
    (d) => d.at <= history.at && history.at < d.end,
  );
  if (!today) return empty;
  const value = { ...empty, confirmed: today.usd, end: today.end };
  if (!paused && (now < today.at || now >= today.end)) return empty;
  if (
    !connected ||
    (!paused &&
      (Math.abs(now - history.at) > 90 || history.at - history.to > 90))
  )
    return {
      ...value,
      status: 'stale',
      detail: 'Waiting for fresh earnings before estimating the day’s total.',
    };
  if (
    !today.covered ||
    today.status === 'unknown' ||
    today.from !== today.at ||
    today.to < history.at - 90
  )
    return {
      ...value,
      detail:
        'Today has incomplete credit coverage. A full-day estimate would be misleading.',
    };
  if (!projection || projection.status !== 'ready')
    return {
      ...value,
      status: projection?.status === 'learning' ? 'learning' : 'unavailable',
      detail:
        projection?.detail ||
        'Waiting for a warm, ready model and enough settled earnings history.',
    };
  if (
    !Array.isArray(projection.models) ||
    !projection.models.length ||
    projection.models.some((m) => typeof m !== 'string') ||
    ![projection.asOf, projection.hours, projection.days].every(
      Number.isFinite,
    ) ||
    projection.hours < 0.5 ||
    projection.days < 1
  )
    return {
      ...value,
      status: 'learning',
      detail: 'Learning the current model’s verified earnings history.',
    };
  if (
    history.model &&
    history.model !== '@inference' &&
    (projection.models.length !== 1 || projection.models[0] !== history.model)
  )
    return {
      ...value,
      detail:
        'This model is not running on its own. Choose All earnings or Inference only for the current model selection’s outlook.',
    };
  if (
    Math.abs(projection.asOf - history.at) > 180 ||
    (!paused && Math.abs(now - projection.asOf) > 180)
  )
    return {
      ...value,
      status: 'stale',
      detail: 'Waiting for a fresh model forecast to match today’s credits.',
    };
  const points = projection.points;
  if (
    !Array.isArray(points) ||
    points.length < 2 ||
    points.length > 300 ||
    points.some(
      (p, i) =>
        !p ||
        !Number.isFinite(p.at) ||
        !Number.isFinite(p.additional) ||
        p.additional < 0 ||
        (i > 0 &&
          (p.at <= points[i - 1].at ||
            p.additional < points[i - 1].additional)),
    ) ||
    points[0].at !== projection.asOf ||
    points[0].additional !== 0
  )
    return { ...value, detail: 'Waiting for a complete model forecast.' };
  if (points.at(-1)!.at < today.end)
    return {
      ...value,
      detail: 'The current forecast does not yet cover local midnight.',
    };
  const additionalAt = (at: number) => {
    const index = points.findIndex((p) => p.at >= at);
    // API snapshots arrive a few seconds apart. For that bounded gap only,
    // extend the first predicted segment back to the confirmed cutoff. This
    // remains an estimate, never an invented ledger reading.
    const right = Math.max(1, index),
      a = points[right - 1],
      b = points[right];
    return (
      a.additional +
      ((b.additional - a.additional) * (at - a.at)) / (b.at - a.at)
    );
  };
  const additionalInference = Math.max(
    0,
    additionalAt(today.end) - additionalAt(today.to),
  );
  // Base rewards arrive in batches. Estimate their full-day amount separately
  // from up to 14 completed days, then subtract base already credited today.
  // Incomplete days are never treated as zero or extrapolated into this prior.
  const prior = history.days
    .filter(
      (d) =>
        d.status === 'complete' &&
        d.covered &&
        d.from === d.at &&
        d.to === d.end &&
        d.end <= today.at,
    )
    .sort((a, b) => b.at - a.at)
    .slice(0, 14);
  let additionalBase = 0;
  const baseEstimated = !history.includesBase || prior.length >= 3;
  if (history.includesBase && baseEstimated) {
    const weighted = prior.map((d) => ({
      day: d,
      weight: Math.pow(0.5, (today.at - d.end) / (7 * 86400)),
    }));
    const rate =
      weighted.reduce((n, r) => n + r.day.baseUsd * r.weight, 0) /
      weighted.reduce((n, r) => n + (r.day.end - r.day.at) * r.weight, 0);
    additionalBase = Math.max(0, rate * (today.end - today.at) - today.baseUsd);
  }
  const total = today.usd + additionalInference + additionalBase;
  if (![total, additionalInference, additionalBase].every(Number.isFinite))
    return { ...value, detail: 'Waiting for a complete model forecast.' };
  return {
    ...value,
    status: 'ready',
    total,
    additionalInference,
    additionalBase,
    baseEstimated,
    baseDays: history.includesBase ? prior.length : 0,
    hours: projection.hours,
    days: projection.days,
    models: [...projection.models],
    detail:
      'Assumes the current model selection stays warm through local midnight. Uses its settled earnings history, weekday/weekend time slots and recent pace; a short spike fades over the remaining hours.',
  };
}
