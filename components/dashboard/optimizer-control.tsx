'use client';
import { useEffect, useRef, useState, type ReactNode } from 'react';
import { LoaderCircle, RefreshCw, SlidersHorizontal, Hand } from 'lucide-react';
import { startChartPolling } from '@/lib/chart-polling';
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
  type AutomaticRequest,
  type OptimizerControlState,
} from '@/lib/optimizer-control';
import { recordSupportIssue } from '@/lib/support-issues';
import { Button } from '@/components/ui/button';
import { useScreenActive, useAppNavigation } from './app-navigation';
import { shortModel } from './shared';
import { ManualModelControl } from './manual-model';
import { OptimizerIntro } from './optimizer-intro';
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
}: {
  onSettings: () => void;
  onChanged: () => void;
  connectionError?: string;
  /** Follow demand's plan, edited in place on this card. */
  plan?: ReactNode;
}) {
  const visible = usePageVisible(),
    active = useScreenActive();
  const navigation = useAppNavigation();
  const [state, setState] = useState<OptimizerControlState | null>(null);
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
          throw new Error(
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
  const automaticMode = state?.automatic.mode;
  useEffect(() => {
    if (automaticMode === 'on') setManualOpen(false);
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
    if (phase === 'manual') {
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
  }, [phase]);

  function chooseOn() {
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
          ? error.message
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
  const canEnable =
    !!state?.automatic.canEnable &&
    (state.hasSavedPlan || (state.firstPlan && initialModels.length >= 2));
  const blockerAction = state?.automatic.blocker?.action;
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
          <h2>Let Bloomkeeper choose. Or take control.</h2>
        </div>
      </div>
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
      <div
        className={`optimizer-primary-status ${blocked ? 'attention' : ''}`}
        role="status"
        aria-live="polite"
      >
        {(sending || preparing) && <LoaderCircle className="spin" size={18} />}
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
                    : optimizerPhaseLabel(state!)}
          </strong>
          <p>
            {uncertain
              ? 'The reply is unconfirmed. Bloomkeeper is checking its receipt; retrying the same choice will not repeat an accepted command.'
              : actionError ||
                (!connectionError && loadError) ||
                (stale
                  ? 'Waiting for fresh control status. Last confirmed settings are shown above.'
                  : state?.automatic.detail)}
          </p>
        </div>
      </div>
      <div className="optimizer-current-model">
        <span>{state?.providerRunning ? 'Current model' : 'Saved model'}</span>
        <strong>
          {state?.currentModel
            ? shortModel(state.currentModel)
            : 'No model selected'}
        </strong>
        <small>
          {stale
            ? 'Last confirmed reading'
            : state?.providerRunning
              ? state.warmup?.status === 'ready'
                ? 'Warm and ready'
                : 'Checking readiness'
              : 'Stopped'}
        </small>
      </div>
      {!isOn && state && !stale && (
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
      {!stale && state?.firstPlan && initialModels.length < 2 && (
        <p className="footnote">
          Download another supported model in Darkbloom, then check again.
          Optimization needs at least two models.
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
      {(!isOn || manualOpen) && (
        <div className="optimizer-manual-inline">
          <ManualModelControl
            embedded
            pickerRequest={manualPickerRequest}
            connectionError={connectionError || loadError}
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
        <button
          type="button"
          className="text-link"
          onClick={() => navigation.navigate('pairs')}
        >
          Pair tests
        </button>
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
      />
    </section>
  );
}
