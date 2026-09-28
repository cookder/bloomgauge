'use client';
import { useEffect, useRef, useState, useId } from 'react';
import { bounds, RangePicker, shortModel, type Range } from './shared';
import {
  pulseChartRows,
  pulseSegments,
  readPulseHistory,
  type PulseHistory as History,
} from '@/lib/pulse-smoothing';
import {
  displayPace,
  intervalPace,
  paceInterval,
  paceReading,
} from '@/lib/pulse-display';
import { modelColor } from '@/lib/model-earnings';
import { startChartPolling } from '@/lib/chart-polling';
export type RateReading = {
  at: number;
  streamId: string | null;
  sessionId: number | null;
  models: string[];
  status: string;
  windows: Record<string, { rate: number | null }>;
};

export function RateTrend({
  pulse,
  paused,
  active = true,
  connected,
  windowSeconds,
  range,
  onRangeChange,
  reference,
  color,
  historyEndpoint,
  title,
  unit,
  format,
  minimum = 0.01,
  maxSourceGap = 5,
  compact = false,
  minDecimals = 2,
  readingDecimals = 3,
  readingUnit = unit,
  intervalOverview = false,
}: {
  pulse?: RateReading;
  paused: boolean;
  active?: boolean;
  connected: boolean;
  windowSeconds: string;
  range: Range;
  onRangeChange: (range: Range) => void;
  reference: number | null;
  color: string;
  historyEndpoint: string;
  title: string;
  unit: string;
  format: (value: number | null | undefined, decimals: number) => string;
  minimum?: number;
  maxSourceGap?: number;
  compact?: boolean;
  minDecimals?: number;
  readingDecimals?: number;
  readingUnit?: string;
  intervalOverview?: boolean;
}) {
  const [detail, setDetail] = useState(false);
  const [inspection, setInspection] = useState<{
    key: string;
    at: number;
  } | null>(null);
  const clipId = useId().replace(/:/g, '');
  const graphRef = useRef<SVGSVGElement>(null);
  const [size, setSize] = useState({ width: 220, height: 104 });
  useEffect(() => {
    const graph = graphRef.current;
    if (!graph) return;
    const observer = new ResizeObserver(([entry]) => {
      const { width, height } = entry.contentRect;
      if (width > 0 && height > 0)
        setSize((previous) =>
          Math.abs(previous.width - width) < 0.5 &&
          Math.abs(previous.height - height) < 0.5
            ? previous
            : { width, height },
        );
    });
    observer.observe(graph);
    return () => observer.disconnect();
  }, []);
  const [saved, setSaved] = useState<{ key: string; history: History } | null>(
    null,
  );
  const [request, setRequest] = useState<{
    key: string;
    status: 'loading' | 'ready' | 'error';
  } | null>(null);
  const [retry, setRetry] = useState(0);
  const savedKey = useRef<string | null>(null);
  const latest = useRef(pulse);
  latest.current = pulse;
  useEffect(() => {
    if (!inspection) return;
    const dismiss = (event: PointerEvent) => {
      if (!graphRef.current?.parentElement?.contains(event.target as Node))
        setInspection(null);
    };
    document.addEventListener('pointerdown', dismiss);
    return () => document.removeEventListener('pointerdown', dismiss);
  }, [inspection != null]);
  const session = pulse?.sessionId;
  const key = `${historyEndpoint}:${pulse?.streamId}:${session}:${JSON.stringify(range)}`;
  useEffect(() => {
    if (!active || !connected || session == null) return;
    // Pausing freezes the current query. A deliberate range change can still
    // load one historical result, including when the first view opens paused.
    if (paused && savedKey.current === key) return;
    setRequest({ key, status: 'loading' });
    return startChartPolling<History>({
      async load(signal) {
        const b = bounds(range, latest.current?.at);
        const response = await fetch(
          `${historyEndpoint}${historyEndpoint.includes('?') ? '&' : '?'}scope=models&session=${session}&from=${b.start}&to=${b.end}`,
          { cache: 'no-store', signal },
        );
        if (!response.ok) throw new Error('History unavailable');
        return readPulseHistory(await response.json(), session as number, true);
      },
      onValue(history) {
        savedKey.current = key;
        setSaved({ key, history });
        setRequest({ key, status: 'ready' });
      },
      onError() {
        setRequest({ key, status: 'error' });
      },
      intervalMs: 3000,
      repeat: !paused,
    });
  }, [key, session, paused, active, connected, range, retry, historyEndpoint]);

  const history = saved?.key === key ? saved.history : null;
  const error = request?.key === key && request.status === 'error';
  const loading =
    !history && request?.key === key && request.status === 'loading';
  const b = bounds(range, pulse?.at);
  const start =
    range.preset === 'all' ? (history?.coverageStart ?? b.end - 300) : b.start;
  const end = Math.max(start + 1, b.end);
  const field = windowSeconds === '300' ? 'rate300' : 'rate60';
  const maxGap = Math.max(maxSourceGap, (history?.bucketSeconds ?? 1) * 2);
  // Use the exact same sample as the dial, including its frozen sample while
  // paused. A late history response must not move ahead of the visible meter.
  const current =
    connected && pulse?.status === 'live'
      ? (pulse.windows[windowSeconds]?.rate ?? null)
      : null;
  const rows = pulseChartRows(
    history?.samples ?? [],
    pulse
      ? {
          at: pulse.at,
          sessionId: pulse.sessionId ?? undefined,
          rate60: field === 'rate60' ? current : null,
          rate300: field === 'rate300' ? current : null,
        }
      : null,
    start,
    end,
    maxGap,
  );
  const right = Math.max(60, size.width - 8);
  const bars = intervalOverview && !detail && end - start >= 900;
  const bottom = Math.max(40, size.height - (bars ? 24 : 12));
  const rawSegments = pulseSegments(rows, field, maxGap);
  const display = displayPace(rawSegments, field, {
    start,
    end,
    width: right - 48,
    sourceBucket: history?.bucketSeconds ?? 1,
    detail,
  });
  const intervals = bars
    ? intervalPace(rawSegments, field, {
        start,
        end,
        width: right - 48,
        sourceBucket: history?.bucketSeconds ?? 1,
      })
    : null;
  const segments = intervals
    ? intervals.points.map((p) => [p])
    : display.segments;
  const last = rows.at(-1);
  const endpoint = last?.[field];
  const exactPoint =
    last && endpoint != null && Number.isFinite(endpoint)
      ? paceReading(last, field)
      : null;
  const plotted = segments.flat();
  const values = [
    ...plotted.flatMap((p) => (bars ? [p.low, p.high, p.value] : [p.value])),
    ...(exactPoint ? [exactPoint.value] : []),
  ];
  const rawLow = Math.min(0, ...values);
  const rawHigh = Math.max(minimum, reference ?? 0, ...values) * 1.12;
  const roughStep = (rawHigh - rawLow) / 3;
  const magnitude = 10 ** Math.floor(Math.log10(roughStep));
  const step =
    ([1, 2, 5, 10].find((n) => n * magnitude >= roughStep) ?? 10) * magnitude;
  const low = Math.floor(rawLow / step) * step;
  const high = Math.ceil(rawHigh / step) * step;
  const ticks = Array.from(
    { length: Math.round((high - low) / step) + 1 },
    (_, i) => low + i * step,
  );
  const decimals = Math.min(
    6,
    Math.max(minDecimals, -Math.floor(Math.log10(step))),
  );
  const x = (at: number) => 48 + ((at - start) / (end - start)) * (right - 48);
  const y = (value: number) =>
    12 + ((high - value) / (high - low)) * (bottom - 12);
  const sessions = new Map((history?.sessions ?? []).map((s) => [s.id, s]));
  const modelsFor = (id: number | undefined) =>
    sessions.get(id ?? -1)?.models ??
    (id === session ? (pulse?.models ?? []) : []);
  const modelKey = (id: number | undefined) =>
    modelsFor(id).slice().sort().join(' + ') || 'Unknown model';
  const lineColor = (id: number | undefined) => modelColor(modelKey(id));
  const modelLabel = (id: number | undefined) =>
    modelsFor(id).map(shortModel).join(' + ') || 'Unknown model';
  const modelLegend = [
    ...new Map(
      segments.map((segment) => {
        const id = segment[0].sessionId;
        return [
          modelKey(id),
          {
            key: modelKey(id),
            label: modelLabel(id),
            color: lineColor(id),
            current: modelKey(id) === modelKey(session ?? undefined),
          },
        ];
      }),
    ).values(),
  ];
  // Models sharing a palette color get distinct patterns in both plot and
  // legend. Sorting keeps those patterns stable as samples arrive.
  const pattern = (key: string) => {
    const peers = modelLegend
      .filter((m) => m.color === modelColor(key))
      .map((m) => m.key)
      .sort();
    const index = peers.indexOf(key);
    return index > 0
      ? `${Math.max(2, 10 - index * 2)} 3${index > 3 ? ' 2 3' : ''}`
      : undefined;
  };
  const activeColor = lineColor(session ?? undefined);
  const liveEdge =
    connected &&
    !paused &&
    active &&
    pulse?.status === 'live' &&
    last?.at === pulse.at &&
    endpoint != null &&
    Number.isFinite(endpoint);
  const edgeLabel = liveEdge ? 'Live' : paused ? 'Paused' : 'Last recorded';
  const stamp = (at: number) =>
    new Date(at * 1000).toLocaleString(
      [],
      end - start >= 86400
        ? { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }
        : { hour: 'numeric', minute: '2-digit' },
    );
  const rangeLabel =
    range.preset === 'custom'
      ? 'Custom'
      : range.preset === 'all'
        ? 'All history'
        : range.preset;
  const inspectKey = `${key}:${windowSeconds}:${detail}`;
  const selectable = [
    ...plotted.filter(
      (p) =>
        !exactPoint ||
        p.at !== exactPoint.at ||
        p.sessionId !== exactPoint.sessionId,
    ),
    ...(exactPoint ? [exactPoint] : []),
  ].sort((a, b) => a.at - b.at);
  const inspected =
    inspection?.key === inspectKey && selectable.length
      ? selectable.reduce((a, b) =>
          Math.abs(a.at - inspection.at) <= Math.abs(b.at - inspection.at)
            ? a
            : b,
        )
      : null;
  const inspectPointer = (clientX: number) => {
    const box = graphRef.current?.getBoundingClientRect();
    if (!box || !selectable.length) return;
    const at =
      start +
      Math.max(0, Math.min(1, (clientX - box.left - 48) / (right - 48))) *
        (end - start);
    setInspection({ key: inspectKey, at });
  };
  const lastTrend = segments.at(-1)?.at(-1);
  const canJoinTip =
    !bars &&
    !detail &&
    exactPoint &&
    lastTrend &&
    lastTrend.at < exactPoint.at &&
    rawSegments.at(-1)?.at(-1)?.at === exactPoint.at &&
    lastTrend.sessionId === exactPoint.sessionId;
  const fragmented =
    !!history?.samples.length &&
    history.samples.filter((p) => p[field] == null).length >
      history.samples.length / 3;
  const resolution = detail
    ? history && history.bucketSeconds > 1
      ? `Source ${paceInterval(history.bucketSeconds)} avg`
      : 'Recorded readings'
    : `${paceInterval(intervals?.bucketSeconds ?? display.bucketSeconds)} avg`;
  return (
    <div className={`pulse-trend${compact ? ' rate-trend-compact' : ''}`}>
      <div className="pulse-trend-title">
        <span>{title}</span>
        <span
          className="pulse-trend-status"
          style={liveEdge ? { color: activeColor } : undefined}
        >
          {liveEdge ? '● Live' : rangeLabel}
        </span>
      </div>
      <div className="pulse-trend-range">
        <RangePicker
          value={range}
          onChange={onRangeChange}
          label={`${title} chart date range`}
        />
      </div>
      <strong
        style={{
          color: last?.sessionId != null ? lineColor(last.sessionId) : color,
        }}
        aria-label={`${edgeLabel} reading in this range: ${format(endpoint, readingDecimals)} ${unit}`}
      >
        {format(endpoint, readingDecimals)}
        <small> {readingUnit}</small>
      </strong>
      <div
        className="pulse-chart-frame"
        tabIndex={0}
        role="group"
        aria-label={`${title} chart. Tap or hover to inspect; use arrow keys to move through readings and Escape to clear.`}
        onPointerMove={(event) => {
          if (event.pointerType === 'mouse') inspectPointer(event.clientX);
          else setInspection(null);
        }}
        onPointerDown={(event) => inspectPointer(event.clientX)}
        onPointerLeave={(event) => {
          if (event.pointerType === 'mouse') setInspection(null);
        }}
        onBlur={() => setInspection(null)}
        onKeyDown={(event) => {
          if (event.key === 'Escape') {
            setInspection(null);
            return;
          }
          if (
            !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key) ||
            !selectable.length
          )
            return;
          event.preventDefault();
          const index = inspected
            ? selectable.indexOf(inspected)
            : selectable.length - 1;
          const next =
            event.key === 'Home'
              ? 0
              : event.key === 'End'
                ? selectable.length - 1
                : Math.max(
                    0,
                    Math.min(
                      selectable.length - 1,
                      index + (event.key === 'ArrowLeft' ? -1 : 1),
                    ),
                  );
          setInspection({ key: inspectKey, at: selectable[next].at });
        }}
      >
        <svg
          className="pulse-trend-graph"
          ref={graphRef}
          viewBox={`0 0 ${size.width} ${size.height}`}
          role="img"
          aria-label={`Model history: ${title.toLowerCase()} in ${unit}, ${rangeLabel}. ${detail ? resolution : `${resolution} of recorded pace, weighted by time.`} ${bars ? 'Bars show observed averages; thin marks show the observed range. The strip below marks recorded coverage; striped sections have no readings.' : 'Gaps and session boundaries remain separate.'} Dot is the exact latest reading. ${windowSeconds}-second rolling rate. ${edgeLabel} reading ${format(endpoint, readingDecimals)} ${unit}. ${reference == null ? 'No comparison baseline yet.' : `Dashed current-model reference ${format(reference, 3)} ${unit}.`}`}
        >
          {ticks.map((tick) => (
            <g key={tick}>
              <line
                x1="48"
                x2={right}
                y1={y(tick)}
                y2={y(tick)}
                className="pulse-trend-grid"
              />
              <text x="43" y={y(tick) + 4} textAnchor="end">
                {format(tick, decimals)}
              </text>
            </g>
          ))}
          <line
            x1="48"
            x2="48"
            y1="12"
            y2={bottom}
            className="pulse-trend-grid"
          />
          {reference != null && (
            <line
              x1="48"
              x2={right}
              y1={y(reference)}
              y2={y(reference)}
              stroke="var(--c-7a8f85)"
              strokeDasharray="3 4"
            />
          )}
          <defs>
            <clipPath id={clipId}>
              <rect
                x="46"
                y="5"
                width={Math.max(1, right - 40)}
                height={bottom + 6}
              />
            </clipPath>
            <pattern
              id={`${clipId}-missing`}
              width="5"
              height="5"
              patternUnits="userSpaceOnUse"
            >
              <path
                d="M-1,1 L1,-1 M0,5 L5,0 M4,6 L6,4"
                stroke="var(--c-738279)"
                strokeWidth="1"
                opacity=".65"
              />
            </pattern>
          </defs>
          <g clipPath={`url(#${clipId})`}>
            {segments.map((segment, index) => {
              const sid = segment[0].sessionId;
              const d = segment
                .map(
                  (p, i) =>
                    `${i ? 'L' : 'M'}${x(p.at).toFixed(2)},${y(p.value).toFixed(2)}`,
                )
                .join(' ');
              const label = `${modelLabel(sid)} · Session ${sid ?? 'unknown'} · ${stamp(segment[0].at)} – ${stamp(segment.at(-1)!.at)}`;
              return (
                <g
                  key={`${sid}:${index}`}
                  className="pulse-model-segment"
                  data-session={sid}
                  data-model={modelKey(sid)}
                >
                  <title>{label}</title>
                  {bars ? (
                    <g
                      className="pulse-interval-bar"
                      data-from={segment[0].from}
                      data-to={segment[0].to}
                      data-rate={segment[0].value}
                      data-coverage={segment[0].coverage}
                    >
                      <line
                        x1={x(segment[0].at)}
                        x2={x(segment[0].at)}
                        y1={y(segment[0].low)}
                        y2={y(segment[0].high)}
                        stroke={lineColor(sid)}
                        opacity=".5"
                        strokeWidth="1.25"
                      />
                      {segment[0].kind === 'average' ? (
                        <rect
                          x={x(segment[0].from) + 1}
                          width={Math.max(
                            1,
                            x(segment[0].to) - x(segment[0].from) - 2,
                          )}
                          y={
                            segment[0].value >= 0
                              ? y(segment[0].value) -
                                (segment[0].value === 0 ? 1 : 0)
                              : y(0)
                          }
                          height={Math.max(
                            2,
                            Math.abs(y(segment[0].value) - y(0)),
                          )}
                          rx="1"
                          fill={lineColor(sid)}
                          opacity={
                            (segment[0].coverage ?? 0) < 0.8 ? 0.5 : 0.85
                          }
                        />
                      ) : (
                        <path
                          d={`M${x(segment[0].at) - 2},${y(segment[0].value)}h4`}
                          stroke={lineColor(sid)}
                          strokeWidth="2"
                        />
                      )}
                    </g>
                  ) : segment.length === 1 ? (
                    segment[0].kind === 'average' &&
                    x(segment[0].to) - x(segment[0].from) >= 2 ? (
                      <line
                        x1={x(segment[0].from)}
                        x2={x(segment[0].to)}
                        y1={y(segment[0].value)}
                        y2={y(segment[0].value)}
                        stroke={lineColor(sid)}
                        strokeWidth="2"
                      />
                    ) : (
                      <circle
                        cx={x(segment[0].at)}
                        cy={y(segment[0].value)}
                        r={end - start >= 86400 ? 1.5 : 2}
                        fill={lineColor(sid)}
                        opacity={end - start >= 86400 ? 0.8 : 1}
                      />
                    )
                  ) : (
                    <path
                      d={d}
                      fill="none"
                      stroke={lineColor(sid)}
                      strokeWidth={detail ? 1.5 : 2.25}
                      strokeLinejoin="round"
                      strokeDasharray={pattern(modelKey(sid))}
                      vectorEffect="non-scaling-stroke"
                    />
                  )}
                </g>
              );
            })}
            {canJoinTip && (
              <path
                className="pulse-trend-tip-link"
                d={`M${x(lastTrend.at)},${y(lastTrend.value)} L${x(exactPoint.at)},${y(exactPoint.value)}`}
                fill="none"
                stroke={lineColor(exactPoint.sessionId)}
                strokeWidth="1.5"
                strokeDasharray="2 3"
                opacity=".7"
              />
            )}
            {exactPoint && (
              <g
                className="pulse-trend-endpoint"
                data-rate={exactPoint.value}
                data-at={exactPoint.at}
                data-live={liveEdge}
              >
                <title>
                  {edgeLabel}: {format(exactPoint.value, readingDecimals)}{' '}
                  {unit} · {stamp(exactPoint.at)}
                </title>
                {liveEdge && (
                  <circle
                    cx={x(exactPoint.at)}
                    cy={y(exactPoint.value)}
                    r="5.5"
                    fill={lineColor(exactPoint.sessionId)}
                    opacity=".18"
                  />
                )}
                <circle
                  cx={x(exactPoint.at)}
                  cy={y(exactPoint.value)}
                  r="3"
                  fill={lineColor(exactPoint.sessionId)}
                />
              </g>
            )}
            {inspected && (
              <g className="pulse-chart-cursor">
                <line
                  x1={x(inspected.at)}
                  x2={x(inspected.at)}
                  y1="8"
                  y2={bottom}
                  stroke="var(--c-b4c7c0)"
                  strokeDasharray="2 3"
                  opacity=".6"
                />
                <circle
                  cx={x(inspected.at)}
                  cy={y(inspected.value)}
                  r="3.5"
                  fill={lineColor(inspected.sessionId)}
                  stroke="var(--c-dcece4)"
                />
              </g>
            )}
          </g>
          {intervals && (
            <g
              className="pulse-coverage-strip"
              aria-label="Recorded coverage; stripes indicate missing readings"
            >
              <rect
                x="48"
                y={bottom + 10}
                width={right - 48}
                height="4"
                fill={`url(#${clipId}-missing)`}
              />
              {intervals.coverage.map((p, i) => (
                <rect
                  key={i}
                  x={x(p.from)}
                  y={bottom + 10}
                  width={Math.max(0, x(p.to) - x(p.from))}
                  height="4"
                  fill={lineColor(p.sessionId)}
                  opacity=".8"
                />
              ))}
            </g>
          )}
          {!values.length && (
            <text x={(48 + right) / 2} y={size.height / 2} textAnchor="middle">
              <tspan x={(48 + right) / 2}>
                {error ? 'Unavailable' : loading ? 'Loading…' : 'No readings'}
              </tspan>
              {!error && !loading && (
                <tspan x={(48 + right) / 2} dy="15">
                  {session == null ? 'No active session' : 'in this range'}
                </tspan>
              )}
            </text>
          )}
        </svg>
        {inspected && (
          <div className="pulse-chart-tooltip" role="status">
            <strong>
              {format(inspected.value, readingDecimals)} {readingUnit}
            </strong>
            <span>
              {modelLabel(inspected.sessionId)} ·{' '}
              {inspected.kind === 'average'
                ? `${stamp(inspected.from)} – ${stamp(inspected.to)}`
                : stamp(inspected.at)}
            </span>
            <span>
              {inspected.kind === 'average'
                ? `${paceInterval(inspected.to - inspected.from)} interval · ${inspected.observedSeconds == null ? 'observed average' : `${Math.round((inspected.coverage ?? 0) * 100)}% recorded`} · range ${format(inspected.low, readingDecimals)}–${format(inspected.high, readingDecimals)}`
                : inspected.at === exactPoint?.at
                  ? `${edgeLabel} reading`
                  : 'Recorded reading'}
            </span>
          </div>
        )}
      </div>
      <div className="pulse-trend-times">
        <span>{stamp(start)}</span>
        <span className="pulse-trend-midtime">{stamp((start + end) / 2)}</span>
        <span>{stamp(end)}</span>
      </div>
      {modelLegend.length > 0 && (
        <div
          className="pulse-model-legend"
          aria-label={`${title} model colors`}
        >
          {modelLegend.map((model) => (
            <span key={model.key} title={model.key}>
              <svg viewBox="0 0 15 8" aria-hidden="true">
                <line
                  x1="0"
                  x2="15"
                  y1="4"
                  y2="4"
                  stroke={model.color}
                  strokeWidth="2.5"
                  strokeDasharray={pattern(model.key)}
                />
              </svg>
              <span>
                {model.label}
                {model.current ? ' · current' : ''}
              </span>
            </span>
          ))}
        </div>
      )}
      {intervals ? (
        <p className="pulse-interval-key">
          Bars: observed average · thin marks: range
          <br />
          Coverage strip:{' '}
          {Math.round(
            Math.min(1, intervals.observedSeconds / (end - start)) * 100,
          )}
          % recorded · stripes: gaps
        </p>
      ) : (
        fragmented && (
          <p className="pulse-trend-gap-note">Recorded history has gaps.</p>
        )
      )}
      <p className="pulse-trend-axis-key">
        {unit} · local time
        {reference != null && (
          <>
            <br />
            Dashed: current reference {format(reference, 3)} {unit}
          </>
        )}
      </p>
      <p
        className="pulse-trend-note"
        title="Trend uses time-weighted averages sized to this chart and range. Gaps and sessions stay separate. Detail retains recorded peaks. Tap or hover to inspect."
      >
        {error || !connected
          ? history
            ? 'Saved readings · reconnecting'
            : 'History unavailable'
          : loading
            ? 'Loading history…'
            : paused
              ? 'View paused'
              : `${windowSeconds === '300' ? '5m' : '60s'} pace`}
        {' · '}
        {resolution.toLowerCase()}
        {' · '}
        <button
          type="button"
          className="pulse-smoothing-toggle"
          aria-pressed={detail}
          onClick={() => setDetail(!detail)}
        >
          {detail ? 'Show trend' : 'Show detail'}
        </button>
        {error && connected && (
          <>
            {' · '}
            <button
              type="button"
              className="pulse-smoothing-toggle"
              onClick={() => {
                savedKey.current = null;
                setRetry((value) => value + 1);
              }}
            >
              Retry
            </button>
          </>
        )}
      </p>
    </div>
  );
}
