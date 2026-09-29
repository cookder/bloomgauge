'use client';
import { useEffect, useRef, useState } from 'react';
import { startChartPolling } from '@/lib/chart-polling';
import { usePageVisible } from '@/lib/use-page-visibility';
import {
  dailyBounds,
  earningsTiers,
  earningsTone,
  earningsToneLabel,
  validDailyEarnings,
  type DailyEarnings,
} from '@/lib/daily-earnings';
import { dayOutlook } from '@/lib/day-outlook';
import type { ModelProjection } from '@/lib/cumulative-earnings';
import { shareOutlook, sharedOutlook } from './daily-earnings';
import { money } from './shared';

/** The full tile: the Overview's "Your earning days" card while it is shown (the layout
 * editor keeps hidden cards in the page with `hidden`). */
export const dailyWidgetSelector = '[data-dashboard-widget="daily"]:not([hidden])';
const SHARED_SECONDS = 25;

/** Today's end-of-day estimate on the Pulse: the same reading (the last 30 days, all models),
 * function (dayOutlook), colors and `connected` as "Your earning days"; one tap opens it. */
export function PulseDayOutlook({
  active,
  paused,
  connected,
  projection,
}: {
  active: boolean;
  paused: boolean;
  connected: boolean;
  projection?: ModelProjection;
}) {
  const visible = usePageVisible();
  const [history, setHistory] = useState<DailyEarnings | null>(null);
  const [failed, setFailed] = useState(false);
  const [linked, setLinked] = useState(false);
  const flash = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  useEffect(() => {
    if (!active || !visible || (paused && history)) return;
    return startChartPolling({
      load: async (signal) => {
        // The daily card's reading when it is fresh: one request for both.
        const shared = sharedOutlook;
        if (
          shared &&
          shared.timezone === timezone &&
          Date.now() - shared.at < SHARED_SECONDS * 1000
        )
          return shared.value;
        const { start, end } = dailyBounds({ preset: '30d' }, new Date());
        const params = new URLSearchParams({
          from: String(start),
          to: String(end),
          timezone,
        });
        const res = await fetch('/api/earnings-daily?' + params, {
          signal,
          cache: 'no-store',
        });
        if (!res.ok) throw Error('Daily earnings could not be refreshed.');
        const value: unknown = await res.json();
        if (!validDailyEarnings(value))
          throw Error('Daily earnings returned incomplete data.');
        shareOutlook(timezone, value);
        return value;
      },
      onValue: (value) => {
        setHistory(value);
        setFailed(false);
      },
      onError: () => setFailed(true),
      intervalMs: 20000,
      repeat: !paused,
    });
  }, [active, visible, paused, timezone]);
  useEffect(() => {
    setLinked(!!document.querySelector(dailyWidgetSelector));
  });
  useEffect(() => () => clearTimeout(flash.current), []);
  const outlook = dayOutlook(
    history,
    projection,
    Date.now() / 1000,
    connected && !failed,
    paused,
  );
  const tiers = earningsTiers(history?.days);
  const tone = earningsTone(outlook.total, 24, tiers);
  const ready = outlook.status === 'ready';
  const open = () => {
    const card = document.querySelector<HTMLElement>(dailyWidgetSelector);
    if (!card) return;
    card.scrollIntoView({ behavior: 'smooth', block: 'start' });
    if (!card.hasAttribute('tabindex')) card.setAttribute('tabindex', '-1');
    card.focus({ preventScroll: true });
    clearTimeout(flash.current);
    card.classList.remove('widget-flash');
    void card.offsetWidth; // restart the outline on a second tap
    card.classList.add('widget-flash');
    flash.current = setTimeout(() => card.classList.remove('widget-flash'), 1600);
  };
  const body = (
    <>
      <span className="pulse-day-outlook-label">Today · end of day</span>
      <strong className="earnings-tone" data-tone={tone}>
        {ready ? `≈${money(outlook.total)}` : '—'}
      </strong>
      <small className="earnings-tone" data-tone={tone}>
        {ready
          ? tiers
            ? `${earningsToneLabel[tone]} day`
            : 'Not rated yet'
          : outlook.status === 'learning'
            ? 'Learning'
            : 'Awaiting data'}
        {linked && <span aria-hidden="true"> →</span>}
      </small>
    </>
  );
  return linked ? (
    <button
      type="button"
      className="pulse-day-outlook"
      data-tone={tone}
      onClick={open}
      title="Open Your earning days"
    >
      {body}
      <span className="sr-only"> Open Your earning days.</span>
    </button>
  ) : (
    <div
      className="pulse-day-outlook"
      data-tone={tone}
      role="group"
      aria-label="Today's end-of-day estimate"
    >
      {body}
    </div>
  );
}
