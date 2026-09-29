'use client';
import { recordSupportIssue } from '@/lib/support-issues';
import { startChartPolling } from '@/lib/chart-polling';
import { usePageVisible } from '@/lib/use-page-visibility';
import { useScreenActive } from './app-navigation';
import { useEffect, useRef, useState } from 'react';
import {
  Check,
  Copy,
  ExternalLink,
  ShieldCheck,
  Smartphone,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible';

type Access = {
  status: string;
  detail: string;
  url?: string | null;
  authURL?: string | null;
  owner?: string;
  canManage: boolean;
  canEnable: boolean;
  enabled?: boolean;
  error?: string | null;
  operationPending?: boolean;
  operationRevision?: number;
  statusRevision?: number;
  statusInstance?: string;
};

function validAccess(value: unknown): value is Access {
  if (!value || typeof value !== 'object') return false;
  const access = value as Access;
  return (
    typeof access.status === 'string' &&
    typeof access.detail === 'string' &&
    typeof access.canManage === 'boolean' &&
    typeof access.canEnable === 'boolean' &&
    (access.operationPending === undefined ||
      typeof access.operationPending === 'boolean') &&
    (access.statusInstance === undefined ||
      (typeof access.statusInstance === 'string' &&
        /^[a-f0-9]{32}$/.test(access.statusInstance))) &&
    [access.operationRevision, access.statusRevision].every(
      (value) =>
        value === undefined || (Number.isSafeInteger(value) && value >= 0),
    )
  );
}

export function PhoneAccess({ alwaysOpen = false }: { alwaysOpen?: boolean }) {
  const pageVisible = usePageVisible();
  const screenActive = useScreenActive();
  const [expanded, setOpen] = useState(false),
    [data, setData] = useState<Access | null>(null);
  const [error, setError] = useState(''),
    [busy, setBusy] = useState(false),
    [copied, setCopied] = useState(false),
    [needsRefresh, setNeedsRefresh] = useState(false),
    [refreshKey, setRefreshKey] = useState(0);
  const open = alwaysOpen || expanded;
  const address = useRef<HTMLInputElement>(null);
  const actionRequest = useRef<AbortController | null>(null);
  const updateEpoch = useRef(0);
  const reconciliation = useRef<{
    operation: number;
    status: number;
    instance?: string;
  } | null>(null);
  useEffect(
    () => () => {
      actionRequest.current?.abort();
      actionRequest.current = null;
    },
    [],
  );
  useEffect(() => {
    if (!pageVisible || !screenActive || busy) return;
    return startChartPolling({
      issueContext: 'phone',
      load: async (signal) => {
        const epoch = updateEpoch.current;
        const response = await fetch('/api/remote', {
          cache: 'no-store',
          signal,
        });
        if (!response.ok)
          throw Error(
            'Connection settings are unavailable. Check that BloomGauge is open on your Mac.',
          );
        const value: unknown = await response.json();
        if (!validAccess(value))
          throw Error('Connection settings are not ready.');
        return { value, epoch };
      },
      onValue: ({ value, epoch }) => {
        if (epoch !== updateEpoch.current || actionRequest.current) return;
        setData(value);
        const baseline = reconciliation.current;
        const restarted =
          baseline?.instance &&
          value.statusInstance &&
          baseline.instance !== value.statusInstance &&
          (value.statusRevision ?? 0) > 0;
        if (
          baseline &&
          (value.operationPending !== false ||
            !(
              restarted ||
              (value.operationRevision ?? -1) > baseline.operation ||
              (value.statusRevision ?? -1) > baseline.status
            ))
        )
          return;
        reconciliation.current = null;
        setError('');
        setNeedsRefresh(false);
      },
      onError: (error) => {
        if (!actionRequest.current) setError(error.message);
      },
      intervalMs: open ? 5000 : 30000,
    });
  }, [open, pageVisible, screenActive, busy, refreshKey]);
  async function change(action: 'enable' | 'disable') {
    if (
      actionRequest.current ||
      needsRefresh ||
      data?.operationPending ||
      !data?.canManage
    )
      return;
    const request = new AbortController();
    actionRequest.current = request;
    updateEpoch.current++;
    setBusy(true);
    setError('');
    let deadline: ReturnType<typeof setTimeout> | undefined;
    try {
      const timeout = new Promise<never>((_, reject) => {
        deadline = setTimeout(() => {
          reject(Error('Phone setup took too long to respond.'));
          request.abort();
        }, 30000);
      });
      const value = await Promise.race([
        (async () => {
          const response = await fetch('/api/remote', {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
              'X-Bloom-Action': 'remote-access',
            },
            body: JSON.stringify({ action }),
            signal: request.signal,
          });
          const value: unknown = await response.json();
          if (!response.ok) {
            const message =
              value && typeof value === 'object' && 'error' in value
                ? value.error
                : null;
            throw Error(
              typeof message === 'string'
                ? message
                : 'Could not change phone access.',
            );
          }
          if (!validAccess(value))
            throw Error('Phone setup returned an incomplete response.');
          return value;
        })(),
        timeout,
      ]);
      if (actionRequest.current !== request) return;
      setData(value);
    } catch (e) {
      if (actionRequest.current !== request) return;
      recordSupportIssue('action', 'phone');
      reconciliation.current = {
        operation: data.operationRevision ?? -1,
        status: data.statusRevision ?? -1,
        instance: data.statusInstance,
      };
      setNeedsRefresh(true);
      setError(
        e instanceof Error ? e.message : 'Could not change phone access.',
      );
    } finally {
      clearTimeout(deadline);
      if (actionRequest.current === request) {
        actionRequest.current = null;
        setBusy(false);
      }
    }
  }
  async function copy() {
    if (!data?.url) return;
    try {
      await navigator.clipboard.writeText(data.url);
      setCopied(true);
      setTimeout(() => setCopied(false), 2500);
    } catch {
      address.current?.focus();
      address.current?.select();
      setError('The address is selected. Copy it using the Edit menu.');
    }
  }
  return (
    <Collapsible open={open} onOpenChange={setOpen} className="phone-access">
      {!alwaysOpen && (
        <CollapsibleTrigger className="phone-trigger">
          <Smartphone size={17} />
          <span>
            {data?.canManage === false ? 'Private connection' : 'Phone access'}
          </span>
          <span
            className={`phone-state ${data?.status === 'enabled' ? 'good' : ''}`}
          >
            {data?.status === 'enabled' ? 'Connected' : 'Set up'}
          </span>
          <span className="phone-expand">{open ? '−' : '+'}</span>
        </CollapsibleTrigger>
      )}
      <CollapsibleContent>
        <section
          className="panel phone-panel"
          aria-label="Private phone access"
        >
          <div className="panel-heading">
            <div>
              <div className="eyebrow">PRIVATE ACCESS</div>
              <h2>Your dashboard, wherever you are.</h2>
            </div>
            <ShieldCheck size={23} />
          </div>
          <p className="phone-detail" role="status">
            {data?.detail || 'Checking connection settings…'}
          </p>
          {(error || data?.error) && (
            <p className="notice" role="alert">
              {error || data?.error}
            </p>
          )}
          {needsRefresh && (
            <p className="panel-note" role="status">
              The request was not confirmed; phone access may have changed.
              Refreshing current settings before another change.
            </p>
          )}
          {data?.operationPending && (
            <p className="panel-note" role="status">
              Phone access is still updating on this Mac. Wait for the current
              operation to finish.
            </p>
          )}
          {(error || needsRefresh) && !busy && (
            <Button
              variant="outline"
              onClick={() => setRefreshKey((value) => value + 1)}
            >
              Refresh connection status
            </Button>
          )}
          {data?.url && (
            <div className="phone-address">
              <label htmlFor="phone-url">Your private dashboard link</label>
              <div>
                <Input
                  id="phone-url"
                  ref={address}
                  value={data.url}
                  readOnly
                  onFocus={(event) => event.target.select()}
                />
                <Button variant="outline" onClick={copy}>
                  {copied ? <Check /> : <Copy />}
                  {copied ? 'Copied' : 'Copy'}
                </Button>
              </div>
              {data.canManage && (
                <img
                  className="phone-qr"
                  src={`/api/remote/qr.png?url=${encodeURIComponent(data.url)}`}
                  alt="Scan this code with your phone camera to open your private dashboard"
                  width={164}
                  height={164}
                />
              )}
            </div>
          )}
          {data?.canManage !== false ? (
            <>
              <ol className="phone-steps">
                <li>
                  <strong>Connect this Mac.</strong>
                  <span>
                    <a
                      href="https://tailscale.com/download/mac"
                      target="_blank"
                      rel="noreferrer"
                    >
                      Install Tailscale <ExternalLink size={13} />
                    </a>
                    , open it, and sign in. Allow its VPN connection when macOS
                    asks.
                  </span>
                </li>
                <li>
                  <strong>Connect your phone.</strong>
                  <span>
                    Install Tailscale for{' '}
                    <a
                      href="https://tailscale.com/download/ios"
                      target="_blank"
                      rel="noreferrer"
                    >
                      iPhone
                    </a>{' '}
                    or{' '}
                    <a
                      href="https://tailscale.com/download/android"
                      target="_blank"
                      rel="noreferrer"
                    >
                      Android
                    </a>
                    . Sign in{' '}
                    {data?.owner ? (
                      <>
                        as <b>{data.owner}</b>
                      </>
                    ) : (
                      'to the same account'
                    )}{' '}
                    and turn the connection on.
                  </span>
                </li>
                <li>
                  <strong>Open your dashboard.</strong>
                  <span>
                    Enable phone access below, then scan the code or open the
                    private link in your phone’s browser. It works over cellular
                    and Wi-Fi.
                  </span>
                </li>
              </ol>
              <div className="phone-actions">
                {data?.authURL && (
                  <a
                    className="text-link"
                    href={data.authURL}
                    target="_blank"
                    rel="noreferrer"
                  >
                    Finish Tailscale HTTPS setup <ExternalLink size={15} />
                  </a>
                )}
                {data?.status !== 'enabled' && (
                  <Button
                    disabled={
                      !data?.canEnable ||
                      busy ||
                      needsRefresh ||
                      data?.operationPending
                    }
                    onClick={() => change('enable')}
                  >
                    {busy ? 'Connecting…' : 'Enable phone access'}
                  </Button>
                )}
                {data?.enabled && (
                  <Button
                    variant="outline"
                    disabled={busy || needsRefresh || data?.operationPending}
                    onClick={() => change('disable')}
                  >
                    {busy ? 'Updating…' : 'Turn off phone access'}
                  </Button>
                )}
              </div>
            </>
          ) : (
            <p className="panel-note">
              Connection settings are managed in BloomGauge on your Mac.
            </p>
          )}
          <p className="panel-note">
            Keep this Mac awake, connected to the internet, and running BloomGauge
            Dashboard. Its window can be closed; BloomGauge stays in the menu bar.
            Your readings and saved history stay on this Mac.
          </p>
          <p className="panel-note">
            For a shortcut on your phone, open this dashboard in your browser
            and choose Add to Home Screen from the Share or browser menu.
          </p>
        </section>
      </CollapsibleContent>
    </Collapsible>
  );
}
