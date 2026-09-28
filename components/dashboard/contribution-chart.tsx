'use client';
import { useEffect, useId, useRef, useState } from 'react';
import { usePageVisible } from '@/lib/use-page-visibility';
import { useScreenActive } from './app-navigation';

export type ContributionInterval = {
  from: number;
  to: number;
  values: (number | null)[];
};
export type ContributionSeries = { id: string; label: string; color: string };

// Partial stacks retain known contributions, with an explicit hatch and no implied total.
export function ContributionChart({
  rows,
  series,
  from,
  to,
  selected,
  onSelect,
  format,
  unit,
  height = 260,
  barGap = 0,
  label = 'Model contributions over time',
  sliderLabel = 'Inspect network timeline',
}: {
  rows: ContributionInterval[];
  series: ContributionSeries[];
  from: number;
  to: number;
  selected: number | null;
  onSelect: (index: number | null) => void;
  format: (value: number) => string;
  unit: string;
  height?: number;
  barGap?: number;
  label?: string;
  sliderLabel?: string;
}) {
  const ref = useRef<SVGSVGElement>(null),
    [width, setWidth] = useState(600);
  const active = useScreenActive(),
    visible = usePageVisible(),
    id = useId().replace(/:/g, '');
  useEffect(() => {
    const el = ref.current;
    if (!el || !active || !visible) return;
    const observer = new ResizeObserver(([entry]) => {
      if (entry.contentRect.width > 0) setWidth(entry.contentRect.width);
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, [active, visible]);
  const left = 58,
    right = Math.max(left + 1, width - 9),
    top = 15,
    bottom = height - 40;
  const totals = rows.map((r) =>
    r.values.length === series.length &&
    r.values.every((v) => v !== null && Number.isFinite(v))
      ? r.values.reduce<number>((n, v) => n + v!, 0)
      : null,
  );
  const upper = Math.max(
    0,
    ...rows.map((r) =>
      r.values.reduce<number>((n, v) => n + Math.max(0, v ?? 0), 0),
    ),
  );
  const lower = Math.min(
    0,
    ...rows.map((r) =>
      r.values.reduce<number>((n, v) => n + Math.min(0, v ?? 0), 0),
    ),
  );
  const max = upper === 0 && lower === 0 ? 1 : upper * 1.08,
    min = lower * 1.08,
    span = max - min;
  const x = (at: number) =>
    left +
    ((Math.min(to, Math.max(from, at)) - from) / Math.max(1, to - from)) *
      (right - left);
  const y = (value: number) => bottom - ((value - min) / span) * (bottom - top);
  const paths = series.map(() => [] as string[]);
  rows.forEach((row) => {
    if (
      row.values.every((v) => v === null) ||
      row.to <= row.from ||
      row.to <= from ||
      row.from >= to
    )
      return;
    let positive = 0,
      negative = 0;
    const inset = Math.min(
      barGap / 2,
      Math.max(0, (x(row.to) - x(row.from)) / 4),
    );
    row.values.forEach((value, k) => {
      const base = value !== null && value < 0 ? negative : positive;
      if (value !== null && value !== 0)
        paths[k].push(
          `M${x(row.from) + inset},${y(base + value)}H${x(row.to) - inset}V${y(base)}H${x(row.from) + inset}Z`,
        );
      if (value !== null && value < 0) negative += value;
      else positive += value ?? 0;
    });
  });
  const pick = (clientX: number) => {
    const box = ref.current?.getBoundingClientRect();
    if (!box || !rows.length) return;
    const at =
      from +
      ((Math.max(left, Math.min(right, clientX - box.left)) - left) /
        (right - left)) *
        (to - from);
    const index = rows.findIndex((r) => at >= r.from && at < r.to);
    if (index >= 0) onSelect(index);
  };
  const time = (at: number) =>
    new Date(at * 1000).toLocaleString(
      [],
      to - from > 86400
        ? { month: 'short', day: 'numeric', hour: 'numeric' }
        : { hour: 'numeric', minute: '2-digit' },
    );
  const index =
    selected !== null && selected >= 0 && selected < rows.length
      ? selected
      : null;
  return (
    <div className="contribution-chart">
      <p className="contribution-note">{unit}</p>
      <svg
        ref={ref}
        viewBox={`0 0 ${width} ${height}`}
        style={{ height }}
        role="img"
        aria-label={`${label}, ${unit}. Use the time slider to inspect each interval.`}
        onPointerDown={(e) => pick(e.clientX)}
        onPointerMove={(e) => {
          if (e.pointerType === 'mouse' || e.buttons === 1) pick(e.clientX);
        }}
      >
        <defs>
          <pattern
            id={`${id}-gap`}
            width="6"
            height="6"
            patternUnits="userSpaceOnUse"
          >
            <path
              d="M-1,1L1,-1M0,6L6,0M5,7L7,5"
              stroke="var(--c-788b9a)"
              strokeWidth="1"
              opacity=".55"
            />
          </pattern>
        </defs>
        {[min, min + span / 2, max].map((tick) => (
          <g key={tick}>
            <line
              x1={left}
              x2={right}
              y1={y(tick)}
              y2={y(tick)}
              stroke="var(--c-293a42)"
              strokeDasharray="3 5"
            />
            <text
              x={left - 7}
              y={y(tick) + 4}
              textAnchor="end"
              fill="var(--c-a7b9c5)"
              fontSize="11"
            >
              {format(tick)}
            </text>
          </g>
        ))}
        <line
          x1={left}
          x2={right}
          y1={y(0)}
          y2={y(0)}
          stroke="var(--c-80978d)"
        />
        {active &&
          visible &&
          paths.map((p, k) => (
            <path
              key={series[k].id}
              d={p.join('')}
              fill={series[k].color}
              opacity=".82"
            />
          ))}
        {rows.map((r, i) =>
          totals[i] === null && r.values.some((v) => v !== null) ? (
            <rect
              key={`partial-${i}`}
              x={x(r.from)}
              y={top}
              width={Math.max(0, x(r.to) - x(r.from))}
              height={bottom - top}
              fill={`url(#${id}-gap)`}
              opacity=".22"
            />
          ) : null,
        )}
        <rect
          x={left}
          y={bottom + 5}
          width={right - left}
          height="4"
          fill={`url(#${id}-gap)`}
        />
        {rows.map((r, i) =>
          r.values.some((v) => v !== null) && r.to > from && r.from < to ? (
            <rect
              key={i}
              x={x(r.from)}
              y={bottom + 5}
              width={Math.max(0, x(r.to) - x(r.from))}
              height="4"
              fill={totals[i] === null ? 'var(--c-c49d56)' : 'var(--c-739d88)'}
            />
          ) : null,
        )}
        {index !== null && (
          <rect
            x={x(rows[index].from)}
            y={top}
            width={Math.max(1, x(rows[index].to) - x(rows[index].from))}
            height={bottom - top}
            fill="var(--c-ffffff)"
            fillOpacity=".08"
            stroke="var(--c-e4ede6)"
            strokeWidth="1"
          />
        )}
        {[0, 0.5, 1].map((f) => (
          <text
            key={f}
            x={left + (right - left) * f}
            y={height - 8}
            textAnchor={f === 0 ? 'start' : f === 1 ? 'end' : 'middle'}
            fill="var(--c-a7b9c5)"
            fontSize="11"
          >
            {time(from + (to - from) * f)}
          </text>
        ))}
      </svg>
      {!!rows.length && (
        <input
          type="range"
          min={0}
          max={rows.length - 1}
          step={1}
          value={index ?? rows.length - 1}
          aria-label={sliderLabel}
          aria-valuetext={`${time(rows[index ?? rows.length - 1].from)}, ${totals[index ?? rows.length - 1] === null ? 'incomplete coverage' : format(totals[index ?? rows.length - 1]!)} ${unit}`}
          onChange={(e) => onSelect(Number(e.target.value))}
        />
      )}
    </div>
  );
}
