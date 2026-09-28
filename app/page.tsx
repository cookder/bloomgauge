'use client';
import { useEffect, useRef, useState } from 'react';
import { startChartPolling } from '@/lib/chart-polling';
import { usePageVisible } from '@/lib/use-page-visibility';
import { dashboardConnection, readStatusJSON } from '@/lib/connection-status';
import { BloomStatus } from '@/components/dashboard/bloom-status';
import { ThemeToggle } from '@/components/dashboard/theme-toggle';
import { ScreenErrorBoundary } from '@/components/dashboard/screen-error';
import { observeSupportCondition } from '@/lib/support-issues';
import {
  AppNavigationProvider,
  DesktopNavigation,
  NavigationHub,
  AppScreen,
  MobileNavigation,
  MobileBottomNavigation,
  AppFrame,
  MobilePageTitle,
  useAppNavigation,
} from '@/components/dashboard/app-navigation';
import {
  Activity,
  ArrowUpRight,
  CircleDollarSign,
  Database,
  Pause,
  Play,
  Radio,
  Zap,
} from 'lucide-react';
import { Tabs, TabsContent } from '@/components/ui/tabs';
import {
  CreditsPanel,
  EarningsPanel,
  HardwareCard,
  ProcessPanel,
  type Hardware,
  type Monitor,
} from '@/components/dashboard/widgets';
import { NetworkTab } from '@/components/dashboard/network-tab';
import { OptimizerTab } from '@/components/dashboard/optimizer-tab';
import { PhoneAccess } from '@/components/dashboard/phone-access';
import { SupportDiagnostics } from '@/components/dashboard/support-diagnostics';
import { Guide } from '@/components/dashboard/guide';
import { AboutBloom } from '@/components/dashboard/about-bloom';
import { OverviewSummary } from '@/components/dashboard/overview-summary';
import {
  UsageInvitation,
  useDashboardUsage,
} from '@/components/dashboard/usage-sharing';
import { FeatureDiscovery } from '@/components/dashboard/feature-discovery';
import { ReleaseNotesNotice } from '@/components/dashboard/release-notes';
import { MyMacs } from '@/components/dashboard/my-macs';
import { ReputationPanel } from '@/components/dashboard/reputation';
import {
  EnergyPanel,
  ConcurrencyHistoryPanel,
} from '@/components/dashboard/operating-history';
import {
  ProviderSessionPanel,
  sessionStamp,
  sessionDuration,
  type ProviderSession,
} from '@/components/dashboard/provider-session';
import { WorkloadPanel } from '@/components/dashboard/workload';
import { CommunityInsightsPanel } from '@/components/dashboard/community-insights';
import { OptimizerLivePanel } from '@/components/dashboard/optimizer-live';
import { DemandGlance } from '@/components/dashboard/demand-glance';
import { EarningsLayout } from '@/components/dashboard/earnings-layout';
import { EarningsPulse } from '@/components/dashboard/earnings-pulse';
import { DailyEarningsPanel } from '@/components/dashboard/daily-earnings';
import { EarningsTarget } from '@/components/dashboard/earnings-target';
import type { EarningsPulseData } from '@/lib/earnings-pulse';
import type { TrafficPulseData } from '@/lib/traffic-pulse';
import {
  ForecastSummary,
  HourlyOutputChart,
  type Forecast,
} from '@/components/dashboard/forecast';
import {
  age,
  Choice,
  HistoryNote,
  money,
  num,
  RangePicker,
  shortModel,
  TimeChart,
  useHistory,
  type Range,
} from '@/components/dashboard/shared';
type Provider = {
  tracking?: { counting: boolean; detail: string };
  online: boolean;
  starting?: boolean;
  active: boolean;
  model: string;
  version: string;
  sessionTokens: number | null;
  sessionJobs: number | null;
  uptime: number | null;
  tokensPerSecond: number | null;
  memoryGB: number | null;
  session: ProviderSession | null;
};
type Snapshot = {
  deviceName?: string;
  at: number;
  forecast?: Forecast;
  pulse?: EarningsPulseData;
  traffic?: TrafficPulseData;
  hardware: Hardware;
  provider: Provider;
  earnings: {
    status: string;
    error: string | null;
    updatedAt: number | null;
    balance: number | null;
    lifetime: number | null;
    count: number | null;
    revision?: number;
  };
  monitor: Monitor;
  sources: {
    name: string;
    status: string;
    detail: string;
    updatedAt: number | null;
  }[];
};
function Metric({
  label,
  value,
  unit,
  detail,
  icon: Icon,
  tone = '',
}: {
  label: string;
  value: string;
  unit?: string;
  detail: string;
  icon: typeof Zap;
  tone?: string;
}) {
  return (
    <article className={`metric ${tone}`}>
      <div className="metric-label">
        <span>{label}</span>
        <Icon size={18} />
      </div>
      <div className="metric-value">
        {value}
        <span>{unit}</span>
      </div>
      <p>{detail}</p>
    </article>
  );
}
const throughputSeries = [
  {
    key: 'tokensPerSecond',
    label: 'Average output tok/s',
    color: 'var(--c-a995ff)',
  },
  {
    key: 'peakTokensPerSecond',
    label: 'Peak output tok/s',
    color: 'var(--c-637795)',
  },
];
function ThroughputPanel({
  provider: p,
  paused,
  forecast,
}: {
  provider: Provider | undefined;
  paused: boolean;
  forecast?: Forecast;
}) {
  const [range, setRange] = useState<Range>({ preset: '15m' });
  const [mode, setMode] = useState('rate');
  const { data, error } = useHistory(
    range,
    paused,
    'hardware',
    mode === 'rate',
  );
  return (
    <section className="panel performance-panel">
      <div className="panel-heading">
        <div>
          <div className="eyebrow">LIVE ACTIVITY</div>
          <h2>Provider throughput.</h2>
        </div>
        <span className="model-tag">
          <span className={`machine-dot ${p?.online ? '' : 'offline'}`} />
          {p?.session?.models.map(shortModel).join(' + ') ||
            (p?.model ? shortModel(p.model) : 'No model loaded')}
        </span>
      </div>
      <div className="chart-toolbar">
        <Choice
          label="Throughput chart"
          value={mode}
          onChange={(value) => {
            setMode(value);
            if (value === 'hourly' && ['5m', '15m'].includes(range.preset))
              setRange({ preset: '24h' });
          }}
          options={[
            { value: 'rate', label: 'Live tokens / second' },
            { value: 'hourly', label: 'Hourly output + forecast' },
          ]}
        />
        <RangePicker
          value={range}
          onChange={setRange}
          label="Throughput date range"
        />
      </div>
      <div className="performance-grid">
        <div>
          {mode === 'hourly' ? (
            <HourlyOutputChart
              range={range}
              paused={paused}
              forecast={forecast}
            />
          ) : (
            <>
              <TimeChart
                data={data?.samples ?? []}
                series={throughputSeries}
                area
                height={200}
              />
              <HistoryNote data={data} error={error} />
            </>
          )}
          {(!range.end || range.end >= (forecast?.at ?? 0)) && (
            <ForecastSummary value={forecast} kind="throughput" />
          )}
        </div>
        <div className="session-details">
          <div>
            <span>{p?.session?.label ?? 'Session'} warm runtime</span>
            <strong>{sessionDuration(p?.uptime)}</strong>
            <small>
              {p?.session
                ? `${p.session.performance?.status === 'counting' ? 'Counting from' : 'Paused · tracked from'} ${sessionStamp(p.session.performance?.since)}`
                : 'Waiting for session identity'}
            </small>
          </div>
          <div>
            <span>Model memory</span>
            <strong>{num(p?.memoryGB, 1)} GB</strong>
          </div>
          <div>
            <span>Provider version</span>
            <strong>{p?.version || '—'}</strong>
          </div>
          <p>
            History stays on this Mac after you close the app. New samples are
            recorded while it is open. Throughput follows the provider’s roughly
            three-second counter updates.
          </p>
        </div>
      </div>
    </section>
  );
}
export default function Dashboard() {
  return (
    <AppNavigationProvider>
      <ScreenErrorBoundary name="dashboard">
        <DashboardContent />
      </ScreenErrorBoundary>
    </AppNavigationProvider>
  );
}
function DashboardContent() {
  const navigation = useAppNavigation();
  const pageVisible = usePageVisible();
  const [clock, setClock] = useState(() => Date.now() / 1000);
  useEffect(() => {
    if (!pageVisible) return;
    const timer = setInterval(() => setClock(Date.now() / 1000), 1000);
    return () => clearInterval(timer);
  }, [pageVisible]);
  const { tab, mobile, visible } = navigation;
  const visited = useRef(new Set(['mac']));
  visited.current.add(tab);
  const [data, setData] = useState<Snapshot | null>(null),
    [error, setError] = useState(''),
    [paused, setPaused] = useState(false);
  // Hourly history is ~100 KB and changes a few times a minute; the server
  // leaves it out when this revision is current.
  const knownHours = useRef<{
    revision: string;
    hours: Monitor['hours'];
  } | null>(null);
  // Live numbers need one-second readings only on the Mac tab.
  const liveTab = tab === 'mac';
  useEffect(() => {
    if (paused || !pageVisible) return;
    return startChartPolling({
      issueContext: 'overview',
      load: async (signal) => {
        const read = async (known: typeof knownHours.current) =>
          (await readStatusJSON(
            await fetch(
              known
                ? `/api/snapshot?hours=${encodeURIComponent(known.revision)}`
                : '/api/snapshot',
              { signal, cache: 'no-store' },
            ),
            'Dashboard connection',
          )) as Snapshot;
        const known = knownHours.current;
        let d = await read(known);
        if (d?.monitor && !Array.isArray(d.monitor.hours)) {
          // Omitted only for the revision we sent; anything else, ask for the full reading.
          if (known && d.monitor.hoursRevision === known.revision)
            d.monitor.hours = known.hours;
          else d = await read(null);
        }
        if (d?.monitor && Array.isArray(d.monitor.hours)) {
          knownHours.current = d.monitor.hoursRevision
            ? { revision: d.monitor.hoursRevision, hours: d.monitor.hours }
            : null;
        }
        if (
          !d ||
          !Number.isFinite(d.at) ||
          !d.hardware ||
          !d.provider ||
          !d.earnings ||
          !d.monitor ||
          !Array.isArray(d.sources) ||
          d.sources.some(
            (source) =>
              !source ||
              typeof source.name !== 'string' ||
              typeof source.detail !== 'string',
          )
        )
          throw Error('The local snapshot is not ready.');
        return d;
      },
      onValue: (d) => {
        observeSupportCondition(
          'connection',
          'earnings',
          ['stale', 'error'].includes(d.earnings.status),
        );
        setData((previous) => ({
          ...d,
          forecast:
            previous?.forecast?.at === d.forecast?.at
              ? previous?.forecast
              : d.forecast,
          monitor:
            previous?.monitor.updatedAt === d.monitor.updatedAt &&
            previous.monitor.revision === d.monitor.revision &&
            previous.monitor.observedAt === d.monitor.observedAt &&
            previous.monitor.status === d.monitor.status
              ? previous.monitor
              : d.monitor,
          earnings:
            previous?.earnings.updatedAt === d.earnings.updatedAt &&
            previous.earnings.status === d.earnings.status &&
            previous.earnings.revision === d.earnings.revision
              ? previous.earnings
              : d.earnings,
        }));
        setError('');
      },
      onError: (error) =>
        setError(
          error instanceof TypeError
            ? 'The dashboard connection is unavailable. Retrying automatically.'
            : error.message,
        ),
      intervalMs: liveTab ? 1000 : 5000,
      timeoutMs: 10000,
    });
  }, [paused, pageVisible, liveTab]);
  useEffect(() => {
    if (paused || !pageVisible)
      observeSupportCondition('connection', 'earnings', false);
    return () => observeSupportCondition('connection', 'earnings', false);
  }, [paused, pageVisible]);
  const h = data?.hardware,
    p = data?.provider,
    e = data?.earnings;
  const now = paused && data ? data.at : clock;
  const connection = dashboardConnection(
    data?.at,
    now,
    error,
    paused,
    pageVisible,
  );
  const { stale, live } = connection;
  // A single missed refresh need not blank still-fresh charts. Control guards
  // continue using their own fresh responses and the unmodified error state.
  const readingsFresh = dashboardConnection(
    data?.at,
    now,
    '',
    false,
    true,
  ).live;
  useDashboardUsage(pageVisible && !!data);
  return (
    <AppFrame>
      <main
        ref={navigation.scrollRoot}
        className={`dashboard${mobile ? ' mobile-app' : ''}`}
      >
        <header className="topbar">
          <div className="brand">
            <span className="brand-icon">
              <Activity size={23} />
            </span>
            <div>
              <strong>
                Bloomkeeper<span className="brand-wide"> / Dashboard</span>
                <MobilePageTitle />
              </strong>
              <p>{data?.deviceName || 'YOUR MAC, IN VIEW'}</p>
            </div>
          </div>
          <div className="header-actions">
            <BloomStatus
              data={data}
              now={clock}
              error={error}
              paused={paused}
              visible={pageVisible}
            />
            <button
              className="icon-button"
              onClick={() => setPaused(!paused)}
              title={paused ? 'Resume live view' : 'Pause live view'}
              aria-label={paused ? 'Resume live view' : 'Pause live view'}
            >
              {paused ? <Play size={17} /> : <Pause size={17} />}
            </button>
            <ThemeToggle />
          </div>
        </header>
        <DesktopNavigation />
        <MobileNavigation />
        {navigation.screen !== 'machines' && (
          <div className="device-scope" aria-label="Current Mac">
            <span>
              {data?.deviceName || 'This Mac'}
              {h?.chip ? ` · ${h.chip}` : ''}
            </span>
            <button
              className="text-link"
              onClick={() => navigation.navigate('machines')}
            >
              My Macs ↗
            </button>
          </div>
        )}
        <ReleaseNotesNotice banner={navigation.screen === 'overview'} />

        <AppScreen name="access">
          <PhoneAccess alwaysOpen={mobile || navigation.screen === 'access'} />
        </AppScreen>
        <Tabs
          value={tab}
          onValueChange={(v) => navigation.selectTab(String(v))}
          className="dashboard-tabs"
        >
          <TabsContent
            value="mac"
            className={`mac-overview ${navigation.screen === 'overview' ? 'is-overview' : ''}`}
            keepMounted
          >
            <section className="page-heading desktop-page-heading">
              <div>
                <div className="eyebrow">LOCAL COMPUTE / OVERVIEW</div>
                <h1>Your Mac at work.</h1>
                <p>
                  <span className="machine-dot" />
                  {h?.chip ?? 'Apple Silicon'} ·{' '}
                  {h?.memoryTotalGB
                    ? `${num(h.memoryTotalGB)} GB unified memory`
                    : 'Reading your Mac'}
                  <span className="heading-divider">/</span>
                  <span>
                    {p?.online
                      ? p.starting
                        ? 'Starting, loading models'
                        : p.active
                          ? 'Serving inference'
                          : 'Ready for requests'
                      : 'Provider offline'}
                  </span>
                </p>
              </div>
              <a
                className="text-link"
                href="https://console.darkbloom.dev"
                target="_blank"
                rel="noreferrer"
              >
                Darkbloom console <ArrowUpRight size={16} />
              </a>
            </section>
            {e?.error && (
              <div role="status" className="notice">
                Earnings: {e.error}{' '}
                {e.updatedAt ? 'Last confirmed values remain visible.' : ''}
              </div>
            )}
            <AppScreen name="tools">
              <NavigationHub />
            </AppScreen>
            <div className="desktop-earnings-workspace earnings-workspace-layout">
              <AppScreen name="overview">
                <OverviewSummary
                  active={
                    visible('overview') &&
                    tab === 'mac' &&
                    pageVisible &&
                    !paused
                  }
                  live={readingsFresh && !!data}
                  online={p?.online ?? null}
                  starting={p?.starting === true}
                  model={p?.model || null}
                  pulse={data?.pulse}
                  hours={data?.monitor.hours}
                  now={now}
                />
                <EarningsLayout
                  render={(id, shown) => {
                    switch (id) {
                      case 'balance':
                        return (
                          <Metric
                            label="Available balance"
                            value={money(e?.balance)}
                            detail={`Confirmed · 20s sync · ${age(e?.updatedAt, now)}`}
                            icon={CircleDollarSign}
                            tone="money"
                          />
                        );
                      case 'lifetime':
                        return (
                          <Metric
                            label="Lifetime earnings"
                            value={money(e?.lifetime)}
                            detail={
                              e?.lifetime != null && e.lifetime === e.balance
                                ? `Same as balance: nothing withdrawn yet · ${num(e?.count)} credits`
                                : `${num(e?.count)} credited entries · account total`
                            }
                            icon={Database}
                          />
                        );
                      case 'throughput':
                        return (
                          <Metric
                            label="Output throughput"
                            value={num(p?.tokensPerSecond, 1)}
                            unit="tok/s"
                            detail={
                              p?.tracking?.counting
                                ? 'Warm model counter change · ~3s source sample'
                                : (p?.tracking?.detail ??
                                  'Waiting for a fresh provider snapshot')
                            }
                            icon={Zap}
                            tone="compute"
                          />
                        );
                      case 'requests':
                        return (
                          <Metric
                            label={`${p?.session?.label ?? 'Session'} warm requests`}
                            value={`${p?.session?.performance?.requestsPartial && p.sessionJobs != null ? '≥ ' : ''}${num(p?.sessionJobs)}`}
                            detail={`${p?.session?.performance?.tokensPartial && p.sessionTokens != null ? '≥ ' : ''}${num(p?.sessionTokens)} warm output tokens · ${p?.session?.performance?.requestsPartial || p?.session?.performance?.tokensPartial ? 'partial counter coverage · ' : ''}${p?.session?.performance?.status === 'counting' ? `counted from ${sessionStamp(p.session.performance.since)}` : 'statistics paused'}`}
                            icon={Radio}
                          />
                        );
                      case 'pulse':
                        return (
                          <EarningsPulse
                            session={p?.session}
                            pulse={data?.pulse}
                            traffic={data?.traffic}
                            paused={paused}
                            active={
                              shown && visible('overview') && tab === 'mac'
                            }
                            connected={(paused || readingsFresh) && !!data}
                            forecast={data?.forecast}
                            earningsUpdatedAt={e?.updatedAt}
                            earningsStatus={e?.status}
                            monitor={data?.monitor}
                            at={data?.at}
                          />
                        );
                      case 'optimizer':
                        return (
                          <OptimizerLivePanel
                            active={
                              shown && visible('overview') && tab === 'mac'
                            }
                          />
                        );
                      case 'demand':
                        return <DemandGlance paused={paused} />;
                      case 'earnings':
                        return (
                          <EarningsPanel
                            m={data?.monitor}
                            at={Math.floor(data?.at ?? now)}
                            forecast={data?.forecast}
                          />
                        );
                      case 'daily':
                        return (
                          <DailyEarningsPanel
                            paused={paused}
                            projection={data?.forecast?.modelProjection}
                            connected={(paused || !stale) && !!data}
                          />
                        );
                    }
                  }}
                />
                <UsageInvitation
                  active={
                    navigation.screen === 'overview' && pageVisible && !!data
                  }
                />
                <FeatureDiscovery
                  active={navigation.screen === 'overview'}
                  healthy={
                    live &&
                    p?.online === true &&
                    e?.status === 'ok' &&
                    !e.error &&
                    e.updatedAt != null &&
                    now - e.updatedAt >= 0 &&
                    now - e.updatedAt <= 60
                  }
                />
              </AppScreen>
              <AppScreen name="charts">
                <EarningsPanel
                  m={data?.monitor}
                  at={Math.floor(data?.at ?? now)}
                  forecast={data?.forecast}
                />
              </AppScreen>
            </div>
            <AppScreen name="workload">
              <WorkloadPanel
                paused={paused}
                enabled={visible('workload') && tab === 'mac'}
              />
            </AppScreen>
            <AppScreen name="target">
              <EarningsTarget paused={paused} />
            </AppScreen>
            <AppScreen name="sessions">
              <ProviderSessionPanel
                session={p?.session ?? null}
                paused={paused || !visible('sessions') || tab !== 'mac'}
              />
            </AppScreen>
            <AppScreen name="reputation">
              <ReputationPanel
                paused={paused || !visible('reputation') || tab !== 'mac'}
                session={p?.session ?? null}
              />
            </AppScreen>
            <div className="main-grid">
              <AppScreen name="hardware">
                <HardwareCard
                  h={h}
                  paused={paused || !visible('hardware') || tab !== 'mac'}
                />
              </AppScreen>
            </div>
            <AppScreen name="energy">
              <EnergyPanel
                paused={paused || !visible('energy') || tab !== 'mac'}
              />
            </AppScreen>
            <AppScreen name="throughput">
              <ReputationPanel
                concurrencyOnly
                session={p?.session ?? null}
                paused={paused || !visible('throughput') || tab !== 'mac'}
              />
              <ThroughputPanel
                provider={p}
                paused={paused || !visible('throughput') || tab !== 'mac'}
                forecast={data?.forecast}
              />
              <ConcurrencyHistoryPanel
                paused={paused || !visible('throughput') || tab !== 'mac'}
              />
            </AppScreen>
            <div className="bottom-grid">
              <AppScreen name="processes">
                <ProcessPanel h={h} />
              </AppScreen>
              <AppScreen name="credits">
                <CreditsPanel
                  paused={paused || !visible('credits') || tab !== 'mac'}
                  revision={e?.revision ?? 0}
                />
              </AppScreen>
            </div>
            <AppScreen name="guide">
              <Guide />
            </AppScreen>
            <AppScreen name="support">
              <SupportDiagnostics />
            </AppScreen>
            <AppScreen name="plan">
              <AboutBloom />
            </AppScreen>
            <AppScreen name="machines">
              <MyMacs paused={paused} />
            </AppScreen>
            <AppScreen name="sources">
              <footer className="sources">
                <div className="sources-title">
                  <Activity size={16} />
                  <strong>Connected sources</strong>
                  <span>Last snapshot {age(data?.at, now)}</span>
                </div>
                <div className="source-grid">
                  {data?.sources.map((s) => (
                    <div key={s.name} className="source">
                      <span
                        className={`source-dot ${s.status === 'ok' ? '' : 'warn'}`}
                      />
                      <div>
                        <strong>{s.name}</strong>
                        <p>{s.detail}</p>
                      </div>
                      <span>
                        {s.status === 'ok'
                          ? 'Connected'
                          : s.status === 'stale'
                            ? 'Stale'
                            : 'Unavailable'}
                      </span>
                    </div>
                  ))}
                </div>
              </footer>
            </AppScreen>
          </TabsContent>
          <TabsContent value="network" keepMounted>
            {visited.current.has('network') && (
              <ScreenErrorBoundary name="network">
                <NetworkTab paused={paused || tab !== 'network'} />
              </ScreenErrorBoundary>
            )}
          </TabsContent>
          <TabsContent value="community" keepMounted>
            {visited.current.has('community') && (
              <AppScreen name="community">
                <CommunityInsightsPanel enabled={tab === 'community'} />
              </AppScreen>
            )}
          </TabsContent>
          <TabsContent value="optimizer" keepMounted>
            {visited.current.has('optimizer') && (
              <ScreenErrorBoundary name="models">
                <OptimizerTab
                  paused={paused || tab !== 'optimizer'}
                  connectionError={error}
                />
              </ScreenErrorBoundary>
            )}
          </TabsContent>
        </Tabs>
        <div className="privacy-line">
          <span>Stored on your Mac · saved history · temperatures in °F</span>
          <span>
            USD ·{' '}
            {new Date(now * 1000).toLocaleDateString([], {
              month: 'short',
              day: 'numeric',
              year: 'numeric',
            })}
          </span>
        </div>
      </main>
      <MobileBottomNavigation />
    </AppFrame>
  );
}
