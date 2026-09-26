import type { OptimizerLive } from './optimizer-live';

export type GlanceReason = 'serving' | 'target' | 'busiest' | 'candidate';
export const glanceReasonLabel: Record<GlanceReason, string> = {
  serving: 'serving',
  target: 'trial or switch target',
  busiest: 'busiest',
  candidate: 'optimizer candidate',
};
const GLANCE_LIMIT = 7;

/** Models for the dashboard demand chart: the three busiest, plus anything the
 * optimizer is serving, trialling, proposing or weighing, capped for legibility. */
export function glanceModels(
  models: { id: string; averageLoad: number }[],
  live: OptimizerLive | null,
  limit = GLANCE_LIMIT,
) {
  const known = new Map(models.map((m) => [m.id, m]));
  const picked = new Map<string, GlanceReason>();
  const add = (id: string | null | undefined, reason: GlanceReason) => {
    if (id && known.has(id) && !picked.has(id) && picked.size < limit)
      picked.set(id, reason);
  };
  add(live?.currentModel, 'serving');
  for (const id of [
    live?.pendingTarget,
    live?.proposalTarget,
    live?.measurement?.model,
    live?.comparisonTarget,
  ])
    add(id, 'target');
  [...models]
    .sort((a, b) => b.averageLoad - a.averageLoad)
    .slice(0, 3)
    .forEach((m) => add(m.id, 'busiest'));
  (live?.candidates ?? [])
    .filter(
      (c) =>
        c.group === 'paid_alternative' ||
        c.group === 'comparison_target' ||
        c.eligible ||
        c.kind === 'explore',
    )
    .sort(
      (a, b) =>
        (known.get(b.model)?.averageLoad ?? 0) -
        (known.get(a.model)?.averageLoad ?? 0),
    )
    .forEach((c) => add(c.model, 'candidate'));
  return [...picked].map(([id, reason]) => ({ id, reason }));
}
