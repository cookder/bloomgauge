'use client';
import { useEffect, useState } from 'react';
import { ArrowRight, RefreshCw } from 'lucide-react';
import { startChartPolling } from '@/lib/chart-polling';
import { usePageVisible } from '@/lib/use-page-visibility';
import { readStatusJSON } from '@/lib/connection-status';
import { forgetShared, sharedGet } from '@/lib/shared-get';
import {
  readOptimizerLive,
  optimizerLiveGroups,
  type OptimizerLive,
  type OptimizerCandidate,
} from '@/lib/optimizer-live';
import { loadStrategy, type Strategy } from '@/lib/optimizer-manager';
import { requestOptimizerDetails } from '@/lib/optimizer-details-intent';
import { useAppNavigation } from './app-navigation';
import { age, money, num, shortModel } from './shared';
import { ConfirmationProgress } from './optimizer-confirmation';
const phases = {
  off: 'Manual',
  waiting: 'Waiting',
  watching: 'Watching',
  confirming: 'Confirming a switch',
  switching: 'Switching',
  measuring: 'Measuring a trial',
};
const managerPhases: Partial<Record<keyof typeof phases, string>> = {
  off: 'Off',
  watching: 'Manager on',
};

export function OptimizerLivePanel({ active }: { active: boolean }) {
  const visible = usePageVisible(),
    navigation = useAppNavigation();
  const [data, setData] = useState<OptimizerLive | null>(null),
    [strategy, setStrategy] = useState<Strategy | null>(null),
    [error, setError] = useState(''),
    [revision, setRevision] = useState(0),
    [received, setReceived] = useState(0),
    [now, setNow] = useState(Date.now() / 1000);
  useEffect(() => {
    if (!active || !visible) return;
    const timer = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(timer);
  }, [active, visible]);
  useEffect(() => {
    if (!active || !visible) return;
    return startChartPolling({
      intervalMs: 30000,
      timeoutMs: 10000,
      load: async (signal) => {
        const [live, strategy] = await Promise.all([
          sharedGet('/api/optimizer/live', 20000, signal, (r) =>
            readStatusJSON(r, 'Optimizer status'),
          ),
          loadStrategy(sharedGet, signal),
        ]);
        return { live: readOptimizerLive(live), strategy };
      },
      onValue: ({ live: value, strategy }) => {
        setStrategy(strategy);
        setData((old) => (old && old.at > value.at ? old : value));
        setReceived(Date.now() / 1000);
        setNow(Date.now() / 1000);
        setError('');
      },
      onError: (error) =>
        setError(
          error instanceof TypeError
            ? 'Optimizer connection is unavailable. Retrying automatically.'
            : error.message,
        ),
    });
  }, [active, visible, revision]);
  const managed = strategy === 'manager';
  const transportFresh =
    !!data && !error && now - data.at <= 45 && now - data.at >= -5;
  const fresh = transportFresh && data.fresh;
  const groups = optimizerLiveGroups(fresh ? data.candidates : []);
  const confirmation =
    transportFresh &&
    data?.mode === 'demand' &&
    data.confirmation &&
    data.phase !== 'switching' &&
    data.phase !== 'measuring'
      ? data.confirmation
      : null;
  const confirmationLabel =
    confirmation?.expiresAt != null && now >= confirmation.expiresAt
      ? 'Waiting for fresh status'
      : confirmation?.status === 'paused'
        ? 'Confirmation paused'
        : confirmation?.status === 'ready'
          ? 'Earnings confirmed'
          : null;
  const target = transportFresh
    ? data?.phase === 'switching'
      ? data.pendingTarget
      : confirmation
        ? data?.proposalTarget
        : null
    : null;
  const remaining = Math.max(0, Math.ceil(30 - (now - received)));
  function details() {
    requestOptimizerDetails();
    navigation.navigate('test');
  }
  function candidate(row: OptimizerCandidate, index?: number) {
    return (
      <li key={row.model} className="optimizer-live-candidate">
        <div>
          <span className="optimizer-live-candidate-name">
            {index != null && <b>{index}</b>}
            {shortModel(row.model)}
          </span>
          <strong>
            {row.netGainUsd == null
              ? 'Unrated'
              : `${row.netGainUsd > 0 ? '+' : ''}${money(row.netGainUsd)}`}
          </strong>
        </div>
        <small>
          {row.group === 'comparison_target'
            ? row.selectionReason
              ? row.selectionReason.replaceAll('_', ' ')
              : row.kind === 'explore'
                ? 'Trial candidate'
                : 'Optimizer comparison'
            : row.group === 'paid_alternative'
              ? 'Eligible earnings comparison'
              : row.firstBlocker || 'Collecting comparable paid history'}
        </small>
      </li>
    );
  }
  return (
    <section className="panel optimizer-live-panel" aria-label="Live optimizer">
      <div className="optimizer-live-heading">
        <div>
          <div className="eyebrow">OPTIMIZER</div>
          <h2>{managed ? 'Model comparisons' : 'Next candidates'}</h2>
        </div>
        <span className="optimizer-phase-label">
          {transportFresh && visible
            ? (confirmationLabel ??
              (managed ? managerPhases[data.phase] : null) ??
              phases[data.phase])
            : 'Details updating'}
        </span>
      </div>
      <div className="optimizer-live-current">
        <span>
          {target
            ? `${data?.phase === 'switching' ? 'Switching to' : confirmation?.status === 'ready' ? 'Preferred' : 'Checking'} ${shortModel(target)}`
            : data?.currentModel
              ? `${transportFresh ? 'Current selection' : 'Last confirmed selection'} ${shortModel(data.currentModel)}`
              : 'Reading the current model'}
        </span>
        {fresh && data.currentPaidBasis && (
          <small>
            {money(
              data.currentPaidBasis.liveGuardUsdPerHour ??
                data.currentPaidBasis.recentGuardUsdPerHour ??
                data.currentPaidBasis.meanUsdPerWarmHour,
            )}{' '}
            / hr ·{' '}
            {data.currentPaidBasis.liveGuardUsdPerHour != null ||
            data.currentPaidBasis.recentGuardUsdPerHour != null ||
            data.currentPaidBasis.basis === 'recent_paid'
              ? 'recent paid pace'
              : data.currentPaidBasis.basis === 'matched_history'
                ? 'matched paid history'
                : 'limited paid history'}
          </small>
        )}
        {(!confirmation || error || data?.reason !== confirmation.reason) && (
          <p role="status">
            {error ||
              data?.reason ||
              'Waiting for the optimizer’s first reading…'}
          </p>
        )}
        {transportFresh && data?.measurement && data.phase === 'measuring' && (
          <small>
            {num(data.measurement.warmSeconds / 60, 1)} /{' '}
            {num(data.measurement.trialMinutes, 0)} warm minutes measured
          </small>
        )}
        {confirmation && (
          <ConfirmationProgress value={confirmation} now={now} />
        )}
      </div>
      {fresh ? (
        <div className="optimizer-live-rankings">
          {managed && (
            <p className="optimizer-live-caveat">
              For reference: the manager doesn’t switch on these. It holds its
              home model and moves only on strong network evidence.
            </p>
          )}
          <p className="optimizer-live-caveat">
            {data.planningMinutes != null
              ? `Switching advantage · next ${num(data.planningMinutes / 60, 1)}h`
              : 'Switching advantage'}{' '}
            · negative means less than staying
          </p>
          {!!groups.target.length && (
            <div>
              <h3>Optimizer candidate</h3>
              <ul>{groups.target.map((r) => candidate(r))}</ul>
              <p className="optimizer-live-caveat">
                {target
                  ? 'Checks must finish before the switch proceeds.'
                  : 'Informational choice; no switch is queued.'}
              </p>
            </div>
          )}
          {!!groups.paid.length && (
            <div>
              <h3>Eligible earnings comparisons</h3>
              <ol>{groups.paid.map((r, i) => candidate(r, i + 1))}</ol>
            </div>
          )}
          {!groups.target.length && !groups.paid.length && (
            <p className="optimizer-live-no-pick">
              {data.phase === 'waiting'
                ? 'Last comparison: no switch qualified.'
                : 'No switch currently qualifies.'}
            </p>
          )}
          {!!groups.held.length && (
            <div>
              <h3>On hold</h3>
              <ul>{groups.held.slice(0, 3).map((r) => candidate(r))}</ul>
              {groups.held.length > 3 && (
                <details>
                  <summary>{groups.held.length - 3} more held models</summary>
                  <ul>{groups.held.slice(3).map((r) => candidate(r))}</ul>
                </details>
              )}
            </div>
          )}
          {!!groups.unknown.length && (
            <details>
              <summary>
                {groups.unknown.length}{' '}
                {groups.unknown.length === 1 ? 'model needs' : 'models need'}{' '}
                more evidence
              </summary>
              <ul>{groups.unknown.map((r) => candidate(r))}</ul>
            </details>
          )}
          <p className="optimizer-live-caveat">
            {data.planningMinutes != null
              ? `Amounts: cautious gain over ${num(data.planningMinutes / 60, 1)}h versus staying, after loading.`
              : 'Amounts compare switching with staying.'}{' '}
            Held models are unranked.
          </p>
        </div>
      ) : (
        <p className="optimizer-live-empty">
          {data?.phase === 'off'
            ? managed
              ? 'Automatic control is off. Model choices are yours.'
              : 'Manual mode leaves model choices to you. Enable the optimizer to follow candidates.'
            : data
              ? 'Candidate comparisons are waiting for fresh, matching readings.'
              : 'Candidate rankings will appear after a verified evaluation.'}
        </p>
      )}
      <div className="optimizer-live-footer">
        <small>
          {data?.lastComparisonAt != null
            ? `Compared ${age(data.lastComparisonAt, now).toLowerCase()} · `
            : ''}
          {error
            ? 'Refresh delayed'
            : received
              ? `Refresh ${remaining}s`
              : 'Refreshes every 30s'}
        </small>
        <button
          type="button"
          aria-label="Refresh optimizer status"
          title="Refresh display"
          onClick={() => {
            forgetShared('/api/optimizer/live');
            setRevision((n) => n + 1);
          }}
        >
          <RefreshCw size={14} />
        </button>
      </div>
      <button type="button" className="optimizer-live-open" onClick={details}>
        Open full optimizer
        <ArrowRight size={16} aria-hidden="true" />
      </button>
    </section>
  );
}
