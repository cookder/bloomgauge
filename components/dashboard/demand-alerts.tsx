'use client';
import { useEffect, useRef, useState } from 'react';
import { Bell, TrendingUp } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { startChartPolling } from '@/lib/chart-polling';
import { usePageVisible } from '@/lib/use-page-visibility';
import { useAppNavigation, useScreenActive } from './app-navigation';
import { age, money, num, shortModel } from './shared';

type DemandModel = {
  model: string;
  status: string;
  detail: string;
  current: boolean;
  observedAt?: number;
  load: number | null;
  baselineLoad: number | null;
  loadRatio: number | null;
  pressureRatio: number | null;
  localRatePerHour: number | null;
  localWarmHours: number;
  baselineScope?: string;
  baselineHours?: number;
  baselineDays?: number;
};
type DemandState = {
  at: number;
  status: string;
  models: DemandModel[];
  alerts: { id: string; model: string; at: number }[];
};
type PushDevice = {
  id: string;
  status: string;
  lastAttemptAt?: number | null;
  lastSentAt?: number | null;
  lastError?: string | null;
  lastTestAt?: number | null;
  lastTestResultAt?: number | null;
  lastTestError?: string | null;
  retryAfter?: number | null;
};
type PushState = {
  supported: boolean;
  contactConfigured?: boolean;
  publicKey?: string;
  subscriptionCount: number;
  subscriptionId?: string;
  subscriptions?: PushDevice[];
  lastSentAt?: number | null;
  lastError?: string | null;
  detail?: string;
};
async function readJson<T>(url: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(url, { signal, cache: 'no-store' });
  if (!response.ok) throw new Error('Waiting for the dashboard connection.');
  return response.json();
}
async function savePush(body: object): Promise<PushState> {
  const controller = new AbortController();
  const deadline = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch('/api/demand-alerts/notifications', {
      method: 'POST',
      signal: controller.signal,
      headers: {
        'Content-Type': 'application/json',
        'X-Bloom-Action': 'demand-alerts',
      },
      body: JSON.stringify(body),
    });
    const data = (await response.json()) as PushState & { error?: string };
    if (!response.ok)
      throw new Error(data.error || 'Could not save notifications.');
    return data;
  } finally {
    clearTimeout(deadline);
  }
}
function applicationKey(encoded: string) {
  const value = atob(encoded.replace(/-/g, '+').replace(/_/g, '/'));
  return Uint8Array.from(value, (char) => char.charCodeAt(0));
}
async function bounded<T>(operation: Promise<T>): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      operation,
      new Promise<never>((_, reject) => {
        timer = setTimeout(
          () =>
            reject(
              new Error(
                'Notification setup timed out. Check this device’s permission, then retry.',
              ),
            ),
          20000,
        );
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

export function ModelSwitchNotifications() {
  const active = useScreenActive();
  const visible = usePageVisible();
  const [data, setData] = useState<PushState | null>(null);
  const [registered, setRegistered] = useState(false);
  const [deviceId, setDeviceId] = useState<string>();
  const [busy, setBusy] = useState(false);
  const [initializing, setInitializing] = useState(true);
  const [error, setError] = useState('');
  const [readError, setReadError] = useState('');
  const [testAt, setTestAt] = useState<number>();
  const writing = useRef(false);
  const supported =
    typeof window !== 'undefined' &&
    window.isSecureContext &&
    'Notification' in window &&
    'PushManager' in window &&
    'serviceWorker' in navigator;
  const device = data?.subscriptions?.find((s) => s.id === deviceId);
  const expired = device?.status === 'expired';
  const enabled = registered && !!device && !expired;
  const contactMissing = data?.contactConfigured === false;
  const coolingDown =
    Math.max(device?.retryAfter ?? 0, (device?.lastTestAt ?? 0) + 60) >
    Date.now() / 1000;
  const testComplete =
    testAt != null && (device?.lastTestResultAt ?? 0) >= testAt;
  useEffect(() => {
    const controller = new AbortController();
    const deadline = setTimeout(() => controller.abort(), 15000);
    let disposed = false;
    void (async () => {
      try {
        const state = await readJson<PushState>(
          '/api/demand-alerts/notifications',
          controller.signal,
        );
        if (disposed) return;
        setData(state);
        if (!supported || !state.supported) return;
        const registration = await bounded(
          navigator.serviceWorker.getRegistration('/'),
        );
        if (registration) await bounded(registration.update());
        const subscription = registration
          ? await bounded(registration.pushManager.getSubscription())
          : null;
        if (disposed) return;
        if (subscription && Notification.permission === 'granted') {
          const renewed = await savePush({
            action: 'subscribe',
            subscription: subscription.toJSON(),
          });
          if (!disposed) {
            setData(renewed);
            setDeviceId(renewed.subscriptionId);
            setRegistered(true);
          }
        }
      } catch (e) {
        if (!disposed)
          setError(
            e instanceof Error
              ? e.message
              : 'Could not load notification settings.',
          );
      } finally {
        clearTimeout(deadline);
        if (!disposed) setInitializing(false);
      }
    })();
    return () => {
      disposed = true;
      clearTimeout(deadline);
      controller.abort();
    };
  }, [supported]);

  useEffect(() => {
    if (!active || !visible || initializing || busy) return;
    return startChartPolling<PushState>({
      load: (signal) => readJson('/api/demand-alerts/notifications', signal),
      intervalMs: 5000,
      onValue: (state) => {
        if (writing.current) return;
        setData(state);
        setReadError('');
        if (supported && Notification.permission !== 'granted')
          setRegistered(false);
      },
      onError: (e) => {
        if (!writing.current) setReadError(e.message);
      },
    });
  }, [active, visible, initializing, busy, supported]);

  async function change() {
    if (writing.current) return;
    writing.current = true;
    setBusy(true);
    setError('');
    setTestAt(undefined);
    try {
      if (enabled) {
        const registration = await bounded(
          navigator.serviceWorker.getRegistration('/'),
        );
        const subscription = registration
          ? await bounded(registration.pushManager.getSubscription())
          : null;
        if (subscription) {
          setData(
            await savePush({
              action: 'unsubscribe',
              endpoint: subscription.endpoint,
            }),
          );
          await bounded(subscription.unsubscribe());
        }
        setRegistered(false);
        setDeviceId(undefined);
      } else {
        // Ask inside the explicit button gesture, as required by phone browsers.
        const permission = await Notification.requestPermission();
        if (permission !== 'granted')
          throw new Error(
            'Notifications are not allowed. You can change this in your phone’s notification settings.',
          );
        if (!data?.publicKey)
          throw new Error('Wait for notification settings to connect.');
        await bounded(
          navigator.serviceWorker.register('/push-sw.js', { scope: '/' }),
        );
        const registration = await bounded(navigator.serviceWorker.ready);
        let existing = await bounded(
          registration.pushManager.getSubscription(),
        );
        if (expired && existing) {
          await bounded(existing.unsubscribe());
          existing = null;
        }
        const subscription =
          existing ??
          (await bounded(
            registration.pushManager.subscribe({
              userVisibleOnly: true,
              applicationServerKey: applicationKey(data.publicKey),
            }),
          ));
        const saved = await savePush({
          action: 'subscribe',
          subscription: subscription.toJSON(),
        });
        setData(saved);
        setDeviceId(saved.subscriptionId);
        setRegistered(true);
      }
    } catch (e) {
      setError(
        e instanceof Error
          ? e.message
          : 'Could not enable notifications. Try again.',
      );
    } finally {
      writing.current = false;
      setBusy(false);
    }
  }
  async function sendTest() {
    if (writing.current || !deviceId) return;
    writing.current = true;
    setBusy(true);
    setError('');
    try {
      const saved = await savePush({
        action: 'test',
        subscriptionId: deviceId,
      });
      setData(saved);
      setTestAt(
        saved.subscriptions?.find((s) => s.id === deviceId)?.lastTestAt ??
          undefined,
      );
    } catch (e) {
      setError(
        e instanceof Error ? e.message : 'Could not send a test notification.',
      );
    } finally {
      writing.current = false;
      setBusy(false);
    }
  }
  return (
    <div className="demand-notifications">
      <div>
        <strong>
          <Bell size={15} /> Model-switch notifications
        </strong>
        <p className="small muted">
          {enabled
            ? 'Registered on this device. Get the new model and why it changed, after warm-up succeeds. Demand-spike alerts are off.'
            : 'On iPhone, open your private dashboard in Safari, add it to the Home Screen, then open it there to enable alerts.'}
        </p>
        {!supported && (
          <p className="small muted">
            Enable this from a supported phone Home Screen app or browser.
          </p>
        )}
        {data && (!data.supported || contactMissing) && (
          <p className="notice">{data.detail}</p>
        )}
        {enabled && !device?.lastSentAt && !device?.lastError && (
          <p className="small muted">
            No accepted delivery yet. Send a test to check this device.
          </p>
        )}
        {device?.lastSentAt && (
          <p className="small muted">
            This device: last accepted by push service {age(device.lastSentAt)}.
            This does not confirm display on the phone.
          </p>
        )}
        {device?.lastError && <p className="notice">{device.lastError}</p>}
        {testAt != null && (
          <p className="small muted" role="status">
            {testComplete
              ? device?.lastTestError
                ? `Test failed: ${device.lastTestError}`
                : 'Test accepted by the push service. Check this device’s notifications.'
              : 'Test queued. Keep Bloomkeeper running on the Mac while it sends.'}
          </p>
        )}
        {enabled && coolingDown && (
          <p className="small muted">
            Another test is available after the retry delay.
          </p>
        )}
        {readError && (
          <p className="notice">Delivery status is not live. {readError}</p>
        )}
      </div>
      {enabled && (
        <Button
          type="button"
          variant="outline"
          disabled={
            busy || initializing || contactMissing || coolingDown || !!readError
          }
          onClick={() => void sendTest()}
        >
          Send test notification
        </Button>
      )}
      <Button
        type="button"
        variant="outline"
        disabled={!supported || !data?.supported || busy || initializing}
        onClick={() => void change()}
      >
        {busy
          ? 'Saving…'
          : initializing
            ? 'Connecting…'
            : enabled
              ? 'Turn off on this device'
              : 'Enable notifications'}
      </Button>
      {error && (
        <p role="alert" className="notice">
          {error}
        </p>
      )}
    </div>
  );
}

export function DemandAlertsPanel() {
  const active = useScreenActive();
  const visible = usePageVisible();
  const navigation = useAppNavigation();
  const [data, setData] = useState<DemandState | null>(null);
  const [error, setError] = useState('');
  const [expanded, setExpanded] = useState(false);
  useEffect(() => {
    if (!active || !visible) return;
    return startChartPolling<DemandState>({
      load: (signal) => readJson('/api/demand-alerts', signal),
      intervalMs: 15000,
      onValue: (value) => {
        setData(value);
        setError('');
      },
      onError: (e) => setError(e.message),
    });
  }, [active, visible]);
  const stale =
    !!error ||
    !data ||
    Date.now() / 1000 - data.at > 120 ||
    data.status === 'stale';
  const models = data?.models ?? [];
  const labels: Record<string, string> = {
    spike: 'Sustained spike',
    watching: 'Checking spike',
    normal: 'No sustained spike',
    learning: 'Learning baseline',
    stale: 'Waiting for data',
    no_headroom: 'No warm capacity',
  };
  function reviewModels() {
    if (navigation.mobile) navigation.navigate('switch');
    else {
      navigation.selectTab('mac');
      requestAnimationFrame(() =>
        document
          .querySelector('.manual-model-control')
          ?.scrollIntoView({ block: 'center', behavior: 'smooth' }),
      );
    }
  }
  return (
    <section className="panel demand-alert-panel">
      <div className="panel-heading">
        <div>
          <div className="eyebrow">DEMAND WATCH</div>
          <h2>Catch a busy window.</h2>
        </div>
        <TrendingUp size={21} />
      </div>
      <p className="small muted">
        Scans eligible models every minute. Looks for a sustained rise in active
        + queued requests and demand per warm provider, with time-of-day
        comparisons when coverage allows.
      </p>
      {stale && (
        <p className="notice">
          {error ||
            'Waiting for a fresh scan. Saved readings are not live alerts.'}
        </p>
      )}
      {!models.length && !stale && (
        <p className="small muted">
          Waiting for eligible models and enough recorded network history.
        </p>
      )}
      <div className="demand-watch-list">
        {(expanded ? models : models.slice(0, 4)).map((model) => (
          <article
            className={`demand-watch-model ${!stale && model.status === 'spike' ? 'is-spike' : ''}`}
            key={model.model}
          >
            <div className="demand-watch-title">
              <strong>
                {shortModel(model.model)}
                {model.current ? ' · running' : ''}
              </strong>
              <span>
                {stale ? 'Saved' : (labels[model.status] ?? model.status)}
              </span>
            </div>
            <div className="demand-watch-metrics">
              <div>
                <span>5m active + queued</span>
                <strong>{num(model.load, 1)}</strong>
                <small>
                  {model.loadRatio != null
                    ? `${num(model.loadRatio, 1)}× usual`
                    : 'Learning usual load'}
                </small>
              </div>
              <div>
                <span>Per warm provider</span>
                <strong>
                  {model.pressureRatio != null
                    ? `${num(model.pressureRatio, 1)}×`
                    : '—'}
                </strong>
                <small>relative demand</small>
              </div>
              <div>
                <span>This Mac’s history</span>
                <strong>
                  {money(model.localRatePerHour)}
                  <small> / hr</small>
                </strong>
                <small>{num(model.localWarmHours, 1)} warm hours</small>
              </div>
            </div>
            <p className="small muted">
              {model.baselineScope === 'daytype_hour'
                ? 'Comparable day & time'
                : 'All recorded hours'}{' '}
              · {num(model.baselineHours, 1)}h network baseline
            </p>
            {model.status !== 'normal' && (
              <p className="small muted">{model.detail}</p>
            )}
          </article>
        ))}
      </div>
      <div className="chart-toolbar">
        {models.length > 4 && (
          <button
            className="text-button"
            onClick={() => setExpanded(!expanded)}
          >
            {expanded
              ? 'Show fewer models'
              : `Show all ${models.length} models`}
          </button>
        )}
        <Button variant="outline" onClick={reviewModels}>
          Review model selection
        </Button>
        <Button
          variant="outline"
          onClick={() => {
            navigation.navigate('test');
            if (!navigation.mobile)
              requestAnimationFrame(() =>
                document
                  .querySelector('.demand-auto-panel')
                  ?.scrollIntoView({ block: 'start' }),
              );
          }}
        >
          Demand auto-switch
        </Button>
      </div>
      <p className="footnote">
        Network concurrency is a demand signal, not completed requests per
        minute. The $/hour values are verified historical paid work on this Mac,
        including warm idle time. Demand auto-switch is a separate opt-in
        strategy that also checks paid performance, switching costs and memory.
      </p>
      {!!data?.alerts.length && (
        <details className="demand-alert-history">
          <summary>Recent alerts · {data.alerts.length}</summary>
          {data.alerts.slice(0, 10).map((event) => (
            <p className="small muted" key={event.id}>
              {shortModel(event.model)} ·{' '}
              {new Date(event.at * 1000).toLocaleString()}
            </p>
          ))}
        </details>
      )}
      <p className="small muted">
        Demand spikes stay in this view. Phone alerts now cover completed model
        switches; manage them in Models → Test.
      </p>
    </section>
  );
}
