'use client';
import { useEffect, useMemo, useState } from 'react';
import { useAppNavigation, useScreenActive } from './app-navigation';
import { usePageVisible } from '@/lib/use-page-visibility';
import { useStudy } from './model-research';
import { Choice, num, RangePicker, type Range } from './shared';
import {
  busiestCell,
  currentWeekHour,
  heatLevel,
  validWeeklyNetwork,
  type WeeklyNetwork,
  type WeeklyMetric,
} from '@/lib/network-weekly';

const days = [
  'Monday',
  'Tuesday',
  'Wednesday',
  'Thursday',
  'Friday',
  'Saturday',
  'Sunday',
];
const hour = (h: number) => `${h % 12 || 12}${h % 24 < 12 ? 'am' : 'pm'}`;
const period = (h: number) => `${hour(h)}–${hour((h + 1) % 24)}`;
const compact = (v: number) =>
  new Intl.NumberFormat('en-US', {
    notation: 'compact',
    maximumFractionDigits: 1,
  }).format(v);
const stamp = (at: number | null, zone: string) =>
  at === null
    ? 'No hourly history yet'
    : new Date(at * 1000).toLocaleString('en-US', {
        timeZone: zone,
        month: 'short',
        day: 'numeric',
        hour: 'numeric',
        minute: '2-digit',
      });
const readClock = () => ({
  at: Date.now(),
  zone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC',
});

export function WeeklyTraffic({ paused }: { paused: boolean }) {
  const [range, setRange] = useState<Range>({ preset: '30d' });
  const [metric, setMetric] = useState<WeeklyMetric>('requestsPerMinute');
  const [selection, setSelection] = useState<number | null>(null);
  const [part, setPart] = useState<number | null>(null);
  const [clock, setClock] = useState(readClock);
  const active = useScreenActive(),
    visible = usePageVisible();
  useEffect(() => {
    if (!active || !visible) return;
    let timer: ReturnType<typeof setTimeout>;
    const refresh = () => {
      clearTimeout(timer);
      setClock(readClock());
      timer = setTimeout(refresh, 60000 - (Date.now() % 60000) + 50);
    };
    refresh();
    window.addEventListener('focus', refresh);
    window.addEventListener('pageshow', refresh);
    return () => {
      clearTimeout(timer);
      window.removeEventListener('focus', refresh);
      window.removeEventListener('pageshow', refresh);
    };
  }, [active, visible]);
  // Returning to this screen starts at now; tapping cells remains an explicit inspection.
  useEffect(() => {
    if (active) {
      setSelection(null);
      setPart(null);
    }
  }, [active]);
  const current = useMemo(() => currentWeekHour(clock.at, clock.zone), [clock]);
  const { mobile } = useAppNavigation();
  const { data, error } = useStudy<WeeklyNetwork>(
    `/api/network/weekly?timezone=${encodeURIComponent(clock.zone)}`,
    range,
    paused,
    (d) => validWeeklyNetwork(d) && d.timezone === clock.zone,
    300000,
  );
  const best = busiestCell(data?.cells ?? [], metric);
  const selected = data?.cells[selection ?? current.index];
  const max = best?.[metric] ?? 0;
  const firstHour = mobile
    ? (part ?? Math.floor((selected?.hour ?? 9) / 6)) * 6
    : 0;
  const hours = Array.from(
    { length: mobile ? 6 : 24 },
    (_, i) => firstHour + i,
  );
  const unit =
    metric === 'requestsPerMinute' ? 'requests / min' : 'output tokens / sec';
  const selectionDates =
    selected?.dates.filter((d) => d[metric] !== null) ?? [];
  const min = selectionDates.length
    ? Math.min(...selectionDates.map((d) => d[metric]!))
    : null;
  const high = selectionDates.length
    ? Math.max(...selectionDates.map((d) => d[metric]!))
    : null;
  const dateCount = best?.days ?? 0;
  return (
    <section
      className="panel weekly-traffic"
      aria-label="Weekly network traffic"
    >
      <div className="panel-heading">
        <div>
          <div className="eyebrow">ACROSS THE WEEK</div>
          <h2>When is the network busiest?</h2>
        </div>
        <span className="status-pill" title={clock.zone}>
          Local time
        </span>
      </div>
      <div className="chart-toolbar">
        <Choice
          label="Weekly traffic metric"
          value={metric}
          onChange={(v) => setMetric(v as WeeklyMetric)}
          options={[
            { value: 'requestsPerMinute', label: 'Requests / minute' },
            { value: 'tokensPerSecond', label: 'Output tokens / second' },
          ]}
        />
        <RangePicker
          label="Weekly traffic date range"
          value={range}
          onChange={setRange}
        />
      </div>
      {error && (
        <p className="notice" role="status">
          {error} {data ? 'Showing the last loaded pattern.' : ''}{' '}
          {paused ? 'Resume to retry.' : 'Retrying automatically.'}
        </p>
      )}
      {!data && !error && (
        <p className="muted" role="status">
          Reading saved network history…
        </p>
      )}
      {data && (
        <>
          <div className="weekly-lead">
            <span>Highest observed hour</span>
            <strong>
              {best
                ? `${days[best.day]} · ${period(best.hour)}`
                : 'Not enough covered hours yet'}
            </strong>
            <small>
              {best
                ? `${num(max)} ${unit} · ${dateCount} recorded ${dateCount === 1 ? 'date' : 'dates'}`
                : 'Hours need at least 80% recorded coverage.'}
            </small>
          </div>
          <p className="footnote weekly-caution">
            {data.qualifiedDates < 28 ? 'Early pattern' : 'Observed history'} ·{' '}
            {num(data.observedHours / 24, 1)} days of hourly coverage. Each cell
            averages recorded traffic for that weekday and hour; bursts and
            network growth can outweigh a weekly rhythm.
          </p>
          <div className="weekly-now">
            <span>
              <i aria-hidden="true" /> Now · {days[current.day].slice(0, 3)}{' '}
              {period(current.hour)}{' '}
              <small>{clock.zone.replaceAll('_', ' ')}</small>
            </span>
            <button
              type="button"
              onClick={() => {
                setClock(readClock());
                setSelection(null);
                setPart(null);
              }}
            >
              Current hour
            </button>
          </div>
          {mobile && (
            <div
              className="weekly-parts"
              role="group"
              aria-label="Heatmap time of day"
            >
              {['12am–6am', '6am–12pm', '12pm–6pm', '6pm–12am'].map(
                (label, i) => (
                  <button
                    key={label}
                    type="button"
                    aria-pressed={firstHour === i * 6}
                    onClick={() => {
                      setPart(i);
                      setSelection((selected?.day ?? 0) * 24 + i * 6);
                    }}
                  >
                    {label}
                  </button>
                ),
              )}
            </div>
          )}
          <div
            className="weekly-grid"
            style={{
              gridTemplateColumns: `38px repeat(${hours.length},minmax(0,1fr))`,
            }}
            role="group"
            aria-label={`Traffic heatmap in ${clock.zone}, ${unit}. Select an hour for values and coverage.`}
          >
            <span className="weekly-corner">Day</span>
            {hours.map((h) => (
              <span className="weekly-hour" key={h}>
                {mobile ? hour(h) : h % 3 === 0 ? hour(h) : ''}
              </span>
            ))}
            {days.map((name, day) => (
              <div className="weekly-row" key={name}>
                <span className="weekly-day">{name.slice(0, 3)}</span>
                {hours.map((h) => {
                  const c = data.cells[day * 24 + h],
                    value = c[metric],
                    level = heatLevel(value, max),
                    chosen = selected?.day === day && selected.hour === h,
                    now = day === current.day && h === current.hour;
                  const label = `${name}, ${period(h)} ${clock.zone}${now ? ', current hour' : ''}: ${value === null ? 'insufficient coverage' : `${num(value)} ${unit}, ${c.days} recorded dates`}`;
                  return (
                    <button
                      key={h}
                      type="button"
                      className={`weekly-cell heat-${level ?? 'missing'}`}
                      aria-label={label}
                      title={label}
                      aria-current={now ? 'time' : undefined}
                      aria-pressed={chosen}
                      onClick={() => setSelection(day * 24 + h)}
                    >
                      {now && (
                        <small className="weekly-now-marker" aria-hidden="true">
                          Now
                        </small>
                      )}
                      <span>
                        {mobile ? (value === null ? '—' : compact(value)) : ''}
                      </span>
                      {c.days === 1 && value !== null && (
                        <i aria-hidden="true" />
                      )}
                    </button>
                  );
                })}
              </div>
            ))}
          </div>
          <div className="weekly-legend">
            <span>Quiet</span>
            <div className="weekly-scale" aria-hidden="true">
              {[0, 1, 2, 3, 4, 5].map((n) => (
                <i key={n} className={`heat-${n}`} />
              ))}
            </div>
            <span>{compact(max)} · busiest</span>
            <span>
              <i className="weekly-missing-key" /> Unknown
            </span>
            <span>• One date</span>
          </div>
          <div className="weekly-selection" aria-live="polite">
            <div>
              <strong>
                {selected
                  ? `${days[selected.day]} · ${period(selected.hour)}`
                  : 'Select an hour'}
              </strong>
              <span>
                {selected?.[metric] != null
                  ? `${num(selected[metric])} ${unit}`
                  : 'Not enough covered data'}
              </span>
            </div>
            <p>
              {selected
                ? `${selected.days} of ${selected.possibleDays} elapsed date-hours qualify · ${num(selected.expectedSeconds ? (selected.observedSeconds / selected.expectedSeconds) * 100 : 0)}% recorded coverage.`
                : ''}{' '}
              {min !== null && high !== null && selectionDates.length > 1
                ? `Across dates: ${num(min)}–${num(high)} ${unit}.`
                : ''}
            </p>
            {!!selected?.dates.length && (
              <details>
                <summary>See the individual dates</summary>
                <ul>
                  {selected.dates.map((d) => (
                    <li key={d.date}>
                      <span>{d.date}</span>
                      <strong>
                        {d[metric] === null
                          ? 'Insufficient coverage'
                          : `${num(d[metric])} ${unit}`}
                      </strong>
                      <small>{num(d.coverage * 100)}% recorded</small>
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </div>
          <details className="weekly-method">
            <summary>Coverage & how to read this</summary>
            <p className="footnote">
              Whole Darkbloom network, from the saved official traffic series.
              These are recorded request and output-token rates, not
              active/queued concurrency or this Mac’s earnings. Hours with no
              readings stay unknown; observed zero traffic stays zero.
            </p>
            <p className="footnote">
              Hourly history starts{' '}
              {stamp(data.hourlyHistoryStart, data.timezone)}. This comparison
              covers {stamp(data.coverageStart, data.timezone)} through{' '}
              {stamp(data.coverageEnd, data.timezone)} ({data.timezone}). The
              current unfinished hour and partial range-boundary hours are
              excluded. Each date-hour needs at least 80% coverage. Averages are
              weighted by recorded seconds, so more samples or more repeat
              weekdays do not inflate the rate.
            </p>
            <p className="footnote">
              {num(data.coarseBuckets)} buckets longer than an hour were
              excluded; four-hour totals cannot reveal individual hours.{' '}
              {num(data.boundaryBuckets)} boundary buckets and{' '}
              {num(data.invalidBuckets)} invalid/overlapping buckets excluded.
              Repeated daylight-saving hours use their actual duration.
              Refreshes every five minutes while visible.
            </p>
          </details>
        </>
      )}
    </section>
  );
}
