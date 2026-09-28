'use client';
import { useEffect, useState } from 'react';
import { startChartPolling } from '@/lib/chart-polling';
import { usePageVisible } from '@/lib/use-page-visibility';
import {
  bounds,
  Choice,
  RangePicker,
  TimeChart,
  money,
  num,
  shortModel,
  type Range,
} from './shared';
import { modelColor } from '@/lib/model-earnings';

type Pricing = {
  inputUSDPerMillion: number | null;
  outputUSDPerMillion: number | null;
  at: number | null;
  fresh: boolean;
  status: string;
  inputFraction: number | null;
  completeJobs: number;
  totalJobs: number;
  blendUSDPerMillion: number | null;
  realizedUSDPerMillion: number | null;
};
type Stats = {
  requests: number;
  adjustments: number;
  confirmedUSD: number;
  usdPerRequest: number | null;
  meanPrompt: number | null;
  meanOutput: number | null;
  medianOutput: number | null;
  p90Output: number | null;
  usdPerMillionTokens: number | null;
  usdPerMillionOutput: number | null;
  promptSamples: number;
  outputSamples: number;
  completeTokenSamples: number;
  warmHours?: number;
  pricing?: Pricing;
};
type Report = {
  at: number;
  settledThrough: number;
  totals: Stats;
  models: (Stats & { id: string })[];
  samples: (Stats & { at: number })[];
  bucketSeconds: number;
  distributions: {
    kind: string;
    samples: number;
    bins: { label: string; count: number; percent: number }[];
  }[];
  unusual: {
    at: number;
    model: string;
    kind: string;
    ratio: number;
    detail: string;
  }[];
  excludedUnverifiedCredits: number;
  scope: string;
  tracking: { detail: string; counting: boolean };
  coverageStart: number | null;
  coverageEnd: number | null;
};
const metrics = {
  sizes: {
    label: 'Mean request sizes',
    unit: 'tokens / request',
    precision: 0,
    series: [
      { key: 'meanPrompt', label: 'Prompt', color: 'var(--c-b49cff)' },
      { key: 'meanOutput', label: 'Output', color: 'var(--c-82efb5)' },
    ],
  },
  job: {
    label: 'Earnings per request',
    unit: 'USD / credited request',
    precision: 6,
    series: [
      {
        key: 'usdPerRequest',
        label: 'Earnings / request',
        color: 'var(--c-82efb5)',
      },
    ],
  },
  tokens: {
    label: 'Earnings per million tokens',
    unit: 'USD / 1M total tokens',
    precision: 3,
    series: [
      {
        key: 'usdPerMillionTokens',
        label: 'Blended earnings / 1M tokens',
        color: 'var(--c-91bcff)',
      },
    ],
  },
  output: {
    label: 'Earnings per million output tokens',
    unit: 'USD / 1M output tokens',
    precision: 3,
    series: [
      {
        key: 'usdPerMillionOutput',
        label: 'Job earnings / 1M output tokens',
        color: 'var(--c-f3c57e)',
      },
    ],
  },
};
const stamp = (at: number | null) =>
  at == null
    ? '—'
    : new Date(at * 1000).toLocaleString([], {
        month: 'short',
        day: 'numeric',
        hour: 'numeric',
        minute: '2-digit',
      });
export function WorkloadPanel({
  paused,
  enabled = true,
}: {
  paused: boolean;
  enabled?: boolean;
}) {
  const [range, setRange] = useState<Range>({ preset: '24h' });
  const [model, setModel] = useState('');
  const [metric, setMetric] = useState<keyof typeof metrics>('sizes');
  const [sort, setSort] = useState('requests');
  const [size, setSize] = useState('output');
  const [saved, setSaved] = useState<{ key: string; data: Report } | null>(
    null,
  );
  const [failure, setFailure] = useState<{
    key: string;
    message: string;
  } | null>(null);
  const [retry, setRetry] = useState(0);
  const visible = usePageVisible();
  const key = JSON.stringify([range, model]);
  const data = saved?.key === key ? saved.data : null;
  const error = failure?.key === key ? failure.message : '';
  useEffect(() => {
    if (!enabled || !visible || (paused && saved?.key === key && retry === 0))
      return;
    return startChartPolling({
      load: async (signal) => {
        const { start, end } = bounds(range);
        const r = await fetch(
          `/api/workload?from=${start}&to=${end}${model ? `&model=${encodeURIComponent(model)}` : ''}`,
          { cache: 'no-store', signal },
        );
        if (!r.ok)
          throw Error('Workload history is unavailable. Retry when connected.');
        const d = (await r.json()) as Report;
        if (
          !d?.totals ||
          !Array.isArray(d.models) ||
          !Array.isArray(d.samples) ||
          !Array.isArray(d.distributions) ||
          !Array.isArray(d.unusual) ||
          !d.tracking
        )
          throw Error('Incomplete workload response.');
        return d as Report;
      },
      onValue: (d) => {
        setSaved({ key, data: d });
        setFailure(null);
      },
      onError: (e) => setFailure({ key, message: e.message }),
      intervalMs: 20000,
      repeat: !paused,
    });
  }, [key, paused, enabled, visible, retry]);
  const t = data?.totals;
  const chart = metrics[metric];
  const rows = [...(data?.models ?? [])].sort((a, b) =>
    sort === 'requests'
      ? b.requests - a.requests
      : sort === 'job'
        ? (b.usdPerRequest ?? -Infinity) - (a.usdPerRequest ?? -Infinity)
        : (b.meanOutput ?? -Infinity) - (a.meanOutput ?? -Infinity),
  );
  const distribution = data?.distributions.find((d) => d.kind === size);
  const peak = Math.max(1, ...(distribution?.bins.map((b) => b.count) ?? []));
  return (
    <section className="panel workload-panel" aria-label="Workload trends">
      <div className="panel-heading">
        <div>
          <div className="eyebrow">WORKLOAD / THIS MAC</div>
          <h2>What kind of work pays?</h2>
        </div>
        <span className={`status-pill ${error ? 'warn' : 'good'}`}>
          {error
            ? 'Saved / unavailable'
            : paused
              ? 'View paused'
              : 'Saved + incoming credits'}
        </span>
      </div>
      <div className="chart-toolbar">
        <Choice
          label="Workload model"
          value={model}
          onChange={setModel}
          options={[
            { value: '', label: 'All models' },
            ...Array.from(
              new Set(
                [
                  model,
                  ...(data?.models ?? saved?.data.models ?? []).map(
                    (m) => m.id,
                  ),
                ].filter(Boolean),
              ),
            ).map((id) => ({ value: id, label: shortModel(id) })),
          ]}
        />
        <RangePicker value={range} onChange={setRange} label="Workload range" />
      </div>
      {error && (
        <p className="notice" role="status">
          {error}
          {data ? ' Showing the saved result for this selection.' : ''}{' '}
          <button
            className="small-button"
            onClick={() => setRetry((n) => n + 1)}
          >
            Retry workload
          </button>
        </p>
      )}
      <p className="footnote">
        {data?.scope ?? 'Loading verified workload history…'}
        {data ? ` · ${num(t?.warmHours, 1)} warm hours observed` : ''}
      </p>
      <div className="workload-stats">
        <div>
          <span>Credited requests</span>
          <strong>{num(t?.requests)}</strong>
        </div>
        <div>
          <span>Earnings / request</span>
          <strong>{money(t?.usdPerRequest)}</strong>
        </div>
        <div>
          <span>Median / p90 output</span>
          <strong>
            {num(t?.medianOutput)} / {num(t?.p90Output)}
          </strong>
          <small>tokens · {num(t?.outputSamples)} measured jobs</small>
        </div>
        <div>
          <span>Earnings / 1M tokens</span>
          <strong>{money(t?.usdPerMillionTokens)}</strong>
          <small>
            {num(t?.completeTokenSamples)} jobs with both token counts
          </small>
        </div>
      </div>
      {data && (
        <p className="workload-coverage">
          Prompt sizes: {num(t?.promptSamples)} / {num(t?.requests)} jobs ·
          Output sizes: {num(t?.outputSamples)} / {num(t?.requests)} jobs.{' '}
          {num(data.excludedUnverifiedCredits)} credits outside verified warm
          evidence are excluded here; recorded earnings remain intact.{' '}
          {t?.adjustments
            ? `${num(t.adjustments)} signed adjustments are separate from request averages.`
            : ''}
        </p>
      )}
      <div className="workload-visuals">
        <div>
          <div className="workload-heading">
            <h3>Workload over time</h3>
            <Choice
              label="Workload trend metric"
              value={metric}
              onChange={(v) => setMetric(v as keyof typeof metrics)}
              options={Object.entries(metrics).map(([value, m]) => ({
                value,
                label: m.label,
              }))}
            />
          </div>
          <div className="workload-legend">
            {chart.series.map((s) => (
              <span key={s.key}>
                <i style={{ background: s.color }} />
                {s.label}
              </span>
            ))}
          </div>
          {enabled && (
            <TimeChart
              data={data?.samples ?? []}
              series={chart.series}
              unit={chart.unit}
              precision={chart.precision}
              height={230}
            />
          )}
          <p className="footnote">
            {chart.unit} · local time · {num((data?.bucketSeconds ?? 300) / 60)}
            m buckets. Missing measurements stay blank. Token earnings are
            blended job yields, not advertised token prices.
          </p>
        </div>
        <div>
          <div className="workload-heading">
            <h3>Request size distribution</h3>
            <Choice
              label="Request size field"
              value={size}
              onChange={setSize}
              options={[
                { value: 'output', label: 'Output tokens' },
                { value: 'prompt', label: 'Prompt tokens' },
              ]}
            />
          </div>
          <div
            className="workload-distribution"
            aria-label={`${size} token distribution`}
          >
            {(distribution?.bins ?? []).map((b) => (
              <div key={b.label}>
                <span>{b.label}</span>
                <div className="workload-bar">
                  <i
                    style={{
                      width: `${(b.count / peak) * 100}%`,
                      background:
                        size === 'output'
                          ? 'var(--c-82efb5)'
                          : 'var(--c-b49cff)',
                    }}
                  />
                </div>
                <strong>{num(b.percent, 1)}%</strong>
                <small>{num(b.count)} jobs</small>
              </div>
            ))}
            {!distribution?.samples && (
              <p className="muted">
                No measured {size} sizes in this selection.
              </p>
            )}
          </div>
        </div>
      </div>
      <div className="workload-heading">
        <h3>Compare models</h3>
        <Choice
          label="Sort workload models"
          value={sort}
          onChange={setSort}
          options={[
            { value: 'requests', label: 'Most credited requests' },
            { value: 'job', label: 'Earnings / request' },
            { value: 'output', label: 'Mean output size' },
          ]}
        />
      </div>
      <div className="workload-models">
        {rows.map((r) => (
          <button
            key={r.id}
            className={`workload-model ${model === r.id ? 'selected' : ''}`}
            onClick={() => setModel(model === r.id ? '' : r.id)}
            aria-pressed={model === r.id}
          >
            <h4>
              <i style={{ background: modelColor(r.id) }} />
              {shortModel(r.id)}
            </h4>
            <div>
              <span>
                Requests <strong>{num(r.requests)}</strong>
              </span>
              <span>
                USD / request <strong>{money(r.usdPerRequest)}</strong>
              </span>
              <span>
                Mean prompt / output{' '}
                <strong>
                  {num(r.meanPrompt)} / {num(r.meanOutput)}
                </strong>
              </span>
              <span>
                USD / 1M tokens <strong>{money(r.usdPerMillionTokens)}</strong>
              </span>
            </div>
            <small>
              Prompt coverage {num(r.promptSamples)} / {num(r.requests)} ·{' '}
              {r.requests
                ? 'Click to filter charts'
                : 'No settled warm jobs in this period'}
            </small>
            {r.pricing && (
              <div className="workload-pricing">
                <strong>Published price & actual pay</strong>
                <span>
                  Input / output quote{' '}
                  <b>
                    {money(r.pricing.inputUSDPerMillion)} /{' '}
                    {money(r.pricing.outputUSDPerMillion)}
                  </b>
                </span>
                <span>
                  Observed input / output mix{' '}
                  <b>
                    {r.pricing.inputFraction == null
                      ? '—'
                      : `${num(r.pricing.inputFraction * 100, 1)}% / ${num((1 - r.pricing.inputFraction) * 100, 1)}%`}
                  </b>
                </span>
                {r.pricing.inputFraction != null && (
                  <span
                    className="token-mix-bar"
                    aria-label={`Input ${num(r.pricing.inputFraction * 100, 1)} percent`}
                  >
                    <i style={{ width: `${r.pricing.inputFraction * 100}%` }} />
                  </span>
                )}
                <span>
                  Current quote at that mix{' '}
                  <b>{money(r.pricing.blendUSDPerMillion)}</b>
                </span>
                <span>
                  Realized payment, same jobs{' '}
                  <b>{money(r.pricing.realizedUSDPerMillion)}</b>
                </span>
                <small>
                  USD per million total tokens · {num(r.pricing.completeJobs)} /{' '}
                  {num(r.pricing.totalJobs)} jobs with both counts.{' '}
                  {r.pricing.fresh
                    ? 'Price checked'
                    : 'Price unavailable or stale'}{' '}
                  {stamp(r.pricing.at)}. Today’s quote is not a historical
                  payment rate.
                </small>
              </div>
            )}
          </button>
        ))}
      </div>
      {data && !rows.length && (
        <p className="muted">
          No matched inference credits in this period. Try a longer range.
        </p>
      )}
      <h3 className="workload-alert-heading">Unusual activity</h3>
      <p className="footnote">
        Completed five-minute windows compared with the same model’s preceding
        hour. These are workload changes, not spam determinations.
      </p>
      <div className="workload-alerts">
        {data?.unusual.map((a, i) => (
          <article key={`${a.model}:${a.at}:${a.kind}:${i}`}>
            <strong>
              {a.kind} · {shortModel(a.model)}
            </strong>
            <span>{stamp(a.at)}</span>
            <p>{a.detail}</p>
          </article>
        ))}
      </div>
      {data && !data.unusual.length && (
        <p className="muted">
          No qualifying bursts or size shifts. Comparisons need 20 recent and 30
          earlier credited requests, four recent warm minutes and 30 earlier
          warm minutes.
        </p>
      )}
      <details className="workload-method">
        <summary>Coverage & calculations</summary>
        <p>
          Earlier output sizes reuse saved credits. Prompt counts begin with API
          pages captured by this update; older unknown sizes are never
          zero-filled. Request averages exclude base rewards and negative
          adjustments. Total-token yield uses only jobs with both token counts.
          Output-token yield includes the whole job’s payment, including prompt
          work.
        </p>
        <p>
          Only complete, covered warm minutes count, with the existing
          two-minute settlement delay. Both models must be warm for a
          combination; individual credits stay attributed to their own model.
          Bursts require at least 3× the preceding hour’s requests per warm
          minute; size shifts require a mean output at least 3× larger or 3×
          smaller with sufficient samples. No model is switched and no credits
          are removed.
        </p>
        <p>
          Observed evidence: {stamp(data?.coverageStart ?? null)} –{' '}
          {stamp(data?.coverageEnd ?? null)}. Latest eligible cutoff:{' '}
          {stamp(data?.settledThrough ?? null)}. {data?.tracking.detail}
        </p>
      </details>
    </section>
  );
}
