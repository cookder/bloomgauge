'use client';
import { memo, useEffect, useMemo, useState } from 'react';
import { Check, TrendingUp } from 'lucide-react';
import { startChartPolling } from '@/lib/chart-polling';
import { modelColor } from '@/lib/model-earnings';
import { usePageVisible } from '@/lib/use-page-visibility';
import { useScreenActive } from './app-navigation';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import {
  age,
  bounds,
  Choice,
  num,
  RangePicker,
  TimeChart,
  type Range,
} from './shared';

export type Capacity = {
  id: string;
  ready: boolean;
  can_accept: boolean;
  routable_providers: number;
  warm_providers: number;
  running_providers: number;
  active_requests: number;
  queued_requests: number;
  aggregate_tps: number;
  estimated_ttft_ms: number;
};
type Metric = 'load' | 'active' | 'queued' | 'pressure' | 'share' | 'warm';
type Point = { at: number } & Record<Metric, number | null>;
type Demand = {
  id: string;
  averageLoad: number;
  averageActive: number;
  averageQueued: number;
  peakLoad: number;
  averagePressure: number | null;
  sharePercent: number | null;
  samples: number;
  first: number;
  last: number;
  coveragePercent: number;
  noWarmSamples: number;
  firstHalfAverage: number | null;
  lastHalfAverage: number | null;
  firstHalfCoverage: number;
  lastHalfCoverage: number;
  trendPercent: number | null;
  trendState: 'up' | 'down' | 'steady' | 'new' | 'insufficient';
  chart: Point[];
};
type DemandHistory = {
  models: Demand[];
  from: number;
  to: number;
  count: number;
  coverageStart: number | null;
  coverageEnd: number | null;
  bucketSeconds: number;
  comparison: {
    start: number;
    midpoint: number;
    end: number;
    minimumCoverage: number;
  };
};
const emptyCapacity: Capacity[] = [];
const emptySelection: string[] = [];
const metrics = [
  { value: 'load', label: 'Active + queued requests' },
  { value: 'active', label: 'Concurrency · active requests' },
  { value: 'queued', label: 'Queued requests' },
  { value: 'pressure', label: 'Load / warm provider' },
  { value: 'share', label: 'Share of sampled load (%)' },
  { value: 'warm', label: 'Warm providers' },
];
const date = (at: number) =>
  new Date(at * 1000).toLocaleString([], {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
// Keep quantization and concrete identifiers: some network variants share a name.
const name = (id: string) =>
  id.replace(/^EigenLabs\//, '').replaceAll('-', ' ');
const trend = (m?: Demand) =>
  !m || m.trendState === 'insufficient'
    ? 'More history needed'
    : m.trendState === 'new'
      ? 'Up from zero'
      : m.trendState === 'steady'
        ? 'Unchanged'
        : `${m.trendState === 'up' ? '+' : ''}${num(m.trendPercent, 1)}%`;

export const ModelDemandPanel = memo(function ModelDemandPanel({
  capacity,
  paused,
}: {
  capacity?: {
    status: string;
    updatedAt?: number;
    error?: string;
    data?: { models: Capacity[] };
  };
  paused: boolean;
}) {
  const [range, setRange] = useState<Range>({ preset: '1h' });
  const queryKey = JSON.stringify(range);
  const [saved, setSaved] = useState<{
    key: string;
    data: DemandHistory;
  } | null>(null);
  const [failure, setFailure] = useState<{
    key: string;
    message: string;
  } | null>(null);
  const data = saved?.key === queryKey ? saved.data : null;
  const error = failure?.key === queryKey ? failure.message : '';
  const active = useScreenActive();
  const pageVisible = usePageVisible();
  const [metric, setMetric] = useState<Metric>('load');
  const [selected, setSelected] = useState<string[] | null>(null);
  const [sort, setSort] = useState('average');
  const [query, setQuery] = useState('');
  const [now, setNow] = useState(() => Date.now() / 1000);

  useEffect(() => {
    if (!active || !pageVisible || (paused && saved?.key === queryKey)) return;
    return startChartPolling({
      load: async (signal) => {
        const b = bounds(range);
        const response = await fetch(
          `/api/network/models?from=${b.start}&to=${b.end}`,
          { signal, cache: 'no-store' },
        );
        if (!response.ok)
          throw new Error(
            `Demand history is unavailable (${response.status}).`,
          );
        const next = (await response.json()) as DemandHistory;
        if (
          !next ||
          !Array.isArray(next.models) ||
          !next.comparison ||
          !next.models.every(
            (row) =>
              row && typeof row.id === 'string' && Array.isArray(row.chart),
          )
        )
          throw new Error('Demand history returned an incomplete response.');
        return next;
      },
      onValue: (next) => {
        setSaved({ key: queryKey, data: next });
        setFailure(null);
        setNow(Date.now() / 1000);
        // Ranking changes must not replace the models being followed.
        if (next.models.length)
          setSelected(
            (previous) => previous ?? next.models.slice(0, 3).map((m) => m.id),
          );
      },
      onError: (error) => {
        setFailure({ key: queryKey, message: error.message });
        setNow(Date.now() / 1000);
      },
      intervalMs: 30000,
      repeat: !paused,
    });
  }, [queryKey, range, paused, active, pageVisible]);

  const current = capacity?.data?.models ?? emptyCapacity;
  const fresh =
    capacity?.status === 'ok' &&
    capacity.updatedAt != null &&
    now - capacity.updatedAt < 90;
  const rows = useMemo(() => {
    const live = new Map(current.map((m) => [m.id, m]));
    const saved = new Map((data?.models ?? []).map((m) => [m.id, m]));
    const score = (id: string) => {
      const m = saved.get(id),
        c = live.get(id);
      if (sort === 'now')
        return c ? c.active_requests + c.queued_requests : -Infinity;
      if (sort === 'queued') return m?.averageQueued ?? -Infinity;
      if (sort === 'pressure') return m?.averagePressure ?? -Infinity;
      if (sort === 'trend')
        return m?.trendState === 'new'
          ? Infinity
          : (m?.trendPercent ?? -Infinity);
      return m?.averageLoad ?? -Infinity;
    };
    return [...new Set([...live.keys(), ...saved.keys()])]
      .sort((a, b) => score(b) - score(a) || a.localeCompare(b))
      .map((id) => ({ id, live: live.get(id), saved: saved.get(id) }));
  }, [current, data, sort]);
  const chosen = selected ?? emptySelection;
  const series = chosen.map((id, i) => ({
    key: `model${i}`,
    label: name(id),
    color: modelColor(id),
  }));
  const chart = useMemo(() => {
    const points = new Map<
      number,
      { at: number; [key: string]: number | null }
    >();
    chosen.forEach((id, i) => {
      data?.models
        .find((m) => m.id === id)
        ?.chart.forEach((point) => {
          const row = points.get(point.at) ?? { at: point.at };
          row[`model${i}`] = point[metric];
          points.set(point.at, row);
        });
    });
    return [...points.values()].sort((a, b) => a.at - b.at);
  }, [data, chosen, metric]);
  const leader = data?.models[0];
  const busiest = [...current].sort(
    (a, b) =>
      b.active_requests +
      b.queued_requests -
      a.active_requests -
      a.queued_requests,
  )[0];
  const rising = data?.models
    .filter((m) => m.trendPercent != null && m.trendPercent > 0)
    .sort((a, b) => b.trendPercent! - a.trendPercent!)[0];
  const visible = rows.filter((m) =>
    m.id.toLowerCase().includes(query.trim().toLowerCase()),
  );
  function toggle(id: string) {
    setSelected((previous) => {
      const ids = previous ?? [];
      return ids.includes(id)
        ? ids.filter((x) => x !== id)
        : ids.length < 6
          ? [...ids, id]
          : ids;
    });
  }

  return (
    <section className="panel model-demand-panel">
      <div className="panel-heading">
        <div>
          <div className="eyebrow">MODEL DEMAND</div>
          <h2>Follow the demand, model by model.</h2>
        </div>
        <TrendingUp size={22} className="muted" />
      </div>
      <p className="footnote demand-intro">
        Compare network load over time. Select up to six models below, or click
        a model’s name to focus on it.
      </p>
      <div className="chart-toolbar">
        <Choice
          value={metric}
          onChange={(value) => setMetric(value as Metric)}
          options={metrics}
          label="Model demand chart metric"
        />
        <RangePicker
          value={range}
          onChange={setRange}
          label="Model demand date range"
        />
      </div>
      {error && (
        <output className="notice">
          {error}{' '}
          {data ? 'Showing the last loaded history for this range.' : ''}{' '}
          {paused
            ? 'Resume the live view to retry.'
            : 'Retrying automatically.'}
        </output>
      )}
      <div className="demand-highlights">
        <div>
          <span>Busiest {fresh ? 'now' : 'at last snapshot'}</span>
          <strong>
            {busiest && busiest.active_requests + busiest.queued_requests > 0
              ? name(busiest.id)
              : current.length
                ? 'No active demand'
                : 'Waiting for data'}
          </strong>
          <small>
            {busiest
              ? `${num(busiest.active_requests)} active · ${num(busiest.queued_requests)} queued`
              : 'Capacity snapshots every ~30 seconds'}
          </small>
        </div>
        <div>
          <span>Most load in this range</span>
          <strong>
            {leader && leader.averageLoad > 0
              ? name(leader.id)
              : leader
                ? 'No sampled demand'
                : 'Waiting for history'}
          </strong>
          <small>
            {leader
              ? `${num(leader.averageLoad, 1)} average concurrent requests · ${num(leader.sharePercent, 1)}% of sampled load`
              : 'Saved locally while the app is running'}
          </small>
        </div>
        <div>
          <span>Largest increase in this range</span>
          <strong>
            {rising
              ? name(rising.id)
              : data
                ? 'No measured increase'
                : 'Waiting for history'}
          </strong>
          <small>
            {rising
              ? `${trend(rising)} · second half vs first half`
              : 'Requires at least 80% coverage in both halves'}
          </small>
        </div>
      </div>
      <div
        className="demand-legend"
        aria-label="Models on the comparison chart"
      >
        {chosen.map((id) => (
          <Button
            key={id}
            variant="outline"
            size="sm"
            className="demand-chip"
            aria-label={`Remove ${id} from comparison`}
            onClick={() => toggle(id)}
            title={id}
          >
            <span
              className="demand-dot"
              style={{ background: modelColor(id) }}
            />
            <span>{name(id)}</span>
            <Check size={13} />
          </Button>
        ))}
        <Button
          variant="outline"
          size="sm"
          onClick={() =>
            setSelected((data?.models ?? []).slice(0, 3).map((m) => m.id))
          }
          disabled={!data?.models.length}
        >
          Top 3 in range
        </Button>
        <span className="small muted">{chosen.length}/6 selected</span>
      </div>
      {chosen.length ? (
        <TimeChart
          key={`${queryKey}:${metric}:${chosen.join('|')}`}
          data={chart}
          series={series}
          height={280}
          unit={metric === 'share' ? '%' : ''}
        />
      ) : (
        <div className="empty">
          Select models below to compare their demand.
        </div>
      )}
      <p className="footnote demand-chart-note">
        {metric === 'pressure'
          ? 'Active + queued requests divided by warm providers. No warm providers means this ratio is unavailable. '
          : metric === 'share'
            ? 'Each model’s share of all sampled network load in a chart bucket. Zero total demand has no percentage. '
            : metric === 'warm'
              ? 'Warm providers are available supply for each model. '
              : 'Requests are concurrent active or waiting work, not arrivals or completed jobs. '}
        {data
          ? `${num(data.bucketSeconds)}-second averages of observed readings; gaps have no samples. `
          : 'Loading saved history… '}
        {data?.coverageStart != null
          ? `Model history begins ${date(data.coverageStart)}. `
          : ''}
        {paused
          ? 'View paused.'
          : `Capacity ${fresh ? 'updated' : 'last received'} ${age(capacity?.updatedAt, now)}.`}
      </p>
      <div className="chart-toolbar demand-table-toolbar">
        <Input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          type="search"
          aria-label="Search models"
          placeholder="Find a model…"
          className="demand-search"
        />
        <Choice
          value={sort}
          onChange={setSort}
          label="Sort model demand"
          options={[
            { value: 'average', label: 'Sort: average load' },
            { value: 'now', label: 'Sort: latest load' },
            { value: 'queued', label: 'Sort: average queue' },
            { value: 'trend', label: 'Sort: rising demand' },
            { value: 'pressure', label: 'Sort: load / warm provider' },
          ]}
        />
      </div>
      <div className="network-table">
        <Table className="model-demand-table">
          <TableHeader>
            <TableRow>
              <TableHead>
                <span className="sr-only">Compare</span>
              </TableHead>
              <TableHead>Model</TableHead>
              <TableHead className="numeric">
                {fresh ? 'Now' : 'Last snapshot'}
              </TableHead>
              <TableHead className="numeric">Average load</TableHead>
              <TableHead className="numeric">Share of load</TableHead>
              <TableHead className="numeric">Trend</TableHead>
              <TableHead className="numeric">Load / warm</TableHead>
              <TableHead className="numeric">Warm / routable</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {visible.map(({ id, live, saved }) => {
              const colorIndex = chosen.indexOf(id);
              return (
                <TableRow key={id} data-selected={colorIndex >= 0}>
                  <TableCell>
                    <Checkbox
                      checked={colorIndex >= 0}
                      onCheckedChange={() => toggle(id)}
                      disabled={colorIndex < 0 && chosen.length >= 6}
                      aria-label={`Compare ${id}`}
                    />
                  </TableCell>
                  <TableCell className="demand-model-cell">
                    <button
                      className="demand-focus"
                      title={`View only ${id}`}
                      onClick={() => setSelected([id])}
                    >
                      {colorIndex >= 0 && (
                        <span
                          className="demand-dot"
                          style={{ background: modelColor(id) }}
                        />
                      )}
                      {name(id)}
                    </button>
                    <small>
                      {saved
                        ? `${num(saved.coveragePercent)}% range coverage · ${num(saved.samples)} readings`
                        : 'No readings in this range'}
                    </small>
                    <small className={live?.can_accept ? 'connected' : 'muted'}>
                      {!live
                        ? 'Absent from latest snapshot'
                        : `${live.can_accept ? 'Accepting work' : live.ready ? 'At capacity' : 'Not ready'}${!fresh ? ' · last snapshot' : ''}`}
                    </small>
                  </TableCell>
                  <TableCell className="numeric">
                    <strong>
                      {live
                        ? num(live.active_requests + live.queued_requests)
                        : '—'}
                    </strong>
                    <small>
                      {live
                        ? `${num(live.active_requests)} active / ${num(live.queued_requests)} queued`
                        : 'No current reading'}
                    </small>
                  </TableCell>
                  <TableCell className="numeric">
                    <strong>{num(saved?.averageLoad, 1)}</strong>
                    <small>
                      {num(saved?.peakLoad)} peak ·{' '}
                      {num(saved?.averageQueued, 1)} queued avg.
                    </small>
                  </TableCell>
                  <TableCell className="numeric">
                    <strong>
                      {saved?.sharePercent != null
                        ? `${num(saved.sharePercent, 1)}%`
                        : '—'}
                    </strong>
                    <div className="demand-share-track">
                      <span
                        style={{
                          width: `${saved?.sharePercent ?? 0}%`,
                          background:
                            colorIndex >= 0
                              ? modelColor(id)
                              : 'var(--c-657d99)',
                        }}
                      />
                    </div>
                  </TableCell>
                  <TableCell className="numeric">
                    <strong
                      className={`demand-trend-${saved?.trendState ?? 'insufficient'}`}
                    >
                      {trend(saved)}
                    </strong>
                    {saved && saved.trendState !== 'insufficient' && (
                      <small>
                        {num(saved.firstHalfAverage, 1)} →{' '}
                        {num(saved.lastHalfAverage, 1)} avg.
                      </small>
                    )}
                  </TableCell>
                  <TableCell className="numeric">
                    <strong>{num(saved?.averagePressure, 2)}</strong>
                    <small>
                      {saved?.noWarmSamples
                        ? `${num(saved.noWarmSamples)} readings without warm supply`
                        : 'Range average'}
                    </small>
                  </TableCell>
                  <TableCell className="numeric">
                    <strong>
                      {live
                        ? `${num(live.warm_providers)} / ${num(live.routable_providers)}`
                        : '—'}
                    </strong>
                    <small>
                      {live
                        ? `${num(live.aggregate_tps)} tok/s capacity`
                        : 'No current reading'}
                    </small>
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
        {!visible.length && (
          <div className="empty">
            {query
              ? 'No models match this search.'
              : 'Waiting for model demand snapshots.'}
          </div>
        )}
      </div>
      <p className="footnote">
        {data && data.comparison.end > data.comparison.start
          ? `Trend compares ${date(data.comparison.midpoint)}–${date(data.comparison.end)} with ${date(data.comparison.start)}–${date(data.comparison.midpoint)}. `
          : ''}
        Each half needs at least five minutes and 80% sampling coverage. Share
        uses all models’ recorded load; missing data is excluded. Capacity is
        advertised supply. Network demand does not guarantee traffic or earnings
        on your Mac.
      </p>
    </section>
  );
});
