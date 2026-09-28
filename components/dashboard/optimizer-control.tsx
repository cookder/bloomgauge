'use client';
import { useEffect, useRef, useState, type ReactNode } from 'react';
import {
  LoaderCircle,
  RefreshCw,
  SlidersHorizontal,
  Hand,
  MapPin,
  Power,
  ShieldCheck,
} from 'lucide-react';
import {
  ResponseValidationError,
  startChartPolling,
} from '@/lib/chart-polling';
import { usePageVisible } from '@/lib/use-page-visibility';
import { readStatusJSON } from '@/lib/connection-status';
import {
  validOptimizerControl,
  optimizerPhaseLabel,
  optimizerControlFresh,
  initialOptimizerModels,
  automaticRequest,
  automaticRequestKey,
  readAutomaticRequest,
  optimizerRequestObserved,
  releasePinRequest,
  keepCurrentRequest,
  type AutomaticRequest,
  type OptimizerControlState,
} from '@/lib/optimizer-control';
import { recordSupportIssue } from '@/lib/support-issues';
import {
  readiness as readinessOf,
  optimizerStrategy,
  readManagerSummary,
  withSummary,
  withoutCircularHint,
  type ManagerView,
  type Strategy,
} from '@/lib/optimizer-manager';
import { ManagerStatusCard, ReadinessLine } from './manager-status';
import { Button } from '@/components/ui/button';
import { useScreenActive, useAppNavigation } from './app-navigation';
import { shortModel } from './shared';
import { ManualModelControl } from './manual-model';
import { OptimizerIntro } from './optimizer-intro';
import { showWhatsChanged } from './whats-changed';
import {
  introBoostKey,
  introSeenKey,
  startIntroBoost,
} from '@/lib/optimizer-intro';

export function OptimizerControl({
  onSettings,
  onChanged,
  connectionError = '',
  plan,
  strategy = null,
  manager: managerView = null,
  excursions = null,
  excursionsBusy = false,
  onExcursions,
  lastPause = null,
}: {
  onSettings: () => void;
  onChanged: () => void;
  connectionError?: string;
  /** Follow demand's plan, edited in place on this card. */
  plan?: ReactNode;
  /** Which optimizer runs: the manager (hold, recover, move on evidence) or legacy. */
  strategy?: Strategy | null;
  /** The manager's view from GET /api/optimizer (refreshed every 30 s). */
  manager?: ManagerView | null;
  /** managerExcursions: null while this backend has no such setting. */
  excursions?: boolean | null;
  excursionsBusy?: boolean;
  onExcursions?: (on: boolean) => void;
  /** Why automatic control is off, when it paused itself. */
  lastPause?: { at: number; model: string | null; detail: string } | null;
}) {
  const visible = usePageVisible(),
    active = useScreenActive();
  const navigation = useAppNavigation();
  const [state, setState] = useState<OptimizerControlState | null>(null);
  // The 3-second control status carries the manager's live facts; the full view
  // (every 30 s) adds evidence and the ledger.
  const manager = withSummary(
    managerView,
    optimizerControlFresh(state) ? readManagerSummary(state?.manager) : null,
  );
  const [loadError, setLoadError] = useState(''),
    [actionError, setActionError] = useState('');
  const [sending, setSending] = useState(false),
    [revision, setRevision] = useState(0);
  const [manualOpen, setManualOpen] = useState(false),
    [uncertain, setUncertain] = useState(false);
  const [manualPickerRequest, setManualPickerRequest] = useState(0);
  const [intro, setIntro] = useState<'first' | 'info' | null>(null),
    [boostNote, setBoostNote] = useState('');
  const boosting = useRef(false);
  // Manager: "Manual (pin)" opens the picker; "Manager on" releases a pin (release-pin).
  const [pinIntent, setPinIntent] = useState(false),
    [releaseConfirm, setReleaseConfirm] = useState(false);
  // The 3-second control status names the strategy; until something does, On waits
  // (a first On under the wrong strategy would offer the legacy boost).
  const knownStrategy =
    optimizerStrategy({ controlStrategy: state?.strategy }) ??
    strategy ??
    optimizerStrategy({ controlPolicy: state?.demandPolicy });
  const managed = knownStrategy === 'manager';
  function showManualModels() {
    setManualOpen(true);
    setManualPickerRequest((n) => n + 1);
  }
  const pending = useRef(false),
    epoch = useRef(0);
  const lastReading = useRef(0);
  // Store the entire request: a UUID retry must have identical content, even if
  // the model catalog or plan changes while the reply is missing.
  const retry = useRef<AutomaticRequest | null>(null);
  function remember(request: AutomaticRequest | null) {
    retry.current = request;
    setUncertain(!!request);
    try {
      if (request)
        localStorage.setItem(automaticRequestKey, JSON.stringify(request));
      else localStorage.removeItem(automaticRequestKey);
    } catch {
      /* Server receipts still survive when browser storage is unavailable. */
    }
  }
  useEffect(() => {
    try {
      remember(readAutomaticRequest(localStorage.getItem(automaticRequestKey)));
    } catch {
      /* Optional storage. */
    }
  }, []);
  useEffect(() => {
    if (!active || !visible) return;
    return startChartPolling({
      issueContext: 'models',
      intervalMs: 3000,
      timeoutMs: 10000,
      load: async (signal) => {
        const own = epoch.current;
        const response = await fetch('/api/optimizer/control', {
          cache: 'no-store',
          signal,
        });
        const value = await readStatusJSON(response, 'Optimizer controls');
        if (!validOptimizerControl(value))
          throw new ResponseValidationError(
            'optimizer-controls',
            'Waiting for a complete control status. No settings were changed.',
          );
        return { own, value };
      },
      onValue: ({ own, value }) => {
        if (
          own !== epoch.current ||
          pending.current ||
          value.at < lastReading.current
        )
          return;
        if (!optimizerControlFresh(value)) {
          setLoadError(
            'Waiting for fresh control status. Retrying automatically.',
          );
          return;
        }
        lastReading.current = value.at;
        setState(value);
        setLoadError('');
        if (retry.current && optimizerRequestObserved(value, retry.current)) {
          if (!retry.current.enabled) showManualModels();
          remember(null);
          setActionError('');
          onChanged();
        }
      },
      onError: (error) => {
        if (!pending.current)
          setLoadError(
            error instanceof TypeError
              ? 'Control status is delayed. Retrying automatically.'
              : error.message,
          );
      },
    });
  }, [active, visible, revision]);

  // Turning On hides the manual list again; Manual (or a blocker) reopens it.
  // Manual (pin) keeps it open: the pick made there becomes the pinned model.
  const automaticMode = state?.automatic.mode;
  useEffect(() => {
    if (automaticMode === 'on' && !pinIntent) setManualOpen(false);
  }, [automaticMode]);

  // A boost chosen in the first-On explainer starts once On is active, not while it is still starting.
  const phase = state?.automatic.phase;
  useEffect(() => {
    let wanted = false;
    try {
      wanted = localStorage.getItem(introBoostKey) === '1';
    } catch {
      /* Optional storage. */
    }
    if (!wanted || boosting.current || !phase) return;
    const clear = () => {
      try {
        localStorage.removeItem(introBoostKey);
      } catch {
        /* Optional storage. */
      }
    };
    // The manager has no Learning boost (the backend refuses it).
    if (phase === 'manual' || managed) {
      clear();
      return;
    }
    if (phase !== 'active') return;
    boosting.current = true;
    const controller = new AbortController();
    startIntroBoost(controller.signal)
      .then((result) => {
        clear();
        setBoostNote(
          result === 'started'
            ? '3-day Learning boost started. It ends by itself; stop it any time under Learning boost.'
            : 'A Learning boost was already running.',
        );
        onChanged();
      })
      .catch((error) => {
        if (!controller.signal.aborted) {
          clear();
          setBoostNote(
            `The optimizer is on, but the Learning boost didn’t start: ${error instanceof Error ? error.message : 'try again'}. Start it under Learning boost.`,
          );
        }
      })
      .finally(() => {
        boosting.current = false;
      });
    return () => controller.abort();
  }, [phase, managed]);

  function choosePin() {
    setReleaseConfirm(false);
    setPinIntent(true);
    showManualModels();
    // The manager must be on for a pick to become the pin.
    if (!isOn && !retry.current) chooseOn();
  }
  function chooseManager() {
    setPinIntent(false);
    if (isOn && manager?.pinned) setReleaseConfirm(true);
    else chooseOn();
  }
  // One backend action: the pin is dropped and the manager returns to its home model
  // through a normal, confirmed move. Automatic control stays on throughout.
  function releasePin() {
    setReleaseConfirm(false);
    if (!state) return;
    void managerAction(
      releasePinRequest(state, crypto.randomUUID()),
      'Could not release your pick. Refresh the status and try again.',
      'The response was incomplete. Checking whether your pick was released…',
    );
  }
  // "Keep current" on a planned home change: the serving model becomes the pick.
  function keepCurrent(model: string) {
    if (!state) return;
    void managerAction(
      keepCurrentRequest(state, model, crypto.randomUUID()),
      `Could not keep ${shortModel(model)}. Refresh the status and try again.`,
      `The response was incomplete. Checking whether ${shortModel(model)} was kept…`,
    );
  }
  async function managerAction(
    request: ReturnType<typeof releasePinRequest | typeof keepCurrentRequest>,
    failed: string,
    incomplete: string,
  ) {
    if (!state || pending.current || !optimizerControlFresh(state)) return;
    pending.current = true;
    epoch.current++;
    setSending(true);
    setActionError('');
    try {
      const response = await fetch('/api/optimizer/control', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Bloom-Action': 'optimizer',
        },
        signal: AbortSignal.timeout(15000),
        body: JSON.stringify(request),
      });
      const value = await response.json();
      if (!response.ok)
        throw new Error(
          value &&
            typeof value === 'object' &&
            'error' in value &&
            typeof value.error === 'string'
            ? value.error
            : failed,
        );
      if (!validOptimizerControl(value)) throw new Error(incomplete);
      if (value.at >= lastReading.current) {
        lastReading.current = value.at;
        setState(value);
        setLoadError('');
      }
      onChanged();
    } catch (error) {
      recordSupportIssue('action', 'models');
      setActionError(
        error instanceof Error &&
          !['AbortError', 'TimeoutError'].includes(error.name)
          ? withoutCircularHint(error.message)
          : 'The reply was interrupted. Checking the latest state before another request.',
      );
    } finally {
      pending.current = false;
      epoch.current++;
      setSending(false);
      setRevision((n) => n + 1);
    }
  }
  function chooseOn() {
    if (!knownStrategy) return;
    let seen = false;
    try {
      seen = localStorage.getItem(introSeenKey) === '1';
    } catch {
      /* Optional storage. */
    }
    if (state?.firstPlan && !seen && !retry.current) setIntro('first');
    else void setAutomatic(true);
  }
  function introChosen(boost: boolean) {
    try {
      localStorage.setItem(introSeenKey, '1');
      if (boost) localStorage.setItem(introBoostKey, '1');
      else localStorage.removeItem(introBoostKey);
    } catch {
      /* Optional storage: the boost can still be started under Learning boost. */
    }
    setIntro(null);
    setBoostNote('');
    void setAutomatic(true);
  }

  async function setAutomatic(enabled: boolean) {
    if (!state || pending.current || (enabled && !optimizerControlFresh(state)))
      return;
    if (!enabled) setPinIntent(false);
    pending.current = true;
    epoch.current++;
    setSending(true);
    setActionError('');
    const request =
      retry.current?.enabled === enabled
        ? retry.current
        : automaticRequest(state, enabled, crypto.randomUUID());
    remember(request);
    try {
      const response = await fetch('/api/optimizer/control', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Bloom-Action': 'optimizer',
        },
        signal: AbortSignal.timeout(15000),
        body: JSON.stringify(request),
      });
      const value = await response.json();
      if (!response.ok) {
        // A server error can follow admission. Keep the exact request until a
        // matching receipt or a definitive client rejection resolves it.
        if (response.status >= 400 && response.status < 500) remember(null);
        throw new Error(
          value &&
            typeof value === 'object' &&
            'error' in value &&
            typeof value.error === 'string'
            ? value.error
            : 'Could not change optimizer mode. Refresh the status and try again.',
        );
      }
      if (!validOptimizerControl(value))
        throw new Error(
          'The response was incomplete. Checking whether your request was accepted…',
        );
      if (!optimizerControlFresh(value))
        throw new Error(
          'Waiting for fresh control status. Checking the original request.',
        );
      if (value.at >= lastReading.current) {
        lastReading.current = value.at;
        setState(value);
        setLoadError('');
      }
      if (optimizerRequestObserved(value, request)) {
        if (!request.enabled) showManualModels();
        remember(null);
        onChanged();
      }
    } catch (error) {
      recordSupportIssue('action', 'models');
      setActionError(
        error instanceof Error &&
          !['AbortError', 'TimeoutError'].includes(error.name)
          ? withoutCircularHint(error.message)
          : 'The reply was interrupted. Checking the latest state before another request.',
      );
    } finally {
      pending.current = false;
      epoch.current++;
      setSending(false);
      setRevision((n) => n + 1);
    }
  }

  const stale = !!loadError || !optimizerControlFresh(state);
  const isOn = state?.automatic.mode === 'on';
  const preparing =
    state?.automatic.phase === 'starting' ||
    state?.automatic.phase === 'waiting';
  const blocked = state?.automatic.phase === 'blocked';
  const initialModels = state ? initialOptimizerModels(state) : [];
  // The manager can hold one model; legacy demand following compares two or more.
  const fewestModels = managed ? 1 : 2;
  const canEnable =
    !!state?.automatic.canEnable &&
    (state.hasSavedPlan ||
      (state.firstPlan && initialModels.length >= fewestModels));
  const blockerAction = state?.automatic.blocker?.action;
  // Manager strategy: Off, Manual (pin) or Manager on.
  const managerMode: 'off' | 'pin' | 'manager' = !isOn
    ? 'off'
    : manager?.pinned
      ? 'pin'
      : 'manager';
  const now = state?.at ?? Date.now() / 1000;
  // "Checking readiness" never stands alone: remember since when this view has
  // seen the current model not ready (the manager's watchdog time wins).
  const notReady =
    state?.providerRunning && state.warmup?.status !== 'ready'
      ? `${state.currentModel}:${state.warmup?.status ?? ''}`
      : null;
  const seen = useRef<{ key: string; at: number } | null>(null);
  if (!notReady) seen.current = null;
  else if (seen.current?.key !== notReady && state)
    seen.current = { key: notReady, at: state.at };
  const ready = readinessOf({
    providerRunning: stale ? null : state?.providerRunning,
    warmup: state?.warmup,
    watchdog:
      manager?.active && manager.watchdog?.darkSince != null
        ? manager.watchdog
        : null,
    seen: seen.current?.at,
  });
  // "Manual mode." is the legacy name for Off.
  const detail = withoutCircularHint(
    managed
      ? state?.automatic.detail.replace(/^Manual mode\.\s*/, '')
      : state?.automatic.detail,
  );
  const names = Object.fromEntries(
    (state?.models ?? []).map((m) => [m.id, m.name]),
  );
  // The manager card replaces the status line once the manager is on: also while it
  // waits for readiness or recovers ('waiting'), but not while On is being turned on.
  const turningOn = ['pending', 'starting', 'waiting'].includes(
    state?.operation?.status ?? '',
  );
  const showManagerCard =
    managed &&
    !!manager &&
    isOn &&
    !turningOn &&
    !stale &&
    !sending &&
    !uncertain;
  const managerCopy = {
    off: 'Bloomkeeper won’t change, restore or restart models. Choose one below.',
    pin: `Bloomkeeper keeps ${manager?.home ? names[manager.home.model] || shortModel(manager.home.model) : 'your pick'} running and restores it if it fails. It never switches away from your pick.`,
    manager: `Bloomkeeper keeps the best model for this Mac running and recovers by itself after a failed switch${excursions === false ? '. It does not move for network evidence.' : excursions ? ', moving to a better model only when network evidence is strong.' : '.'}`,
  }[managerMode];
  async function checkAgain() {
    if (pending.current) return;
    // An unresolved mutation is reconciled by GET; Refresh is a different request
    // and must not overwrite its receipt or erase an authoritative rejection.
    if (retry.current) {
      setRevision((n) => n + 1);
      return;
    }
    pending.current = true;
    epoch.current++;
    setSending(true);
    try {
      const response = await fetch('/api/optimizer/control', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Bloom-Action': 'optimizer',
        },
        signal: AbortSignal.timeout(10000),
        body: JSON.stringify({
          action: 'refresh',
          requestId: crypto.randomUUID(),
          expectedControl: state?.controlVersion ?? '',
        }),
      });
      const value = await response.json();
      if (
        !response.ok ||
        !validOptimizerControl(value) ||
        !optimizerControlFresh(value)
      )
        throw new Error(
          'Could not refresh controls yet. Retrying the connection automatically.',
        );
      if (value.at >= lastReading.current) {
        lastReading.current = value.at;
        setState(value);
        setLoadError('');
      }
    } catch (error) {
      setActionError(
        error instanceof Error ? error.message : 'Could not refresh controls.',
      );
    } finally {
      pending.current = false;
      epoch.current++;
      setSending(false);
      setRevision((n) => n + 1);
    }
  }
  return (
    <section
      className="panel optimizer-main-control"
      aria-label="Optimizer control"
    >
      <div className="optimizer-control-heading">
        <div>
          <p className="eyebrow">THIS MAC</p>
          <h2>
            {managed
              ? 'Keep this Mac on its best model.'
              : knownStrategy
                ? 'Let Bloomkeeper choose. Or take control.'
                : 'Checking how this Mac picks models.'}
          </h2>
        </div>
      </div>
      {managed ? (
        <div
          className="optimizer-mode-picker manager-modes"
          role="group"
          aria-label="Optimizer mode"
        >
          <button
            type="button"
            aria-pressed={!!state && managerMode === 'off'}
            disabled={sending || !state || (stale && !isOn && !retry.current)}
            onClick={() => {
              setPinIntent(false);
              setReleaseConfirm(false);
              if (isOn || retry.current) void setAutomatic(false);
              else showManualModels();
            }}
          >
            <Power size={20} />
            <span>
              <strong>Off</strong>
              <small>You choose; nothing is restored</small>
            </span>
          </button>
          <button
            type="button"
            aria-pressed={!!state && managerMode === 'pin'}
            disabled={
              sending ||
              stale ||
              (!isOn && !canEnable && !retry.current?.enabled)
            }
            onClick={choosePin}
          >
            <MapPin size={20} />
            <span>
              <strong>Manual (pin)</strong>
              <small>Your pick, kept running</small>
            </span>
          </button>
          <button
            type="button"
            aria-pressed={!!state && managerMode === 'manager'}
            disabled={
              sending ||
              stale ||
              (managerMode === 'manager' && !blocked) ||
              (!isOn && !canEnable && !retry.current?.enabled)
            }
            onClick={chooseManager}
          >
            <ShieldCheck size={20} />
            <span>
              <strong>Manager on</strong>
              <small>Holds the best model, recovers</small>
            </span>
          </button>
        </div>
      ) : (
        <div
          className="optimizer-mode-picker"
          role="group"
          aria-label="Optimizer mode"
        >
          <button
            type="button"
            aria-pressed={!isOn && !!state}
            disabled={sending || !state || (stale && !isOn && !retry.current)}
            onClick={() => {
              if (isOn || retry.current) void setAutomatic(false);
              else showManualModels();
            }}
          >
            <Hand size={20} />
            <span>
              <strong>Manual</strong>
              <small>You choose the model</small>
            </span>
          </button>
          <button
            type="button"
            aria-pressed={!!isOn}
            disabled={
              sending ||
              stale ||
              !knownStrategy ||
              (!!isOn && !blocked) ||
              (!canEnable && !retry.current?.enabled)
            }
            onClick={chooseOn}
          >
            <SlidersHorizontal size={20} />
            <span>
              <strong>Optimizer on</strong>
              <small>Bloomkeeper follows earning opportunities</small>
            </span>
          </button>
        </div>
      )}
      {managed && state && !stale && (
        <p className="manager-mode-copy">{managerCopy}</p>
      )}
      {releaseConfirm && (
        <div
          className="notice provider-confirm"
          role="group"
          aria-label="Let Bloomkeeper choose"
        >
          <strong>Let Bloomkeeper choose the home model again?</strong>
          <p>
            Your pick
            {manager?.home ? `, ${shortModel(manager.home.model)},` : ''} is
            released: Bloomkeeper holds the model that has paid best here and
            switches back to it on its next check. Automatic control stays on,
            and the current model keeps serving meanwhile.
          </p>
          <div className="provider-actions">
            <Button disabled={sending} onClick={releasePin}>
              Let Bloomkeeper choose
            </Button>
            <Button
              variant="outline"
              disabled={sending}
              onClick={() => setReleaseConfirm(false)}
            >
              Keep my pick
            </Button>
          </div>
        </div>
      )}
      {managed && excursions !== null && state && (
        <label className="manager-excursions">
          <input
            type="checkbox"
            role="switch"
            aria-checked={!!excursions}
            checked={!!excursions}
            disabled={excursionsBusy || !onExcursions || stale}
            onChange={(event) => onExcursions?.(event.target.checked)}
          />
          <span>
            <strong>
              Switch to better models when network evidence is strong
            </strong>
            <small>
              Only after 2 hours of clear evidence from at least 5 Macs like
              this one. Comes back when that evidence fades or it pays less than
              home would, and turns itself off if excursions don’t clearly pay
              off.
            </small>
          </span>
        </label>
      )}
      {showManagerCard ? (
        <ManagerStatusCard
          view={manager!}
          currentModel={state?.currentModel}
          readiness={ready}
          now={now}
          names={names}
          onKeepCurrent={isOn && !stale ? keepCurrent : undefined}
          busy={sending}
        />
      ) : (
        <div
          className={`optimizer-primary-status ${blocked ? 'attention' : ''}`}
          role="status"
          aria-live="polite"
        >
          {(sending || preparing) && (
            <LoaderCircle className="spin" size={18} />
          )}
          <div>
            <strong>
              {sending
                ? 'Saving your choice…'
                : uncertain
                  ? 'Checking your request'
                  : !visible
                    ? 'View in background'
                    : stale
                      ? state
                        ? 'Last confirmed mode'
                        : loadError
                          ? 'Status unavailable'
                          : 'Connecting to this Mac'
                      : managed && !isOn && state?.automatic.phase === 'manual'
                        ? state.providerRunning
                          ? 'Off · model running'
                          : 'Off · Darkbloom stopped'
                        : managed && isOn && !turningOn
                          ? 'Manager on'
                          : managed && turningOn
                            ? 'Turning the manager on'
                            : optimizerPhaseLabel(state!)}
            </strong>
            <p>
              {uncertain
                ? 'The reply is unconfirmed. Bloomkeeper is checking its receipt; retrying the same choice will not repeat an accepted command.'
                : actionError ||
                  (!connectionError && loadError) ||
                  (stale
                    ? 'Waiting for fresh control status. Last confirmed settings are shown above.'
                    : detail ||
                      (preparing
                        ? 'Waiting for Darkbloom to report its status.'
                        : ''))}
            </p>
          </div>
        </div>
      )}
      {managed &&
        !isOn &&
        !stale &&
        lastPause &&
        now - lastPause.at < 86400 && (
          <p className="notice manager-paused" role="status">
            <strong>
              Automatic control turned itself off at{' '}
              {new Date(lastPause.at * 1000).toLocaleTimeString([], {
                hour: 'numeric',
                minute: '2-digit',
              })}
              .
            </strong>{' '}
            {withoutCircularHint(lastPause.detail)}
          </p>
        )}
      {!showManagerCard && (
        <div className="optimizer-current-model">
          <span>
            {state?.providerRunning ? 'Current model' : 'Saved model'}
          </span>
          {stale ? (
            <>
              <strong>
                {state?.currentModel
                  ? shortModel(state.currentModel)
                  : 'No model selected'}
              </strong>
              <small>Last confirmed reading</small>
            </>
          ) : (
            <ReadinessLine
              model={state?.currentModel}
              value={ready}
              now={now}
              names={names}
            />
          )}
        </div>
      )}
      {!isOn && state && !stale && !managed && (
        <p className="footnote">
          Choose a model below, then start or switch to it. Manual keeps
          automatic switching off.
        </p>
      )}
      {boostNote && (
        <p className="footnote" role="status">
          {boostNote}
        </p>
      )}
      {(loadError || actionError || blocked || uncertain) && (
        <div className="optimizer-status-actions">
          {blocked &&
          blockerAction === 'retry' &&
          canEnable &&
          !stale &&
          !uncertain ? (
            <Button
              variant="outline"
              disabled={sending}
              onClick={() => setAutomatic(true)}
            >
              Try turning on again
            </Button>
          ) : (
            <Button variant="outline" disabled={sending} onClick={checkAgain}>
              <RefreshCw size={15} />
              Check again
            </Button>
          )}
        </div>
      )}
      {!stale && state?.firstPlan && initialModels.length < fewestModels && (
        <p className="footnote">
          {managed
            ? 'Download a supported model in Darkbloom, then check again.'
            : 'Download another supported model in Darkbloom, then check again. Optimization needs at least two models.'}
        </p>
      )}
      {!stale && blockerAction === 'configure' && (
        <Button variant="outline" onClick={onSettings}>
          Review settings
        </Button>
      )}
      {!stale &&
        (blockerAction === 'controller' || blockerAction === 'manual') && (
          <Button variant="outline" onClick={() => setManualOpen(true)}>
            Show model controls
          </Button>
        )}
      {/* Manual shows its model list in place; On shows the plan. No separate settings panel. */}
      {(!isOn || manualOpen || managerMode === 'pin') && (
        <div className="optimizer-manual-inline">
          <ManualModelControl
            embedded
            pickerRequest={manualPickerRequest}
            connectionError={connectionError || loadError}
            managed={managed && (isOn || pinIntent)}
            homeModel={manager?.home?.model}
            pinned={!!manager?.pinned}
            holdReason={
              managed && pinIntent && !isOn
                ? 'Wait until the manager is on; the model you pick then becomes the one it holds.'
                : managed && pinIntent && preparing
                  ? 'The manager is starting. Pick a model once it is on.'
                  : ''
            }
          />
        </div>
      )}
      {plan && <div id="optimizer-plan">{plan}</div>}
      <nav className="optimizer-tool-links" aria-label="Optimizer tools">
        <button
          type="button"
          className="text-link"
          onClick={() => navigation.navigate('diagnostics')}
        >
          Decision log
        </button>
        {!managed && (
          <button
            type="button"
            className="text-link"
            onClick={() => navigation.navigate('pairs')}
          >
            Pair tests
          </button>
        )}
        <button
          type="button"
          className="text-link"
          onClick={() => navigation.navigate('optimizer-tools')}
        >
          All tools
        </button>
        <button
          type="button"
          className="text-link"
          onClick={() => setIntro('info')}
        >
          What it does
        </button>
        <button type="button" className="text-link" onClick={showWhatsChanged}>
          What’s changed
        </button>
        <button
          type="button"
          className="text-link"
          onClick={() => navigation.navigate('guide')}
        >
          How this works
        </button>
      </nav>
      <OptimizerIntro
        open={!!intro}
        onClose={() => setIntro(null)}
        onChoose={intro === 'first' ? introChosen : undefined}
        protectUsdPerHour={state?.demandPolicy.protectUsdPerHour ?? 0.2}
        learningMinutesPerDay={state?.demandPolicy.learningMinutesPerDay ?? 60}
        manager={managed}
      />
    </section>
  );
}
