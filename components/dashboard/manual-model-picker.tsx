'use client';
import { useEffect, useId, useRef, useState } from 'react';
import { distinctLabels } from '@/lib/model-label';
import { ChevronDown, LoaderCircle, Search } from 'lucide-react';
import { Input } from '@/components/ui/input';
import { startChartPolling } from '@/lib/chart-polling';
import { usePageVisible } from '@/lib/use-page-visibility';
import { money, num, shortModel } from './shared';
import { readModelInsights, type ModelInsights } from '@/lib/model-insights';

export type ManualModelOption = {
  id: string;
  name: string;
  available: boolean;
  reason?: string;
  memoryGB?: number | null;
  loadBudget?: { afterUnloadGB: number; requiredGB: number } | null;
  requiresRuntimeVerification?: boolean;
};

/** Memory fit in plain words: "Fits · 28.7 GB of 35.1 GB free after unloading". */
export function memoryFit(
  model: Pick<ManualModelOption, 'memoryGB' | 'loadBudget'>,
  running: boolean,
) {
  const size = model.memoryGB != null ? `${num(model.memoryGB, 1)} GB` : '';
  const b = model.loadBudget;
  if (!b) return size;
  const free = `${num(b.afterUnloadGB, 1)} GB free${running ? ' after unloading' : ''}`;
  return b.afterUnloadGB >= b.requiredGB
    ? `${size ? `${size} · ` : ''}Fits · needs ${num(b.requiredGB, 1)} of ${free}`
    : `${size ? `${size} · ` : ''}Needs ${num(b.requiredGB - b.afterUnloadGB, 1)} GB more memory (${free})`;
}

/**
 * The model list for manual control: an inline radio list sized for touch
 * (every row is a 56 px+ target), no dialog and no auto-focused search field, so
 * a phone keyboard never covers it. Arrow keys move through the list, Enter or
 * Escape closes it. Choosing only fills the selection; the caller confirms.
 */
export function ManualModelPicker({
  models,
  value,
  currentModel,
  homeModel,
  pinned = false,
  scope = '',
  disabled,
  onChange,
  openRequest = 0,
  actionLabel = 'Switch',
}: {
  models: ManualModelOption[];
  value: string;
  currentModel?: string;
  /** The manager's home model (or pin), marked in the list. */
  homeModel?: string | null;
  pinned?: boolean;
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
  const listId = useId();
  const trigger = useRef<HTMLButtonElement>(null);
  // A change right after a key press came from the keyboard (arrow keys move the
  // choice and keep the list open); any other change is a tap or click, which closes it.
  const keyed = useRef(0);
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
        setError('Earnings history is unavailable. You can still choose.');
        setLoading(false);
      },
    });
  }, [open, visible, ids, timezone, scope, revision]);
  const byId = new Map(data?.models?.map((m) => [m.id, m]) ?? []);
  const selected = models.find((m) => m.id === value);
  const searchable = models.length > 12;
  const choices = models
    .filter(
      (m) =>
        !searchable ||
        `${m.name} ${m.id}`.toLowerCase().includes(search.toLowerCase()),
    )
    // Loadable models first, then the rest with their reasons; order is otherwise kept.
    .sort((a, b) => Number(b.available) - Number(a.available));
  // Catalog names ("Qwen 3.8 27B"), with the quantization added where two collide.
  const names = new Map(models.map((m) => [m.id, m.name]));
  const labels = distinctLabels(
    models.map((m) => m.id),
    (id) => names.get(id) || shortModel(id),
  );
  const running = actionLabel === 'Switch';
  function close(focus = true) {
    setOpen(false);
    if (focus) requestAnimationFrame(() => trigger.current?.focus());
  }
  return (
    <div className={`manual-picker ${open ? 'open' : ''}`}>
      <button
        ref={trigger}
        type="button"
        className="manual-picker-trigger"
        aria-expanded={open}
        aria-controls={listId}
        disabled={disabled && !open}
        onClick={() => setOpen(!open)}
      >
        <span>
          <strong>
            {selected ? labels(selected.id) : 'Choose a model'}
            {selected?.id === currentModel && (
              <span className="manual-picker-badge serving">Serving</span>
            )}
          </strong>
          <small>
            {open
              ? 'Tap a model to choose it'
              : selected
                ? selected.available
                  ? memoryFit(selected, running) || 'Available'
                  : selected.reason || 'Unavailable for this Mac right now'
                : `${models.filter((m) => m.available).length} of ${models.length} models can load now`}
          </small>
        </span>
        <ChevronDown size={18} aria-hidden="true" />
      </button>
      {open && (
        <div className="manual-picker-panel" id={listId}>
          {searchable && (
            <label className="manual-picker-search">
              <Search size={16} aria-hidden="true" />
              <Input
                aria-label="Filter models"
                placeholder="Filter models"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            </label>
          )}
          <fieldset className="manual-picker-rows">
            <legend className="sr-only">Model to run</legend>
            {choices.map((m) => {
              const row = byId.get(m.id);
              const label = labels(m.id);
              const earned = row?.observed.usdPerWarmHour;
              const next = row?.incomeNext8h.usdPerWarmHour;
              const blocked = !m.available;
              return (
                <label
                  key={m.id}
                  className={`manual-picker-row${m.id === value ? ' selected' : ''}${blocked ? ' unavailable' : ''}`}
                >
                  <input
                    type="radio"
                    name={`${listId}-model`}
                    value={m.id}
                    checked={m.id === value}
                    disabled={disabled || blocked}
                    onKeyDown={(event) => {
                      keyed.current = Date.now();
                      if (event.key === 'Enter' || event.key === 'Escape') {
                        event.preventDefault();
                        close();
                      }
                    }}
                    onChange={() => {
                      onChange(m.id);
                      if (Date.now() - keyed.current > 150)
                        requestAnimationFrame(() => close(false));
                    }}
                  />
                  <span className="manual-picker-row-body">
                    <span className="manual-picker-row-name">
                      <strong>{label}</strong>
                      {m.id === currentModel && (
                        <span className="manual-picker-badge serving">
                          Serving now
                        </span>
                      )}
                      {m.id === homeModel && (
                        <span className="manual-picker-badge home">
                          {pinned ? 'Your pick' : 'Home'}
                        </span>
                      )}
                    </span>
                    <small>
                      {blocked
                        ? m.reason || 'Unavailable for this Mac right now.'
                        : m.requiresRuntimeVerification
                          ? `${memoryFit(m, running) || 'Available'} · verified when you ${actionLabel.toLowerCase()}`
                          : memoryFit(m, running) || 'Available'}
                    </small>
                    {(earned != null || next != null) && (
                      <small className="manual-picker-row-money">
                        {earned != null
                          ? `Earned here ${money(earned)} / warm h`
                          : 'No earnings history here'}
                        {next != null
                          ? ` · next 8 h about ${money(next)} / warm h`
                          : ''}
                      </small>
                    )}
                  </span>
                </label>
              );
            })}
            {!choices.length && (
              <p className="empty">No models match this filter.</p>
            )}
          </fieldset>
          <p className="manual-picker-note" role="status">
            {loading && !data ? (
              <>
                <LoaderCircle size={13} className="spin" /> Loading earnings
                history…
              </>
            ) : error ? (
              <>
                {error}{' '}
                <button
                  type="button"
                  className="text-link"
                  onClick={() => setRevision((n) => n + 1)}
                >
                  Retry
                </button>
              </>
            ) : (
              'Earnings are this Mac’s paid history per warm hour over 28 days; the next-8-hour figure is an estimate if the model stays warm.'
            )}
          </p>
          <button
            type="button"
            className="manual-picker-done"
            onClick={() => close()}
          >
            Done
          </button>
        </div>
      )}
    </div>
  );
}
