export type PulseCredit = {
  id: number;
  at: number;
  receivedAt: number;
  microUsd: number;
  model: string;
};
export type EarningsPulseData = {
  reporting?: import('./pulse-run-status').MultiModelReporting;
  at: number;
  streamId: string | null;
  sessionId: number | null;
  status: string;
  detail?: string;
  models: string[];
  updatedAt: number | null;
  pollSeconds: number;
  cacheSeconds: number;
  windows: Record<
    string,
    {
      ratePerHour: number | null;
      microUsd: number;
      seconds: number;
      end: number;
    }
  >;
  baseline: {
    ratePerHour: number | null;
    hours: number;
    models: string[];
    scope?: 'weekday_hour' | 'daytype_hour' | 'hour' | 'model' | 'learning';
    days?: number;
    totalHours?: number;
    totalDays?: number;
    localHour?: number;
    localWeekday?: number;
    timezone?: string;
    detail?: string;
  } | null;
  sessionMicroUsd: number | null;
  events: PulseCredit[];
  demand?: PulseDemand | null;
};
export type PulseCursor = { key: string; at: number; ids: number[] };

// Opening/resuming/reconnecting seeds the watermark. A backlog is never played
// as though the Mac just earned it. Scope changes also discard queued motion.
export function creditArrivals(
  previous: PulseCursor | null,
  pulse: EarningsPulseData,
  active: boolean,
) {
  const key = `${pulse.streamId ?? ''}:${pulse.sessionId ?? ''}`;
  const ids = pulse.events.map((event) => event.id);
  const seed =
    !active ||
    !previous ||
    previous.key !== key ||
    pulse.at - previous.at > 10 ||
    pulse.at < previous.at;
  const known = new Set(previous?.ids ?? []);
  const events = seed
    ? []
    : pulse.events.filter(
        (event) =>
          !known.has(event.id) &&
          event.microUsd > 0 &&
          Number.isSafeInteger(event.microUsd) &&
          event.receivedAt >= pulse.at - 45 &&
          event.receivedAt <= pulse.at + 2,
      );
  return {
    reset: seed,
    cursor: active
      ? {
          key,
          at: pulse.at,
          ids: [
            ...new Set([
              ...ids,
              ...(previous?.key === key ? previous.ids : []),
            ]),
          ].slice(0, 256),
        }
      : null,
    events,
  };
}

export function pulseComparison(
  rate: number | null | undefined,
  reference: number,
) {
  if (
    rate == null ||
    !Number.isFinite(rate) ||
    !Number.isFinite(reference) ||
    reference <= 0
  )
    return null;
  return {
    ratio: rate / reference,
    gauge: Math.min(2, Math.max(0, rate / reference)),
    percent: (rate / reference - 1) * 100,
  };
}

export function creditBursts(events: PulseCredit[]) {
  const individual = events
    .slice(0, 10)
    .map((event) => ({ ...event, count: 1 }));
  const rest = events.slice(10);
  if (rest.length)
    individual.push({
      ...rest[0],
      microUsd: rest.reduce((sum, event) => sum + event.microUsd, 0),
      count: rest.length,
      model: 'Credit batch',
    });
  return individual;
}

// Earnings intensity only; these colors do not describe hardware temperature.
export function pulseDial(ratio: number | null | undefined, previousScale = 4) {
  const valid = ratio != null && Number.isFinite(ratio);
  const value = valid ? Math.max(0, ratio) : 0;
  // Expand before the needle reaches the stop, but only contract once the
  // previous range is mostly empty. Small pace changes must not halve/double
  // the visual scale on every reading near a boundary.
  let scale =
    Number.isFinite(previousScale) && previousScale >= 4 ? previousScale : 4;
  if (valid) {
    while (value > scale * 0.8 && scale < Number.MAX_VALUE / 2) scale *= 2;
    while (scale > 4 && value < scale * 0.3) scale /= 2;
  }
  const band = !valid
    ? ['#91a497', 'Waiting']
    : value < 1
      ? ['#87b9ff', 'Below reference']
      : value < 2
        ? ['#82efb5', 'Above reference']
        : value < 3
          ? ['#ffd079', 'Running hot']
          : value < 5
            ? ['#ff9b64', 'On fire']
            : ['#f68cce', 'Supercharged'];
  return {
    scale,
    fraction: Math.min(1, value / scale),
    color: band[0],
    label: band[1],
  };
}

/** Network demand for the serving model (native/pulse_demand.py). */
export type PulseDemand = {
  model: string;
  at: number;
  load: number;
  warm: number;
  pressure: number;
  typicalPressure: number | null;
  typicalSamples: number;
  ratio: number | null;
};
const ok = (v: unknown): v is number =>
  typeof v === 'number' && Number.isFinite(v) && v >= 0;

/** The demand arc on the Pulse meter, on the needle's own × scale; null hides it. */
export function pulseDemandView(
  value: unknown,
  scale: number,
  paceRatio: number | null | undefined,
  now: number,
) {
  if (!value || typeof value !== 'object') return null;
  const d = value as Record<string, unknown>;
  if (
    typeof d.model !== 'string' ||
    !ok(d.at) ||
    !ok(d.load) ||
    !ok(d.warm) ||
    !ok(d.pressure) ||
    !(d.ratio === null || ok(d.ratio)) ||
    !(d.typicalPressure === null || ok(d.typicalPressure))
  )
    return null;
  // The collector drops readings older than 2 minutes; allow for a paused view.
  if (now - d.at > 300) return null;
  const load = d.load as number,
    warm = d.warm as number,
    ratio = d.ratio as number | null;
  const requests = `${Math.round(load)} request${Math.round(load) === 1 ? '' : 's'} on ${Math.round(warm)} warm Mac${Math.round(warm) === 1 ? '' : 's'}`;
  const pace =
    paceRatio != null && Number.isFinite(paceRatio) ? paceRatio : null;
  const hint =
    ratio == null || pace == null
      ? null
      : ratio >= 0.8 && pace < 0.5
        ? 'Demand is normal or higher but pay is low. Work may not be reaching this Mac.'
        : ratio < 0.5 && pace < 0.5
          ? 'Network demand for this model is low, which explains the slower pace.'
          : null;
  return {
    ratio,
    fraction: ratio == null ? null : Math.min(1, ratio / scale),
    label:
      ratio == null
        ? 'Network demand'
        : `Demand ${ratio >= 10 ? Math.round(ratio) : ratio.toFixed(1)}× usual`,
    detail:
      ratio == null
        ? `${requests} · usual level still being measured`
        : requests,
    hint,
  };
}

/** The Pulse meter's pace can read in cents (20.53¢) or dollars ($0.21) per hour;
 * cents are the default. The choice is kept per device. */
export type PaceUnit = 'cents' | 'dollars';
export const paceUnitKey = 'bloom.pulse.pace-unit.v1';
export function readPaceUnit(value: unknown): PaceUnit {
  return value === 'dollars' ? 'dollars' : 'cents';
}
const centsPerHour = new Intl.NumberFormat('en-US', {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});
/** Cents with two decimals, so small paces (1.20¢) never read as zero. */
export function paceCents(usdPerHour: number | null | undefined) {
  return usdPerHour == null || !Number.isFinite(usdPerHour)
    ? '—'
    : `${centsPerHour.format(usdPerHour * 100)}¢`;
}
