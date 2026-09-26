export const supportCategories = [
  'manual',
  'ui',
  'connection',
  'setup',
  'model',
  'action',
] as const;
export const supportContexts = [
  'overview',
  'setup',
  'models',
  'phone',
  'earnings',
  'hardware',
  'network',
  'help',
  'unknown',
] as const;
export type SupportCategory = (typeof supportCategories)[number];
export type SupportContext = (typeof supportContexts)[number];
export type SupportIssue = {
  category: SupportCategory;
  context: SupportContext;
};
type State = {
  prompt: SupportIssue | null;
  request: (SupportIssue & { sequence: number }) | null;
};
const empty: State = { prompt: null, request: null };
let state = empty,
  sequence = 0,
  context: SupportContext = 'unknown',
  reporting = false;
const listeners = new Set<() => void>();
const cooldowns = new Map<string, number>();
const conditions = new Map<string, number>();
const bootAt = Date.now() / 1000;
let lastModelResult = '';
const storageKey = 'bloom-support-prompts-v1';
const emit = () => {
  for (const listener of listeners) listener();
};
const valid = (category: unknown, area: unknown) =>
  supportCategories.includes(category as SupportCategory) &&
  supportContexts.includes(area as SupportContext);
function remembered(key: string) {
  try {
    const saved = JSON.parse(localStorage.getItem(storageKey) || '{}');
    const until = saved?.[key];
    if (
      typeof until === 'number' &&
      Number.isFinite(until) &&
      until <= Date.now() + 86400000
    )
      return until;
  } catch {
    /* Local preferences are optional; raw errors are never stored. */
  }
  return 0;
}
function mute(issue: SupportIssue, duration: number) {
  const key = issue.category + ':' + issue.context,
    until = Date.now() + duration;
  cooldowns.set(key, until);
  try {
    const previous = JSON.parse(localStorage.getItem(storageKey) || '{}');
    for (const category of supportCategories)
      for (const area of supportContexts) {
        const k = category + ':' + area,
          v = previous?.[k];
        if (
          typeof v === 'number' &&
          Number.isFinite(v) &&
          v > Date.now() &&
          v <= Date.now() + 86400000
        )
          cooldowns.set(k, Math.max(cooldowns.get(k) || 0, v));
      }
    const saved = Object.fromEntries(
      [...cooldowns]
        .filter(([k, v]) => v > Date.now() && /^([a-z]+):([a-z]+)$/.test(k))
        .slice(-32),
    );
    localStorage.setItem(storageKey, JSON.stringify(saved));
  } catch {
    /* No reporting or upload is coupled to browser storage. */
  }
}
export function supportContext(value: string): SupportContext {
  if (
    [
      'test',
      'results',
      'switch',
      'pairs',
      'diagnostics',
      'optimizer-tools',
      'models',
    ].includes(value)
  )
    return 'models';
  if (['overview', 'charts', 'credits', 'target', 'earnings'].includes(value))
    return value === 'overview' ? 'overview' : 'earnings';
  if (['hardware', 'energy', 'processes', 'throughput'].includes(value))
    return 'hardware';
  if (['demand', 'traffic', 'fleet', 'network', 'community'].includes(value))
    return 'network';
  if (value === 'access' || value === 'phone') return 'phone';
  if (['support', 'help', 'plan', 'tools'].includes(value)) return 'help';
  return supportContexts.includes(value as SupportContext)
    ? (value as SupportContext)
    : 'unknown';
}
export function setSupportContext(value: string) {
  context = supportContext(value);
}
export function setSupportReporting(value: boolean) {
  reporting = value;
}
export const subscribeSupportIssues = (listener: () => void) => {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
};
export const getSupportIssues = () => state;
export const getServerSupportIssues = () => empty;
export function recordSupportIssue(
  category: SupportCategory,
  area: SupportContext = context,
) {
  if (reporting || category === 'manual' || !valid(category, area)) return;
  const key = category + ':' + area,
    until = Math.max(cooldowns.get(key) || 0, remembered(key));
  if (until > Date.now() || state.prompt) return;
  const issue = { category, context: area };
  mute(issue, 30 * 60000);
  state = { ...state, prompt: issue };
  emit();
}
export function dismissSupportIssue() {
  if (state.prompt) mute(state.prompt, 86400000);
  state = { ...state, prompt: null };
  emit();
}
/** Clear the prompt after it was handled (sent), keeping the 30-minute mute from recording. */
export function clearSupportPrompt() {
  state = { ...state, prompt: null };
  emit();
}
/** After an automatic report, the same kind of problem waits six hours before sending again. */
export function clearAutoSentPrompt(issue = state.prompt) {
  if (!issue) return;
  mute(issue, 6 * 3600000);
  // Only the prompt that was sent; a newer one stays for the user.
  if (
    state.prompt?.category === issue.category &&
    state.prompt.context === issue.context
  )
    clearSupportPrompt();
}
export function openSupportReport(
  category: SupportCategory = 'manual',
  area: SupportContext = context,
) {
  if (!valid(category, area)) return;
  state = {
    prompt: null,
    request: { category, context: area, sequence: ++sequence },
  };
  emit();
}
export function observeSupportCondition(
  category: SupportCategory,
  area: SupportContext,
  failed: boolean,
  now = Date.now(),
) {
  if (!valid(category, area)) return;
  const key = category + ':' + area;
  if (!failed) {
    conditions.delete(key);
    return;
  }
  const since = conditions.get(key);
  if (since === undefined) {
    conditions.set(key, now);
    return;
  }
  if (now - since >= 60000) recordSupportIssue(category, area);
}
export function observeModelResult(value: unknown) {
  if (!value || typeof value !== 'object') return;
  const result = value as { at?: unknown; outcome?: unknown };
  if (
    typeof result.at !== 'number' ||
    !Number.isFinite(result.at) ||
    typeof result.outcome !== 'string' ||
    !['failed', 'recovered', 'switched', 'deferred'].includes(result.outcome)
  )
    return;
  const key = result.at + ':' + result.outcome;
  if (key === lastModelResult) return;
  lastModelResult = key;
  if (
    result.at >= bootAt &&
    result.at <= Date.now() / 1000 + 300 &&
    ['failed', 'recovered'].includes(result.outcome)
  )
    recordSupportIssue('model', 'models');
}

export const supportTitles: Record<SupportCategory, string> = {
  manual: 'Report a problem',
  ui: 'A view ran into a problem',
  connection: 'A connection needs attention',
  setup: 'Setup needs attention',
  model: 'A model switch needs review',
  action: 'A request could not be confirmed',
};
