'use client';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Sparkles, X } from 'lucide-react';
import {
  validReleaseNotes,
  readSeenReleases,
  markReleaseSeen,
  releaseSeenKey,
  releaseNoticesKey,
  type ReleaseNotes,
} from '@/lib/release-notes';
import { Button } from '@/components/ui/button';
import { bloomWebsite } from '@/lib/website-links';

export function ReleaseNotesNotice({ banner = true }: { banner?: boolean }) {
  const [data, setData] = useState<ReleaseNotes | null>(null),
    [open, setOpen] = useState(false),
    [unseen, setUnseen] = useState(false);
  const [enabled, setEnabled] = useState(true),
    [error, setError] = useState(''),
    [loading, setLoading] = useState(true);
  const panel = useRef<HTMLElement>(null),
    epoch = useRef(0);
  const load = useCallback(async () => {
    const own = ++epoch.current;
    setLoading(true);
    setError('');
    try {
      const response = await fetch('/api/release-notes', {
        cache: 'no-store',
        signal: AbortSignal.timeout(8000),
      });
      const value: unknown = await response.json();
      if (!response.ok || !validReleaseNotes(value))
        throw Error('Release notes could not be loaded.');
      if (own !== epoch.current) return;
      setData(value);
      let seen: string[] = [];
      let notices = true;
      try {
        seen = readSeenReleases(localStorage.getItem(releaseSeenKey));
        notices = localStorage.getItem(releaseNoticesKey) !== 'false';
      } catch {
        /* Local preferences are optional. */
      }
      setEnabled(notices);
      setUnseen(!!value.release && !seen.includes(value.release.id));
    } catch {
      if (own === epoch.current)
        setError(
          'Release notes are unavailable right now. Your dashboard is still available.',
        );
    } finally {
      if (own === epoch.current) setLoading(false);
    }
  }, []);
  useEffect(() => {
    void load();
    return () => {
      epoch.current++;
    };
  }, [load]);
  useEffect(() => {
    const show = () => {
      setOpen(true);
      requestAnimationFrame(() =>
        panel.current?.scrollIntoView({ block: 'start', behavior: 'smooth' }),
      );
    };
    window.addEventListener('bloom-show-release-notes', show);
    return () => window.removeEventListener('bloom-show-release-notes', show);
  }, []);
  function dismiss() {
    if (data?.release)
      try {
        localStorage.setItem(
          releaseSeenKey,
          markReleaseSeen(
            localStorage.getItem(releaseSeenKey),
            data.release.id,
          ),
        );
      } catch {
        /* Keep this session dismissed. */
      }
    setUnseen(false);
    setOpen(false);
  }
  if (!open && (!banner || !unseen || !enabled || !data?.release)) return null;
  return (
    <section
      ref={panel}
      className={`release-notice ${open ? 'release-notice-open' : ''}`}
      aria-label="What's new in BloomGauge"
    >
      <div className="release-notice-heading">
        <Sparkles size={19} />
        <div>
          <strong>
            {data?.release
              ? `What’s new in BloomGauge ${data.installedVersion}`
              : 'What’s new in BloomGauge'}
          </strong>
          {!open && <p>{data?.release?.title}</p>}
        </div>
        {!open && (
          <Button variant="ghost" onClick={() => setOpen(true)}>
            Review changes
          </Button>
        )}
        <button
          type="button"
          className="icon-button"
          aria-label="Dismiss release notes"
          onClick={dismiss}
        >
          <X size={16} />
        </button>
      </div>
      {open && (
        <div className="release-notice-body">
          {loading ? (
            <p role="status">Loading release notes…</p>
          ) : error ? (
            <p role="status">
              {error}{' '}
              <Button variant="ghost" onClick={() => void load()}>
                Retry
              </Button>
            </p>
          ) : data?.release ? (
            <>
              <h2>{data.release.title}</h2>
              <ul>
                {data.release.highlights.map((item, i) => (
                  <li key={i}>
                    <strong>{item.title}</strong>
                    <p>{item.detail}</p>
                  </li>
                ))}
              </ul>
              <p className="footnote">
                These changes are included in the version running on this Mac.
              </p>
            </>
          ) : (
            <p>No release notes are bundled with this build yet.</p>
          )}
          <label className="release-notice-preference">
            <input
              type="checkbox"
              checked={enabled}
              onChange={(event) => {
                setEnabled(event.target.checked);
                try {
                  localStorage.setItem(
                    releaseNoticesKey,
                    String(event.target.checked),
                  );
                } catch {
                  /* Optional preference. */
                }
              }}
            />
            Show these notices after updates
          </label>
          <p className="footnote">
            Saved in this browser. Reopen anytime from More → Help & feedback →
            What’s new.
          </p>
          <p>
            <a
              className="text-link"
              href={bloomWebsite.changelog}
              target="_blank"
              rel="noopener noreferrer"
              referrerPolicy="no-referrer"
            >
              Read the full changelog <span aria-hidden="true">↗</span>
            </a>
          </p>
          <Button variant="outline" onClick={dismiss}>
            Done
          </Button>
        </div>
      )}
    </section>
  );
}
