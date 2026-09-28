'use client';
import { useRef, useState, type CSSProperties } from 'react';
import { Choice, num, shortModel, type Range } from './shared';
import { RateTrend } from './rate-trend';
import { pulseDial } from '@/lib/earnings-pulse';
import {
  trafficRate,
  trafficReading,
  type TrafficPulseData,
} from '@/lib/traffic-pulse';

export function TrafficPulse({
  traffic,
  paused,
  active,
  connected,
}: {
  traffic?: TrafficPulseData;
  paused: boolean;
  active: boolean;
  connected: boolean;
}) {
  const [metric, setMetric] = useState<'tokens' | 'requests'>('tokens');
  const [range, setRange] = useState<Range>({ preset: '1h' });
  const [windowSeconds, setWindowSeconds] = useState('60');
  const reading = trafficReading(traffic, metric);
  const field = metric === 'tokens' ? 'tokensPerSecond' : 'requestsPerMinute';
  const live = connected && traffic?.status === 'live';
  const rate = live ? (reading?.windows[windowSeconds]?.rate ?? null) : null;
  const reference = trafficRate(traffic?.baseline?.[field]);
  const comparison = reference != null && reference > 0;
  const guide = metric === 'tokens' ? 20 : 6;
  const ratio = rate == null ? null : rate / (comparison ? reference : guide);
  const key = `${reading?.streamId}:${metric}:${windowSeconds}:${reference}`;
  const previous = useRef({ key, scale: 4 });
  const dial = pulseDial(
    ratio,
    previous.current.key === key ? previous.current.scale : 4,
  );
  previous.current = { key, scale: dial.scale };
  const unit = metric === 'tokens' ? 'tokens / sec' : 'requests / min';
  const color = comparison ? dial.color : 'var(--c-91bcff)';
  const referenceSeconds =
    traffic?.baseline?.[
      metric === 'tokens' ? 'tokensSeconds' : 'requestsSeconds'
    ] ?? 0;
  const label = paused
    ? 'View paused'
    : !connected
      ? 'Reconnecting'
      : traffic?.status === 'live'
        ? rate == null
          ? 'Counter unavailable'
          : rate === 0
            ? 'Ready · idle'
            : 'Live traffic'
        : traffic?.status === 'warming'
          ? 'Building a reading'
          : 'Waiting for warm traffic';
  return (
    <section className="traffic-pulse" aria-label="Live traffic pulse">
      <div className="traffic-pulse-heading">
        <div>
          <div className="eyebrow">TRAFFIC / THIS MAC</div>
          <span className="small muted">{label}</span>
        </div>
        <Choice
          label="Traffic pulse metric"
          value={metric}
          onChange={(value) => setMetric(value as 'tokens' | 'requests')}
          options={[
            { value: 'tokens', label: 'Tokens / sec' },
            { value: 'requests', label: 'Requests / min' },
          ]}
        />
      </div>
      <div className="traffic-pulse-layout">
        <div
          className="pulse-instrument"
          style={{ '--pulse-color': color } as CSSProperties}
        >
          <svg
            className="pulse-gauge"
            viewBox="0 0 300 172"
            role="img"
            aria-label={`Traffic pace: ${num(rate, 2)} ${unit}`}
          >
            <path
              className="pulse-arc-track"
              d="M 32 142 A 118 118 0 0 1 268 142"
              pathLength="100"
            />
            <path
              className="pulse-arc-value"
              d="M 32 142 A 118 118 0 0 1 268 142"
              pathLength="100"
              strokeDasharray={`${dial.fraction * 100} 100`}
            />
            {Array.from({ length: 11 }, (_, i) => {
              const angle = Math.PI - (i / 10) * Math.PI;
              return (
                <line
                  key={i}
                  x1={150 + Math.cos(angle) * 100}
                  y1={142 - Math.sin(angle) * 100}
                  x2={150 + Math.cos(angle) * 90}
                  y2={142 - Math.sin(angle) * 90}
                  className="pulse-tick"
                />
              );
            })}
            <line
              x1="150"
              y1="142"
              x2="56"
              y2="142"
              className={`pulse-needle ${rate == null ? 'inactive' : ''}`}
              style={{ transform: `rotate(${dial.fraction * 180}deg)` }}
            />
            <circle cx="150" cy="142" r="6" fill="currentColor" />
            <text x="30" y="167" textAnchor="middle">
              0
            </text>
            <text x="267" y="167" textAnchor="middle">
              {comparison ? `${num(dial.scale)}×` : num(dial.scale * guide)}
            </text>
          </svg>
          <div className="pulse-rate">
            <strong>{num(rate, 2)}</strong>
            <span>{unit}</span>
          </div>
          <p className="traffic-comparison">
            {comparison && ratio != null
              ? `${num(ratio, 2)}× earlier session pace`
              : 'Observed local traffic'}
          </p>
          <p className="small muted">
            {traffic?.sessionId
              ? `Session ${traffic.sessionId}`
              : 'No active session'}
          </p>
          <Choice
            label="Traffic rolling window"
            value={windowSeconds}
            onChange={setWindowSeconds}
            options={[
              { value: '60', label: '60s pace' },
              { value: '300', label: '5m pace' },
            ]}
          />
        </div>
        <RateTrend
          pulse={reading}
          paused={paused}
          active={active}
          connected={connected}
          windowSeconds={windowSeconds}
          range={range}
          onRangeChange={setRange}
          reference={reference}
          color={color}
          historyEndpoint={`/api/traffic-history?metric=${metric}`}
          title={metric === 'tokens' ? 'Output tokens' : 'Completed requests'}
          unit={unit}
          format={num}
          readingDecimals={2}
          minimum={metric === 'tokens' ? 1 : 0.1}
          maxSourceGap={10}
          compact
          minDecimals={0}
        />
      </div>
      <details className="traffic-method">
        <summary>How traffic is measured</summary>
        <p>
          {traffic?.models.map(shortModel).join(' + ') || 'Current model'} ·
          provider counters update about every 3 seconds. The meter uses a{' '}
          {windowSeconds === '60' ? '60-second' : 'five-minute'} rolling window
          of verified warm observations. Warm idle time counts; loading, counter
          resets and missing readings leave gaps. Traffic is completed requests
          and generated output, not confirmed earnings.
        </p>
        <p>
          {comparison
            ? `Reference: ${num(reference, 2)} ${unit} across ${num(referenceSeconds / 60, 1)} earlier measured warm minutes in this session.`
            : 'The dial uses a labeled numeric scale until an earlier session baseline is available.'}{' '}
          The baseline excludes the newest five minutes and needs five earlier
          warm minutes. History starts when this feature begins recording;
          earlier traffic is not reconstructed.
        </p>
      </details>
    </section>
  );
}
