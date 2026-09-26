'use client';
import { useEffect, useState } from 'react';
import { distinctLabels } from '@/lib/model-label';
import { Check, ChevronDown, LoaderCircle, Search } from 'lucide-react';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { startChartPolling } from '@/lib/chart-polling';
import { usePageVisible } from '@/lib/use-page-visibility';
import { money, num, shortModel, age } from './shared';
import { readModelInsights, type ModelInsights } from '@/lib/model-insights';

export type ManualModelOption = {
  id: string;
  name: string;
  available: boolean;
  reason?: string;
  memoryGB?: number | null;
  requiresRuntimeVerification?: boolean;
};
const dollars = (value: number | null | undefined) =>
  value == null ? 'Not enough history' : money(value);

export function ManualModelPicker({
  models,
  value,
  currentModel,
  scope = '',
  disabled,
  onChange,
  openRequest = 0,
  actionLabel = 'Switch',
}: {
  models: ManualModelOption[];
  value: string;
  currentModel?: string;
  scope?: string;
  disabled: boolean;
  onChange: (model: string) => void;
  openRequest?: number;
  actionLabel?: 'Start' | 'Switch';
}) {
  const [open, setOpen] = useState(false),
    [search, setSearch] = useState(''),
    [data, setData] = useState<ModelInsights | null>(null);
  const [error, setError] = useState(''),
    [revision, setRevision] = useState(0),
    [loading, setLoading] = useState(false);
  const visible = usePageVisible();
  const ids = JSON.stringify(models.map((m) => m.id).sort());
  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  useEffect(() => {
    if (openRequest > 0) setOpen(true);
  }, [openRequest]);
  useEffect(() => {
    setData(null);
    setError('');
  }, [ids, timezone, scope]);
  useEffect(() => {
    if (!open || !visible || !models.length) return;
    setLoading(true);
    return startChartPolling({
      intervalMs: 60000,
      timeoutMs: 20000,
      load: async (signal) => {
        const params = new URLSearchParams({ timezone });
        for (const id of JSON.parse(ids)) params.append('model', id);
        const response = await fetch(`/api/model-insights?${params}`, {
          cache: 'no-store',
          signal,
        });
        if (!response.ok)
          throw new Error(
            'Model statistics are unavailable. You can still choose a model.',
          );
        return readModelInsights(
          await response.json(),
          timezone,
          JSON.parse(ids),
        );
      },
      onValue: (value) => {
        setData((previous) =>
          previous && previous.at > value.at ? previous : value,
        );
        setError('');
        setLoading(false);
      },
      onError: () => {
        setError(
          'Model statistics are unavailable. You can still choose a model.',
        );
        setLoading(false);
      },
    });
  }, [open, visible, ids, timezone, scope, revision]);
  const byId = new Map(data?.models?.map((m) => [m.id, m]) ?? []);
  const selected = models.find((m) => m.id === value);
  const choices = models.filter((m) =>
    `${m.name} ${m.id}`.toLowerCase().includes(search.toLowerCase()),
  );
  const labels = distinctLabels(
    choices.map((m) => m.id),
    shortModel,
  );
  const time = (at: number) =>
    new Date(at * 1000).toLocaleTimeString([], {
      hour: 'numeric',
      minute: '2-digit',
    });
  return (
    <>
      <Button
        type="button"
        variant="outline"
        className="manual-picker-trigger"
        aria-label="Manual serving model"
        aria-haspopup="dialog"
        aria-expanded={open}
        disabled={disabled}
        onClick={() => setOpen(true)}
      >
        <span>
          {selected?.name || 'Choose a model'}
          <small>Compare earnings & next 8 hours</small>
        </span>
        <ChevronDown size={16} />
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="manual-model-picker-dialog">
          <DialogHeader>
            <DialogTitle>Choose a model</DialogTitle>
            <DialogDescription>
              Compare this Mac’s paid history and the next eight hours. Choosing
              a row fills the selection; use {actionLabel} to apply it.
            </DialogDescription>
          </DialogHeader>
          <div className="manual-picker-toolbar">
            <label>
              <Search size={16} />
              <Input
                aria-label="Search models"
                placeholder="Search models"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            </label>
            <span>
              {data
                ? `${time(data.horizon.from)}–${time(data.horizon.to)} · ${data.timezone}`
                : 'Next 8 hours · ' + timezone}
            </span>
          </div>
          <div className="manual-picker-stat-status" role="status">
            {loading && !data ? (
              <>
                <LoaderCircle size={14} className="spin" />
                Loading model statistics…
              </>
            ) : error ? (
              <>
                {data ? 'Showing saved statistics. ' : ''}
                {error}
                <button
                  type="button"
                  className="text-link"
                  onClick={() => setRevision((n) => n + 1)}
                >
                  Retry statistics
                </button>
              </>
            ) : data ? (
              `${Date.now() / 1000 - data.at > 90 ? 'Saved statistics · ' : ''}Updated ${age(data.at).toLowerCase()}`
            ) : (
              'Choose any model while statistics load.'
            )}
          </div>
          <div
            className="manual-picker-list"
            aria-label="Models and statistics"
          >
            {choices.map((m) => {
              const row = byId.get(m.id);
              const label = labels(m.id);
              return (
                <button
                  type="button"
                  className={`manual-picker-option ${m.id === value ? 'selected' : ''}`}
                  key={m.id}
                  aria-label={`Choose ${label}`}
                  disabled={disabled}
                  onClick={() => {
                    onChange(m.id);
                    setOpen(false);
                  }}
                >
                  <span className="manual-picker-model">
                    <strong>
                      {label}
                      {m.id === value && <Check size={15} />}
                    </strong>
                    <small>
                      {m.id === currentModel ? 'Current · ' : ''}
                      {m.available
                        ? m.requiresRuntimeVerification
                          ? 'Verifies on ' + actionLabel.toLowerCase()
                          : 'Available'
                        : 'Unavailable'}
                      {m.memoryGB != null ? ` · ${num(m.memoryGB, 1)} GB` : ''}
                    </small>
                    {!m.available && (
                      <small>
                        {m.reason || 'Unavailable for this Mac right now.'}
                      </small>
                    )}
                  </span>
                  <span className="manual-picker-metrics">
                    <span title={row?.observed.reason}>
                      <small>Observed / warm hour</small>
                      <strong>{dollars(row?.observed.usdPerWarmHour)}</strong>
                      <small>
                        {row
                          ? `${num(row.observed.warmHours, 1)} warm hours · ${row.observed.days} days`
                          : 'History unavailable'}
                      </small>
                      {row?.observed.asOf != null && (
                        <small>
                          Last evidence {age(row.observed.asOf).toLowerCase()}
                        </small>
                      )}
                    </span>
                    <span>
                      <small>Average request · output</small>
                      <strong>
                        {row?.requestSize.meanOutputTokens == null
                          ? 'Not enough history'
                          : `${num(row.requestSize.meanOutputTokens)} tokens`}
                      </strong>
                      <small>
                        {row
                          ? `${num(row.requestSize.outputSamples)} of ${num(row.requestSize.creditedRequests)} requests measured`
                          : 'Reported output tokens'}
                      </small>
                    </span>
                    <span title={row?.demandNext8h.reason}>
                      <small>Next 8h · network load</small>
                      <strong>
                        {row?.demandNext8h.meanConcurrentRequests == null
                          ? 'Not enough history'
                          : num(row.demandNext8h.meanConcurrentRequests, 1)}
                      </strong>
                      <small>
                        {row?.demandNext8h.status === 'partial'
                          ? `${num(row.demandNext8h.supportedSeconds / 3600, 1)} of 8h supported`
                          : 'Active + queued · historical'}
                      </small>
                    </span>
                    <span title={row?.incomeNext8h.reason}>
                      <small>Next 8h · estimate / warm hr</small>
                      <strong>
                        {dollars(row?.incomeNext8h.usdPerWarmHour)}
                      </strong>
                      <small>
                        {row?.incomeNext8h.usdPerWarmHour == null
                          ? row?.incomeNext8h.reason ||
                            'Waiting for supported history'
                          : 'If warm throughout · conditional'}
                      </small>
                    </span>
                  </span>
                </button>
              );
            })}
            {!choices.length && (
              <p className="empty">No models match this search.</p>
            )}
          </div>
          <p className="manual-picker-footnote">
            History covers 28 days. Earnings include warm idle time and exclude
            base rewards and cold/loading time. Network load is concurrent
            requests across the network, not work guaranteed to this Mac.
            Estimates use comparable historical hours; missing evidence stays
            unavailable.
          </p>
        </DialogContent>
      </Dialog>
    </>
  );
}
