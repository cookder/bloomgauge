'use client';
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import {
  Bar,
  CartesianGrid,
  ComposedChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { currencyAxisPrecision } from '@/lib/chart-precision';
import { startChartPolling } from '@/lib/chart-polling';
import {
  clockTicks,
  clockTime,
  isShortWindow,
  overviewHourlyBars,
  overviewHourlyKey,
  overviewHourlyLabel,
  overviewHourlyWindows,
  readOverviewHourlyWindow,
  shortBarLabel,
  shortEarningsBars,
  shortWindowSpan,
  type HourEndForecast,
  type OverviewHourlyWindow,
  type ShortWindow,
} from '@/lib/hourly-earnings';
import { earningsShares, UNATTRIBUTED } from '@/lib/model-earnings';
import {
  readNetworkContributions,
  type NetworkContributions,
} from '@/lib/network-contributions';
import { TimeBucketBar } from './time-bucket-bar';
import { money, num, shortModel } from './shared';
import type { Monitor } from './widgets';

const label = (s: { model: string; label?: string }) =>
  s.label ??
  (s.model === UNATTRIBUTED
    ? 'Unattributed / adjustments'
    : shortModel(s.model));

type BarsView = {
  rows: {
    at: number;
    byModel: Record<string, number | null>;
    projected: number | null;
  }[];
  series: { model: string; label?: string; key: string; color: string }[];
  domain: [number, number];
  axisMaximum: number;
  total: number;
  current?: { projected: number } | null;
};
type ShortRow = ReturnType<typeof shortEarningsBars>['rows'][number];

/**
 * 1 h and 3 h: the recorded-credits report (1-minute points; the source of the
 * Network tab's "My earnings" bars and the Pulse's former 2-minute bars), read
 * only while a short range is chosen and the Pulse is on screen.
 */
function useRecordedCredits(
  windowHours: ShortWindow | null,
  at: number,
  paused: boolean,
  active: boolean,
  connected: boolean,
) {
  const [saved, setSaved] = useState<{
    key: ShortWindow;
    data: NetworkContributions;
  } | null>(null);
  const [error, setError] = useState<{
    key: ShortWindow;
    message: string;
  } | null>(null);
  const [retry, setRetry] = useState(0);
  // Each poll reads the newest snapshot time without restarting the poller.
  const latest = useRef(at);
  useEffect(() => {
    latest.current = at;
  }, [at]);
  const savedKey = useRef<ShortWindow | null>(null);
  useEffect(() => {
    if (
      windowHours == null ||
      !active ||
      !connected ||
      (paused && savedKey.current === windowHours)
    )
      return;
    // A new poller starts clean: an old failure must not flash back on return.
    setError(null);
    return startChartPolling({
      load: async (signal) => {
        const span = shortWindowSpan(latest.current, windowHours);
        const url = `/api/network/contributions?metric=earnings&from=${span.from}&to=${span.to}`;
        let res = await fetch(url, { signal, cache: 'no-store' });
        // 503 while the same report is still being built (e.g. a remount aborted
        // the first request): ask once more shortly instead of showing an error.
        if (res.status === 503) {
          await new Promise((resolve) => setTimeout(resolve, 1500));
          res = await fetch(url, { signal, cache: 'no-store' });
        }
        if (!res.ok) throw Error('Recorded credits are unavailable.');
        return readNetworkContributions(await res.json(), 'earnings');
      },
      onValue: (data) => {
        savedKey.current = windowHours;
        setSaved({ key: windowHours, data });
        setError(null);
      },
      onError: (e) => setError({ key: windowHours, message: e.message }),
      intervalMs: 15000,
      repeat: !paused,
    });
  }, [windowHours, paused, active, connected, retry]);
  return {
    data: windowHours != null && saved?.key === windowHours ? saved.data : null,
    failure:
      windowHours != null && error?.key === windowHours ? error.message : '',
    retry: () => {
      savedKey.current = null;
      setRetry((n) => n + 1);
    },
  };
}

/** The Charts tab's hourly Earnings bars, compact, beside the Pulse gauge: dollars per hour
 * and the total earned over a chosen span. `today` sits beside that total. 1 h and 3 h
 * draw 5-minute bars of recorded credits instead: every model + base rewards, like
 * the hourly bars (inference only, and said so, if a report lacks base rewards). */
export function PulseHourlyBars({
  monitor,
  forecast,
  at,
  paused = false,
  active = true,
  connected = true,
  today,
}: {
  monitor?: Monitor;
  forecast?: HourEndForecast;
  at: number;
  paused?: boolean;
  active?: boolean;
  connected?: boolean;
  today?: ReactNode;
}) {
  const [windowHours, setWindowHours] = useState<OverviewHourlyWindow>(() => {
    try {
      return readOverviewHourlyWindow(localStorage.getItem(overviewHourlyKey));
    } catch {
      return 12;
    }
  });
  const choose = (value: OverviewHourlyWindow) => {
    setWindowHours(value);
    try {
      localStorage.setItem(overviewHourlyKey, String(value));
    } catch {}
  };
  const now = Math.floor(at);
  const short = isShortWindow(windowHours) ? windowHours : null;
  const credits = useRecordedCredits(short, now, paused, active, connected);
  const hourly = useMemo(
    () =>
      short ? null : overviewHourlyBars(monitor, forecast, now, windowHours),
    [monitor, forecast, now, windowHours, short],
  );
  const fine = useMemo(
    () =>
      short && credits.data ? shortEarningsBars(credits.data, short) : null,
    [short, credits.data],
  );
  const bars: BarsView = hourly ??
    fine ?? {
      rows: [],
      series: [],
      domain: [now - 3600, now],
      axisMaximum: 0,
      total: 0,
      current: null,
    };
  const precision = currencyAxisPrecision(bars.axisMaximum);
  const span = bars.domain[1] - bars.domain[0];
  const hasBars = fine
    ? fine.series.length > 0 && fine.rows.some((r) => r.known)
    : bars.rows.length > 0;
  const days = windowHours > 24;
  // Past a day, one tick per local midnight (DST-safe), labelled with the weekday.
  const midnights = useMemo(() => {
    if (!days) return undefined;
    const ticks: number[] = [];
    const day = new Date(bars.domain[0] * 1000);
    day.setHours(24, 0, 0, 0);
    while (day.getTime() / 1000 <= bars.domain[1]) {
      ticks.push(day.getTime() / 1000);
      day.setDate(day.getDate() + 1);
    }
    return ticks;
  }, [days, bars.domain]);
  // 1 h: a tick every quarter hour; 3 h: every half hour (10:15, 10:30…).
  const clock = useMemo(
    () => (short ? clockTicks(bars.domain, short === 1 ? 15 : 30) : undefined),
    [short, bars.domain],
  );
  const shortEmpty = !credits.data
    ? credits.failure
      ? credits.failure
      : connected
        ? 'Reading recorded credits…'
        : 'Waiting for a connection…'
    : credits.data.status === 'unavailable'
      ? (credits.data.notes[0] ?? 'Recorded credits are unavailable.')
      : fine?.recorded
        ? 'No credits in this window.'
        : 'No recorded credit readings in this window.';
  return (
    <div
      className="pulse-hourly"
      aria-label={
        short
          ? 'Dollars per hour, 5-minute bars'
          : 'Dollars per hour, hourly bars'
      }
    >
      <div className="pulse-trend-title">
        <span>Dollars per hour · last {overviewHourlyLabel(windowHours)}</span>
        <div
          className="demand-glance-ranges pulse-hourly-ranges"
          role="group"
          aria-label="Hourly earnings range"
        >
          {overviewHourlyWindows.map((h) => (
            <button
              key={h}
              type="button"
              aria-pressed={windowHours === h}
              onClick={() => choose(h)}
            >
              {overviewHourlyLabel(h, true)}
            </button>
          ))}
        </div>
      </div>
      <div className="pulse-hourly-head">
        <strong className="pulse-hourly-total">
          {(short ? fine?.recorded : monitor) ? money(bars.total) : '—'}
          <small>
            {' '}
            earned in the last {overviewHourlyLabel(windowHours)}
            {fine && !fine.baseRewards ? ' · inference credits' : ''}
          </small>
        </strong>
        {today}
      </div>
      <div className="pulse-hourly-chart">
        {hasBars ? (
          <ResponsiveContainer
            width="100%"
            height="100%"
            minWidth={0}
            initialDimension={{ width: 320, height: 150 }}
          >
            <ComposedChart<{ at: number }>
              key={windowHours}
              stackOffset="sign"
              data={bars.rows}
              margin={{ top: 8, right: 4, left: 0, bottom: 0 }}
            >
              <CartesianGrid
                stroke="var(--border)"
                vertical={false}
                strokeDasharray="2 6"
              />
              <XAxis
                dataKey="at"
                type="number"
                domain={bars.domain}
                ticks={clock ?? midnights}
                allowDataOverflow
                tickFormatter={(v) =>
                  short
                    ? clockTime(v)
                    : days
                      ? new Date(v * 1000).toLocaleDateString([], {
                          weekday: 'short',
                        })
                      : new Date(v * 1000).toLocaleTimeString([], {
                          hour: 'numeric',
                        })
                }
                minTickGap={short ? 12 : 40}
                tick={{ fill: 'var(--muted-foreground)', fontSize: 10 }}
                axisLine={false}
                tickLine={false}
              />
              <YAxis
                tickFormatter={(v) => money(v, precision)}
                width={Math.max(
                  40,
                  money(bars.axisMaximum, precision).length * 6.5 + 6,
                )}
                tickCount={3}
                tick={{ fill: 'var(--muted-foreground)', fontSize: 10 }}
                axisLine={false}
                tickLine={false}
              />
              <Tooltip
                cursor={{ fill: 'var(--muted)', fillOpacity: 0.5 }}
                contentStyle={{
                  background: 'var(--popover)',
                  border: '1px solid var(--border)',
                  borderRadius: 10,
                  color: 'var(--popover-foreground)',
                }}
                labelStyle={{ color: 'var(--popover-foreground)' }}
                labelFormatter={(v, payload) => {
                  const row = payload?.[0]?.payload as ShortRow | undefined;
                  if (short && row) return shortBarLabel(row);
                  return new Date(Number(v) * 1000).toLocaleString([], {
                    month: 'short',
                    day: 'numeric',
                    hour: 'numeric',
                    minute: '2-digit',
                  });
                }}
                formatter={(v, name, item) => {
                  const key = String(item.dataKey ?? '');
                  const shares = earningsShares(
                    bars.series.map((s) => item.payload?.byModel?.[s.key] ?? 0),
                  );
                  const index = bars.series.findIndex(
                    (s) => key === `byModel.${s.key}`,
                  );
                  const share = shares && index >= 0 ? shares[index] : null;
                  if (short)
                    return [
                      `${money(Number(v))}/h · ${money(Number(v) / 12)} earned${share != null ? ` · ${num(share, 1)}%` : ''}`,
                      name,
                    ];
                  return [
                    `${money(Number(v))}${share != null ? ` · ${num(share, 1)}% of hour total` : ''}`,
                    name === 'projected'
                      ? 'Estimated remaining earnings'
                      : name,
                  ];
                }}
              />
              {bars.series.map((s) => (
                <Bar
                  key={s.model}
                  dataKey={`byModel.${s.key}`}
                  name={label(s)}
                  stackId="earnings"
                  fill={s.color}
                  maxBarSize={24}
                  shape={
                    <TimeBucketBar
                      spanSeconds={span}
                      bucketSeconds={short ? 300 : 3600}
                    />
                  }
                  activeBar={false}
                  isAnimationActive={false}
                />
              ))}
              <Bar
                dataKey="projected"
                stackId="earnings"
                fill="var(--primary)"
                fillOpacity={0.22}
                stroke="var(--primary)"
                strokeOpacity={0.45}
                strokeDasharray="3 3"
                radius={[3, 3, 0, 0]}
                maxBarSize={24}
                shape={<TimeBucketBar spanSeconds={span} />}
                isAnimationActive={false}
              />
            </ComposedChart>
          </ResponsiveContainer>
        ) : (
          <p className="pulse-bars-empty" role={short ? 'status' : undefined}>
            {short
              ? shortEmpty
              : monitor
                ? 'No tracked earnings in this window.'
                : 'Reading hourly earnings…'}
            {short && credits.failure && !credits.data && (
              <>
                {' '}
                <button
                  type="button"
                  className="pulse-smoothing-toggle"
                  onClick={credits.retry}
                >
                  Retry
                </button>
              </>
            )}
          </p>
        )}
      </div>
      <div className="projection-legend pulse-hourly-legend">
        {bars.series.map((s) => (
          <span key={s.model} title={label(s)}>
            <i style={{ background: s.color }} />
            {label(s)}
          </span>
        ))}
        {bars.current && (
          <span>
            <i className="faint" />
            Estimated rest of this hour · hour end ≈
            {money(bars.current.projected)}
          </span>
        )}
      </div>
      {short ? (
        <p className="pulse-trend-note">
          {fine && !fine.baseRewards
            ? '5-minute bars: confirmed inference credits × 12, as dollars per hour · base rewards show in the hourly views · local time.'
            : '5-minute bars: confirmed dollars × 12, as dollars per hour · all models + base rewards · local time.'}
          {fine && fine.blanks > 0
            ? ` ${fine.blanks} blank ${fine.blanks === 1 ? 'bar was' : 'bars were'} not fully recorded and ${fine.blanks === 1 ? 'isn’t' : 'aren’t'} counted.`
            : ''}
          {credits.failure && credits.data
            ? ` ${credits.failure} Showing the last reading.`
            : ''}
        </p>
      ) : (
        <p className="pulse-trend-note">
          Confirmed dollars per clock hour, all models + base rewards · local
          time. Same bars and hour-end estimate as Charts.
        </p>
      )}
    </div>
  );
}
