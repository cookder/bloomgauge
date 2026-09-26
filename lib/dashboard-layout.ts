export const dashboardWidgets = [
  { id: 'balance', title: 'Available balance', tile: true },
  { id: 'lifetime', title: 'Lifetime earnings', tile: true },
  { id: 'throughput', title: 'Output throughput', tile: true },
  { id: 'requests', title: 'Session requests', tile: true },
  { id: 'pulse', title: 'Live earnings & traffic', tile: false },
  { id: 'optimizer', title: 'Live optimizer', tile: false },
  { id: 'demand', title: 'Model demand', tile: false },
  { id: 'earnings', title: 'Earnings history', tile: false },
  { id: 'daily', title: 'Daily earnings', tile: false },
] as const;
export type DashboardWidgetId = (typeof dashboardWidgets)[number]['id'];
export type DashboardWidth = 'compact' | 'half' | 'full';
export type DashboardLayout = {
  order: DashboardWidgetId[];
  hidden: DashboardWidgetId[];
  widths: Record<DashboardWidgetId, DashboardWidth>;
};
export const dashboardLayoutKey = (mobile: boolean) =>
  `bloom.earnings-layout.v1.${mobile ? 'phone' : 'desktop'}`;
export function defaultDashboardLayout(mobile: boolean): DashboardLayout {
  return {
    order: dashboardWidgets.map((w) => w.id),
    hidden: mobile ? ['throughput', 'requests', 'earnings'] : [],
    widths: Object.fromEntries(
      dashboardWidgets.map((w) => [
        w.id,
        w.tile
          ? 'compact'
          : w.id === 'pulse' || w.id === 'optimizer'
            ? 'half'
            : 'full',
      ]),
    ) as DashboardLayout['widths'],
  };
}
export function readDashboardLayout(
  raw: string | null,
  mobile: boolean,
): DashboardLayout {
  const defaults = defaultDashboardLayout(mobile);
  if (!raw || raw.length > 10000) return defaults;
  try {
    const value: unknown = JSON.parse(raw);
    if (
      !value ||
      typeof value !== 'object' ||
      (value as { version?: unknown }).version !== 1
    )
      return defaults;
    const data = value as {
      order?: unknown;
      hidden?: unknown;
      widths?: unknown;
    };
    const validId = (id: unknown): id is DashboardWidgetId =>
      dashboardWidgets.some((w) => w.id === id);
    const order = Array.isArray(data.order)
      ? [...new Set(data.order.filter(validId))]
      : defaults.order;
    const hidden = Array.isArray(data.hidden)
      ? [...new Set(data.hidden.filter(validId))]
      : defaults.hidden;
    const widths = { ...defaults.widths };
    if (data.widths && typeof data.widths === 'object') {
      for (const w of dashboardWidgets) {
        const width = (data.widths as Record<string, unknown>)[w.id];
        if (
          width === 'full' ||
          width === 'half' ||
          (w.tile && width === 'compact')
        )
          widths[w.id] = width;
      }
    }
    // A card added in an update lands after its default predecessor, not at the end.
    const merged = [...order];
    defaults.order.forEach((id, index) => {
      if (merged.includes(id)) return;
      const before = defaults.order
        .slice(0, index)
        .reverse()
        .find((prev) => merged.includes(prev));
      merged.splice(before ? merged.indexOf(before) + 1 : 0, 0, id);
    });
    return { order: merged, hidden, widths };
  } catch {
    return defaults;
  }
}
export function serializeDashboardLayout(layout: DashboardLayout) {
  return JSON.stringify({ version: 1, ...layout });
}
/** Reorder the visible cards, retaining hidden cards and their preferences. */
export function moveDashboardWidget(
  layout: DashboardLayout,
  id: DashboardWidgetId,
  target: DashboardWidgetId,
): DashboardLayout {
  if (
    id === target ||
    layout.hidden.includes(id) ||
    layout.hidden.includes(target)
  )
    return layout;
  const order = [...layout.order],
    from = order.indexOf(id),
    to = order.indexOf(target);
  if (from < 0 || to < 0) return layout;
  order.splice(from, 1);
  order.splice(to, 0, id);
  return { ...layout, order };
}
