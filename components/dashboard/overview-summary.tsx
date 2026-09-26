'use client';
import { useEffect, useState } from 'react';
import { startChartPolling } from '@/lib/chart-polling';
import { readStatusJSON } from '@/lib/connection-status';
import { readOptimizerLive } from '@/lib/optimizer-live';
import { overviewSummary, todayTotal } from '@/lib/overview-summary';
import { sharedGet } from '@/lib/shared-get';
import type { EarningsPulseData } from '@/lib/earnings-pulse';
import { money, shortModel } from './shared';

export function OverviewSummary({
  active,
  live,
  online,
  model,
  pulse,
  hours,
  now,
}: {
  active: boolean;
  live: boolean;
  online: boolean | null;
  model: string | null;
  pulse?: EarningsPulseData;
  hours?: { at: number; usd: number }[];
  now: number;
}) {
  const [phase, setPhase] = useState<string | null>(null);
  useEffect(() => {
    if (!active) return;
    return startChartPolling({
      load: async (signal) =>
        readOptimizerLive(
          await sharedGet('/api/optimizer/live', 20000, signal, (r) =>
            readStatusJSON(r, 'Optimizer status'),
          ),
        ),
      onValue: (value) => setPhase(value.phase),
      onError: () => setPhase(null),
      intervalMs: 30000,
    });
  }, [active]);
  const pace =
    live && pulse?.status === 'live'
      ? (pulse.windows?.['300']?.ratePerHour ?? null)
      : null;
  const text = overviewSummary({
    online: live ? online : null,
    model,
    pace,
    today: todayTotal(hours, now),
    phase,
    money,
    name: shortModel,
  });
  return text ? (
    <p className="overview-summary" role="status">
      {text}
    </p>
  ) : null;
}
