import type { SupportContext } from './support-issues';
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
  let failedSince: number | null = null;
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
        failedSince = null;
        onValue(value);
      }
    } catch (error) {
      if (!stopped) {
        failures++;
        if (failedSince === null) failedSince = Date.now();
        if (
          repeat &&
          failures >= 2 &&
          Date.now() - failedSince >= 60000 &&
          typeof window !== 'undefined'
        ) {
          window.dispatchEvent(
            new CustomEvent('bloom-support-issue', {
              detail: { category: 'connection', context: issueContext },
            }),
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
