'use client';
import { memo, useEffect, useState } from 'react';
import { ShieldCheck, ArrowUpRight } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { age, num, shortModel } from './shared';
import {
  validConcurrency,
  concurrencyTotal,
  type Concurrency,
} from '@/lib/concurrency';
import { sessionStamp, type ProviderSession } from './provider-session';
import { useScreenActive } from './app-navigation';
import { usePageVisible } from '@/lib/use-page-visibility';
import { startChartPolling } from '@/lib/chart-polling';

type ReputationData = {
  concurrency?: Concurrency | null;
  observedSessionId?: number | null;
  score: number;
  updatedAt: number;
  totalJobs: number | null;
  successfulJobs: number | null;
  failedJobs: number | null;
  uptimeSeconds: number | null;
  responseTimeMs: number | null;
  challengesPassed: number | null;
  challengesFailed: number | null;
  trustLevel: string | null;
  providerStatus: string | null;
};
type State = {
  status: string;
  data: ReputationData | null;
  nativeAvailable: boolean;
  identityAvailable: boolean;
  session: ProviderSession | null;
};
type NativeWindow = Window & {
  webkit?: {
    messageHandlers?: {
      bloomAccount?: { postMessage: (value: { action: string }) => void };
    };
  };
};
const bridge = () =>
  typeof window === 'undefined'
    ? undefined
    : (window as NativeWindow).webkit?.messageHandlers?.bloomAccount;
const messages: Record<string, string> = {
  disconnected:
    'Connect your Darkbloom console account to see the official reputation for this Mac. The earnings login cannot read this endpoint.',
  connecting:
    'Sign in on the Darkbloom page that opens. Email sign-in stays inside the app. This card updates when your Mac is matched.',
  auth_required:
    'Darkbloom needs you to sign in again to refresh your reputation.',
  unavailable:
    'Darkbloom’s reputation response is unavailable. The app will retry while connected.',
  unmatched:
    'This Mac was not uniquely matched in the signed-in account. Check that you used the same Darkbloom account as your provider.',
  stale:
    'Showing the last saved reputation. Open the connection to refresh your Darkbloom sign-in if needed.',
};

export const ReputationPanel = memo(function ReputationPanel({
  paused,
  session,
  concurrencyOnly = false,
}: {
  paused: boolean;
  session: ProviderSession | null;
  concurrencyOnly?: boolean;
}) {
  const active = useScreenActive();
  const pageVisible = usePageVisible();
  const [state, setState] = useState<State | null>(null);
  const [error, setError] = useState('');
  const [native] = useState(() => Boolean(bridge()));
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    if (!active || !pageVisible || (paused && state)) return;
    return startChartPolling({
      load: async (signal) => {
        const response = await fetch('/api/reputation', {
          signal,
          cache: 'no-store',
        });
        if (!response.ok)
          throw Error(
            'Could not read the reputation connection. Retrying automatically.',
          );
        const next: State = await response.json();
        if (
          !next ||
          typeof next.status !== 'string' ||
          (next.data != null &&
            (!Number.isFinite(next.data.score) ||
              !Number.isFinite(next.data.updatedAt))) ||
          (next.session != null && !Number.isFinite(next.session.id))
        )
          throw Error(
            'Reputation returned an incomplete response. Retrying automatically.',
          );
        return next;
      },
      onValue: (next) => {
        setState(next);
        setError('');
        setNow(Date.now() / 1000);
      },
      onError: (error) => setError(error.message),
      intervalMs: 5000,
      repeat: !paused,
    });
    // A received snapshot is intentionally not a polling dependency.
  }, [paused, active, pageVisible, session?.id]);
  useEffect(() => {
    if (paused || !active || !pageVisible) return;
    // Freshness must keep aging even when the network stops responding.
    const tick = () => setNow(Date.now() / 1000);
    tick();
    const timer = setInterval(tick, 5000);
    return () => clearInterval(timer);
  }, [paused, active, pageVisible]);
  const r = state?.data;
  const jobsPercent =
    r?.totalJobs && r.successfulJobs != null
      ? (r.successfulJobs * 100) / r.totalJobs
      : null;
  const challenges =
    r?.challengesPassed != null && r.challengesFailed != null
      ? r.challengesPassed + r.challengesFailed
      : null;
  const challengePercent =
    challenges && r?.challengesPassed != null
      ? (r.challengesPassed * 100) / challenges
      : null;
  const canConnect = native && state?.nativeAvailable;
  const fresh =
    !error && state?.status === 'ok' && r && now - r.updatedAt <= 150;
  const sessionRep =
    session && state?.session?.id === session.id
      ? state.session.reputation
      : null;
  if (concurrencyOnly) {
    const candidate = r?.concurrency;
    const c =
      fresh &&
      session?.status === 'active' &&
      session.performance?.status === 'counting' &&
      r?.observedSessionId === session.id &&
      validConcurrency(candidate) &&
      now - candidate.at >= -5 &&
      now - candidate.at <= 60
        ? candidate
        : null;
    return (
      <section className="panel concurrency-panel">
        <div className="panel-heading">
          <div>
            <div className="eyebrow">THIS MAC / CONCURRENCY</div>
            <h2>Requests running together.</h2>
          </div>
          <span className={`small ${c ? 'connected' : 'muted'}`}>
            {paused
              ? 'View paused'
              : c
                ? 'Updated ' + age(c.at, now)
                : 'Unavailable'}
          </span>
        </div>
        <div className="concurrency-grid">
          <div>
            <span>Generating now</span>
            <strong>{num(c ? concurrencyTotal(c, 'running') : null)}</strong>
          </div>
          <div>
            <span>Waiting on this Mac</span>
            <strong>{num(c ? concurrencyTotal(c, 'waiting') : null)}</strong>
          </div>
          <div>
            <span>Coordinator in flight</span>
            <strong>{num(c?.pending)}</strong>
          </div>
          <div>
            <span>Reported machine limit</span>
            <strong>{num(c?.limit ?? null)}</strong>
          </div>
        </div>
        {c?.slots.map((slot, i) => (
          <div className="concurrency-slot" key={`${slot.model}:${i}`}>
            <div>
              <strong>{shortModel(slot.model)}</strong>
              <span>
                {slot.state?.replaceAll('_', ' ') ?? 'State unavailable'}
              </span>
            </div>
            <div>
              <strong>
                {num(slot.running)} / {num(slot.limit ?? null)}
              </strong>
              <span>generating / slot limit · {num(slot.waiting)} waiting</span>
            </div>
            {slot.running !== null && slot.limit != null && slot.limit > 0 && (
              <meter
                min={0}
                max={slot.limit}
                value={Math.min(slot.running, slot.limit)}
                aria-label={`${slot.model} concurrent requests`}
              />
            )}
          </div>
        ))}
        <p className="footnote">
          {session?.label ?? 'Current session'} ·{' '}
          {session?.models.map(shortModel).join(' + ') ||
            'Waiting for model identity'}
          .
        </p>
        {!c && (
          <p className="footnote">
            {error ||
              (session?.performance?.status !== 'counting'
                ? 'Waiting for a fresh, verified warm session.'
                : 'A fresh signed-in Darkbloom provider reading is required. Missing counters are not zero.')}
          </p>
        )}
        <div className="reputation-actions">
          {canConnect ? (
            <Button
              variant="outline"
              onClick={() => bridge()?.postMessage({ action: 'connect' })}
            >
              Open Darkbloom connection
            </Button>
          ) : !c ? (
            <span className="small muted">
              Open the Reputation panel in the native Mac app to connect.
              Connected readings also appear on your phone.
            </span>
          ) : null}
        </div>
        <p className="footnote">Darkbloom owner API · ~15-second refresh</p>
        <details className="footnote">
          <summary>How counts work</summary>
          <p>
            Instantaneous request counts, not session totals. Coordinator
            in-flight requests may include work awaiting backend admission. Slot
            limits can differ from the machine limit. Refreshes while the
            console connection is active; no concurrency settings are changed.
          </p>
        </details>
      </section>
    );
  }
  return (
    <section className="panel reputation-panel">
      <div className="panel-heading">
        <div>
          <div className="eyebrow">YOUR NETWORK REPUTATION</div>
          <h2>How Darkbloom rates this Mac.</h2>
        </div>
        <ShieldCheck size={23} className="muted" />
      </div>
      {(error || state?.status !== 'ok') && (
        <output className="reputation-notice">
          {error ||
            (state
              ? messages[state.status]
              : 'Reading your reputation connection…')}
        </output>
      )}
      {r && (
        <div className="reputation-readout">
          <div className="reputation-score">
            <span className="small muted">
              Official score · continuing network record
            </span>
            <strong>
              {num(r.score * 100, 1)}
              <span> / 100</span>
            </strong>
            <meter
              min={0}
              max={1}
              value={r.score}
              aria-label={`Official reputation score: ${num(r.score * 100, 1)} out of 100`}
            />
            <span className={`small ${fresh ? 'connected' : 'pending'}`}>
              {paused
                ? 'View paused'
                : fresh
                  ? 'Current reading'
                  : 'Saved reading'}{' '}
              · {age(r.updatedAt, now)}
            </span>
            <small>
              {r.trustLevel
                ? `${r.trustLevel.charAt(0).toUpperCase() + r.trustLevel.slice(1)} trust`
                : 'Trust tier unavailable'}
              {r.providerStatus
                ? ` · ${r.providerStatus.replaceAll('_', ' ')}`
                : ''}
            </small>
          </div>
          <div className="reputation-details">
            <div>
              <span>Network record · job success</span>
              <strong>
                {jobsPercent == null ? '—' : `${num(jobsPercent, 2)}%`}
              </strong>
              <small>
                {num(r.successfulJobs)} successful / {num(r.totalJobs)} total ·{' '}
                {num(r.failedJobs)} failed
              </small>
            </div>
            <div>
              <span>Network record · challenges</span>
              <strong>
                {challengePercent == null
                  ? '—'
                  : `${num(challengePercent, 2)}% passed`}
              </strong>
              <small>
                {num(r.challengesPassed)} passed · {num(r.challengesFailed)}{' '}
                failed
              </small>
            </div>
            <div>
              <span>Network-record uptime</span>
              <strong>
                {r.uptimeSeconds == null
                  ? '—'
                  : `${num(r.uptimeSeconds / 3600, 1)} hours`}
              </strong>
              <small>
                Persists across service sessions when retained by Darkbloom
              </small>
            </div>
            <div>
              <span>Network-record responsiveness</span>
              <strong>
                {r.responseTimeMs && r.responseTimeMs > 0
                  ? `${num(r.responseTimeMs)} ms`
                  : '—'}
              </strong>
              <small>
                Smoothed first-content latency, adjusted for prompt prefill
              </small>
            </div>
          </div>
        </div>
      )}
      {session && (
        <div className="session-reputation">
          <h3>{session.label} · reputation during warm interval</h3>
          {sessionRep ? (
            <>
              <p className="footnote">
                From {sessionStamp(sessionRep.since)} ·{' '}
                {session.status === 'ended'
                  ? 'Ended session'
                  : paused
                    ? 'View paused'
                    : fresh && sessionRep.status === 'observed'
                      ? 'Current session observation'
                      : 'Saved observation'}{' '}
                · updated {sessionStamp(sessionRep.asOf)}.
                {sessionRep.baselineReason === 'connection_changed'
                  ? ' The network reconnected; this observation baseline restarted.'
                  : sessionRep.baselineReason === 'warm_resumed'
                    ? ' A new warm interval started; its observation baseline restarted.'
                    : ''}
                {sessionRep.status === 'reset'
                  ? ' Darkbloom reset a counter. Counter changes are unavailable for this baseline.'
                  : ''}
              </p>
              <div className="session-reputation-grid">
                <div>
                  <span>Score change</span>
                  <strong>
                    {sessionRep.scoreChange >= 0 ? '+' : ''}
                    {num(sessionRep.scoreChange, 1)} points
                  </strong>
                  <small>
                    {num(sessionRep.scoreStart * 100, 1)} →{' '}
                    {num(sessionRep.scoreNow * 100, 1)} / 100
                  </small>
                </div>
                <div>
                  <span>Jobs observed</span>
                  <strong>{num(sessionRep.totalJobs)}</strong>
                  <small>
                    {num(sessionRep.successfulJobs)} successful ·{' '}
                    {num(sessionRep.failedJobs)} failed
                  </small>
                </div>
                <div>
                  <span>Uptime credited</span>
                  <strong>
                    {sessionRep.uptimeSeconds == null
                      ? '—'
                      : `${num(sessionRep.uptimeSeconds / 60, 1)} min`}
                  </strong>
                  <small>Network-counter change during this observation</small>
                </div>
                <div>
                  <span>Challenges observed</span>
                  <strong>{num(sessionRep.challengesPassed)} passed</strong>
                  <small>{num(sessionRep.challengesFailed)} failed</small>
                </div>
              </div>
            </>
          ) : (
            <p className="footnote">
              Waiting for the first fresh reputation reading matched to{' '}
              {session.label}. Earlier readings are not assigned to this
              session.
            </p>
          )}
          <p className="footnote">
            Changes start with the first matched reading in the current
            continuous warm interval. Cold gaps pause observation and restart
            its baseline. The official score above is Darkbloom’s continuing
            record and does not reset when a model changes.
          </p>
        </div>
      )}
      <div className="reputation-actions">
        {canConnect ? (
          <>
            <Button
              variant="outline"
              onClick={() => bridge()?.postMessage({ action: 'connect' })}
            >
              {state?.status === 'ok'
                ? 'Open connection'
                : state?.status === 'disconnected'
                  ? 'Connect Darkbloom'
                  : 'Open Darkbloom sign-in'}
            </Button>
            {state?.status !== 'disconnected' && (
              <Button
                variant="ghost"
                onClick={() => bridge()?.postMessage({ action: 'disconnect' })}
              >
                Disconnect
              </Button>
            )}
          </>
        ) : (
          <span className="small muted">
            Manage the connection in Bloomkeeper on your Mac. Connected
            readings also appear here on your phone.
          </span>
        )}
        <a
          className="text-link"
          href="https://console.darkbloom.dev/earn"
          target="_blank"
          rel="noreferrer"
        >
          Darkbloom console <ArrowUpRight size={14} />
        </a>
      </div>
      <p className="footnote">
        {r
          ? 'The score comes directly from Darkbloom, scaled from 0–1 to 0–100. '
          : ''}
        Reputation is separate from hardware trust.{' '}
        {r
          ? 'The network record can carry across service restarts and model changes; it is separate from this session’s observations and your lifetime earnings history. '
          : ''}
        The sign-in stays in the app’s WebKit session; only this Mac’s
        reputation and concurrency readings are saved locally. Refreshes about
        every 15 seconds.
      </p>
    </section>
  );
});
