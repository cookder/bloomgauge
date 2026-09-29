'use client';
import { memo, useEffect, useMemo, useState } from 'react';
import { ArrowRight } from 'lucide-react';
import { startChartPolling } from '@/lib/chart-polling';
import { modelColor } from '@/lib/model-earnings';
import { usePageVisible } from '@/lib/use-page-visibility';
import { useAppNavigation, useScreenActive } from './app-navigation';
import { requestDemandTrends } from './model-research';
import { readStatusJSON } from '@/lib/connection-status';
import { sharedGet } from '@/lib/shared-get';
import { readOptimizerLive, type OptimizerLive } from '@/lib/optimizer-live';
import { glanceModels, glanceReasonLabel } from '@/lib/demand-glance';
import { bounds, shortModel, TimeChart } from './shared';

type Point = { at: number; load: number | null };
type Demand = { id: string; averageLoad: number; chart: Point[] };
type History = { models: Demand[]; bucketSeconds: number };

const glanceRanges = [
  { preset: '1h', label: '1 h' },
  { preset: '24h', label: '24 h' },
  { preset: '7d', label: '7 d' },
  { preset: '30d', label: '30 d' },
] as const;

/** Main-dashboard summary of network demand: top three models, one tap to change the range. */
export const DemandGlance = memo(function DemandGlance({
  paused,
}: {
  paused: boolean;
}) {
  const [preset, setPreset] = useState<string>(() => {
    try {
      return localStorage.getItem('bloom.demand-glance.range') || '24h';
    } catch {
      return '24h';
    }
  });
  const [saved, setSaved] = useState<{ preset: string; data: History } | null>(
    null,
  );
  const [failure, setFailure] = useState<{
    preset: string;
    message: string;
  } | null>(null);
  const [live, setLive] = useState<OptimizerLive | null>(null);
  const active = useScreenActive();
  const pageVisible = usePageVisible();
  const { navigate } = useAppNavigation();
  const data = saved?.preset === preset ? saved.data : null;
  const error = failure?.preset === preset ? failure.message : '';

  useEffect(() => {
    if (!active || !pageVisible || (paused && saved?.preset === preset)) return;
    return startChartPolling({
      load: async (signal) => {
        const b = bounds({ preset });
        const response = await fetch(
          `/api/network/models?from=${b.start}&to=${b.end}`,
          { signal, cache: 'no-store' },
        );
        if (!response.ok)
          throw new Error(
            `Demand history is unavailable (${response.status}).`,
          );
        const next = (await response.json()) as History;
        if (
          !next ||
          !Array.isArray(next.models) ||
          !next.models.every(
            (m) => m && typeof m.id === 'string' && Array.isArray(m.chart),
          )
        )
          throw new Error('Demand history returned an incomplete response.');
        return next;
      },
      onValue: (next) => {
        setSaved({ preset, data: next });
        setFailure(null);
      },
      onError: (e) => setFailure({ preset, message: e.message }),
      intervalMs: 60000,
      repeat: !paused,
    });
  }, [preset, paused, active, pageVisible]);

  // What the optimizer is serving and weighing; without it the card shows the busiest three.
  useEffect(() => {
    if (!active || !pageVisible) return;
    return startChartPolling({
      load: async (signal) =>
        readOptimizerLive(
          await sharedGet('/api/optimizer/live', 20000, signal, (r) =>
            readStatusJSON(r, 'Optimizer status'),
          ),
        ),
      onValue: setLive,
      onError: () => setLive(null),
      intervalMs: 60000,
      repeat: !paused,
    });
  }, [paused, active, pageVisible]);

  function choose(next: string) {
    setPreset(next);
    try {
      localStorage.setItem('bloom.demand-glance.range', next);
    } catch {
      /* Range memory is a convenience. */
    }
  }

  const picks = useMemo(
    () => glanceModels(data?.models ?? [], live),
    [data, live],
  );
  const top = useMemo(
    () => picks.map((p) => data!.models.find((m) => m.id === p.id)!),
    [picks, data],
  );
  const series = picks.map((p, i) => ({
    key: `model${i}`,
    label:
      p.reason === 'busiest'
        ? shortModel(p.id)
        : `${shortModel(p.id)} · ${glanceReasonLabel[p.reason]}`,
    color: modelColor(p.id),
  }));
  const chart = useMemo(() => {
    const points = new Map<
      number,
      { at: number; [key: string]: number | null }
    >();
    top.forEach((m, i) =>
      m.chart.forEach((point) => {
        const row = points.get(point.at) ?? { at: point.at };
        row[`model${i}`] = point.load;
        points.set(point.at, row);
      }),
    );
    return [...points.values()].sort((a, b) => a.at - b.at);
  }, [top]);

  return (
    <section
      className="panel demand-glance"
      aria-labelledby="demand-glance-title"
    >
      <div className="demand-glance-head">
        <div>
          <div className="eyebrow">MODEL DEMAND</div>
          <h2 id="demand-glance-title">
            Network demand for the models that matter
          </h2>
        </div>
        <div
          className="demand-glance-ranges"
          role="group"
          aria-label="Demand time range"
        >
          {glanceRanges.map((r) => (
            <button
              key={r.preset}
              type="button"
              aria-pressed={preset === r.preset}
              onClick={() => choose(r.preset)}
            >
              {r.label}
            </button>
          ))}
        </div>
      </div>
      {top.length ? (
        <>
          <TimeChart
            key={`${preset}:${top.map((m) => m.id).join('|')}`}
            data={chart}
            series={series}
            height={180}
            precision={0}
          />
          <ul className="demand-glance-legend" aria-label="Chart lines">
            {series.map((s) => (
              <li key={s.key}>
                <i style={{ background: s.color }} aria-hidden="true" />
                {s.label}
              </li>
            ))}
          </ul>
        </>
      ) : (
        <div className="empty">
          {error ||
            (data
              ? 'No demand recorded in this range yet.'
              : 'Loading demand…')}
        </div>
      )}
      <div className="demand-glance-foot">
        <span className="footnote">
          Active + queued requests
          {data
            ? `, averaged every ${Math.round(data.bucketSeconds / 60) || 1} min`
            : ''}
          . The three busiest, plus any model BloomGauge is serving, trialling or
          considering.
        </span>
        <button
          type="button"
          className="text-link"
          onClick={() => {
            requestDemandTrends();
            navigate('demand');
          }}
        >
          All models and metrics <ArrowRight size={14} aria-hidden="true" />
        </button>
      </div>
    </section>
  );
});
