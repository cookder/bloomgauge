'use client';
import { recordSupportIssue } from '@/lib/support-issues';
import { useCallback, useEffect, useRef, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { validUpdateStatus, type UpdateStatus } from '@/lib/update-status';
import { useScreenActive } from './app-navigation';

type NativeWindow = Window & {
  webkit?: {
    messageHandlers?: {
      bloomUpdates?: { postMessage: (value: unknown) => void };
    };
  };
};
const bridge = () =>
  (window as NativeWindow).webkit?.messageHandlers?.bloomUpdates;

export function UpdateSettings({
  onboarding = false,
}: {
  onboarding?: boolean;
}) {
  const active = useScreenActive();
  const [native, setNative] = useState(false);
  const [state, setState] = useState<UpdateStatus | null>(null);
  const [busy, setBusy] = useState(false),
    [error, setError] = useState('');
  const pending = useRef<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const clear = useCallback(() => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = null;
  }, []);
  const send = useCallback(
    (action: 'status' | 'check' | 'set-automatic', enabled?: boolean) => {
      const handler = bridge();
      if (!handler) return;
      clear();
      const requestId = crypto.randomUUID();
      pending.current = requestId;
      setBusy(true);
      setError('');
      timer.current = setTimeout(() => {
        if (pending.current !== requestId) return;
        pending.current = null;
        setBusy(false);
        recordSupportIssue('action', 'help');
        setError(
          'Update settings did not respond. Refresh their status or use Check for Updates in the Mac app menu.',
        );
      }, 10000);
      try {
        handler.postMessage({
          action,
          requestId,
          ...(action === 'set-automatic' ? { enabled } : {}),
        });
      } catch {
        recordSupportIssue('action', 'help');
        clear();
        pending.current = null;
        setBusy(false);
        setError(
          'Could not open update controls. Use Check for Updates in the Mac app menu.',
        );
      }
    },
    [clear],
  );

  useEffect(() => {
    if (!active) return;
    setNative(!!bridge());
    const receive = (event: Event) => {
      const value = (event as CustomEvent).detail;
      if (
        !validUpdateStatus(value) ||
        (value.requestId !== null && value.requestId !== pending.current)
      )
        return;
      if (value.status === 'error') recordSupportIssue('action', 'help');
      setState(value);
      if (value.requestId === pending.current) {
        clear();
        pending.current = null;
        setBusy(false);
        setError('');
      }
    };
    const refresh = () => {
      if (!document.hidden && !pending.current) send('status');
    };
    window.addEventListener('bloom-updates-status', receive);
    window.addEventListener('focus', refresh);
    document.addEventListener('visibilitychange', refresh);
    send('status');
    return () => {
      clear();
      pending.current = null;
      window.removeEventListener('bloom-updates-status', receive);
      window.removeEventListener('focus', refresh);
      document.removeEventListener('visibilitychange', refresh);
    };
  }, [active, clear, send]);

  if (onboarding && (!native || state?.available === false)) return null;
  const usable = native && state?.available === true;
  const text =
    state?.status === 'update-available'
      ? 'An update is available. Review the Mac update window to install it.'
      : state?.status === 'up-to-date'
        ? 'You’re up to date.'
        : state?.checking
          ? 'Checking for updates…'
          : state?.lastCheck
            ? `Last checked ${new Date(state.lastCheck * 1000).toLocaleString()}`
            : 'No completed update check yet.';
  return (
    <section
      className={`update-settings ${onboarding ? 'update-settings-inline' : 'panel'}`}
      aria-label="App updates"
    >
      <div className="update-heading">
        <RefreshCw size={18} aria-hidden="true" />
        <h3>{onboarding ? 'Stay up to date.' : 'App updates'}</h3>
        {state && <span>Bloomkeeper {state.installedVersion}</span>}
      </div>
      {usable ? (
        <>
          <label className="update-option">
            <input
              type="checkbox"
              checked={state.automaticChecks}
              disabled={busy}
              onChange={(e) => send('set-automatic', e.target.checked)}
            />
            <span>
              Notify me about app updates
              <small>
                Checks about every six hours while Bloomkeeper is running. You approve
                downloading and installing. Your optional usage-sharing choice
                stays separate.
              </small>
            </span>
          </label>
          {!onboarding && (
            <div className="update-actions">
              <button
                type="button"
                className="small-button"
                disabled={busy || !state.canCheck || state.checking}
                onClick={() => send('check')}
              >
                Check for updates
              </button>
              <p role="status">{text}</p>
            </div>
          )}
          {!onboarding && !state.automaticChecks && (
            <p className="footnote">
              Automatic checks are off. Use the button above whenever you want
              to check.
            </p>
          )}
        </>
      ) : native && !state ? (
        <p role="status">Reading update settings…</p>
      ) : (
        <p>
          Install updates in Bloomkeeper on your Mac. Choose{' '}
          <strong>Check for Updates…</strong> in the app menu. If that option is
          missing, download the latest beta and replace the app in Applications.
        </p>
      )}
      {(error || state?.error) && (
        <p className="support-error" role="alert">
          {error || state?.error}
        </p>
      )}
      {native && error && (
        <button
          type="button"
          className="small-button"
          disabled={busy}
          onClick={() => send('status')}
        >
          Refresh update status
        </button>
      )}
      {!onboarding && (
        <div className="update-actions">
          <button
            type="button"
            className="small-button"
            onClick={() =>
              window.dispatchEvent(new Event('bloom-show-release-notes'))
            }
          >
            What’s new
          </button>
          <a
            className="text-link"
            href="https://bloomformac.com/#release"
            target="_blank"
            rel="noreferrer"
          >
            Download page ↗
          </a>
        </div>
      )}
    </section>
  );
}
