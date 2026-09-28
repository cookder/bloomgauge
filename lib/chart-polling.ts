import type { SupportContext, ValidationSource } from './support-issues';

/**
 * The server answered, but a response validator rejected it: a version mismatch or a
 * bug, not a connection problem. `validator` names the check (never raw data or text).
 */
export class ResponseValidationError extends Error {
  validator: ValidationSource;
  constructor(validator: ValidationSource, message: string) {
    super(message);
    this.name = 'ResponseValidationError';
    this.validator = validator;
  }
}

type PollOptions<T> = {
  load: (signal: AbortSignal) => Promise<T>;
  onValue: (value: T) => void;
  onError: (error: Error) => void;
  intervalMs: number;
  timeoutMs?: number;
  repeat?: boolean;
  issueContext?: SupportContext;
};

/** One request at a time, including across slow connections and retries.
 * Disposing an old query prevents every later result from committing, even
 * when a transport finishes after abort (common when a phone reconnects).
 */
export function startChartPolling<T>({
  load,
  onValue,
  onError,
  intervalMs,
  timeoutMs = 15000,
  repeat = true,
  issueContext = 'unknown',
}: PollOptions<T>): () => void {
  let stopped = false;
  let failures = 0;
  // The reporting streak: failures of one kind (a connection, or one validator).
  let streak = 0;
  let failedSince: number | null = null;
  let failedKind = '';
  let timer: ReturnType<typeof setTimeout> | undefined;
  let deadline: ReturnType<typeof setTimeout> | undefined;
  let controller: AbortController | undefined;

  async function poll() {
    const request = new AbortController();
    controller = request;
    try {
      const timeout = new Promise<never>((_, reject) => {
        deadline = setTimeout(() => {
          // Reject first so the useful timeout message wins over AbortError.
          reject(
            new Error(
              repeat
                ? 'The connection timed out. Retrying automatically.'
                : 'The connection timed out. Resume live view or retry this range.',
            ),
          );
          request.abort();
        }, timeoutMs);
      });
      const value = await Promise.race([load(request.signal), timeout]);
      if (!stopped) {
        failures = 0;
        streak = 0;
        failedSince = null;
        onValue(value);
      }
    } catch (error) {
      if (!stopped) {
        failures++;
        const issue: {
          category: 'validation' | 'connection';
          context: SupportContext;
          source?: ValidationSource;
        } =
          error instanceof ResponseValidationError
            ? {
                category: 'validation',
                context: issueContext,
                source: error.validator,
              }
            : { category: 'connection', context: issueContext };
        // A different kind of failure starts its own streak, so one timeout during a
        // validator rejection (or one bad response after an outage) is not reported
        // as the other kind.
        const kind = issue.category + ':' + (issue.source ?? '');
        if (kind !== failedKind || failedSince === null) {
          failedKind = kind;
          failedSince = Date.now();
          streak = 0;
        }
        streak++;
        if (
          repeat &&
          streak >= 2 &&
          Date.now() - failedSince >= 60000 &&
          typeof window !== 'undefined'
        ) {
          window.dispatchEvent(
            new CustomEvent('bloom-support-issue', { detail: issue }),
          );
        }
        onError(
          error instanceof Error ? error : new Error('Connection unavailable.'),
        );
      }
    } finally {
      clearTimeout(deadline);
      if (!stopped && repeat) {
        const delay = Math.min(
          intervalMs * 2 ** Math.min(failures, 4),
          Math.max(intervalMs, 30000),
        );
        timer = setTimeout(poll, delay);
      }
    }
  }

  void poll();
  return () => {
    stopped = true;
    clearTimeout(timer);
    clearTimeout(deadline);
    controller?.abort();
  };
}
