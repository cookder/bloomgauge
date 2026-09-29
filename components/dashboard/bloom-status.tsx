'use client';
import { useEffect, useRef, useState } from 'react';
import {
  bloomHealth,
  advanceBloomStatus,
  bloomStatusLabel,
  type BloomHealthSnapshot,
  type BloomStatusState,
} from '@/lib/bloom-status';
import { age } from './shared';

export function BloomStatus({
  data,
  now,
  error,
  paused,
  visible,
}: {
  data: BloomHealthSnapshot | null;
  now: number;
  error: string;
  paused: boolean;
  visible: boolean;
}) {
  const [startedAt] = useState(() => Date.now() / 1000),
    [state, setState] = useState<BloomStatusState | null>(null);
  const panel = useRef<HTMLDetailsElement>(null);
  const health = bloomHealth(data, now, startedAt, error, paused, visible);
  useEffect(() => {
    setState((previous) => advanceBloomStatus(previous, health, now));
  }, [health.tone, health.urgent, now]);
  useEffect(() => {
    const close = (event: PointerEvent) => {
      if (panel.current && !panel.current.contains(event.target as Node))
        panel.current.open = false;
    };
    document.addEventListener('pointerdown', close);
    return () => document.removeEventListener('pointerdown', close);
  }, []);
  // Hard faults and intentional pauses bypass the presentation grace period.
  const tone =
    health.urgent || health.tone === 'red' || health.tone === 'neutral'
      ? health.tone
      : (state?.tone ?? health.tone);
  const label =
    tone === health.tone && health.label
      ? health.label
      : tone === 'neutral' && !paused
        ? 'Background'
        : bloomStatusLabel[tone];
  const recovering = tone !== health.tone && health.tone === 'green';
  return (
    <details
      ref={panel}
      className={`bloom-status bloom-status-${tone}`}
      data-bloom-status={tone}
      onKeyDown={(event) => {
        if (event.key === 'Escape') {
          event.currentTarget.open = false;
          event.currentTarget.querySelector('summary')?.focus();
        }
      }}
    >
      <summary aria-label={`BloomGauge status: ${label}. Show details`}>
        <i aria-hidden="true" />
        <span role="status" aria-live="polite">
          {label}
        </span>
      </summary>
      <div className="bloom-status-details">
        <strong>BloomGauge status · {label}</strong>
        <p>
          {recovering
            ? 'Readings have resumed. Checking that the connection stays steady before returning to green.'
            : health.detail}
        </p>
        <dl>
          <dt>Dashboard reading</dt>
          <dd>{data ? age(data.at) : 'Not received'}</dd>
          <dt>Confirmed earnings</dt>
          <dd>
            {data?.earnings?.updatedAt
              ? age(data.earnings.updatedAt)
              : 'Not received'}
          </dd>
        </dl>
        {error && health.detail !== error && !health.detail.includes(error) && (
          <p className="small muted">{error}</p>
        )}
        <p className="small muted">
          Green: current readings. Yellow: updating or degraded. Red: needs
          attention. Gray: this view is paused or in the background, or
          statistics are paused for a lasting reason shown above. Model
          controls keep their own safety checks.
        </p>
      </div>
    </details>
  );
}
