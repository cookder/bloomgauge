'use client';
import { memo, useEffect, useMemo, useRef, useState } from 'react';
import { startChartPolling } from '@/lib/chart-polling';
import { usePageVisible } from '@/lib/use-page-visibility';
import { currencyAxisPrecision } from '@/lib/chart-precision';
import {
  creditSortOptions,
  creditPreferencesKey,
  readCreditPreferences,
  validCreditHistory,
  type CreditHistory,
} from '@/lib/credit-history';
import { ForecastSummary, type Forecast } from './forecast';
import { TimeBucketBar } from './time-bucket-bar';
import { useScreenActive } from './app-navigation';
import {
  cumulativeEarnings,
  projectedCumulative,
} from '@/lib/cumulative-earnings';
import {
  cumulativeForModels,
  ALL_EARNINGS,
  earningsMetrics,
  earningsShares,
  filterEarningsHours,
  filterModelProjection,
  projectedEarningsHour,
  modelColor,
  modelEarnings,
  UNATTRIBUTED,
} from '@/lib/model-earnings';
import { withHourProjection } from '@/lib/hourly-earnings';
import {
  Cpu,
  Fan,
  Thermometer,
  ChevronDown,
  Gauge,
  CircleDollarSign,
  Check,
} from 'lucide-react';
import {
  Bar,
  Area,
  Line,
  ComposedChart,
  CartesianGrid,
  ResponsiveContainer,
  ReferenceArea,
  ReferenceDot,
  ReferenceLine,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import {
  Pagination,
  PaginationContent,
  PaginationItem,
  PaginationNext,
  PaginationPrevious,
} from '@/components/ui/pagination';
import {
  bounds,
  Choice,
  fahrenheit,
  HistoryNote,
  money,
  num,
  RangePicker,
  shortModel,
  thermalBand,
  TimeChart,
  useHistory,
  type Range,
  plural,
} from './shared';
export type Hardware = {
  chip: string;
  cpuPercent: number | null;
  gpuPercent: number | null;
  memoryUsedGB: number | null;
  memoryTotalGB: number | null;
  compressedGB: number | null;
  swapGB: number | null;
  cpuTemp: number | null;
  gpuTemp: number | null;
  fanRPM: (number | null)[];
  thermal: string;
  processes: {
    pid: number;
    name: string;
    cpu: number;
    gpu?: number | null;
    memoryGB: number;
  }[];
};
export type Monitor = {
  revision?: string;
  hoursRevision?: string;
  liveCredits?: {
    status: string;
    added: number;
    detail: string;
    updatedAt?: number;
  };
  status: string;
  updatedAt: number | null;
  observedAt?: number;
  coverageStartedAt: number | null;
  gaps: number;
  coverageIntervals?: { start: number; end: number }[];
  hours: {
    at: number;
    usd: number;
    jobs: number;
    categories: Record<string, number>;
    categoryJobs?: Record<string, number>;
  }[];
};
function Meter({
  label,
  value,
  color,
  detail,
}: {
  label: string;
  value: number | null;
  color: string;
  detail: string;
}) {
  return (
    <div className="meter">
      <div>
        <span>{label}</span>
        <strong>
          {num(value, 1)}
          <small>%</small>
        </strong>
      </div>
      <div className="meter-track">
        <i
          style={{
            width: `${Math.min(100, Math.max(0, value ?? 0))}%`,
            background: color,
          }}
        />
      </div>
      <p>{detail}</p>
    </div>
  );
}
const temperatureSeries = [
  { key: 'cpuTempF', label: 'CPU °F', color: 'var(--c-a995ff)' },
  { key: 'gpuTempF', label: 'GPU °F', color: 'var(--c-f3c57e)' },
];
const loadSeries = [
  { key: 'cpuPercent', label: 'CPU', color: 'var(--c-a995ff)' },
  { key: 'gpuPercent', label: 'GPU', color: 'var(--c-87b9ff)' },
];
const memorySeries = [
  { key: 'memoryUsedGB', label: 'Memory', color: 'var(--c-82efb5)' },
];
function HardwareHistory({ paused }: { paused: boolean }) {
  const [range, setRange] = useState<Range>({ preset: '5m' }),
    [metric, setMetric] = useState('temperature');
  const { data, error } = useHistory(range, paused);
  return (
    <div className="hardware-history">
      <div className="chart-toolbar">
        <Choice
          label="Hardware chart"
          value={metric}
          onChange={setMetric}
          options={[
            { value: 'temperature', label: 'Temperature °F' },
            { value: 'load', label: 'CPU & GPU %' },
            { value: 'memory', label: 'Memory GB' },
          ]}
        />
        <RangePicker
          value={range}
          onChange={setRange}
          label="Hardware history range"
        />
      </div>
      <div className="chart-legend">
        {(metric === 'temperature'
          ? temperatureSeries
          : metric === 'load'
            ? loadSeries
            : memorySeries
        ).map((s) => (
          <span key={s.key}>
            <i style={{ background: s.color }} />
            {s.label}
          </span>
        ))}
      </div>
      <TimeChart
        key={`${JSON.stringify(range)}:${metric}`}
        data={data?.samples ?? []}
        series={
          metric === 'temperature'
            ? temperatureSeries
            : metric === 'load'
              ? loadSeries
              : memorySeries
        }
        unit={
          metric === 'temperature' ? ' °F' : metric === 'load' ? '%' : ' GB'
        }
        height={230}
      />
      <HistoryNote data={data} error={error} />
    </div>
  );
}
export function HardwareCard({
  h,
  paused,
}: {
  h: Hardware | undefined;
  paused: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const ram = h?.memoryTotalGB,
    mem = h?.memoryUsedGB;
  const cpu = fahrenheit(h?.cpuTemp),
    gpu = fahrenheit(h?.gpuTemp);
  return (
    <section
      className={`panel hardware-panel ${h?.thermal === 'Serious' || h?.thermal === 'Critical' ? 'thermal-alert' : ''}`}
    >
      <div className="panel-heading">
        <div>
          <div className="eyebrow">HARDWARE</div>
          <h2>Load & temperatures.</h2>
        </div>
        <Cpu size={21} className="muted" />
      </div>
      <Meter
        label="CPU"
        value={h?.cpuPercent ?? null}
        color="var(--c-a995ff)"
        detail="Whole machine · all cores"
      />
      <Meter
        label="GPU"
        value={h?.gpuPercent ?? null}
        color="var(--c-87b9ff)"
        detail="Apple GPU device utilization"
      />
      <Meter
        label="Unified memory"
        value={mem != null && ram ? (mem / ram) * 100 : null}
        color="var(--c-f3c57e)"
        detail={`${num(mem, 1)} of ${num(ram)} GB · ${num(h?.compressedGB, 1)} GB compressed`}
      />
      <div className="temperature-tiles">
        {[
          { label: 'CPU', value: cpu },
          { label: 'GPU', value: gpu },
        ].map((t) => {
          const band = thermalBand(t.value);
          return (
            <div
              key={t.label}
              style={{
                color: band.color,
                borderColor: `color-mix(in srgb, ${band.color} 25%, transparent)`,
                background: `color-mix(in srgb, ${band.color} 4%, transparent)`,
              }}
            >
              <span>
                <Thermometer size={15} />
                {t.label}
              </span>
              <strong>
                {num(t.value, 1)}
                <small>°F</small>
              </strong>
              <span>{band.label}</span>
            </div>
          );
        })}
      </div>
      <div className="hardware-footer">
        <span>
          <Fan size={16} />
          {h?.fanRPM?.map((n) => num(n)).join(' / ') ?? '—'} RPM
        </span>
        <span
          className={
            h?.thermal === 'Serious' || h?.thermal === 'Critical'
              ? 'danger-text'
              : ''
          }
        >
          Thermal pressure: {h?.thermal ?? 'Unknown'}
        </span>
      </div>
      <div className="temperature-bands">
        <span style={{ color: 'var(--c-87b9ff)' }}>Cool &lt;104°</span>
        <span style={{ color: 'var(--c-82efb5)' }}>Normal 104–175°</span>
        <span style={{ color: 'var(--c-f3c57e)' }}>Warm 176–193°</span>
        <span style={{ color: 'var(--c-ff8d88)' }}>Hot ≥194°</span>
      </div>
      <p className="footnote">
        Display bands, not Apple safety limits. Cooler chips are usually fine.
        macOS thermal pressure indicates heat stress. Swap {num(h?.swapGB, 1)}{' '}
        GB.
      </p>
      <Collapsible open={expanded} onOpenChange={setExpanded}>
        <CollapsibleTrigger className="expand-button">
          <span>{expanded ? 'Hide' : 'Show'} live hardware history</span>
          <ChevronDown
            size={16}
            style={{ transform: expanded ? 'rotate(180deg)' : 'none' }}
          />
        </CollapsibleTrigger>
        <CollapsibleContent>
          {expanded && <HardwareHistory paused={paused} />}
        </CollapsibleContent>
      </Collapsible>
    </section>
  );
}
export function ProcessPanel({ h }: { h: Hardware | undefined }) {
  const [sort, setSort] = useState('gpu');
  const processes = useMemo(
    () =>
      [...(h?.processes ?? [])].sort((a, b) =>
        sort === 'gpu'
          ? (b.gpu ?? -1) - (a.gpu ?? -1)
          : sort === 'cpu'
            ? b.cpu - a.cpu
            : b.memoryGB - a.memoryGB,
      ),
    [h?.processes, sort],
  );
  return (
    <section className="panel">
      <div className="panel-heading">
        <div>
          <div className="eyebrow">PROCESSES</div>
          <h2>What’s using your Mac.</h2>
        </div>
        <Gauge size={20} className="muted" />
      </div>
      <div className="chart-toolbar">
        <span className="muted small">Top processes</span>
        <Choice
          value={sort}
          onChange={setSort}
          label="Sort processes"
          options={[
            { value: 'gpu', label: 'GPU first' },
            { value: 'cpu', label: 'CPU first' },
            { value: 'memory', label: 'Memory first' },
          ]}
        />
      </div>
      <div className="process-scroll">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Process</TableHead>
              <TableHead className="numeric">CPU</TableHead>
              <TableHead className="numeric">GPU</TableHead>
              <TableHead className="numeric">Memory</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {processes.slice(0, 20).map((p) => (
              <TableRow key={p.pid}>
                <TableCell>
                  <span className="process-name">{p.name}</span>
                  <span className="pid">{p.pid}</span>
                </TableCell>
                <TableCell className="numeric">{num(p.cpu, 1)}%</TableCell>
                <TableCell className="numeric gpu-cell">
                  {p.gpu == null ? '—' : `${num(p.gpu, 1)}%`}
                </TableCell>
                <TableCell className="numeric">
                  {num(p.memoryGB, 2)} GB
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
      <p className="footnote">
        GPU is measured per process, refreshed each second. “—” means macOS
        exposes no GPU counter. CPU cores and GPU queues can overlap, so ratios
        may exceed 100%.
      </p>
    </section>
  );
}
export const EarningsPanel = memo(function EarningsPanel({
  m,
  at,
  forecast,
}: {
  m: Monitor | undefined;
  at: number;
  forecast?: Forecast;
}) {
  const screenActive = useScreenActive();
  const [range, setRange] = useState<Range>({ preset: '24h' }),
    [metric, setMetric] = useState('usd'),
    [horizon, setHorizon] = useState('hour'),
    [breakdown, setBreakdown] = useState('model'),
    [model, setModel] = useState(ALL_EARNINGS);
  const filtered = model !== ALL_EARNINGS;
  const canBreakDown =
    !filtered && (metric === 'usd' || metric === 'cumulative');
  const byModel = canBreakDown && breakdown === 'model';
  const modelLabel = (name: string) =>
    name === UNATTRIBUTED ? 'Unattributed / adjustments' : shortModel(name);
  const b = bounds(range, at);
  const modelOptions = useMemo(() => {
    const names = new Set(
      m?.hours.flatMap((h) => Object.keys(h.categories)) ?? [],
    );
    for (const name of forecast?.modelProjection?.models ?? []) names.add(name);
    if (
      m?.hours.some(
        (h) =>
          Math.round(h.usd * 1e6) !==
          Object.values(h.categories).reduce(
            (sum, usd) => sum + Math.round(usd * 1e6),
            0,
          ),
      )
    )
      names.add(UNATTRIBUTED);
    if (filtered) names.add(model);
    return [...names].sort();
  }, [m, forecast?.modelProjection?.models, filtered, model]);
  const selectedName = filtered ? modelLabel(model) : 'All earnings';
  const selectedColor = filtered ? modelColor(model) : 'var(--c-82efb5)';
  const hours = useMemo(
    () =>
      earningsMetrics(
        filterEarningsHours(
          m?.hours.filter((h) => h.at + 3600 > b.start && h.at < b.end) ?? [],
          model,
        ),
        m?.observedAt ?? m?.updatedAt ?? at,
        m?.coverageStartedAt,
        m?.coverageIntervals,
        (m?.gaps ?? 0) > (m?.coverageIntervals?.length ?? 0),
      ),
    [m, b.start, b.end, at, model],
  );
  const total = hours.reduce(
    (a, h) => ({
      usd: a.usd + h.usd,
      jobs: a.jobs == null || h.jobs == null ? null : a.jobs + h.jobs,
      seconds: a.seconds + h.seconds,
      work: a.work + h.usd - (h.categories.base_reward ?? 0),
      rateComplete: a.rateComplete && h.rateComplete,
    }),
    {
      usd: 0,
      jobs: 0 as number | null,
      seconds: 0,
      work: 0,
      rateComplete: true,
    },
  );
  const rateSeconds = total.rateComplete ? total.seconds : 0;
  const hourlyRateChart = hours.flatMap((hour, index) => {
    const previous = hours[index - 1];
    const point = { at: hour.at, perHour: hour.perHour };
    // Do not bridge missing hourly records or use forecast-only rows as zero earnings.
    return previous && hour.at > previous.at + 3600
      ? [{ at: previous.at + 3600, perHour: null }, point]
      : [point];
  });
  const cumulative = useMemo(
    () =>
      cumulativeEarnings(
        hours,
        m?.observedAt ?? m?.updatedAt ?? at,
        m?.coverageStartedAt,
      ),
    [hours, m?.observedAt, m?.updatedAt, m?.coverageStartedAt, at],
  );
  const modelProjection = filterModelProjection(
    forecast?.modelProjection,
    model,
  );
  const perModel = useMemo(
    () =>
      modelEarnings(
        hours,
        m?.observedAt ?? m?.updatedAt ?? at,
        m?.coverageStartedAt,
      ),
    [hours, m?.observedAt, m?.updatedAt, m?.coverageStartedAt, at],
  );
  const projectionModels = modelProjection?.models ?? [];
  const projectionName = filtered
    ? selectedName
    : byModel
      ? projectionModels.map(shortModel).join(' + ') || 'current models'
      : 'combined total';
  const projectionColor = filtered
    ? selectedColor
    : byModel
      ? projectionModels.length === 1
        ? modelColor(projectionModels[0])
        : 'var(--c-dab7ff)'
      : 'var(--c-82efb5)';
  const projectionBaseline = useMemo(
    () =>
      byModel && modelProjection?.models.length
        ? cumulativeForModels(perModel, modelProjection.models, cumulative)
        : cumulative,
    [byModel, perModel, modelProjection?.models, cumulative],
  );
  // The stacked bars show all contributions. A matching solid serving-model
  // subtotal still anchors the dotted forecast exactly, including joint rates.
  const needsProjectionBaseline = byModel;
  const liveRange = b.start < at && (!range.end || range.end >= at);
  const projectionEnd =
    range.end ??
    (horizon === 'hour'
      ? (forecast?.hourEnd ?? at)
      : at + Number(horizon) * 3600);
  const cumulativeWithProjection = useMemo(
    () =>
      projectedCumulative(
        projectionBaseline,
        liveRange ? modelProjection : undefined,
        projectionEnd,
      ),
    [projectionBaseline, liveRange, modelProjection, projectionEnd],
  );
  const projectedEnd = cumulativeWithProjection.at(-1);
  const hasProjection = projectedEnd?.predicted != null;
  const projectionStart = cumulativeWithProjection.find(
    (point) => point.predicted != null,
  );
  const series = [...perModel.series].sort((a, b) => b.usd - a.usd);
  const periodShares = earningsShares(series.map((s) => s.usd));
  // Put the serving model(s) at the bottom so their solid forecast anchor also
  // follows the top of their own stack, never other models or base rewards.
  const barSeries =
    metric === 'cumulative' && hasProjection
      ? [...series].sort(
          (a, b) =>
            Number(projectionModels.includes(b.model)) -
            Number(projectionModels.includes(a.model)),
        )
      : series;
  const emptyModels = Object.fromEntries(series.map((s) => [s.key, null]));
  const cumulativeChart = cumulativeWithProjection.map((point, i) => ({
    ...point,
    byModel: perModel.cumulative[i]?.byModel ?? emptyModels,
  }));
  const labels: Record<string, string> = {
    usd: 'Tracked earnings',
    cumulative: 'Cumulative tracked earnings',
    perMinute: 'USD per tracked minute',
    perHour: 'Earnings per hour',
    perJob: 'Average inference earnings / job',
  };
  const showForecast =
    forecast &&
    forecast.hourEnd > b.start &&
    forecast.hourStart < b.end &&
    (!range.end || range.end >= forecast.at);
  // Hourly all-model forecasts include base rewards. A filtered hour instead
  // uses only the exactly matching serving-model projection and confirmed USD.
  let earningsForecast = forecast;
  if (filtered && forecast) {
    const actual = hours.find((h) => h.at === forecast.hourStart)?.usd ?? null;
    const projection = projectedEarningsHour(hours, modelProjection, {
      start: forecast.hourStart,
      end: forecast.hourEnd,
      at,
      observedAt: m?.observedAt ?? m?.updatedAt ?? at,
      sourceReady: forecast.earnings.status === 'ready',
    });
    earningsForecast = {
      ...forecast,
      earnings: {
        status: projection != null ? 'ready' : 'unavailable',
        actual,
        projected: projection ?? null,
        additional:
          projection != null && actual != null ? projection - actual : null,
        jobsProjected: null,
        detail: !modelProjection
          ? model === 'base_reward' || model === UNATTRIBUTED
            ? 'This category shows confirmed earnings only.'
            : 'A forecast is available only when this model is running on its own; joint-model estimates are not split.'
          : projection == null
            ? forecast.earnings.status !== 'ready'
              ? forecast.earnings.detail
              : modelProjection.status !== 'ready'
                ? modelProjection.detail
                : 'Waiting for a fresh, recorded current hour to anchor this model’s forecast.'
            : modelProjection.detail,
      },
    };
  }
  const chartHours = withHourProjection(
    hours,
    earningsForecast,
    !!showForecast,
  );
  const hourlyChart = chartHours.map((h) => ({
    ...h,
    byModel:
      perModel.hourly.find((point) => point.at === h.at)?.byModel ??
      emptyModels,
  }));
  const plottedRows =
    metric === 'cumulative'
      ? cumulativeChart
      : metric === 'perHour'
        ? hourlyRateChart
        : hourlyChart;
  // Bars retain real elapsed time when hours are missing. Half-hour padding
  // keeps the first/last bucket fully visible without inventing zero samples.
  const hourlyBars = metric !== 'cumulative' && metric !== 'perHour';
  const firstAt = plottedRows[0]?.at ?? at;
  const lastAt = plottedRows.at(-1)?.at ?? at;
  const edgePadding = hourlyBars || firstAt === lastAt ? 1800 : 0;
  const chartDomain: [number, number] = [
    firstAt - edgePadding,
    lastAt + edgePadding,
  ];
  const chartSpan = chartDomain[1] - chartDomain[0];
  const axisMaximum = plottedRows.reduce((maximum, row) => {
    const point = row as unknown as Record<string, unknown>;
    const amounts = byModel
      ? Object.values((point.byModel ?? {}) as Record<string, unknown>)
      : [point[metric]];
    if (metric === 'usd') amounts.push(point.projected);
    const finite = amounts.filter(
      (value): value is number =>
        typeof value === 'number' && Number.isFinite(value),
    );
    const positive = finite.reduce((sum, value) => sum + Math.max(0, value), 0);
    const negative = finite.reduce((sum, value) => sum + Math.min(0, value), 0);
    const predicted =
      typeof point.predicted === 'number' && Number.isFinite(point.predicted)
        ? Math.abs(point.predicted)
        : 0;
    return Math.max(maximum, positive, Math.abs(negative), predicted);
  }, 0);
  const axisPrecision = currencyAxisPrecision(axisMaximum);
  return (
    <section className="panel earnings-panel">
      <div className="panel-heading">
        <div>
          <div className="eyebrow">EARNINGS</div>
          <h2>
            {metric === 'cumulative'
              ? 'Cumulative earnings.'
              : metric === 'perHour'
                ? 'Earnings per hour.'
                : 'Earnings over time.'}
          </h2>
        </div>
      </div>
      <div className="chart-toolbar">
        <Choice
          label="Earnings chart metric"
          value={metric}
          onChange={setMetric}
          options={[
            { value: 'usd', label: 'Earnings' },
            { value: 'cumulative', label: 'Cumulative earnings' },
            { value: 'perMinute', label: 'USD / minute' },
            { value: 'perHour', label: 'Earnings per hour' },
            { value: 'perJob', label: 'USD / job' },
          ]}
        />
        <Choice
          label="Filter earnings by model"
          value={model}
          onChange={setModel}
          options={[
            { value: ALL_EARNINGS, label: 'All models + rewards' },
            ...modelOptions.map((name) => ({
              value: name,
              label:
                name === UNATTRIBUTED || name === 'base_reward'
                  ? modelLabel(name)
                  : name,
            })),
          ]}
        />
        {canBreakDown && (
          <Choice
            label="Earnings model breakdown"
            value={breakdown}
            onChange={setBreakdown}
            options={[
              { value: 'model', label: 'By model' },
              { value: 'combined', label: 'Combined total' },
            ]}
          />
        )}
        <RangePicker value={range} onChange={setRange} label="Earnings range" />
        {metric === 'cumulative' && liveRange && !range.end && (
          <Choice
            label="Projection horizon"
            value={horizon}
            onChange={setHorizon}
            options={[
              { value: 'hour', label: 'Project to hour end' },
              { value: '1', label: 'Project next 1h' },
              { value: '6', label: 'Project next 6h' },
              { value: '24', label: 'Project next 24h' },
            ]}
          />
        )}
      </div>
      <div className="chart-summary">
        <strong>
          {metric === 'perHour'
            ? `${money(rateSeconds ? total.usd / (rateSeconds / 3600) : null)} / hr`
            : metric === 'perMinute'
              ? `${money(rateSeconds ? total.usd / (rateSeconds / 60) : null)} / min`
              : metric === 'perJob'
                ? `${money(total.jobs ? total.work / total.jobs : null)} / job`
                : m
                  ? money(total.usd)
                  : '—'}
        </strong>
        <span>
          {['perHour', 'perMinute', 'perJob'].includes(metric)
            ? 'Period average · '
            : ''}
          {selectedName} ·{' '}
          {total.jobs == null
            ? 'Job count unavailable'
            : `${num(total.jobs)} tracked jobs`}
        </span>
      </div>
      <div className="earnings-stat-strip">
        <div>
          <span>Avg. inference / job</span>
          <strong>{money(total.jobs ? total.work / total.jobs : null)}</strong>
        </div>
        <div>
          <span>Per tracked minute</span>
          <strong>
            {money(rateSeconds ? total.usd / (rateSeconds / 60) : null)}
          </strong>
        </div>
        <div>
          <span>Per tracked hour</span>
          <strong>
            {money(rateSeconds ? total.usd / (rateSeconds / 3600) : null)}
          </strong>
        </div>
      </div>
      <div className="earnings-chart">
        {!screenActive ? null : (metric !== 'perJob' ||
            hours.some((h) => h.perJob != null)) &&
          (metric === 'cumulative'
            ? cumulative.length
            : metric === 'perHour'
              ? hourlyRateChart.length
              : chartHours.length) ? (
          <ResponsiveContainer
            width="100%"
            height="100%"
            minWidth={0}
            initialDimension={{ width: 600, height: 206 }}
          >
            <ComposedChart<{ at: number }>
              key={`${JSON.stringify(range)}:${metric}:${model}:${breakdown}:${horizon}`}
              stackOffset="sign"
              data={plottedRows}
              margin={{
                top: metric === 'cumulative' && hasProjection ? 28 : 15,
                right: 16,
                left: -12,
                bottom: 0,
              }}
            >
              <CartesianGrid
                stroke="var(--c-27303c)"
                vertical={false}
                strokeDasharray="2 6"
              />
              <XAxis
                dataKey="at"
                type="number"
                domain={chartDomain}
                allowDataOverflow
                tickFormatter={(v) =>
                  b.end - b.start < 86401
                    ? new Date(v * 1000).toLocaleTimeString([], {
                        hour: '2-digit',
                        minute: '2-digit',
                      })
                    : new Date(v * 1000).toLocaleDateString([], {
                        month: 'short',
                        day: 'numeric',
                      })
                }
                minTickGap={70}
                tick={{ fill: 'var(--c-8997aa)', fontSize: 12 }}
                axisLine={false}
                tickLine={false}
              />
              {metric === 'cumulative' && hasProjection && projectionStart && (
                <>
                  <ReferenceArea
                    x1={projectionStart.at}
                    x2={projectedEnd.at}
                    fill={projectionColor}
                    fillOpacity={0.07}
                    strokeOpacity={0}
                  />
                  <ReferenceLine
                    x={projectionStart.at}
                    stroke="var(--c-b4a5c8)"
                    strokeDasharray="3 5"
                    label={{
                      value: 'Now',
                      position: 'insideTopLeft',
                      fill: 'var(--c-d8cce7)',
                      fontSize: 13,
                    }}
                  />
                </>
              )}
              <YAxis
                tickFormatter={(v) => money(v, axisPrecision)}
                width={Math.max(
                  65,
                  money(axisMaximum, axisPrecision).length * 7 + 10,
                )}
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
                formatter={(v, name, item) => {
                  const key = String(item.dataKey ?? '');
                  const amounts = series.map(
                    (s) => item.payload?.byModel?.[s.key] ?? 0,
                  );
                  const shares = byModel ? earningsShares(amounts) : null;
                  const index = series.findIndex(
                    (s) => key === `byModel.${s.key}`,
                  );
                  const share = shares && index >= 0 ? shares[index] : null;
                  return [
                    `${money(Number(v))}${metric === 'perHour' ? ' / hr' : ''}${share != null ? ` · ${num(share, 1)}% of ${metric === 'cumulative' ? 'total so far' : 'hour total'}` : ''}`,
                    name === 'projected'
                      ? 'Estimated remaining earnings'
                      : name === 'predicted'
                        ? `Projected ${projectionName}`
                        : name === 'cumulative'
                          ? `Confirmed ${projectionName}`
                          : byModel
                            ? name
                            : `${filtered ? selectedName + ' · ' : ''}${labels[metric]}`,
                  ];
                }}
              />
              {byModel ? (
                barSeries.map((s) => (
                  <Bar
                    key={s.model}
                    dataKey={`byModel.${s.key}`}
                    name={modelLabel(s.model)}
                    stackId="earnings"
                    fill={s.color}
                    maxBarSize={24}
                    shape={<TimeBucketBar spanSeconds={chartSpan} />}
                    activeBar={false}
                    isAnimationActive={false}
                  />
                ))
              ) : metric === 'cumulative' ? (
                <Area
                  type="stepAfter"
                  dataKey="cumulative"
                  stroke={selectedColor}
                  strokeWidth={2}
                  fill={selectedColor}
                  fillOpacity={0.12}
                  connectNulls={false}
                  dot={false}
                  isAnimationActive={false}
                />
              ) : metric === 'perHour' ? (
                <Line
                  type="linear"
                  dataKey="perHour"
                  stroke={selectedColor}
                  strokeWidth={2}
                  connectNulls={false}
                  dot={{ r: 2, strokeWidth: 0, fill: selectedColor }}
                  activeDot={{ r: 5 }}
                  isAnimationActive={false}
                />
              ) : (
                <Bar
                  dataKey={metric}
                  stackId="earnings"
                  fill={selectedColor}
                  radius={[3, 3, 0, 0]}
                  maxBarSize={24}
                  shape={<TimeBucketBar spanSeconds={chartSpan} />}
                  isAnimationActive={false}
                />
              )}
              {metric === 'cumulative' &&
                needsProjectionBaseline &&
                hasProjection && (
                  <Line
                    type="stepAfter"
                    dataKey="cumulative"
                    stroke={projectionColor}
                    strokeWidth={2.5}
                    strokeOpacity={0.85}
                    connectNulls={false}
                    dot={false}
                    isAnimationActive={false}
                  />
                )}
              {metric === 'cumulative' && hasProjection && (
                <Line
                  type="linear"
                  dataKey="predicted"
                  stroke={projectionColor}
                  strokeWidth={3.5}
                  strokeDasharray="1 7"
                  strokeLinecap="round"
                  connectNulls={false}
                  dot={false}
                  activeDot={{
                    r: 6,
                    stroke: 'var(--c-10151c)',
                    strokeWidth: 2,
                  }}
                  isAnimationActive={false}
                />
              )}
              {metric === 'cumulative' && hasProjection && projectionStart && (
                <>
                  <ReferenceDot
                    x={projectionStart.at}
                    y={projectionStart.predicted!}
                    r={4.5}
                    fill={projectionColor}
                    stroke="var(--c-10151c)"
                    strokeWidth={2}
                  />
                  <ReferenceDot
                    x={projectedEnd.at}
                    y={projectedEnd.predicted!}
                    r={6}
                    fill="var(--c-171520)"
                    stroke={projectionColor}
                    strokeWidth={2.5}
                  />
                </>
              )}
              {metric === 'usd' && (
                <Bar
                  dataKey="projected"
                  stackId="earnings"
                  fill={selectedColor}
                  fillOpacity={0.22}
                  stroke={selectedColor}
                  strokeOpacity={0.45}
                  strokeDasharray="3 3"
                  radius={[3, 3, 0, 0]}
                  maxBarSize={24}
                  shape={<TimeBucketBar spanSeconds={chartSpan} />}
                  isAnimationActive={false}
                />
              )}
            </ComposedChart>
          </ResponsiveContainer>
        ) : (
          <div className="empty">
            {metric === 'perJob' && hours.length
              ? model === 'base_reward' || model === UNATTRIBUTED
                ? 'This category has no model-attributed inference job average.'
                : total.jobs == null
                  ? 'Per-model job counts are unavailable for these historical records.'
                  : 'No recorded inference jobs for this selection.'
              : 'No tracked earnings in this window.'}
          </div>
        )}
      </div>
      {byModel && (
        <p className="model-chart-caption">
          {metric === 'cumulative'
            ? 'Each stacked bar shows the total earned so far in this period; colors show each model’s contribution.'
            : 'Each stacked bar shows an hour’s earnings; colors show each model’s contribution.'}{' '}
          Hover or tap for amounts and shares. Base rewards stay separate.
          {metric === 'cumulative' && hasProjection
            ? ` The solid ${projectionName} line joins its dotted projection at the same confirmed amount.`
            : ''}
        </p>
      )}
      {metric === 'cumulative' && (
        <>
          <div className="projection-legend">
            <span>
              <i />
              {byModel ? 'Confirmed · by model' : 'Confirmed earnings'}
            </span>
            {needsProjectionBaseline && hasProjection && (
              <span>
                <i
                  className="confirmed-total"
                  style={{ background: projectionColor }}
                />
                Confirmed {projectionName}
              </span>
            )}
            {hasProjection && (
              <span style={{ color: projectionColor }}>
                <i
                  className="model-dotted"
                  style={{ borderColor: projectionColor }}
                />
                Projected {projectionName}
              </span>
            )}
          </div>
          <div className="forecast-summary earnings">
            <div className="forecast-title">
              <span className="forecast-dot" />
              Current-model projection
            </div>
            {hasProjection ? (
              <>
                <div className="forecast-values">
                  <div>
                    <span>Expected {projectionName}</span>
                    <strong>≈{money(projectedEnd.predicted)}</strong>
                  </div>
                  <div>
                    <span>By</span>
                    <strong>
                      {new Date(projectedEnd.at * 1000).toLocaleString([], {
                        month: 'short',
                        day: 'numeric',
                        hour: 'numeric',
                        minute: '2-digit',
                      })}
                    </strong>
                  </div>
                </div>
                <p className="forecast-explanation">
                  Estimated inference pace: ≈
                  {money(modelProjection?.ratePerHour)} / hour.{' '}
                  {modelProjection?.models.map(shortModel).join(' + ')} ·{' '}
                  {num(modelProjection?.hours, 1)} observed hours across{' '}
                  {plural(modelProjection?.days, 'day')}.{' '}
                  {modelProjection?.detail} Assumes these models stay online.
                  This is an estimate, not a guaranteed payout.
                  {range.end && range.end > at + 86400
                    ? ' Projection is limited to the next 24 hours.'
                    : ''}
                </p>
              </>
            ) : (
              <p className="forecast-unavailable">
                {!liveRange
                  ? 'Historical periods show confirmed earnings only. Choose a live period to project the currently running models.'
                  : filtered && !modelProjection
                    ? (earningsForecast?.earnings.detail ??
                      'No projection for this selection.')
                    : modelProjection?.status !== 'ready'
                      ? (modelProjection?.detail ??
                        'Waiting for model earnings history.')
                      : 'Waiting for a fresh recorded hour to anchor the projection.'}
              </p>
            )}
          </div>
          <p className="footnote cumulative-note">
            {filtered && (model === 'base_reward' || model === UNATTRIBUTED)
              ? 'This category shows confirmed earnings only.'
              : filtered
                ? 'The projection starts at this model’s confirmed earnings. Base rewards and other models are excluded.'
                : byModel
                  ? 'The projection starts at the serving models’ confirmed earnings; other models and base rewards stay separate.'
                  : 'The projection starts at the confirmed combined total, including base rewards.'}{' '}
            Future estimates add inference earnings only. Gaps mark missing
            hourly records.
          </p>
        </>
      )}
      {metric === 'usd' && (
        <div className="projection-legend">
          <span>
            <i />
            Earned
          </span>
          {showForecast && earningsForecast?.earnings.status === 'ready' && (
            <span>
              <i className="faint" />
              Estimated rest of this hour
            </span>
          )}
        </div>
      )}
      {m?.liveCredits && (
        <p className="footnote">
          {m.liveCredits.status === 'synced'
            ? 'Live confirmed credits are included between Monitor updates.'
            : m.liveCredits.status === 'partial'
              ? 'Known new credits are included; tracking coverage has not fully caught up.'
              : m.liveCredits.status === 'stale'
                ? 'Showing saved confirmed credits while earnings reconnect.'
                : m.liveCredits.detail}{' '}
          The same ledger updates every earnings view. Hourly source precision
          is unchanged.
        </p>
      )}
      {!total.rateComplete && (
        <p className="footnote">
          Rate unavailable for this period: a coverage gap affects its hourly
          earnings. Confirmed amounts remain included.
        </p>
      )}
      {(metric === 'perHour' || metric === 'perMinute') && (
        <p className="footnote">
          {filtered
            ? 'Selected earnings per tracked time, including periods when other models ran.'
            : 'Rates include base rewards.'}{' '}
          Partial hours use observed time; gaps mark missing records.
        </p>
      )}
      {showForecast && metric !== 'cumulative' && metric !== 'perHour' && (
        <ForecastSummary value={earningsForecast} kind="earnings" />
      )}
      {byModel && (
        <p className="model-period-caption">Share of the selected period</p>
      )}
      <div className={`earnings-breakdown${byModel ? ' with-shares' : ''}`}>
        {series.map((s, index) => (
          <div key={s.model}>
            <span>
              <i style={{ background: s.color }} />
              {modelLabel(s.model)}
            </span>
            <strong>{money(s.usd)}</strong>
            <small className="model-contribution">
              {periodShares
                ? `${num(periodShares[index], 1)}% of period earnings`
                : series.some((s) => s.usd < 0)
                  ? 'Share unavailable with negative adjustments'
                  : 'No earnings in this period'}
              {hasProjection && modelProjection?.models.includes(s.model)
                ? ' · Forecast model'
                : ''}
            </small>
            {byModel && periodShares && (
              <div className="model-share-track" aria-hidden="true">
                <div
                  style={{
                    width: `${periodShares[index]}%`,
                    background: s.color,
                  }}
                />
              </div>
            )}
          </div>
        ))}
      </div>
      <p className="footnote">
        Hourly source: includes the entire boundary hours. Rates use observed
        time within those hours; inference/job excludes base rewards. History
        begins{' '}
        {m?.coverageStartedAt
          ? new Date(m.coverageStartedAt * 1000).toLocaleString()
          : 'when available'}
        . {m?.gaps ? `${m.gaps} coverage gaps may reduce tracked totals.` : ''}
        {filtered
          ? ' Only this selection is included in the chart and summary. Older missing category job counts remain unavailable; unlabeled earnings stay in Unattributed / adjustments.'
          : ''}
        {m?.status === 'stale' ? ' History is stale.' : ''}
      </p>
    </section>
  );
});
type Credits = CreditHistory;
export const CreditsPanel = memo(function CreditsPanel({
  paused,
  revision = 0,
}: {
  paused: boolean;
  revision?: number;
}) {
  const [preferences, setPreferences] = useState(() => {
    try {
      return readCreditPreferences(localStorage.getItem(creditPreferencesKey));
    } catch {
      return readCreditPreferences(null);
    }
  });
  const { sort, category, limit, view, showSummary } = preferences;
  const [model, setModel] = useState('');
  const [page, setPage] = useState(1),
    [range, setRange] = useState<Range>({ preset: 'all' });
  useEffect(() => {
    try {
      localStorage.setItem(creditPreferencesKey, JSON.stringify(preferences));
    } catch {
      /* Optional view preferences. */
    }
  }, [preferences]);
  const [browseWindow, setBrowseWindow] = useState<{
    start: number;
    end: number;
  } | null>(null);
  const queryKey = JSON.stringify([
    page,
    limit,
    range,
    browseWindow,
    sort,
    category,
    model,
  ]);
  const [saved, setSaved] = useState<{
    key: string;
    data: Credits | null;
    error: string;
    window?: { start: number; end: number };
  } | null>(null);
  const data = saved?.key === queryKey ? saved.data : null;
  const error = saved?.key === queryKey ? saved.error : '';
  const active = useScreenActive();
  const pageVisible = usePageVisible();
  const revisionRef = useRef(revision);
  revisionRef.current = revision;
  useEffect(() => {
    if (!active || !pageVisible || (paused && data)) return;
    let latest: Credits | null = null;
    let loadedWindow: { start: number; end: number } | undefined;
    let loadedRevision: number | undefined;
    let refreshedAt = 0;
    return startChartPolling({
      load: async (signal) => {
        const requestedRevision = revisionRef.current;
        // Revisions can arrive every second. Do not abort an in-flight phone
        // request for each one: finish it, then immediately catch up as needed.
        if (
          latest &&
          loadedRevision === requestedRevision &&
          Date.now() - refreshedAt < 15000
        )
          return latest;
        const b = browseWindow ?? bounds(range);
        const response = await fetch(
          `/api/credits?from=${b.start}&to=${b.end}&page=${page}&limit=${limit}&sort=${encodeURIComponent(sort)}&category=${encodeURIComponent(category)}&model=${encodeURIComponent(model)}`,
          { signal, cache: 'no-store' },
        );
        if (!response.ok) throw Error('Credit history is unavailable.');
        const value = (await response.json()) as Credits;
        if (
          !validCreditHistory(value) ||
          value.page !== page ||
          value.limit !== Number(limit)
        )
          throw Error('Credit history returned an incomplete response.');
        latest = value;
        loadedWindow = b;
        loadedRevision = requestedRevision;
        refreshedAt = Date.now();
        return value;
      },
      onValue: (value) => {
        const lastPage = Math.max(1, Math.ceil(value.count / Number(limit)));
        if (page > lastPage) {
          setPage(lastPage);
          if (lastPage === 1) setBrowseWindow(null);
          return;
        }
        setSaved((previous) =>
          previous?.key === queryKey &&
          previous.data === value &&
          !previous.error
            ? previous
            : { key: queryKey, data: value, error: '', window: loadedWindow },
        );
      },
      onError: (error) =>
        setSaved((previous) => ({
          key: queryKey,
          data: previous?.key === queryKey ? previous.data : null,
          window: previous?.key === queryKey ? previous.window : undefined,
          error: error.message,
        })),
      intervalMs: 1000,
      repeat: !paused,
    });
    // The saved result freezes this query when paused. Revision updates are
    // read by the serial poller, never used to restart an active request.
  }, [queryKey, paused, active, pageVisible]);
  const pages = Math.max(1, Math.ceil((data?.count ?? 0) / Number(limit)));
  const previousDisabled = !data || page <= 1;
  const nextDisabled = !data || page >= pages;
  const leaderboard = (data?.leaderboard ?? []).filter(
    (row) => row.model !== 'base_reward',
  );
  function resetPage() {
    setPage(1);
    setBrowseWindow(null);
  }
  return (
    <section className="panel credits-panel">
      <div className="panel-heading">
        <div>
          <div className="eyebrow">RECENT CREDITS</div>
          <h2>Your credits, a little clearer.</h2>
        </div>
        <CircleDollarSign size={20} className="muted" />
      </div>
      <div className="chart-toolbar">
        <Choice
          label="Credit sort order"
          value={sort}
          options={creditSortOptions}
          onChange={(sort) => {
            resetPage();
            setPreferences((p) => ({ ...p, sort }));
          }}
        />
        <Choice
          label="Credit category"
          value={category}
          options={[
            { value: 'all', label: 'All credits' },
            { value: 'inference', label: 'Inference only' },
            { value: 'base_reward', label: 'Base rewards' },
          ]}
          onChange={(category) => {
            resetPage();
            setModel('');
            setPreferences((p) => ({ ...p, category }));
          }}
        />
        <Choice
          label="Credit model"
          value={model}
          options={[
            { value: '', label: 'All model labels' },
            ...[
              ...new Set([...(data?.models ?? []), ...(model ? [model] : [])]),
            ].map((value) => ({
              value,
              label: shortModel(value) || 'Unattributed',
            })),
          ]}
          onChange={(model) => {
            resetPage();
            setModel(model);
          }}
        />
        <Choice
          label="Credits per page"
          value={limit}
          onChange={(v) => {
            setPage(1);
            setBrowseWindow(null);
            setPreferences((p) => ({ ...p, limit: v }));
          }}
          options={['25', '100', '250'].map((v) => ({
            value: v,
            label: `${v} per page`,
          }))}
        />
        <RangePicker
          value={range}
          onChange={(r) => {
            setPage(1);
            setBrowseWindow(null);
            setRange(r);
          }}
          label="Credit history range"
        />
      </div>
      <p className="small muted">
        {num(data?.count)} saved credits ·{' '}
        {creditSortOptions.find((o) => o.value === sort)?.label.toLowerCase()} ·
        account-level history
      </p>
      <div className="credit-view-options">
        <div className="app-result-toggle" aria-label="Credit view">
          <button
            type="button"
            aria-pressed={view === 'list'}
            onClick={() => setPreferences((p) => ({ ...p, view: 'list' }))}
          >
            Credit list
          </button>
          <button
            type="button"
            aria-pressed={view === 'models'}
            onClick={() => setPreferences((p) => ({ ...p, view: 'models' }))}
          >
            Model leaderboard
          </button>
        </div>
        <label>
          <input
            type="checkbox"
            checked={showSummary}
            onChange={(e) =>
              setPreferences((p) => ({ ...p, showSummary: e.target.checked }))
            }
          />
          Show summary
        </label>
      </div>
      {showSummary && data && (
        <div
          className="credit-summary"
          aria-label="Credit statistics for selected range"
        >
          <div>
            <span>Inference credits</span>
            <strong>{money(data.summary.inferenceUsd)}</strong>
            <small>{num(data.count)} credits in this selection</small>
          </div>
          <div>
            <span>Base rewards</span>
            <strong>{money(data.summary.baseRewardUsd)}</strong>
            <small>Kept separate from model earnings</small>
          </div>
          <div>
            <span>Average credit</span>
            <strong>{money(data.summary.averageUsd)}</strong>
            <small>Across the full filtered range</small>
          </div>
          <div>
            <span>Largest credit</span>
            <strong>{money(data.summary.maxUsd)}</strong>
            <small>
              {num(data.summary.outputTokens)} reported output tokens in range
            </small>
          </div>
        </div>
      )}
      <p className="footnote">
        {browseWindow
          ? `Browsing credits through ${new Date(browseWindow.end * 1000).toLocaleString()}. Return to page 1 for the latest arrivals.`
          : paused
            ? 'View paused. Choose another page or range to load it once.'
            : range.end
              ? 'Selected dates · refreshing saved credits.'
              : 'Live · new arrivals update this page.'}
      </p>
      {error && (
        <p role="alert" className="notice">
          {error} {data ? 'Showing the last loaded credits for this page.' : ''}{' '}
          {paused
            ? 'Resume the live view to retry.'
            : 'Retrying automatically.'}
        </p>
      )}
      {view === 'models' && (
        <div className="credit-leaderboard" aria-label="Model leaderboard">
          <p className="footnote">
            Ranked by confirmed inference dollars in this selection. Account
            totals can include several Macs; this is not an earnings-per-hour
            comparison or a model recommendation. Base rewards remain separate.
          </p>
          {leaderboard.map((row, index) => (
            <div className="credit-rank-row" key={JSON.stringify(row.model)}>
              <span className="credit-rank">{index + 1}</span>
              <div>
                <strong>
                  {row.model ? shortModel(row.model) : 'Unattributed'}
                </strong>
                <small>
                  {num(row.count)} credits · {num(row.outputTokens)} reported
                  output tokens
                </small>
                <div className="credit-rank-track" aria-hidden="true">
                  <i
                    style={{
                      width: `${data && data.summary.inferenceUsd > 0 ? Math.min(100, Math.max(0, (row.usd / data.summary.inferenceUsd) * 100)) : 0}%`,
                      background: modelColor(row.model ?? 'unattributed'),
                    }}
                  />
                </div>
              </div>
              <strong>{money(row.usd)}</strong>
            </div>
          ))}
          {data && !leaderboard.length && (
            <p className="empty">No inference credits in this selection.</p>
          )}
        </div>
      )}
      <div
        className="credits credits-scroll"
        hidden={view !== 'list'}
        aria-busy={!data && !error}
      >
        {data?.entries.map((entry) => (
          <div className="credit-row" key={entry.id}>
            <span className="credit-icon">
              <Check size={15} />
            </span>
            <div>
              <strong>
                {entry.model ? shortModel(entry.model) : 'Unattributed'}
              </strong>
              <span>
                {new Date(entry.at).toLocaleString([], {
                  month: 'short',
                  day: 'numeric',
                  hour: '2-digit',
                  minute: '2-digit',
                  second: '2-digit',
                })}{' '}
                ·{' '}
                {entry.outputTokens === null
                  ? 'Output tokens not reported'
                  : `${num(entry.outputTokens)} output tokens`}
              </span>
            </div>
            <strong>{money(entry.usd)}</strong>
          </div>
        ))}
        {!data?.entries.length && (
          <div className="empty">
            {data
              ? 'No saved credits in this range.'
              : error
                ? 'Credits could not be loaded.'
                : 'Loading credits…'}
          </div>
        )}
      </div>
      {view === 'list' && (
        <Pagination aria-label="Credit history pages">
          <PaginationContent>
            <PaginationItem>
              <PaginationPrevious
                href="#"
                aria-disabled={previousDisabled}
                tabIndex={previousDisabled ? -1 : 0}
                onClick={(e) => {
                  e.preventDefault();
                  if (!previousDisabled) {
                    setPage(page - 1);
                    if (page === 2) setBrowseWindow(null);
                  }
                }}
              />
            </PaginationItem>
            <PaginationItem>
              <span className="page-number">
                {page} / {data ? pages : '…'}
              </span>
            </PaginationItem>
            <PaginationItem>
              <PaginationNext
                href="#"
                aria-disabled={nextDisabled}
                tabIndex={nextDisabled ? -1 : 0}
                onClick={(e) => {
                  e.preventDefault();
                  if (!nextDisabled) {
                    // Reuse the window that produced page 1, not the later click
                    // time, so new arrivals cannot move its last row onto page 2.
                    if (page === 1)
                      setBrowseWindow(saved?.window ?? bounds(range));
                    setPage(page + 1);
                  }
                }}
              />
            </PaginationItem>
          </PaginationContent>
        </Pagination>
      )}
      <p className="footnote">
        The API supplies the latest 1,000 credits; new credits are saved
        locally. Available since{' '}
        {data?.coverageStart
          ? new Date(data.coverageStart * 1000).toLocaleString()
          : 'the first successful sync'}
        . Older hourly totals remain in Earnings.
      </p>
    </section>
  );
});
