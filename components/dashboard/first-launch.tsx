'use client';
import { useEffect, useState, type ReactNode } from 'react';
import { ArrowRight, Check, Cpu, Leaf, ShieldCheck } from 'lucide-react';
import { validTariff, type Tariff } from '@/lib/operating-response';
import { UsageSharing } from './usage-sharing';
import { UpdateSettings } from './update-settings';
import {
  observeSupportCondition,
  recordSupportIssue,
  setSupportContext,
} from '@/lib/support-issues';

type Setup = {
  completed: boolean;
  localOnly: boolean;
  compatible?: boolean;
  macOS?: string;
  chip?: string;
  memoryGB?: number;
  providerInstalled?: boolean;
  loginPresent?: boolean;
  earningsConnected?: boolean;
  earningsConnection?: { status: string; detail: string };
  providerOnline?: boolean;
  mode?: string;
  tariff?: Tariff;
};

export function FirstLaunch({ children }: { children: ReactNode }) {
  const [data, setData] = useState<Setup | null>(null),
    [error, setError] = useState('');
  const [step, setStep] = useState(0),
    [saving, setSaving] = useState(false),
    [understood, setUnderstood] = useState(false);
  const [rate, setRate] = useState(''),
    [rateSaved, setRateSaved] = useState(false);
  useEffect(() => {
    const control = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function read() {
      const request = new AbortController();
      const cancel = () => request.abort();
      control.signal.addEventListener('abort', cancel, { once: true });
      const deadline = setTimeout(() => request.abort(), 12000);
      try {
        const response = await fetch('/api/setup', {
          signal: request.signal,
          cache: 'no-store',
        });
        if (!response.ok) throw Error('Setup could not connect to BloomGauge.');
        const result = (await response.json()) as Setup;
        if (
          !result ||
          typeof result.completed !== 'boolean' ||
          typeof result?.localOnly !== 'boolean'
        )
          throw Error('Setup received an incomplete response.');
        observeSupportCondition('connection', 'setup', false);
        if (!result.completed) {
          setSupportContext('setup');
          if (
            result.loginPresent &&
            result.earningsConnection?.status === 'sign_in_required'
          )
            recordSupportIssue('setup', 'setup');
        }
        setData((previous) => (previous?.completed ? previous : result));
        setError('');
        if (!result.completed) timer = setTimeout(read, 5000);
      } catch (e) {
        if (!control.signal.aborted) {
          observeSupportCondition('connection', 'setup', true);
          setError(e instanceof Error ? e.message : 'Reconnecting…');
          timer = setTimeout(read, 5000);
        }
      } finally {
        clearTimeout(deadline);
        control.signal.removeEventListener('abort', cancel);
      }
    }
    void read();
    return () => {
      control.abort();
      clearTimeout(timer);
      observeSupportCondition('connection', 'setup', false);
    };
  }, []);
  async function finish() {
    setSaving(true);
    setError('');
    try {
      const response = await fetch('/api/setup', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Bloom-Action': 'setup',
        },
        body: JSON.stringify({ action: 'complete', understood }),
        signal: AbortSignal.timeout(12000),
      });
      const result = (await response.json()) as {
        completed?: boolean;
        error?: string;
      };
      if (!response.ok || result?.completed !== true)
        throw Error(result?.error || 'Could not finish setup.');
      setData((d) => (d ? { ...d, completed: true } : d));
    } catch (e) {
      recordSupportIssue('setup', 'setup');
      setError(e instanceof Error ? e.message : 'Could not finish setup.');
    } finally {
      setSaving(false);
    }
  }
  async function saveRate() {
    if (!data?.tariff || rate.trim() === '') return;
    setSaving(true);
    setError('');
    try {
      const response = await fetch('/api/energy/tariff', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Bloom-Action': 'energy-tariff',
        },
        body: JSON.stringify({
          expectedId: data.tariff.id,
          rate: Number(rate),
          label: 'My electricity rate',
        }),
        signal: AbortSignal.timeout(12000),
      });
      const result: unknown = await response.json();
      if (!response.ok || !validTariff(result))
        throw Error('Could not save the rate. Refresh and check the value.');
      setData((d) => (d ? { ...d, tariff: result } : d));
      setRateSaved(true);
    } catch (e) {
      recordSupportIssue('action', 'setup');
      setError(e instanceof Error ? e.message : 'Could not save the rate.');
    } finally {
      setSaving(false);
    }
  }
  if (data?.completed) return <>{children}</>;
  return (
    <main className="setup-shell">
      <div className="setup-brand">
        <Leaf size={25} /> BloomGauge <span>FOR MAC · PRIVATE BETA</span>
      </div>
      <section className="setup-card" aria-label="Set up BloomGauge">
        {!data ? (
          <>
            <h1>Getting BloomGauge ready.</h1>
            <p>Checking the local service on your Mac…</p>
          </>
        ) : data.localOnly ? (
          <>
            <h1>Start on your Mac.</h1>
            <p>
              Open BloomGauge on the Mac to finish setup. Your account connection
              stays on that device.
            </p>
          </>
        ) : (
          <>
            <ol className="setup-steps" aria-label="Setup progress">
              {['Your Mac', 'Make it yours', 'Ready to observe'].map(
                (title, i) => (
                  <li
                    key={title}
                    aria-current={step === i ? 'step' : undefined}
                  >
                    <span>{i < step ? <Check size={14} /> : i + 1}</span>
                    {title}
                  </li>
                ),
              )}
            </ol>
            {step === 0 ? (
              <>
                <div className="eyebrow">YOUR MAC, IN VIEW</div>
                <h1>Meet your new control room.</h1>
                <p>
                  See your Darkbloom earnings, understand demand, and let your
                  Mac’s own results guide better model choices.
                </p>
                <div className="setup-checks">
                  <CheckRow
                    good={!!data.compatible}
                    title={data.chip || 'Apple Silicon Mac'}
                    detail={`${data.macOS ? `macOS ${data.macOS}` : 'Checking macOS'}${data.memoryGB ? ` · ${Math.round(data.memoryGB)} GB memory` : ''}. This beta targets Apple Silicon on macOS 14 or later; provider and model requirements may be higher.`}
                  />
                  <CheckRow
                    good={!!data.providerInstalled}
                    title="Darkbloom provider"
                    detail={
                      data.providerInstalled
                        ? data.providerOnline
                          ? 'Installed and running. BloomGauge will observe the current model.'
                          : 'Installed. After setup, use the manual model controls in Optimizer → Overview to choose and start a model.'
                        : 'Install and sign in to Darkbloom first. Then use the manual model controls in Optimizer → Overview for everyday start, stop and model changes.'
                    }
                  />
                  <CheckRow
                    good={!!data.earningsConnected}
                    title="Confirmed earnings"
                    detail={
                      data.earningsConnection?.detail ||
                      (data.earningsConnected
                        ? 'Connected to your existing Darkbloom login.'
                        : data.loginPresent
                          ? 'Login found. Checking the earnings connection…'
                          : 'Sign in using Darkbloom on this Mac. Credentials stay on your Mac.')
                    }
                  />
                </div>
                <a
                  className="text-link"
                  href="https://console.darkbloom.dev/providers/setup"
                  target="_blank"
                  rel="noreferrer"
                >
                  Open Darkbloom setup ↗
                </a>
                <p className="footnote">
                  Existing provider required for earning. Stats is optional.
                  Monitor history can enrich reports when installed. Sensor and
                  model compatibility are still being validated on other Macs.
                </p>
              </>
            ) : step === 1 ? (
              <>
                <div className="eyebrow">A LITTLE PERSONALIZATION</div>
                <h1>Your data. Your Mac.</h1>
                <p>
                  BloomGauge keeps history locally. New installs begin with empty
                  model evidence, so forecasts improve as paid work is recorded.
                </p>
                <div className="setup-note">
                  <ShieldCheck />
                  <div>
                    <strong>Reporting is free. Automation is optional.</strong>
                    <p>
                      Every dashboard, manual control and optimizer feature is
                      included. We’re evaluating whether the optimizer improves
                      earnings over Darkbloom alone. Choose Optimizer on when
                      you’re ready. Your saved Manual choice or paused plan
                      stays unchanged.
                    </p>
                  </div>
                </div>
                <form
                  className="setup-rate"
                  onSubmit={(e) => {
                    e.preventDefault();
                    void saveRate();
                  }}
                >
                  <label htmlFor="setup-rate">
                    Electricity price <span>Optional · USD per kWh</span>
                  </label>
                  <div>
                    <input
                      id="setup-rate"
                      inputMode="decimal"
                      type="number"
                      min="0"
                      max="10"
                      step="any"
                      value={rate}
                      onChange={(e) => {
                        setRate(e.target.value);
                        setRateSaved(false);
                      }}
                      placeholder="e.g. 0.15"
                    />
                    <button
                      className="secondary-button"
                      disabled={saving || !rate.trim()}
                      type="submit"
                    >
                      {rateSaved ? 'Saved' : 'Save rate'}
                    </button>
                  </div>
                  <p className="footnote">
                    Leave blank if unknown. Missing electricity cost stays
                    unknown, not $0. You can change this in More → Energy.
                  </p>
                </form>
                <UsageSharing setup />
              </>
            ) : (
              <>
                <div className="eyebrow">READY WHEN YOU ARE</div>
                <h1>Start with a clear view.</h1>
                <div className="setup-note">
                  <Cpu />
                  <div>
                    <strong>Close the window. Keep the work going.</strong>
                    <p>
                      BloomGauge stays in the menu bar to collect data and run any
                      optimizer you enable. Quit BloomGauge stops its monitoring and
                      automation; your Darkbloom provider keeps its own state.
                    </p>
                  </div>
                </div>
                <div className="setup-note">
                  <ShieldCheck />
                  <div>
                    <strong>Phone access is optional.</strong>
                    <p>
                      Connect it later in More → Phone access using your own
                      Tailscale account. Account setup stays on the Mac.
                    </p>
                  </div>
                </div>
                <UpdateSettings onboarding />
                <label className="setup-consent">
                  <input
                    type="checkbox"
                    checked={understood}
                    onChange={(e) => setUnderstood(e.target.checked)}
                  />
                  <span>
                    I understand BloomGauge begins in observation mode. Earnings
                    vary; forecasts are estimates, not guaranteed income.
                  </span>
                </label>
              </>
            )}
            <div className="setup-actions">
              {step > 0 ? (
                <button
                  className="text-link"
                  disabled={saving}
                  onClick={() => setStep((s) => s - 1)}
                >
                  Back
                </button>
              ) : (
                <span />
              )}
              <button
                className="setup-primary"
                disabled={saving || (step === 2 && !understood)}
                onClick={() =>
                  step < 2 ? setStep((s) => s + 1) : void finish()
                }
              >
                {saving
                  ? 'Saving…'
                  : step === 2
                    ? 'Open dashboard'
                    : 'Continue'}
                <ArrowRight size={18} />
              </button>
            </div>
          </>
        )}
        {error && (
          <p className="notice" role="alert">
            {error}
          </p>
        )}
      </section>
      <p className="setup-footer">
        Independent companion for Darkbloom. Your account and history stay on
        this Mac.
      </p>
    </main>
  );
}
function CheckRow({
  good,
  title,
  detail,
}: {
  good: boolean;
  title: string;
  detail: string;
}) {
  return (
    <div className="setup-check">
      <span className={good ? 'good' : 'pending'}>
        {good ? <Check size={16} /> : <span>·</span>}
      </span>
      <div>
        <strong>{title}</strong>
        <p>{detail}</p>
      </div>
    </div>
  );
}
