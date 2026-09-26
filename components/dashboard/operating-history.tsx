'use client';
import { recordSupportIssue } from '@/lib/support-issues';
import { useState } from 'react';
import {
  validEnergy,
  validTariff,
  validConcurrencyHistory,
  type EnergyData,
  type ConcurrencyData,
  type Tariff,
} from '@/lib/operating-response';
import { useStudy, StudyChart } from './model-research';
import {
  age,
  bounds,
  Choice,
  money,
  num,
  RangePicker,
  shortModel,
  type Range,
} from './shared';

const stamp = (at: number | null | undefined) =>
  at == null
    ? 'Not recorded yet'
    : new Date(at * 1000).toLocaleString([], {
        month: 'short',
        day: 'numeric',
        hour: 'numeric',
        minute: '2-digit',
      });

export function EnergyPanel({ paused }: { paused: boolean }) {
  const [range, setRange] = useState<Range>({ preset: '1h' });
  const [revision, setRevision] = useState(0);
  const [edit, setEdit] = useState<Tariff | null>(null);
  const [rate, setRate] = useState('');
  const [label, setLabel] = useState('');
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState('');
  const { data, error } = useStudy<EnergyData>(
    `/api/energy?v=${revision}`,
    range,
    paused,
    validEnergy,
    10000,
  );
  const b = bounds(range),
    from = data?.from ?? b.start,
    to = data?.to ?? b.end;
  const live = data?.latest,
    fresh = live && Date.now() / 1000 - live.at <= 15 && !error;
  async function save() {
    if (!edit || !rate.trim() || !Number.isFinite(Number(rate))) return;
    setSaving(true);
    setMessage('');
    const control = new AbortController();
    const timer = setTimeout(() => control.abort(), 12000);
    try {
      const res = await fetch('/api/energy/tariff', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Bloom-Action': 'energy-tariff',
        },
        body: JSON.stringify({
          expectedId: edit.id,
          rate: Number(rate),
          label,
        }),
        signal: control.signal,
      });
      const result: unknown = await res.json();
      if (!res.ok || !validTariff(result))
        throw Error(
          result &&
            typeof result === 'object' &&
            'error' in result &&
            typeof result.error === 'string'
            ? result.error
            : 'Could not verify the saved rate. Refresh before trying again.',
        );
      setEdit(null);
      setRevision((v) => v + 1);
      setMessage(
        'Saved for future readings. Earlier costs retain their original rate.',
      );
    } catch (e) {
      recordSupportIssue('action', 'hardware');
      setMessage(
        e instanceof Error
          ? e.message
          : 'Could not save. Refresh before retrying.',
      );
    } finally {
      clearTimeout(timer);
      setSaving(false);
    }
  }
  const c = data?.comparison;
  return (
    <section
      className="panel research-panel"
      aria-label="Energy and electricity cost"
    >
      <div className="panel-heading">
        <div>
          <div className="eyebrow">ENERGY / THIS MAC</div>
          <h2>What it takes to earn.</h2>
        </div>
        <span className={`status-pill ${fresh ? 'good' : 'warn'}`}>
          {paused ? 'View paused' : fresh ? 'Sensor live' : 'Waiting / saved'}
        </span>
      </div>
      <div className="chart-toolbar">
        <RangePicker
          value={range}
          onChange={setRange}
          label="Energy history range"
        />
      </div>
      {error && (
        <p className="notice" role="status">
          {error} {data ? 'Showing saved values.' : ''}
        </p>
      )}
      <div className="research-factor-grid energy-totals">
        <span>
          Internal system power
          <strong>{num(fresh || paused ? live?.watts : null, 1)} W</strong>
          {live
            ? `${live.powerSource} · ${age(live.at)}`
            : 'Waiting for a supported sensor'}
        </span>
        <span>
          Observed energy<strong>{num(data?.totals.kwh, 4)} kWh</strong>
          {num((data?.totals.seconds ?? 0) / 60, 1)} observed minutes
        </span>
        <span>
          Estimated AC cost<strong>{money(data?.totals.costUsd)}</strong>
          {num((data?.totals.acSeconds ?? 0) / 60, 1)} AC minutes
        </span>
        <span>
          Current electricity rate
          <strong>
            {num(data?.tariff.rate != null ? data.tariff.rate * 100 : null, 3)}¢
            / kWh
          </strong>
          <button
            className="text-link"
            disabled={!data || saving}
            onClick={() => {
              if (data) {
                setEdit(data.tariff);
                setRate(
                  data.tariff.rate == null
                    ? ''
                    : String(Number(data.tariff.rate.toFixed(8))),
                );
                setLabel(data.tariff.label);
                setMessage('');
              }
            }}
          >
            Edit rate
          </button>
        </span>
      </div>
      <p className="footnote">
        {data?.tariff.label}. Bill-average pricing allocates fixed charges too;
        this is an estimated cost for the whole Mac, not an exact incremental
        Darkbloom charge or wall-meter measurement.
      </p>
      {edit && (
        <form
          className="energy-rate-form"
          onSubmit={(e) => {
            e.preventDefault();
            void save();
          }}
        >
          <label>
            USD per kWh
            <input
              aria-label="Electricity rate USD per kWh"
              inputMode="decimal"
              type="number"
              min="0"
              max="10"
              step="any"
              required
              value={rate}
              onChange={(e) => setRate(e.target.value)}
            />
          </label>
          <label>
            Rate description
            <input
              maxLength={120}
              required
              value={label}
              onChange={(e) => setLabel(e.target.value)}
            />
          </label>
          <div>
            <button
              type="submit"
              className="secondary-button"
              disabled={saving}
            >
              {saving ? 'Saving…' : 'Save rate'}
            </button>
            <button
              type="button"
              className="text-link"
              disabled={saving}
              onClick={() => setEdit(null)}
            >
              Cancel
            </button>
          </div>
        </form>
      )}
      {message && (
        <p className="notice" role="status">
          {message}
        </p>
      )}
      <p className="footnote">
        Chart: {num((data?.bucketSeconds ?? 60) / 60)}m observed averages. The
        live watts above use the latest sensor reading.
      </p>
      <StudyChart
        data={data?.samples ?? []}
        series={[{ key: 'watts', label: 'System power', color: '#f3c57e' }]}
        from={from}
        to={to}
        unit="W"
        height={230}
        syncId="energy-history"
      />
      <h3>Money and cost · matching observed minutes</h3>
      <div className="research-factor-grid energy-totals">
        <span>
          Confirmed inference
          <strong>{c?.seconds ? money(c.inferenceUsd) : '—'}</strong>
        </span>
        <span>
          Account base rewards
          <strong>{c?.seconds ? money(c.accountBaseUsd) : '—'}</strong>
        </span>
        <span>
          Estimated electricity
          <strong>{c?.seconds ? money(c.costUsd) : '—'}</strong>
        </span>
        <span>
          After estimated electricity<strong>{money(c?.afterCostUsd)}</strong>
        </span>
      </div>
      <StudyChart
        data={data?.samples ?? []}
        series={[
          {
            key: 'inferenceRate',
            label: 'Confirmed inference',
            color: '#82efb5',
          },
          { key: 'costRate', label: 'Estimated electricity', color: '#f3c57e' },
        ]}
        from={from}
        to={to}
        unit="USD / observed hour"
        syncId="energy-history"
      />
      <p className="footnote">
        Only {num((c?.seconds ?? 0) / 60)} complete AC minutes with matching
        covered credits, settled for two minutes, enter the comparison. Quiet
        time counts; missing power or credits do not. This is partial-period
        reporting, not full-period profit. Base rewards remain account-level.
      </p>
      <details className="research-outcomes">
        <summary>Measurement and rate history</summary>
        <p className="footnote">
          {data?.source}. {data?.method} Chart averages need 80% observed
          coverage in each {num((data?.bucketSeconds ?? 60) / 60)}m bucket.
          Recording starts {stamp(data?.coverageStart)}; power history cannot be
          backfilled.
        </p>
        {data?.tariffs.map((t) => (
          <p key={t.id} className="footnote">
            {stamp(t.at)} · {num(t.rate == null ? null : t.rate * 100, 3)}¢/kWh
            · {t.label}
          </p>
        ))}
      </details>
    </section>
  );
}

export function ConcurrencyHistoryPanel({ paused }: { paused: boolean }) {
  const [range, setRange] = useState<Range>({ preset: '1h' });
  const [session, setSession] = useState('');
  const [model, setModel] = useState('');
  const [metric, setMetric] = useState('counts');
  const { data, error } = useStudy<ConcurrencyData>(
    `/api/concurrency-history?session=${session}&model=${encodeURIComponent(model)}`,
    range,
    paused,
    validConcurrencyHistory,
    15000,
  );
  const b = bounds(range),
    from = data?.from ?? b.start,
    to = data?.to ?? b.end;
  const series =
    metric === 'counts'
      ? [
          { key: 'running', label: 'Generating', color: '#82efb5' },
          { key: 'waiting', label: 'Backend waiting', color: '#f3c57e' },
          { key: 'pending', label: 'Coordinator in flight', color: '#b49cff' },
        ]
      : metric === 'limits'
        ? [
            { key: 'providerLimit', label: 'Machine limit', color: '#91bcff' },
            { key: 'slotLimit', label: 'Slot limits', color: '#f3c57e' },
          ]
        : [{ key: 'score', label: 'Network reputation', color: '#82efb5' }];
  return (
    <section className="panel research-panel" aria-label="Concurrency history">
      <div className="panel-heading">
        <div>
          <div className="eyebrow">CONCURRENCY / HISTORY</div>
          <h2>Assignments, queue and capacity.</h2>
        </div>
      </div>
      <div className="chart-toolbar">
        <RangePicker
          value={range}
          onChange={setRange}
          label="Concurrency history range"
        />
        <Choice
          value={session}
          onChange={setSession}
          label="Concurrency session"
          options={[
            { value: '', label: 'All recorded sessions' },
            ...(session && !data?.sessions.some((s) => String(s.id) === session)
              ? [{ value: session, label: `Session ${session}` }]
              : []),
            ...(data?.sessions ?? []).map((s) => ({
              value: String(s.id),
              label: `Session ${s.id} · ${s.models.map(shortModel).join(' + ')}`,
            })),
          ]}
        />
        <Choice
          value={model}
          onChange={setModel}
          label="Concurrency model"
          options={[
            { value: '', label: 'All models' },
            ...[
              ...new Set([...(model ? [model] : []), ...(data?.models ?? [])]),
            ].map((m) => ({ value: m, label: shortModel(m) })),
          ]}
        />
      </div>
      {error && (
        <p className="notice" role="status">
          {error} {data ? 'Showing saved values.' : ''}
        </p>
      )}
      <Choice
        value={metric}
        onChange={setMetric}
        label="Concurrency chart metric"
        options={[
          { value: 'counts', label: 'Concurrent requests' },
          { value: 'limits', label: 'Reported limits' },
          { value: 'reputation', label: 'Network reputation' },
        ]}
      />
      <StudyChart
        data={data?.samples ?? []}
        series={series}
        from={from}
        to={to}
        unit={metric === 'reputation' ? 'score (0–1)' : 'concurrent count'}
        height={230}
        syncId="concurrency-history"
      />
      <p className="footnote">
        {data?.method ??
          'Recording begins with fresh, authenticated, warm-session readings. Missing data is unknown.'}{' '}
        Limits are shown separately from active work.
      </p>
      <p className="footnote">
        {num(data?.observations)} distinct readings ·{' '}
        {num((data?.bucketSeconds ?? 30) / 60, 1)}m sample averages · latest{' '}
        {stamp(data?.latest?.at)}
        {data?.latest
          ? ` · Session ${data.latest.session} · ${data.latest.models.map(shortModel).join(' + ')}`
          : ''}
        . Failure-counter increases: {num(data?.failureIncrements)} across
        adjacent readings in the same session, not lifetime failures or a
        full-period total.
      </p>
      <p className="footnote">
        History starts {stamp(data?.coverageStart)}. A model filter shows its
        backend slots; machine-wide reservations/limits are hidden for
        mixed-model sessions. Network reputation is a cumulative network record,
        not a session score.{' '}
        {data?.truncated
          ? 'Only the latest 100,000 readings in this range are shown. Narrow the range for older detail.'
          : ''}
      </p>
    </section>
  );
}
