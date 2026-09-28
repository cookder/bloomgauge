'use client';
import { useEffect, useState } from 'react';
import { startChartPolling } from '@/lib/chart-polling';
import { usePageVisible } from '@/lib/use-page-visibility';
import { useScreenActive } from './app-navigation';
import { Activity } from 'lucide-react';
import {
  perToken,
  validUnitEarnings,
  yieldMoney,
  type UnitEarnings,
} from '@/lib/model-yields';
import {
  validConditionalBaseline,
  type ConditionalBaseline,
} from '@/lib/optimizer-response';
import {
  bounds,
  Choice,
  money,
  num,
  shortModel,
  TimeChart,
  type Range,
} from './shared';

type ModelRecord = {
  id: string;
  members: string[];
  credits: { usd: number; jobs: number; first: number; last: number } | null;
  warmHours: number;
  earlierHours: number;
  first: number | null;
  last: number | null;
  unitEarnings?: UnitEarnings;
  demandUnitEarnings?: UnitEarnings;
  demandComparison?: ConditionalBaseline | null;
  evidence: {
    hours?: number;
    usd?: number;
    usdPerHour?: number;
    jobs?: number;
    days?: number;
  };
};
type History = {
  at: number;
  models: ModelRecord[];
  selectedModel: string | null;
  totals: {
    inferenceUSD: number;
    paidJobs: number;
    warmHours: number;
    earlierHours: number;
  };
  coverageStart: number | null;
  coverageEnd: number | null;
  bucketSeconds: number;
  samples: {
    at: number;
    confirmedUSD: number | null;
    usdPerWarmHour: number | null;
    warmHours: number | null;
    earlierHours: number | null;
    usdPerRequest?: number | null;
    usdPerMillionOutput?: number | null;
    usdPerMillionTokens?: number | null;
    matchedUsdPerWarmHour?: number | null;
    matchedUsdPerRequest?: number | null;
    matchedUsdPerMillionOutput?: number | null;
    matchedUsdPerMillionTokens?: number | null;
  }[];
  tracking: { counting: boolean; detail: string; models: string[] };
  switchingMode: string;
};
const date = (at?: number | null) =>
  at == null
    ? '—'
    : new Date(at * 1000).toLocaleDateString([], {
        month: 'short',
        day: 'numeric',
      });
const lines = {
  credits: [
    {
      key: 'confirmedUSD',
      label: 'Saved inference credits',
      color: 'var(--c-82efb5)',
    },
  ],
  rate: [
    {
      key: 'usdPerWarmHour',
      label: 'Earnings / verified warm hour',
      color: 'var(--c-82efb5)',
    },
  ],
  time: [
    {
      key: 'warmHours',
      label: 'Verified warm hours',
      color: 'var(--c-82efb5)',
    },
    {
      key: 'earlierHours',
      label: 'Earlier hours · warmth unverified',
      color: 'var(--c-f3c57e)',
    },
  ],
  request: [
    {
      key: 'usdPerRequest',
      label: 'Earnings / credited request',
      color: 'var(--c-b49cff)',
    },
  ],
  output: [
    {
      key: 'usdPerMillionOutput',
      label: 'Job earnings / 1M output tokens',
      color: 'var(--c-91bcff)',
    },
  ],
  tokens: [
    {
      key: 'usdPerMillionTokens',
      label: 'Job earnings / 1M total tokens',
      color: 'var(--c-91bcff)',
    },
  ],
};

function YieldReadout({
  stats,
  totalTokens,
}: {
  stats?: UnitEarnings;
  totalTokens: boolean;
}) {
  const rate = totalTokens
    ? stats?.usdPerMillionTokens
    : stats?.usdPerMillionOutput;
  const count = totalTokens
    ? stats?.completeTokenSamples
    : stats?.outputSamples;
  return (
    <dl
      className="model-yields"
      aria-label="Observed earnings per token and request"
    >
      <div>
        <dt>Earnings / {totalTokens ? 'total' : 'output'} token</dt>
        <dd>{yieldMoney(perToken(rate), 8)}</dd>
        <small>{yieldMoney(rate, 3)} / 1M tokens</small>
        <small>
          {num(count)} / {num(stats?.requests)} requests measured
        </small>
      </div>
      <div>
        <dt>Earnings / credited request</dt>
        <dd>{yieldMoney(stats?.usdPerRequest)}</dd>
        <small>{num(stats?.requests)} credited requests</small>
        {!!stats?.adjustments && (
          <small>{num(stats.adjustments)} adjustments excluded</small>
        )}
      </div>
    </dl>
  );
}

function DemandReadout({ row }: { row?: ModelRecord }) {
  const d = row?.demandComparison;
  const scope =
    d?.scope === 'weekday_time'
      ? 'Same weekday · nearby time'
      : d?.scope === 'daytype_time'
        ? 'Same weekday/weekend · nearby time'
        : 'Similar demand · all times';
  return (
    <div className="history-demand" aria-label="Performance at similar demand">
      <div className="history-demand-title">
        <span>At similar demand</span>
        <small>
          {d?.forecastUsable
            ? 'Repeated evidence'
            : d?.hours
              ? 'Early observation'
              : 'Not yet measured'}
        </small>
      </div>
      <strong>
        {money(d?.usdPerHour)}
        <small> / warm hour</small>
      </strong>
      <p>
        {num(d?.hours != null ? d.hours * 60 : null)} matched warm min ·{' '}
        {num(d?.days)} dates · {num(d?.paidJobs)} credits
      </p>
      {d?.hours ? (
        <p>
          {num(d.requestsPerMinute, 1)} req / min · {num(d.tokensPerSecond, 1)}{' '}
          output tok / s<br />
          {scope}
        </p>
      ) : null}
      <small>
        {row?.members.length === 2
          ? 'Pair demand matching is not available. Choose All warm work for the exact pair’s results.'
          : !d?.current
            ? 'Waiting for fresh demand readings to match saved work.'
            : d.hours
              ? d.forecastUsable
                ? 'Observed comparison, not guaranteed future earnings.'
                : 'Small or incomplete sample · not a reliable earnings forecast yet.'
              : 'No warm work at this demand yet. Older quiet runs do not predict today’s earnings.'}
      </small>
      {d?.current && (
        <details>
          <summary>Demand match & coverage</summary>
          <p>
            Now: {num(d.current.pressure, 2)} load / warm ·{' '}
            {num(d.current.active, 1)} active · {num(d.current.warm, 1)} warm
            providers.
          </p>
          <p>
            {num(d.otherDemandHours, 2)}h at other demand ·{' '}
            {num(d.otherContextHours, 2)}h outside the selected time/coverage
            context · {num(d.unknownDemandHours, 2)}h without paired network
            readings. These hours remain in historical totals.
          </p>
          <p>
            {num((d.matchingCoverage ?? 0) * 100)}% network coverage in
            comparison periods. Match pressure, active requests, total load and
            warm capacity within a factor of two. Nearby weekday/time is used
            when repeated observations support it.
          </p>
        </details>
      )}
    </div>
  );
}

export function ModelHistory({
  range,
  paused,
  enabled = true,
}: {
  range: Range;
  paused: boolean;
  enabled?: boolean;
}) {
  const [saved, setSaved] = useState<{ key: string; data: History } | null>(
    null,
  );
  const [failure, setFailure] = useState<{
    key: string;
    message: string;
  } | null>(null);
  const [model, setModel] = useState('');
  const [metric, setMetric] = useState<keyof typeof lines>('rate');
  const [view, setView] = useState<'models' | 'trend'>('models');
  const [sort, setSort] = useState('rate');
  const [tokenBasis, setTokenBasis] = useState('output');
  const [comparison, setComparison] = useState('demand');
  const queryKey = JSON.stringify([range, model]);
  const data = saved?.key === queryKey ? saved.data : null;
  const error = failure?.key === queryKey ? failure.message : '';
  const active = useScreenActive();
  const pageVisible = usePageVisible();
  useEffect(() => {
    if (
      !active ||
      !pageVisible ||
      !enabled ||
      (paused && saved?.key === queryKey)
    )
      return;
    return startChartPolling({
      load: async (signal) => {
        const { start, end } = bounds(range);
        const res = await fetch(
          `/api/model-history?from=${start}&to=${end}${model ? `&model=${encodeURIComponent(model)}` : ''}`,
          { cache: 'no-store', signal },
        );
        if (!res.ok)
          throw Error(
            'Saved model history could not be refreshed. Check that Bloomkeeper is running on your Mac.',
          );
        const value = (await res.json()) as History;
        if (
          !value ||
          !Array.isArray(value.models) ||
          !Array.isArray(value.samples) ||
          !value.totals ||
          !value.tracking ||
          !Array.isArray(value.tracking.models) ||
          !value.tracking.models.every((id) => typeof id === 'string') ||
          !value.models.every(
            (row) =>
              row &&
              typeof row.id === 'string' &&
              Array.isArray(row.members) &&
              row.members.every((id) => typeof id === 'string') &&
              row.evidence &&
              validUnitEarnings(row.unitEarnings) &&
              validUnitEarnings(row.demandUnitEarnings) &&
              (row.demandComparison == null ||
                validConditionalBaseline(row.demandComparison)),
          ) ||
          !value.samples.every((sample) =>
            validUnitEarnings(
              sample && 'requests' in sample ? sample : undefined,
            ),
          )
        )
          throw Error('Saved model history returned an incomplete response.');
        return value;
      },
      onValue: (value) => {
        setSaved({ key: queryKey, data: value });
        setFailure(null);
      },
      onError: (error) => setFailure({ key: queryKey, message: error.message }),
      intervalMs: 15000,
      repeat: !paused,
    });
  }, [range, model, queryKey, paused, active, pageVisible, enabled]);
  const tokenKey =
    tokenBasis === 'total' ? 'usdPerMillionTokens' : 'usdPerMillionOutput';
  const yields = (row?: ModelRecord) =>
    comparison === 'demand' ? row?.demandUnitEarnings : row?.unitEarnings;
  const rate = (row: ModelRecord) =>
    comparison === 'demand'
      ? row.demandComparison?.usdPerHour
      : row.evidence.usdPerHour;
  const series = lines[metric].map((s) =>
    comparison === 'demand' && !['time', 'credits'].includes(metric)
      ? {
          ...s,
          key: `matched${s.key[0].toUpperCase()}${s.key.slice(1)}`,
          label: `${s.label} · similar demand`,
        }
      : s,
  );
  const rows = [...(data?.models ?? [])].sort((a, b) =>
    sort === 'token' || sort === 'request'
      ? (yields(b)?.[sort === 'token' ? tokenKey : 'usdPerRequest'] ??
          -Infinity) -
        (yields(a)?.[sort === 'token' ? tokenKey : 'usdPerRequest'] ??
          -Infinity)
      : sort === 'warm'
        ? b.warmHours - a.warmHours
        : sort === 'earlier'
          ? b.earlierHours - a.earlierHours
          : sort === 'rate'
            ? (rate(b) ?? -Infinity) - (rate(a) ?? -Infinity)
            : (b.credits?.usd ?? 0) - (a.credits?.usd ?? 0),
  );
  const modelOptions = data?.models ?? saved?.data.models ?? [];
  const selected = modelOptions.find(
    (row) =>
      row.id === (model || data?.selectedModel || saved?.data.selectedModel),
  );
  const peakHours = Math.max(
    1,
    ...rows.map((row) => row.warmHours + row.earlierHours),
  );
  const pickModel = (id: string) => {
    setModel(id);
    if (id.startsWith('@combo:')) setMetric('rate');
    setView('trend');
  };
  return (
    <div className="model-history">
      <div className="passive-status" role="status">
        <Activity size={20} aria-hidden="true" />
        <div>
          <strong>
            {error
              ? 'History connection interrupted'
              : paused
                ? 'History view paused'
                : data?.tracking.counting
                  ? 'Passive tracking · counting warm time'
                  : data
                    ? 'Passive tracking · waiting for readiness'
                    : 'Loading saved history…'}
          </strong>
          <p>
            {error
              ? `${error}${data ? ' Showing the last loaded history for this selection.' : ''}`
              : data?.tracking.detail ||
                'Saved history stays available without starting a test.'}
          </p>
          {data?.tracking.models.length ? (
            <small>
              {data.tracking.models.map(shortModel).join(' + ')} ·{' '}
              {data.switchingMode === 'observe'
                ? 'Automatic switching off'
                : 'Automatic switching enabled'}
            </small>
          ) : null}
        </div>
      </div>
      <div className="model-history-summary">
        <div>
          <span>Saved inference earnings</span>
          <strong>{money(data?.totals.inferenceUSD)}</strong>
          <small>{num(data?.totals.paidJobs)} paid credits · this Mac</small>
        </div>
        <div>
          <span>Verified warm time</span>
          <strong>
            {num(data?.totals.warmHours, 2)} <small>h</small>
          </strong>
          <small>Including ready idle time</small>
        </div>
        <div>
          <span>Earlier observations</span>
          <strong>
            {num(data?.totals.earlierHours, 2)} <small>h</small>
          </strong>
          <small>Saved before warm-up verification</small>
        </div>
      </div>
      <p className="footnote">
        Every observed run adds to this library, whether you choose the model
        yourself or run a scheduled test. Starting a test resets its schedule,
        never these records. Collecting requires Bloomkeeper to stay open.
      </p>
      <div className="model-history-toolbar">
        <Choice
          value={comparison}
          onChange={setComparison}
          label="Model performance comparison"
          options={[
            { value: 'demand', label: 'Similar demand now' },
            { value: 'all', label: 'All warm work' },
          ]}
        />
        <div className="app-result-toggle" aria-label="Saved history view">
          <button
            type="button"
            aria-pressed={view === 'models'}
            onClick={() => setView('models')}
          >
            All models
          </button>
          <button
            type="button"
            aria-pressed={view === 'trend'}
            onClick={() => setView('trend')}
          >
            Model trend
          </button>
        </div>
        <Choice
          value={tokenBasis}
          onChange={(v) => {
            setTokenBasis(v);
            if (metric === 'output' || metric === 'tokens')
              setMetric(v === 'total' ? 'tokens' : 'output');
          }}
          label="Earnings token basis"
          options={[
            { value: 'output', label: 'Output tokens' },
            { value: 'total', label: 'Prompt + output tokens' },
          ]}
        />
        {view === 'models' ? (
          <Choice
            value={sort}
            onChange={setSort}
            label="Sort saved models"
            options={[
              { value: 'credits', label: 'Saved earnings' },
              { value: 'warm', label: 'Verified warm hours' },
              { value: 'earlier', label: 'Earlier observed hours' },
              { value: 'rate', label: 'Verified earnings / hour' },
              { value: 'token', label: 'Earnings / token' },
              { value: 'request', label: 'Earnings / request' },
            ]}
          />
        ) : null}
      </div>
      <p className="footnote model-yield-scope">
        {comparison === 'demand'
          ? 'Comparing only work at similar network demand in the selected period. Quiet runs at other demand stay in historical totals. Rankings show observations, not switching recommendations. '
          : 'Comparing all verified warm work in the selected period, including quiet hours. '}
        {comparison === 'demand' &&
        (error || (data && Date.now() / 1000 - data.at > 90))
          ? 'Saved demand comparison · refresh needed. '
          : ''}
        Yields use settled credits from complete, covered warm minutes in this
        period. Token values divide the whole job’s earnings by its measured{' '}
        {tokenBasis === 'total' ? 'prompt + output' : 'output'} tokens; they are
        observed yields, not token price quotes. Sample counts show how much
        evidence supports each value.
      </p>
      {view === 'models' ? (
        <div className="model-history-cards">
          {rows.map((row) => (
            <article key={row.id} className="model-history-card">
              <div className="model-history-card-title">
                <h3>{shortModel(row.id)}</h3>
                <span>
                  {date(row.first)}–{date(row.last)}
                </span>
              </div>
              {comparison === 'demand' && <DemandReadout row={row} />}
              <YieldReadout
                stats={yields(row)}
                totalTokens={tokenBasis === 'total'}
              />
              {row.members.length === 2 && (
                <p className="footnote">
                  Exact pair · combined request yield. Not included in either
                  solo model’s yield.
                </p>
              )}
              <dl>
                <div>
                  <dt>Saved credits</dt>
                  <dd>
                    {row.members.length === 2
                      ? 'Joint warm results below'
                      : row.credits
                        ? money(row.credits.usd)
                        : 'No saved credits'}
                  </dd>
                </div>
                <div>
                  <dt>Historical earnings / warm hour</dt>
                  <dd>{money(row.evidence.usdPerHour)}</dd>
                </div>
                <div>
                  <dt>Verified warm hours</dt>
                  <dd>{num(row.warmHours, 2)}h</dd>
                </div>
                <div>
                  <dt>Earlier observed hours</dt>
                  <dd className={row.earlierHours ? 'earlier-value' : ''}>
                    {num(row.earlierHours, 2)}h
                  </dd>
                </div>
              </dl>
              <div
                className="history-hours-track"
                role="img"
                aria-label={`${num(row.warmHours, 2)} verified warm hours and ${num(row.earlierHours, 2)} earlier hours with unverified warmth`}
              >
                <span
                  style={{ width: `${(row.warmHours / peakHours) * 100}%` }}
                />
                <span
                  style={{ width: `${(row.earlierHours / peakHours) * 100}%` }}
                />
              </div>
              <p className="footnote">
                {row.evidence.hours
                  ? `${num(row.evidence.hours, 2)}h with settled, covered earnings · ${money(row.evidence.usd)} · ${num(row.evidence.jobs)} paid jobs`
                  : 'No complete warm minutes with settled earnings in this period yet.'}
              </p>
              <button
                className="text-link"
                type="button"
                onClick={() => pickModel(row.id)}
              >
                View model trend →
              </button>
            </article>
          ))}
        </div>
      ) : (
        <div className="model-history-trend">
          <div className="optimizer-options">
            <Choice
              value={
                model || data?.selectedModel || saved?.data.selectedModel || ''
              }
              onChange={pickModel}
              label="Saved model history"
              options={[
                ...modelOptions.map((row) => ({
                  value: row.id,
                  label: shortModel(row.id),
                })),
                ...(model && !modelOptions.some((row) => row.id === model)
                  ? [
                      {
                        value: model,
                        label: `${shortModel(model)} · no observations`,
                      },
                    ]
                  : []),
              ]}
            />
            <Choice
              value={metric}
              onChange={(v) => setMetric(v as keyof typeof lines)}
              label="Saved model trend metric"
              options={[
                ...(selected?.members.length === 2
                  ? []
                  : [{ value: 'credits', label: 'Saved inference earnings' }]),
                { value: 'rate', label: 'Earnings / verified warm hour' },
                { value: 'time', label: 'Recorded hours' },
                { value: 'request', label: 'Earnings / credited request' },
                {
                  value: tokenBasis === 'total' ? 'tokens' : 'output',
                  label: `Earnings / 1M ${tokenBasis === 'total' ? 'total' : 'output'} tokens`,
                },
              ]}
            />
          </div>
          {comparison === 'demand' && (
            <DemandReadout row={data ? selected : undefined} />
          )}
          <YieldReadout
            stats={data ? yields(selected) : undefined}
            totalTokens={tokenBasis === 'total'}
          />
          {enabled && (
            <TimeChart
              key={`${queryKey}:${metric}:${comparison}`}
              data={data?.samples ?? []}
              series={series}
              showPoints={
                comparison === 'demand' &&
                series.some(
                  (s) =>
                    (data?.samples ?? []).filter(
                      (p) => typeof p[s.key as keyof typeof p] === 'number',
                    ).length <= 10,
                )
              }
              height={230}
              precision={metric === 'request' ? 8 : 4}
              unit={
                metric === 'credits'
                  ? ' USD'
                  : metric === 'rate'
                    ? ' USD / warm hour'
                    : metric === 'request'
                      ? ' USD / credited request'
                      : metric === 'output' || metric === 'tokens'
                        ? ` USD / 1M ${metric === 'output' ? 'output' : 'total'} tokens`
                        : ' hours'
              }
            />
          )}
          <div className="model-history-legend">
            {series.map((s) => (
              <span key={s.key}>
                <i style={{ background: s.color }} />
                {s.label}
              </span>
            ))}
          </div>
          <p className="footnote">
            {comparison === 'demand' && !['time', 'credits'].includes(metric)
              ? 'Only matching demand/time minutes enter these points; other demand and missing observations stay blank. '
              : ''}
            {num((data?.bucketSeconds ?? 3600) / 3600)}-hour buckets.{' '}
            {metric === 'credits'
              ? 'All saved credits across all demand levels, including older runs and credits whose warm-up state is unknown. Incomplete coverage can show a partial amount; missing intervals stay blank.'
              : metric === 'time'
                ? 'All recorded demand levels. Green is verified warm time; amber is earlier time whose warmth was not recorded. Minute-resolution boundary time is prorated.'
                : metric === 'request' ||
                    metric === 'output' ||
                    metric === 'tokens'
                  ? 'Each point divides matching credited earnings by matching requests or measured tokens in that bucket. No requests or missing token measurements stay blank. Only settled, covered warm work is included; signed adjustments are separate.'
                  : 'Only complete verified warm minutes with covered earnings enter this rate, including ready idle time. Earnings settle for two minutes; missing evidence stays blank.'}
          </p>
        </div>
      )}
      {data && !data.models.length && (
        <div className="empty">
          No saved model observations or device-matched credits in this period.
          Choose a longer range; passive tracking needs no test to begin.
        </div>
      )}
      <details className="model-history-note">
        <summary>Why older hours differ from verified performance</summary>
        <p className="footnote">
          Older records are preserved, but did not save proof that the model was
          loaded and pre-warmed. They appear in amber and never inflate verified
          earnings rates or automatic-switching evidence. Saved credits remain
          real money; they can cover periods when Bloomkeeper was not recording
          runtime. No missing hours are reconstructed from credit timestamps.
        </p>
        <p className="footnote">
          Verified warm hours include recently recorded and partial minutes.
          Rates require complete minutes plus earnings coverage and a two-minute
          settlement delay. Pair runtime belongs to the exact pair; it is not
          added to either model’s solo runtime. Base rewards and unmatched
          devices are excluded here.
        </p>
        <p className="footnote">
          Per-request yields use nonnegative inference credits, including
          zero-dollar requests; negative adjustments remain in saved earnings
          but are excluded from job averages. Token yields use only the earnings
          of those same jobs with known token counts. Unknown counts are not
          zero-filled. Solo yields exclude jobs served in a pair; each exact
          pair has its own combined yield.
        </p>
        <p className="footnote">
          Automatic optimization uses qualifying passive and scheduled
          observations from the trailing seven days, regardless of this display
          range. Older saved history remains available here.
        </p>
      </details>
    </div>
  );
}
