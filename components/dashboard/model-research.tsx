'use client';
import { EarningsOutlookPanel } from './earnings-outlook';
import { ScreenActivityBoundary } from './app-navigation';
import { useEffect, useState } from 'react';
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { startChartPolling } from '@/lib/chart-polling';
import { usePageVisible } from '@/lib/use-page-visibility';
import { validSmoothing } from '@/lib/operating-response';
import { modelColor } from '@/lib/model-earnings';
import { useScreenActive } from './app-navigation';
import { ModelDemandPanel, type Capacity } from './model-demand';
import {
  bounds,
  Choice,
  money,
  num,
  RangePicker,
  shortModel,
  TimeChart,
  type Range,
} from './shared';

type Point = { at: number; [key: string]: number | null };
type Detail = {
  model: string;
  from: number;
  to: number;
  coverageStart: number | null;
  coverageEnd: number | null;
  samples: Point[];
  localSamples: Point[];
  markers: { at: number; kind: string; model: string }[];
  bucketSeconds: number;
  localBucketSeconds: number;
};
type Factor = {
  id: string;
  available: boolean;
  score: number | null;
  fixedScore: number | null;
  pressure: number | null;
  active: number | null;
  warm: number | null;
  coverage: number;
  sourceAt: number | null;
  mixBasis: string;
  inputFractionUsed: number;
  blend: number | null;
  reason: string | null;
  mixMinutes: number;
  mixLastAt: number | null;
  price: {
    at: number | null;
    fresh: boolean;
    inputUSDPerMillion: number | null;
    outputUSDPerMillion: number | null;
  };
  mix: {
    completeJobs: number;
    totalJobs: number;
    realizedUSDPerMillion: number | null;
  };
  priceOnlyRange: [number, number] | null;
};
type Outcome = {
  minutes: number;
  status: string;
  usdPerClockHour: number | null;
  warmUSDPerHour?: number | null;
  warmMinutes?: number;
  selectionChanged?: boolean;
  comparable?: boolean;
  demandRatio?: number | null;
  coveredPercent?: number;
  firstWorkSeconds?: number | null;
};
type Evaluation = {
  at: number;
  model: string;
  leader?: string | null;
  score?: number | null;
  outcomes: Outcome[];
  forecastUSDPerWarmHour?: number | null;
  reason?: string;
  switchSeconds?: number | null;
  kind?: string;
};
type Study = {
  at: number;
  version: string;
  passive: boolean;
  fresh: boolean;
  coverageStart: number | null;
  coverageEnd: number | null;
  observations: number;
  bucketSeconds: number;
  history: { at: number; scores: Record<string, number> }[];
  latest: {
    at: number;
    models: Factor[];
    selected: string[];
    leader: string | null;
    fixedLeader: string | null;
    decision: { phase: string; reason: string } | null;
  } | null;
  evaluations: Evaluation[];
  switches: Evaluation[];
};
const stamp = (at: number | null | undefined) =>
  at == null
    ? 'Not yet recorded'
    : new Date(at * 1000).toLocaleString([], {
        month: 'short',
        day: 'numeric',
        hour: 'numeric',
        minute: '2-digit',
      });
const finite = (n: unknown): n is number =>
  typeof n === 'number' && Number.isFinite(n);

export function useStudy<T>(
  path: string,
  range: Range,
  paused: boolean,
  valid: (d: T) => boolean,
  intervalMs = 60000,
) {
  const key = JSON.stringify([path, range]);
  const [saved, setSaved] = useState<{ key: string; data: T } | null>(null);
  const [failure, setFailure] = useState<{ key: string; error: string } | null>(
    null,
  );
  const active = useScreenActive(),
    visible = usePageVisible();
  useEffect(() => {
    if (!active || !visible || (paused && saved?.key === key)) return;
    return startChartPolling({
      load: async (signal) => {
        const b = bounds(range);
        const res = await fetch(
          `${path}${path.includes('?') ? '&' : '?'}from=${b.start}&to=${b.end}`,
          { signal, cache: 'no-store' },
        );
        if (!res.ok) throw Error('History is unavailable.');
        const d = (await res.json()) as T;
        if (!valid(d)) throw Error('Incomplete history response.');
        return d;
      },
      onValue: (data) => {
        setSaved({ key, data });
        setFailure(null);
      },
      onError: (e) => setFailure({ key, error: e.message }),
      intervalMs,
      repeat: !paused,
    });
  }, [key, paused, active, visible]);
  return {
    data: saved?.key === key ? saved.data : null,
    error: failure?.key === key ? failure.error : '',
  };
}
const validDetail = (d: Detail) =>
  !!d &&
  typeof d.model === 'string' &&
  Array.isArray(d.samples) &&
  d.samples.every((p) => p && finite(p.at)) &&
  Array.isArray(d.localSamples) &&
  d.localSamples.every((p) => p && finite(p.at)) &&
  Array.isArray(d.markers);
const validEvaluation = (r: Evaluation) =>
  !!r &&
  finite(r.at) &&
  typeof r.model === 'string' &&
  (r.leader == null || typeof r.leader === 'string') &&
  Array.isArray(r.outcomes) &&
  r.outcomes.every(
    (o) => o && finite(o.minutes) && typeof o.status === 'string',
  );
const validStudy = (d: Study) =>
  !!d &&
  d.passive === true &&
  Array.isArray(d.history) &&
  d.history.every(
    (p) => p && finite(p.at) && p.scores && typeof p.scores === 'object',
  ) &&
  Array.isArray(d.evaluations) &&
  d.evaluations.every(validEvaluation) &&
  Array.isArray(d.switches) &&
  d.switches.every(validEvaluation) &&
  (!d.latest ||
    (Array.isArray(d.latest.models) &&
      d.latest.models.every(
        (m) =>
          m &&
          typeof m.id === 'string' &&
          m.price &&
          m.mix &&
          (m.priceOnlyRange == null ||
            (Array.isArray(m.priceOnlyRange) && m.priceOnlyRange.length === 2)),
      ) &&
      Array.isArray(d.latest.selected)));

export function StudyChart({
  data,
  series,
  from,
  to,
  unit,
  height = 190,
  markers = [],
  syncId,
}: {
  data: Point[];
  series: { key: string; label: string; color: string; faint?: boolean }[];
  from: number;
  to: number;
  unit: string;
  height?: number;
  markers?: Detail['markers'];
  syncId: string;
}) {
  const active = useScreenActive(),
    visible = usePageVisible();
  const precision = unit.includes('USD') || unit.startsWith('score') ? 3 : 1;
  const last = series
    .filter((s) => !s.faint)
    .map((s) => ({ s, p: [...data].reverse().find((p) => finite(p[s.key])) }));
  const plotted: Point[] = data.length
    ? [{ at: from }, ...data, { at: to }]
    : [];
  return (
    <div className="research-chart-block">
      <div className="research-end-labels">
        {last.map(({ s, p }) => (
          <span key={s.key} style={{ color: s.color }}>
            <i style={{ background: s.color }} />
            {s.label} <strong>{num(p?.[s.key], precision)}</strong>
          </span>
        ))}
      </div>
      <div
        style={{ height, minWidth: 0 }}
        aria-label={series
          .filter((s) => !s.faint)
          .map((s) => s.label)
          .join(', ')}
      >
        {active && visible && plotted.length > 0 ? (
          <ResponsiveContainer
            width="100%"
            height="100%"
            minWidth={0}
            initialDimension={{ width: 500, height }}
          >
            <LineChart
              data={plotted}
              syncId={syncId}
              syncMethod="value"
              margin={{ left: -14, right: 12, top: 8, bottom: 0 }}
            >
              <CartesianGrid
                vertical={false}
                stroke="#27303c"
                strokeDasharray="2 6"
              />
              <XAxis
                dataKey="at"
                type="number"
                domain={[from, to]}
                allowDataOverflow
                axisLine={false}
                tickLine={false}
                minTickGap={55}
                tick={{ fill: '#96a6b8', fontSize: 11 }}
                tickFormatter={(v) =>
                  new Date(v * 1000).toLocaleString(
                    [],
                    to - from > 86400
                      ? { month: 'short', day: 'numeric' }
                      : { hour: 'numeric', minute: '2-digit' },
                  )
                }
              />
              <YAxis
                width={64}
                axisLine={false}
                tickLine={false}
                tick={{ fill: '#96a6b8', fontSize: 11 }}
                tickFormatter={(v) =>
                  unit.includes('USD') ? money(v) : num(v, 1)
                }
              />
              <Tooltip
                contentStyle={{
                  background: '#171d27',
                  border: '1px solid #354153',
                  borderRadius: 10,
                  fontSize: 12,
                }}
                labelFormatter={(v) => stamp(Number(v))}
                formatter={(v, n) => [
                  `${num(Number(v), unit.includes('USD') ? 4 : precision)} ${unit}`,
                  n,
                ]}
              />
              {markers
                .filter((m) => m.kind === 'switching')
                .slice(-8)
                .map((m) => (
                  <ReferenceLine
                    key={m.at}
                    x={m.at}
                    stroke="#ad9aff"
                    strokeOpacity={0.5}
                    strokeDasharray="3 4"
                  />
                ))}
              {series.map((s) => (
                <Line
                  key={s.key}
                  dataKey={s.key}
                  name={s.label}
                  stroke={s.color}
                  strokeOpacity={s.faint ? 0.22 : 1}
                  strokeWidth={s.faint ? 1 : 2}
                  dot={({ cx, cy, index }) => {
                    const i = index ?? -1;
                    return !s.faint &&
                      finite(cx) &&
                      finite(cy) &&
                      finite(plotted[i]?.[s.key]) &&
                      !finite(plotted[i - 1]?.[s.key]) &&
                      !finite(plotted[i + 1]?.[s.key]) ? (
                      <circle
                        className="research-isolated-dot"
                        key={i}
                        cx={cx}
                        cy={cy}
                        r={3}
                        fill={s.color}
                      />
                    ) : (
                      <g key={i} />
                    );
                  }}
                  connectNulls={false}
                  isAnimationActive={false}
                  type="linear"
                />
              ))}
            </LineChart>
          </ResponsiveContainer>
        ) : (
          <div className="empty">Waiting for observed data.</div>
        )}
      </div>
      <small className="muted">
        {unit} · local time · latest observed values above
      </small>
    </div>
  );
}

function SupplyDemand({
  paused,
  capacity,
}: {
  paused: boolean;
  capacity: Capacity[];
}) {
  const [range, setRange] = useState<Range>({ preset: '1h' });
  const [chosen, setChosen] = useState('');
  const [raw, setRaw] = useState(false);
  useEffect(() => {
    if (!chosen && capacity.length)
      setChosen(
        [...capacity].sort(
          (a, b) =>
            b.active_requests - a.active_requests || a.id.localeCompare(b.id),
        )[0].id,
      );
  }, [capacity, chosen]);
  const model = chosen || capacity[0]?.id || 'gemma-4-26b-qat-4bit';
  const { data, error } = useStudy<Detail>(
    `/api/network/model-detail?model=${encodeURIComponent(model)}`,
    range,
    paused,
    validDetail,
  );
  const b = bounds(range);
  const from =
    range.preset === 'all'
      ? (data?.coverageStart ?? b.start)
      : (data?.from ?? b.start);
  const to = data?.to ?? b.end;
  const latest = data?.samples.filter((p) => finite(p.pressure)).at(-1);
  return (
    <section
      className="panel research-panel"
      aria-label="Supply and demand detail"
    >
      <div className="panel-heading">
        <div>
          <div className="eyebrow">SUPPLY & DEMAND</div>
          <h2>Is demand reaching your Mac?</h2>
        </div>
      </div>
      <div className="chart-toolbar">
        <Choice
          label="Supply and demand model"
          value={model}
          onChange={setChosen}
          options={[...new Set([model, ...capacity.map((m) => m.id)])].map(
            (id) => ({ value: id, label: shortModel(id) }),
          )}
        />
        <RangePicker
          value={range}
          onChange={setRange}
          label="Supply and demand range"
        />
      </div>
      {error && (
        <p className="notice" role="status">
          {error}{' '}
          {paused
            ? 'Resume the live view to retry.'
            : 'Retrying automatically.'}{' '}
          {data ? 'Showing saved values for this selection.' : ''}
        </p>
      )}
      <div className="research-summary">
        <span>
          <strong>{num(latest?.pressure, 2)}</strong> load / warm provider
        </span>
        <label>
          <input
            type="checkbox"
            checked={raw}
            onChange={(e) => setRaw(e.target.checked)}
          />{' '}
          Show unsmoothed samples
        </label>
      </div>
      <StudyChart
        data={data?.samples ?? []}
        series={[
          ...(raw
            ? [
                {
                  key: 'rawActive',
                  label: 'Sampled active',
                  color: '#96a6b8',
                  faint: true,
                },
              ]
            : []),
          { key: 'active', label: 'Active requests', color: '#91bcff' },
          { key: 'warm', label: 'Warm providers', color: '#82efb5' },
          { key: 'queued', label: 'Queued', color: '#f3c57e' },
        ]}
        from={from}
        to={to}
        unit="concurrent count"
        height={240}
        markers={data?.markers}
        syncId="demand-study"
      />
      <p className="footnote">
        15-minute trailing averages with at least 80% sampling coverage. Load =
        active + queued; providers can serve several requests. A high ratio is
        an opportunity signal, not a promise of paid work.
      </p>
      <div className="research-local">
        <div>
          <h3>This Mac · paid pace</h3>
          <StudyChart
            data={data?.localSamples ?? []}
            series={[
              {
                key: 'usdPerWarmHour',
                label: 'Confirmed inference',
                color: '#82efb5',
              },
            ]}
            from={from}
            to={to}
            unit="USD / warm hour"
            markers={data?.markers}
            syncId="demand-study"
          />
        </div>
        <div>
          <h3>This Mac · served requests</h3>
          <StudyChart
            data={data?.localSamples ?? []}
            series={[
              {
                key: 'requestsPerMinute',
                label: 'Local requests',
                color: '#b49cff',
              },
            ]}
            from={from}
            to={to}
            unit="requests / warm minute"
            markers={data?.markers}
            syncId="demand-study"
          />
        </div>
      </div>
      <p className="footnote">
        Local strips use {num((data?.localBucketSeconds ?? 300) / 60)}m buckets
        with at least 80% complete, covered solo warm minutes; two-minute credit
        settlement. Idle warm time counts. Other models, pairs and missing
        observations stay blank. Purple markers show switch starts.
      </p>
      <p className="footnote">
        Network history begins {stamp(data?.coverageStart)}. Latest snapshot{' '}
        {stamp(data?.coverageEnd)}.{' '}
        {data?.coverageStart != null && data.coverageStart > from && (
          <button
            className="text-link"
            onClick={() =>
              setRange({
                preset: 'custom',
                start: data.coverageStart!,
                end: to,
              })
            }
          >
            Fit available history
          </button>
        )}
      </p>
      {!!data?.markers.length && (
        <details>
          <summary>Model changes in this period</summary>
          {data.markers
            .slice(-12)
            .reverse()
            .map((m) => (
              <p className="footnote" key={`${m.at}:${m.kind}`}>
                {stamp(m.at)} · {m.kind} · {shortModel(m.model)}
              </p>
            ))}
        </details>
      )}
    </section>
  );
}

function Results({
  rows,
  switches = false,
}: {
  rows: Evaluation[];
  switches?: boolean;
}) {
  const [minutes, setMinutes] = useState('30');
  return (
    <div className="research-results">
      <Choice
        label={
          switches ? 'Switch outcome horizon' : 'Observation outcome horizon'
        }
        value={minutes}
        onChange={setMinutes}
        options={['15', '30', '60'].map((v) => ({
          value: v,
          label: `Next ${v} minutes`,
        }))}
      />
      {!rows.length && (
        <p className="muted">
          Collecting prospective observations. Settled results will appear here.
        </p>
      )}
      {rows.map((r, i) => {
        const o = r.outcomes.find((v) => v.minutes === Number(minutes));
        return (
          <article key={`${r.at}:${i}`}>
            <div>
              <strong>{shortModel(r.model)}</strong>
              <small>
                {stamp(r.at)}
                {!switches && r.leader
                  ? ` · score leader: ${shortModel(r.leader)}`
                  : ''}
              </small>
            </div>
            <div>
              <strong>{money(o?.usdPerClockHour)} / clock hr</strong>
              <small>
                {o?.status === 'settling'
                  ? 'Waiting for the full period + settlement'
                  : o?.status === 'partial'
                    ? `Incomplete credit coverage (${num(o.coveredPercent)}%)`
                    : o?.selectionChanged
                      ? 'Selection changed during this period'
                      : o?.comparable
                        ? 'Same model · covered observation'
                        : 'Limited warm coverage'}
              </small>
            </div>
            {switches && (
              <p className="footnote">
                {r.forecastUSDPerWarmHour == null
                  ? 'No qualified dollar forecast'
                  : `Saved forecast ${money(r.forecastUSDPerWarmHour)} / warm hr`}
                ; observed {money(o?.warmUSDPerHour)} / covered warm hr. Switch{' '}
                {num(r.switchSeconds)}s. {r.reason}
              </p>
            )}
            {o?.demandRatio != null && (
              <small>
                Following demand: {num(o.demandRatio, 2)}× its starting
                pressure.
              </small>
            )}
          </article>
        );
      })}
      <p className="footnote">
        Clock rates include quiet and switching time and all this Mac’s
        inference credits during the period. A changed selection is not a
        single-model outcome. Warm rates use only complete verified minutes.
        Unchosen-model earnings are unknown; overlapping windows are not
        independent experiments.
      </p>
    </div>
  );
}

function SmoothingComparison({ value }: { value: unknown }) {
  if (!validSmoothing(value)) return null;
  return (
    <details className="research-outcomes">
      <summary>Smoothing lab · ranking stability</summary>
      <div className="smoothing-grid">
        {value.variants.map((v) => (
          <article key={v.halfLifeSeconds}>
            <strong>
              {v.halfLifeSeconds
                ? `${v.halfLifeSeconds / 60}m half-life`
                : 'Saved index'}
            </strong>
            <p>{num(v.leaderChanges)} leader changes</p>
            <p>{num(v.reversals)} quick reversals</p>
            <p>
              {num(
                v.meanFollowSeconds == null ? null : v.meanFollowSeconds / 60,
                1,
              )}
              m mean follow delay
            </p>
            <p>
              {num(v.unresolvedLeaders)} not followed /{' '}
              {num(v.confirmedLeaders)} persistent leaders
            </p>
            <p>{num(v.agreementPercent)}% rank agreement</p>
          </article>
        ))}
      </div>
      <p className="footnote">
        {value.method} {num(value.variants[0]?.observations)} observations in
        this range. The auto-picker does not use these smoothed rankings. Fewer
        reversals alone does not establish better earnings.
      </p>
    </details>
  );
}

function Opportunities({ paused }: { paused: boolean }) {
  const [range, setRange] = useState<Range>({ preset: '24h' });
  const [focus, setFocus] = useState('');
  const { data, error } = useStudy<Study>(
    '/api/opportunities',
    range,
    paused,
    validStudy,
  );
  const models = data?.latest?.models ?? [];
  const selected =
    models.find((m) => m.id === focus) ??
    models.find((m) => m.available) ??
    models[0];
  const top = models.filter((m) => m.available).slice(0, 3);
  const chartModels = focus && selected ? [selected] : top;
  const chart = (data?.history ?? []).map((p) => ({
    at: p.at,
    ...Object.fromEntries(
      chartModels.map((m, i) => [
        `model${i}`,
        finite(p.scores[m.id]) ? p.scores[m.id] : null,
      ]),
    ),
  }));
  return (
    <section
      className="panel research-panel"
      aria-label="Passive opportunity scores"
    >
      <div className="panel-heading">
        <div>
          <div className="eyebrow">OPPORTUNITY SCORES</div>
          <h2>Learn before changing the picker.</h2>
        </div>
        <span
          className={`status-pill ${data?.fresh && !error ? 'good' : 'warn'}`}
        >
          {paused
            ? 'View paused'
            : data?.fresh &&
                !error &&
                Date.now() / 1000 - (data.coverageEnd ?? 0) <= 120
              ? 'Passive · collecting'
              : 'Waiting / saved'}
        </span>
      </div>
      <p className="footnote">
        Price-aware demand rankings are recorded every minute on your Mac, even
        with the phone closed. The auto-picker’s decisions are unchanged.
      </p>
      <div className="chart-toolbar">
        <RangePicker
          value={range}
          onChange={setRange}
          label="Opportunity history range"
        />
        <Choice
          value={focus}
          onChange={setFocus}
          label="Opportunity model"
          options={[
            { value: '', label: 'Top 3 locally available' },
            ...models.map((m) => ({ value: m.id, label: shortModel(m.id) })),
          ]}
        />
      </div>
      {error && (
        <p className="notice" role="status">
          {error}{' '}
          {paused
            ? 'Resume the live view to retry.'
            : 'Retrying automatically.'}{' '}
          {data ? 'Showing saved results.' : ''}
        </p>
      )}
      <div className="research-score-legend">
        {chartModels.map((m) => (
          <span key={m.id} style={{ color: modelColor(m.id) }}>
            {shortModel(m.id)} <strong>{num(m.score, 4)}</strong>
          </span>
        ))}
      </div>
      <TimeChart
        data={chart}
        series={chartModels.map((m, i) => ({
          key: `model${i}`,
          label: shortModel(m.id),
          color: modelColor(m.id),
        }))}
        unit=" index"
        precision={4}
        height={220}
        showPoints={chart.length === 1}
      />
      <p className="footnote">
        Index, not dollars/hour. Saved history starts{' '}
        {stamp(data?.coverageStart)}; latest {stamp(data?.coverageEnd)}.{' '}
        {num(data?.observations)} observations. Chart uses{' '}
        {num((data?.bucketSeconds ?? 60) / 60)}m averages; blank periods have no
        saved score.
      </p>
      <p className="footnote">
        Latest ranking · {stamp(data?.latest?.at)} · local availability is not
        full switch eligibility.
      </p>
      <div className="research-ranking" aria-label="Latest passive ranking">
        {models.map((m) => (
          <button
            key={m.id}
            onClick={() => setFocus(m.id)}
            aria-pressed={selected?.id === m.id}
            className="research-rank"
          >
            <span className="research-rank-name">
              <i style={{ background: modelColor(m.id) }} />
              {shortModel(m.id)}
              <small>
                {m.available ? 'Locally available' : 'Network only'}
                {data?.latest?.selected.includes(m.id) ? ' · selected' : ''}
              </small>
            </span>
            <span>
              <strong>{num(m.score, 4)}</strong>
              <small>
                {m.mixBasis === 'measured'
                  ? 'Measured token mix'
                  : '85/15 assumption'}
              </small>
            </span>
          </button>
        ))}
      </div>
      {selected && (
        <div className="research-factors">
          <h3>{shortModel(selected.id)} · score ingredients</h3>
          <div className="research-factor-grid">
            <span>
              Active / warm, 15m<strong>{num(selected.pressure, 3)}</strong>
            </span>
            <span>
              Published price at token mix
              <strong>{money(selected.blend)} / 1M</strong>
            </span>
            <span>
              Measured-mix / fixed-mix score
              <strong>
                {num(selected.score, 4)} / {num(selected.fixedScore, 4)}
              </strong>
            </span>
            <span>
              Input / output mix used
              <strong>
                {num(selected.inputFractionUsed * 100, 1)}% /{' '}
                {num((1 - selected.inputFractionUsed) * 100, 1)}%
              </strong>
            </span>
          </div>
          <p className="footnote">
            {selected.mixBasis === 'measured'
              ? `${num(selected.mix.completeJobs)} measured jobs across ${num(selected.mixMinutes)} solo warm minutes in the preceding seven days.`
              : 'Using an explicit 85% input / 15% output assumption until there are 50 measured jobs across 30 solo warm minutes, with work in the last 24 hours.'}{' '}
            Demand coverage {num(selected.coverage * 100)}%. Price{' '}
            {selected.price.fresh ? 'checked' : 'stale / unavailable'}{' '}
            {stamp(selected.price.at)}. {selected.reason}
          </p>
          <p className="footnote">
            Score = average active requests / warm providers × blended published
            USD per million tokens. Model weight is 1. Queued requests are shown
            in Supply & demand, but excluded from this score. Token mix can
            change; its price-only range is{' '}
            {selected.priceOnlyRange
              ? `${num(selected.priceOnlyRange[0], 4)}–${num(selected.priceOnlyRange[1], 4)}`
              : 'unknown'}
            , not a confidence interval. Actual yield:{' '}
            {money(selected.mix.realizedUSDPerMillion)} / 1M measured tokens.
          </p>
        </div>
      )}
      {data?.latest?.decision && (
        <p className="notice">Actual picker: {data.latest.decision.reason}</p>
      )}
      <SmoothingComparison
        value={data && 'smoothing' in data ? data.smoothing : null}
      />
      <details className="research-outcomes">
        <summary>Score observations → actual earnings</summary>
        <p className="footnote">
          Latest 24 hourly checkpoints within this range. Scores are saved
          before the following earnings arrive.
        </p>
        <Results rows={data?.evaluations ?? []} />
      </details>
      <details className="research-outcomes">
        <summary>Actual switches → forecast and outcome</summary>
        <p className="footnote">
          Switches since passive recording began. A saved warm-hour forecast and
          a clock-hour result have different denominators; compare the covered
          warm result alongside demand persistence.
        </p>
        <Results rows={data?.switches ?? []} switches />
      </details>
    </section>
  );
}

// The dashboard's demand card opens this panel on Demand trends instead of the earnings outlook.
let pendingTrends = false;
export function requestDemandTrends() {
  pendingTrends = true;
  window.dispatchEvent(new Event('bloom:demand-trends'));
}

export function ModelResearchPanel({
  paused,
  capacity,
}: {
  paused: boolean;
  capacity?: {
    status: string;
    updatedAt?: number;
    error?: string;
    data?: { models: Capacity[] };
  };
}) {
  const [expanded, setExpanded] = useState(false),
    [view, setView] = useState('detail'),
    [mainView, setMainView] = useState(() => {
      const trends = pendingTrends;
      pendingTrends = false;
      return trends ? 'traffic' : 'earnings';
    });
  useEffect(() => {
    const show = () => {
      pendingTrends = false;
      setMainView('traffic');
    };
    window.addEventListener('bloom:demand-trends', show);
    return () => window.removeEventListener('bloom:demand-trends', show);
  }, []);
  // The forecast lab exists only in the personal edition; ask once when the advanced panel opens.
  const [personal, setPersonal] = useState(false);
  useEffect(() => {
    if (!expanded || personal) return;
    const c = new AbortController();
    fetch('/api/predictive-lab', { cache: 'no-store', signal: c.signal })
      .then((r) => (r.ok ? (r.json() as Promise<{ enabled?: boolean }>) : null))
      .then((p) => setPersonal(p?.enabled === true))
      .catch(() => {});
    return () => c.abort();
  }, [expanded, personal]);
  return (
    <div className="model-research">
      <div
        className="research-tabs outlook-view-tabs"
        role="group"
        aria-label="Demand comparison view"
      >
        {[
          ['earnings', 'Earnings outlook'],
          ['traffic', 'Demand trends'],
        ].map(([id, label]) => (
          <button
            key={id}
            type="button"
            aria-pressed={mainView === id}
            onClick={() => setMainView(id)}
          >
            {label}
          </button>
        ))}
      </div>
      <div hidden={mainView !== 'earnings'}>
        <ScreenActivityBoundary active={mainView === 'earnings'}>
          <EarningsOutlookPanel paused={paused} />
        </ScreenActivityBoundary>
      </div>
      <div hidden={mainView !== 'traffic'}>
        <ScreenActivityBoundary active={mainView === 'traffic'}>
          <ModelDemandPanel capacity={capacity} paused={paused} />
        </ScreenActivityBoundary>
      </div>
      <details
        className="advanced-analysis"
        onToggle={(e) => setExpanded(e.currentTarget.open)}
      >
        <summary>Advanced analysis · supply, scores & outcomes</summary>
        {expanded && (
          <>
            <div
              className="research-tabs"
              role="group"
              aria-label="Advanced demand analysis"
            >
              {[
                ['detail', 'Supply & demand'],
                ['scores', 'Experimental scores'],
                ...(personal
                  ? [['forecasts', 'Forecast & portfolio lab']]
                  : []),
              ].map(([id, label]) => (
                <button
                  key={id}
                  onClick={() => setView(id)}
                  aria-pressed={view === id}
                >
                  {label}
                </button>
              ))}
            </div>
            {view === 'detail' ? (
              <SupplyDemand
                capacity={capacity?.data?.models ?? []}
                paused={paused}
              />
            ) : view === 'forecasts' && personal ? (
              <PredictiveLabPanel paused={paused} />
            ) : (
              <Opportunities paused={paused} />
            )}
          </>
        )}
      </details>
    </div>
  );
}

type LabForecast = {
  minutes: number;
  prediction: number | null;
  baseline: number;
  trainingWindows: number;
  income: {
    usdPerWarmHour: number | null;
    observedHours: number;
    dates: number;
    status: string;
    assumption: string;
  } | null;
};
type LabModel = {
  id: string;
  available: boolean;
  observed: { pressure: number };
  forecasts: LabForecast[];
};
type Portfolio = {
  id: string;
  members: string[];
  hours: number;
  usd: number;
  observedUSDPerHour: number;
  blocks: number;
  days: number;
  stddevUSDPerHour: number | null;
  belowTargetPercent: number | null;
  status: string;
  perModel: Record<string, { usd: number }>;
};
type Lab = {
  enabled: boolean;
  version: string;
  fresh: boolean;
  observations: number;
  latest: { origin: number; models: LabModel[] } | null;
  accuracy: {
    minutes: number;
    windows: number;
    mae: number | null;
    baselineMAE: number | null;
    improvementPercent: number | null;
  }[];
  portfolios: Portfolio[];
};
const validLab = (d: Lab) =>
  !!d &&
  d.enabled === true &&
  typeof d.version === 'string' &&
  Array.isArray(d.accuracy) &&
  d.accuracy.every(
    (a) =>
      finite(a.minutes) &&
      finite(a.windows) &&
      (a.mae == null || finite(a.mae)) &&
      (a.baselineMAE == null || finite(a.baselineMAE)),
  ) &&
  Array.isArray(d.portfolios) &&
  d.portfolios.every(
    (p) =>
      typeof p.id === 'string' &&
      Array.isArray(p.members) &&
      p.members.every((m) => typeof m === 'string') &&
      p.perModel &&
      typeof p.perModel === 'object' &&
      finite(p.hours) &&
      finite(p.observedUSDPerHour),
  ) &&
  (!d.latest ||
    (finite(d.latest.origin) &&
      Array.isArray(d.latest.models) &&
      d.latest.models.every(
        (m) =>
          typeof m.id === 'string' &&
          m.observed &&
          finite(m.observed.pressure) &&
          Array.isArray(m.forecasts) &&
          m.forecasts.every(
            (f) =>
              finite(f.minutes) &&
              finite(f.baseline) &&
              (f.prediction == null || finite(f.prediction)) &&
              (!f.income ||
                f.income.usdPerWarmHour == null ||
                finite(f.income.usdPerWarmHour)),
          ),
      )));

function PredictiveLabPanel({ paused }: { paused: boolean }) {
  const [range, setRange] = useState<Range>({ preset: '30d' }),
    [model, setModel] = useState('');
  const { data, error } = useStudy<Lab>(
    '/api/predictive-lab',
    range,
    paused,
    validLab,
  );
  const rows = data?.latest?.models ?? [],
    selected =
      rows.find((m) => m.id === model) ??
      rows.find((m) => m.available) ??
      rows[0];
  return (
    <section
      className="panel predictive-lab"
      aria-label="Forecast and portfolio lab"
    >
      <div className="panel-heading">
        <div>
          <div className="eyebrow">LOCAL RESEARCH TRIAL</div>
          <h2>Look ahead. Measure what happens.</h2>
        </div>
        <span
          className={`status-pill ${data?.fresh && !error ? 'good' : 'warn'}`}
        >
          {paused
            ? 'View paused'
            : data?.fresh && !error
              ? 'Shadow · collecting'
              : 'Waiting / saved'}
        </span>
      </div>
      <p className="footnote">
        Forecasts learn from past demand and run on this Mac with the phone
        closed. They do not control model selection. Earnings under future
        demand are conditional estimates, not guaranteed returns.
      </p>
      <div className="chart-toolbar">
        <RangePicker
          value={range}
          onChange={setRange}
          label="Forecast evaluation and portfolio range"
        />
        <Choice
          value={selected?.id ?? ''}
          onChange={setModel}
          label="Forecast model"
          options={rows.map((m) => ({ value: m.id, label: shortModel(m.id) }))}
        />
      </div>
      {error && (
        <p className="notice" role="alert">
          {error} Saved readings may be stale.
        </p>
      )}
      {!data && !error && (
        <p className="muted">Loading the local research trial…</p>
      )}
      {data && !data.latest && (
        <p className="muted">
          Waiting for the next quarter-hour checkpoint with an uninterrupted
          hour of demand readings. Earlier forecasts are never backfilled.
        </p>
      )}
      {selected && (
        <>
          <div className="predictive-heading">
            <h3>{shortModel(selected.id)}</h3>
            <span>
              {stamp(data?.latest?.origin)} · {!data?.fresh ? 'saved · ' : ''}
              {selected.available
                ? 'locally eligible'
                : 'not currently eligible for switching'}
            </span>
          </div>
          <div className="predictive-grid">
            {selected.forecasts.map((f) => (
              <article key={f.minutes}>
                <span className="eyebrow">
                  NEXT {f.minutes} MINUTES · MEAN DEMAND
                </span>
                <strong className="predictive-value">
                  {num(f.prediction, 2)} <small>load / warm</small>
                </strong>
                <p className="small">
                  Persistence comparison: {num(f.baseline, 2)}
                </p>
                <p className="footnote">
                  {f.prediction == null
                    ? 'Learning · needs 48 past hourly windows across three dates.'
                    : `${num(f.trainingWindows, 0)} past hourly windows · experimental regression`}
                </p>
                <div className="predictive-income">
                  <span>At predicted demand, if supply stays similar</span>
                  <strong>
                    {f.income?.usdPerWarmHour == null
                      ? 'Earnings unknown'
                      : `${money(f.income.usdPerWarmHour)} / warm hr`}
                  </strong>
                  <small>
                    {num(f.income?.observedHours, 1)} matched paid hours ·{' '}
                    {num(f.income?.dates, 0)} dates. Warm-hour estimates exclude
                    switching and cold time.
                  </small>
                </div>
              </article>
            ))}
          </div>
        </>
      )}
      <h3>Does prediction beat staying with the recent average?</h3>
      <div className="predictive-grid">
        {data?.accuracy.map((a) => (
          <article key={a.minutes}>
            <strong>{a.minutes}-minute accuracy</strong>
            <p>
              {a.windows
                ? `${num(a.windows, 0)} settled model checkpoints`
                : 'Waiting for recorded forecasts to mature'}
            </p>
            <p className="small">
              Mean absolute error: {num(a.mae, 3)} · recent-average baseline:{' '}
              {num(a.baselineMAE, 3)}
            </p>
            <span className="footnote">
              {a.improvementPercent == null
                ? 'No measured improvement yet.'
                : `${num(Math.abs(a.improvementPercent), 1)}% ${a.improvementPercent >= 0 ? 'lower' : 'higher'} error. This is demand accuracy, not earnings uplift.`}
            </span>
          </article>
        ))}
      </div>
      <p className="footnote">
        Only hourly checkpoints with fully observed outcomes are scored, after a
        two-minute delay. Missing readings remain gaps. Models share network
        conditions, so checkpoint counts are not independent experiments.
      </p>
      <h3>One model or a pair?</h3>
      <p className="footnote">
        Bloomkeeper supports trials of two resident models. Automatic demand selection
        still ranks solo models. These are measured outcomes for each exact
        serving set; solo rates are never added to predict pair income.
      </p>
      <div className="predictive-portfolios">
        {data?.portfolios.map((p) => (
          <article key={p.id}>
            <div>
              <strong>{p.members.map(shortModel).join(' + ')}</strong>
              <small>
                {num(p.hours, 1)} verified warm hours · {num(p.blocks, 0)}{' '}
                complete 15-minute blocks ·{' '}
                {p.status === 'limited'
                  ? 'limited evidence'
                  : 'repeated observations'}
              </small>
            </div>
            <strong>
              {money(p.observedUSDPerHour)} <small>/ warm hr observed</small>
            </strong>
            <p className="footnote">
              Earnings variability:{' '}
              {p.stddevUSDPerHour == null
                ? 'not enough joint / repeated history'
                : `${money(p.stddevUSDPerHour)} / hr standard deviation`}
              .{' '}
              {p.belowTargetPercent == null
                ? 'No complete blocks yet.'
                : `${num(p.belowTargetPercent, 0)}% of complete blocks below $0.12/hr.`}
            </p>
            {p.members.length > 1 && (
              <p className="footnote">
                {p.members
                  .map(
                    (m) =>
                      `${shortModel(m)}: ${money(p.perModel[m]?.usd)} confirmed`,
                  )
                  .join(' · ')}
              </p>
            )}
          </article>
        ))}
      </div>
      {data && !data.portfolios.some((p) => p.members.length > 1) && (
        <p className="notice">
          No verified pair history in this range. A joint earnings forecast and
          any reduction in variability remain unknown.
        </p>
      )}
      <p className="footnote">
        Variability needs 24 complete blocks across three dates. Different
        models ran at different times and demand levels; this report cannot
        establish that pairing causes higher or steadier earnings.
      </p>
    </section>
  );
}
