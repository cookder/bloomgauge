'use client';
import { useEffect, useState } from 'react';
import {
  CartesianGrid,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { startChartPolling } from '@/lib/chart-polling';
import { usePageVisible } from '@/lib/use-page-visibility';
import {
  validBaselineReport,
  type BaselineReport,
  type ConditionalBaseline,
} from '@/lib/optimizer-response';
import { useScreenActive } from './app-navigation';
import {
  bounds,
  Choice,
  money,
  num,
  shortModel,
  type Range,
  plural,
} from './shared';
import type { DemandAutoData } from './demand-auto';

const date = (at: number) =>
  new Date(at * 1000).toLocaleString([], {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
const scopes: Record<string, string> = {
  weekday_time: 'Same day of week · nearby time',
  daytype_time: 'Same weekday/weekend · nearby time',
  similar_demand: 'Similar demand · all times of day',
};
const metrics = {
  usdPerHour: 'USD / warm hour',
  requestsPerMinute: 'Local requests / minute',
  tokensPerSecond: 'Local output tokens / second',
};
type Metric = keyof typeof metrics;
type Period = BaselineReport['periods'][number];

function MatchedReadout({ value }: { value: ConditionalBaseline }) {
  return (
    <div className="conditional-readout">
      <strong>
        {value.forecastUsable
          ? 'Repeated comparable paid history'
          : 'Still learning comparable demand'}
      </strong>
      <span>
        {num(value.hours, 1)} matched warm hours · {value.days} dates ·{' '}
        {value.blocks} substantial periods
      </span>
      <span>{scopes[value.scope] ?? value.scope}</span>
      {value.hours > 0 && (
        <span>
          {money(value.usdPerHour)} / hr · {num(value.requestsPerMinute, 2)}{' '}
          requests / min · {num(value.tokensPerSecond, 1)} tokens / s observed
        </span>
      )}
      <small>{value.reason}</small>
      {value.matchingCoverage != null && (
        <small>
          {num(value.matchingCoverage * 100)}% network coverage in comparison
          periods · {num(value.otherDemandHours, 1)}h at other demand ·{' '}
          {num(value.unknownDemandHours, 1)}h without paired demand. Unrelated
          history does not dilute this comparison.
        </small>
      )}
    </div>
  );
}

export function DemandBaselines({
  range,
  paused,
  enabled,
  auto,
}: {
  range: Range;
  paused: boolean;
  enabled: boolean;
  auto?: DemandAutoData;
}) {
  const [model, setModel] = useState('');
  const [metric, setMetric] = useState<Metric>('usdPerHour');
  const [saved, setSaved] = useState<{
    key: string;
    data: BaselineReport | null;
    error: string;
  } | null>(null);
  const [page, setPage] = useState(0);
  const key = JSON.stringify([range, model]);
  const data = saved?.key === key ? saved.data : null;
  const error = saved?.key === key ? saved.error : '';
  const active = useScreenActive(),
    visible = usePageVisible();
  useEffect(() => {
    if (!active || !visible || !enabled || (paused && data)) return;
    return startChartPolling({
      load: async (signal) => {
        const { start, end } = bounds(range);
        const res = await fetch(
          `/api/optimizer/baselines?from=${start}&to=${end}${model ? `&model=${encodeURIComponent(model)}` : ''}`,
          { signal, cache: 'no-store' },
        );
        if (!res.ok)
          throw Error(
            'Demand comparisons could not be refreshed. Saved readings may be out of date.',
          );
        const value: unknown = await res.json();
        if (!validBaselineReport(value))
          throw Error('Demand comparisons returned incomplete data. Retrying.');
        return value;
      },
      onValue: (value) => setSaved({ key, data: value, error: '' }),
      onError: (problem) =>
        setSaved((old) => ({
          key,
          data: old?.key === key ? old.data : null,
          error: problem.message,
        })),
      intervalMs: 30000,
      repeat: !paused,
    });
    // A new range loads once while paused; old ranges cannot be relabeled.
  }, [key, range, model, paused, active, visible, enabled]);
  useEffect(() => setPage(0), [key]);
  const live = auto?.opportunities.find((r) => r.model === data?.model);
  const stale = !!error || (!!data && Date.now() / 1000 - data.at > 90);
  const sorted = [...(data?.periods ?? [])].reverse();
  const pageCount = Math.max(1, Math.ceil(sorted.length / 20));
  const currentPage = Math.min(page, pageCount - 1);
  const chartPoint = (p: Period) => (
    <div className="baseline-tooltip">
      <strong>{date(p.at)}</strong>
      <div>
        {money(p.usdPerHour)} / warm hr · {num(p.hours * 60, 0)} warm min
      </div>
      <div>
        {num(p.pressure, 2)} load / warm · {num(p.active, 1)} active ·{' '}
        {num(p.warm, 1)} warm providers
      </div>
      <div>
        {num(p.requestsPerMinute, 2)} local req / min ·{' '}
        {num(p.tokensPerSecond, 1)} tokens / s
      </div>
    </div>
  );
  return (
    <div className="demand-baselines">
      <p className="footnote">
        What this Mac actually earned at each model’s observed demand. Choose a
        model and period to compare paid pace and traffic. Each dot groups
        observations from one time period; empty time is left out.
      </p>
      <div className="baseline-toolbar">
        <label>
          Model
          <Choice
            value={data?.model ?? model}
            label="Demand comparison model"
            onChange={setModel}
            options={(
              data?.models ??
              saved?.data?.models ??
              (model ? [{ id: model }] : [])
            ).map((r) => ({ value: r.id, label: shortModel(r.id) }))}
          />
        </label>
        <label>
          Outcome
          <Choice
            value={metric}
            label="Demand comparison outcome"
            onChange={(v) => setMetric(v as Metric)}
            options={Object.entries(metrics).map(([value, label]) => ({
              value,
              label,
            }))}
          />
        </label>
      </div>
      {error && (
        <p className="notice" role="alert">
          {error}
        </p>
      )}
      {!data ? (
        <p className="muted">
          {error
            ? 'This selection has no saved response.'
            : 'Pairing warm work with network observations…'}
        </p>
      ) : (
        <>
          <div className="baseline-stats">
            <div>
              <span>Paired warm time</span>
              <strong>{num(data.summary.hours, 1)}h</strong>
              <small>
                {num(data.baseline.coverage * 100, 0)}% of covered warm history
                · {plural(data.summary.days, 'date')}
              </small>
            </div>
            <div>
              <span>Observed paid pace</span>
              <strong>
                {money(data.summary.usdPerHour)}
                <small> / hr</small>
              </strong>
              <small>{money(data.summary.usd)} matched inference credits</small>
            </div>
            <div>
              <span>Local traffic received</span>
              <strong>
                {num(data.summary.requestsPerMinute, 2)}
                <small> / min</small>
              </strong>
              <small>
                {num(data.summary.tokensPerSecond, 1)} output tokens / s ·{' '}
                {data.summary.busyPercent == null
                  ? '—'
                  : num(100 - data.summary.busyPercent, 0) + '%'}{' '}
                idle
              </small>
            </div>
          </div>
          <div className="baseline-chart-heading">
            <strong>{metrics[metric]}</strong>
            <span className="small muted">
              {stale
                ? 'Saved data · stale'
                : paused
                  ? 'Paused view'
                  : 'Observed history'}{' '}
              · {num(data.periodSeconds / 60)}m periods
            </span>
          </div>
          {data.periods.length && active && visible && enabled ? (
            <div
              className="baseline-chart"
              role="img"
              aria-label={`${metrics[metric]} versus network load per warm provider. Exact readings in the period table below.`}
            >
              <ResponsiveContainer
                width="100%"
                height="100%"
                minWidth={0}
                initialDimension={{ width: 600, height: 250 }}
              >
                <ScatterChart
                  key={`${key}-${metric}`}
                  margin={{ top: 12, right: 12, bottom: 12, left: 0 }}
                >
                  <CartesianGrid stroke="#263642" strokeDasharray="3 6" />
                  <XAxis
                    type="number"
                    dataKey="pressure"
                    name="Load / warm"
                    domain={[0, 'auto']}
                    tick={{ fontSize: 11, fill: '#9bacb9' }}
                    tickFormatter={(v) => num(v, 1)}
                    tickCount={5}
                  />
                  <YAxis
                    type="number"
                    dataKey={metric}
                    name={metrics[metric]}
                    domain={[0, 'auto']}
                    tick={{ fontSize: 11, fill: '#9bacb9' }}
                    tickFormatter={(v) =>
                      metric === 'usdPerHour' ? money(v) : num(v, 1)
                    }
                    width={62}
                  />
                  <Tooltip
                    cursor={{ strokeDasharray: '3 3' }}
                    content={({ active, payload }) =>
                      active && payload?.[0]?.payload
                        ? chartPoint(payload[0].payload as Period)
                        : null
                    }
                  />
                  <Scatter
                    name="Weekday"
                    fill="#82efb5"
                    fillOpacity={0.75}
                    isAnimationActive={false}
                    data={data.periods.filter(
                      (p) => ![0, 6].includes(new Date(p.at * 1000).getDay()),
                    )}
                  />
                  <Scatter
                    name="Weekend"
                    fill="#ac98ff"
                    fillOpacity={0.75}
                    isAnimationActive={false}
                    data={data.periods.filter((p) =>
                      [0, 6].includes(new Date(p.at * 1000).getDay()),
                    )}
                  />
                </ScatterChart>
              </ResponsiveContainer>
            </div>
          ) : (
            <p className="notice">
              {data.periods.length
                ? 'Chart hidden while this screen is inactive.'
                : 'No paired warm work and network readings in this period. Missing overlap is unknown, not $0.'}
            </p>
          )}
          <div className="baseline-chart-caption">
            <span>Network (active + queued) / warm providers →</span>
            <span>
              <i className="weekday-dot" /> Weekday{' '}
              <i className="weekend-dot" /> Weekend
            </span>
          </div>
          <details className="baseline-detail" open>
            <summary>Demand bands · selected period</summary>
            <div
              className="baseline-table-scroll"
              tabIndex={0}
              role="region"
              aria-label="Observed outcomes by demand band; scroll for all metrics"
            >
              <table>
                <thead>
                  <tr>
                    <th>Load / warm</th>
                    <th>Active</th>
                    <th>Warm</th>
                    <th>USD / hr</th>
                    <th>Req / min</th>
                    <th>Tok / s</th>
                    <th>Warm hours</th>
                    <th>Dates</th>
                  </tr>
                </thead>
                <tbody>
                  {data.bands.map((r) => (
                    <tr key={r.low}>
                      <th>
                        {r.high === null ? `${r.low}+` : `${r.low}–<${r.high}`}
                      </th>
                      <td>{num(r.active, 1)}</td>
                      <td>{num(r.warm, 1)}</td>
                      <td>{money(r.usdPerHour)}</td>
                      <td>{num(r.requestsPerMinute, 2)}</td>
                      <td>{num(r.tokensPerSecond, 1)}</td>
                      <td>{num(r.hours, 1)}</td>
                      <td>{r.days}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>
          <details className="baseline-detail">
            <summary>
              Compare individual periods · {data.periods.length}
            </summary>
            <p className="footnote">
              Only paired warm minutes count within each{' '}
              {num(data.periodSeconds / 60)}-minute period. Partial periods show
              their actual warm time.
            </p>
            <div
              className="baseline-table-scroll"
              tabIndex={0}
              role="region"
              aria-label="Individual observed periods"
            >
              <table>
                <thead>
                  <tr>
                    <th>Local period start</th>
                    <th>Load / warm</th>
                    <th>Active</th>
                    <th>Warm</th>
                    <th>USD / hr</th>
                    <th>Req / min</th>
                    <th>Tok / s</th>
                    <th>Warm min</th>
                  </tr>
                </thead>
                <tbody>
                  {sorted
                    .slice(currentPage * 20, (currentPage + 1) * 20)
                    .map((r) => (
                      <tr key={r.at}>
                        <th>{date(r.at)}</th>
                        <td>{num(r.pressure, 2)}</td>
                        <td>{num(r.active, 1)}</td>
                        <td>{num(r.warm, 1)}</td>
                        <td>{money(r.usdPerHour)}</td>
                        <td>{num(r.requestsPerMinute, 2)}</td>
                        <td>{num(r.tokensPerSecond, 1)}</td>
                        <td>{num(r.hours * 60, 0)}</td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </div>
            <div className="baseline-pagination">
              <button
                disabled={!currentPage}
                onClick={() => setPage(currentPage - 1)}
              >
                Newer
              </button>
              <span>
                {currentPage + 1} / {pageCount}
              </span>
              <button
                disabled={currentPage >= pageCount - 1}
                onClick={() => setPage(currentPage + 1)}
              >
                Older
              </button>
            </div>
          </details>
          <h3>Matching today’s demand · selected period</h3>
          <MatchedReadout value={data.baseline} />
          {data.baseline.current && (
            <p className="small muted">
              Current network: {num(data.baseline.current.pressure, 2)} load /
              warm · {num(data.baseline.current.active, 1)} active requests ·{' '}
              {num(data.baseline.current.warm, 1)} warm providers.
            </p>
          )}
          {live?.conditional && (
            <div className="baseline-optimizer-note">
              <strong>Optimizer now · last 30 days</strong>
              <p>
                {Date.now() / 1000 - (auto?.at ?? 0) > 90
                  ? 'Saved decision · waiting for fresh optimizer readings.'
                  : live.estimate?.forecastUsable
                    ? 'Comparable history supports a conservative earnings forecast.'
                    : live.conditional.usable
                      ? 'Comparable history can gently adjust trials; there is not enough evidence for an earnings forecast.'
                      : 'History is still limited. Trials stay eligible without a history penalty.'}
              </p>
              <span>
                {num(live.conditional.hours, 1)} matched hours ·{' '}
                {num(live.historyWeight ?? 1, 2)}× trial history weight · capped
                at ±20%
              </span>
            </div>
          )}
          <p className="footnote">
            {data.scope}. {data.method} Comparisons show association, not
            guaranteed future earnings. Similar demand uses pressure, active
            requests, total load and warm provider count within a factor of two;
            near-zero counts allow a difference of one. Nearby local time is
            preferred once repeated observations exist.
          </p>
        </>
      )}
    </div>
  );
}
