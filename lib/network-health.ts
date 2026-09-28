// "It's not you": the Darkbloom-wide outage notice on Overview (Mac and phone).
// Reads /api/snapshot `networkHealth` (native/network_health.py view()); anything
// malformed shows no banner rather than a wrong one.

export type NetworkOutage = {
  since: number;
  scope: 'network' | 'model' | 'cell';
  key: string | null;
  dropPct: number;
  detail: string;
  /** What it was measured on; 'api' = Darkbloom's servers answered with errors. */
  signal: 'api' | 'warm' | 'live' | 'model' | 'cell' | null;
};

const record = (v: unknown): v is Record<string, unknown> =>
  !!v && typeof v === 'object' && !Array.isArray(v);
const finite = (v: unknown): v is number =>
  typeof v === 'number' && Number.isFinite(v);
const SCOPES = ['network', 'model', 'cell'];
const SIGNALS = ['api', 'warm', 'live', 'model', 'cell'];

export function networkOutage(health: unknown): NetworkOutage | null {
  const o = record(health) ? health.outage : null;
  if (
    !record(o) ||
    !finite(o.since) ||
    o.since <= 0 ||
    !SCOPES.includes(o.scope as string) ||
    !(o.key === null || typeof o.key === 'string') ||
    (o.scope !== 'network' && !o.key) ||
    !finite(o.dropPct) ||
    o.dropPct < 0 ||
    o.dropPct > 100 ||
    typeof o.detail !== 'string'
  )
    return null;
  const signal =
    typeof o.signal === 'string' && SIGNALS.includes(o.signal)
      ? (o.signal as NetworkOutage['signal'])
      : null;
  return {
    since: o.since,
    scope: o.scope as NetworkOutage['scope'],
    key: o.key as string | null,
    dropPct: o.dropPct,
    detail: o.detail,
    signal,
  };
}

/** 'HH:MM' in the viewer's time zone. */
export const clockTime = (seconds: number) =>
  new Date(seconds * 1000).toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
  });

/**
 * The banner sentence (native/network_health.py describe() words the log the same way):
 * "Darkbloom network problem since HH:MM: X% of Macs stopped getting work. Nothing to
 * fix on this Mac."
 */
export function outageMessage(
  outage: NetworkOutage,
  time: (seconds: number) => string = clockTime,
  model: (id: string) => string = (id) => id,
): string {
  const head = `Darkbloom network problem since ${time(outage.since)}: `;
  const tail = ' Nothing to fix on this Mac.';
  if (outage.signal === 'api')
    return `${head}Darkbloom’s servers stopped answering, so Macs aren’t getting work.${tail}`;
  const who =
    outage.scope === 'model' && outage.key
      ? `of Macs serving ${model(outage.key)}`
      : outage.scope === 'cell' && outage.key
        ? `of ${cellLabel(outage.key)} Macs`
        : 'of Macs';
  return `${head}${Math.round(outage.dropPct)}% ${who} stopped getting work.${tail}`;
}

/**
 * What Bloomkeeper does about it (native/network_health.py stall_wait): a network-wide or
 * hardware-type problem holds restarts and model switches; one model's problem holds only
 * a restart for that model, since moving to another model can still help.
 */
export function outageEffect(outage: NetworkOutage): string {
  const reports = 'automatic problem reports wait until it’s over.';
  return outage.scope === 'model'
    ? `Bloomkeeper won’t restart the provider for it; moving to another model is still allowed, and ${reports}`
    : `Bloomkeeper won’t restart or switch models because of it, and ${reports}`;
}

/** 'M5 Pro|48' -> 'M5 Pro 48 GB'. */
export function cellLabel(cell: string): string {
  const [name, memory] = [
    cell.slice(0, cell.lastIndexOf('|')),
    cell.slice(cell.lastIndexOf('|') + 1),
  ];
  return name && /^\d+$/.test(memory) ? `${name} ${memory} GB` : cell;
}
