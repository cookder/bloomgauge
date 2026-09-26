export type Concurrency = {
  at: number;
  pending: number | null;
  limit: number | null;
  slots: {
    model: string;
    state: string | null;
    running: number | null;
    waiting: number | null;
    limit: number | null;
  }[];
};
const count = (n: unknown) =>
  n === null ||
  (typeof n === 'number' && Number.isSafeInteger(n) && n >= 0 && n <= 1000000);
export function validConcurrency(value: unknown): value is Concurrency {
  if (!value || typeof value !== 'object') return false;
  const c = value as Concurrency;
  return (
    Number.isFinite(c.at) &&
    count(c.pending) &&
    count(c.limit) &&
    Array.isArray(c.slots) &&
    c.slots.length <= 128 &&
    c.slots.every(
      (s) =>
        s &&
        typeof s.model === 'string' &&
        (s.state === null || typeof s.state === 'string') &&
        count(s.running) &&
        count(s.waiting) &&
        count(s.limit),
    )
  );
}
/** A missing slot counter cannot turn into a zero in a machine total. */
export function concurrencyTotal(c: Concurrency, key: 'running' | 'waiting') {
  return c.slots.length && c.slots.every((s) => s[key] !== null)
    ? c.slots.reduce((sum, s) => sum + s[key]!, 0)
    : null;
}
