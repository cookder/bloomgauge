'use client';
import { pausedReason } from '@/lib/optimizer-manager';
import { useEffect, useId, useRef, useState, type CSSProperties } from 'react';
import { Activity, ArrowLeftRight, ChevronDown, Sparkles } from 'lucide-react';
import { Input } from '@/components/ui/input';
import { Choice, age, money, num, shortModel } from './shared';
import { TrafficPulse } from './traffic-pulse';
import { PulseHourlyBars } from './pulse-hourly-bars';
import { PulseDayOutlook } from './pulse-day-outlook';
import type { Monitor } from './widgets';
import { useAppNavigation } from './app-navigation';
import { PulseRunIndicator } from './pulse-run-indicator';
import type { RunSession } from '@/lib/pulse-run-status';
import type { TrafficPulseData } from '@/lib/traffic-pulse';
import type { Forecast } from './forecast';
import { modelColor } from '@/lib/model-earnings';
import { usePageVisible } from '@/lib/use-page-visibility';
import { earningsTone, earningsToneLabel } from '@/lib/daily-earnings';
import { useEarningsTiers } from './daily-earnings';
import { creditSortOptions, sortedPulseCredits } from '@/lib/credit-history';
import {
  creditArrivals,
  creditBursts,
  paceCents,
  paceUnitKey,
  pulseComparison,
  pulseDial,
  pulseBenchmarkView,
  pulseDemandView,
  pulseReference,
  readPaceUnit,
  type EarningsPulseData,
  type PaceUnit,
  type PulseCursor,
} from '@/lib/earnings-pulse';

export function EarningsPulse({
  pulse,
  traffic,
  paused,
  active: viewActive = true,
  connected,
  outlookConnected = connected,
  forecast,
  earningsUpdatedAt,
  earningsStatus,
  session,
  monitor,
  at,
}: {
  pulse?: EarningsPulseData;
  traffic?: TrafficPulseData;
  paused: boolean;
  active?: boolean;
  connected: boolean;
  /** Your earning days' own rule, so today's estimate reads the same in both places. */
  outlookConnected?: boolean;
  forecast?: Forecast;
  earningsUpdatedAt?: number | null;
  earningsStatus?: string;
  session?: RunSession | null;
  monitor?: Monitor;
  at?: number;
}) {
  const navigation = useAppNavigation();
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [creditsOpen, setCreditsOpen] = useState(false);
  const [creditSort, setCreditSort] = useState('newest');
  const [creditLimit, setCreditLimit] = useState('5');
  const detailsId = useId();
  // BloomGauge's pace everywhere is the 5-minute confirmed window; 60 s is an optional instant view.
  const [windowSeconds, setWindowSeconds] = useState('300');
  const [comparisonMode, setComparisonMode] = useState('history');
  const [referenceUsd, setReferenceUsd] = useState('');
  const [paceUnit, setPaceUnit] = useState<PaceUnit>(() => {
    try {
      return readPaceUnit(localStorage.getItem(paceUnitKey));
    } catch {
      return 'cents';
    }
  });
  const choosePaceUnit = (value: string) => {
    const next = readPaceUnit(value);
    setPaceUnit(next);
    try {
      localStorage.setItem(paceUnitKey, next);
    } catch {}
  };
  const pace = (n: number | null | undefined) =>
    paceUnit === 'cents' ? paceCents(n) : money(n);
  const visible = usePageVisible();
  const [bursts, setBursts] = useState<ReturnType<typeof creditBursts>>([]);
  const cursor = useRef<PulseCursor | null>(null);
  useEffect(() => {
    if (!pulse) return;
    const active =
      viewActive && !paused && connected && visible && pulse.status === 'live';
    const next = creditArrivals(cursor.current, pulse, active);
    cursor.current = next.cursor;
    if (!active || next.reset) setBursts([]);
    else if (next.events.length) setBursts(creditBursts(next.events));
  }, [pulse, paused, connected, visible, viewActive]);
  useEffect(() => {
    if (!bursts.length) return;
    const timer = setTimeout(() => setBursts([]), 8000);
    return () => clearTimeout(timer);
  }, [bursts]);

  const window = pulse?.windows[windowSeconds];
  const active = connected && pulse?.status === 'live';
  const rate = active ? window?.ratePerHour : null;
  const historical = pulse?.baseline?.ratePerHour;
  const basis = pulseReference(comparisonMode, historical, referenceUsd);
  const reference = basis.value;
  const useHistory = basis.source === 'history';
  const historyScope = pulse?.baseline?.scope;
  const seasonal =
    !!historyScope &&
    ['weekday_hour', 'daytype_hour', 'hour'].includes(historyScope);
  const historyLabel =
    historyScope === 'weekday_hour'
      ? 'same day & time'
      : historyScope === 'daytype_hour'
        ? `${(pulse?.baseline?.localWeekday ?? 0) >= 5 ? 'weekend' : 'weekday'} & time`
        : historyScope === 'hour'
          ? 'same time of day'
          : 'all model hours';
  const comparison =
    reference == null ? null : pulseComparison(rate, reference);
  const dialKey = `${pulse?.streamId}:${pulse?.sessionId}:${reference}:${windowSeconds}`;
  const dialRange = useRef({ key: dialKey, scale: 4 });
  const dial = pulseDial(
    comparison?.ratio,
    dialRange.current.key === dialKey ? dialRange.current.scale : 4,
  );
  dialRange.current = { key: dialKey, scale: dial.scale };
  const color = dial.color;
  // No rating without a reference; never a routing hint from a typed reference.
  const rating =
    comparison || rate == null
      ? dial.label
      : comparisonMode !== 'history'
        ? 'Enter a reference'
        : historical == null
          ? 'Building a baseline'
          : 'No paid baseline yet';
  const demand = connected
    ? pulseDemandView(
        pulse?.demand,
        dial.scale,
        basis.routing ? comparison?.ratio : null,
        pulse?.at ?? 0,
      )
    : null;
  const peers = connected
    ? pulseBenchmarkView(pulse?.peers, pulse?.at ?? 0, pulse?.models ?? [])
    : null;
  const hourEstimateReady =
    connected &&
    forecast?.earnings.status === 'ready' &&
    Number.isFinite(forecast.earnings.projected);
  const tiers = useEarningsTiers(viewActive && hourEstimateReady);
  const hourTone = earningsTone(
    hourEstimateReady ? forecast?.earnings.projected : null,
    forecast ? (forecast.hourEnd - forecast.hourStart) / 3600 : 1,
    tiers,
  );
  const recent = sortedPulseCredits(pulse?.events ?? [], creditSort).slice(
    0,
    Number(creditLimit),
  );
  const state = paused
    ? 'View paused'
    : !connected || pulse?.status === 'stale'
      ? 'Waiting for fresh credits'
      : pulse?.status === 'offline'
        ? 'Session ended'
        : pulse?.status === 'paused'
          ? `Statistics paused · ${pausedReason(pulse.detail) ?? (pulse.reporting && !pulse.reporting.counting ? pausedReason(pulse.reporting.detail) : null) ?? 'model not ready'}`
          : pulse?.status === 'unmatched'
            ? 'Matching this Mac'
            : rate == null
              ? 'Building a pace reading'
              : rate === 0
                ? 'No credits in this window'
                : 'Credits flowing';
  return (
    <section
      className={`panel earnings-pulse-panel${detailsOpen ? ' pulse-expanded' : ''}`}
      aria-label="Live earnings pulse"
    >
      <div className="panel-heading">
        <div>
          <div className="eyebrow">EARNINGS / PULSE</div>
          <h2 className="pulse-desktop-copy">
            Watch the little credits add up.
          </h2>
        </div>
      </div>
      <button
        type="button"
        className="pulse-controller-link"
        onClick={() => navigation.navigate('switch')}
      >
        Model controls · Start, stop & choose models →
      </button>
      <PulseRunIndicator
        session={session}
        active={viewActive && visible}
        paused={paused}
        connected={connected}
        reporting={pulse?.reporting}
      />
      <div className="pulse-layout with-hourly">
        <div
          className="pulse-instrument"
          style={{ '--pulse-color': color } as CSSProperties}
        >
          <div
            className={`pulse-burst-layer ${paused || !viewActive || !visible ? 'motion-paused' : ''}`}
            aria-hidden="true"
          >
            {bursts.map((credit, index) => (
              <span
                key={`${credit.id}:${credit.receivedAt}`}
                className="pulse-credit-burst"
                style={
                  {
                    color:
                      credit.count > 1
                        ? 'var(--c-82efb5)'
                        : modelColor(credit.model),
                    '--delay': `${index * 0.35}s`,
                    '--lane': `${8 + (index % 3) * 22}%`,
                  } as CSSProperties
                }
              >
                +{money(credit.microUsd / 1e6)}
                {credit.count > 1 ? ` · ${credit.count} credits` : ''}
              </span>
            ))}
            {!!bursts.length && (
              <span className="pulse-reduced-credit">
                +
                {money(
                  bursts.reduce((total, credit) => total + credit.microUsd, 0) /
                    1e6,
                )}
              </span>
            )}
          </div>
          <div className="pulse-meter">
            <p
              className={`pulse-serving${pulse?.models.length ? '' : ' is-empty'}`}
              aria-live="polite"
            >
              <span className="pulse-serving-label">
                {pulse?.models.length === 2 ? 'Serving pair' : 'Serving'}
              </span>
              {pulse?.models.length ? (
                pulse.models.map((m) => (
                  <strong key={m}>
                    <i
                      style={{ background: modelColor(m) }}
                      aria-hidden="true"
                    />
                    {shortModel(m)}
                  </strong>
                ))
              ) : (
                <strong>{connected ? 'No model serving' : 'Checking…'}</strong>
              )}
            </p>
            <svg
              className="pulse-gauge"
              viewBox="0 0 300 172"
              role="img"
              aria-label={
                (rate == null
                  ? 'Earnings pace unavailable'
                  : `Confirmed earnings pace ${pace(rate)} per hour; ${comparison ? `${num(comparison.ratio * 100)} percent of reference` : rating.toLowerCase()}`) +
                (demand ? `. ${demand.label}` : '')
              }
            >
              {demand && (
                <>
                  <path
                    className="pulse-demand-track"
                    d="M 18 142 A 132 132 0 0 1 282 142"
                    pathLength="100"
                  />
                  {demand.fraction != null && (
                    <path
                      className="pulse-demand-value"
                      d="M 18 142 A 132 132 0 0 1 282 142"
                      pathLength="100"
                      strokeDasharray={`${demand.fraction * 100} 100`}
                    />
                  )}
                </>
              )}
              <path
                className="pulse-arc-track"
                d="M 32 142 A 118 118 0 0 1 268 142"
                pathLength="100"
              />
              <path
                className="pulse-arc-value"
                d="M 32 142 A 118 118 0 0 1 268 142"
                pathLength="100"
                strokeDasharray={`${dial.fraction * 100} 100`}
              />
              {Array.from({ length: 21 }, (_, i) => {
                const angle = Math.PI - (i / 20) * Math.PI;
                return (
                  <line
                    key={i}
                    x1={150 + Math.cos(angle) * 100}
                    y1={142 - Math.sin(angle) * 100}
                    x2={150 + Math.cos(angle) * (i % 5 === 0 ? 88 : 94)}
                    y2={142 - Math.sin(angle) * (i % 5 === 0 ? 88 : 94)}
                    className="pulse-tick"
                  />
                );
              })}
              <line
                x1="150"
                y1="142"
                x2="56"
                y2="142"
                className={`pulse-needle ${rate == null ? 'inactive' : ''}`}
                style={{
                  transform: `rotate(${dial.fraction * 180}deg)`,
                }}
              />
              <circle cx="150" cy="142" r="6" fill="currentColor" />
              <text x="30" y="167" textAnchor="middle">
                0
              </text>
              <text
                x="150"
                y="75"
                textAnchor="middle"
                className="pulse-reference-mark"
              >
                {num(dial.scale / 2)}×
              </text>
              <text x="267" y="167" textAnchor="middle">
                {num(dial.scale)}×
              </text>
            </svg>
            <button
              type="button"
              className="pulse-rate"
              onClick={() =>
                choosePaceUnit(paceUnit === 'cents' ? 'dollars' : 'cents')
              }
              title={`Show in ${paceUnit === 'cents' ? 'dollars' : 'cents'} per hour`}
              aria-label={`${rate == null ? 'Pace unavailable' : `${pace(rate)} per hour`}. Show in ${paceUnit === 'cents' ? 'dollars' : 'cents'}`}
            >
              <strong>{rate == null ? '—' : pace(rate)}</strong>
              <span>
                per hour
                <ArrowLeftRight size={14} aria-hidden="true" />
              </span>
            </button>
            <p className="pulse-intensity" style={{ color }}>
              {rating}
            </p>
            {demand && (
              <p
                className="pulse-demand"
                title="Requests per warm provider for this model across the network, compared with its average over the last 7 days. Outer arc; same scale as the needle."
              >
                <span className="pulse-demand-swatch" aria-hidden="true" />
                {demand.label}
                <small> · {demand.detail}</small>
              </p>
            )}
            {demand?.hint && (
              <p className="small muted pulse-demand-hint">{demand.hint}</p>
            )}
          </div>
          <div className="pulse-instrument-details">
            <p className="small muted pulse-reading-note">{state}</p>
            <p className="pulse-comparison">
              <span className="pulse-desktop-copy">
                {comparison
                  ? `${num(comparison.ratio, 2)}× ${useHistory ? (seasonal ? 'time-adjusted model average' : 'this model’s historical average') : 'reference pace'}`
                  : 'Confirmed-credit pace'}
              </span>
              <span className="pulse-mobile-copy">
                {comparison
                  ? `${num(comparison.ratio, 2)}× ${useHistory ? 'model average' : 'reference'}`
                  : 'Confirmed pace'}
              </span>
            </p>
            {useHistory && (
              <p
                className="small muted pulse-baseline-scope"
                title={pulse?.baseline?.detail}
              >
                {historyLabel} · {num(pulse?.baseline?.hours, 1)}h
                {seasonal ? ` / ${pulse?.baseline?.days ?? 0} days` : ''}
              </p>
            )}
            <p className="small muted pulse-desktop-copy">
              {windowSeconds === '60' ? '60-second' : '5-minute'} confirmed
              window
              {window && window.seconds < Number(windowSeconds)
                ? ` · ${num(window.seconds)} warm seconds covered`
                : ''}
              {window && window.seconds > 0
                ? ` · through ${new Date(window.end * 1000).toLocaleTimeString()}`
                : ''}
            </p>
            <p className="small muted pulse-mobile-copy">
              {windowSeconds === '60' ? '60s' : '5m'} window
              {window && window.seconds > 0
                ? ` · ${num(window.seconds)}s warm`
                : ''}
            </p>
            {peers && (
              <p
                className="small pulse-peers"
                data-tone={peers.tone}
                title={peers.title}
              >
                Macs like yours
                {peers.percentile && (
                  <>
                    : <strong>{peers.percentile}</strong>
                  </>
                )}{' '}
                · {peers.requests} ({peers.peers})
                {peers.usdPerHour != null && peers.peerUsdPerHour != null && (
                  <span className="muted">
                    {' '}
                    · ≈{pace(peers.usdPerHour)} vs {pace(peers.peerUsdPerHour)}
                    /h
                  </span>
                )}
              </p>
            )}
            <div
              className="pulse-session-total"
              role="button"
              tabIndex={0}
              aria-label="Recent earnings. Double-tap or double-click this total, or press Enter, to show or hide credits."
              aria-expanded={creditsOpen}
              aria-controls={`${detailsId}-feed`}
              title="Double-click or double-tap to see recent earnings"
              onDoubleClick={() => setCreditsOpen((open) => !open)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' || event.key === ' ') {
                  event.preventDefault();
                  setCreditsOpen((open) => !open);
                }
              }}
            >
              <span className="small muted">
                {pulse?.sessionId
                  ? `Session ${pulse.sessionId}`
                  : 'Current session'}{' '}
                <span className="pulse-desktop-copy">
                  · confirmed inference credits
                </span>
                <span className="pulse-mobile-copy"> · inference</span>
              </span>
              <strong
                key={`${pulse?.sessionId}:${pulse?.sessionMicroUsd}`}
                className={
                  !paused && viewActive && active && visible
                    ? 'pulse-money-tick'
                    : ''
                }
              >
                {money(
                  pulse?.sessionMicroUsd == null
                    ? null
                    : pulse.sessionMicroUsd / 1e6,
                  6,
                )}
              </strong>
              <span className="small muted">
                {pulse?.models.map(shortModel).join(' + ') ||
                  'Waiting for a verified model session'}
              </span>
            </div>
          </div>
        </div>
        <PulseHourlyBars
          monitor={monitor}
          forecast={forecast}
          at={at ?? pulse?.at ?? Date.now() / 1000}
          paused={paused}
          active={viewActive && visible}
          connected={connected}
          today={
            <PulseDayOutlook
              active={viewActive && visible}
              paused={paused}
              connected={outlookConnected}
              projection={forecast?.modelProjection}
            />
          }
        />
      </div>
      <div
        className="pulse-feed-details"
        id={`${detailsId}-feed`}
        hidden={!creditsOpen}
      >
        <div className="pulse-feed-title">
          <Sparkles size={15} />
          <span>Recent confirmed</span>
          <span className="muted">fractions of a cent</span>
        </div>
        <div className="chart-toolbar pulse-credit-options">
          <Choice
            label="Recent credit order"
            value={creditSort}
            onChange={setCreditSort}
            options={creditSortOptions.filter((o) => o.value !== 'tokens-desc')}
          />
          <Choice
            label="Recent credits shown"
            value={creditLimit}
            onChange={setCreditLimit}
            options={['5', '10', '20'].map((value) => ({
              value,
              label: `Show ${value}`,
            }))}
          />
        </div>
        <div className="pulse-credit-feed">
          {recent.map((credit) => (
            <div key={credit.id} className="pulse-feed-row">
              <i style={{ background: modelColor(credit.model) }} />
              <span>{shortModel(credit.model)}</span>
              <strong style={{ color: modelColor(credit.model) }}>
                {credit.microUsd >= 0 ? '+' : ''}
                {money(credit.microUsd / 1e6)}
              </strong>
            </div>
          ))}
          {!recent.length && (
            <p className="small muted">
              {active
                ? 'New paid credits will appear here as Darkbloom posts them.'
                : 'Waiting for fresh, matched credits from this Mac.'}
            </p>
          )}
        </div>
        <p className="footnote">
          This Mac · {pulse?.events.length ?? 0} recent matched credits.{' '}
          <button
            type="button"
            className="text-link"
            onClick={() => navigation.navigate('credits')}
          >
            Explore saved credit history →
          </button>
        </p>
      </div>
      <div className="pulse-hour-summary">
        <div>
          <span>This hour · confirmed</span>
          <strong>{money(forecast?.earnings.actual)}</strong>
        </div>
        <div className="pulse-hour-estimate">
          <span>Hour-end estimate</span>
          <strong className="earnings-tone" data-tone={hourTone}>
            {hourEstimateReady
              ? `≈${money(forecast?.earnings.projected)}`
              : '—'}
          </strong>
          {hourEstimateReady && tiers && (
            <small
              className="hour-estimate-rating earnings-tone"
              data-tone={hourTone}
              title={`Same colors as this Mac’s daily earnings, scaled to one hour: good ≈${money(tiers.green / 24)}, great ≈${money(tiers.purple / 24)}, outstanding ≈${money(tiers.gold / 24)}.`}
            >
              {earningsToneLabel[hourTone]} pace · daily scale
            </small>
          )}
        </div>
        <p>
          All models + rewards ·{' '}
          {paused
            ? 'View paused'
            : `${connected && earningsStatus === 'ok' ? 'Synced' : 'Saved'} ${age(earningsUpdatedAt, pulse?.at ?? Date.now() / 1000)}`}
        </p>
      </div>
      <TrafficPulse
        traffic={traffic}
        paused={paused}
        active={viewActive && visible}
        connected={connected}
      />
      <div className="pulse-disclosures">
        <button
          type="button"
          className="pulse-details-toggle"
          aria-expanded={creditsOpen}
          aria-controls={`${detailsId}-feed`}
          onClick={() => setCreditsOpen((open) => !open)}
        >
          {creditsOpen ? 'Hide credits' : 'Recent credits'}
          <ChevronDown size={16} />
        </button>
        <button
          type="button"
          className="pulse-details-toggle"
          aria-expanded={detailsOpen}
          aria-controls={`${detailsId}-settings`}
          onClick={() => setDetailsOpen(!detailsOpen)}
        >
          {detailsOpen ? 'Hide settings' : 'Pulse settings'}
          <ChevronDown size={16} />
        </button>
      </div>
      <div
        className="pulse-settings-details"
        id={`${detailsId}-settings`}
        hidden={!detailsOpen}
      >
        <div className="chart-toolbar pulse-controls">
          <Choice
            label="Pulse rolling window"
            value={windowSeconds}
            onChange={setWindowSeconds}
            options={[
              { value: '300', label: 'Last 5 minutes' },
              { value: '60', label: 'Last 60 seconds' },
            ]}
          />
          <Choice
            label="Show pace in"
            value={paceUnit}
            onChange={choosePaceUnit}
            options={[
              { value: 'cents', label: 'Cents per hour · 20.53¢' },
              { value: 'dollars', label: 'Dollars per hour · $0.21' },
            ]}
          />
          <Choice
            label="Compare earnings pulse with"
            value={comparisonMode}
            onChange={setComparisonMode}
            options={[
              { value: 'history', label: 'Model history · adjusts for time' },
              { value: 'reference', label: 'Custom reference' },
            ]}
          />
          {comparisonMode === 'reference' && (
            <label className="pulse-reference-input">
              <Input
                type="number"
                min="0.01"
                step="0.01"
                value={referenceUsd}
                placeholder="$/hour"
                onChange={(event) => setReferenceUsd(event.target.value)}
                aria-label="Reference dollars per hour"
              />
              <span>$/hour reference</span>
            </label>
          )}
        </div>
        <p className="footnote pulse-method">
          <Activity size={14} />
          <span>
            {useHistory
              ? `Current-model history: ${money(reference)}/hour across ${num(pulse?.baseline?.hours, 1)} verified warm hours for ${pulse?.models.map(shortModel).join(' + ') || 'these models'}. ${pulse?.baseline?.detail ?? 'Uses the past 30 days and excludes this session. Ready idle time is included; other models are excluded.'} ${seasonal ? `Total matched history remains ${num(pulse?.baseline?.totalHours, 1)} warm hours. ` : ''}`
              : comparisonMode === 'history'
                ? `No rating yet. ${historical == null ? 'Building a baseline: it needs 30 minutes of settled warm time on these models, and this session counts after 10 minutes.' : 'The recorded average for these models is zero.'}`
                : reference == null
                  ? 'Enter a reference to rate the pace.'
                  : `Reference: ${money(reference)}/hour.`}{' '}
            One-second display; Darkbloom confirms credits in batches with a
            roughly 20-second API cache. Updated{' '}
            {age(pulse?.updatedAt, pulse?.at ?? Date.now() / 1000)}. Meter uses
            credit timestamps within verified warm intervals. Cold time,
            warm-ups and base rewards are excluded from pace; confirmed earnings
            remain recorded. The hourly bars show confirmed dollars per clock
            hour, all models and base rewards, over 6 hours to 7 days, with the
            total for that span (the first bar is the whole clock hour); Charts
            has every range. Today’s end-of-day estimate is Your earning days’
            for all models.
            {forecast && (
              <span className="pulse-mobile-copy">
                {' '}
                Hour-end estimate: {forecast.earnings.detail}
              </span>
            )}
          </span>
        </p>
      </div>
    </section>
  );
}
