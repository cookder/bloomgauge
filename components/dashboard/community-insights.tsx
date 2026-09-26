'use client';
import { useEffect, useState } from 'react';
import {
  BookOpen,
  Clock3,
  ExternalLink,
  MessageSquare,
  RefreshCw,
} from 'lucide-react';
import { startChartPolling } from '@/lib/chart-polling';
import { usePageVisible } from '@/lib/use-page-visibility';
import { useAppNavigation } from './app-navigation';

type Source = {
  url: string;
  channel: string;
  at: number;
  author: string;
  authority: 'team' | 'community' | 'unverified';
};
type Insight = {
  id: string;
  kind: string;
  title: string;
  body: string;
  relevance: string;
  measure: string;
  models: string[];
  sources: Source[];
};
type Digest = {
  id: string;
  at: number;
  from: number;
  to: number;
  status: string;
  headline: string;
  summary: string;
  channels: string[];
  items: Insight[];
};
type CommunityState = {
  at: number;
  status: string;
  detail: string;
  lastSuccessAt: number | null;
  nextCheckAt: number | null;
  lastAttempt: {
    at: number;
    status: string;
    from: number;
    to: number;
    channels: string[];
    detail: string;
  } | null;
  schedule: { enabled: boolean; hours: number[]; timeZone: string };
  digests: Digest[];
};
const categories = [
  ['all', 'All insights'],
  ['team_update', 'Team updates'],
  ['model_tip', 'Model tips'],
  ['operations', 'Running your Mac'],
  ['measurement', 'What to measure'],
] as const;
const date = (at: number | null | undefined) =>
  at == null
    ? 'Not yet'
    : new Date(at * 1000).toLocaleString([], {
        month: 'short',
        day: 'numeric',
        hour: 'numeric',
        minute: '2-digit',
      });
const safeSource = (url: string) =>
  /^https:\/\/[a-z0-9][a-z0-9-]*\.slack\.com\/archives\/[CG][A-Z0-9]+\/p[0-9]{15,20}$/.test(
    url,
  );

export function CommunityInsightsPanel({ enabled }: { enabled: boolean }) {
  const pageVisible = usePageVisible();
  const { navigate } = useAppNavigation();
  const [data, setData] = useState<CommunityState | null>(null);
  const [error, setError] = useState('');
  const [retry, setRetry] = useState(0);
  const [selected, setSelected] = useState('latest');
  const [category, setCategory] = useState('all');
  useEffect(() => {
    if (!enabled || !pageVisible) return;
    return startChartPolling({
      load: async (signal) => {
        const response = await fetch('/api/community-insights', {
          signal,
          cache: 'no-store',
        });
        if (!response.ok)
          throw new Error('Could not refresh the community digest.');
        const value = (await response.json()) as CommunityState;
        if (
          !value ||
          !Number.isFinite(value.at) ||
          !value.schedule ||
          !Array.isArray(value.digests) ||
          value.digests.some((d) => !d || !Array.isArray(d.items))
        )
          throw new Error('The community digest is not ready.');
        return value;
      },
      onValue: (value) => {
        setData(value);
        setError('');
      },
      onError: (e) => setError(e.message),
      intervalMs: 60000,
    });
  }, [enabled, pageVisible, retry]);
  const digest =
    selected === 'latest'
      ? data?.digests[0]
      : data?.digests.find((d) => d.id === selected);
  const rows =
    digest?.items.filter(
      (item) => category === 'all' || item.kind === category,
    ) ?? [];
  const status = error ? 'error' : (data?.status ?? 'loading');
  const statusLabel: Record<string, string> = {
    ok: 'Review up to date',
    partial: 'Partial review',
    blocked: 'Slack access needed',
    error: 'Check needs attention',
    waiting: 'First review pending',
    stale: 'Review overdue',
    loading: 'Loading digest',
  };
  return (
    <section
      className="community-view"
      aria-label="Darkbloom community insights"
    >
      <header className="community-heading">
        <div>
          <div className="eyebrow">DARKBLOOM / COMMUNITY</div>
          <h1>From the provider community.</h1>
          <p>
            Useful discussions, team updates, and ideas worth testing on this
            Mac.
          </p>
        </div>
        <MessageSquare size={30} aria-hidden="true" />
      </header>
      <div className="community-status panel">
        <div className="community-status-top">
          <strong
            className={`community-state ${status === 'ok' ? 'good' : 'pending'}`}
          >
            <i />
            {statusLabel[status] ?? 'Review unavailable'}
          </strong>
          <button
            type="button"
            className="text-link"
            onClick={() => setRetry((n) => n + 1)}
            aria-label="Reload saved community digest"
          >
            <RefreshCw size={14} /> Reload
          </button>
        </div>
        <p role={error ? 'alert' : undefined}>
          {error || data?.detail || 'Loading the latest saved review…'}
          {error && data?.digests.length
            ? ' The previously loaded digest remains below.'
            : ''}
        </p>
        <div className="community-checks">
          <div>
            <span>Last check</span>
            <strong>{date(data?.lastAttempt?.at)}</strong>
          </div>
          <div>
            <span>Last complete review</span>
            <strong>{date(data?.lastSuccessAt)}</strong>
          </div>
          <div>
            <span>Scheduled checks</span>
            <strong>
              {data?.schedule.enabled
                ? '8 am & 8 pm Central'
                : 'Not scheduled yet'}
            </strong>
          </div>
        </div>
        <details>
          <summary>
            <Clock3 size={14} /> Check schedule & coverage
          </summary>
          <p>
            Codex reviews Slack twice a day while this Mac is awake, Codex is
            running, and Slack is signed in. Reload refreshes the saved digest;
            it does not launch another Slack scan. Chart pause does not pause
            these scheduled reviews.
          </p>
          <p>
            Next scheduled check: {date(data?.nextCheckAt)} (shown in your local
            time).{' '}
            {data?.lastAttempt?.channels.length
              ? `Last check covered ${data.lastAttempt.channels.map((c) => '#' + c).join(', ')} from ${date(data.lastAttempt.from)} to ${date(data.lastAttempt.to)}.`
              : 'No channel coverage has been confirmed yet.'}
          </p>
        </details>
      </div>
      {!digest && (
        <div className="panel community-empty">
          <BookOpen size={30} />
          <h2>
            {data?.digests.length
              ? 'That saved review is no longer available.'
              : 'Your next useful idea starts here.'}
          </h2>
          <p>
            {data?.digests.length
              ? 'Choose the latest review from the digest menu.'
              : 'The first digest will appear after Slack access is verified. It will include model tips, operating advice, useful measurements and links to the original discussions.'}
          </p>
        </div>
      )}
      {Boolean(data?.digests.length) && (
        <>
          <div className="community-toolbar">
            <label>
              Digest
              <select
                aria-label="Community digest"
                value={selected}
                onChange={(e) => {
                  setSelected(e.target.value);
                  setCategory('all');
                }}
              >
                <option value="latest">Latest summary</option>
                {data?.digests.map((d) => (
                  <option key={d.id} value={d.id}>
                    {date(d.at)} · {d.headline}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Show
              <select
                aria-label="Insight category"
                value={category}
                onChange={(e) => setCategory(e.target.value)}
              >
                {categories.map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
          </div>
          {digest && (
            <>
              <article className="panel community-summary">
                <div className="eyebrow">
                  {selected === 'latest' ? 'LATEST DIGEST' : 'SAVED DIGEST'} ·{' '}
                  {date(digest.at)}
                  {digest.status === 'partial' ? ' · PARTIAL' : ''}
                </div>
                <h2>{digest.headline}</h2>
                <p>{digest.summary}</p>
                <small>
                  Reviewed {date(digest.from)} – {date(digest.to)} ·{' '}
                  {digest.channels.map((c) => '#' + c).join(', ')}. Community
                  observations are leads to investigate, not verified earnings
                  on this Mac.
                </small>
              </article>
              <div className="community-cards">
                {rows.map((item) => (
                  <article
                    className={`panel community-card ${item.kind}`}
                    key={item.id}
                  >
                    <div className="community-card-label">
                      <span>
                        {categories.find(([id]) => id === item.kind)?.[1]}
                      </span>
                      <span>
                        {item.sources.every((s) => s.authority === 'team')
                          ? 'Team source'
                          : item.sources.some((s) => s.authority === 'team')
                            ? 'Team + community'
                            : 'Community report'}
                      </span>
                    </div>
                    <h3>{item.title}</h3>
                    <p>{item.body}</p>
                    {!!item.models.length && (
                      <div className="community-models">
                        {item.models.map((model) => (
                          <span key={model}>{model}</span>
                        ))}
                      </div>
                    )}
                    {item.relevance && (
                      <div className="community-takeaway">
                        <strong>For your Mac</strong>
                        <p>{item.relevance}</p>
                      </div>
                    )}
                    {item.measure && (
                      <div className="community-takeaway">
                        <strong>Worth measuring</strong>
                        <p>{item.measure}</p>
                      </div>
                    )}
                    <details className="community-sources">
                      <summary>
                        {item.sources.length === 1
                          ? 'View source'
                          : `View ${item.sources.length} sources`}
                      </summary>
                      {item.sources
                        .filter((s) => safeSource(s.url))
                        .map((source) => (
                          <a
                            href={source.url}
                            key={source.url}
                            target="_blank"
                            rel="noreferrer"
                          >
                            <span>
                              #{source.channel} · {source.author}
                              <small>
                                {date(source.at)} ·{' '}
                                {source.authority === 'team'
                                  ? 'Darkbloom team'
                                  : source.authority === 'community'
                                    ? 'Community member'
                                    : 'Role unverified'}
                              </small>
                            </span>
                            <ExternalLink size={14} />
                          </a>
                        ))}
                    </details>
                  </article>
                ))}
              </div>
              {!rows.length && (
                <p className="community-no-matches">
                  No insights in this category for this review. Choose All
                  insights or another digest.
                </p>
              )}
            </>
          )}
        </>
      )}
      <div className="panel community-next">
        <div>
          <strong>Put an idea to the test.</strong>
          <p>
            Compare verified earnings per warm hour, idle time, request sizes
            and concurrent demand before choosing a model.
          </p>
        </div>
        <div>
          <button className="text-link" onClick={() => navigate('results')}>
            Model history
          </button>
          <button className="text-link" onClick={() => navigate('workload')}>
            Workload metrics
          </button>
          <button className="text-link" onClick={() => navigate('demand')}>
            Network demand
          </button>
        </div>
      </div>
    </section>
  );
}
