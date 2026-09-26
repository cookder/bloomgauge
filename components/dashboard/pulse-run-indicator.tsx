'use client';
import { observeModelResult } from '@/lib/support-issues';
import { useEffect, useState } from 'react';
import { ChevronDown, Info } from 'lucide-react';
import { startChartPolling } from '@/lib/chart-polling';
import {
  validOptimizerResponse,
  withoutInvalidHistory,
} from '@/lib/optimizer-response';
import { pulseRunStatus, type RunSession } from '@/lib/pulse-run-status';
import { useAppNavigation } from './app-navigation';
import { shortModel } from './shared';

export function PulseRunIndicator({
  session,
  active,
  paused,
  connected,
  reporting,
}: {
  session?: RunSession | null;
  active: boolean;
  paused: boolean;
  connected: boolean;
  reporting?: unknown;
}) {
  const { navigate } = useAppNavigation();
  const key = JSON.stringify([session?.id, session?.models]);
  const [reading, setReading] = useState<{
    key: string;
    value: unknown;
  } | null>(null);
  const [failed, setFailed] = useState(false);
  const [now, setNow] = useState(Date.now() / 1000);
  useEffect(() => {
    if (!active || paused) return;
    const timer = setInterval(() => setNow(Date.now() / 1000), 5000);
    return () => clearInterval(timer);
  }, [active, paused]);
  useEffect(() => {
    if (!active || paused) return;
    return startChartPolling({
      issueContext: 'models',
      load: async (signal) => {
        // view=run: only the live run fields (a few KB instead of ~300 KB). The
        // endpoint still summarizes history, so bound that work to one hour.
        const end = Math.floor(Date.now() / 1000);
        const response = await fetch(
          `/api/optimizer?from=${end - 3600}&to=${end}&view=run`,
          { signal, cache: 'no-store' },
        );
        if (!response.ok) throw Error('Run status unavailable');
        const value: unknown = withoutInvalidHistory(await response.json());
        if (!validOptimizerResponse(value))
          throw Error('Incomplete run status');
        return value;
      },
      onValue: (value) => {
        observeModelResult(
          (value as { lastSwitchResult?: unknown }).lastSwitchResult,
        );
        setReading({ key, value });
        setFailed(false);
        setNow(Date.now() / 1000);
      },
      onError: () => setFailed(true),
      intervalMs: 15000,
    });
  }, [active, paused, key]);
  const value =
    reading?.key === key && !failed && connected ? reading.value : null;
  const status = pulseRunStatus(
    value,
    session,
    now,
    connected ? reporting : null,
  );
  return (
    <details className="pulse-run-indicator run-details" key={key}>
      <summary aria-label="Model run details">
        <Info size={15} aria-hidden="true" />
        <strong>Model details</strong>
        <span className="pulse-run-progress">
          {session?.models.map(shortModel).join(' + ')}
        </span>
        <ChevronDown
          size={14}
          className="pulse-run-chevron"
          aria-hidden="true"
        />
      </summary>
      <div className="pulse-run-detail">
        <p>
          <strong>{paused ? 'View paused' : status.label}</strong>
          {!paused && status.progress ? ` · ${status.progress}` : ''}
        </p>
        {session && (
          <strong>
            {session.models.map(shortModel).join(' + ')} · Session {session.id}
          </strong>
        )}
        <p>
          {paused
            ? 'Resume the live view to refresh the run indicator. The optimizer continues independently.'
            : status.detail}
        </p>
        {!paused && status.alternative && (
          <p className="pulse-paid-alternative">
            <strong>
              Paid alternative · {shortModel(status.alternative.model)}
            </strong>
            <br />
            {status.alternative.reason}
          </p>
        )}
        {!paused && status.reason && <p>{status.reason}</p>}
        <button type="button" onClick={() => navigate('test')}>
          View optimizer details →
        </button>
      </div>
    </details>
  );
}
