'use client';
import { useEffect, useState } from 'react';
import { Target } from 'lucide-react';
import { startChartPolling } from '@/lib/chart-polling';
import { usePageVisible } from '@/lib/use-page-visibility';
import {
  validTargetReport,
  type TargetReport,
  type TargetHour,
} from '@/lib/earnings-target';
import { useScreenActive, useAppNavigation } from './app-navigation';
import {
  bounds,
  Choice,
  RangePicker,
  money,
  num,
  shortModel,
  type Range,
} from './shared';

const date = (at: number) =>
  new Date(at * 1000).toLocaleString([], {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
const statuses = {
  met: 'Goal reached',
  below: 'Below goal',
  complete: 'Complete hour',
  unknown: 'Coverage missing',
  partial: 'Partial hour',
  settling: 'Credits settling',
};
export function EarningsTarget({ paused }: { paused: boolean }) {
  const [range, setRange] = useState<Range>({ preset: '24h' });
  const [model, setModel] = useState('');
  const [saved, setSaved] = useState<{
    key: string;
    data: TargetReport | null;
    error: string;
  } | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const active = useScreenActive(),
    visible = usePageVisible(),
    { mobile } = useAppNavigation();
  const key = JSON.stringify([range, model]);
  const data = saved?.key === key ? saved.data : null;
  const error = saved?.key === key ? saved.error : '';
  useEffect(() => {
    if (!active || !visible || (paused && data)) return;
    return startChartPolling({
      load: async (signal) => {
        const { start, end } = bounds(range);
        const res = await fetch(
          `/api/earnings-target?from=${start}&to=${end}${model ? `&model=${encodeURIComponent(model)}` : ''}`,
          { signal, cache: 'no-store' },
        );
        if (!res.ok)
          throw Error('Target history could not be refreshed. Retrying.');
        const value: unknown = await res.json();
        if (!validTargetReport(value))
          throw Error('Target history returned incomplete data. Retrying.');
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
  }, [key, range, model, paused, active, visible]);
  const stale = !!error || (!!data && Date.now() / 1000 - data.at > 90);
  const modelOptions = [
    ...new Set([
      ...(data?.models ?? saved?.data?.models ?? []),
      ...(model && model !== '@inference' ? [model] : []),
    ]),
  ];
  const days = new Map<string, TargetHour[]>();
  for (const hour of data?.hourly ?? []) {
    const day = new Date(hour.at * 1000).toLocaleDateString([], {
      month: 'short',
      day: 'numeric',
    });
    days.set(day, [...(days.get(day) ?? []), hour]);
  }
  const picked =
    data?.hourly.find((h) => h.at === selected) ??
    [...(data?.hourly ?? [])]
      .reverse()
      .find((h) => ['met', 'below', 'complete'].includes(h.status)) ??
    data?.hourly.at(-1);
  return (
    <section className="panel earnings-target-panel">
      <div className="panel-heading">
        <div>
          <div className="eyebrow">EARNINGS TARGET</div>
          <h2>Make every hour count.</h2>
        </div>
        <Target size={22} />
      </div>
      <div className="target-toolbar">
        <RangePicker
          value={range}
          onChange={setRange}
          label="Target history range"
        />
        <Choice
          value={model}
          onChange={setModel}
          label="Target earnings model"
          options={[
            { value: '', label: 'This Mac + account base rewards' },
            { value: '@inference', label: 'This Mac · inference only' },
            ...modelOptions.map((m) => ({ value: m, label: shortModel(m) })),
          ]}
        />
      </div>
      {error && (
        <p className="notice" role="alert">
          {error}
        </p>
      )}
      {!data ? (
        <p className="muted">
          {error
            ? 'No saved response for this selection.'
            : 'Reconciling confirmed credits with clock hours…'}
        </p>
      ) : (
        <>
          <div className="target-summary">
            <div>
              <span>Confirmed earnings</span>
              <strong>{money(data.usd)}</strong>
              <small>
                {data.clockUsdPerHour != null
                  ? `${money(data.clockUsdPerHour)} / settled clock hour`
                  : 'Partial history · no average inferred'}
              </small>
            </div>
            {data.targetUsdPerHour == null ? (
              <div>
                <span>Hourly goal</span>
                <strong>Set a goal</strong>
                <small>
                  {data.completeHours} complete hours ·{' '}
                  {data.completeHourAverageUsd == null
                    ? 'no average yet'
                    : `${money(data.completeHourAverageUsd)} average`}
                </small>
              </div>
            ) : (
              <>
                <div>
                  <span>Hours at {money(data.targetUsdPerHour)}+</span>
                  <strong
                    className={
                      data.metPercent != null && data.metPercent >= 80
                        ? 'good-text'
                        : ''
                    }
                  >
                    {data.metPercent == null
                      ? '—'
                      : `${num(data.metPercent, 0)}%`}
                  </strong>
                  <small>
                    {data.metHours} of {data.completeHours} complete hours
                  </small>
                </div>
                <div>
                  <span>Longest below goal</span>
                  <strong>
                    {data.completeHours
                      ? `${num(data.longestBelowHours ?? 0)}h`
                      : '—'}
                  </strong>
                  <small>Consecutive covered clock hours</small>
                </div>
              </>
            )}
            <div>
              <span>Credit coverage</span>
              <strong>
                {num((100 * data.coveredSeconds) / data.rangeSeconds, 0)}%
              </strong>
              <small>{data.unknownHours} full hours unknown or settling</small>
            </div>
          </div>
          <p className="target-breakdown small muted">
            {money(data.inferenceUsd)} inference
            {data.includesBase
              ? ` + ${money(data.accountBaseUsd)} account base rewards`
              : ' · base rewards excluded'}
            <br />
            {data.targetUsdPerHour == null
              ? 'No goal set. Pick one in Optimizer settings under Earnings goal. '
              : `Goal: ${money(data.targetUsdPerHour)} / hour · ${money(data.dailyTargetUsd)} / day if sustained. `}
            {stale
              ? 'Saved data · awaiting refresh.'
              : paused
                ? 'Paused view.'
                : 'Confirmed ledger history.'}
          </p>
          <details className="target-hour-details" open={mobile}>
            <summary>Hourly map · {data.hourly.length} hour slots</summary>
            <div className="target-legend">
              {data.targetUsdPerHour == null ? (
                <span>Complete</span>
              ) : (
                <>
                  <span className="met">Reached</span>
                  <span className="below">Below</span>
                </>
              )}
              <span className="unknown">Unknown</span>
              <span className="partial">Partial / settling</span>
            </div>
            <div className="target-heatmap">
              {[...days].map(([day, hours]) => (
                <div className="target-day" key={day}>
                  <span>{day}</span>
                  <div>
                    {hours.map((h) => (
                      <button
                        key={h.at}
                        type="button"
                        className={`target-cell ${h.status} ${h.at === picked?.at ? 'selected' : ''}`}
                        onClick={() => setSelected(h.at)}
                        aria-pressed={h.at === picked?.at}
                        aria-label={`${date(h.at)}, ${money(h.usd)}, ${statuses[h.status]}`}
                        title={`${date(h.at)} · ${money(h.usd)} · ${statuses[h.status]}`}
                      >
                        {new Date(h.at * 1000)
                          .getHours()
                          .toString()
                          .padStart(2, '0')}
                      </button>
                    ))}
                  </div>
                </div>
              ))}
            </div>
            {picked && (
              <div className="target-hour-readout" aria-live="polite">
                <strong>
                  {date(picked.at)} –{' '}
                  {new Date(picked.end * 1000).toLocaleTimeString([], {
                    hour: 'numeric',
                    minute: '2-digit',
                  })}
                </strong>
                <span>
                  {money(picked.usd)} recorded · {statuses[picked.status]}
                </span>
                {picked.status === 'unknown' && (
                  <small>Missing coverage is not zero earnings.</small>
                )}
                {picked.status === 'partial' && (
                  <small>
                    Only {date(picked.from)} – {date(picked.to)} is in this
                    range. Excluded from full-hour results.
                  </small>
                )}
              </div>
            )}
            {data.chartTruncated && (
              <p className="footnote">
                The map shows the last 31 days of this selection. Totals cover
                the entire selected history; use custom dates to inspect an
                earlier period.
              </p>
            )}
          </details>
          <p className="footnote">
            Clock time includes switching and quiet periods; it is not divided
            by warm time. {data.switchCount} recorded switches/recoveries ·{' '}
            {num(data.switchSeconds / 60, 1)} min elapsed, included in the
            period. Full hours settle after two minutes.{' '}
            {data.includesBase ? 'Base rewards are account-level.' : ''} Local
            time · {date(data.from)} to {date(data.to)}.
          </p>
        </>
      )}
    </section>
  );
}
