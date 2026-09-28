// Keep in step with native/support_reports.py CATEGORIES (and the website's list).
export const supportCategories = [
  'manual',
  'ui',
  'connection',
  'setup',
  'model',
  'action',
  'validation',
] as const;
/** Validators whose rejections are reported as `validation` issues, and what they read. */
export const validationSources = {
  'run-status': 'the run status',
  optimizer: 'the optimizer plan',
  'optimizer-controls': 'the optimizer controls',
  'model-controls': 'the model status',
  reputation: 'the reputation reading',
} as const;
export type ValidationSource = keyof typeof validationSources;
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
/**
 * The report schema has no field for the validator, so a validation report's context
 * says which one failed: validation · overview = run status, · network = reputation,
 * · setup = optimizer controls, · models = the optimizer plan or model status.
 */
export const validationContexts: Record<ValidationSource, SupportContext> = {
  'run-status': 'overview',
  optimizer: 'models',
  'optimizer-controls': 'setup',
  'model-controls': 'models',
  reputation: 'network',
};
const validationSource = (category: string, source: unknown) =>
  category === 'validation' &&
  typeof source === 'string' &&
  Object.hasOwn(validationSources, source)
    ? (source as ValidationSource)
    : undefined;
export type SupportIssue = {
  category: SupportCategory;
  context: SupportContext;
  /** Which validator rejected a response (validation issues only; shown, not uploaded). */
  source?: ValidationSource;
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
  source?: string,
) {
  if (reporting || category === 'manual' || !valid(category, area)) return;
  const validator = validationSource(category, source);
  if (validator) area = validationContexts[validator];
  const key = category + ':' + area,
    until = Math.max(cooldowns.get(key) || 0, remembered(key));
  if (until > Date.now() || state.prompt) return;
  const issue: SupportIssue = { category, context: area };
  if (validator) issue.source = validator;
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
/**
 * Send a prompt automatically. Sent or not, the prompt is cleared and muted for six
 * hours: while auto-send is on the prompt is never shown, so one the Mac app holds back
 * (its shared limit) or that fails to send would otherwise block every later problem.
 * The Mac app's own limit already refuses the same problem for 24 hours, so retrying it
 * sooner would only be refused again. Resolves true when the report was sent.
 */
export async function autoSendSupportPrompt(
  issue: SupportIssue,
  send: (issue: SupportIssue) => Promise<unknown>,
): Promise<boolean> {
  try {
    await send(issue);
    return true;
  } catch {
    return false;
  } finally {
    clearAutoSentPrompt(issue);
  }
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
  source?: string,
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
  if (now - since >= 60000) recordSupportIssue(category, area, source);
}
/**
 * The backend writes `failed` when no model was verified after a switch, and
 * `recovered` after it restored and verified a model: either the manager's routine
 * self-heal (manager.py restore(), no prompt) or a switch that failed and was rolled
 * back (optimizer.py switch(): a legacy pause or a failed excursion), which still
 * needs a report so its failure code reaches triage. lastSwitchResult carries no field
 * that tells the two apart, so a `recovered` result counts as a failed switch when it
 * names a failure code or stage, or when the switch run it closed (`runs`, the plan's
 * switch history: same completion time) ended `recovered`. Manager restores record no run.
 */
export function observeModelResult(value: unknown, runs?: unknown) {
  if (!value || typeof value !== 'object') return;
  const result = value as {
    at?: unknown;
    outcome?: unknown;
    code?: unknown;
    stage?: unknown;
  };
  if (
    typeof result.at !== 'number' ||
    !Number.isFinite(result.at) ||
    typeof result.outcome !== 'string' ||
    !['failed', 'recovered', 'switched', 'deferred'].includes(result.outcome)
  )
    return;
  const rolledBack =
    result.outcome === 'recovered' &&
    (typeof result.code === 'string' ||
      typeof result.stage === 'string' ||
      (Array.isArray(runs) &&
        runs.some(
          (run) =>
            !!run &&
            typeof run === 'object' &&
            run.completedAt === result.at &&
            run.result === 'recovered',
        )));
  if (result.outcome !== 'failed' && !rolledBack) return;
  // A routine restore seen first without the switch history must not hide it later.
  const key = result.at + ':' + result.outcome;
  if (key === lastModelResult) return;
  lastModelResult = key;
  if (result.at >= bootAt && result.at <= Date.now() / 1000 + 300)
    recordSupportIssue('model', 'models');
}

export const supportTitles: Record<SupportCategory, string> = {
  manual: 'Report a problem',
  ui: 'A view ran into a problem',
  connection: 'A connection needs attention',
  setup: 'Setup needs attention',
  model: 'A model switch needs review',
  action: 'A request could not be confirmed',
  validation: 'Some data couldn’t be read',
};
/** The prompt's title; a validation issue names what couldn't be read. */
export function supportIssueTitle(issue: SupportIssue): string {
  return issue.category === 'validation' && issue.source
    ? `Couldn’t read ${validationSources[issue.source]}`
    : supportTitles[issue.category];
}
