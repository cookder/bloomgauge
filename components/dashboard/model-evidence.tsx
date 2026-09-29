'use client';
import { useEffect, useState } from 'react';
import { startChartPolling } from '@/lib/chart-polling';
import { usePageVisible } from '@/lib/use-page-visibility';
import {
  shadowProgress,
  validShadowReport,
  type ShadowPrediction,
  type ShadowReport,
} from '@/lib/shadow-estimator';
import { useScreenActive } from './app-navigation';
import { money, num, shortModel } from './shared';

type Row = {
  model: string;
  selected: boolean;
  current: boolean;
  daysSinceMeasured?: number | null;
  estimate?: {
    rate: number;
    lower: number;
    upper: number;
    hours: number;
    evidenceHours?: number;
    days: number;
    forecastUsable?: boolean;
    scope: string;
  } | null;
};

const measured = (days?: number | null) =>
  days == null
    ? 'Never'
    : days === 0
      ? 'Today'
      : days === 1
        ? 'Yesterday'
        : days >= 7
          ? '7+ days ago'
          : `${days} days ago`;

function Shadow({ value }: { value?: ShadowPrediction }) {
  if (!value) return <span className="muted">—</span>;
  if (value.usdPerHour == null)
    return (
      <span
        className="muted"
        title="No current network demand reading for this model"
      >
        No demand reading
      </span>
    );
  const thin =
    value.basis === 'prior'
      ? 'no history yet'
      : value.basis === 'shared'
        ? 'starting estimate, not yet run here'
        : value.basis === 'limited'
          ? 'thin history'
          : value.extrapolated
            ? 'beyond measured demand'
            : '';
  return (
    <span className="shadow-estimate">
      {money(value.usdPerHour)}
      <small>
        {' '}
        {money(value.lower)}–{money(value.upper)}
        {thin && ` · ${thin}`}
      </small>
    </span>
  );
}

/** Shadow predictions, loaded only while the table is open. */
function useShadowReport(open: boolean) {
  const [report, setReport] = useState<ShadowReport | null>(null);
  const [error, setError] = useState('');
  const active = useScreenActive(),
    visible = usePageVisible();
  useEffect(() => {
    if (!open || !active || !visible) return;
    return startChartPolling({
      load: async (signal) => {
        const res = await fetch('/api/optimizer/shadow-estimator', {
          signal,
          cache: 'no-store',
        });
        if (!res.ok) throw Error('Shadow estimates are unavailable right now.');
        const value: unknown = await res.json();
        if (!validShadowReport(value))
          throw Error('Shadow estimates returned incomplete data.');
        return value;
      },
      onValue: (value) => {
        setReport(value);
        setError('');
      },
      onError: (problem) => setError(problem.message),
      intervalMs: 60000,
    });
  }, [open, active, visible]);
  return { report, error };
}

/** What BloomGauge has measured for each selected model, and whether it can switch to it confidently. */
export function ModelEvidence({
  rows,
  open = true,
}: {
  rows?: Row[];
  open?: boolean;
}) {
  const { report, error } = useShadowReport(open);
  const list = (rows ?? [])
    .filter((r) => r.selected || r.current)
    .sort(
      (a, b) =>
        Number(b.current) - Number(a.current) ||
        Number(!!b.estimate?.forecastUsable) -
          Number(!!a.estimate?.forecastUsable) ||
        (b.estimate?.evidenceHours ?? 0) - (a.estimate?.evidenceHours ?? 0),
    );
  if (!list.length) return <p className="footnote">No models selected yet.</p>;
  const ready = list.filter(
    (r) => !r.current && r.estimate?.forecastUsable,
  ).length;
  const shadow = report?.latest?.models;
  return (
    <div className="model-evidence">
      <p className="footnote">
        {ready
          ? `${ready} other model${ready === 1 ? ' has' : 's have'} enough matched history for a confident switch.`
          : 'No other model has enough matched history for a confident switch yet. Learning runs fill this in.'}{' '}
        Rates are this Mac’s paid dollars per warm hour at demand like now.
      </p>
      <div className="model-evidence-scroll">
        <table>
          <thead>
            <tr>
              <th scope="col">Model</th>
              <th scope="col">Expected now</th>
              <th scope="col">Measured</th>
              <th scope="col">Last measured</th>
              <th scope="col">Confident switch</th>
              <th scope="col" className="shadow-column">
                Shadow estimate <span className="shadow-tag">test</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {list.map((r) => (
              <tr key={r.model}>
                <th scope="row">
                  {shortModel(r.model)}
                  {r.current && <small> · serving</small>}
                </th>
                <td>
                  {r.estimate ? (
                    <>
                      {money(r.estimate.rate)}
                      <small>
                        {' '}
                        {money(r.estimate.lower)}–{money(r.estimate.upper)}
                      </small>
                    </>
                  ) : (
                    <span className="muted">No paid history</span>
                  )}
                </td>
                <td>
                  {r.estimate
                    ? `${num(r.estimate.evidenceHours ?? r.estimate.hours, 1)} h · ${num(r.estimate.days, 0)} day${r.estimate.days === 1 ? '' : 's'}`
                    : '—'}
                </td>
                <td>{r.current ? 'Now' : measured(r.daysSinceMeasured)}</td>
                <td>
                  {r.current ? (
                    '—'
                  ) : r.estimate?.forecastUsable ? (
                    <span className="good-text">Ready</span>
                  ) : (
                    <span className="muted">Needs more</span>
                  )}
                </td>
                <td className="shadow-column">
                  {report ? (
                    <Shadow value={shadow?.[r.model]} />
                  ) : (
                    <span className="muted">
                      {error ? 'Unavailable' : 'Loading…'}
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="footnote shadow-note">
        <strong>Shadow estimate</strong> is a new method BloomGauge is testing: it
        predicts each model’s pay from how busy the network is for it right now,
        including models with little history. It is{' '}
        <strong>not used for switching</strong>; BloomGauge checks it against what
        actually happens. {report ? shadowProgress(report, money) : error}
        {report?.latest && (
          <>
            {' '}
            Updated{' '}
            {new Date(report.latest.at * 1000).toLocaleTimeString([], {
              hour: 'numeric',
              minute: '2-digit',
            })}
            .
          </>
        )}
      </p>
    </div>
  );
}
