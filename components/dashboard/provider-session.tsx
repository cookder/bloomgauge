'use client';
import { useEffect, useState } from 'react';
import { ChevronDown } from 'lucide-react';
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { num, shortModel } from './shared';
import { useScreenActive } from './app-navigation';
import { usePageVisible } from '@/lib/use-page-visibility';
import { startChartPolling } from '@/lib/chart-polling';

export type SessionReputation = {
  status: string;
  since: number;
  asOf: number;
  baselineReason: string;
  scoreStart: number;
  scoreNow: number;
  scoreChange: number;
  totalJobs: number | null;
  successfulJobs: number | null;
  failedJobs: number | null;
  uptimeSeconds: number | null;
  challengesPassed: number | null;
  challengesFailed: number | null;
};
export type ProviderSession = {
  performance?: {
    status: string;
    detail: string;
    seconds: number;
    requests: number | null;
    tokens: number | null;
    requestsPartial?: boolean;
    tokensPartial?: boolean;
    since: number | null;
    segmentStartedAt: number | null;
  };
  id: number;
  label: string;
  status: string;
  models: string[];
  startedAt: number;
  providerStartedAt: number;
  countersSince: number;
  requestsSince: number | null;
  tokensSince: number | null;
  counterScope: string;
  startReason: string;
  endedAt: number | null;
  endReason: string | null;
  lastSeenAt: number;
  durationSeconds: number;
  requests: number | null;
  tokens: number | null;
  reputation: SessionReputation | null;
};
export const sessionStamp = (at: number | null | undefined) =>
  at == null
    ? '—'
    : new Date(at * 1000).toLocaleString([], {
        month: 'short',
        day: 'numeric',
        hour: 'numeric',
        minute: '2-digit',
        second: '2-digit',
      });
export const sessionDuration = (seconds: number | null | undefined) =>
  seconds == null
    ? '—'
    : `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
const reasons: Record<string, string> = {
  first_observed: 'Tracking began',
  service_restarted: 'Service restarted',
  model_changed: 'Model selection changed',
  counter_reset: 'Provider counters reset',
  service_resumed: 'Service resumed',
  service_stopped: 'Service stopped',
  identity_changed: 'Account or device changed',
  identity_unavailable: 'Account unavailable',
};

export function ProviderSessionPanel({
  session,
  paused,
}: {
  session: ProviderSession | null;
  paused: boolean;
}) {
  const active = useScreenActive();
  const pageVisible = usePageVisible();
  const sessionId = session?.id ?? null;
  const [expanded, setExpanded] = useState(false);
  const [saved, setSaved] = useState<{
    sessionId: number | null;
    recent: ProviderSession[];
  } | null>(null);
  const recent = saved?.sessionId === sessionId ? saved.recent : [];
  const [error, setError] = useState('');
  useEffect(() => {
    if (
      !expanded ||
      !active ||
      !pageVisible ||
      (paused && saved?.sessionId === sessionId)
    )
      return;
    return startChartPolling({
      load: async (signal) => {
        const response = await fetch('/api/sessions', {
          cache: 'no-store',
          signal,
        });
        if (!response.ok)
          throw Error(
            'Session history is unavailable. Retrying automatically.',
          );
        const data = (await response.json()) as { recent: ProviderSession[] };
        if (
          !Array.isArray(data?.recent) ||
          data.recent.some(
            (row) =>
              !row ||
              !Number.isFinite(row.id) ||
              typeof row.label !== 'string' ||
              !Array.isArray(row.models) ||
              row.models.some((model) => typeof model !== 'string'),
          )
        )
          throw Error(
            'Session history returned an incomplete response. Retrying automatically.',
          );
        return data.recent;
      },
      onValue: (rows) => {
        setSaved({ sessionId, recent: rows });
        setError('');
      },
      onError: (error) => setError(error.message),
      intervalMs: 5000,
      repeat: !paused,
    });
    // A received snapshot is intentionally not a polling dependency.
  }, [expanded, paused, sessionId, active, pageVisible]);
  return (
    <section
      className="panel provider-session-panel"
      aria-label="Provider reporting session"
    >
      <div className="provider-session-heading">
        <strong>{session?.label ?? 'Provider session'}</strong>
        <span
          className={`status-pill ${session?.status === 'active' && session.performance?.status === 'counting' ? 'good' : 'warn'}`}
        >
          {paused
            ? 'View paused'
            : session?.status === 'active'
              ? session.performance?.status === 'counting'
                ? 'Counting warm time'
                : 'Statistics paused'
              : session?.status === 'ended'
                ? 'Ended'
                : 'Awaiting fresh readings'}
        </span>
        <span>
          {session?.models.map(shortModel).join(' + ') ||
            'No verified session yet'}
        </span>
      </div>
      {session && (
        <p className="provider-session-meta">
          {session.counterScope === 'observed' ? 'Observed from' : 'Started'}{' '}
          {sessionStamp(session.startedAt)}
          {session.endedAt != null
            ? ` · End detected ${sessionStamp(session.endedAt)}`
            : ''}
          {' · '}
          {sessionDuration(session.durationSeconds)} elapsed ·{' '}
          {sessionDuration(session.performance?.seconds)} warm time counted
          {' · '}
          {reasons[session.endReason ?? session.startReason] ??
            'Provider session'}
        </p>
      )}
      <p className="footnote">
        {session?.performance?.detail ??
          'Warm-time measurements start when readiness is verified.'}{' '}
        Stops end a session; service restarts and changes to the selected model
        set begin another. Switching work between two models already loaded
        together stays in the same session. Loading, cold time and warm-up work
        are excluded; ready idle time counts.
        {session?.status === 'stale'
          ? ' Last confirmed counts are held while the service state is uncertain.'
          : ''}
      </p>
      <Collapsible open={expanded} onOpenChange={setExpanded}>
        <CollapsibleTrigger className="session-history-toggle">
          Recent sessions <ChevronDown size={16} />
        </CollapsibleTrigger>
        <CollapsibleContent>
          {error && (
            <p className="footnote" role="status">
              {error}{' '}
              {recent.length > 0 ? 'Showing the last received history.' : ''}
            </p>
          )}
          {!error && saved?.sessionId !== sessionId && (
            <p className="footnote" role="status">
              Loading saved sessions…
            </p>
          )}
          <Table className="session-history-table">
            <TableHeader>
              <TableRow>
                <TableHead>Session / models</TableHead>
                <TableHead>Period</TableHead>
                <TableHead>Warm requests</TableHead>
                <TableHead>Warm output tokens</TableHead>
                <TableHead>Reputation change</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {recent.map((row) => (
                <TableRow key={row.id}>
                  <TableCell>
                    <strong>{row.label}</strong>
                    <small>{row.models.map(shortModel).join(' + ')}</small>
                  </TableCell>
                  <TableCell>
                    {sessionStamp(row.startedAt)}
                    <small>
                      {row.endedAt != null
                        ? `Ended ${sessionStamp(row.endedAt)}`
                        : row.status === 'active'
                          ? 'Active'
                          : 'Last known session · stale'}
                    </small>
                    <small>
                      {sessionDuration(row.durationSeconds)} elapsed ·{' '}
                      {sessionDuration(row.performance?.seconds)} warm
                    </small>
                  </TableCell>
                  <TableCell>
                    {row.performance?.requestsPartial &&
                    row.performance.requests != null
                      ? '≥ '
                      : ''}
                    {num(row.performance?.requests)}
                    <small>
                      {row.performance
                        ? `Ready observations from ${sessionStamp(row.performance.since)}`
                        : 'Earlier readiness unverified'}
                    </small>
                    {row.performance?.requestsPartial && (
                      <small>Partial counter coverage</small>
                    )}
                  </TableCell>
                  <TableCell>
                    {row.performance?.tokensPartial &&
                    row.performance.tokens != null
                      ? '≥ '
                      : ''}
                    {num(row.performance?.tokens)}
                    {row.performance?.tokensPartial && (
                      <small>Partial counter coverage</small>
                    )}
                  </TableCell>
                  <TableCell>
                    {row.reputation
                      ? `${row.reputation.scoreChange >= 0 ? '+' : ''}${num(row.reputation.scoreChange, 1)} points`
                      : '—'}
                    <small>
                      {row.reputation
                        ? `Observed from ${sessionStamp(row.reputation.since)}`
                        : 'No matched reputation readings'}
                    </small>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          <p className="footnote">
            Last 20 recorded sessions on this Mac. Counters use verified warm
            intervals. Confirmed paid credits and account earnings keep their
            own date ranges.
          </p>
        </CollapsibleContent>
      </Collapsible>
    </section>
  );
}
