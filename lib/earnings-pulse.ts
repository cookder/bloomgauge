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
  peers?: PulseBenchmark | null;
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

/** What the Pulse rates against. There is no built-in pace: until this model set
 * has 30 settled warm minutes (native/live_earnings.py) the meter shows no rating,
 * and only this Mac's own history can suggest work isn't reaching it. */
export function pulseReference(
  mode: string,
  historical: number | null | undefined,
  custom: string,
) {
  const typed = custom.trim() === '' ? NaN : Number(custom);
  if (mode === 'reference')
    return Number.isFinite(typed) && typed > 0
      ? { value: typed, source: 'custom' as const, routing: false }
      : { value: null, source: null, routing: false };
  return historical != null && Number.isFinite(historical) && historical > 0
    ? { value: historical, source: 'history' as const, routing: true }
    : { value: null, source: null, routing: false };
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
    ? ['var(--c-91a497)', 'Waiting']
    : value < 1
      ? ['var(--c-87b9ff)', 'Below reference']
      : value < 2
        ? ['var(--c-82efb5)', 'Above reference']
        : value < 3
          ? ['var(--c-ffd079)', 'Running hot']
          : value < 5
            ? ['var(--c-ff9b64)', 'On fire']
            : ['var(--c-f68cce)', 'Supercharged'];
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
        ? 'Demand is usual or higher for this time of day, but pay is low. Work may not be reaching this Mac.'
        : ratio < 0.5 && pace < 0.5
          ? 'Network demand for this model is low for this time of day, which explains the slower pace.'
          : null;
  return {
    ratio,
    fraction: ratio == null ? null : Math.min(1, ratio / scale),
    label:
      ratio == null
        ? 'Network demand'
        : `Demand ${ratio >= 10 ? Math.round(ratio) : ratio.toFixed(1)}× usual for this time of day`,
    detail:
      ratio == null
        ? `${requests} · usual level still being measured`
        : requests,
    hint,
  };
}

/** "Macs like yours" (native/network_evidence.py `benchmark`): this Mac's requests per hour
 * on its model against the median of same-cell, same-model, same-level (dedicated or mixed)
 * Macs in Darkbloom's public counters, over the last `hours`. */
export type PulseBenchmark = {
  at: number;
  hours: number;
  cell: string;
  model: string;
  dedicated: boolean;
  windows: number;
  peers: number;
  percentile: number | null;
  reqPerHour: number;
  peerMedianReqPerHour: number;
  peerZeroShare: number | null;
  usdPerRequest: number | null;
  usdBasis: 'own' | 'list' | null;
  usdPerHour: number | null;
  peerUsdPerHour: number | null;
};
/** Below this many peers a median says little (network_evidence.MIN_PROVIDERS). */
export const PEER_BENCHMARK_MIN = 5;
/** Windows close every ~5 minutes; three missed ones make the comparison stale. */
export const PEER_BENCHMARK_FRESH_SECONDS = 900;

const ordinal = (n: number) => {
  const tens = n % 100;
  const suffix =
    tens >= 11 && tens <= 13
      ? 'th'
      : (['th', 'st', 'nd', 'rd'][n % 10] ?? 'th');
  return `${n}${suffix}`;
};
const perHour = (n: number) =>
  n >= 100 ? String(Math.round(n)) : n >= 10 ? n.toFixed(0) : n.toFixed(1);

/** The Pulse's benchmark line, or null to hide it: too few peers, stale windows, a model
 * that isn't serving now, or a malformed payload. */
export function pulseBenchmarkView(
  value: unknown,
  now: number,
  serving: string[] = [],
) {
  if (!value || typeof value !== 'object') return null;
  const b = value as Record<string, unknown>;
  if (
    typeof b.model !== 'string' ||
    typeof b.cell !== 'string' ||
    typeof b.dedicated !== 'boolean' ||
    !ok(b.at) ||
    !ok(b.hours) ||
    !ok(b.peers) ||
    !ok(b.reqPerHour) ||
    !ok(b.peerMedianReqPerHour) ||
    !(b.percentile === null || (ok(b.percentile) && b.percentile <= 1)) ||
    !(b.usdPerHour == null || ok(b.usdPerHour)) ||
    !(b.peerUsdPerHour == null || ok(b.peerUsdPerHour))
  )
    return null;
  if ((b.peers as number) < PEER_BENCHMARK_MIN) return null;
  if (now - (b.at as number) > PEER_BENCHMARK_FRESH_SECONDS) return null;
  if (serving.length && !serving.includes(b.model)) return null;
  const pct =
    b.percentile == null
      ? null
      : Math.min(99, Math.max(1, Math.round((b.percentile as number) * 100)));
  const own = b.reqPerHour as number,
    median = b.peerMedianReqPerHour as number,
    peers = Math.round(b.peers as number);
  const level = b.dedicated ? 'dedicated' : 'mixed';
  return {
    percentile: pct == null ? null : `${ordinal(pct)} percentile`,
    // Well above / below the median: 25% either way.
    tone:
      median > 0 && own >= median * 1.25
        ? ('above' as const)
        : own <= median * 0.75
          ? ('below' as const)
          : ('typical' as const),
    requests: `${perHour(own)} req/h vs ${perHour(median)} median`,
    peers: `${peers} Mac${peers === 1 ? '' : 's'}`,
    usdPerHour: (b.usdPerHour as number | null) ?? null,
    peerUsdPerHour: (b.peerUsdPerHour as number | null) ?? null,
    title:
      `This Mac against ${peers} other ${b.cell.replace('|', ' · ')} GB Macs serving the same model (${level}) in Darkbloom’s public counters, over the last ${b.hours} h. ` +
      (b.usdBasis === 'own'
        ? 'Dollars use this Mac’s own recent pay per request.'
        : 'Dollars are estimates: list price × the network’s tokens per request.'),
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
