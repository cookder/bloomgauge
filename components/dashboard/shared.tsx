'use client';
import { memo, useEffect, useState } from 'react';
import { modelLabel } from '@/lib/model-label';
import { startChartPolling } from '@/lib/chart-polling';
import { usePageVisible } from '@/lib/use-page-visibility';
import { useScreenActive } from './app-navigation';
import {
  Dialog,
  DialogContent,
  DialogTitle,
  DialogDescription,
} from '@/components/ui/dialog';
import {
  Area,
  AreaChart,
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Input } from '@/components/ui/input';
const cents = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});
const small = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  minimumSignificantDigits: 2,
  maximumSignificantDigits: 2,
});
/** The one money format: cents from $0.10 up, two significant digits below
 * ($0.061, $0.0034), so small paces never read as $0.00. Chart axes may pass
 * fixed decimals so their ticks line up. */
export const money = (n: number | null | undefined, d?: number) =>
  n == null || !Number.isFinite(n)
    ? '—'
    : d != null
      ? new Intl.NumberFormat('en-US', {
          style: 'currency',
          currency: 'USD',
          minimumFractionDigits: d,
          maximumFractionDigits: d,
        }).format(n)
      : n === 0 || Math.abs(n) >= 0.1
        ? cents.format(n)
        : small.format(n);
/** '1 hour', '2.5 hours', '1 date'. */
export const plural = (n: number | null | undefined, word: string, d = 0) =>
  `${num(n, d)} ${n === 1 ? word : word + 's'}`;
export const num = (n: number | null | undefined, d = 0) =>
  n == null || !Number.isFinite(n)
    ? '—'
    : n.toLocaleString('en-US', { maximumFractionDigits: d });
const time = (n: number) =>
  new Date(n * 1000).toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
export const shortModel = (s: string): string => {
  if (s.startsWith('@combo:')) {
    try {
      const models: unknown = JSON.parse(s.slice(7));
      if (
        Array.isArray(models) &&
        models.length === 2 &&
        models.every((m) => typeof m === 'string' && !m.startsWith('@combo:'))
      )
        return models.map(shortModel).join(' + ');
    } catch {
      /* Keep unknown IDs readable without interpreting them. */
    }
  }
  return s === 'base_reward' ? 'Base reward' : modelLabel(s);
};
export const age = (at: number | null | undefined, now = Date.now() / 1000) =>
  at == null
    ? 'Not received'
    : now - at < 10
      ? 'Just now'
      : now - at < 120
        ? `${Math.round(now - at)}s ago`
        : `${Math.floor((now - at) / 60)}m ago`;
export const fahrenheit = (c: number | null | undefined) =>
  c == null ? null : c * 1.8 + 32;
export const thermalBand = (f: number | null) =>
  f == null
    ? { label: 'Unavailable', color: '#8291a6' }
    : f < 104
      ? { label: 'Cool', color: '#87b9ff' }
      : f < 176
        ? { label: 'Normal', color: '#82efb5' }
        : f < 194
          ? { label: 'Warm', color: '#f3c57e' }
          : { label: 'Hot', color: '#ff8d88' };
export type Range = { preset: string; start?: number; end?: number };
const seconds: Record<string, number> = {
  '5m': 300,
  '15m': 900,
  '1h': 3600,
  '4h': 14400,
  '8h': 28800,
  '12h': 43200,
  '24h': 86400,
  '7d': 604800,
  '30d': 2592000,
  '90d': 7776000,
};
export function bounds(range: Range, now = Date.now() / 1000) {
  return {
    start:
      range.preset === 'all'
        ? 0
        : range.preset === 'custom'
          ? (range.start ?? now - 3600)
          : now - (seconds[range.preset] ?? 900),
    end: range.preset === 'custom' ? (range.end ?? now) : now,
  };
}
const localDate = (at: number) => {
  const d = new Date(at * 1000);
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000)
    .toISOString()
    .slice(0, 16);
};
export function Choice({
  value,
  onChange,
  options,
  label,
  disabled = false,
}: {
  value: string;
  onChange: (v: string) => void;
  options: { value: string; label: string }[];
  label: string;
  disabled?: boolean;
}) {
  return (
    <Select
      disabled={disabled}
      value={value}
      onValueChange={(v) => v != null && onChange(String(v))}
      items={options}
    >
      <SelectTrigger aria-label={label} className="choice">
        <SelectValue />
      </SelectTrigger>
      <SelectContent className="choice-menu">
        {options.map((o) => (
          <SelectItem key={o.value} value={o.value}>
            {o.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
export function RangePicker({
  value,
  onChange,
  label = 'Date range',
  presets,
}: {
  value: Range;
  onChange: (r: Range) => void;
  label?: string;
  presets?: string[];
}) {
  const [editing, setEditing] = useState(false);
  const [start, setStart] = useState(''),
    [end, setEnd] = useState('');
  const [error, setError] = useState('');
  function edit() {
    const selected = bounds(value);
    setStart(localDate(selected.start || Date.now() / 1000 - 3600));
    setEnd(value.preset === 'custom' && value.end ? localDate(value.end) : '');
    setError('');
    setEditing(true);
  }
  return (
    <div className="range-picker">
      <Choice
        value={value.preset}
        onChange={(preset) =>
          preset === 'custom' ? edit() : onChange({ preset })
        }
        label={label}
        options={(
          presets ?? [
            '5m',
            '15m',
            '1h',
            '4h',
            '8h',
            '12h',
            '24h',
            '7d',
            '30d',
            'all',
            'custom',
          ]
        ).map((v) => ({
          value: v,
          label:
            v === 'all'
              ? 'All history'
              : v === 'custom'
                ? 'Custom dates'
                : `Last ${v}`,
        }))}
      />
      {value.preset === 'custom' && (
        <button
          className="small-button"
          type="button"
          onClick={edit}
          aria-label={`Edit ${label.toLowerCase()}`}
        >
          Edit dates
        </button>
      )}
      <Dialog open={editing} onOpenChange={setEditing}>
        <DialogContent className="range-dialog">
          <DialogTitle>{label}</DialogTitle>
          <DialogDescription>
            Choose local dates. Leave the end blank to keep following live data.
          </DialogDescription>
          <form
            className="custom-range"
            onSubmit={(event) => {
              event.preventDefault();
              const from = Date.parse(start) / 1000,
                to = end ? Date.parse(end) / 1000 : undefined;
              if (
                !Number.isFinite(from) ||
                (to != null && (!Number.isFinite(to) || to <= from)) ||
                from >= Date.now() / 1000
              ) {
                setError('Choose a start before the end and before now.');
                return;
              }
              onChange({ preset: 'custom', start: from, end: to });
              setEditing(false);
            }}
          >
            <label>
              From
              <Input
                type="datetime-local"
                value={start}
                onChange={(e) => setStart(e.target.value)}
                required
              />
            </label>
            <label>
              To · blank stays live
              <Input
                type="datetime-local"
                value={end}
                onChange={(e) => setEnd(e.target.value)}
              />
            </label>
            {error && (
              <span className="range-error" role="alert">
                {error}
              </span>
            )}
            <div className="range-dialog-actions">
              <button
                className="small-button"
                type="button"
                onClick={() => setEditing(false)}
              >
                Cancel
              </button>
              <button className="small-button" type="submit">
                Apply range
              </button>
            </div>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  );
}
export type HistoryPoint = {
  at: number;
  tokensPerSecond?: number | null;
  peakTokensPerSecond?: number | null;
  cpuPercent?: number | null;
  gpuPercent?: number | null;
  memoryUsedGB?: number | null;
  cpuTempF?: number | null;
  gpuTempF?: number | null;
  requestsPerMinute?: number | null;
  outputTokens?: number | null;
};
export type HistoryResponse = {
  samples: HistoryPoint[];
  coverageStart: number | null;
  coverageEnd: number | null;
  bucketSeconds: number;
  count: number;
};
export function useHistory(
  range: Range,
  paused = false,
  kind = 'hardware',
  enabled = true,
) {
  const active = useScreenActive();
  const pageVisible = usePageVisible();
  const key = JSON.stringify([range.preset, range.start, range.end, kind]);
  const [saved, setSaved] = useState<{
    key: string;
    data: HistoryResponse | null;
    error: string;
  } | null>(null);
  const data = saved?.key === key ? saved.data : null;
  const error = saved?.key === key ? saved.error : '';
  useEffect(() => {
    if (!enabled || !active || !pageVisible || (paused && data)) return;
    return startChartPolling({
      load: async (signal) => {
        const b = bounds(range);
        const response = await fetch(
          `/api/history?from=${b.start}&to=${b.end}&kind=${encodeURIComponent(kind)}`,
          { signal, cache: 'no-store' },
        );
        if (!response.ok)
          throw Error(
            'History connection interrupted. Saved readings may be out of date.',
          );
        const result = (await response.json()) as HistoryResponse;
        if (!result || !Array.isArray(result.samples))
          throw Error('History unavailable.');
        return result;
      },
      onValue: (result) => setSaved({ key, data: result, error: '' }),
      onError: (error) =>
        setSaved((previous) => ({
          key,
          data: previous?.key === key ? previous.data : null,
          error: error.message,
        })),
      intervalMs:
        kind === 'network'
          ? 30000
          : kind === 'output'
            ? 15000
            : bounds(range).end - bounds(range).start <= 900
              ? 1000
              : 10000,
      repeat: !paused,
    });
    // Data is the frozen result for this query, not a polling dependency.
  }, [key, paused, active, pageVisible, enabled]);
  return { data, error };
}
const tipStyle = {
  background: '#171d27',
  border: '1px solid #354153',
  borderRadius: 10,
  color: '#edf3fa',
  fontSize: 14,
};
export const TimeChart = memo(function TimeChart({
  data,
  series,
  height = 180,
  unit = '',
  area = false,
  precision = 2,
  showPoints = false,
}: {
  data: HistoryPoint[];
  series: { key: string; label: string; color: string }[];
  height?: number;
  unit?: string;
  area?: boolean;
  precision?: number;
  showPoints?: boolean;
}) {
  const active = useScreenActive();
  const pageVisible = usePageVisible();
  const long = data.length > 1 && data[data.length - 1].at - data[0].at > 86400;
  const axis = (n: number) =>
    long
      ? new Date(n * 1000).toLocaleDateString([], {
          month: 'short',
          day: 'numeric',
        })
      : new Date(n * 1000).toLocaleTimeString([], {
          hour: '2-digit',
          minute: '2-digit',
        });
  const maximum = Math.max(
    0,
    ...data.flatMap((point) =>
      series.map((s) => {
        const value = point[s.key as keyof HistoryPoint];
        return typeof value === 'number' && Number.isFinite(value)
          ? Math.abs(value)
          : 0;
      }),
    ),
  );
  const axisPrecision =
    maximum > 0 && maximum < 1
      ? Math.min(6, Math.ceil(-Math.log10(maximum / 5)))
      : 1;
  const common = (
    <>
      <CartesianGrid stroke="#27303c" vertical={false} strokeDasharray="2 6" />
      <XAxis
        dataKey="at"
        type="number"
        domain={['dataMin', 'dataMax']}
        tickFormatter={axis}
        minTickGap={70}
        axisLine={false}
        tickLine={false}
        tick={{ fill: '#8997aa', fontSize: 12 }}
      />
      <YAxis
        axisLine={false}
        tickLine={false}
        tick={{ fill: '#8997aa', fontSize: 12 }}
        tickFormatter={(n) =>
          `${unit.includes('USD') ? '$' : ''}${num(n, axisPrecision)}`
        }
      />
      <Tooltip
        contentStyle={tipStyle}
        labelFormatter={(v) => new Date(Number(v) * 1000).toLocaleString()}
        formatter={(v, n) => [`${num(Number(v), precision)}${unit}`, n]}
      />
    </>
  );
  return (
    <div
      className="time-chart"
      aria-label={series.map((s) => s.label).join(', ')}
      style={{ height, minWidth: 0 }}
    >
      {!active || !pageVisible ? null : data.length > (showPoints ? 0 : 1) ? (
        <ResponsiveContainer
          width="100%"
          height="100%"
          minWidth={0}
          initialDimension={{ width: 600, height }}
        >
          {area ? (
            <AreaChart
              data={data}
              margin={{ top: 10, right: 10, left: -15, bottom: 0 }}
            >
              {common}
              {series.map((s) => (
                <Area
                  key={s.key}
                  type="linear"
                  dataKey={s.key}
                  name={s.label}
                  stroke={s.color}
                  fill={s.color}
                  fillOpacity={0.09}
                  dot={showPoints ? { r: 3 } : false}
                  isAnimationActive={false}
                  connectNulls={false}
                />
              ))}
            </AreaChart>
          ) : (
            <LineChart
              data={data}
              margin={{ top: 10, right: 10, left: -15, bottom: 0 }}
            >
              {common}
              {series.map((s) => (
                <Line
                  key={s.key}
                  type="linear"
                  dataKey={s.key}
                  name={s.label}
                  stroke={s.color}
                  strokeWidth={1.7}
                  dot={showPoints ? { r: 3 } : false}
                  isAnimationActive={false}
                  connectNulls={false}
                />
              ))}
            </LineChart>
          )}
        </ResponsiveContainer>
      ) : (
        <div className="empty">No recorded samples in this range yet.</div>
      )}
    </div>
  );
});
export function HistoryNote({
  data,
  error,
  aggregation = 'average',
}: {
  data: HistoryResponse | null;
  error: string;
  aggregation?: 'average' | 'total';
}) {
  const seconds = data?.bucketSeconds ?? 1;
  const bucket =
    seconds >= 3600 && seconds % 3600 === 0
      ? `${num(seconds / 3600)}-hour`
      : seconds >= 60 && seconds % 60 === 0
        ? `${num(seconds / 60)}-minute`
        : `${num(seconds)}-second`;
  return (
    <p className="footnote">
      {error
        ? `${error}${data ? ' Showing the last loaded history for this range.' : ''}`
        : !data
          ? 'Loading saved history…'
          : data.coverageStart != null
            ? `Recorded since ${new Date(data.coverageStart * 1000).toLocaleString()}. ${aggregation === 'total' ? `${bucket} bucket totals.` : seconds > 1 ? `${bucket} chart averages; raw readings are retained.` : 'One-second readings.'} Gaps mean the app was not collecting.`
            : 'History begins when the collector starts.'}
    </p>
  );
}
