// My Macs: per-model rollup across a fleet (native/machines.py `models`).
export type FleetDemand = {
  load: number;
  warm: number;
  pressure: number;
  typicalPressure: number | null;
  ratio: number | null;
};
export type FleetModel = {
  model: string;
  macs: string[];
  ratePerHour: number | null;
  demand: FleetDemand | null;
};

const finite = (n: unknown): n is number =>
  typeof n === 'number' && Number.isFinite(n);

export function validFleetModels(v: unknown): v is FleetModel[] {
  return (
    Array.isArray(v) &&
    v.length <= 40 &&
    v.every(
      (m) =>
        !!m &&
        typeof m === 'object' &&
        typeof m.model === 'string' &&
        Array.isArray(m.macs) &&
        m.macs.every((id: unknown) => typeof id === 'string') &&
        (m.ratePerHour === null || finite(m.ratePerHour)) &&
        (m.demand === null ||
          (!!m.demand &&
            finite(m.demand.load) &&
            finite(m.demand.warm) &&
            finite(m.demand.pressure) &&
            (m.demand.ratio === null || finite(m.demand.ratio)))),
    )
  );
}

/** Plain words for network demand versus that model's usual week. */
export function demandLabel(d: FleetDemand | null): {
  text: string;
  tone: 'high' | 'normal' | 'low' | 'unknown';
} {
  if (!d) return { text: 'No demand reading', tone: 'unknown' };
  if (d.ratio === null)
    return {
      text: `${d.load} requests · ${d.warm} warm Macs`,
      tone: 'unknown',
    };
  if (d.ratio >= 1.5)
    return { text: `${d.ratio.toFixed(1)}× usual demand`, tone: 'high' };
  if (d.ratio <= 0.6)
    return { text: `${d.ratio.toFixed(1)}× usual · quiet`, tone: 'low' };
  return { text: 'Usual demand', tone: 'normal' };
}
