'use client';
import { recordSupportIssue } from '@/lib/support-issues';
import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowRight, Laptop, Plus, RefreshCw } from 'lucide-react';
import { useAppNavigation, useScreenActive } from './app-navigation';
import { money, shortModel } from './shared';
import { demandLabel, validFleetModels, type FleetModel } from '@/lib/fleet';

type Summary = {
  at: number | null;
  from: number;
  to: number;
  name: string;
  chip: string;
  memoryGB: number | null;
  models: string[];
  ready: boolean;
  switching: boolean;
  optimizer: string;
  cpuPercent: number | null;
  gpuPercent: number | null;
  cpuTempF: number | null;
  gpuTempF: number | null;
  ratePerHour: number | null;
  knownInferenceUsd: number | null;
  earningsFresh: boolean;
  coveredSeconds: number;
  coverageSeconds: number;
};
type Mac = {
  installation: string;
  name: string;
  local: boolean;
  url: string | null;
  status:
    | 'connected'
    | 'connecting'
    | 'stale'
    | 'unreachable'
    | 'identity-changed'
    | 'duplicate-device';
  included: boolean;
  report: Summary | null;
};
type Fleet = {
  at: number;
  hours: number;
  canManage: boolean;
  limit: number;
  machines: Mac[];
  knownInferenceUsd: number | null;
  included: number;
  complete: boolean;
  managedRemoteAvailable: false;
  ratePerHour: number | null;
  rateMacs: number;
  models: FleetModel[];
};
const percent = (n: number | null) => (n == null ? '—' : `${Math.round(n)}%`);
const modes: Record<string, string> = {
  observe: 'Observing',
  demand: 'Following demand',
  week: 'Scheduled trials',
  optimize: 'Optimizing',
  combo: 'Pair trials',
};
const statuses: Record<Mac['status'], string> = {
  connected: 'Connected',
  connecting: 'Connecting',
  stale: 'Readings stale',
  unreachable: 'Unreachable',
  'identity-changed': 'Mac identity changed',
  'duplicate-device': 'Duplicate device',
};
function valid(v: unknown): v is Fleet {
  const d = v as Fleet;
  return (
    !!d &&
    Number.isFinite(d.at) &&
    [1, 24, 168].includes(d.hours) &&
    typeof d.canManage === 'boolean' &&
    d.limit === 10 &&
    Array.isArray(d.machines) &&
    d.machines.length > 0 &&
    d.machines.length <= 10 &&
    (d.ratePerHour === null || Number.isFinite(d.ratePerHour)) &&
    Number.isInteger(d.rateMacs) &&
    validFleetModels(d.models) &&
    d.machines.every(
      (m) =>
        typeof m.name === 'string' &&
        /^[a-f0-9]{32}$/.test(m.installation) &&
        m.status in statuses &&
        typeof m.local === 'boolean' &&
        (m.local ||
          (typeof m.url === 'string' &&
            /^https:\/\/[a-z0-9-]+\.[a-z0-9-]+\.ts\.net:8443$/.test(m.url))) &&
        (m.report === null ||
          (Array.isArray(m.report.models) &&
            m.report.models.every((x) => typeof x === 'string') &&
            typeof m.report.coverageSeconds === 'number')),
    )
  );
}

export function MyMacs({ paused = false }: { paused?: boolean }) {
  const active = useScreenActive(),
    { navigate } = useAppNavigation();
  const [hours, setHours] = useState(24),
    [data, setData] = useState<Fleet | null>(null),
    [error, setError] = useState('');
  const [name, setName] = useState(''),
    [url, setUrl] = useState(''),
    [ownName, setOwnName] = useState('');
  const [message, setMessage] = useState(''),
    [saveError, setSaveError] = useState(''),
    [saving, setSaving] = useState(false);
  const version = useRef(0),
    pending = useRef(false);
  const refresh = useCallback(
    async (signal?: AbortSignal) => {
      const own = ++version.current;
      try {
        const response = await fetch(`/api/machines?hours=${hours}`, {
          cache: 'no-store',
          signal: signal
            ? AbortSignal.any([signal, AbortSignal.timeout(10000)])
            : AbortSignal.timeout(10000),
        });
        if (!response.ok)
          throw Error(
            'Could not refresh your Macs. Keep the dashboard Mac awake and connected.',
          );
        const value: unknown = await response.json();
        if (!valid(value) || value.hours !== hours)
          throw Error(
            'Could not read this fleet response. Update Bloomkeeper on each Mac.',
          );
        if (own === version.current) {
          setData(value);
          setError('');
        }
      } catch (e) {
        if (own === version.current && !signal?.aborted)
          setError(
            e instanceof Error
              ? e.message
              : 'Macs are temporarily unavailable.',
          );
      }
    },
    [hours],
  );
  useEffect(() => {
    if (!active || paused) return;
    const controller = new AbortController();
    let alive = true,
      busy = false;
    async function poll() {
      if (busy || document.visibilityState !== 'visible') return;
      busy = true;
      try {
        await refresh(controller.signal);
      } finally {
        busy = false;
      }
    }
    void poll();
    const timer = setInterval(() => {
      if (alive) void poll();
    }, 5000);
    const visible = () => {
      if (document.visibilityState === 'visible') void poll();
    };
    document.addEventListener('visibilitychange', visible);
    return () => {
      alive = false;
      controller.abort();
      version.current++;
      clearInterval(timer);
      document.removeEventListener('visibilitychange', visible);
    };
  }, [active, paused, refresh]);
  async function save(body: Record<string, string>) {
    if (pending.current) return;
    pending.current = true;
    setSaving(true);
    setMessage('');
    setSaveError('');
    try {
      const response = await fetch('/api/machines', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Bloom-Action': 'machines',
        },
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(12000),
      });
      const value = (await response.json()) as {
        saved?: boolean;
        error?: string;
      };
      if (!response.ok || value.saved !== true)
        throw Error(
          typeof value.error === 'string'
            ? value.error
            : 'Could not save this Mac connection.',
        );
      if (body.action === 'add') {
        setName('');
        setUrl('');
      }
      setMessage(
        body.action === 'remove'
          ? 'Mac removed from this dashboard. It keeps running independently.'
          : body.action === 'rename'
            ? 'Mac name saved.'
            : 'Mac connected. Its first readings will appear shortly.',
      );
      await refresh();
    } catch (e) {
      recordSupportIssue('action', 'overview');
      setSaveError(
        e instanceof Error
          ? e.message
          : 'Could not save this connection. Refresh before retrying.',
      );
    } finally {
      pending.current = false;
      setSaving(false);
    }
  }
  const current = data?.hours === hours ? data : null;
  const live = !!current && !error && !paused;
  const ready =
    current?.machines.filter((m) => m.status === 'connected' && m.report?.ready)
      .length || 0;
  const own = current?.machines.find((m) => m.local);
  const range =
    hours === 1 ? 'last hour' : hours === 24 ? 'last 24 hours' : 'last 7 days';
  return (
    <section className="my-macs" aria-label="My Macs">
      <div className="panel-heading">
        <div>
          <div className="eyebrow">MY MACS</div>
          <h1>One view. Every Mac.</h1>
          <p>
            Private monitoring for up to ten Macs: combined pace, what each one
            serves, and how busy those models are. Each optimizer runs on its
            own machine.
          </p>
        </div>
        <label className="mac-range">
          Earnings window
          <select
            value={hours}
            onChange={(e) => {
              setHours(Number(e.target.value));
              setData(null);
            }}
          >
            <option value={1}>Last hour</option>
            <option value={24}>Last 24 hours</option>
            <option value={168}>Last 7 days</option>
          </select>
        </label>
      </div>
      {error && (
        <div className="notice" role="alert">
          {error}{' '}
          <button className="text-link" onClick={() => void refresh()}>
            Retry
          </button>
        </div>
      )}
      {paused && (
        <div className="notice">
          View paused. Each Mac keeps collecting and optimizing.
        </div>
      )}
      <div className="mac-summary-grid">
        <article className="panel">
          <span className="eyebrow">COMBINED LIVE PACE</span>
          <strong className="mac-total">
            {money(live ? (current?.ratePerHour ?? null) : null)}
            <small> /hr</small>
          </strong>
          <p>
            {current
              ? `${current.rateMacs} of ${current.machines.length} Macs reporting a live pace`
              : 'Loading your Macs…'}{' '}
            · last 5 minutes
          </p>
        </article>
        <article className="panel">
          <span className="eyebrow">
            KNOWN INFERENCE · {range.toUpperCase()}
          </span>
          <strong className="mac-total">
            {money(current?.knownInferenceUsd ?? null)}
          </strong>
          <p>
            {current
              ? `${current.included} of ${current.machines.length} Macs included`
              : 'Loading your Macs…'}
            {current && !current.complete ? ' · partial coverage' : ''}
            {!live && current ? ' · last readings' : ''}
          </p>
        </article>
        <article className="panel">
          <span className="eyebrow">WARM & SERVING</span>
          <strong className="mac-total">
            {current ? ready : '—'}
            <small> / {current?.machines.length ?? '—'}</small>
          </strong>
          <p>
            {live
              ? 'Readings refresh automatically.'
              : paused
                ? 'Paused display.'
                : 'Waiting for current readings.'}{' '}
            This is readiness, not a guarantee of paid traffic.
          </p>
        </article>
      </div>
      {current && current.models.length > 0 && (
        <section
          className="panel mac-models-panel"
          aria-label="Models across your Macs"
        >
          <h2>Models across your Macs</h2>
          <div className="mac-models-table" role="table">
            <div role="row" className="mac-models-head">
              <span role="columnheader">Model</span>
              <span role="columnheader">Serving on</span>
              <span role="columnheader">Pace</span>
              <span role="columnheader">Network demand</span>
            </div>
            {current.models.map((m) => {
              const d = demandLabel(m.demand);
              const names = m.macs.map(
                (id) =>
                  current.machines.find((x) => x.installation === id)?.name ??
                  'Mac',
              );
              return (
                <div role="row" key={m.model}>
                  <span role="cell" className="mac-model-name">
                    {shortModel(m.model)}
                  </span>
                  <span role="cell">{names.join(', ')}</span>
                  <span role="cell">
                    {money(live ? m.ratePerHour : null)}
                    <small> /hr</small>
                  </span>
                  <span role="cell" className={`mac-demand is-${d.tone}`}>
                    {d.text}
                  </span>
                </div>
              );
            })}
          </div>
          <p className="footnote">
            Pace is each live Mac’s confirmed pay over its last 5 warm minutes,
            as everywhere in Bloomkeeper; a pair’s pace is split between its two
            models. Demand is the network’s requests per warm Mac for that model
            now, compared with its usual week, read on this Mac.
          </p>
        </section>
      )}
      <details className="mac-explainer">
        <summary>How fleet earnings are counted</summary>
        <p className="footnote">
          Inference credits attributed to each Mac only. Account balances and
          base rewards are excluded to prevent double counting. Missing history
          stays partial; stale, disconnected or duplicate Macs are excluded from
          the current total.
        </p>
      </details>
      <div className="mac-cards">
        {current?.machines.map((mac) => {
          const r = mac.report,
            connected = live && mac.status === 'connected';
          const state = connected
            ? r?.switching
              ? 'Switching model'
              : r?.ready
                ? 'Warm & serving'
                : 'Not ready'
            : !live && mac.status === 'connected'
              ? 'Last readings'
              : statuses[mac.status];
          const coverage = r
            ? Math.min(
                100,
                Math.max(0, (100 * r.coveredSeconds) / r.coverageSeconds),
              )
            : 0;
          return (
            <article
              key={mac.installation}
              className={`panel mac-card ${connected && r?.ready ? 'is-ready' : ''}`}
              aria-label={mac.name}
            >
              <header>
                <Laptop size={23} />
                <div>
                  <h2>{mac.name}</h2>
                  <span>
                    {mac.local ? 'Dashboard host · ' : ''}
                    {r?.chip || 'Apple Silicon'}
                    {r?.memoryGB ? ` · ${r.memoryGB} GB` : ''}
                  </span>
                </div>
              </header>
              <span
                className={`mac-status ${connected && r?.ready ? 'is-ready' : ''}`}
              >
                {state}
              </span>
              <p className="mac-models">
                {r?.models.length
                  ? r.models.map(shortModel).join(' + ')
                  : 'No verified model reading'}
              </p>
              {r?.models.length === 1 &&
                (() => {
                  const d = demandLabel(
                    current.models.find((m) => m.model === r.models[0])
                      ?.demand ?? null,
                  );
                  return (
                    d.tone !== 'unknown' && (
                      <span className={`mac-demand is-${d.tone}`}>
                        {d.text}
                      </span>
                    )
                  );
                })()}
              <div className="mac-metrics">
                <div>
                  <span>Inference · {range}</span>
                  <strong>{money(r?.knownInferenceUsd ?? null)}</strong>
                </div>
                <div>
                  <span>Live pace · 5 minutes</span>
                  <strong>
                    {money(connected ? (r?.ratePerHour ?? null) : null)}
                    <small> /hr</small>
                  </strong>
                </div>
              </div>
              <details className="mac-details">
                <summary>Hardware, coverage & freshness</summary>
                <div className="mac-coverage">
                  <div>
                    <span>Settled coverage</span>
                    <span>{r ? `${Math.round(coverage)}%` : 'Unknown'}</span>
                  </div>
                  <progress
                    value={coverage}
                    max={100}
                    aria-label={`${mac.name} earnings coverage`}
                  />
                </div>
                {r && (
                  <>
                    <div className="mac-hardware">
                      <span>
                        CPU <b>{percent(connected ? r.cpuPercent : null)}</b>
                      </span>
                      <span>
                        GPU <b>{percent(connected ? r.gpuPercent : null)}</b>
                      </span>
                      <span>
                        GPU{' '}
                        <b>
                          {connected && r.gpuTempF != null
                            ? `${Math.round(r.gpuTempF)}°F`
                            : '—'}
                        </b>
                      </span>
                    </div>
                    <p className="footnote">
                      {modes[r.optimizer] || 'Observing'}
                      {!r.earningsFresh ? ' · earnings need a fresh sync' : ''}
                    </p>
                  </>
                )}
                {r?.at != null && (
                  <p className="footnote">
                    Last reading {new Date(r.at * 1000).toLocaleString()}
                  </p>
                )}
              </details>
              <p className="footnote">
                {r ? modes[r.optimizer] || 'Observing' : 'Awaiting readings'}
                {r && !r.earningsFresh ? ' · earnings need a fresh sync' : ''}
              </p>
              {mac.status === 'identity-changed' && (
                <p className="notice">
                  This address now identifies a different installation.
                  Reconnect it on the dashboard host Mac.
                </p>
              )}
              {mac.status === 'unreachable' && (
                <p className="footnote">
                  Check that this Mac is awake, Bloomkeeper is running and Tailscale
                  is connected.
                </p>
              )}
              {mac.status === 'duplicate-device' && (
                <p className="footnote">
                  Another installation is reporting the same physical device. It
                  is counted once.
                </p>
              )}
              <footer>
                {mac.local ? (
                  <button
                    className="text-link"
                    onClick={() => navigate('overview')}
                  >
                    Open this Mac <ArrowRight size={14} aria-hidden="true" />
                  </button>
                ) : mac.status === 'identity-changed' ? (
                  <span className="footnote">Reconnect before opening</span>
                ) : (
                  <a
                    className="text-link"
                    href={`${mac.url}/?screen=overview`}
                    target="_blank"
                    rel="noreferrer"
                  >
                    Open {mac.name} <ArrowRight size={14} aria-hidden="true" />
                  </a>
                )}
                {!mac.local && current.canManage && (
                  <button
                    className="text-link muted"
                    disabled={saving}
                    onClick={() =>
                      void save({
                        action: 'remove',
                        installation: mac.installation,
                      })
                    }
                  >
                    Remove
                  </button>
                )}
              </footer>
            </article>
          );
        })}
      </div>
      <p className="footnote">
        Open a specific Mac to use its full charts or controls in a separate
        dashboard. Connections here are for monitoring; changing one Mac never
        changes another.
      </p>
      {current?.canManage ? (
        <section className="panel mac-connections">
          <div className="panel-heading">
            <div>
              <div className="eyebrow">PRIVATE CONNECTIONS</div>
              <h2>Bring your Macs together.</h2>
            </div>
            <Plus size={22} />
          </div>
          <ol className="mac-setup-steps">
            <li>
              <strong>Install Bloomkeeper on each Mac</strong> and finish its setup.
              Each Mac keeps running its own optimizer.
            </li>
            <li>
              <strong>Install Tailscale on every Mac</strong>, signed in to the
              same account.
            </li>
            <li>
              <strong>On the other Mac</strong>, open More → Phone access,
              choose Enable phone access and copy its private address (it ends
              in <code>.ts.net:8443</code>).
            </li>
            <li>
              <strong>Here</strong>, give that Mac a name and paste the address.
              Repeat for each Mac, up to ten.
            </li>
          </ol>
          <p className="footnote">
            Connections are read-only and private to your Tailscale account.
            Nothing goes through a Bloomkeeper server, and it’s free.
          </p>
          <form
            className="mac-rename"
            onSubmit={(e) => {
              e.preventDefault();
              void save({ action: 'rename', name: ownName });
            }}
          >
            <label>
              Name this dashboard Mac
              <input
                value={ownName}
                placeholder={own?.name || 'This Mac'}
                maxLength={48}
                onChange={(e) => setOwnName(e.target.value)}
                autoComplete="off"
              />
            </label>
            <button
              className="setup-secondary"
              disabled={saving || !ownName.trim()}
            >
              Save name
            </button>
          </form>
          {current.machines.length < current.limit ? (
            <form
              className="mac-add-form"
              onSubmit={(e) => {
                e.preventDefault();
                void save({ action: 'add', name, url });
              }}
            >
              <label>
                Other Mac’s name
                <input
                  value={name}
                  maxLength={48}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="Studio Mac"
                  autoComplete="off"
                />
              </label>
              <label>
                Its Phone access address
                <input
                  type="url"
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                  maxLength={220}
                  placeholder="Paste the private HTTPS address"
                  autoComplete="off"
                  spellCheck={false}
                />
              </label>
              <button
                className="setup-primary"
                disabled={saving || !name.trim() || !url.trim()}
              >
                {saving ? (
                  <>
                    <RefreshCw size={15} />
                    Checking…
                  </>
                ) : (
                  'Connect Mac'
                )}
              </button>
            </form>
          ) : (
            <p>
              All ten Mac slots are in use. Remove a connection to add another.
            </p>
          )}
          {message && (
            <p className="notice" role="status">
              {message}
            </p>
          )}
          {saveError && (
            <p className="notice" role="alert">
              {saveError}
            </p>
          )}
          <button className="text-link" onClick={() => navigate('access')}>
            Open Phone access setup <ArrowRight size={14} aria-hidden="true" />
          </button>
        </section>
      ) : current ? (
        <div className="notice">
          Add, rename or remove connections in Bloomkeeper on the dashboard host Mac.
          Your phone can monitor the whole group from this page.
        </div>
      ) : null}
      <section className="panel mac-connect-note">
        <div>
          <h2>Your Macs, together.</h2>
          <p>
            Monitor up to ten Macs with your own Tailscale setup. Each Mac keeps
            its own optimizer controls.
          </p>
        </div>
      </section>
    </section>
  );
}
