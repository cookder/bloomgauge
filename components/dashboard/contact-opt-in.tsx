'use client';
import { useCallback, useEffect, useState } from 'react';
import { Mail } from 'lucide-react';
import {
  contactKind,
  validContactStatus,
  type ContactStatus,
} from '@/lib/user-contact';

const privacyUrl = 'https://bloomformac.com/privacy';

async function contactRequest(body?: unknown): Promise<ContactStatus> {
  const response = await fetch('/api/contact', {
    method: body ? 'POST' : 'GET',
    ...(body
      ? {
          headers: {
            'Content-Type': 'application/json',
            'X-Bloom-Action': 'contact',
          },
          body: JSON.stringify(body),
        }
      : {}),
    cache: 'no-store',
    signal: AbortSignal.timeout(25000),
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
  if (!validContactStatus(value))
    throw Error('Could not confirm the change. Nothing was changed.');
  return value;
}

/** Optional: lets Andrew reach this user about updates and problems. Off unless filled in. */
export function ContactOptIn() {
  const [status, setStatus] = useState<ContactStatus | null>(null),
    [contact, setContact] = useState(''),
    [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState(false),
    [editing, setEditing] = useState(false),
    [error, setError] = useState(''),
    [message, setMessage] = useState('');
  const refresh = useCallback(async () => {
    try {
      setStatus(await contactRequest());
      setError('');
    } catch {
      setError('Contact details are unavailable right now.');
    }
  }, []);
  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function change(body: unknown, done: string) {
    if (busy) return;
    setBusy(true);
    setError('');
    setMessage('');
    try {
      const next = await contactRequest(body);
      setStatus(next);
      setEditing(false);
      setConsent(false);
      setContact('');
      setMessage(done);
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

  const trimmed = contact.trim(),
    kind = contactKind(trimmed);
  const showForm = status?.canChange && (!status.contact || editing);
  return (
    <section
      className="usage-sharing panel contact-opt-in"
      aria-label="Stay in touch"
    >
      <div className="usage-sharing-heading">
        <Mail size={21} aria-hidden="true" />
        <h2>
          Stay in touch <span>Optional</span>
        </h2>
        {status && (
          <span className="usage-sharing-state">
            {status.contact ? 'Shared' : 'Not shared'}
          </span>
        )}
      </div>
      <p>
        Leave an email or Slack handle if you’d like Andrew, BloomGauge’s developer,
        to reach you about updates, fixes or the beta. BloomGauge works the same
        either way.
      </p>
      <details className="usage-sharing-details">
        <summary>What is shared</summary>
        <p>
          Only what you type here, whether it looks like an email or a Slack
          handle, your BloomGauge version and a random ID so this Mac can change or
          delete it later. It is not linked to usage sharing, problem reports,
          earnings or your Darkbloom account.
        </p>
        <p>
          Only Andrew can see it. It is never sold or shared, and you can remove
          it here at any time, which deletes it from bloomformac.com.
        </p>
      </details>
      <p>
        Or join the{' '}
        <a
          className="text-link contact-opt-in-inline"
          href="https://darkbloom.slack.com/archives/C0C4HC8HZLN"
          target="_blank"
          rel="noopener noreferrer"
        >
          BloomGauge channel on the Darkbloom Slack ↗
        </a>{' '}
        for updates and questions.
      </p>
      <a
        className="text-link"
        href={privacyUrl}
        target="_blank"
        rel="noreferrer"
      >
        Read the privacy notice ↗
      </a>
      {!status && !error && <p role="status">Checking…</p>}
      {status && !status.canChange && (
        <p className="footnote">
          {status.contact ? `Shared: ${status.contact}. ` : ''}Change this in
          BloomGauge on your Mac.
        </p>
      )}
      {status?.canChange && status.contact && !editing && (
        <div className="usage-sharing-actions">
          <span>
            Shared:{' '}
            <strong className="contact-opt-in-value">{status.contact}</strong>
          </span>
          <button
            type="button"
            className="secondary-button"
            disabled={busy}
            onClick={() => {
              setEditing(true);
              setContact(status.contact || '');
              setMessage('');
            }}
          >
            Change
          </button>
          <button
            type="button"
            className="secondary-button"
            disabled={busy}
            onClick={() =>
              void change(
                { action: 'remove' },
                'Removed. Andrew no longer has your contact details.',
              )
            }
          >
            {busy ? 'Removing…' : 'Remove'}
          </button>
        </div>
      )}
      {showForm && (
        <form
          className="contact-opt-in-form"
          onSubmit={(event) => {
            event.preventDefault();
            if (kind && consent)
              void change(
                { action: 'save', contact: trimmed, consent: true },
                'Saved. Thanks — Andrew can now reach you.',
              );
          }}
        >
          <label htmlFor="contact-opt-in-value">
            Email or Slack handle
            <input
              id="contact-opt-in-value"
              type="text"
              inputMode="email"
              maxLength={254}
              autoComplete="email"
              placeholder="you@example.com or @yourname"
              value={contact}
              disabled={busy}
              onChange={(event) => {
                setContact(event.target.value);
                setError('');
              }}
            />
          </label>
          {trimmed && !kind && (
            <p className="footnote">
              Enter an email address, or a Slack handle starting with @.
            </p>
          )}
          <label className="contact-opt-in-consent">
            <input
              type="checkbox"
              checked={consent}
              disabled={busy}
              onChange={(event) => setConsent(event.target.checked)}
            />
            <span>
              Send this to BloomGauge’s developer so he can contact me. I can remove
              it at any time.
            </span>
          </label>
          <div className="usage-sharing-actions">
            <button
              type="submit"
              className="setup-primary"
              disabled={busy || !kind || !consent}
            >
              {busy ? 'Saving…' : 'Save'}
            </button>
            {editing && (
              <button
                type="button"
                className="secondary-button"
                disabled={busy}
                onClick={() => {
                  setEditing(false);
                  setContact('');
                  setConsent(false);
                  setError('');
                }}
              >
                Cancel
              </button>
            )}
          </div>
        </form>
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
