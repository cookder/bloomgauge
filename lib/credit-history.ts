import type { PulseCredit } from './earnings-pulse';
export const creditSortOptions = [
  { value: 'newest', label: 'Newest first' },
  { value: 'oldest', label: 'Oldest first' },
  { value: 'amount-desc', label: 'Largest credit' },
  { value: 'amount-asc', label: 'Smallest credit' },
  { value: 'tokens-desc', label: 'Most output tokens' },
];
export type CreditPreferences = {
  sort: string;
  category: string;
  limit: string;
  view: 'list' | 'models';
  showSummary: boolean;
};
export const creditPreferencesKey = 'bloom-credit-view-v1';
export function readCreditPreferences(raw: string | null): CreditPreferences {
  const defaults: CreditPreferences = {
    sort: 'newest',
    category: 'all',
    limit: '100',
    view: 'list',
    showSummary: true,
  };
  try {
    const v = JSON.parse(raw || 'null');
    if (!v || typeof v !== 'object') return defaults;
    return {
      sort: creditSortOptions.some((o) => o.value === v.sort)
        ? v.sort
        : defaults.sort,
      category: ['all', 'inference', 'base_reward'].includes(v.category)
        ? v.category
        : defaults.category,
      limit: ['25', '100', '250'].includes(v.limit) ? v.limit : defaults.limit,
      view: v.view === 'models' ? 'models' : 'list',
      showSummary: typeof v.showSummary === 'boolean' ? v.showSummary : true,
    };
  } catch {
    return defaults;
  }
}
export type CreditHistory = {
  count: number;
  page: number;
  limit: number;
  coverageStart: number | null;
  coverageEnd?: number | null;
  entries: {
    id: number;
    at: string;
    model: string | null;
    usd: number;
    outputTokens: number | null;
  }[];
  models: string[];
  summary: {
    count: number;
    totalUsd: number;
    inferenceUsd: number;
    baseRewardUsd: number;
    averageUsd: number | null;
    minUsd: number | null;
    maxUsd: number | null;
    outputTokens: number;
  };
  leaderboard: {
    model: string | null;
    count: number;
    usd: number;
    outputTokens: number;
  }[];
};
const finite = (v: unknown): v is number =>
  typeof v === 'number' && Number.isFinite(v);
const whole = (v: unknown) => finite(v) && Number.isSafeInteger(v) && v >= 0;
const record = (v: unknown): v is Record<string, unknown> =>
  !!v && typeof v === 'object' && !Array.isArray(v);
export function validCreditHistory(v: unknown): v is CreditHistory {
  if (
    !record(v) ||
    !whole(v.count) ||
    !whole(v.page) ||
    Number(v.page) < 1 ||
    !whole(v.limit) ||
    Number(v.limit) > 250 ||
    !(v.coverageStart === null || finite(v.coverageStart)) ||
    !Array.isArray(v.entries) ||
    v.entries.length > Number(v.limit) ||
    !v.entries.every(
      (e) =>
        record(e) &&
        whole(e.id) &&
        typeof e.at === 'string' &&
        Number.isFinite(Date.parse(e.at)) &&
        (e.model === null || typeof e.model === 'string') &&
        finite(e.usd) &&
        (e.outputTokens === null || whole(e.outputTokens)),
    ) ||
    !Array.isArray(v.models) ||
    !v.models.every((m) => typeof m === 'string') ||
    !Array.isArray(v.leaderboard)
  )
    return false;
  const s = v.summary;
  return (
    record(s) &&
    whole(s.count) &&
    s.count === v.count &&
    ['totalUsd', 'inferenceUsd', 'baseRewardUsd'].every((k) => finite(s[k])) &&
    ['averageUsd', 'minUsd', 'maxUsd'].every(
      (k) => s[k] === null || finite(s[k]),
    ) &&
    whole(s.outputTokens) &&
    v.leaderboard.every(
      (r) =>
        record(r) &&
        (r.model === null || typeof r.model === 'string') &&
        whole(r.count) &&
        finite(r.usd) &&
        whole(r.outputTokens),
    )
  );
}
export function sortedPulseCredits(
  events: PulseCredit[],
  sort: string,
): PulseCredit[] {
  return [...events].sort((a, b) => {
    const time = b.at - a.at || b.id - a.id;
    return sort === 'oldest'
      ? -time
      : sort === 'amount-desc'
        ? b.microUsd - a.microUsd || time
        : sort === 'amount-asc'
          ? a.microUsd - b.microUsd || time
          : time;
  });
}
