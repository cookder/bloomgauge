'use client';
import { useEffect, useState } from 'react';
import { Choice } from './shared';
import { PulseEarningsBars } from './pulse-earnings-bars';
import {
  readPulseGraphStyle,
  type PulseGraphStyle,
} from '@/lib/pulse-earnings-bars';
import type { EarningsPulseData } from '@/lib/earnings-pulse';
import { money, type Range } from './shared';
import { RateTrend } from './rate-trend';

export function PulseTrend(props: {
  pulse?: EarningsPulseData;
  paused: boolean;
  active?: boolean;
  connected: boolean;
  windowSeconds: string;
  range: Range;
  onRangeChange: (range: Range) => void;
  reference: number;
  color: string;
}) {
  const [style, setStyle] = useState<PulseGraphStyle>('earnings');
  useEffect(() => {
    try {
      setStyle(
        readPulseGraphStyle(localStorage.getItem('bloom.pulse.graph-style.v1')),
      );
    } catch {}
  }, []);
  const choose = (value: string) => {
    const next = readPulseGraphStyle(value);
    setStyle(next);
    try {
      localStorage.setItem('bloom.pulse.graph-style.v1', next);
    } catch {}
  };
  const pulse = props.pulse;
  const reading = pulse && {
    at: pulse.at,
    streamId: pulse.streamId,
    sessionId: pulse.sessionId,
    status: pulse.status,
    models: pulse.models,
    windows: Object.fromEntries(
      Object.entries(pulse.windows).map(([key, value]) => [
        key,
        { rate: value.ratePerHour },
      ]),
    ),
  };
  return (
    <div className="pulse-earnings-graph">
      <div className="pulse-graph-picker">
        <Choice
          label="Pulse earnings graph style"
          value={style}
          onChange={choose}
          options={[
            { value: 'earnings', label: 'Earnings bars' },
            { value: 'line', label: 'Pace line' },
            { value: 'range', label: 'Pace bars + range' },
          ]}
        />
      </div>
      {style === 'earnings' ? (
        <PulseEarningsBars {...props} />
      ) : (
        <RateTrend
          key={style}
          {...props}
          pulse={reading}
          intervalOverview={style === 'range'}
          historyEndpoint="/api/pulse-history"
          title="Dollars per hour"
          unit="USD / hour"
          format={money}
          readingDecimals={4}
          readingUnit="/hr"
        />
      )}
    </div>
  );
}
