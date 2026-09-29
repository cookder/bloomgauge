'use client';
import { useEffect, useState } from 'react';
import { CalendarDays } from 'lucide-react';
import { startChartPolling } from '@/lib/chart-polling';
import { usePageVisible } from '@/lib/use-page-visibility';
import {
  dailyBounds,
  dayTone,
  earningsTiers,
  earningsTone,
  earningsToneLabel,
  validDailyEarnings,
  TIER_MIN_DAYS,
  type DailyEarnings,
  type EarningsTiers,
} from '@/lib/daily-earnings';
import { dayOutlook } from '@/lib/day-outlook';
import type { ModelProjection } from '@/lib/cumulative-earnings';
import { useScreenActive } from './app-navigation';
import { Choice, RangePicker, money, shortModel, type Range } from './shared';
const labels = {
  complete: 'Complete day',
  today: 'Today · still earning',
  partial: 'Partial date selection',
  settling: 'Credits settling',
  unknown: 'Incomplete coverage',
};
// This Mac's day scale (all earnings, last 30 days) for the hour-end estimates,
// shared by every caller and refreshed at most every 10 minutes.
let tierCache: { at: number; tiers: EarningsTiers | null } | null = null;
/** The latest all-models 30-day reading (today's outlook), shared with the Pulse's tile so
 * the two don't both poll /api/earnings-daily when both are on screen. */
export let sharedOutlook: {
  at: number;
  timezone: string;
  value: DailyEarnings;
} | null = null;
export function shareOutlook(timezone: string, value: DailyEarnings) {
  sharedOutlook = { at: Date.now(), timezone, value };
}
let tierRequest: Promise<void> | null = null;
function refreshTiers() {
  if (tierCache && Date.now() - tierCache.at < 600000) return Promise.resolve();
  return (tierRequest ??= (async () => {
    const { start, end } = dailyBounds({ preset: '30d' });
    const params = new URLSearchParams({
      from: String(start),
      to: String(end),
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    });
    try {
      const res = await fetch('/api/earnings-daily?' + params, {
        cache: 'no-store',
      });
      const value: unknown = res.ok ? await res.json() : null;
      if (validDailyEarnings(value))
        tierCache = { at: Date.now(), tiers: earningsTiers(value.days) };
    } catch {}
  })().finally(() => {
    tierRequest = null;
  }));
}
export function useEarningsTiers(active = true) {
  const visible = usePageVisible();
  const [tiers, setTiers] = useState(tierCache?.tiers ?? null);
  useEffect(() => {
    if (!active || !visible) return;
    let stopped = false;
    const load = () =>
      refreshTiers().then(() => {
        if (!stopped) setTiers(tierCache?.tiers ?? null);
      });
    load();
    const timer = setInterval(load, 600000);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [active, visible]);
  return tiers;
}
const dateLabel = (date: string) =>
  new Date(date + 'T12:00:00').toLocaleDateString([], {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
  });
export function DailyEarningsPanel({
  paused,
  projection,
  connected = true,
}: {
  paused: boolean;
  projection?: ModelProjection;
  connected?: boolean;
}) {
  const [range, setRange] = useState<Range>({ preset: '30d' }),
    [model, setModel] = useState('');
  const [saved, setSaved] = useState<{
    key: string;
    data: DailyEarnings | null;
    outlook: DailyEarnings | null;
    error: string;
    outlookError: string;
  } | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const active = useScreenActive(),
    visible = usePageVisible();
  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const key = JSON.stringify([range, model, timezone]);
  const data = saved?.key === key ? saved.data : null,
    error = saved?.key === key ? saved.error : '';
  useEffect(() => {
    if (!active || !visible || (paused && data)) return;
    return startChartPolling({
      load: async (signal) => {
        const now = new Date(),
          bounds = dailyBounds(range, now),
          outlookBounds = dailyBounds({ preset: '30d' }, now);
        const load = async ({ start, end }: { start: number; end: number }) => {
          const params = new URLSearchParams({
            from: String(start),
            to: String(end),
            timezone,
          });
          if (model) params.set('model', model);
          const res = await fetch('/api/earnings-daily?' + params, {
            signal,
            cache: 'no-store',
          });
          if (!res.ok)
            throw Error('Daily earnings could not be refreshed. Retrying.');
          const value: unknown = await res.json();
          if (!validDailyEarnings(value))
            throw Error('Daily earnings returned incomplete data. Retrying.');
          return value;
        };
        // A past/custom chart selection must not become the confirmed starting
        // amount for today's outlook. Reuse the usual 30-day request when equal.
        const chart = load(bounds);
        const outlook =
          bounds.start === outlookBounds.start &&
          bounds.end === outlookBounds.end
            ? chart
            : load(outlookBounds);
        const [a, b] = await Promise.allSettled([chart, outlook]);
        if (a.status === 'rejected') throw a.reason;
        return {
          data: a.value,
          outlook: b.status === 'fulfilled' ? b.value : null,
          outlookError:
            b.status === 'rejected'
              ? 'Day-end outlook could not be refreshed. Retrying.'
              : '',
        };
      },
      onValue: (value) => {
        if (value.outlook && !model) shareOutlook(timezone, value.outlook);
        setSaved({ key, ...value, error: '' });
      },
      onError: (e) =>
        setSaved((old) => ({
          key,
          data: old?.key === key ? old.data : null,
          outlook: old?.key === key ? old.outlook : null,
          outlookError: '',
          error: e.message,
        })),
      intervalMs: 20000,
      repeat: !paused,
    });
  }, [key, active, visible, paused]);
  const days = (data?.days ?? []).filter(
    (d) => data?.historyStart == null || d.end > data.historyStart,
  );
  const picked =
    data?.days.find((d) => d.date === selected) ?? data?.days.at(-1);
  const avg = data?.averageDayUsd;
  const delta =
    picked?.status === 'complete' && avg != null && avg > 0
      ? (picked.usd / avg - 1) * 100
      : null;
  const models = [
    ...new Set([
      ...(data?.models ?? saved?.data?.models ?? []),
      ...(model && model !== '@inference' ? [model] : []),
    ]),
  ];
  const padding = days.length
    ? (new Date(days[0].date + 'T12:00:00').getDay() + 6) % 7
    : 0;
  const outlook = dayOutlook(
    saved?.key === key ? saved.outlook : null,
    projection,
    Date.now() / 1000,
    connected && !error,
    paused,
  );
  // Colours compare days with this Mac's own last 30 complete days (same filter).
  const tiers = earningsTiers(
    (saved?.key === key ? saved.outlook : null)?.days ?? data?.days,
  );
  const outlookTone = earningsTone(outlook.total, 24, tiers);
  return (
    <section className="panel daily-earnings-panel" aria-label="Daily earnings">
      <div className="panel-heading">
        <div>
          <div className="eyebrow">YOUR EARNING DAYS</div>
          <h2>A little every day.</h2>
        </div>
        <CalendarDays size={21} />
      </div>
      <div className="daily-toolbar">
        <RangePicker
          value={range}
          onChange={setRange}
          label="Daily earnings range"
          presets={['7d', '30d', '90d', 'all', 'custom']}
        />
        <Choice
          value={model}
          onChange={setModel}
          label="Daily earnings model"
          options={[
            { value: '', label: 'All earnings' },
            { value: '@inference', label: 'Inference only' },
            ...models.map((m) => ({ value: m, label: shortModel(m) })),
          ]}
        />
      </div>
      {error && (
        <p className="notice" role="alert">
          {error} {data ? 'Showing saved confirmed credits.' : ''}
        </p>
      )}
      {!data ? (
        <p className="muted">
          {error
            ? 'No saved readings for this range.'
            : 'Loading confirmed daily earnings…'}
        </p>
      ) : (
        <>
          <div className="daily-summary">
            <strong>
              {money(data.usd)}
              <small> recorded in range</small>
            </strong>
            <span>
              {data.completeDays} complete days
              {avg != null ? ` · ${money(avg)} / day average` : ''}
            </span>
          </div>
          <div
            className="daily-calendar"
            tabIndex={days.length > 35 ? 0 : undefined}
            aria-label="Earnings calendar"
          >
            <div className="daily-weekdays" aria-hidden="true">
              {['M', 'T', 'W', 'T', 'F', 'S', 'S'].map((day, i) => (
                <span key={i}>{day}</span>
              ))}
            </div>
            <div className="daily-cells">
              {Array.from({ length: padding }, (_, i) => (
                <span key={'pad' + i} />
              ))}
              {days.map((day) => (
                <button
                  type="button"
                  key={day.date}
                  className={`daily-cell ${dayTone(day, tiers)} ${day.status !== 'complete' ? 'partial' : ''}`}
                  aria-pressed={picked?.date === day.date}
                  onClick={() => setSelected(day.date)}
                  aria-label={`${dateLabel(day.date)}, ${money(day.usd)} recorded, ${labels[day.status]}${day.status === 'unknown' ? ', unknown earnings coverage' : tiers ? `, ${earningsToneLabel[dayTone(day, tiers)]} day` : ''}`}
                >
                  <span>
                    {Number(day.date.slice(8)) === 1 || day === days[0]
                      ? new Date(day.date + 'T12:00:00').toLocaleDateString(
                          [],
                          { month: 'short', day: 'numeric' },
                        )
                      : Number(day.date.slice(8))}
                  </span>
                  <strong>
                    {day.status === 'unknown' && day.usd === 0
                      ? '—'
                      : money(day.usd)}
                  </strong>
                  {day.status !== 'complete' && <i aria-hidden="true" />}
                </button>
              ))}
            </div>
          </div>
          {picked && (
            <div className="daily-readout" aria-live="polite">
              <div>
                <strong>{dateLabel(picked.date)}</strong>
                <span>{labels[picked.status]}</span>
              </div>
              <div>
                <strong
                  className="earnings-tone"
                  data-tone={dayTone(picked, tiers)}
                >
                  {money(picked.usd)}
                </strong>
                <span>
                  {delta != null
                    ? `${Math.abs(delta).toFixed(0)}% ${delta >= 0 ? 'above' : 'below'} this range’s daily average`
                    : picked.status === 'complete'
                      ? 'At least 3 complete days needed for an average'
                      : 'Actual credits so far · no extrapolation'}
                </span>
              </div>
              <p>
                {money(picked.inferenceUsd)} inference
                {data.includesBase
                  ? ` + ${money(picked.baseUsd)} account base rewards`
                  : ''}
                .{' '}
                {picked.status === 'unknown'
                  ? 'Missing coverage is not zero earnings.'
                  : ''}
              </p>
            </div>
          )}
          <div className="daily-legend">
            {tiers ? (
              <>
                <span className="quiet">Under {money(tiers.steady)}</span>
                <span className="steady">{money(tiers.steady)}+ steady</span>
                <span className="green">{money(tiers.green)}+ good</span>
                <span className="purple">{money(tiers.purple)}+ great</span>
                <span className="gold">{money(tiers.gold)}+ outstanding</span>
              </>
            ) : (
              <span className="quiet">
                Colours start after {TIER_MIN_DAYS} complete days
              </span>
            )}
            <span className="unknown">Missing</span>
          </div>
          {tiers && (
            <p className="footnote">
              Compared with this Mac’s last {tiers.days} complete days.
            </p>
          )}
          <div
            className="daily-outlook"
            data-tone={outlookTone}
            aria-label="Today's end-of-day estimate"
          >
            <div className="daily-outlook-heading">
              <div>
                <span className="eyebrow">TODAY’S OUTLOOK</span>
                <h3>End-of-day estimate</h3>
              </div>
              <span
                className="daily-outlook-rating earnings-tone"
                data-tone={outlookTone}
              >
                {outlook.status === 'ready'
                  ? tiers
                    ? `${earningsToneLabel[outlookTone]} day`
                    : 'Not rated yet'
                  : outlook.status === 'learning'
                    ? 'Learning'
                    : 'Awaiting data'}
              </span>
            </div>
            {outlook.status === 'ready' ? (
              <>
                <strong
                  className="daily-outlook-total earnings-tone"
                  data-tone={outlookTone}
                >
                  ≈{money(outlook.total)}
                </strong>
                <p className="daily-outlook-split">
                  {money(outlook.confirmed)} confirmed{' '}
                  <span>
                    + ≈
                    {money(
                      (outlook.additionalInference ?? 0) +
                        (outlook.additionalBase ?? 0),
                    )}{' '}
                    expected
                  </span>
                </p>
                <p className="daily-outlook-basis">
                  {outlook.models.map(shortModel).join(' + ')} ·{' '}
                  {outlook.hours.toFixed(1)} warm hours across {outlook.days}{' '}
                  {outlook.days === 1 ? 'day' : 'days'}
                </p>
                <details>
                  <summary>
                    {outlook.hours < 4 || outlook.days < 3
                      ? 'Early estimate · how it works'
                      : 'How this is estimated'}
                  </summary>
                  <p>
                    {outlook.detail}{' '}
                    {outlook.includesBase
                      ? outlook.baseEstimated
                        ? `Account base rewards use ${outlook.baseDays} recent complete days, minus rewards already credited today.`
                        : 'Future base rewards are excluded until at least 3 complete days are available; base rewards already credited are included.'
                      : 'Base rewards are excluded by your filter.'}{' '}
                    Today’s total follows the model filter, independently of the
                    calendar date selection. This is an estimate, not guaranteed
                    earnings.
                  </p>
                </details>
                {outlook.includesBase && !outlook.baseEstimated && (
                  <p className="daily-outlook-caveat">
                    Future base rewards not yet estimated.
                  </p>
                )}
              </>
            ) : (
              <p className="daily-outlook-unavailable">
                {(saved?.key === key && saved.outlookError) || outlook.detail}
              </p>
            )}
            <span className="daily-outlook-time">
              {paused ? 'View paused · ' : ''}Through local midnight ·{' '}
              {timezone.replaceAll('_', ' ')}
            </span>
          </div>
          <p className="footnote">
            {data.scope}.{' '}
            {data.historyStart != null
              ? `History begins ${new Date(data.historyStart * 1000).toLocaleDateString([], { month: 'short', day: 'numeric' })}. `
              : 'No recorded history yet. '}
            Calendar colors use confirmed credits; a dot marks a partial or
            unfinished day. {timezone.replaceAll('_', ' ')} ·{' '}
            {paused
              ? 'view paused'
              : error || Date.now() / 1000 - data.at > 90
                ? 'saved readings'
                : 'confirmed credits, refreshed every 20s'}
            .{' '}
            {data.chartTruncated
              ? 'Calendar shows the latest 366 days; totals cover your full selection. Choose custom dates for earlier days.'
              : ''}
          </p>
        </>
      )}
    </section>
  );
}
