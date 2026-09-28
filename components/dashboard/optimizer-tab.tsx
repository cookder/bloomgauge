'use client';
import { ModelEvidence } from './model-evidence';
import { StallRecovery } from './stall-recovery';
import {
  recordSupportIssue,
  observeModelResult,
  observeSupportCondition,
} from '@/lib/support-issues';
import {
  ResponseValidationError,
  startChartPolling,
} from '@/lib/chart-polling';
import { usePageVisible } from '@/lib/use-page-visibility';
import { distinctLabels } from '@/lib/model-label';
import {
  keepReadablePlan,
  readOptimizerResponse,
  savedPlanUnreadable,
} from '@/lib/optimizer-response';
import {
  consumeOptimizerDetails,
  OPTIMIZER_DETAILS_EVENT,
} from '@/lib/optimizer-details-intent';
import {
  consumeFeatureSetup,
  FEATURE_SETUP_EVENT,
} from '@/lib/feature-discovery-intent';
import { useEffect, useRef, useState } from 'react';
import { AppScreen, NavigationHub, useAppNavigation } from './app-navigation';
import { ManualModelControl } from './manual-model';
import { OptimizerControl } from './optimizer-control';
import { ModelHistory } from './model-history';
import { DemandBaselines } from './demand-baselines';
import { ModelSwitchNotifications } from './demand-alerts';
import {
  DemandAutoPanel,
  defaultDemandRules,
  type DemandAutoData,
  type DemandRules,
} from './demand-auto';
import { OptimizerPlan } from './optimizer-plan';
import { NetworkEvidencePanel } from './manager-status';
import { WhatsChanged } from './whats-changed';
import {
  excursionsSetting,
  lastPause,
  optimizerStrategy,
  readManager,
} from '@/lib/optimizer-manager';
import { CalendarDays, Play, RefreshCw, RotateCcw } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { type Warmup } from './model-warmup';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import {
  age,
  bounds,
  Choice,
  money,
  num,
  RangePicker,
  shortModel,
  TimeChart,
  type HistoryResponse,
  type Range,
  plural,
} from './shared';

type Evidence = {
  hours?: number;
  usd?: number;
  jobs?: number;
  localJobs?: number;
  usdPerHour?: number;
  jobsPerHour?: number;
  tokensPerSecond?: number;
  busyPercent?: number;
  days?: number;
  blocks?: number;
  switchMinutes?: number;
  eligible?: boolean;
  tested?: boolean;
  bothWarmPercent?: number;
  perModel?: Record<string, { usd: number; jobs: number }>;
  timeSlots?: {
    weekend: boolean;
    hour: number;
    hours: number;
    usdPerHour: number;
  }[];
};
type Model = {
  id: string;
  name: string;
  available: boolean;
  reason?: string;
  downloaded: boolean;
  memoryGB?: number;
  selected: boolean;
  current: boolean;
  evidence: Evidence;
  demand?: {
    active: number;
    queued: number;
    warm: number;
    pressure: number;
    samples: number;
  };
  liveDemand?: { active: number; queued: number; warm: number };
  loadBudget?: {
    afterUnloadGB: number;
    requiredGB: number;
    reserveGB: number;
  } | null;
};
type Optimizer = {
  reporting?: import('@/lib/pulse-run-status').MultiModelReporting;
  at: number;
  mode: string;
  status: string;
  detail: string;
  lastSwitchResult?: { at: number; outcome: string } | null;
  demandAuto?: DemandAutoData;
  stallRecovery?: unknown;
  resumeDemand?: {
    hasSavedPlan: boolean;
    available: boolean;
    currentModel?: string;
    selectedCount: number;
    availableCount?: number;
    reason?: string;
  };
  warmup?: Warmup;
  models: Model[];
  selected: string[];
  blockHours: number;
  startedAt?: number;
  endsAt?: number;
  nextSwitchAt?: number;
  currentModel?: string;
  combinations?: {
    plan?: {
      status: string;
      pairs: string[][];
      queuedFor: number;
      blockHours: number;
      startedAt?: number;
      endsAt?: number;
      referenceModel?: string;
      endedAt?: number;
      detail: string;
    };
    candidates: {
      id: string;
      models: string[];
      name: string;
      available: boolean;
      fitsNow: boolean;
      reason?: string;
      loadBudget?: Model['loadBudget'];
    }[];
    results: { id: string; name: string; evidence: Evidence }[];
  };
  originalModel?: string;
  requestedModel?: string;
  requestedKind?: string;
  memory?: {
    availableGB?: number;
    cachedFilesGB?: number;
    purgeableGB?: number;
    providerGB?: number;
    providerCacheGB?: number;
    automaticPurge?: boolean;
    cacheRecovery?: {
      status: string;
      detail: string;
      model?: string;
      at?: number;
    };
  };
  busy: boolean;
  canManage: boolean;
  remote: boolean;
  controlVersion: string;
  controlError?: string;
  discoveryError?: string;
  discoveryAt: number;
  identityVerified: boolean;
  networkFresh: boolean;
  coverageStart?: number;
  coverageEnd?: number;
  events: {
    at: number;
    kind: string;
    model: string;
    detail: string;
    downtime: number;
  }[];
};
const stamp = (v?: number) =>
  v
    ? new Date(v * 1000).toLocaleString([], {
        weekday: 'short',
        month: 'short',
        day: 'numeric',
        hour: 'numeric',
        minute: '2-digit',
      })
    : '—';
const series = {
  demand: [
    {
      key: 'activeRequests',
      label: 'Active + queued requests',
      color: 'var(--c-a995ff)',
    },
  ],
  pressure: [
    {
      key: 'pressure',
      label: 'Requests per warm provider',
      color: 'var(--c-87b9ff)',
    },
  ],
  earnings: [
    {
      key: 'usdPerHour',
      label: 'This Mac · inference USD / hour',
      color: 'var(--c-82efb5)',
    },
  ],
};

export function OptimizerTab({
  paused,
  connectionError = '',
}: {
  paused: boolean;
  connectionError?: string;
}) {
  const pageVisible = usePageVisible();
  const { mobile, visible, navigate, tab } = useAppNavigation();
  const [data, setData] = useState<Optimizer | null>(null),
    // Parts of the last response that were dropped as unreadable ("Couldn't read").
    [unreadable, setUnreadable] = useState<string[]>([]),
    [error, setError] = useState(''),
    [actionError, setActionError] = useState(''),
    [busy, setBusy] = useState(false);
  // The last reading, so a plan section this reading dropped keeps its last value.
  const lastData = useRef<Optimizer | null>(null);
  useEffect(() => {
    lastData.current = data;
  }, [data]);
  const [range, setRange] = useState<Range>({ preset: 'all' }),
    [selected, setSelected] = useState<string[]>([]),
    // Other strategies (scheduled tests). Follow demand is edited through the plan.
    [mode, setMode] = useState('week'),
    [hours, setHours] = useState('2');
  const [autoRules, setAutoRules] = useState<DemandRules>(defaultDemandRules);
  const rangeKey = JSON.stringify(range);
  const [loadedRangeKey, setLoadedRangeKey] = useState<string | null>(null);
  const rangeMatches = rangeKey === loadedRangeKey;
  const [chartModel, setChartModel] = useState(''),
    [metric, setMetric] = useState<keyof typeof series>('demand'),
    [savedHistory, setSavedHistory] = useState<{
      key: string;
      data: HistoryResponse | null;
      error: string;
    } | null>(null);
  const historyKey = JSON.stringify([range, chartModel]);
  const history = savedHistory?.key === historyKey ? savedHistory.data : null;
  const chartError = savedHistory?.key === historyKey ? savedHistory.error : '';
  const initialized = useRef(false);
  const serverPlan = useRef('');
  const actionPending = useRef(false);
  const updateEpoch = useRef(0);
  const [purgeCopied, setPurgeCopied] = useState(false);
  const [resultsView, setResultsView] = useState('history');
  const [insightsOpen, setInsightsOpen] = useState(false);
  const [evidenceOpen, setEvidenceOpen] = useState(false);
  useEffect(() => {
    const reveal = () => {
      if (consumeOptimizerDetails()) setInsightsOpen(true);
    };
    window.addEventListener(OPTIMIZER_DETAILS_EVENT, reveal);
    reveal();
    return () => window.removeEventListener(OPTIMIZER_DETAILS_EVENT, reveal);
  }, []);
  const [refreshRevision, setRefreshRevision] = useState(0);
  const analyticsVisible =
    (visible('results') && resultsView !== 'history') ||
    visible('pairs') ||
    visible('diagnostics') ||
    visible('test') ||
    // Model controls read the manager's home and pin from this response.
    visible('switch');
  const planDirty = useRef(false);
  function showPlan() {
    requestAnimationFrame(() =>
      document
        .getElementById('optimizer-plan')
        ?.scrollIntoView({ block: 'start', behavior: 'smooth' }),
    );
  }
  useEffect(() => {
    const reveal = () => {
      if (consumeFeatureSetup()) showPlan();
    };
    window.addEventListener(FEATURE_SETUP_EVENT, reveal);
    reveal();
    return () => window.removeEventListener(FEATURE_SETUP_EVENT, reveal);
  }, []);
  useEffect(() => {
    if (!pageVisible || tab !== 'optimizer' || !analyticsVisible) return;
    return startChartPolling({
      issueContext: 'models',
      load: async (signal) => {
        const epoch = updateEpoch.current;
        const { start, end } = bounds(
          visible('results') ? range : { preset: '24h' },
        );
        const res = await fetch(`/api/optimizer?from=${start}&to=${end}`, {
          cache: 'no-store',
          signal,
        });
        if (!res.ok)
          throw Error(
            'The optimizer is unavailable. Check that Bloomkeeper is running.',
          );
        const read = readOptimizerResponse<Optimizer>(await res.json());
        if (!read)
          throw new ResponseValidationError(
            'optimizer',
            'The optimizer response is incomplete. Retrying without replacing saved readings.',
          );
        return { ...read, epoch };
      },
      onValue: ({ value: reading, unreadable, reportable, epoch }) => {
        if (epoch !== updateEpoch.current || actionPending.current) return;
        const value = keepReadablePlan(reading, unreadable, lastData.current);
        observeModelResult(value.lastSwitchResult, value.demandAuto?.runs);
        // A dropped part counts as a validation issue once it persists (60 s);
        // dropped history rows are only named.
        observeSupportCondition(
          'validation',
          'models',
          reportable,
          Date.now(),
          'optimizer',
        );
        setUnreadable(unreadable);
        setData(value);
        setLoadedRangeKey(
          visible('results') ? rangeKey : JSON.stringify({ preset: '24h' }),
        );
        setError('');
        const plan = JSON.stringify([
          value.mode,
          value.selected,
          value.blockHours,
          value.demandAuto?.savedPolicy ?? value.demandAuto?.policy,
        ]);
        if (
          plan !== serverPlan.current &&
          !planDirty.current &&
          (value.mode !== 'observe' || value.selected.length)
        ) {
          if (value.selected.length) setSelected(value.selected);
          setHours(String(value.blockHours));
          if (value.mode !== 'observe' && value.mode !== 'demand')
            setMode(value.mode === 'combo' ? 'week' : value.mode);
          if (value.demandAuto)
            setAutoRules({
              ...defaultDemandRules,
              ...(value.demandAuto.savedPolicy ?? value.demandAuto.policy),
            });
        }
        serverPlan.current = plan;
        if (
          !initialized.current &&
          (value.selected.length || value.models.length)
        ) {
          const available = value.models.filter((m) => m.available);
          const preferred = value.selected.length
            ? value.selected
            : available.map((m) => m.id);
          setSelected(
            value.selected.length
              ? [...value.selected]
              : [...new Set([value.currentModel, ...preferred])]
                  .filter((id): id is string => !!id)
                  .slice(0, 16),
          );
          setHours(String(value.blockHours));
          setMode(value.mode === 'optimize' ? 'optimize' : 'week');
          if (value.demandAuto)
            setAutoRules({
              ...defaultDemandRules,
              ...(value.demandAuto.savedPolicy ?? value.demandAuto.policy),
            });
          initialized.current = true;
        }
        setChartModel(
          (previous) =>
            previous || value.currentModel || value.models[0]?.id || '',
        );
      },
      onError: (error) => {
        if (!actionPending.current) setError(error.message);
      },
      intervalMs: 30000,
    });
  }, [
    rangeKey,
    pageVisible,
    tab,
    analyticsVisible,
    refreshRevision,
    visible('results'),
  ]);
  useEffect(() => {
    if (
      !pageVisible ||
      tab !== 'optimizer' ||
      !visible('results') ||
      resultsView !== 'trends' ||
      !chartModel ||
      (paused && history)
    )
      return;
    return startChartPolling({
      load: async (signal) => {
        const { start, end } = bounds(range);
        const res = await fetch(
          `/api/optimizer/history?from=${start}&to=${end}&model=${encodeURIComponent(chartModel)}`,
          { cache: 'no-store', signal },
        );
        if (!res.ok)
          throw Error(
            'Model history could not be loaded. Saved readings may be out of date.',
          );
        const value = (await res.json()) as HistoryResponse;
        if (!value || !Array.isArray(value.samples))
          throw Error('Model history is unavailable.');
        return value;
      },
      onValue: (value) =>
        setSavedHistory({ key: historyKey, data: value, error: '' }),
      onError: (error) =>
        setSavedHistory((old) => ({
          key: historyKey,
          data: old?.key === historyKey ? old.data : null,
          error: error.message,
        })),
      intervalMs: 30000,
      repeat: !paused,
    });
  }, [
    paused,
    historyKey,
    mobile,
    visible('results'),
    resultsView,
    pageVisible,
    tab,
  ]);
  async function action(
    action:
      | 'start'
      | 'pause'
      | 'restore'
      | 'refresh'
      | 'schedule-combos'
      | 'cancel-combos',
  ) {
    if (actionPending.current || !data) return;
    actionPending.current = true;
    updateEpoch.current += 1;
    setBusy(true);
    setActionError('');
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const res = await fetch('/api/optimizer', {
        method: 'POST',
        signal: controller.signal,
        headers: {
          'Content-Type': 'application/json',
          'X-Bloom-Action': 'optimizer',
        },
        body: JSON.stringify({
          action,
          mode,
          models: selected,
          blockHours: Number(hours),
          demandPolicy: autoRules,
          expectedControl: data.controlVersion,
        }),
      });
      const reply = (await res.json()) as Optimizer & { error?: string };
      if (!res.ok)
        throw Error(reply.error || 'Could not change optimizer settings.');
      const read = readOptimizerResponse<Optimizer>(reply),
        value = read?.value;
      if (!value)
        throw Error(
          'The request response was incomplete. Check the live optimizer status before retrying.',
        );
      // Action response uses the default range; leave comparisons to the next ranged poll.
      setData((previous) =>
        previous
          ? {
              ...previous,
              mode: value.mode,
              status: value.status,
              detail: value.detail,
              warmup: value.warmup,
              demandAuto: keepReadablePlan(
                value,
                read?.unreadable ?? [],
                previous,
              ).demandAuto,
              resumeDemand: value.resumeDemand,
              selected: value.selected,
              busy: value.busy,
              startedAt: value.startedAt,
              endsAt: value.endsAt,
              originalModel: value.originalModel,
              requestedModel: value.requestedModel,
              requestedKind: value.requestedKind,
              controlVersion: value.controlVersion,
              nextSwitchAt: value.nextSwitchAt,
              combinations: value.combinations
                ? {
                    ...value.combinations,
                    results: previous.combinations?.results ?? [],
                  }
                : value.combinations,
            }
          : value,
      );
    } catch (e) {
      recordSupportIssue('action', 'models');
      setActionError(
        e instanceof Error && e.name !== 'AbortError'
          ? e.message
          : 'The request could not be confirmed. Check the live test status before retrying.',
      );
    } finally {
      clearTimeout(timeout);
      updateEpoch.current += 1;
      actionPending.current = false;
      setBusy(false);
    }
  }
  async function setDataGathering(seconds: number) {
    if (actionPending.current || !data) return;
    actionPending.current = true;
    updateEpoch.current += 1;
    setBusy(true);
    setActionError('');
    try {
      const res = await fetch('/api/optimizer', {
        method: 'POST',
        signal: AbortSignal.timeout(15000),
        headers: {
          'Content-Type': 'application/json',
          'X-Bloom-Action': 'optimizer',
        },
        body: JSON.stringify({
          action: 'data-gathering',
          seconds,
          expectedControl: data.controlVersion,
        }),
      });
      const reply = (await res.json()) as Optimizer & { error?: string };
      if (!res.ok)
        throw Error(reply.error || 'Could not change learning boost.');
      const read = readOptimizerResponse<Optimizer>(reply),
        value = read?.value;
      if (!value)
        throw Error(
          'The request response was incomplete. Check the live optimizer status before retrying.',
        );
      setData((previous) =>
        previous
          ? {
              ...previous,
              demandAuto: keepReadablePlan(
                value,
                read?.unreadable ?? [],
                previous,
              ).demandAuto,
              controlVersion: value.controlVersion,
              status: value.status,
              detail: value.detail,
            }
          : value,
      );
    } catch (e) {
      setActionError(
        e instanceof Error && e.name !== 'TimeoutError'
          ? e.message
          : 'The request could not be confirmed. Check the live optimizer status before retrying.',
      );
    } finally {
      updateEpoch.current += 1;
      actionPending.current = false;
      setBusy(false);
    }
  }
  // One saved setting (e.g. managerExcursions), merged into the saved plan by the
  // backend. Never switches a model.
  const [policyBusy, setPolicyBusy] = useState(false);
  async function setPolicy(changes: Partial<DemandRules>) {
    if (actionPending.current || !data) return;
    actionPending.current = true;
    updateEpoch.current += 1;
    setBusy(true);
    setPolicyBusy(true);
    setActionError('');
    try {
      const res = await fetch('/api/optimizer', {
        method: 'POST',
        signal: AbortSignal.timeout(15000),
        headers: {
          'Content-Type': 'application/json',
          'X-Bloom-Action': 'optimizer',
        },
        body: JSON.stringify({
          action: 'update-plan',
          demandPolicy: changes,
          expectedControl: data.controlVersion,
        }),
      });
      const reply = (await res.json()) as Optimizer & { error?: string };
      if (!res.ok) throw Error(reply.error || 'Could not save the setting.');
      const read = readOptimizerResponse<Optimizer>(reply),
        value = read?.value;
      if (!value)
        throw Error(
          'The request response was incomplete. Check the live optimizer status before retrying.',
        );
      setData((previous) =>
        previous
          ? {
              ...previous,
              demandAuto: keepReadablePlan(
                value,
                read?.unreadable ?? [],
                previous,
              ).demandAuto,
              controlVersion: value.controlVersion,
              status: value.status,
              detail: value.detail,
            }
          : value,
      );
      // Keep an unsaved plan edit, but carry the new value into it.
      setAutoRules((rules) => ({ ...rules, ...changes }));
    } catch (e) {
      setActionError(
        e instanceof Error && e.name !== 'TimeoutError'
          ? e.message
          : 'The request could not be confirmed. Check the live optimizer status before retrying.',
      );
    } finally {
      updateEpoch.current += 1;
      actionPending.current = false;
      setBusy(false);
      setPolicyBusy(false);
    }
  }
  const manager = readManager(data?.demandAuto?.manager);
  const savedPolicy = (data?.demandAuto?.savedPolicy ??
    data?.demandAuto?.policy) as Record<string, unknown> | undefined;
  const strategy = optimizerStrategy({ manager, savedPolicy });
  const managed = strategy === 'manager';
  const excursions = excursionsSetting(savedPolicy);
  const savedRules = data?.demandAuto
    ? {
        ...defaultDemandRules,
        ...(data.demandAuto.savedPolicy ?? data.demandAuto.policy),
      }
    : null;
  // Before a first plan is saved, the On switch uses the current model plus every available one.
  const savedSelection = !data
    ? []
    : data.selected.length
      ? data.selected
      : [
          ...new Set([
            data.currentModel,
            ...data.models.filter((m) => m.available).map((m) => m.id),
          ]),
        ]
          .filter((id): id is string => !!id)
          .slice(0, 16);
  planDirty.current =
    !!data &&
    (selected.length !== savedSelection.length ||
      selected.some((id) => !savedSelection.includes(id)) ||
      (!!savedRules &&
        JSON.stringify(autoRules) !== JSON.stringify(savedRules)));
  // Saving while the saved plan can't be read could replace it with something else.
  const planUnreadable = savedPlanUnreadable(unreadable);
  const planEditable =
    !!data?.canManage &&
    !data.busy &&
    !data.requestedModel &&
    !planUnreadable &&
    (data.mode === 'observe' || data.mode === 'demand');
  const planLockedReason = !data
    ? ''
    : !data.canManage
      ? 'Open the dashboard on your Mac or your private phone connection to change the plan.'
      : data.busy || data.requestedModel
        ? 'A model change is in progress. The plan can be changed when it finishes.'
        : planUnreadable
          ? 'Your saved plan couldn’t be read. You can change it once it loads again.'
          : data.mode !== 'observe' && data.mode !== 'demand'
            ? 'A scheduled test is running. Switch to Manual to change the plan.'
            : '';
  function discardPlan() {
    if (!data) return;
    planDirty.current = false;
    setSelected(savedSelection);
    if (savedRules) setAutoRules(savedRules);
  }
  async function savePlan() {
    if (actionPending.current || !data) return;
    actionPending.current = true;
    updateEpoch.current += 1;
    setBusy(true);
    setActionError('');
    try {
      const res = await fetch('/api/optimizer', {
        method: 'POST',
        signal: AbortSignal.timeout(15000),
        headers: {
          'Content-Type': 'application/json',
          'X-Bloom-Action': 'optimizer',
        },
        body: JSON.stringify({
          action: 'update-plan',
          models: selected,
          demandPolicy: autoRules,
          expectedControl: data.controlVersion,
        }),
      });
      const reply = (await res.json()) as Optimizer & { error?: string };
      if (!res.ok) throw Error(reply.error || 'Could not save the plan.');
      const read = readOptimizerResponse<Optimizer>(reply),
        value = read?.value;
      if (!value)
        throw Error(
          'The request response was incomplete. Check the live optimizer status before retrying.',
        );
      planDirty.current = false;
      setData((previous) =>
        previous
          ? {
              ...previous,
              selected: value.selected,
              demandAuto: keepReadablePlan(
                value,
                read?.unreadable ?? [],
                previous,
              ).demandAuto,
              controlVersion: value.controlVersion,
              status: value.status,
              detail: value.detail,
            }
          : value,
      );
      setRefreshRevision((n) => n + 1);
    } catch (e) {
      setActionError(
        e instanceof Error && e.name !== 'TimeoutError'
          ? e.message
          : 'The request could not be confirmed. Check the live optimizer status before retrying.',
      );
    } finally {
      updateEpoch.current += 1;
      actionPending.current = false;
      setBusy(false);
    }
  }
  const comparisonModels = (data?.models ?? []).map((model) =>
    rangeMatches
      ? model
      : {
          ...model,
          evidence: {} as Evidence,
          demand: undefined,
        },
  );
  const historyModels = [
    ...comparisonModels,
    ...(data?.combinations?.results ?? []).map((pair) =>
      rangeMatches ? pair : { ...pair, evidence: {} as Evidence },
    ),
  ];
  const chosen = historyModels.find((m) => m.id === chartModel);
  const combo = data?.combinations;
  const comboResults = rangeMatches ? (combo?.results ?? []) : [];
  const comboPlan = combo?.plan;
  // An ended test stays as a dated note; after a week it no longer sets the status.
  const comboEnded =
    !!comboPlan && !['queued', 'running'].includes(comboPlan.status);
  const comboEndedAt =
    comboPlan?.endedAt ?? comboPlan?.endsAt ?? comboPlan?.queuedFor ?? 0;
  const comboStale = comboEnded && Date.now() / 1000 - comboEndedAt > 7 * 86400;
  const compatiblePairs =
    combo?.candidates.filter((pair) => pair.available) ?? [];
  const measured = comparisonModels.filter((m) => m.evidence.hours);
  const best = [...measured].sort(
    (a, b) => (b.evidence.usdPerHour ?? 0) - (a.evidence.usdPerHour ?? 0),
  )[0];
  // Models the provider offers that Darkbloom's catalog no longer lists get no
  // network work; show them (dimmed) so the model list matches the provider.
  const offeredNotDownloaded = Array.isArray(
    data?.reporting?.offeredNotDownloaded,
  )
    ? data.reporting.offeredNotDownloaded.filter(
        (id): id is string => typeof id === 'string' && id.length > 0,
      )
    : [];
  const offeredNotInCatalog = (data?.reporting?.models ?? []).filter(
    (id) => !(data?.models ?? []).some((m) => m.id === id),
  );
  const planModels = [
    ...(data?.models ?? []),
    ...offeredNotInCatalog.map((id) => ({
      id,
      name: id,
      available: false,
      reason: 'Offered by your provider but not in Darkbloom’s catalog',
    })),
  ];
  const running = !!data && data.mode !== 'observe';
  const restoring = !!data?.requestedModel;
  const locked =
    !data?.canManage || busy || data?.busy || !!running || restoring;
  const budgets = (data?.models ?? [])
    .filter((m) => selected.includes(m.id) && m.loadBudget)
    .map((m) => m.loadBudget!);
  const startReason = !data
    ? 'Loading the optimizer…'
    : !data.canManage
      ? 'Open the dashboard on your Mac or through your private phone connection to manage tests.'
      : busy
        ? 'Sending your request…'
        : data.busy || restoring
          ? 'A model change is in progress. Wait for it to finish or use the manual controls above.'
          : running
            ? 'Switch to Manual above before applying settings.'
            : error || data.controlError
              ? error || data.controlError
              : data.models.find(
                    (model) =>
                      model.id === data.currentModel && !model.available,
                  )?.reason
                ? data.models.find((model) => model.id === data.currentModel)!
                    .reason!
                : !data.identityVerified
                  ? 'Waiting to match this Mac to Darkbloom’s provider roster.'
                  : mode === 'demand' &&
                      (!data.demandAuto || data.demandAuto.controlError)
                    ? data.demandAuto?.controlError ||
                      'Waiting for the updated demand-following service.'
                    : selected.length < 2 ||
                        selected.length > (mode === 'demand' ? 16 : 6)
                      ? `Choose 2–${mode === 'demand' ? 16 : 6} models under Models Bloomkeeper can use.`
                      : !selected.includes(data.currentModel ?? '')
                        ? 'Include the current model under Models Bloomkeeper can use so Bloomkeeper can compare it with alternatives.'
                        : selected.some(
                              (id) =>
                                !data.models.some(
                                  (m) => m.id === id && m.available,
                                ),
                            )
                          ? 'Some chosen models are unavailable. Hover a dimmed model to see why.'
                          : '';
  const startDisabled = !!startReason;
  const failedCleanup =
    data?.memory?.cacheRecovery?.status === 'failed'
      ? data.memory.cacheRecovery
      : null;
  return (
    <div className="optimizer-view">
      <section className="page-heading desktop-page-heading">
        <div>
          <div className="eyebrow">OPTIMIZER</div>
          <h1>Your Mac. Your choice.</h1>
          <p>
            Let Bloomkeeper manage models, or choose one yourself. Reporting
            continues in either mode.
          </p>
        </div>
      </section>
      <AppScreen name="results">
        {error && resultsView !== 'history' && (
          <p className="notice" role="status">
            Model comparisons are delayed.{' '}
            {rangeMatches
              ? 'Shown values are the last confirmed readings.'
              : 'This period has not loaded yet.'}{' '}
            <Button
              variant="ghost"
              onClick={() => setRefreshRevision((n) => n + 1)}
            >
              Retry comparisons
            </Button>
          </p>
        )}
        <div
          className="app-result-toggle model-result-views"
          aria-label="Model result view"
        >
          <button
            type="button"
            aria-pressed={resultsView === 'history'}
            onClick={() => setResultsView('history')}
          >
            Saved history
          </button>
          <button
            type="button"
            aria-pressed={resultsView === 'models'}
            onClick={() => setResultsView('models')}
          >
            Compare & select
          </button>
          <button
            type="button"
            aria-pressed={resultsView === 'trends'}
            onClick={() => setResultsView('trends')}
          >
            Network & time slots
          </button>
          <button
            type="button"
            aria-pressed={resultsView === 'baselines'}
            onClick={() => setResultsView('baselines')}
          >
            Demand & earnings
          </button>
        </div>
        <section
          className="panel baseline-panel"
          hidden={resultsView !== 'baselines'}
        >
          <div className="panel-heading">
            <div>
              <div className="eyebrow">DEMAND → LOCAL RESULTS</div>
              <h2>What each level of demand earns.</h2>
            </div>
            <RangePicker
              value={range}
              onChange={setRange}
              label="Demand and earnings comparison range"
            />
          </div>
          <DemandBaselines
            range={range}
            paused={paused}
            enabled={resultsView === 'baselines'}
            auto={data?.demandAuto}
          />
        </section>
        <section
          className="panel passive-history-panel"
          hidden={resultsView !== 'history'}
        >
          <div className="panel-heading">
            <div>
              <div className="eyebrow">PASSIVE MODEL HISTORY</div>
              <h2>All your model runs, together.</h2>
            </div>
            <RangePicker
              value={range}
              onChange={setRange}
              label="Passive model history range"
            />
          </div>
          <ModelHistory
            range={range}
            paused={paused}
            enabled={resultsView === 'history'}
          />
        </section>
        <section className="panel" hidden={resultsView !== 'models'}>
          <div className="panel-heading">
            <div>
              <div className="eyebrow">MODEL COMPARISON</div>
              <h2>
                {!rangeMatches
                  ? 'Loading the selected period…'
                  : best
                    ? `${best.name} leads the measured rates.`
                    : 'Your results start here.'}
              </h2>
            </div>
            <RangePicker
              value={range}
              onChange={setRange}
              label="Model comparison and history range"
            />
          </div>
          <p className="footnote optimizer-intro">
            Select two to six available models for optional automatic switching,
            including your current one. These rates include qualifying passive
            runs and earlier tests in the selected period. Rates use verified
            warm time, including ready idle time. Switching and loading overhead
            is shown separately. Base rewards are shown in Earnings; they cannot
            reliably be assigned to one model.
          </p>
          {!rangeMatches && (
            <p className="notice" role="status">
              {error
                ? 'The selected period could not be loaded. Earlier comparison values are hidden until it reconnects.'
                : 'Loading evidence for this period. Your model selection is retained.'}
            </p>
          )}
          <Table className="optimizer-table">
            <TableHeader>
              <TableRow>
                <TableHead>Test</TableHead>
                <TableHead>Model</TableHead>
                <TableHead>My earnings / hr</TableHead>
                <TableHead>Measured time</TableHead>
                <TableHead>Paid jobs / hr</TableHead>
                <TableHead>Network now</TableHead>
                <TableHead>Avg. load / warm provider</TableHead>
                <TableHead>Evidence</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {comparisonModels.map((m) => (
                <TableRow
                  key={m.id}
                  className={m.current ? 'optimizer-current' : ''}
                >
                  <TableCell>
                    <Checkbox
                      aria-label={`Include ${m.name} in model test`}
                      checked={selected.includes(m.id)}
                      disabled={
                        locked ||
                        !m.available ||
                        (m.current && selected.includes(m.id))
                      }
                      onCheckedChange={(checked) =>
                        setSelected((previous) =>
                          checked
                            ? [...previous, m.id]
                            : previous.filter((id) => id !== m.id),
                        )
                      }
                    />
                  </TableCell>
                  <TableCell>
                    <strong>{m.name}</strong>
                    <small>
                      {m.current ? 'Serving now · ' : ''}
                      {m.available
                        ? `${num(m.memoryGB, 1)} GB · available locally`
                        : m.reason}
                    </small>
                  </TableCell>
                  <TableCell className="optimizer-money">
                    {money(m.evidence.usdPerHour)}
                    <small>
                      {!rangeMatches
                        ? 'Period not loaded'
                        : m.evidence.hours
                          ? `${money(m.evidence.usd)} inference credits`
                          : 'No comparable run time yet'}
                    </small>
                  </TableCell>
                  <TableCell>
                    {m.evidence.hours ? `${num(m.evidence.hours, 1)}h` : '—'}
                    <small>
                      {m.evidence.days
                        ? `${plural(m.evidence.days, 'day')} · ${num(m.evidence.switchMinutes, 1)}m switching`
                        : ''}
                    </small>
                  </TableCell>
                  <TableCell>
                    {num(m.evidence.jobsPerHour, 1)}
                    <small>
                      {m.evidence.jobs != null
                        ? `${num(m.evidence.jobs)} paid jobs`
                        : ''}
                    </small>
                  </TableCell>
                  <TableCell>
                    {data?.networkFresh && m.liveDemand
                      ? `${num(m.liveDemand.active)} active`
                      : 'Unavailable'}
                    <small>
                      {data?.networkFresh && m.liveDemand
                        ? `${num(m.liveDemand.queued)} queued · ${num(m.liveDemand.warm)} warm providers`
                        : 'Waiting for fresh data'}
                    </small>
                  </TableCell>
                  <TableCell>
                    {num(m.demand?.pressure, 2)}
                    <small>
                      {m.demand
                        ? `${num(m.demand.samples)} network snapshots`
                        : 'Collecting history'}
                    </small>
                  </TableCell>
                  <TableCell>
                    <span
                      className={
                        m.evidence.eligible ? 'optimizer-ready' : 'muted'
                      }
                    >
                      {!rangeMatches
                        ? 'Period not loaded'
                        : m.evidence.eligible
                          ? 'Enough to compare'
                          : m.evidence.tested
                            ? 'Measured low traffic'
                            : m.evidence.hours
                              ? 'Limited evidence'
                              : 'No verified warm evidence'}
                    </span>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          {!data?.models.length && (
            <div className="empty">
              {data?.discoveryError ||
                'Reading installed models and the network catalog…'}
            </div>
          )}
          <div className="optimizer-table-footer">
            <p className="footnote">
              {data?.discoveryError ||
                `Model catalog: ${age(data?.discoveryAt)}. Network load is active + queued requests, not completed jobs or an equal share of traffic.`}
            </p>
            <Button
              variant="ghost"
              disabled={!data?.canManage || busy}
              onClick={() => action('refresh')}
            >
              <RefreshCw />
              Refresh models
            </Button>
          </div>
          <p className="footnote">
            Automatic comparisons need at least 6 measured hours, 2 dates, and 3
            substantial two-hour blocks. A winning candidate also needs 20 paid
            jobs; confirmed idle time still counts against a quiet current
            model. A new choice must improve the score by at least 20% and
            $0.005/hour for 15 minutes. Recent network load can adjust the
            estimate by at most ±25%; it cannot make an untested model a winner.
            These are selection rules, not a guarantee of higher earnings.
          </p>
        </section>
        <section className="panel" hidden={resultsView !== 'trends'}>
          <div className="panel-heading">
            <div>
              <div className="eyebrow">DEMAND & YOUR SHARE</div>
              <h2>Compare the network with your results.</h2>
              <RangePicker
                value={range}
                onChange={setRange}
                label="Model demand and time slot range"
              />
            </div>
            <div className="optimizer-options">
              <Choice
                value={chartModel}
                onChange={(value) => {
                  setChartModel(value);
                  if (value.startsWith('@combo:')) setMetric('earnings');
                }}
                label="Model history"
                options={historyModels.map((m, _, all) => ({
                  value: m.id,
                  label: distinctLabels(
                    all.map((x) => x.id),
                    shortModel,
                  )(m.id),
                }))}
              />
              <Choice
                value={metric}
                onChange={(v) => setMetric(v as keyof typeof series)}
                label="Model history metric"
                options={[
                  { value: 'demand', label: 'Network active + queued' },
                  {
                    value: 'pressure',
                    label: 'Network load per warm provider',
                  },
                  { value: 'earnings', label: 'My inference earnings / hour' },
                ]}
              />
            </div>
          </div>
          <TimeChart
            data={history?.samples ?? []}
            series={series[metric]}
            unit={metric === 'earnings' ? ' USD / hour' : ''}
            height={240}
          />
          <p className="footnote">
            {chartError ||
              (history?.coverageStart
                ? `Recorded since ${stamp(history.coverageStart)}. ${num(history.bucketSeconds)}-second chart buckets. Missing observations stay blank.`
                : 'Per-model network history begins with this update; the published historical traffic endpoint is network-wide.')}{' '}
            Earnings need a complete verified warm minute and matching credits,
            with a two-minute settlement delay.
          </p>
          <div className="optimizer-times">
            <h3>
              Your earnings by local time ·{' '}
              {chosen?.name || shortModel(chartModel)}
            </h3>
            <p className="footnote">
              {rangeMatches
                ? 'Each cell shows gross inference USD/hour and measured hours. Compare coverage before drawing conclusions.'
                : 'Loading the selected period. Earlier time-slot values are hidden.'}
            </p>
            <div className="optimizer-time-grid">
              <span />
              {[0, 4, 8, 12, 16, 20].map((h) => (
                <span key={h}>
                  {String(h).padStart(2, '0')}:00–
                  {String(h + 4).padStart(2, '0')}
                  :00
                </span>
              ))}
              {[false, true].map((weekend) => (
                <div className="optimizer-time-row" key={String(weekend)}>
                  <strong>{weekend ? 'Weekend' : 'Weekday'}</strong>
                  {[0, 4, 8, 12, 16, 20].map((hour) => {
                    const slot = chosen?.evidence.timeSlots?.find(
                      (s) => s.weekend === weekend && s.hour === hour,
                    );
                    return (
                      <div
                        key={hour}
                        className={
                          slot && slot.hours >= 1 ? 'has-evidence' : ''
                        }
                      >
                        {slot ? money(slot.usdPerHour) : '—'}
                        <small>
                          {slot
                            ? `${num(slot.hours, 1)}h measured`
                            : rangeMatches
                              ? 'No observations'
                              : 'Not loaded'}
                        </small>
                      </div>
                    );
                  })}
                </div>
              ))}
            </div>
          </div>
        </section>
      </AppScreen>
      <AppScreen name="optimizer-tools">
        <NavigationHub optimizer />
      </AppScreen>
      <AppScreen name="switch">
        <ManualModelControl
          paused={paused || !visible('switch')}
          managed={managed && data?.mode === 'demand'}
          homeModel={manager?.home?.model}
          pinned={!!manager?.pinned}
        />
      </AppScreen>
      <AppScreen name="test">
        <WhatsChanged />
        {data && !error && unreadable.length > 0 && (
          <p className="notice" role="status">
            Couldn’t read {unreadable.join(', ')}. The rest of this page is up
            to date.
          </p>
        )}
        {data?.reporting && (
          <div className="notice" role="status">
            <strong>
              Your provider offers {data.reporting.models.length} models ·
              statistics {data.reporting.counting ? 'on' : 'paused'}
            </strong>
            <p>{data.reporting.detail}</p>
            <p>
              Statistics count while any model the network sends this Mac work
              for is loaded. When Darkbloom loads or unloads a model, counting
              carries on in a new segment, so numbers from different sets never
              mix. While statistics are paused, the live pace and traffic meter
              wait; balance, credits and hourly totals keep updating. Per-model
              evidence is kept only for one model or a pair.
            </p>
            {offeredNotInCatalog.length > 0 && (
              <p>
                {offeredNotInCatalog.join(', ')}{' '}
                {offeredNotInCatalog.length === 1 ? 'is' : 'are'} no longer in
                Darkbloom’s catalog, so the network won’t send{' '}
                {offeredNotInCatalog.length === 1 ? 'it' : 'them'} work. Remove
                with <code>darkbloom models remove &lt;id&gt;</code>, then
                restart Darkbloom so it stops offering{' '}
                {offeredNotInCatalog.length === 1 ? 'it' : 'them'}.
              </p>
            )}
            {offeredNotDownloaded.length > 0 && (
              <p>
                {offeredNotDownloaded.join(', ')}{' '}
                {offeredNotDownloaded.length === 1 ? 'is' : 'are'} no longer
                downloaded, but Darkbloom keeps offering{' '}
                {offeredNotDownloaded.length === 1 ? 'it' : 'them'}. Restart
                Darkbloom to stop offering removed models.
              </p>
            )}
            <p>
              The Manager runs one model, or a pair without Gemma, so Darkbloom
              manages this set and Bloomkeeper never changes it. Public network
              data shows Macs serving Gemma alone get about twice the Gemma work
              of Macs that mix it with other models. To let the Manager run,
              pick one model in Model controls, then turn the Manager on.
            </p>
          </div>
        )}
        <OptimizerControl
          connectionError={connectionError}
          onSettings={showPlan}
          onChanged={() => setRefreshRevision((n) => n + 1)}
          strategy={strategy}
          manager={manager}
          excursions={excursions}
          excursionsBusy={policyBusy || busy || !data?.canManage}
          onExcursions={(on) =>
            void setPolicy({ managerExcursions: on ? 1 : 0 })
          }
          lastPause={lastPause(data?.events)}
          plan={
            !data ? (
              <p className="footnote">
                {error
                  ? 'Your plan is reconnecting. Saved settings are unchanged.'
                  : 'Loading your plan…'}
              </p>
            ) : (
              <>
                {actionError && (
                  <p className="notice" role="alert">
                    {actionError}
                  </p>
                )}
                {error && (
                  <p className="notice" role="status">
                    Settings are reconnecting. Your saved plan is unchanged.
                  </p>
                )}
                <OptimizerPlan
                  on={data.mode === 'demand'}
                  strategy={strategy}
                  excursions={excursions}
                  models={planModels}
                  currentModel={data.currentModel}
                  selected={selected}
                  onSelected={setSelected}
                  rules={autoRules}
                  onRules={setAutoRules}
                  savedSelected={savedSelection}
                  savedRules={
                    data.demandAuto
                      ? {
                          ...defaultDemandRules,
                          ...(data.demandAuto.savedPolicy ??
                            data.demandAuto.policy),
                        }
                      : undefined
                  }
                  gathering={data.demandAuto?.dataGathering}
                  onGathering={setDataGathering}
                  onSave={savePlan}
                  onDiscard={discardPlan}
                  editable={planEditable}
                  lockedReason={planLockedReason}
                  busy={busy}
                >
                  <div className="optimizer-other-strategies">
                    <p className="optimizer-plan-label">Other strategies</p>
                    <div className="optimizer-options">
                      <label>
                        Strategy
                        <Choice
                          value={mode}
                          disabled={locked}
                          onChange={setMode}
                          label="Other strategy"
                          options={[
                            { value: 'week', label: 'Seven-day comparison' },
                            {
                              value: 'optimize',
                              label: 'Historical earnings optimization',
                            },
                          ]}
                        />
                      </label>
                      <label>
                        Minimum run
                        <Choice
                          value={hours}
                          disabled={locked}
                          onChange={setHours}
                          label="Minimum model run time"
                          options={[
                            { value: '2', label: '2 hours' },
                            { value: '4', label: '4 hours' },
                          ]}
                        />
                      </label>
                    </div>
                    <p className="footnote">
                      {mode === 'week'
                        ? 'Rotates your selected models for seven calendar days, shifting their time slots each day. The current model runs first. Missed time stays missing; the test ends in Observe mode.'
                        : 'Checks every 15 seconds. Uses the last seven days of measured earnings, with time-of-day comparisons and a bounded adjustment for recent network demand. Holds models with insufficient evidence instead of guessing.'}
                    </p>
                    <div className="optimizer-actions">
                      <Button
                        variant="outline"
                        disabled={startDisabled}
                        onClick={() => action('start')}
                      >
                        <Play />
                        {mode === 'week'
                          ? 'Start seven-day test'
                          : 'Start historical optimization'}
                      </Button>
                      {data.originalModel && (
                        <Button
                          variant="ghost"
                          disabled={
                            !data.canManage ||
                            busy ||
                            data.busy ||
                            running ||
                            restoring ||
                            data.originalModel === data.currentModel
                          }
                          onClick={() => action('restore')}
                        >
                          <RotateCcw />
                          Restore {shortModel(data.originalModel)}
                        </Button>
                      )}
                    </div>
                    {startReason && (
                      <p className="footnote warning-text" role="status">
                        {startReason}
                      </p>
                    )}
                    <details className="optimizer-schedule">
                      <summary>Schedule details</summary>
                      <CalendarDays size={21} />
                      <dl>
                        <div>
                          <dt>Currently serving</dt>
                          <dd>{shortModel(data?.currentModel || 'Unknown')}</dd>
                        </div>
                        <div>
                          <dt>
                            {data?.mode === 'demand'
                              ? 'Earliest next switch'
                              : 'Next rotation boundary'}
                          </dt>
                          <dd>
                            {running &&
                            (data?.mode === 'week' || data?.mode === 'combo')
                              ? stamp(data.nextSwitchAt)
                              : data?.mode === 'demand' && data.demandAuto
                                ? stamp(data.demandAuto.limits.nextRunAt) +
                                  ' · only if qualified'
                                : 'After evidence + minimum run'}
                          </dd>
                        </div>
                        <div>
                          <dt>
                            {data?.mode === 'demand'
                              ? 'Demand following'
                              : 'Test ends'}
                          </dt>
                          <dd>
                            {data?.mode === 'combo'
                              ? stamp(comboPlan?.endsAt)
                              : running && data?.mode === 'week'
                                ? stamp(data.endsAt)
                                : data?.mode === 'demand'
                                  ? 'Until paused · no fixed rotation'
                                  : '—'}
                          </dd>
                        </div>
                      </dl>
                    </details>
                    <p className="optimizer-operation-note">
                      Keep this app open, the Mac awake, and connected to power.
                      Switching restarts Darkbloom once a switch qualifies; with
                      Darkbloom 0.9.9 or later, accepted work finishes first. It
                      preserves your local endpoint settings and attempts
                      recovery if a switch fails. Closing the app stops scanning
                      and switching; the current provider keeps running. The
                      top-bar pause freezes only the view; choose{' '}
                      {managed ? 'Off' : 'Manual'} to stop automation.
                    </p>
                  </div>
                </OptimizerPlan>
              </>
            )
          }
        />
        {data && (
          <StallRecovery
            value={data.stallRecovery}
            events={data.events}
            now={data.at}
            manager={managed}
          />
        )}
        {managed && data && (
          <NetworkEvidencePanel
            view={manager}
            currentModel={data.currentModel}
            now={data.at}
            names={Object.fromEntries(data.models.map((m) => [m.id, m.name]))}
          />
        )}
        <details
          className="quiet-disclosure model-evidence-disclosure"
          onToggle={(event) => setEvidenceOpen(event.currentTarget.open)}
        >
          <summary>What Bloomkeeper knows about each model</summary>
          <ModelEvidence
            open={evidenceOpen}
            rows={data?.demandAuto?.opportunities}
          />
        </details>
        <details
          className="optimizer-insights quiet-disclosure"
          open={insightsOpen}
          onToggle={(event) => setInsightsOpen(event.currentTarget.open)}
        >
          <summary>
            {managed ? 'Decision details' : 'Why Bloomkeeper chooses a model'}
          </summary>
          {insightsOpen && (
            <>
              {error && (
                <p className="notice" role="status">
                  Decision details are delayed. Optimizer controls remain
                  available above.
                </p>
              )}
              <DemandAutoPanel
                data={data?.demandAuto}
                stale={!!error}
                managed={managed}
              />
            </>
          )}
        </details>
        <details className="quiet-disclosure">
          <summary>Switch notifications</summary>
          <ModelSwitchNotifications />
        </details>

        {failedCleanup && (
          <div className="notice optimizer-switch-warning" role="status">
            <strong>Last switch hit a cache-cleanup blocker.</strong>
            <p>
              {failedCleanup.model
                ? `${shortModel(failedCleanup.model)} · `
                : ''}
              {stamp(failedCleanup.at)}. {failedCleanup.detail}
            </p>
            <p>
              {running
                ? 'This test is running again. The same cleanup can fail on a later rotation until Mac setup is complete.'
                : managed
                  ? 'Bloomkeeper restores the previous or home model and stays on.'
                  : 'Bloomkeeper pauses switching after a failed load and attempts to restore the previous model.'}
            </p>
            {mobile && (
              <button
                className="text-link"
                type="button"
                onClick={() => navigate('diagnostics')}
              >
                Open log & Mac setup →
              </button>
            )}
          </div>
        )}
      </AppScreen>
      <AppScreen name="pairs">
        <section className="panel combo-panel">
          <div className="panel-heading">
            <div>
              <div className="eyebrow">FOLLOW-ON TEST</div>
              <h2>Try two models together.</h2>
            </div>
            <span className="status-pill">
              {comboPlan && !comboStale ? comboPlan.status : 'Not scheduled'}
            </span>
          </div>
          <p className="footnote">
            After the current solo test, compare compatible pairs for seven more
            days in {data?.blockHours ?? 2}-hour runs, with a solo reference and
            changing time slots. Bloomkeeper warms each model in turn, then
            verifies both are loaded together. Memory, power, temperature and
            idle checks still apply.
          </p>
          {comboPlan && (
            <p className="combo-plan-note" role="status">
              {comboEnded &&
                `Last test ${comboPlan.status} ${stamp(comboEndedAt)}. `}
              {comboPlan.detail}{' '}
              {comboEnded
                ? ''
                : comboPlan.status === 'queued'
                  ? `Starts after ${stamp(comboPlan.queuedFor)}, when Bloomkeeper is running.`
                  : comboPlan.startedAt
                    ? `${stamp(comboPlan.startedAt)} → ${stamp(comboPlan.endsAt)}.`
                    : ''}
              {comboPlan.referenceModel
                ? ` Solo reference: ${shortModel(comboPlan.referenceModel)}.`
                : ''}
            </p>
          )}
          <div className="optimizer-actions">
            {comboPlan?.status !== 'queued' &&
              comboPlan?.status !== 'running' && (
                <Button
                  disabled={
                    !data?.canManage ||
                    busy ||
                    data.busy ||
                    !!data.requestedModel ||
                    data.mode !== 'week' ||
                    !data.identityVerified ||
                    !compatiblePairs.length
                  }
                  onClick={() => action('schedule-combos')}
                >
                  <CalendarDays />
                  Queue {Math.min(6, compatiblePairs.length) || ''} compatible
                  pairs after this test
                </Button>
              )}
            {(comboPlan?.status === 'queued' ||
              comboPlan?.status === 'running') && (
              <Button
                variant="outline"
                disabled={!data?.canManage || busy}
                onClick={() => action('cancel-combos')}
              >
                {comboPlan.status === 'queued'
                  ? 'Cancel follow-on test'
                  : 'Stop combination test'}
              </Button>
            )}
          </div>
          {data?.mode !== 'week' && !comboPlan && (
            <p className="footnote">
              Start a single-model week test first to schedule its follow-on
              combinations.
            </p>
          )}
          <Table className="optimizer-table combo-table">
            <TableHeader>
              <TableRow>
                <TableHead>Pair</TableHead>
                <TableHead>Memory required</TableHead>
                <TableHead>Readiness</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {(combo?.candidates ?? []).map((pair) => (
                <TableRow key={pair.id}>
                  <TableCell>
                    <strong>{pair.models.map(shortModel).join(' + ')}</strong>
                    {comboPlan?.pairs.some(
                      (models) =>
                        models.length === pair.models.length &&
                        models.every((m) => pair.models.includes(m)),
                    ) && <small>Included in follow-on plan</small>}
                  </TableCell>
                  <TableCell>
                    {num(pair.loadBudget?.requiredGB, 1)} GB
                    <small>
                      {num(pair.loadBudget?.afterUnloadGB, 1)} GB estimated
                      after unload
                    </small>
                  </TableCell>
                  <TableCell>
                    {pair.available
                      ? pair.fitsNow
                        ? 'Passes memory screen'
                        : 'Needs more available memory'
                      : pair.reason}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          {!compatiblePairs.length && (
            <p className="footnote">
              No pair currently passes the model, configuration and
              physical-memory screen. Requirements include both models plus OS
              and inference headroom.
            </p>
          )}
          {!rangeMatches && (
            <p className="footnote">
              Pair earnings will appear when the selected history period has
              loaded.
            </p>
          )}
          {!!comboResults.length && (
            <>
              <h3 className="combo-results-title">Measured pair results</h3>
              <Table className="optimizer-table combo-table">
                <TableHeader>
                  <TableRow>
                    <TableHead>Pair</TableHead>
                    <TableHead>Inference / hour</TableHead>
                    <TableHead>Observed time</TableHead>
                    <TableHead>Both warm</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {comboResults.map((result) => (
                    <TableRow key={result.id}>
                      <TableCell>
                        <strong>{shortModel(result.id)}</strong>
                        <small>
                          {Object.entries(result.evidence.perModel ?? {})
                            .map(
                              ([model, value]) =>
                                `${shortModel(model)}: ${money(value.usd)}`,
                            )
                            .join(' · ')}
                        </small>
                      </TableCell>
                      <TableCell>
                        {money(result.evidence.usdPerHour)}
                        <small>
                          {num(result.evidence.jobs)} matched paid jobs
                        </small>
                      </TableCell>
                      <TableCell>
                        {num(result.evidence.hours, 1)}h
                        <small>
                          {num(result.evidence.switchMinutes, 1)}m switching /
                          warm-up
                        </small>
                      </TableCell>
                      <TableCell>
                        {num(result.evidence.bothWarmPercent, 1)}%
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </>
          )}
          <p className="footnote">
            Pair results use this Mac’s paid work for that exact combination.
            Idle time counts only while both models are loaded and pre-warmed;
            cold time and warm-up requests are excluded. Failed warm-up pauses
            the test and attempts an idle recovery. Cancelling a queued phase
            leaves the solo test running; manual changes or Pause switching
            cancel the follow-on plan.
          </p>
        </section>
      </AppScreen>
      <AppScreen name="diagnostics">
        {failedCleanup && (
          <section className="panel cache-setup-panel">
            <div className="eyebrow">
              LAST CLEANUP ATTEMPT · {stamp(failedCleanup.at)}
            </div>
            <h2>Check cache cleanup permission.</h2>
            <p>{failedCleanup.detail}</p>
            <p>
              Manual model controls show the current permission and memory
              check. On your Mac, you can enable optional cache cleanup with a
              one-time macOS administrator approval.
            </p>
            <Button variant="outline" onClick={() => navigate('switch')}>
              Open model controls
            </Button>
            <p className="footnote">
              This is the last recorded attempt. Current permission is checked
              separately in model controls.
            </p>
          </section>
        )}
        <section className="panel optimizer-memory">
          <div className="panel-heading">
            <div>
              <div className="eyebrow">MEMORY & CACHE</div>
              <h2>Leave room for the next model.</h2>
            </div>
          </div>
          <div className="optimizer-memory-stats">
            <div>
              <span>Available now</span>
              <strong>{num(data?.memory?.availableGB, 1)} GB</strong>
              <small>Free + inactive pages</small>
            </div>
            <div>
              <span>File cache</span>
              <strong>{num(data?.memory?.cachedFilesGB, 1)} GB</strong>
              <small>Overlaps reclaimable memory</small>
            </div>
            <div>
              <span>After unloading provider</span>
              <strong>{num(budgets[0]?.afterUnloadGB, 1)} GB</strong>
              <small>Estimated available memory</small>
            </div>
            <div>
              <span>Largest selected requirement</span>
              <strong>
                {budgets.length
                  ? num(Math.max(...budgets.map((b) => b.requiredGB)), 1)
                  : '—'}{' '}
                GB
              </strong>
              <small>Weights + load headroom + OS reserve</small>
            </div>
          </div>
          <p className="footnote">
            The optimizer uses the provider’s free + inactive memory calculation
            and estimates space reclaimed from its model and GPU cache. It waits
            if a target cannot fit. Darkbloom performs the final admission
            check; custom provider reserves may be stricter.
          </p>
          <div className="optimizer-purge">
            <div>
              <strong>Automatic cache recovery</strong>
              <p className="footnote">
                If a new model is cold and blocked by file-cache pressure,
                Bloomkeeper clears the macOS cache before retrying warm-up. This
                needs a one-time authorization on the Mac. Cleanup runs at most
                once per provider session and once every ten minutes; healthy
                models are left alone. Your manual{' '}
                <code>sudo /usr/sbin/purge</code> fallback remains available.
                Bloomkeeper never collects your administrator password.
              </p>
              {data?.memory?.cacheRecovery?.detail && (
                <p className="footnote" role="status">
                  {data.memory.cacheRecovery.detail}
                </p>
              )}
            </div>
            <Button
              variant="outline"
              disabled={!data?.canManage || data?.remote}
              onClick={async () => {
                try {
                  await navigator.clipboard.writeText('sudo /usr/sbin/purge');
                  setPurgeCopied(true);
                } catch {
                  setError(
                    'Select and copy the displayed purge command, then run it in Terminal.',
                  );
                }
              }}
            >
              {purgeCopied ? 'Command copied' : 'Copy purge command'}
            </Button>
          </div>
        </section>
      </AppScreen>
      <AppScreen name="diagnostics" continuation>
        <section className="panel">
          <div className="panel-heading">
            <div>
              <div className="eyebrow">ACTIVITY LOG</div>
              <h2>Every change, explained.</h2>
            </div>
          </div>
          {data?.events.length ? (
            <ol className="optimizer-events">
              {data.events.map((event, i) => (
                <li key={`${event.at}-${i}`}>
                  <time>{stamp(event.at)}</time>
                  <span>{event.detail}</span>
                  {event.downtime > 0 && (
                    <small>{num(event.downtime, 0)}s switching</small>
                  )}
                </li>
              ))}
            </ol>
          ) : (
            <p className="footnote">
              No automated changes yet. Observe mode records evidence while your
              current model keeps running.
            </p>
          )}
          <p className="footnote">
            History belongs to this Mac and account.{' '}
            {managed
              ? 'A model you start yourself becomes the manager’s pinned pick; automation stays on.'
              : 'An account change, an unsupported service configuration, or a manual model change pauses automation.'}{' '}
            Historical earnings without verified model run time are not used to
            rank models.
          </p>
        </section>
      </AppScreen>
    </div>
  );
}
