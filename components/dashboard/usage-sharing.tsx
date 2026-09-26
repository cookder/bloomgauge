'use client';
import { recordSupportIssue } from '@/lib/support-issues';
import { useCallback, useEffect, useRef, useState } from 'react';
import { ShieldCheck } from 'lucide-react';
import { useScreenActive } from './app-navigation';
import { forgetShared, sharedGet } from '@/lib/shared-get';

type UsageStatus = {
  schema: 1;
  appVersion: string;
  localOnly: boolean;
  enabled: boolean;
  consentSaved: boolean;
  deletionPending: boolean;
  sending: boolean;
  lastSentAt: number | null;
  lastError: string | null;
  invitationEligible?: boolean;
  invitationOffered?: boolean;
};
type UsageAction =
  | { action: 'consent'; enabled: boolean }
  | {
      action:
        | 'retry-delete'
        | 'dashboard-opened'
        | 'offer-invitation'
        | 'dismiss-invitation';
    };
const changedEvent = 'bloom-usage-choice-changed';
const privacyUrl = 'https://bloomformac.com/privacy';

function validStatus(value: unknown): value is UsageStatus {
  const status = value as UsageStatus | null;
  return (
    !!status &&
    status.schema === 1 &&
    typeof status.appVersion === 'string' &&
    typeof status.localOnly === 'boolean' &&
    typeof status.enabled === 'boolean' &&
    typeof status.consentSaved === 'boolean' &&
    typeof status.deletionPending === 'boolean' &&
    !(status.enabled && status.deletionPending) &&
    typeof status.sending === 'boolean' &&
    (status.lastSentAt === null ||
      (typeof status.lastSentAt === 'number' &&
        Number.isFinite(status.lastSentAt) &&
        status.lastSentAt >= 0)) &&
    (status.lastError === null || typeof status.lastError === 'string')
  );
}

async function usageRequest(action?: UsageAction): Promise<UsageStatus> {
  if (!action) {
    const result = await sharedGet(
      '/api/usage',
      30000,
      undefined,
      async (response) => {
        if (!response.ok) throw Error('Usage sharing status is unavailable.');
        return response.json();
      },
    );
    if (!validStatus(result))
      throw Error('Usage sharing status could not be verified.');
    return result;
  }
  forgetShared('/api/usage');
  const response = await fetch('/api/usage', {
    method: action ? 'POST' : 'GET',
    ...(action
      ? {
          headers: {
            'Content-Type': 'application/json',
            'X-Bloom-Action': 'usage',
          },
          body: JSON.stringify(action),
        }
      : {}),
    cache: 'no-store',
    signal: AbortSignal.timeout(10000),
  });
  if (!response.ok) throw Error('Usage sharing status is unavailable.');
  const result: unknown = await response.json();
  if (!validStatus(result))
    throw Error('Usage sharing status could not be verified.');
  return result;
}

// Mounted by the dashboard after a snapshot has loaded, outside setup. A daily
// flag requires a visible page, rather than the collector's background activity.
export function useDashboardUsage(active: boolean) {
  const recordedDay = useRef<string | null>(null);
  useEffect(() => {
    if (!active) return;
    let stopped = false,
      pending = false;
    async function record() {
      if (stopped || pending || document.visibilityState !== 'visible') return;
      const day = new Date().toISOString().slice(0, 10);
      if (recordedDay.current === day) return;
      pending = true;
      try {
        const status = await usageRequest();
        if (stopped || document.visibilityState !== 'visible') return;
        if (!status.enabled || !status.consentSaved || status.deletionPending) {
          recordedDay.current = null;
          return;
        }
        const result = await usageRequest({ action: 'dashboard-opened' });
        if (
          !stopped &&
          result.enabled &&
          result.consentSaved &&
          !result.deletionPending
        )
          recordedDay.current = day;
      } catch {
        /* Optional reporting never interrupts the dashboard. */
      } finally {
        pending = false;
      }
    }
    const changed = () => {
      recordedDay.current = null;
      void record();
    };
    const visible = () => {
      void record();
    };
    void record();
    const timer = setInterval(visible, 60000);
    window.addEventListener(changedEvent, changed);
    document.addEventListener('visibilitychange', visible);
    return () => {
      stopped = true;
      clearInterval(timer);
      window.removeEventListener(changedEvent, changed);
      document.removeEventListener('visibilitychange', visible);
    };
  }, [active]);
}

export function UsageInvitation({ active }: { active: boolean }) {
  const [offered, setOffered] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const claim = useRef<Promise<UsageStatus> | null>(null);
  const busy = useRef(false),
    dismissed = useRef(false);
  useEffect(() => {
    if (!active) return;
    let stopped = false,
      reading = false;
    async function check() {
      if (
        stopped ||
        reading ||
        busy.current ||
        dismissed.current ||
        document.visibilityState !== 'visible'
      )
        return;
      reading = true;
      try {
        const status = await usageRequest();
        if (stopped || document.visibilityState !== 'visible') return;
        if (
          status.localOnly ||
          status.enabled ||
          status.deletionPending ||
          !status.consentSaved ||
          status.lastError
        ) {
          setOffered(false);
          return;
        }
        if (!claim.current && status.invitationEligible === true) {
          claim.current = usageRequest({ action: 'offer-invitation' });
        }
        if (claim.current) {
          const result = await claim.current;
          if (
            !stopped &&
            !dismissed.current &&
            !busy.current &&
            !result.localOnly &&
            !result.enabled &&
            result.invitationOffered === true
          )
            setOffered(true);
        }
      } catch {
        /* Optional invitation fails quietly; settings remain available. */
      } finally {
        reading = false;
      }
    }
    const changed = () => {
      dismissed.current = true;
      setOffered(false);
    };
    const timer = window.setTimeout(() => void check(), 5000);
    const repeat = window.setInterval(() => void check(), 60000);
    const visible = () => {
      void check();
    };
    document.addEventListener('visibilitychange', visible);
    window.addEventListener(changedEvent, changed);
    return () => {
      stopped = true;
      window.clearTimeout(timer);
      window.clearInterval(repeat);
      document.removeEventListener('visibilitychange', visible);
      window.removeEventListener(changedEvent, changed);
    };
  }, [active]);

  async function choose(share: boolean) {
    if (busy.current) return;
    busy.current = true;
    setSaving(true);
    setError('');
    try {
      const result = await usageRequest(
        share
          ? { action: 'consent', enabled: true }
          : { action: 'dismiss-invitation' },
      );
      if (
        result.localOnly ||
        (share &&
          (!result.enabled || !result.consentSaved || result.deletionPending))
      )
        throw Error();
      dismissed.current = true;
      setOffered(false);
      if (share) window.dispatchEvent(new Event(changedEvent));
    } catch {
      setError(
        'Could not confirm your choice. Try again, or manage sharing in More → About Bloomkeeper.',
      );
    } finally {
      busy.current = false;
      setSaving(false);
    }
  }
  if (!active || !offered) return null;
  return (
    <section
      className="panel usage-invitation"
      aria-labelledby="usage-invitation-title"
    >
      <div>
        <p className="eyebrow">Optional · asked once</p>
        <h2 id="usage-invitation-title">Help shape Bloomkeeper.</h2>
        <p>
          Share basic setup and feature-use reports so we can see what works and
          where to improve. Your choice won’t affect any features.
        </p>
        <details>
          <summary>Exactly what is shared</summary>
          <p>
            App and macOS versions, chip family, RAM range, a broad setup-error
            category, and daily yes/no indicators for setup, dashboard,
            optimizer and phone use; older versions may include historical
            access flags. A random reporting ID is separate from your
            installation ID.
          </p>
          <p>
            No names, email, earnings, account credentials, prompts, raw logs or
            private phone links. Reports are kept for 30 days. Sharing can be
            turned off and reports deleted in More → About Bloomkeeper.
          </p>
          <a
            className="text-link"
            href={privacyUrl}
            target="_blank"
            rel="noreferrer"
          >
            Read the privacy notice ↗
          </a>
        </details>
      </div>
      <div className="usage-invitation-actions">
        <button
          type="button"
          className="setup-primary"
          disabled={saving}
          onClick={() => void choose(true)}
        >
          {saving ? 'Saving…' : 'Share optional usage'}
        </button>
        <button
          type="button"
          className="secondary-button"
          disabled={saving}
          onClick={() => void choose(false)}
        >
          No thanks
        </button>
        <small>We won’t ask again. You can change this later.</small>
      </div>
      {error && (
        <p className="notice" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}

export function UsageSharing({ setup = false }: { setup?: boolean }) {
  const active = useScreenActive();
  const [status, setStatus] = useState<UsageStatus | null>(null);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [saving, setSaving] = useState(false);
  const epoch = useRef(0),
    busy = useRef(false);
  const latest = useRef<UsageStatus | null>(null);
  latest.current = status;
  const refresh = useCallback(async () => {
    if (busy.current) return;
    const own = ++epoch.current;
    try {
      const result = await usageRequest();
      if (own === epoch.current) {
        setStatus(result);
        setError('');
        setMessage('');
      }
    } catch {
      if (own === epoch.current) {
        setStatus(null);
        setMessage('');
        setError(
          'Could not confirm usage sharing status. Refresh to check your saved choice.',
        );
      }
    }
  }, []);
  useEffect(() => {
    if (!active) return;
    const visible = () => {
      if (document.visibilityState === 'visible') void refresh();
    };
    visible();
    const timer = setInterval(() => {
      if (latest.current?.deletionPending || latest.current?.sending) visible();
    }, 15000);
    window.addEventListener(changedEvent, visible);
    document.addEventListener('visibilitychange', visible);
    return () => {
      epoch.current++;
      clearInterval(timer);
      window.removeEventListener(changedEvent, visible);
      document.removeEventListener('visibilitychange', visible);
    };
  }, [active, refresh]);

  async function change(action: UsageAction) {
    if (busy.current || !status || status.localOnly) return;
    busy.current = true;
    const own = ++epoch.current;
    setSaving(true);
    setMessage('');
    setError('');
    try {
      const result = await usageRequest(action);
      if (own !== epoch.current) return;
      const expectedEnabled = action.action === 'consent' && action.enabled;
      if (
        result.localOnly ||
        result.enabled !== expectedEnabled ||
        (expectedEnabled && !result.consentSaved)
      ) {
        throw Error('The requested sharing choice was not confirmed.');
      }
      setStatus(result);
      if (action.action === 'consent' && action.enabled && result.enabled) {
        setMessage(
          'Optional usage sharing is on. You can turn it off here at any time.',
        );
      } else if (
        !result.enabled &&
        result.consentSaved &&
        !result.deletionPending
      ) {
        setMessage('Usage sharing is off. Deletion is complete.');
      }
      window.dispatchEvent(new Event(changedEvent));
    } catch {
      if (own === epoch.current) {
        setStatus(null);
        setError(
          'Could not confirm the change. Refresh to check sharing and deletion status.',
        );
        recordSupportIssue('action', 'help');
      }
    } finally {
      busy.current = false;
      setSaving(false);
    }
  }

  return (
    <section
      className={`usage-sharing ${setup ? 'usage-sharing-setup' : 'panel'}`}
      aria-label="Optional usage sharing"
    >
      <div className="usage-sharing-heading">
        <ShieldCheck size={21} aria-hidden="true" />
        <h2>
          Help improve Bloomkeeper <span>Optional</span>
        </h2>
        {status && (
          <span className="usage-sharing-state">
            {!status.consentSaved
              ? 'Stopped for this session'
              : status.enabled
                ? 'On'
                : 'Off'}
          </span>
        )}
      </div>
      <p>
        Share limited setup and feature-use reports with Bloomkeeper’s developer.
        Sharing starts off. Every Bloomkeeper feature works the same either way.
      </p>
      <details className="usage-sharing-details">
        <summary>What is shared</summary>
        <p>
          App version, macOS major version, chip family, memory range, a coarse
          setup error category, and daily yes/no flags for setup completed,
          dashboard opened, phone used and optimizer used. Older versions may
          include historical access flags. Background updates show that Bloomkeeper is
          running; opening the dashboard is counted separately.
        </p>
        <p>
          A random analytics identifier is separate from your installation ID.
          Reports exclude names, email, account and license IDs, earnings,
          prompts, account credentials, raw errors or logs, and private phone
          URLs.
        </p>
        <p>
          Reports are kept for 30 days. While sharing is on, Bloomkeeper updates them
          at most every six hours, with an extra update for consent or relevant
          setup and access changes. Turn sharing off to stop reports and request
          deletion.
        </p>
      </details>
      <a
        className="text-link"
        href={privacyUrl}
        target="_blank"
        rel="noreferrer"
      >
        Read the privacy notice ↗
      </a>
      {!status && !error && <p role="status">Checking usage sharing…</p>}
      {error && (
        <p className="notice" role="alert">
          {error}{' '}
          <button
            type="button"
            className="text-link"
            disabled={saving}
            onClick={() => void refresh()}
          >
            Refresh status
          </button>
        </p>
      )}
      {status?.localOnly ? (
        <p className="footnote">
          Change usage sharing and request deletion in Bloomkeeper on your Mac.
        </p>
      ) : (
        status && (
          <>
            {!status.consentSaved ? (
              <div className="usage-sharing-pending" role="alert">
                <strong>
                  Sharing is stopped for this session, but Bloomkeeper could not save
                  that choice. Keep Bloomkeeper open and retry before quitting.
                </strong>
                <p>Retry saves your choice before finishing deletion.</p>
                <button
                  type="button"
                  className="secondary-button"
                  disabled={saving}
                  onClick={() => void change({ action: 'retry-delete' })}
                >
                  {saving ? 'Retrying…' : 'Retry deletion'}
                </button>
              </div>
            ) : status.deletionPending ? (
              <div className="usage-sharing-pending" role="status">
                <strong>Sharing is off. Deletion is pending.</strong>
                <p>
                  No new reports will be sent. Bloomkeeper keeps only the credentials
                  needed to finish deletion and will retry when this Mac is
                  online. Keep Bloomkeeper running, or retry below.
                </p>
                <button
                  type="button"
                  className="secondary-button"
                  disabled={saving || status.sending}
                  onClick={() => void change({ action: 'retry-delete' })}
                >
                  {saving || status.sending
                    ? 'Requesting deletion…'
                    : 'Retry deletion'}
                </button>
              </div>
            ) : (
              <div className="usage-sharing-actions">
                <button
                  type="button"
                  className={
                    status.enabled ? 'secondary-button' : 'setup-primary'
                  }
                  disabled={saving}
                  onClick={() =>
                    void change({ action: 'consent', enabled: !status.enabled })
                  }
                >
                  {saving
                    ? 'Saving choice…'
                    : status.enabled
                      ? 'Turn off & delete reports'
                      : 'Share optional usage'}
                </button>
                {!status.enabled && (
                  <span>
                    {setup
                      ? 'Continue below to keep sharing off. Change it later in More → About Bloomkeeper.'
                      : 'Sharing stays off unless you choose to turn it on.'}
                  </span>
                )}
              </div>
            )}
            {status.enabled && status.lastError && (
              <p className="footnote" role="status">
                The last report could not be confirmed. Bloomkeeper will retry; your
                features are unaffected.
              </p>
            )}
          </>
        )
      )}
      {message && (
        <p className="footnote" role="status">
          {message}
        </p>
      )}
    </section>
  );
}
