'use client';
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { HistoryNote, money, num, type Range, useHistory } from './shared';
import type { ModelProjection } from '@/lib/cumulative-earnings';
import { TimeBucketBar } from './time-bucket-bar';
import { useScreenActive } from './app-navigation';
import { earningsTone, earningsToneLabel } from '@/lib/daily-earnings';
import { useEarningsTiers } from './daily-earnings';

export type Forecast = {
  modelProjection?: ModelProjection;
  at: number;
  hourStart: number;
  hourEnd: number;
  earnings: {
    status: string;
    actual: number | null;
    projected: number | null;
    additional: number | null;
    detail: string;
    jobsProjected: number | null;
  };
  throughput: {
    status: string;
    actual: number | null;
    projected: number | null;
    additional: number | null;
    expectedRate: number | null;
    partial?: boolean;
    detail: string;
  };
};
export function ForecastSummary({
  value,
  kind,
}: {
  value?: Forecast;
  kind: 'earnings' | 'throughput';
}) {
  const tiers = useEarningsTiers(kind === 'earnings' && !!value);
  if (!value) return null;
  const f = value[kind],
    ready = f.status === 'ready';
  const time = new Date(value.hourEnd * 1000).toLocaleTimeString([], {
    hour: 'numeric',
    minute: '2-digit',
  });
  const e = value.earnings,
    t = value.throughput;
  const tone = earningsTone(
    ready ? e.projected : null,
    (value.hourEnd - value.hourStart) / 3600,
    tiers,
  );
  return (
    <div className={`forecast-summary ${kind}`}>
      <div className="forecast-title">
        <span className="forecast-dot" />
        End-of-hour estimate <span>{time}</span>
      </div>
      {ready ? (
        <div className="forecast-values">
          {kind === 'earnings' ? (
            <>
              <div>
                <span>Expected total</span>
                <strong
                  className="earnings-tone"
                  data-tone={tone}
                  title={
                    tiers
                      ? `${earningsToneLabel[tone]} pace · this Mac’s daily scale divided by 24`
                      : 'Not rated until this Mac has 7 complete days'
                  }
                >
                  ≈{money(e.projected)}
                </strong>
              </div>
              <div>
                <span>Still to earn</span>
                <strong className="earnings-tone" data-tone={tone}>
                  +{money(e.additional)}
                </strong>
              </div>
              {e.jobsProjected != null && (
                <div>
                  <span>Expected jobs</span>
                  <strong>≈{num(e.jobsProjected)}</strong>
                </div>
              )}
            </>
          ) : (
            <>
              <div>
                <span>Expected pace</span>
                <strong>
                  ≈{num(t.expectedRate, 1)} <small>tok/s</small>
                </strong>
              </div>
              <div>
                <span>
                  {t.partial ? 'Remaining output' : 'Expected hour total'}
                </span>
                <strong>
                  ≈{num(t.partial ? t.additional : t.projected)}{' '}
                  <small>tokens</small>
                </strong>
              </div>
            </>
          )}
        </div>
      ) : (
        <p className="forecast-unavailable">{f.detail}</p>
      )}
      {ready && (
        <p className="forecast-explanation">
          {f.detail} Estimates assume the provider stays online and demand
          follows the recent pace.
        </p>
      )}
    </div>
  );
}

export function HourlyOutputChart({
  range,
  paused,
  forecast,
}: {
  range: Range;
  paused: boolean;
  forecast?: Forecast;
}) {
  const screenActive = useScreenActive();
  const { data, error } = useHistory(range, paused, 'output');
  const showProjection =
    forecast?.throughput.status === 'ready' &&
    (!range.end || range.end >= forecast.at);
  const rows = (data?.samples ?? []).map((row) => ({
    ...row,
    projected:
      showProjection &&
      Number(row.at) <= forecast!.hourStart &&
      Number(row.at) + (data?.bucketSeconds ?? 3600) > forecast!.hourStart
        ? forecast!.throughput.additional
        : null,
  }));
  const bucketSeconds = data?.bucketSeconds ?? 3600;
  const firstAt = rows[0]?.at ?? 0;
  const lastAt = rows.at(-1)?.at ?? firstAt;
  const domain: [number, number] = [
    firstAt - bucketSeconds / 2,
    lastAt + bucketSeconds / 2,
  ];
  return (
    <>
      <div className="output-bars">
        {!screenActive ? null : rows.length ? (
          <ResponsiveContainer
            width="100%"
            height={220}
            minWidth={0}
            initialDimension={{ width: 700, height: 220 }}
          >
            <BarChart
              key={JSON.stringify(range)}
              data={rows}
              margin={{ top: 10, right: 8, bottom: 0, left: -12 }}
            >
              <CartesianGrid
                vertical={false}
                stroke="var(--c-27303c)"
                strokeDasharray="2 6"
              />
              <XAxis
                dataKey="at"
                type="number"
                domain={domain}
                allowDataOverflow
                tickFormatter={(v) =>
                  new Date(v * 1000).toLocaleString([], {
                    month: 'short',
                    day: 'numeric',
                    hour: 'numeric',
                  })
                }
                tick={{ fill: 'var(--c-8997aa)', fontSize: 12 }}
                minTickGap={60}
                axisLine={false}
                tickLine={false}
              />
              <YAxis
                tickFormatter={(v) =>
                  Intl.NumberFormat(undefined, { notation: 'compact' }).format(
                    v,
                  )
                }
                width={70}
                tick={{ fill: 'var(--c-8997aa)', fontSize: 12 }}
                axisLine={false}
                tickLine={false}
              />
              <Tooltip
                contentStyle={{
                  background: 'var(--c-171d27)',
                  border: '1px solid var(--c-354153)',
                  borderRadius: 10,
                }}
                labelFormatter={(v) =>
                  new Date(Number(v) * 1000).toLocaleString()
                }
                formatter={(v, name) => [
                  `≈${num(Number(v))} tokens`,
                  name === 'projected'
                    ? 'Estimated remaining output'
                    : 'Recorded output (sampled)',
                ]}
              />
              <Bar
                dataKey="outputTokens"
                stackId="output"
                fill="var(--c-a995ff)"
                maxBarSize={26}
                shape={
                  <TimeBucketBar
                    spanSeconds={domain[1] - domain[0]}
                    bucketSeconds={bucketSeconds}
                  />
                }
                isAnimationActive={false}
              />
              <Bar
                dataKey="projected"
                stackId="output"
                fill="var(--c-a995ff)"
                fillOpacity={0.22}
                stroke="var(--c-a995ff)"
                strokeOpacity={0.45}
                strokeDasharray="3 3"
                radius={[3, 3, 0, 0]}
                maxBarSize={26}
                shape={
                  <TimeBucketBar
                    spanSeconds={domain[1] - domain[0]}
                    bucketSeconds={bucketSeconds}
                  />
                }
                isAnimationActive={false}
              />
            </BarChart>
          </ResponsiveContainer>
        ) : (
          <div className="empty">
            {error || 'No recorded output in this window.'}
          </div>
        )}
      </div>
      <HistoryNote data={data} error={error} aggregation="total" />
      <div className="projection-legend">
        <span>
          <i className="output-solid" />
          Recorded output
        </span>
        <span>
          <i className="output-faint" />
          Estimated rest of this hour
        </span>
      </div>
      <p className="panel-note">
        Totals approximate provider counter samples. Missing readings are
        excluded; bars include entire boundary hours.
        {(data?.bucketSeconds ?? 3600) > 3600
          ? ` Grouped into ${Math.round(data!.bucketSeconds / 3600)}-hour bars.`
          : ''}
        {forecast?.throughput.partial
          ? ' The current hour is only partly recorded.'
          : ''}
      </p>
    </>
  );
}
