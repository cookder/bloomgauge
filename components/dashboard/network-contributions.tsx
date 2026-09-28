'use client';
import { useEffect, useMemo, useState } from 'react';
import { ContributionChart } from './contribution-chart';
import { useStudy } from './model-research';
import { Choice, money, num, RangePicker, type Range } from './shared';
import { modelColor } from '@/lib/model-earnings';
import {
  contributionGroups,
  contributionShares,
  groupedContributions,
  readNetworkContributions,
  type ContributionMetric,
  type NetworkContributions,
} from '@/lib/network-contributions';

const options = [
  { value: 'activity', label: 'Network activity · by model' },
  { value: 'requests', label: 'Completed requests' },
  { value: 'tokens', label: 'Output tokens' },
  { value: 'earnings', label: 'My earnings · by model' },
];
const rateUnits = {
  activity: 'concurrent requests',
  requests: 'requests / min',
  tokens: 'output tokens / sec',
  earnings: 'USD / elapsed hour',
};
const summaryLabels = {
  activity: 'Average recorded model activity',
  requests: 'Recorded completed requests',
  tokens: 'Recorded output tokens',
  earnings: 'Recorded inference earnings on this Mac',
};
const descriptions = {
  activity: 'Active and queued requests across the network, stacked by model.',
  requests:
    'Completed traffic across the network. The source does not provide a model breakdown for these totals.',
  tokens:
    'Generated output tokens across the network. The source does not provide a model breakdown for these totals.',
  earnings:
    'Actual model credits on this Mac, including signed corrections. Base rewards are excluded.',
};
const compact = (v: number) =>
  new Intl.NumberFormat('en-US', {
    notation: 'compact',
    maximumFractionDigits: 1,
  }).format(v);
const timestamp = (at: number) =>
  new Date(at * 1000).toLocaleString([], {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });

export function NetworkContributionsPanel({ paused }: { paused: boolean }) {
  const [metric, setMetric] = useState<ContributionMetric>('activity'),
    [range, setRange] = useState<Range>({ preset: '24h' });
  const [inspection, setInspection] = useState<number | null>(null);
  const key = JSON.stringify([metric, range]);
  useEffect(() => setInspection(null), [key]);
  const { data, error } = useStudy<NetworkContributions>(
    `/api/network/contributions?metric=${metric}`,
    range,
    paused,
    (d) => {
      readNetworkContributions(d, metric);
      return true;
    },
    60000,
  );
  const groups = useMemo(() => (data ? contributionGroups(data) : []), [data]);
  const series = groups.map((g) => ({
    id: g.id,
    label: g.name,
    color:
      g.id === 'other' ||
      g.id === 'unattributed' ||
      data?.attribution === 'unavailable'
        ? 'var(--c-899da8)'
        : modelColor(g.id.replace(/^model:/, '')),
  }));
  const points = useMemo(
    () =>
      data?.points.map((p) => ({
        from: p.from,
        to: p.to,
        values: groupedContributions(p.values, groups),
      })) ?? [],
    [data, groups],
  );
  const point = inspection !== null ? points[inspection] : undefined;
  const values =
    point?.values ??
    (data ? groupedContributions(data.summary.values, groups) : []);
  const originalValues =
    point && data
      ? data.points[inspection!].values
      : (data?.summary.values ?? []);
  const shares = originalValues.some((v) => v !== null && v < 0)
    ? values.map(() => null)
    : contributionShares(values);
  const format = (v: number) => (metric === 'earnings' ? money(v) : num(v, 1));
  const axis = (v: number) =>
    metric === 'earnings' ? money(v, Math.abs(v) < 1 ? 3 : 2) : compact(v);
  const primary = data?.summary.total;
  const hasValues = points.some((p) => p.values.some((v) => v !== null));
  const partialModels = points.some(
    (p) => p.values.some((v) => v !== null) && p.values.some((v) => v === null),
  );
  return (
    <section
      className="panel network-contributions"
      aria-label="Network traffic and model contributions"
    >
      <div className="panel-heading">
        <div>
          <div className="eyebrow">TRAFFIC & EARNINGS</div>
          <h2>Where the activity comes from.</h2>
        </div>
        <span className="contribution-scope">
          {metric === 'earnings' ? 'This Mac · paid credits' : 'Whole network'}
        </span>
      </div>
      <div className="chart-toolbar">
        <Choice
          label="Contribution metric"
          value={metric}
          onChange={(v) => setMetric(v as ContributionMetric)}
          options={options}
        />
        <RangePicker
          label="Contribution date range"
          presets={['1h', '4h', '8h', '24h', '7d', '30d']}
          value={range}
          onChange={setRange}
        />
      </div>
      <p className="contribution-note">{descriptions[metric]}</p>
      {error && (
        <p className="notice" role="status">
          {error} {data ? 'Showing the last loaded report for this range.' : ''}{' '}
          {paused ? 'Resume to retry.' : 'Retrying automatically.'}
        </p>
      )}
      {!data && !error && (
        <p className="muted" role="status">
          Reading recorded traffic and model contributions…
        </p>
      )}
      {data && (
        <>
          <div className="contribution-summary">
            <div>
              <span>{summaryLabels[metric]}</span>
              <strong>
                {primary == null
                  ? '—'
                  : metric === 'earnings'
                    ? money(primary)
                    : num(primary, metric === 'activity' ? 1 : 0)}
              </strong>
              {primary == null && hasValues && (
                <small>Total unknown · some models are missing</small>
              )}
            </div>
            <div>
              <span>
                {num(data.coverage.fraction * 100, 1)}% observed coverage
              </span>
              <small>
                {timestamp(data.from)} – {timestamp(data.to)}
              </small>
            </div>
          </div>
          {data.status === 'unavailable' || !hasValues ? (
            <div className="contribution-empty">
              <strong>
                {data.status === 'unavailable'
                  ? 'This view is unavailable'
                  : 'No covered intervals in this range'}
              </strong>
              <span>
                {data.notes[0] ??
                  'Choose another range. Missing observations stay unknown.'}
              </span>
            </div>
          ) : (
            <ContributionChart
              rows={points}
              series={series}
              from={data.from}
              to={data.to}
              selected={point ? inspection : null}
              onSelect={setInspection}
              format={axis}
              unit={rateUnits[metric]}
            />
          )}
          {partialModels && (
            <p className="contribution-note">
              Some model readings are missing. Hatched columns show only the
              known contributions; their full total and shares are unknown.
            </p>
          )}
          <div className="contribution-inspection">
            <span>
              {point
                ? `${timestamp(point.from)} – ${timestamp(point.to)} · ${rateUnits[metric]}`
                : 'Model contributions over the selected range'}
            </span>
            {point && (
              <button type="button" onClick={() => setInspection(null)}>
                Whole range
              </button>
            )}
          </div>
          {groups.length > 0 && (
            <div
              className="contribution-models"
              aria-label="Model contribution breakdown"
            >
              {groups.map((g, i) => (
                <div className="contribution-model" key={g.id}>
                  <i
                    style={{ background: series[i].color }}
                    aria-hidden="true"
                  />
                  <span className="contribution-model-name">{g.name}</span>
                  <strong>
                    {values[i] == null
                      ? 'Unknown'
                      : point || metric === 'activity'
                        ? format(values[i]!)
                        : metric === 'earnings'
                          ? money(values[i])
                          : num(values[i], 0)}
                  </strong>
                  <span className="contribution-model-share">
                    {shares[i] === null ? '—' : `${num(shares[i], 1)}%`}
                  </span>
                </div>
              ))}
            </div>
          )}

          <p className="contribution-note">
            {point
              ? 'Tap or drag along the timeline to inspect another interval.'
              : 'Tap the chart or use the slider to inspect a time interval.'}{' '}
            The coverage strip marks complete, partial and missing model
            readings. Times use your local timezone.
          </p>
          {metric === 'earnings' && (
            <p className="contribution-note">{data.networkMoney.reason}</p>
          )}
          <details className="weekly-method">
            <summary>Coverage & data sources</summary>
            {data.notes.map((note, i) => (
              <p className="footnote" key={i}>
                {note}
              </p>
            ))}
            <p className="footnote">
              Chart intervals: {num(data.bucketSeconds / 60, 1)} minutes.
              Updated {timestamp(data.at)}. Refreshes each minute while visible.
            </p>
          </details>
        </>
      )}
    </section>
  );
}
