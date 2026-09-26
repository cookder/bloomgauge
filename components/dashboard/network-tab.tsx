'use client';
import { memo, useEffect, useState } from 'react';
import { AppScreen, useAppNavigation } from './app-navigation';
import { startChartPolling } from '@/lib/chart-polling';
import { sanitizeNetworkResponse } from '@/lib/network-response';
import { usePageVisible } from '@/lib/use-page-visibility';
import { ArrowUpRight, Globe, Layers, Radio, Server } from 'lucide-react';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible';
import { age, money, num, shortModel } from './shared';
import { type Capacity } from './model-demand';
import { ModelResearchPanel } from './model-research';
import { DemandAlertsPanel } from './demand-alerts';
import { WeeklyTraffic } from './network-weekly';
import { NetworkContributionsPanel } from './network-contributions';
type Geo = {
  key: string;
  city?: string;
  region?: string;
  country?: string;
  requests?: number;
  providers?: number;
  prompt_tokens?: number;
  completion_tokens?: number;
  gpu_cores?: number;
};
type Provider = {
  machine_model?: string;
  cpu_cores?: { total: number };
  memory_bandwidth_gbs?: number;
  decode_tps?: number;
  requests_served?: number;
  tokens_generated?: number;
  current_model?: string;
  id: string;
  chip?: string;
  hardware_chip?: string;
  gpu_cores?: number;
  memory_gb?: number;
  models?: string[];
  trust_level?: string;
  status?: string;
  routable?: boolean;
};
type Stats = {
  active_providers?: number;
  total_gpu_cores?: number;
  total_cpu_cores?: number;
  total_memory_gb?: number;
  total_bandwidth_gbs?: number;
  network_capacity_tps?: number;
  active_power_watts?: number;
  location_window_hours?: number;
  request_regions?: Geo[];
  request_locations?: Geo[];
  provider_regions?: Geo[];
  provider_locations?: Geo[];
  unknown_request_location_requests?: number;
  unknown_location_providers?: number;
  request_flows?: { key: string; from: Geo; to: Geo; requests: number }[];
  providers?: Provider[];
  network_utilization?: {
    utilization?: number;
    warm_utilization?: number;
    token_budget_utilization?: number;
    bottleneck_model?: string;
  };
};
type Source<T> = {
  status: string;
  updatedAt?: number;
  error?: string;
  data?: T;
};
type NetworkState = {
  totals?: Source<{
    jobs: number;
    tokens: number;
    earnings_micro_usd: number;
    reward_earnings_micro_usd: number;
    work_earnings_micro_usd: number;
    active_accounts: number;
  }>;
  capacity?: Source<{ models: Capacity[] }>;
  stats?: Source<Stats>;
  backfill?: Source<{ windows: string[] }>;
  series?: Source<unknown>;
};
function location(g: Geo) {
  return (
    [g.city, g.region, g.country].filter(Boolean).join(', ') ||
    g.key ||
    'Unknown'
  );
}
export const NetworkTab = memo(function NetworkTab({
  paused,
}: {
  paused: boolean;
}) {
  const { visible, tab, mobile } = useAppNavigation();
  const screenActive = tab === 'network' && (!mobile || !visible('community'));
  const pageVisible = usePageVisible();
  const [data, setData] = useState<NetworkState | null>(null),
    [error, setError] = useState(''),
    [directory, setDirectory] = useState(false),
    // The fleet runs to hundreds of Macs; show the busiest 15 unless asked.
    [allProviders, setAllProviders] = useState(false);
  useEffect(() => {
    if (!screenActive || !pageVisible || (paused && data)) return;
    return startChartPolling({
      load: async (signal) => {
        const response = await fetch('/api/network', {
          signal,
          cache: 'no-store',
        });
        if (!response.ok) throw Error('Network connection interrupted.');
        const value: unknown = await response.json();
        if (!value || typeof value !== 'object' || Array.isArray(value))
          throw Error('Network returned an incomplete response.');
        return value;
      },
      onValue: (next) => {
        setData(
          (previous) => sanitizeNetworkResponse(next, previous) as NetworkState,
        );
        setError('');
      },
      onError: (error) => setError(error.message),
      intervalMs: 5000,
      repeat: !paused,
    });
  }, [paused, screenActive, pageVisible]);
  const totals = data?.totals?.data,
    stats = data?.stats?.data,
    models = [...(data?.capacity?.data?.models ?? [])].sort(
      (a, b) =>
        b.active_requests - a.active_requests ||
        b.queued_requests - a.queued_requests,
    );
  const active = models.reduce((n, m) => n + m.active_requests, 0),
    queued = models.reduce((n, m) => n + m.queued_requests, 0);
  const demand = [
      ...(stats?.request_regions ?? stats?.request_locations ?? []),
    ].sort((a, b) => (b.requests ?? 0) - (a.requests ?? 0)),
    supply = [
      ...(stats?.provider_regions ?? stats?.provider_locations ?? []),
    ].sort((a, b) => (b.providers ?? 0) - (a.providers ?? 0));
  const unavailable = !stats;
  const composition = Object.values(
    (stats?.providers ?? []).reduce<
      Record<
        string,
        {
          chip: string;
          count: number;
          gpu: number;
          memory: number;
          serving: number;
        }
      >
    >((rows, p) => {
      const key = p.chip ?? 'Unknown';
      const row = rows[key] ?? {
        chip: key,
        count: 0,
        gpu: 0,
        memory: 0,
        serving: 0,
      };
      row.count++;
      row.gpu += p.gpu_cores ?? 0;
      row.memory += p.memory_gb ?? 0;
      if (p.status === 'serving') row.serving++;
      rows[key] = row;
      return rows;
    }, {}),
  ).sort((a, b) => b.count - a.count);
  return (
    <div className="network-view">
      <section className="page-heading desktop-page-heading">
        <div>
          <div className="eyebrow">DARKBLOOM / NETWORK</div>
          <h1>Where the work is.</h1>
          <p>Live demand, model capacity, and network traffic.</p>
        </div>
        <a
          className="text-link"
          href="https://console.darkbloom.dev/stats"
          target="_blank"
          rel="noreferrer"
        >
          Official Network page <ArrowUpRight size={16} />
        </a>
      </section>
      {error && screenActive && (
        <p role="alert" className="notice">
          {error} {data ? 'Showing the last loaded network snapshot.' : ''}{' '}
          {paused
            ? 'Resume the live view to retry.'
            : 'Retrying automatically.'}
        </p>
      )}
      <AppScreen name="fleet" continuation>
        <div className="network-source-status">
          {(['capacity', 'series', 'totals', 'stats'] as const).map((k) => (
            <span
              key={k}
              className={data?.[k]?.status === 'ok' ? 'connected' : 'pending'}
            >
              <i />
              {k === 'capacity'
                ? 'Model capacity'
                : k === 'series'
                  ? 'Traffic'
                  : k === 'totals'
                    ? 'Totals'
                    : 'Geography & fleet'}{' '}
              ·{' '}
              {data?.[k]?.status === 'ok'
                ? age(data[k]?.updatedAt)
                : (data?.[k]?.error ?? 'Connecting')}
            </span>
          ))}
        </div>
      </AppScreen>
      <AppScreen name="demand">
        <ModelResearchPanel capacity={data?.capacity} paused={paused} />
        <details className="advanced-analysis">
          <summary>Network totals & demand notifications</summary>
          <section className="metrics-grid">
            <article className="metric compute">
              <div className="metric-label">
                Network concurrency <Radio size={18} />
              </div>
              <div className="metric-value">
                {models.length ? num(active) : '—'}
              </div>
              <p>
                {models.length
                  ? `Active requests · ${num(queued)} queued · ${models.length} models`
                  : 'Waiting for capacity API'}
              </p>
              <p className="small muted">
                {paused
                  ? 'View paused'
                  : data?.capacity?.status === 'ok' &&
                      Date.now() / 1000 - (data.capacity.updatedAt ?? 0) < 90
                    ? 'Updated'
                    : 'Last saved reading'}{' '}
                {age(data?.capacity?.updatedAt)} · network-wide, not this Mac
              </p>
            </article>
            <article className="metric">
              <div className="metric-label">
                Active providers <Server size={18} />
              </div>
              <div className="metric-value">{num(stats?.active_providers)}</div>
              <p>
                {unavailable
                  ? 'Detailed fleet API unavailable'
                  : 'Current connected fleet'}
              </p>
            </article>
            <article className="metric">
              <div className="metric-label">
                Network jobs <Globe size={18} />
              </div>
              <div className="metric-value">{num(totals?.jobs)}</div>
              <p>
                All-time jobs ·{' '}
                {data?.totals?.status === 'ok' ? 'updated' : 'last reported'}{' '}
                {age(data?.totals?.updatedAt)}
              </p>
            </article>
            <article className="metric money">
              <div className="metric-label">
                Network earnings <Layers size={18} />
              </div>
              <div className="metric-value">
                {money(totals ? totals.earnings_micro_usd / 1e6 : null)}
              </div>
              <p>
                All providers · all time ·{' '}
                {data?.totals?.status === 'ok' ? 'updated' : 'last reported'}{' '}
                {age(data?.totals?.updatedAt)}
              </p>
            </article>
          </section>
          <DemandAlertsPanel />
        </details>
      </AppScreen>
      <AppScreen name="traffic">
        <NetworkContributionsPanel paused={paused} />
        <WeeklyTraffic paused={paused} />
        {data?.backfill?.error && (
          <output className="notice">
            Historical traffic: {data.backfill.error}.{' '}
            {data.backfill.data?.windows.length
              ? `Loaded ${data.backfill.data.windows.join(', ')} history; all saved samples remain available.`
              : 'Retrying automatically.'}
          </output>
        )}
      </AppScreen>
      <AppScreen name="fleet">
        {data?.stats?.status !== 'ok' && (
          <output className="notice">
            {data?.stats?.error ?? 'Connecting to detailed network statistics.'}{' '}
            {stats
              ? 'Showing the last saved geography and fleet snapshot.'
              : 'Darkbloom’s detailed statistics endpoint is unavailable, so geography and fleet details cannot be shown yet.'}{' '}
            The other network sources keep updating.
          </output>
        )}
        <div className="bottom-grid">
          <section className="panel">
            <div className="panel-heading">
              <div>
                <div className="eyebrow">REQUEST ORIGINS</div>
                <h2>Where demand comes from.</h2>
              </div>
            </div>
            {demand.length ? (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Region</TableHead>
                    <TableHead className="numeric">Requests</TableHead>
                    <TableHead className="numeric">Output tokens</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {demand.map((g, i) => (
                    <TableRow key={`${g.key}:${i}`}>
                      <TableCell>{location(g)}</TableCell>
                      <TableCell className="numeric">
                        {num(g.requests)}
                      </TableCell>
                      <TableCell className="numeric">
                        {num(g.completion_tokens)}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            ) : (
              <div className="empty">
                {unavailable
                  ? 'Waiting for the geography endpoint.'
                  : 'No regional demand data was published.'}
              </div>
            )}
            <p className="footnote">
              Source window:{' '}
              {stats?.location_window_hours
                ? `${stats.location_window_hours} hours`
                : 'not reported'}
              . Unknown-location requests:{' '}
              {num(stats?.unknown_request_location_requests)}. Privacy
              suppression can limit location detail.
            </p>
          </section>
          <section className="panel">
            <div className="panel-heading">
              <div>
                <div className="eyebrow">PROVIDER LOCATIONS</div>
                <h2>Where capacity lives.</h2>
              </div>
            </div>
            {supply.length ? (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Region</TableHead>
                    <TableHead className="numeric">Providers</TableHead>
                    <TableHead className="numeric">GPU cores</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {supply.map((g, i) => (
                    <TableRow key={`${g.key}:${i}`}>
                      <TableCell>{location(g)}</TableCell>
                      <TableCell className="numeric">
                        {num(g.providers)}
                      </TableCell>
                      <TableCell className="numeric">
                        {num(g.gpu_cores)}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            ) : (
              <div className="empty">
                {unavailable
                  ? 'Waiting for the provider-location endpoint.'
                  : 'No provider locations were published.'}
              </div>
            )}
            <p className="footnote">
              Provider locations show supply. Request origins show demand.
              Unlocated providers: {num(stats?.unknown_location_providers)}.
            </p>
          </section>
        </div>
        <section className="panel performance-panel">
          <div className="panel-heading">
            <div>
              <div className="eyebrow">NETWORK RESOURCES</div>
              <h2>The fleet behind the models.</h2>
            </div>
          </div>
          <div className="resource-grid">
            {[
              ['GPU cores', num(stats?.total_gpu_cores)],
              [
                'Reported utilization',
                stats?.network_utilization?.utilization != null
                  ? `${num(stats.network_utilization.utilization * 100, 1)}%`
                  : '—',
              ],
              [
                'Reported capacity',
                `${num(stats?.network_capacity_tps)} tok/s`,
              ],
              ['Estimated power', `${num(stats?.active_power_watts)} W`],
              ['CPU cores', num(stats?.total_cpu_cores)],
              ['Unified memory', `${num(stats?.total_memory_gb)} GB`],
              ['Memory bandwidth', `${num(stats?.total_bandwidth_gbs)} GB/s`],
              ['Total tokens served', num(totals?.tokens)],
              ['Active accounts', num(totals?.active_accounts)],
              [
                'Inference earnings',
                money(totals ? totals.work_earnings_micro_usd / 1e6 : null),
              ],
              [
                'Base rewards',
                money(totals ? totals.reward_earnings_micro_usd / 1e6 : null),
              ],
            ].map(([label, value]) => (
              <div key={label}>
                <span>{label}</span>
                <strong>{value}</strong>
              </div>
            ))}
          </div>
          {composition.length > 0 && (
            <div className="network-composition">
              <h3>Hardware composition</h3>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Chip</TableHead>
                    <TableHead className="numeric">Machines</TableHead>
                    <TableHead className="numeric">Serving</TableHead>
                    <TableHead className="numeric">GPU cores</TableHead>
                    <TableHead className="numeric">Memory</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {composition.map((c) => (
                    <TableRow key={c.chip}>
                      <TableCell>{c.chip}</TableCell>
                      <TableCell className="numeric">{num(c.count)}</TableCell>
                      <TableCell className="numeric">
                        {num(c.serving)}
                      </TableCell>
                      <TableCell className="numeric">{num(c.gpu)}</TableCell>
                      <TableCell className="numeric">
                        {num(c.memory)} GB
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
          {!!stats?.request_flows?.length && (
            <div className="network-composition">
              <h3>Request routes</h3>
              <div className="process-scroll">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Origin</TableHead>
                      <TableHead>Provider region</TableHead>
                      <TableHead className="numeric">Requests</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {[...stats.request_flows]
                      .sort((a, b) => b.requests - a.requests)
                      .map((f, i) => (
                        <TableRow key={`${f.key}:${i}`}>
                          <TableCell>{location(f.from)}</TableCell>
                          <TableCell>{location(f.to)}</TableCell>
                          <TableCell className="numeric">
                            {num(f.requests)}
                          </TableCell>
                        </TableRow>
                      ))}
                  </TableBody>
                </Table>
              </div>
            </div>
          )}
          <Collapsible open={directory} onOpenChange={setDirectory}>
            <CollapsibleTrigger className="expand-button">
              Explore provider directory · {num(stats?.providers?.length)}
            </CollapsibleTrigger>
            <CollapsibleContent>
              {directory &&
                (stats?.providers?.length ? (
                  <div className="process-scroll">
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead>Provider</TableHead>
                          <TableHead>Chip</TableHead>
                          <TableHead className="numeric">Memory</TableHead>
                          <TableHead>Trust</TableHead>
                          <TableHead>Routing</TableHead>
                          <TableHead className="numeric">GPU cores</TableHead>
                          <TableHead className="numeric">
                            Decode tok/s
                          </TableHead>
                          <TableHead className="numeric">
                            Requests served
                          </TableHead>
                          <TableHead className="numeric">
                            Output tokens
                          </TableHead>
                          <TableHead>Current model</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {(allProviders
                          ? stats.providers
                          : [...stats.providers]
                              .sort(
                                (a, b) =>
                                  (b.tokens_generated ?? 0) -
                                  (a.tokens_generated ?? 0),
                              )
                              .slice(0, 15)
                        ).map((p, i) => (
                          <TableRow key={`${p.id}:${i}`}>
                            <TableCell title={p.id}>
                              {p.id?.slice(0, 12)}
                            </TableCell>
                            <TableCell>
                              {p.chip ?? p.hardware_chip ?? '—'}
                            </TableCell>
                            <TableCell className="numeric">
                              {num(p.memory_gb)} GB
                            </TableCell>
                            <TableCell>{p.trust_level ?? '—'}</TableCell>
                            <TableCell>
                              {p.routable == null
                                ? '—'
                                : p.routable
                                  ? 'Ready'
                                  : 'Unavailable'}
                            </TableCell>
                            <TableCell className="numeric">
                              {num(p.gpu_cores)}
                            </TableCell>
                            <TableCell className="numeric">
                              {num(p.decode_tps, 1)}
                            </TableCell>
                            <TableCell className="numeric">
                              {num(p.requests_served)}
                            </TableCell>
                            <TableCell className="numeric">
                              {num(p.tokens_generated)}
                            </TableCell>
                            <TableCell>
                              {p.current_model
                                ? shortModel(p.current_model)
                                : '—'}
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                    {stats.providers.length > 15 && (
                      <button
                        type="button"
                        className="text-link"
                        onClick={() => setAllProviders((v) => !v)}
                      >
                        {allProviders
                          ? 'Show the busiest 15'
                          : `Show all ${stats.providers.length} Macs`}
                      </button>
                    )}
                  </div>
                ) : (
                  <p className="footnote">
                    Provider details will appear when the statistics endpoint
                    responds.
                  </p>
                ))}
            </CollapsibleContent>
          </Collapsible>
        </section>
        <p className="footnote network-footer">
          Public Darkbloom APIs · capacity refreshes every 30 seconds; totals,
          geography, and traffic every 60 seconds. Stale values are labeled
          above.
        </p>
      </AppScreen>
    </div>
  );
});
