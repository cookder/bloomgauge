// My Macs: plain labels for one Mac's card (native/machines.py `local_summary`).
const modes: Record<string, string> = {
  observe: 'Observing',
  demand: 'Following demand',
  week: 'Scheduled trials',
  optimize: 'Optimizing',
  combo: 'Pair trials',
};

/** The optimizer on that Mac. Older peers send no strategy. */
export function macMode(optimizer: string, strategy?: string | null): string {
  if (optimizer === 'demand' && strategy === 'manager') return 'Manager';
  return modes[optimizer] || 'Observing';
}

/** Up to four model names, then how many more that Mac offers. */
export function macModels(
  models: string[],
  count: number | undefined,
  name: (model: string) => string,
): string {
  if (!models.length) return 'No verified model reading';
  const more = Number.isInteger(count) ? (count as number) - models.length : 0;
  const shown = models.map(name).join(' + ');
  return more > 0 ? `${shown} and ${more} more` : shown;
}
