import {
  supportCategories,
  supportContexts,
  type SupportIssue,
} from './support-issues';
const statuses = [
  'ok',
  'stale',
  'error',
  'unavailable',
  'missing',
  'loading',
  'idle',
  'waiting',
  'ready',
  'warming',
  'failed',
  'cold',
  'paused',
  'observing',
  'learning',
  'optimizing',
  'switching',
  'unknown',
];
const failures = [
  'startup-command',
  'startup-timeout',
  'readiness-timeout',
  'model-load-error',
  'endpoint-configuration',
  'endpoint-discovery',
  'endpoint-authentication',
  'endpoint-unsafe',
  'warmup-capacity',
  'warmup-response',
  'warmup-rejected',
  'warmup-transport',
  'loaded-model-unconfirmed',
  'cache-permission',
  'cache-command',
  'cache-readings',
  'cache-recovery-limited',
  'readiness-changed',
  'warmup-failed',
  'none',
  'unknown',
];
const recoveries = [
  'none',
  'idle-not-verified',
  'resource-or-identity',
  'provider-changed',
  'restore-not-ready',
  'restored',
  'unknown',
];
const errors = [
  'certificate',
  'authentication',
  'rate_limit',
  'timeout',
  'permission',
  'connection',
  'memory',
  'other',
  'none',
];
const semver = /^(?:\d{1,3}\.\d{1,3}\.\d{1,4}|unknown)$/;
type Plain = Record<string, unknown>;
const exact = (value: unknown, keys: string[]): value is Plain =>
  !!value &&
  typeof value === 'object' &&
  !Array.isArray(value) &&
  Object.keys(value).length === keys.length &&
  keys.every((key) => Object.hasOwn(value, key));
const oneOf = (value: unknown, options: readonly string[]) =>
  typeof value === 'string' && options.includes(value);
export type SupportReport = SupportIssue & {
  schema: 1;
  id: string;
  generatedAt: string;
  appVersion: string;
  surface: 'mac' | 'phone';
  description: string;
  contact: string;
  diagnostics: {
    available: boolean;
    osMajor: number;
    chipFamily: string;
    memoryBand: string;
    providerOnline: boolean | null;
    providerVersion: string;
    optimizerMode: string;
    optimizerStatus: string;
    failureCode: string;
    recoveryCode: string;
    sources: { name: string; status: string; error: string }[];
  };
};
export type SupportPreview = { report: SupportReport; reviewToken: string };
export type SupportFields = SupportIssue & {
  description: string;
  contact: string;
};
export function validSupportPreview(
  value: unknown,
  fields: SupportFields,
  now = Date.now(),
): value is SupportPreview {
  if (
    !exact(value, ['report', 'reviewToken']) ||
    typeof value.reviewToken !== 'string' ||
    !/^[a-f0-9]{64}$/.test(value.reviewToken)
  )
    return false;
  const r = value.report;
  if (
    !exact(r, [
      'schema',
      'id',
      'generatedAt',
      'appVersion',
      'surface',
      'category',
      'context',
      'description',
      'contact',
      'diagnostics',
    ])
  )
    return false;
  if (
    r.schema !== 1 ||
    typeof r.id !== 'string' ||
    !/^[a-f0-9]{32}$/.test(r.id) ||
    !oneOf(r.category, supportCategories) ||
    !oneOf(r.context, supportContexts) ||
    !oneOf(r.surface, ['mac', 'phone'])
  )
    return false;
  if (
    typeof r.generatedAt !== 'string' ||
    !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(
      r.generatedAt,
    )
  )
    return false;
  const at = Date.parse(r.generatedAt);
  if (!Number.isFinite(at) || now - at > 86400000 || at - now > 300000)
    return false;
  if (new Date(at).toISOString().slice(0, 19) !== r.generatedAt.slice(0, 19))
    return false;
  if (
    typeof r.appVersion !== 'string' ||
    !semver.test(r.appVersion) ||
    typeof r.description !== 'string' ||
    [...r.description].length > 2000 ||
    typeof r.contact !== 'string' ||
    [...r.contact].length > 254
  )
    return false;
  if (
    r.category !== fields.category ||
    r.context !== fields.context ||
    r.description !== fields.description ||
    r.contact !== fields.contact
  )
    return false;
  const d = r.diagnostics;
  if (
    !exact(d, [
      'available',
      'osMajor',
      'chipFamily',
      'memoryBand',
      'providerOnline',
      'providerVersion',
      'optimizerMode',
      'optimizerStatus',
      'failureCode',
      'recoveryCode',
      'sources',
    ])
  )
    return false;
  if (
    typeof d.available !== 'boolean' ||
    !Number.isInteger(d.osMajor) ||
    Number(d.osMajor) < 0 ||
    Number(d.osMajor) > 99 ||
    typeof d.chipFamily !== 'string' ||
    !/^(M\d{1,2}( Pro| Max| Ultra)?|Other)$/.test(d.chipFamily)
  )
    return false;
  if (
    !oneOf(d.memoryBand, [
      'up-to-16',
      '17-32',
      '33-64',
      '65-128',
      'over-128',
      'unknown',
    ]) ||
    !(d.providerOnline === null || typeof d.providerOnline === 'boolean') ||
    typeof d.providerVersion !== 'string' ||
    !semver.test(d.providerVersion)
  )
    return false;
  if (
    !oneOf(d.optimizerMode, [
      'observe',
      'week',
      'combo',
      'best',
      'demand',
      'unknown',
    ]) ||
    !oneOf(d.optimizerStatus, statuses) ||
    !oneOf(d.failureCode, failures) ||
    !oneOf(d.recoveryCode, recoveries)
  )
    return false;
  if (
    !Array.isArray(d.sources) ||
    d.sources.length > 3 ||
    !d.sources.every(
      (s) =>
        exact(s, ['name', 'status', 'error']) &&
        oneOf(s.name, ['earnings', 'monitor', 'network']) &&
        oneOf(s.status, statuses) &&
        oneOf(s.error, errors),
    )
  )
    return false;
  if (new Set(d.sources.map((s) => s.name)).size !== d.sources.length)
    return false;
  return new TextEncoder().encode(JSON.stringify(r)).length <= 12288;
}
export function validSupportReceipt(
  value: unknown,
  id: string,
): value is { status: 'sent'; reportId: string } {
  return (
    exact(value, ['status', 'reportId']) &&
    value.status === 'sent' &&
    value.reportId === id
  );
}
export class SupportRequestError extends Error {
  constructor(
    public status:
      | 'unconfirmed'
      | 'busy'
      | 'expired'
      | 'rate_limited'
      | 'unavailable'
      | 'preview_limit'
      | 'mac_only',
  ) {
    super('unconfirmed');
  }
}
/** Both response headers and body parsing share one deadline. Only fixed routes. */
export async function supportRequest(
  action: 'preview' | 'send' | 'auto',
  body: unknown,
  signal: AbortSignal,
  timeoutMs = 20000,
): Promise<unknown> {
  const control = new AbortController(),
    cancel = () => control.abort();
  signal.addEventListener('abort', cancel, { once: true });
  if (signal.aborted) control.abort();
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    const deadline = new Promise<never>((_, reject) => {
      timer = setTimeout(() => {
        reject(Error('unconfirmed'));
        control.abort();
      }, timeoutMs);
    });
    return await Promise.race([
      (async () => {
        const response = await fetch('/api/support/' + action, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-Bloom-Action': 'support',
          },
          body: JSON.stringify(body),
          signal: control.signal,
          cache: 'no-store',
        });
        const text = await response.text();
        if (new TextEncoder().encode(text).length > 16384)
          throw Error('invalid');
        const value: unknown = JSON.parse(text);
        if (!response.ok) {
          const status =
            value && typeof value === 'object' && 'status' in value
              ? value.status
              : null;
          throw new SupportRequestError(
            oneOf(status, [
              'busy',
              'expired',
              'rate_limited',
              'unavailable',
              'preview_limit',
              'mac_only',
            ])
              ? (status as SupportRequestError['status'])
              : 'unconfirmed',
          );
        }
        return value;
      })(),
      deadline,
    ]);
  } finally {
    clearTimeout(timer);
    signal.removeEventListener('abort', cancel);
  }
}

/** One tap: build the same allowlisted report the dialog previews, then send it. Returns the report ID. */
export async function quickSupportReport(
  fields: SupportFields,
  signal: AbortSignal,
): Promise<string> {
  const preview = await supportRequest('preview', fields, signal);
  if (!validSupportPreview(preview, fields))
    throw new SupportRequestError('unconfirmed');
  const result = await supportRequest(
    'send',
    {
      reportId: preview.report.id,
      reviewToken: preview.reviewToken,
      confirmed: true,
    },
    signal,
  );
  if (!validSupportReceipt(result, preview.report.id))
    throw new SupportRequestError('unconfirmed');
  return preview.report.id;
}
export async function readAutoSend(signal: AbortSignal): Promise<boolean> {
  const response = await fetch('/api/support/auto', {
    cache: 'no-store',
    signal,
  });
  const value: unknown = response.ok ? await response.json() : null;
  return (
    !!value &&
    typeof value === 'object' &&
    'autoSend' in value &&
    value.autoSend === true
  );
}
export async function writeAutoSend(
  enabled: boolean,
  signal: AbortSignal,
): Promise<boolean> {
  const value = await supportRequest('auto', { autoSend: enabled }, signal);
  return (
    !!value &&
    typeof value === 'object' &&
    'autoSend' in value &&
    value.autoSend === true
  );
}
