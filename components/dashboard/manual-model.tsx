'use client';
import { useEffect, useId, useRef, useState } from 'react';
import {
  ArrowRightLeft,
  LoaderCircle,
  RefreshCw,
  Play,
  Square,
  X,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { recordSupportIssue } from '@/lib/support-issues';
import {
  ResponseValidationError,
  startChartPolling,
} from '@/lib/chart-polling';
import { usePageVisible } from '@/lib/use-page-visibility';
import { readStatusJSON } from '@/lib/connection-status';
import {
  validManualControl,
  manualControlFresh,
  currentManualWarmup,
  manualBlocker,
  manualRequest,
  manualRequestObserved,
  manualRequestKey,
  readManualRequest,
  type ManualControl,
  type ManualRequest,
} from '@/lib/manual-control';
import { useScreenActive, useAppNavigation } from './app-navigation';
import { shortModel, num } from './shared';
import { ModelWarmup } from './model-warmup';
import { CacheRecoveryPermission } from './cache-recovery-permission';
import { ManualModelPicker, memoryFit } from './manual-model-picker';
import { withoutCircularHint } from '@/lib/optimizer-manager';

export function ManualModelControl({
  paused = false,
  embedded = false,
  connectionError = '',
  pickerRequest = 0,
  managed = false,
  homeModel,
  pinned = false,
  holdReason = '',
}: {
  paused?: boolean;
  embedded?: boolean;
  connectionError?: string;
  pickerRequest?: number;
  /** The manager is on: a pick becomes the pinned model and automatic control continues. */
  managed?: boolean;
  homeModel?: string | null;
  pinned?: boolean;
  /** Why a pick must wait (e.g. the manager is still turning on). */
  holdReason?: string;
}) {
  const navigation = useAppNavigation(),
    visible = usePageVisible(),
    active = useScreenActive();
  const explanationId = useId();
  const [data, setData] = useState<ManualControl | null>(null),
    [model, setModel] = useState('');
  const [busy, setBusy] = useState(false),
    [uncertain, setUncertain] = useState(false);
  const [loadError, setLoadError] = useState(''),
    [actionError, setActionError] = useState('');
  const [revision, setRevision] = useState(0),
    [clock, setClock] = useState(Date.now() / 1000);
  const [stopConfirmation, setStopConfirmation] =
    useState<ManualRequest | null>(null);
  const [ownRequestId, setOwnRequestId] = useState('');
  // A model change is confirmed before it is sent.
  const [confirming, setConfirming] = useState(false);
  const [permissionBusy, setPermissionBusy] = useState(false);
  const pending = useRef(false),
    epoch = useRef(0),
    lastReading = useRef(0),
    chosen = useRef(false);
  const retry = useRef<ManualRequest | null>(null);
  function remember(request: ManualRequest | null) {
    retry.current = request;
    setUncertain(!!request);
    try {
      if (request)
        localStorage.setItem(manualRequestKey, JSON.stringify(request));
      else localStorage.removeItem(manualRequestKey);
    } catch {
      /* Matching server receipts still reconcile this tab. */
    }
    window.dispatchEvent(new Event('bloom-manual-request'));
  }
  function accept(value: ManualControl) {
    if (!manualControlFresh(value)) {
      setLoadError('Waiting for fresh model status. Retrying automatically.');
      return;
    }
    if (value.at < lastReading.current) return;
    lastReading.current = value.at;
    setData(value);
    setLoadError('');
    setClock(Date.now() / 1000);
    if (!chosen.current) {
      const initial =
        value.queuedModel ||
        value.switchingModel ||
        value.providerControl.model ||
        value.currentModel;
      if (initial && value.models.some((m) => m.id === initial)) {
        setModel(initial);
        chosen.current = true;
      }
    }
    if (retry.current && manualRequestObserved(value, retry.current)) {
      setOwnRequestId(retry.current.requestId);
      remember(null);
      setActionError('');
      setStopConfirmation(null);
      setConfirming(false);
    }
  }
  useEffect(() => {
    function restore() {
      try {
        const saved = readManualRequest(localStorage.getItem(manualRequestKey));
        retry.current = saved;
        setUncertain(!!saved);
        if (saved) {
          setOwnRequestId(saved.requestId);
          if (saved.action === 'select') {
            setModel(saved.model);
            chosen.current = true;
          }
        }
      } catch {
        /* Optional browser storage. */
      }
    }
    restore();
    window.addEventListener('bloom-manual-request', restore);
    window.addEventListener('storage', restore);
    return () => {
      window.removeEventListener('bloom-manual-request', restore);
      window.removeEventListener('storage', restore);
    };
  }, [active]);
  useEffect(() => {
    if (!active || !visible) return;
    const timer = setInterval(() => setClock(Date.now() / 1000), 1000);
    return () => clearInterval(timer);
  }, [active, visible]);
  useEffect(() => {
    if (!active || !visible) return;
    return startChartPolling({
      issueContext: 'models',
      intervalMs: 2000,
      timeoutMs: 10000,
      load: async (signal) => {
        const own = epoch.current;
        const response = await fetch('/api/model-control', {
          cache: 'no-store',
          signal,
        });
        const value = await readStatusJSON(response, 'Model status');
        if (!validManualControl(value))
          throw new ResponseValidationError(
            'model-controls',
            'Waiting for a complete model status. Retrying automatically.',
          );
        return { own, value };
      },
      onValue: ({ own, value }) => {
        if (!pending.current && own === epoch.current) accept(value);
      },
      onError: (error) => {
        if (!pending.current)
          setLoadError(
            error instanceof TypeError
              ? 'Model status is delayed. Retrying automatically.'
              : error.message,
          );
      },
    });
  }, [active, visible, revision]);

  async function send(
    request: ManualRequest | { action: 'cancel'; requestId: string },
  ) {
    if (pending.current) return;
    pending.current = true;
    epoch.current++;
    setBusy(true);
    setActionError('');
    if (request.action !== 'cancel') {
      remember(request);
      setOwnRequestId(request.requestId);
    }
    try {
      const response = await fetch('/api/model-control', {
        method: 'POST',
        signal: AbortSignal.timeout(15000),
        headers: {
          'Content-Type': 'application/json',
          'X-Bloom-Action': 'manual-model',
        },
        body: JSON.stringify(request),
      });
      const value: unknown = await response.json();
      if (!response.ok) {
        // A structured client rejection is definitive. Server/transport failures
        // can happen after admission, so retain the exact UUID and target.
        if (response.status >= 400 && response.status < 500) {
          remember(null);
          setStopConfirmation(null);
        }
        throw Error(
          value &&
            typeof value === 'object' &&
            'error' in value &&
            typeof value.error === 'string'
            ? value.error
            : 'Could not complete the model command. Checking its status.',
        );
      }
      if (!validManualControl(value))
        throw Error(
          'The reply was incomplete. Checking whether your request was accepted…',
        );
      accept(value);
    } catch (error) {
      recordSupportIssue('action', 'models');
      setActionError(
        error instanceof Error &&
          !['AbortError', 'TimeoutError'].includes(error.name)
          ? withoutCircularHint(error.message)
          : 'The reply was interrupted. Checking the original request before another command.',
      );
    } finally {
      pending.current = false;
      epoch.current++;
      setBusy(false);
      setConfirming(false);
      setRevision((n) => n + 1);
    }
  }
  async function refreshStatus() {
    if (pending.current) return;
    if (
      retry.current ||
      data?.providerControl.operationPending ||
      data?.queuedModel ||
      data?.switching ||
      (data?.selectionResult &&
        ['queued', 'working'].includes(data.selectionResult.status))
    ) {
      setRevision((n) => n + 1);
      return;
    }
    pending.current = true;
    epoch.current++;
    setBusy(true);
    try {
      const response = await fetch('/api/model-control', {
        method: 'POST',
        signal: AbortSignal.timeout(10000),
        headers: {
          'Content-Type': 'application/json',
          'X-Bloom-Action': 'manual-model',
        },
        body: JSON.stringify({ action: 'refresh' }),
      });
      const value: unknown = await response.json();
      if (!response.ok || !validManualControl(value))
        throw Error(
          'Waiting for a complete model status. Retrying automatically.',
        );
      accept(value);
      setActionError('');
    } catch (error) {
      setLoadError(
        error instanceof Error
          ? error.message
          : 'Model status is delayed. Retrying automatically.',
      );
    } finally {
      pending.current = false;
      epoch.current++;
      setBusy(false);
      setRevision((n) => n + 1);
    }
  }
  const provider = data?.providerControl,
    stopped = provider?.status === 'stopped';
  const fresh = !loadError && manualControlFresh(data, clock);
  const operating =
    !!provider?.operationPending ||
    !!data?.queuedModel ||
    !!data?.switching ||
    (!!data?.selectionResult &&
      ['queued', 'working'].includes(data.selectionResult.status));
  const warmup = fresh ? currentManualWarmup(data) : undefined;
  const target = data?.models.find((m) => m.id === model);
  const blocker = holdReason || manualBlocker(data, model, clock);
  const locked = busy || uncertain || operating || permissionBusy;
  // Catalog display names when known ("Qwen 3.8 27B"), else the short id.
  const nameOf = (id: string) =>
    data?.models.find((m) => m.id === id)?.name || shortModel(id);
  const label = nameOf(model);
  const operationTarget =
    data?.switchingModel || data?.queuedModel || data?.selectionResult?.model;
  const description = !visible
    ? 'View in background'
    : !fresh
      ? data
        ? 'Last confirmed state'
        : loadError
          ? 'Status unavailable'
          : 'Connecting to this Mac'
      : operating
        ? operationTarget
          ? `${stopped ? 'Starting' : operationTarget === data?.currentModel ? 'Getting ready:' : 'Changing to'} ${nameOf(operationTarget)}`
          : 'Updating Darkbloom…'
        : stopped
          ? 'Ready to start a model'
          : provider?.status === 'unavailable'
            ? 'Darkbloom needs setup'
            : data?.currentModel
              ? nameOf(data.currentModel)
              : 'Choose your model';
  const queue = data?.queue;
  const pauseSeconds =
    queue?.pauseInSeconds != null
      ? Math.max(0, Math.ceil(queue.pauseInSeconds))
      : null;
  const lastSelection = data?.selectionResult;
  const result =
    lastSelection &&
    (lastSelection.id === ownRequestId ||
      (clock - lastSelection.at >= 0 && clock - lastSelection.at < 3600))
      ? lastSelection
      : null;
  const choices = (data?.models ?? []).map((m) => ({
    ...m,
    available: stopped ? m.canStart : m.canSwitch,
    reason: (stopped ? m.startReason : m.switchReason) || undefined,
  }));
  const sameRunning = !stopped && model === data?.currentModel;
  const startOrSwitch = stopped
    ? 'Start'
    : sameRunning && provider?.endpointSetupRequired
      ? 'Prepare'
      : 'Switch to';
  const actionLabel =
    sameRunning && !provider?.endpointSetupRequired
      ? managed
        ? pinned && homeModel === model
          ? `${label} is your pick`
          : `Pin ${label}`
        : data?.mode === 'observe'
          ? 'Already running'
          : `Keep ${label}, stop auto-switching`
      : `${startOrSwitch} ${label}`;
  // Nothing to do: the running model is already the pick (or Manual is already on it).
  const nothingToDo =
    sameRunning &&
    !provider?.endpointSetupRequired &&
    (managed ? pinned && homeModel === model : data?.mode === 'observe');
  const current = data?.currentModel ? nameOf(data.currentModel) : '';
  const fit = target ? memoryFit(target, !stopped) : '';
  const consequence = stopped
    ? `Starts Darkbloom with ${label} and checks that it is warm and ready.${fit ? ` ${fit}.` : ''}`
    : sameRunning && !provider?.endpointSetupRequired
      ? managed
        ? `Nothing restarts. BloomGauge keeps ${label} running, restores it if it fails and never switches away from it.`
        : `Nothing restarts. Automatic switching turns off and ${label} keeps serving.`
      : sameRunning
        ? `Sets up pre-warming for ${label}. Darkbloom restarts once and accepted requests finish first.`
        : `Darkbloom restarts with ${label}${current ? ` instead of ${current}` : ''}; with Darkbloom 0.9.9 or later, accepted requests finish first.${fit ? ` ${fit}.` : ''} ${managed ? `${label} becomes your pick: BloomGauge keeps it running and never switches away from it.` : 'Automatic switching stays off.'}`;
  return (
    <section
      className={`${embedded ? 'manual-model-embedded' : 'panel'} manual-model-control`}
      aria-label="Manual model controls"
    >
      <div className="manual-model-heading">
        <div>
          {!embedded && <div className="eyebrow">MANUAL · THIS MAC</div>}
          <h2>{description}</h2>
        </div>
        <span
          role="status"
          className={`status-pill ${fresh && warmup?.status === 'ready' ? 'good' : 'warn'}`}
        >
          <i />
          {!fresh
            ? 'Checking status'
            : operating
              ? 'In progress'
              : stopped
                ? 'Stopped'
                : provider?.status === 'unavailable'
                  ? 'Not set up'
                  : warmup?.status === 'ready'
                    ? 'Running · ready'
                    : warmup?.status === 'warming'
                      ? 'Warming up'
                      : warmup
                        ? 'Not ready yet'
                        : 'Waiting for readiness'}
        </span>
      </div>
      <p className="manual-model-intro">
        {managed
          ? 'Choose the model BloomGauge should hold. It keeps your pick running and restores it if it fails.'
          : stopped
            ? 'Choose a model, then start it here. BloomGauge starts Darkbloom in the background.'
            : 'Choose a model and switch here. BloomGauge handles the change and checks that the model is ready.'}
      </p>
      <div className="manual-model-form">
        <div className="manual-model-choice">
          <span>Model to run</span>
          <ManualModelPicker
            models={choices}
            currentModel={
              !stopped ? (data?.currentModel ?? undefined) : undefined
            }
            homeModel={homeModel}
            pinned={pinned}
            scope={data?.session ?? ''}
            openRequest={pickerRequest}
            value={model}
            disabled={locked}
            actionLabel={stopped ? 'Start' : 'Switch'}
            onChange={(value) => {
              if (locked) return;
              setModel(value);
              chosen.current = true;
              setActionError('');
              setStopConfirmation(null);
              setConfirming(false);
            }}
          />
        </div>
        <Button
          className="manual-model-primary"
          disabled={locked || !fresh || !!blocker || !model || nothingToDo}
          aria-describedby={explanationId}
          aria-expanded={confirming}
          onClick={() => {
            if (data && !blocker && model) setConfirming(true);
          }}
        >
          {busy || operating ? (
            <LoaderCircle className="spin" />
          ) : stopped ? (
            <Play />
          ) : (
            <ArrowRightLeft />
          )}
          {uncertain
            ? 'Checking your request'
            : operating
              ? 'Change in progress'
              : !model
                ? 'Choose a model'
                : actionLabel}
        </Button>
      </div>
      {confirming && data && !locked && fresh && !blocker && (
        <div
          className="notice provider-confirm manual-confirm"
          role="group"
          aria-label="Confirm model change"
        >
          <strong>
            {stopped
              ? `Start ${label}?`
              : sameRunning && !provider?.endpointSetupRequired
                ? managed
                  ? `Pin ${label}?`
                  : `Keep ${label} and stop auto-switching?`
                : sameRunning
                  ? `Prepare ${label}?`
                  : `Switch to ${label}?`}
          </strong>
          <p>{consequence}</p>
          <div className="provider-actions">
            <Button
              onClick={() =>
                void send(manualRequest(data, model, crypto.randomUUID()))
              }
            >
              {stopped
                ? 'Start'
                : sameRunning && !provider?.endpointSetupRequired
                  ? managed
                    ? 'Pin'
                    : 'Keep it'
                  : sameRunning
                    ? 'Prepare'
                    : 'Switch'}
            </Button>
            <Button variant="outline" onClick={() => setConfirming(false)}>
              Cancel
            </Button>
          </div>
        </div>
      )}
      <p
        id={explanationId}
        className={
          blocker && model && fresh && !operating
            ? 'manual-blocker'
            : 'footnote'
        }
      >
        {uncertain
          ? 'Your original selection is held while BloomGauge checks the command receipt.'
          : blocker && fresh && !operating
            ? withoutCircularHint(blocker)
            : managed
              ? 'Your pick becomes the model BloomGauge holds: it is restored after a failure and never switched away. Choose Manager on to let BloomGauge choose again.'
              : stopped
                ? 'Starts the selected model. Automatic switching stays off until you turn the optimizer on.'
                : 'A manual switch pauses automation. With Darkbloom 0.9.9 or later it starts right away and accepted requests finish first. Older versions wait for an idle gap, then switch after five minutes if still busy, which may interrupt requests.'}
      </p>
      {!locked &&
        fresh &&
        target?.loadBudget &&
        target.loadBudget.afterUnloadGB < target.loadBudget.requiredGB && (
          <p className="footnote manual-memory-shortfall">
            Memory check: {num(target.loadBudget.afterUnloadGB, 1)} GB available
            · {num(target.loadBudget.requiredGB, 1)} GB needed
            {' · '}
            {num(
              target.loadBudget.requiredGB - target.loadBudget.afterUnloadGB,
              1,
            )}{' '}
            GB more needed.
          </p>
        )}
      {fresh && target?.cacheRecovery?.needed && !operating && (
        <div className="notice manual-cache-recovery" role="note">
          <strong>
            {target.cacheRecovery.canAttempt
              ? 'Cache cleanup before loading'
              : 'More memory is needed'}
          </strong>
          <p>
            {target.cacheRecovery.canAttempt
              ? `Start ${label} will try one eligible cleanup, then measure memory again. The model starts only if enough memory is available.`
              : target.cacheRecovery.detail}
          </p>
          {target.cacheRecovery.retryAt != null &&
            target.cacheRecovery.retryAt > clock && (
              <p>
                Cleanup can be checked again in{' '}
                {Math.ceil((target.cacheRecovery.retryAt - clock) / 60)} min.
              </p>
            )}
          <CacheRecoveryPermission
            authorization={provider?.cacheRecoveryAuthorization}
            remote={data?.remote}
            fresh={fresh}
            locked={busy || uncertain || operating}
            onRefresh={refreshStatus}
            onBusy={setPermissionBusy}
          />
        </div>
      )}
      {target?.requiresRuntimeVerification && !locked && fresh && !blocker && (
        <p className="notice manual-verification" role="note">
          {startOrSwitch} {label} will also verify runtime support, network
          eligibility and warm readiness.
          {stopped
            ? ' If verification fails, BloomGauge shows the reason here.'
            : ' If verification fails, BloomGauge attempts to restore the previous ready model when safe.'}
        </p>
      )}
      {((loadError && !connectionError) ||
        actionError ||
        (!fresh && !loadError && data)) && (
        <p className="notice" role="alert">
          {actionError ||
            loadError ||
            'Waiting for fresh model status. Retrying automatically.'}
        </p>
      )}
      {uncertain && !busy && (
        <div className="provider-actions">
          <Button variant="outline" onClick={() => setRevision((n) => n + 1)}>
            <RefreshCw size={16} />
            Check request status
          </Button>
          <Button
            variant="ghost"
            onClick={() => {
              if (retry.current) void send(retry.current);
            }}
          >
            Retry same request
          </Button>
        </div>
      )}
      {operating && fresh && (
        <div className="manual-model-status" role="status">
          <p>
            {withoutCircularHint(
              data?.selectionResult &&
                ['queued', 'working'].includes(data.selectionResult.status)
                ? data.selectionResult.detail
                : data?.detail,
            )}
          </p>
          {data?.queuedModel && !data.switching && queue && (
            <>
              <p>
                {!queue.fresh
                  ? 'Waiting for fresh activity readings'
                  : queue.activity === 'busy'
                    ? 'Finishing active requests'
                    : `${Math.floor(queue.idleSeconds)} / ${queue.requiredIdleSeconds}s idle`}
              </p>
              <progress
                max={queue.requiredIdleSeconds}
                value={
                  queue.fresh
                    ? Math.min(queue.idleSeconds, queue.requiredIdleSeconds)
                    : 0
                }
                aria-label="Idle time before switch"
              />
              {pauseSeconds !== null && (
                <p>
                  {pauseSeconds > 0
                    ? `Switch in ${Math.floor(pauseSeconds / 60)}:${String(pauseSeconds % 60).padStart(2, '0')} if still busy.`
                    : 'Checking the provider and available memory before switching.'}
                </p>
              )}
              {data.canCancel && (
                <Button
                  variant="outline"
                  disabled={busy || uncertain}
                  onClick={() =>
                    send({ action: 'cancel', requestId: data.requestId ?? '' })
                  }
                >
                  <X />
                  Cancel queued switch
                </Button>
              )}
            </>
          )}
        </div>
      )}
      {fresh &&
        !operating &&
        result &&
        ['failed', 'cancelled'].includes(result.status) && (
          <p className="notice" role="status">
            <strong>Last attempt · {shortModel(result.model)}</strong>
            <br />
            {withoutCircularHint(result.detail)}
          </p>
        )}
      {warmup && <ModelWarmup value={warmup} />}
      {fresh && provider?.status === 'running' && !operating && !warmup && (
        <p className="footnote" role="status">
          Darkbloom is running. Waiting for fresh readiness checks for this
          model.
        </p>
      )}
      <div className="provider-actions manual-secondary-actions">
        {provider?.status === 'running' && (
          <Button
            variant="outline"
            disabled={locked || !fresh || !provider.canStop}
            onClick={() =>
              setStopConfirmation({
                action: 'provider-stop',
                expectedProvider: provider.version ?? '',
                requestId: crypto.randomUUID(),
              })
            }
          >
            <Square size={16} />
            Stop Darkbloom
          </Button>
        )}
        <Button variant="ghost" disabled={busy} onClick={refreshStatus}>
          <RefreshCw size={16} />
          Refresh status
        </Button>
      </div>
      {stopConfirmation && (
        <div
          className="notice provider-confirm"
          role="group"
          aria-label="Confirm stop"
        >
          <strong>Stop earning on this Mac?</strong>
          <p>
            Stops Darkbloom and pauses automatic switching. Active requests may
            be interrupted.
          </p>
          <div className="provider-actions">
            <Button
              disabled={locked || !fresh}
              onClick={() => send(stopConfirmation)}
            >
              Stop now
            </Button>
            <Button
              variant="outline"
              disabled={busy}
              onClick={() => setStopConfirmation(null)}
            >
              Keep running
            </Button>
          </div>
        </div>
      )}
      {!embedded && (
        <p className="footnote">
          {data?.mode === 'observe'
            ? 'Manual mode · you choose when to change models.'
            : 'Choosing a model here will pause automatic switching.'}{' '}
          <button
            type="button"
            className="text-link"
            onClick={() => navigation.navigate('test')}
          >
            Open optimizer →
          </button>
        </p>
      )}
      <details className="footnote manual-service-details">
        <summary>Model & service details</summary>
        <p>
          {stopped ? 'Saved model' : 'Reported model'}:{' '}
          {shortModel(provider?.model || data?.currentModel || '') || 'Unknown'}
        </p>
        {target?.memoryGB != null && (
          <p>
            {num(target.memoryGB, 1)} GB estimated model memory.
            {target.loadBudget
              ? ` ${num(target.loadBudget.afterUnloadGB, 1)} GB available${stopped ? '' : ' after unloading'}; ${num(target.loadBudget.requiredGB, 1)} GB required with headroom.`
              : ''}
          </p>
        )}
        {provider?.endpointSetupRequired && (
          <p>
            {data?.remote
              ? 'Set up pre-warming once in Optimizer → Overview on the Mac using the manual model controls.'
              : 'Use Prepare for the current model, or Start or Switch for your selected model, to set up authenticated local pre-warming. Stop pauses automatic switching; with the manager on, Prepare and Start keep it running.'}
          </p>
        )}
        {!stopped && provider?.endpoint !== 'ready' && (
          <p>
            {provider?.endpointDetail ||
              'Waiting for the local pre-warming endpoint.'}
          </p>
        )}
        {provider?.configurationIssue && <p>{provider.configurationIssue}</p>}
        {data?.controlError && provider?.status === 'running' && (
          <p>{data.controlError}</p>
        )}
        {data?.selectionResult && (
          <p>
            Last selection · {shortModel(data.selectionResult.model)} ·{' '}
            {data.selectionResult.status}: {data.selectionResult.detail}
          </p>
        )}
        {provider?.lastResult && (
          <p>
            Last service command · {provider.lastResult.status}:{' '}
            {provider.lastResult.detail}
          </p>
        )}
        {!target?.cacheRecovery?.needed && (
          <CacheRecoveryPermission
            authorization={provider?.cacheRecoveryAuthorization}
            remote={data?.remote}
            fresh={fresh}
            locked={busy || uncertain || operating}
            onRefresh={refreshStatus}
            onBusy={setPermissionBusy}
          />
        )}
        <p>
          BloomGauge checks provider identity, memory, power and temperature before
          changing models. You can cancel a queued switch before it starts.
        </p>
        <button
          type="button"
          className="text-link"
          onClick={() => navigation.navigate('support')}
        >
          Help & feedback →
        </button>
      </details>
      {paused && (
        <p className="footnote">
          Model controls stay live while charts are paused.
        </p>
      )}
    </section>
  );
}
