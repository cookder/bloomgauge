// One plain sentence at the top of Overview: pace, model, today's total and what
// the optimizer is doing. Details stay in the cards below.
export type SummaryInput = {
  online: boolean | null;
  /** Darkbloom 0.9.10 loads its models at start, before it takes requests. */
  starting?: boolean;
  model: string | null;
  pace: number | null;
  today: number | null;
  phase: string | null;
  /** 'manager' uses the manager's words; anything else the legacy optimizer's. */
  strategy?: 'manager' | 'legacy' | null;
  money: (n: number) => string;
  name: (model: string) => string;
};
const PHASES: Record<string, string> = {
  off: 'optimizer off',
  waiting: 'optimizer waiting for fresh comparisons',
  watching: 'optimizer watching demand',
  confirming: 'optimizer confirming a better model',
  switching: 'switching models',
  measuring: 'optimizer testing a model',
};
// The manager holds a home model and runs no trials or comparisons.
const MANAGER_PHASES: Record<string, string> = {
  off: 'manager off',
  waiting: 'manager waiting for fresh readings',
  watching: 'manager on',
  switching: 'switching models',
};

export function overviewSummary(s: SummaryInput): string | null {
  if (s.online == null) return null;
  const parts = [
    !s.online
      ? 'Darkbloom is offline'
      : s.starting
        ? 'Darkbloom is starting'
        : s.model
          ? s.pace != null
            ? `Earning ${s.money(s.pace)}/hr on ${s.name(s.model)}`
            : `Serving ${s.name(s.model)}`
          : 'Darkbloom is running',
  ];
  if (s.today != null) parts.push(`${s.money(s.today)} today`);
  const phases = s.strategy === 'manager' ? MANAGER_PHASES : PHASES;
  if (s.phase && phases[s.phase]) parts.push(phases[s.phase]);
  return parts.join(' · ');
}

/** Confirmed hourly totals since local midnight (base rewards included). */
export function todayTotal(
  hours: { at: number; usd: number }[] | undefined,
  now: number,
): number | null {
  if (!hours) return null;
  const midnight = new Date(now * 1000);
  midnight.setHours(0, 0, 0, 0);
  const start = midnight.getTime() / 1000;
  return hours
    .filter((h) => h.at >= start && h.at <= now)
    .reduce((sum, h) => sum + h.usd, 0);
}
