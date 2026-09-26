// Several cards poll the same small status endpoints on their own timers. One
// in-flight or recent response (within ttlMs) serves them all. A caller's abort
// stops only its own wait, never the shared request.
const recent = new Map<string, { at: number; value: Promise<unknown> }>();

export function sharedGet(
  url: string,
  ttlMs: number,
  signal?: AbortSignal,
  read: (r: Response) => Promise<unknown> = (r) => r.json(),
): Promise<unknown> {
  const now = Date.now(),
    saved = recent.get(url);
  let value = saved && now - saved.at < ttlMs ? saved.value : null;
  if (!value) {
    value = fetch(url, {
      cache: 'no-store',
      signal: AbortSignal.timeout(10000),
    }).then(read);
    const entry = { at: now, value };
    recent.set(url, entry);
    value.catch(() => {
      if (recent.get(url) === entry) recent.delete(url);
    });
  }
  if (!signal) return value;
  return new Promise((resolve, reject) => {
    if (signal.aborted) return reject(signal.reason);
    signal.addEventListener('abort', () => reject(signal.reason), {
      once: true,
    });
    value!.then(resolve, reject);
  });
}

/** After a write, the next read must reach the server. */
export function forgetShared(url: string) {
  recent.delete(url);
}
