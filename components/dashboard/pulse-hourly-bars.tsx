'use client';
import { useMemo, useState } from 'react';
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
import {
  overviewHourlyBars,
  overviewHourlyKey,
  readOverviewHourlyWindow,
  type HourEndForecast,
  type OverviewHourlyWindow,
} from '@/lib/hourly-earnings';
import { earningsShares, UNATTRIBUTED } from '@/lib/model-earnings';
import { TimeBucketBar } from './time-bucket-bar';
import { money, num, shortModel } from './shared';
import type { Monitor } from './widgets';

const label = (model: string) =>
  model === UNATTRIBUTED ? 'Unattributed / adjustments' : shortModel(model);

/** The Charts tab's hourly Earnings bars, compact, beside the Pulse graph. */
export function PulseHourlyBars({
  monitor,
  forecast,
  at,
}: {
  monitor?: Monitor;
  forecast?: HourEndForecast;
  at: number;
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
  const bars = useMemo(
    () => overviewHourlyBars(monitor, forecast, now, windowHours),
    [monitor, forecast, now, windowHours],
  );
  const precision = currencyAxisPrecision(bars.axisMaximum);
  const span = bars.domain[1] - bars.domain[0];
  const hasBars = bars.rows.length > 0;
  return (
    <div className="pulse-hourly" aria-label="Dollars per hour, hourly bars">
      <div className="pulse-trend-title">
        <span>Dollars per hour · last {windowHours} h</span>
        <div
          className="demand-glance-ranges pulse-hourly-ranges"
          role="group"
          aria-label="Hourly earnings range"
        >
          {([12, 24] as const).map((h) => (
            <button
              key={h}
              type="button"
              aria-pressed={windowHours === h}
              onClick={() => choose(h)}
            >
              {h}h
            </button>
          ))}
        </div>
      </div>
      <strong className="pulse-hourly-total">
        {monitor ? money(bars.total) : '—'}
        <small> earned in the last {windowHours} h</small>
      </strong>
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
                allowDataOverflow
                tickFormatter={(v) =>
                  new Date(v * 1000).toLocaleTimeString([], {
                    hour: 'numeric',
                  })
                }
                minTickGap={40}
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
                labelFormatter={(v) =>
                  new Date(Number(v) * 1000).toLocaleString([], {
                    month: 'short',
                    day: 'numeric',
                    hour: 'numeric',
                    minute: '2-digit',
                  })
                }
                formatter={(v, name, item) => {
                  const key = String(item.dataKey ?? '');
                  const shares = earningsShares(
                    bars.series.map((s) => item.payload?.byModel?.[s.key] ?? 0),
                  );
                  const index = bars.series.findIndex(
                    (s) => key === `byModel.${s.key}`,
                  );
                  const share = shares && index >= 0 ? shares[index] : null;
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
                  name={label(s.model)}
                  stackId="earnings"
                  fill={s.color}
                  maxBarSize={24}
                  shape={<TimeBucketBar spanSeconds={span} />}
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
          <p className="pulse-bars-empty">
            {monitor
              ? 'No tracked earnings in this window.'
              : 'Reading hourly earnings…'}
          </p>
        )}
      </div>
      <div className="projection-legend pulse-hourly-legend">
        {bars.series.map((s) => (
          <span key={s.model} title={label(s.model)}>
            <i style={{ background: s.color }} />
            {label(s.model)}
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
      <p className="pulse-trend-note">
        Confirmed dollars per clock hour, all models + base rewards · local
        time. Same bars and hour-end estimate as Charts.
      </p>
    </div>
  );
}
