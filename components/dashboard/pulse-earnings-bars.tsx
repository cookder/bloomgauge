'use client';
import { useEffect, useMemo, useRef, useState } from 'react';
import type { EarningsPulseData } from '@/lib/earnings-pulse';
import {
  readNetworkContributions,
  type NetworkContributions,
} from '@/lib/network-contributions';
import { pulseEarningsBars } from '@/lib/pulse-earnings-bars';
import { startChartPolling } from '@/lib/chart-polling';
import { modelColor } from '@/lib/model-earnings';
import { paceInterval } from '@/lib/pulse-display';
import {
  bounds,
  money,
  num,
  RangePicker,
  shortModel,
  type Range,
} from './shared';
import { ContributionChart } from './contribution-chart';

export function PulseEarningsBars({
  pulse,
  paused,
  active = true,
  connected,
  range,
  onRangeChange,
}: {
  pulse?: EarningsPulseData;
  paused: boolean;
  active?: boolean;
  connected: boolean;
  range: Range;
  onRangeChange: (range: Range) => void;
}) {
  const [saved, setSaved] = useState<{
    key: string;
    data: NetworkContributions;
  } | null>(null);
  const [error, setError] = useState<{ key: string; message: string } | null>(
    null,
  );
  const [inspection, setInspection] = useState<number | null>(null),
    [retry, setRetry] = useState(0);
  const latest = useRef(pulse),
    savedKey = useRef<string | null>(null);
  latest.current = pulse;
  const key = JSON.stringify([pulse?.streamId, pulse?.sessionId, range]);
  const b = bounds(range, pulse?.at),
    supported = range.preset !== 'all' && b.end - b.start <= 31 * 86400;
  useEffect(() => setInspection(null), [key]);
  useEffect(() => {
    if (
      !active ||
      !connected ||
      !supported ||
      (paused && savedKey.current === key)
    )
      return;
    return startChartPolling({
      load: async (signal) => {
        const b = bounds(range, latest.current?.at);
        // Whole 30 s steps so repeat polls (and other open views) reuse the
        // backend's 30 s cache instead of recomputing ~1 s of history each time.
        const step = (t: number) => Math.floor(t / 30) * 30;
        const res = await fetch(
          `/api/network/contributions?metric=earnings&from=${step(b.start)}&to=${step(b.end)}`,
          { signal, cache: 'no-store' },
        );
        if (!res.ok) throw Error('Earnings history is unavailable.');
        return readNetworkContributions(await res.json(), 'earnings');
      },
      onValue: (data) => {
        savedKey.current = key;
        setSaved({ key, data });
        setError(null);
      },
      onError: (e) => setError({ key, message: e.message }),
      intervalMs: 15000,
      repeat: !paused,
    });
  }, [key, paused, active, connected, supported, retry]);
  const data = saved?.key === key ? saved.data : null,
    failure = error?.key === key ? error.message : '';
  const bars = useMemo(() => (data ? pulseEarningsBars(data) : null), [data]);
  const selected = inspection !== null ? bars?.points[inspection] : undefined;
  const series =
    data?.series.map((s) => ({
      id: s.id,
      label: s.name,
      color:
        s.id === 'other' || s.id === 'unattributed'
          ? '#899da8'
          : modelColor(s.id.replace(/^model:/, '')),
    })) ?? [];
  const stamp = (at: number) =>
    new Date(at * 1000).toLocaleString(
      [],
      data && data.to - data.from >= 86400
        ? { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }
        : { hour: 'numeric', minute: '2-digit' },
    );
  const hasValues = bars?.points.some((p) => p.values.some((v) => v !== null));
  return (
    <div
      className="pulse-trend pulse-earnings-bars"
      aria-label="Pulse earnings bars"
    >
      <div className="pulse-trend-title">
        <span>Dollars per hour</span>
        <span className="pulse-trend-status">
          {paused
            ? 'Paused'
            : !connected
              ? 'Saved history'
              : 'Recorded credits'}
        </span>
      </div>
      <div className="pulse-trend-range">
        <RangePicker
          value={range}
          onChange={onRangeChange}
          label="Dollars per hour chart date range"
          presets={[
            '5m',
            '15m',
            '1h',
            '4h',
            '8h',
            '12h',
            '24h',
            '7d',
            '30d',
            ...(range.preset === 'all' ? ['all'] : []),
            'custom',
          ]}
        />
      </div>
      {!supported ? (
        <p className="pulse-trend-note">
          Earnings bars support up to 31 days. Choose a shorter range, or use a
          pace graph for older history.
        </p>
      ) : (
        <>
          {failure && (
            <p className="pulse-trend-note" role="status">
              {failure} {data ? 'Showing saved credits for this range.' : ''}{' '}
              <button
                type="button"
                className="pulse-smoothing-toggle"
                onClick={() => {
                  savedKey.current = null;
                  setRetry((n) => n + 1);
                }}
              >
                Retry
              </button>
            </p>
          )}
          {!data && !failure && (
            <p className="pulse-bars-empty" role="status">
              {connected
                ? 'Reading recorded credits…'
                : 'Waiting for a connection…'}
            </p>
          )}
          {data && bars && (
            <>
              {hasValues ? (
                <ContributionChart
                  rows={bars.points}
                  series={series}
                  from={data.from}
                  to={data.to}
                  selected={selected ? inspection : null}
                  onSelect={setInspection}
                  format={(value) =>
                    money(value, Math.abs(value) < 0.01 && value !== 0 ? 4 : 2)
                  }
                  unit="USD / elapsed hour"
                  height={190}
                  barGap={1.5}
                  label="Pulse earnings by model"
                  sliderLabel="Inspect Pulse earnings timeline"
                />
              ) : (
                <p className="pulse-bars-empty">
                  {data.status === 'unavailable'
                    ? data.notes[0]
                    : 'No covered earnings intervals in this range.'}
                </p>
              )}
              {selected && (
                <div className="pulse-bars-inspection" role="status">
                  <strong>
                    {selected.values.every((v) => v !== null)
                      ? `${money(
                          selected.values.reduce<number>((n, v) => n + v!, 0),
                          4,
                        )} /hr`
                      : 'Incomplete interval'}
                  </strong>
                  <span>
                    {stamp(selected.from)} – {stamp(selected.to)} ·{' '}
                    {num(selected.coverageFraction * 100, 0)}% recorded
                  </span>
                  <button
                    type="button"
                    className="pulse-smoothing-toggle"
                    onClick={() => setInspection(null)}
                  >
                    Clear selection
                  </button>
                </div>
              )}
              <div
                className="pulse-model-legend"
                aria-label="Dollars per hour model colors"
              >
                {series.map((s, i) => (
                  <span key={s.id} title={s.label}>
                    <i style={{ background: s.color }} />
                    <span>
                      {shortModel(s.label)}
                      {selected
                        ? ` · ${selected.values[i] === null ? 'Unknown' : `${money(selected.values[i])} /hr`}`
                        : ''}
                    </span>
                  </span>
                ))}
              </div>
              <p className="pulse-trend-note">
                {paceInterval(bars.bucketSeconds)} bars ·{' '}
                {num(data.coverage.fraction * 100, 1)}% recorded · local time
                <br />
                Confirmed model credits / elapsed time; base rewards excluded.
                Gaps stay unknown. The meter shows rolling warm-time pace.
              </p>
            </>
          )}
        </>
      )}
    </div>
  );
}
