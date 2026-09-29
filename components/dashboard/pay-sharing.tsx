'use client';
import { useCallback, useEffect, useState } from 'react';
import { Share2 } from 'lucide-react';

type Status = {
  schema: 1;
  enabled: boolean;
  lastSentAt: number | null;
  models: number;
  canChange: boolean;
};
const valid = (v: unknown): v is Status => {
  const s = v as Status | null;
  return (
    !!s &&
    s.schema === 1 &&
    typeof s.enabled === 'boolean' &&
    typeof s.canChange === 'boolean' &&
    Number.isFinite(s.models) &&
    (s.lastSentAt === null || Number.isFinite(s.lastSentAt))
  );
};

async function request(enabled?: boolean): Promise<Status> {
  const response = await fetch('/api/pay-sharing', {
    method: enabled === undefined ? 'GET' : 'POST',
    cache: 'no-store',
    signal: AbortSignal.timeout(40000),
    ...(enabled === undefined
      ? {}
      : {
          headers: {
            'Content-Type': 'application/json',
            'X-Bloom-Action': 'pay-sharing',
          },
          body: JSON.stringify({ enabled }),
        }),
  });
  const value: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const message =
      value &&
      typeof value === 'object' &&
      'error' in value &&
      typeof value.error === 'string'
        ? value.error
        : '';
    throw Error(
      message || 'Could not confirm the change. Nothing was changed.',
    );
  }
  if (!valid(value))
    throw Error('Could not confirm the change. Nothing was changed.');
  return value;
}

/** Optional, off by default: per-model pay curves that improve everyone's starting estimates. */
export function PaySharing() {
  const [status, setStatus] = useState<Status | null>(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(''),
    [message, setMessage] = useState('');
  const refresh = useCallback(async () => {
    try {
      setStatus(await request());
      setError('');
    } catch {
      setError('Sharing status is unavailable right now.');
    }
  }, []);
  useEffect(() => {
    void refresh();
  }, [refresh]);
  async function change(enabled: boolean) {
    if (busy) return;
    setBusy(true);
    setError('');
    setMessage('');
    try {
      const next = await request(enabled);
      setStatus(next);
      setMessage(
        enabled
          ? next.models
            ? `On. Shared curves for ${next.models} model${next.models === 1 ? '' : 's'}; BloomGauge refreshes them once a week.`
            : 'On. This Mac doesn’t have enough steady pay to summarize yet; BloomGauge will share once it does, at most weekly.'
          : 'Off. Your summaries were deleted from bloomformac.com.',
      );
    } catch (e) {
      setError(
        e instanceof Error
          ? e.message
          : 'Could not confirm the change. Nothing was changed.',
      );
    } finally {
      setBusy(false);
    }
  }
  return (
    <section
      className="usage-sharing panel"
      aria-label="Share model pay summaries"
    >
      <div className="usage-sharing-heading">
        <Share2 size={21} aria-hidden="true" />
        <h2>
          Improve starting estimates <span>Optional</span>
        </h2>
        {status && (
          <span className="usage-sharing-state">
            {status.enabled ? 'On' : 'Off'}
          </span>
        )}
      </div>
      <p>
        BloomGauge starts every Mac with estimates of what each model pays, so the
        optimizer can make good choices before it has measured them itself.
        Share a weekly summary from this Mac to make those estimates better for
        everyone. Off unless you turn it on.
      </p>
      <details className="usage-sharing-details">
        <summary>What is shared</summary>
        <p>
          For each model with at least 2 hours of steady paid work in the last
          30 days: how its pay rose with network demand (two curve numbers), the
          typical spread, and the hours and half-hours measured. Plus chip
          family, memory range and BloomGauge version, and a random ID used only to
          replace or delete this summary.
        </p>
        <p>
          No Darkbloom account or device IDs, balances, individual payments,
          times or anything that names you. Summaries are combined into the
          starting estimates shipped with future BloomGauge versions. Turning this
          off deletes this Mac’s summary from bloomformac.com.
        </p>
      </details>
      <a
        className="text-link"
        href="https://bloomformac.com/privacy"
        target="_blank"
        rel="noreferrer"
      >
        Read the privacy notice ↗
      </a>
      {!status && !error && <p role="status">Checking…</p>}
      {status && !status.canChange && (
        <p className="footnote">Change this in BloomGauge on your Mac.</p>
      )}
      {status?.canChange && (
        <div className="usage-sharing-actions">
          <button
            type="button"
            className={status.enabled ? 'secondary-button' : 'setup-primary'}
            disabled={busy}
            onClick={() => void change(!status.enabled)}
          >
            {busy
              ? 'Saving…'
              : status.enabled
                ? 'Turn off & delete'
                : 'Share pay summaries'}
          </button>
          {status.enabled && status.lastSentAt && (
            <span>
              Last shared{' '}
              {new Date(status.lastSentAt * 1000).toLocaleDateString()} ·{' '}
              {status.models} model{status.models === 1 ? '' : 's'}
            </span>
          )}
        </div>
      )}
      {error && (
        <p className="notice" role="alert">
          {error}
        </p>
      )}
      {message && (
        <p className="footnote" role="status">
          {message}
        </p>
      )}
    </section>
  );
}
