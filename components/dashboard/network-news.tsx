'use client';
import { useEffect, useState } from 'react';
import { Button } from '@/components/ui/button';
import { startChartPolling } from '@/lib/chart-polling';
import { usePageVisible } from '@/lib/use-page-visibility';
import {
  newsLines,
  newsSummary,
  pushText,
  validNetworkNews,
  type NetworkNews as News,
} from '@/lib/network-news';
import { useAppNavigation, useScreenActive } from './app-navigation';

const day = (at: number, now: number) => {
  const date = new Date(at * 1000);
  const today = date.toDateString() === new Date(now * 1000).toDateString();
  return date.toLocaleString(
    [],
    today
      ? { hour: 'numeric', minute: '2-digit' }
      : { weekday: 'short', month: 'short', day: 'numeric' },
  );
};

async function load(signal: AbortSignal): Promise<News> {
  const response = await fetch('/api/network/news', {
    signal,
    cache: 'no-store',
  });
  if (!response.ok) throw new Error('Waiting for the dashboard connection.');
  const value: unknown = await response.json();
  if (!validNetworkNews(value))
    throw new Error('Network news could not be read. Update Bloomkeeper.');
  return value;
}

/** Models joining or leaving Darkbloom and big warm-capacity swings (last 30 days). */
export function NetworkNews({
  offeredNotInCatalog,
  names,
}: {
  offeredNotInCatalog?: readonly string[];
  names?: Record<string, string>;
}) {
  const active = useScreenActive();
  const visible = usePageVisible();
  const { mobile } = useAppNavigation();
  const [news, setNews] = useState<News | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState<boolean | null>(null);
  useEffect(() => {
    if (!active || !visible) return;
    return startChartPolling<News>({
      load,
      intervalMs: 60000,
      onValue: (value) => {
        setNews(value);
        setError('');
      },
      onError: (e) => setError(e.message),
    });
  }, [active, visible]);
  async function save(body: object) {
    setBusy(true);
    try {
      const response = await fetch('/api/network/news', {
        method: 'POST',
        cache: 'no-store',
        signal: AbortSignal.timeout(15000),
        headers: {
          'Content-Type': 'application/json',
          'X-Bloom-Action': 'network-news',
        },
        body: JSON.stringify(body),
      });
      const value = (await response.json()) as News['push'] & {
        error?: string;
      };
      if (!response.ok) throw new Error(value.error || 'Could not save this.');
      setNews((n) => (n ? { ...n, push: value } : n));
      setError('');
    } catch (e) {
      setError(
        e instanceof Error && e.name !== 'TimeoutError'
          ? e.message
          : 'The setting could not be confirmed. Try again.',
      );
    } finally {
      setBusy(false);
    }
  }
  const now = news?.at ?? Date.now() / 1000;
  const lines = newsLines(news, { offeredNotInCatalog, names });
  const flagged = lines.some((l) => l.flag);
  const push = news?.push;
  const muted = !!push?.mutedUntil && push.mutedUntil > now;
  return (
    <details
      className="quiet-disclosure manager-evidence network-news"
      open={open ?? (flagged || !mobile)}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>
        <span>Network news</span>
        <small>{newsSummary(news, lines, now)}</small>
      </summary>
      {error && (
        <p className="notice" role="status">
          {news ? 'Network news is not live. ' : ''}
          {error}
        </p>
      )}
      {lines.length ? (
        <ol className="manager-evidence-rows network-news-rows">
          {lines.map((line) => (
            <li key={line.key} className={line.flag ? 'flagged' : undefined}>
              <div className="manager-evidence-model">
                <strong>{line.title}</strong>
              </div>
              <div className="manager-evidence-rate">
                <small>{day(line.at, now)}</small>
              </div>
              <small className="manager-evidence-why">
                {line.facts.join(' · ')}
              </small>
              {line.flag && (
                <p className="network-news-flag">
                  {line.flag.text} <code>{line.flag.command}</code>
                </p>
              )}
            </li>
          ))}
        </ol>
      ) : (
        news && (
          <p className="footnote">
            No models have joined or left the network
            {news.watchingSince != null
              ? ` since ${day(news.watchingSince, now)}`
              : ''}
            , and warm capacity hasn’t swung sharply.
          </p>
        )
      )}
      <p className="footnote">
        From the public network data Bloomkeeper already reads. A new model
        counts once 3 or more Macs serve it for 10 minutes; a model leaves after
        10 minutes off Darkbloom’s list; a swing is warm Macs halving or
        doubling for 15 minutes against the 3 hours before. $/h is an estimate
        for Macs like yours, not a promise.
      </p>
      {push && (
        <div className="network-news-push">
          <p className="small muted">
            <strong>Phone notice for new models · </strong>
            {pushText(push, now)}
            {push.enabled &&
              ' Uses the phone notifications set up under Switch notifications.'}
          </p>
          <div className="optimizer-quick-actions">
            <Button
              type="button"
              variant="outline"
              disabled={busy || !push.available}
              onClick={() =>
                void save({ action: 'push', enabled: !push.enabled })
              }
            >
              {push.enabled ? 'Turn off' : 'Turn on'}
            </Button>
            {push.enabled && (
              <Button
                type="button"
                variant="outline"
                disabled={busy}
                onClick={() => void save({ action: muted ? 'unmute' : 'mute' })}
              >
                {muted ? 'Unmute' : 'Mute for a week'}
              </Button>
            )}
          </div>
        </div>
      )}
    </details>
  );
}
