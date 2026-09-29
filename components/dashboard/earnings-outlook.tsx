'use client';
import { useEffect, useRef, useState } from 'react';
import { EarningsForecastView } from './earnings-forecast';
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { startChartPolling } from '@/lib/chart-polling';
import { usePageVisible } from '@/lib/use-page-visibility';
import { modelColor } from '@/lib/model-earnings';
import {
  bandLabel,
  demandBands,
  outlookReading,
  validEarningsOutlook,
  type EarningsOutlook,
  type EarningsBand,
} from '@/lib/earnings-outlook';
import { useAppNavigation, useScreenActive } from './app-navigation';
import {
  age,
  bounds,
  Choice,
  money,
  num,
  RangePicker,
  shortModel,
  type Range,
} from './shared';
const scopeLabels: Record<string, string> = {
  weekday_time: 'Same weekday and nearby time',
  daytype_time: 'Same weekday/weekend and nearby time',
  similar_demand: 'Similar demand · all times of day',
};
const bandNames = ['0–.25', '.25–.5', '.5–1', '1–2', '2–4', '4–8', '8+'];
export function EarningsOutlookPanel({ paused }: { paused: boolean }) {
  const [range, setRange] = useState<Range>({ preset: '30d' }),
    [condition, setCondition] = useState('current'),
    [focus, setFocus] = useState('');
  const [saved, setSaved] = useState<{
    key: string;
    data: EarningsOutlook | null;
    error: string;
  } | null>(null);
  const [showAll, setShowAll] = useState(false),
    [view, setView] = useState<'forecast' | 'history'>('forecast');
  const [clock, setClock] = useState(() => Date.now() / 1000);
  const focusPanel = useRef<HTMLDivElement>(null),
    panel = useRef<HTMLElement>(null);
  function inspectModel(model: string) {
    setFocus(model);
    if (window.matchMedia('(max-width:1099px)').matches)
      requestAnimationFrame(() =>
        (view === 'forecast'
          ? panel.current?.querySelector('.forecast-focus')
          : focusPanel.current
        )?.scrollIntoView({ block: 'start', behavior: 'instant' }),
      );
  }
  const active = useScreenActive(),
    visible = usePageVisible(),
    { navigate } = useAppNavigation();
  const key = JSON.stringify(range),
    data = saved?.key === key ? saved.data : null,
    error = saved?.key === key ? saved.error : '';
  useEffect(() => {
    if (!active || !visible || (paused && data)) return;
    return startChartPolling({
      load: async (signal) => {
        const { start, end } = bounds(range);
        const res = await fetch(
          `/api/optimizer/earnings-outlook?from=${start}&to=${end}`,
          { signal, cache: 'no-store' },
        );
        if (!res.ok)
          throw Error('Earnings outlook could not be refreshed. Retrying.');
        const value: unknown = await res.json();
        if (!validEarningsOutlook(value))
          throw Error('Earnings outlook returned incomplete data. Retrying.');
        return value;
      },
      onValue: (value) => {
        setClock(Date.now() / 1000);
        setSaved({ key, data: value, error: '' });
      },
      onError: (e) =>
        setSaved((old) => ({
          key,
          data: old?.key === key ? old.data : null,
          error: e.message,
        })),
      intervalMs: 30000,
      repeat: !paused,
    });
  }, [key, active, visible, paused]);
  useEffect(() => {
    if (!active || !visible || paused) return;
    setClock(Date.now() / 1000);
    const timer = setInterval(() => setClock(Date.now() / 1000), 15000);
    return () => clearInterval(timer);
  }, [active, visible, paused]);
  const now = clock,
    stale = !!error || (!!data && now - data.at > 150);
  const models = [...(data?.models ?? [])].sort(
    (a, b) =>
      Number(b.serving) - Number(a.serving) ||
      Number(b.eligible) - Number(a.eligible) ||
      a.model.localeCompare(b.model),
  );
  const selected =
    models.find((m) => m.model === focus) ??
    models.find((m) => m.serving) ??
    [...models].sort((a, b) => b.pairedWarmHours - a.pairedWarmHours)[0] ??
    models[0];
  const candidatesKnown = models.some((m) => m.eligible);
  const shown =
    showAll || !candidatesKnown
      ? models
      : models.filter(
          (m) => m.eligible || m.serving || m.model === selected?.model,
        );
  const maxRate = Math.max(
    0.01,
    ...models.map((m) =>
      Math.max(0, outlookReading(m, condition, now, stale).rate ?? 0),
    ),
  );
  const picked = selected
    ? outlookReading(selected, condition, now, stale)
    : null;
  const chart =
    selected?.bands.map((band, index) => ({
      ...band,
      index,
      label: bandNames[index],
    })) ?? [];
  const pressure = selected?.current.current?.pressure;
  const currentFresh =
    selected &&
    outlookReading(selected, 'current', now, stale).label !==
      'Demand unavailable';
  const chartTip = (b: EarningsBand) => (
    <div className="baseline-tooltip">
      <strong>{bandLabel(b.low, b.high)} load / warm</strong>
      <p>{money(b.usdPerHour)} / warm hour observed</p>
      <p>
        {num(b.hours, 1)}h · {b.days} dates · {b.blocks} substantial periods
      </p>
      <p>
        {b.quality === 'repeated'
          ? 'Repeated observations'
          : b.quality === 'older'
            ? 'Older evidence'
            : b.hours
              ? 'Limited observations'
              : 'No observations'}
      </p>
    </div>
  );
  return (
    <section
      ref={panel}
      className="panel earnings-outlook-panel"
      aria-label="Earnings outlook by demand"
    >
      <div className="panel-heading">
        <div>
          <div className="eyebrow">DEMAND → EARNINGS</div>
          <h2>
            {view === 'forecast'
              ? 'What could each model earn next hour?'
              : 'Earnings at recorded demand'}
          </h2>
        </div>
        {view === 'history' && (
          <RangePicker
            value={range}
            onChange={setRange}
            presets={['7d', '30d', '90d', 'all', 'custom']}
            label="Earnings outlook history range"
          />
        )}
      </div>
      <div
        className="research-tabs forecast-view-tabs"
        role="group"
        aria-label="Earnings outlook view"
      >
        {(
          [
            ['forecast', 'Next hour'],
            ['history', 'Historical demand'],
          ] as const
        ).map(([id, label]) => (
          <button
            key={id}
            type="button"
            aria-pressed={view === id}
            onClick={() => setView(id)}
          >
            {label}
          </button>
        ))}
      </div>
      <p className="small muted">
        {paused
          ? 'View paused'
          : stale
            ? 'Refresh delayed · saved report'
            : `Updated ${age(data?.at)}`}
      </p>
      {error && (
        <p role="alert" className="notice">
          {error} {data ? 'Showing the saved report.' : ''}
        </p>
      )}
      {view === 'forecast' ? (
        !data ? (
          <p className="muted">Preparing the next-hour outlook…</p>
        ) : !models.length ? (
          <p className="notice">
            No model observations yet. Missing history is not a $0 forecast.
          </p>
        ) : (
          <EarningsForecastView
            models={models}
            selected={selected}
            onSelect={inspectModel}
            now={now}
            stale={stale}
            evaluation={data.forecastEvaluation}
            persistence={data.forecastPersistence}
            onHistory={() => setView('history')}
          />
        )
      ) : (
        <>
          <p className="outlook-intro">
            Similar demand can pay very differently. Compare this Mac’s
            inference earnings per warm hour, with missing history left unknown.
          </p>
          <div className="outlook-toolbar">
            <label>
              Compare at
              <Choice
                value={condition}
                onChange={setCondition}
                label="Earnings outlook demand level"
                options={[
                  { value: 'current', label: 'Each model’s current demand' },
                  ...demandBands.map(([a, b], i) => ({
                    value: String(i),
                    label: `${bandLabel(a, b)} load / warm`,
                  })),
                ]}
              />
            </label>
            <span className="small muted">
              {paused
                ? 'View paused'
                : stale
                  ? 'Saved observations'
                  : `Updated ${age(data?.at)}`}
            </span>
          </div>
          <p className="outlook-context">
            {condition === 'current'
              ? 'Each model is matched to its own current demand, active requests and warm-provider count. Nearby day/time is preferred when enough history exists.'
              : 'All models use this same pressure band. These are observations across recorded times and provider counts, not a forecast at today’s exact conditions.'}
          </p>
          {!data ? (
            <p className="muted">
              {error
                ? 'No saved response for this period.'
                : 'Matching paid work with recorded demand…'}
            </p>
          ) : !models.length ? (
            <p className="notice">
              No model observations yet. Leave BloomGauge running to build
              history; this is not a $0 forecast.
            </p>
          ) : (
            <>
              <div className="outlook-body">
                <div className="outlook-comparison">
                  <div
                    className="outlook-models"
                    aria-label="Model earnings comparison"
                  >
                    {shown.map((model) => {
                      const r = outlookReading(model, condition, now, stale);
                      const color = modelColor(model.model);
                      return (
                        <button
                          key={model.model}
                          type="button"
                          className={`outlook-model ${r.supported ? 'supported' : 'limited'}`}
                          aria-pressed={selected?.model === model.model}
                          onClick={() => inspectModel(model.model)}
                        >
                          <span className="outlook-model-name">
                            <i style={{ background: color }} />
                            <strong>{shortModel(model.model)}</strong>
                            {model.serving && <small>Serving</small>}
                          </span>
                          <span className="outlook-rate">
                            <strong>{money(r.rate)}</strong>
                            <small> / warm hr</small>
                          </span>
                          <span className="outlook-bar" aria-hidden="true">
                            <i
                              style={{
                                width: `${r.rate == null ? 0 : Math.min(100, (Math.max(0, r.rate) / maxRate) * 100)}%`,
                                background: color,
                              }}
                            />
                          </span>
                          <span className="outlook-quality">
                            {r.label}
                            {condition === 'current' &&
                            model.current.current &&
                            model.signalAt != null &&
                            now - model.signalAt < 90 &&
                            !stale
                              ? ` · ${num(model.current.current.pressure, 2)} load/warm`
                              : ''}
                          </span>
                          <span className="outlook-evidence">
                            {num(r.value?.hours, 1)}h matched ·{' '}
                            {r.value?.days ?? 0} dates · {r.value?.blocks ?? 0}{' '}
                            periods
                          </span>
                        </button>
                      );
                    })}
                  </div>
                  {models.length > shown.length && (
                    <button
                      className="text-link"
                      type="button"
                      onClick={() => setShowAll(true)}
                    >
                      Show {models.length - shown.length} historical models
                    </button>
                  )}
                  {showAll &&
                    candidatesKnown &&
                    models.some((m) => !m.eligible && !m.serving) && (
                      <button
                        className="text-link"
                        type="button"
                        onClick={() => setShowAll(false)}
                      >
                        Show current candidates
                      </button>
                    )}
                </div>
                {selected && picked && (
                  <div ref={focusPanel} className="outlook-focus">
                    <div className="outlook-focus-heading">
                      <div>
                        <span className="eyebrow">SELECTED MODEL</span>
                        <h3>{shortModel(selected.model)}</h3>
                      </div>
                      <strong style={{ color: modelColor(selected.model) }}>
                        {money(picked.rate)}
                        <small> / warm hr</small>
                      </strong>
                    </div>
                    <p>{picked.reason}</p>
                    {condition === 'current' && (
                      <p className="small muted">
                        {scopeLabels[selected.current.scope] ??
                          selected.current.scope}
                        .{' '}
                        {picked.supported &&
                        selected.current.lower != null &&
                        selected.current.upper != null
                          ? `Comparison range ${money(selected.current.lower)}–${money(selected.current.upper)} / hr; not a statistical confidence interval. `
                          : ''}
                        {selected.current.matchingCoverage != null
                          ? `${num(selected.current.matchingCoverage * 100)}% network coverage in matched periods.`
                          : ''}
                      </p>
                    )}
                    <div className="outlook-chart-heading">
                      <strong>Across demand levels</strong>
                      <span>Observed USD / warm hour</span>
                    </div>
                    {active &&
                      visible &&
                      chart.some((b) => b.usdPerHour != null) && (
                        <div
                          className="outlook-chart"
                          role="img"
                          aria-label={`${shortModel(selected.model)} observed earnings across seven demand bands. Exact values available in the table below.`}
                        >
                          <ResponsiveContainer
                            width="100%"
                            height="100%"
                            minWidth={0}
                            initialDimension={{ width: 600, height: 235 }}
                          >
                            <BarChart
                              data={chart}
                              margin={{ top: 20, right: 8, bottom: 0, left: 0 }}
                            >
                              <CartesianGrid
                                stroke="var(--c-263642)"
                                strokeDasharray="3 6"
                                vertical={false}
                              />
                              <XAxis
                                dataKey="label"
                                height={42}
                                angle={-30}
                                textAnchor="end"
                                tick={{ fill: 'var(--c-99aabd)', fontSize: 10 }}
                                interval={0}
                                tickLine={false}
                              />
                              <YAxis
                                domain={[
                                  (min: number) => Math.min(0, min),
                                  (max: number) => Math.max(0.01, max),
                                ]}
                                tickFormatter={(v) => money(v)}
                                width={50}
                                tick={{ fill: 'var(--c-99aabd)', fontSize: 11 }}
                                tickLine={false}
                                tickCount={4}
                              />
                              <ReferenceLine y={0} stroke="var(--c-536578)" />
                              <Tooltip
                                cursor={{ fill: 'var(--c-ffffff07)' }}
                                content={({ active, payload }) =>
                                  active && payload?.[0]?.payload
                                    ? chartTip(
                                        payload[0].payload as EarningsBand,
                                      )
                                    : null
                                }
                              />
                              <Bar
                                dataKey="usdPerHour"
                                isAnimationActive={false}
                                maxBarSize={64}
                                radius={[4, 4, 0, 0]}
                              >
                                {chart.map((b) => (
                                  <Cell
                                    key={b.low}
                                    fill={modelColor(selected.model)}
                                    fillOpacity={
                                      b.quality === 'repeated' ? 0.9 : 0.35
                                    }
                                    stroke={
                                      currentFresh &&
                                      pressure != null &&
                                      pressure >= b.low &&
                                      (b.high == null || pressure < b.high)
                                        ? 'var(--c-edf5ff)'
                                        : undefined
                                    }
                                    strokeWidth={2}
                                  />
                                ))}
                              </Bar>
                            </BarChart>
                          </ResponsiveContainer>
                        </div>
                      )}
                    {!chart.some((b) => b.usdPerHour != null) && (
                      <p className="notice">
                        No paired earnings observations for this model in this
                        period. Unknown demand bands have no assumed earnings.
                      </p>
                    )}
                    <div
                      className="outlook-band-selector"
                      aria-label="Inspect a demand band"
                    >
                      {demandBands.map(([a, b], i) => (
                        <button
                          key={a}
                          type="button"
                          className={
                            currentFresh &&
                            pressure != null &&
                            pressure >= a &&
                            (b == null || pressure < b)
                              ? 'current-demand'
                              : ''
                          }
                          aria-pressed={condition === String(i)}
                          onClick={() => setCondition(String(i))}
                        >
                          {bandNames[i]}
                          {selected.bands[i].usdPerHour == null ? (
                            <small>No data</small>
                          ) : (
                            <small>{money(selected.bands[i].usdPerHour)}</small>
                          )}
                        </button>
                      ))}
                    </div>
                    <p className="footnote">
                      Load / warm = (active + queued network requests) ÷ warm
                      providers. Faint bars have limited or older evidence;
                      blank bands are unknown.{' '}
                      {currentFresh
                        ? `White outline marks current demand: ${num(pressure, 2)} load/warm.`
                        : 'Current demand is unavailable; no live band is highlighted.'}{' '}
                      Bands do not assume earnings increase with demand.
                    </p>
                    <details className="outlook-details">
                      <summary>Evidence & exact values</summary>
                      <div
                        className="baseline-table-scroll"
                        tabIndex={0}
                        role="region"
                        aria-label="Earnings evidence by demand band"
                      >
                        <table>
                          <thead>
                            <tr>
                              <th>Load / warm</th>
                              <th>USD / warm hr</th>
                              <th>Warm hours</th>
                              <th>Dates / periods</th>
                              <th>Paid jobs</th>
                              <th>Last observation</th>
                            </tr>
                          </thead>
                          <tbody>
                            {selected.bands.map((b) => (
                              <tr key={b.low}>
                                <th>{bandLabel(b.low, b.high)}</th>
                                <td>{money(b.usdPerHour)}</td>
                                <td>{num(b.hours, 2)}</td>
                                <td>
                                  {b.days} / {b.blocks}
                                </td>
                                <td>{num(b.paidJobs)}</td>
                                <td>
                                  {b.asOf == null
                                    ? '—'
                                    : new Date(
                                        b.asOf * 1000,
                                      ).toLocaleDateString([], {
                                        month: 'short',
                                        day: 'numeric',
                                      })}
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                      <p className="footnote">
                        {num(selected.pairedWarmHours, 1)}h paired with demand
                        out of {num(selected.totalWarmHours, 1)}h covered warm
                        history. Repeated current-demand observations require at
                        least 4 matched hours across 3 dates and 8 substantial
                        periods, 200 credited jobs and a recent observation.
                        Sparse/old evidence remains visible as observations,
                        with no assumed rate for untested bands. Repeated band
                        summaries exclude thin periods; a substantial period has
                        at least 15 matched minutes within 30 minutes.
                      </p>
                    </details>
                  </div>
                )}
              </div>
              <div className="outlook-optimizer-note">
                <strong>How this informs the optimizer</strong>
                <p>
                  Historical comparisons use the optimizer’s existing
                  comparable-history calculation. Low-demand hours don’t dilute
                  a busy-demand comparison. The picker also checks recent paid
                  pace, persistent demand, switching cost and readiness—this
                  chart alone doesn’t trigger a switch.
                </p>
                <button
                  type="button"
                  className="text-link"
                  onClick={() => navigate('test')}
                >
                  View the optimizer’s actual decision →
                </button>
              </div>
              <p className="footnote">
                {data.scope}. Warm idle time counts; loading, base rewards and
                missing observations do not. This is not earnings per total
                clock hour or a promise of future income. The selected history
                range applies to this report; the optimizer uses its own 30-day
                window.
              </p>
            </>
          )}
        </>
      )}
    </section>
  );
}
