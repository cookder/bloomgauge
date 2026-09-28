// Network news (native/model_catalog_watch.py, GET/POST /api/network/news): models
// joining or leaving Darkbloom and big swings in how many Macs keep a model warm,
// from the public data the app already fetches. Pure shaping; the panel is
// components/dashboard/network-news.tsx.
import { modelLabel } from './model-label';
import { usd } from './optimizer-manager';

export type NewsKind = 'new' | 'left' | 'collapse' | 'surge';
const kinds: readonly string[] = ['new', 'left', 'collapse', 'surge'];

export type NewsItem = {
  at: number;
  kind: NewsKind;
  model: string;
  /** A dropped model this Mac still offers, or has downloaded. */
  here: 'offered' | 'downloaded' | null;
  /** A new model still on the capacity list (counts and $/h are live). */
  current: boolean;
  macs?: number | null;
  warm?: number | null;
  serving?: number | null;
  demand?: number | null;
  returned?: boolean;
  before?: number | null;
  after?: number | null;
  usdPerHour?: number | null;
  low?: number | null;
  high?: number | null;
  providers?: number | null;
  source?: string | null;
};

export type NewsPush = {
  enabled: boolean;
  mutedUntil: number | null;
  lastSentAt: number | null;
  perDay: number;
  available: boolean;
};

export type NetworkNews = {
  schema: 1;
  at: number;
  watchingSince: number | null;
  items: NewsItem[];
  push: NewsPush;
};

const finite = (v: unknown): v is number =>
  typeof v === 'number' && Number.isFinite(v);
const optionalNumber = (v: unknown) => v == null || finite(v);
const time = (v: unknown) => v === null || finite(v);

function validItem(v: unknown): v is NewsItem {
  if (!v || typeof v !== 'object' || Array.isArray(v)) return false;
  const r = v as Record<string, unknown>;
  return (
    finite(r.at) &&
    typeof r.kind === 'string' &&
    kinds.includes(r.kind) &&
    typeof r.model === 'string' &&
    r.model.length > 0 &&
    r.model.length <= 200 &&
    (r.here === null || r.here === 'offered' || r.here === 'downloaded') &&
    typeof r.current === 'boolean' &&
    [
      'macs',
      'warm',
      'serving',
      'demand',
      'before',
      'after',
      'usdPerHour',
      'low',
      'high',
      'providers',
    ].every((k) => optionalNumber(r[k])) &&
    (r.returned == null || typeof r.returned === 'boolean') &&
    (r.source == null || typeof r.source === 'string')
  );
}

export function validNetworkNews(v: unknown): v is NetworkNews {
  if (!v || typeof v !== 'object' || Array.isArray(v)) return false;
  const r = v as Record<string, unknown>;
  const push = r.push as Record<string, unknown> | null;
  return (
    r.schema === 1 &&
    finite(r.at) &&
    time(r.watchingSince) &&
    Array.isArray(r.items) &&
    r.items.length <= 50 &&
    r.items.every(validItem) &&
    !!push &&
    typeof push === 'object' &&
    typeof push.enabled === 'boolean' &&
    time(push.mutedUntil) &&
    time(push.lastSentAt) &&
    finite(push.perDay) &&
    typeof push.available === 'boolean'
  );
}

/** Offered models Darkbloom's catalog doesn't list: the network sends them no work.
 * (The optimizer's model list is the catalog; the provider's offer comes from Darkbloom.) */
export function modelsNotInCatalog(
  offered: readonly unknown[] | undefined,
  catalog: readonly { id: string }[] | undefined,
): string[] {
  const listed = new Set((catalog ?? []).map((m) => m.id));
  return (offered ?? []).filter(
    (id): id is string =>
      typeof id === 'string' && id.length > 0 && !listed.has(id),
  );
}

export type NewsLine = {
  key: string;
  kind: NewsKind;
  at: number;
  title: string;
  facts: string[];
  /** A dropped model that is still on this Mac, with the command that removes it. */
  flag: { text: string; command: string } | null;
};

const plural = (n: number, word: string) =>
  `${n.toLocaleString('en-US')} ${word}${n === 1 ? '' : 's'}`;

/** One line per news item, newest first. `offeredNotInCatalog` (from the provider's own
 * report) flags a dropped model even when the backend's copy of the offer is older. */
export function newsLines(
  news: NetworkNews | null,
  options: {
    offeredNotInCatalog?: readonly string[];
    names?: Record<string, string>;
  } = {},
): NewsLine[] {
  if (!news) return [];
  const label = (id: string) => options.names?.[id] || modelLabel(id);
  const offCatalog = new Set(options.offeredNotInCatalog ?? []);
  const newest = new Set<string>();
  return news.items.map((item, i) => {
    const latest = !newest.has(item.model);
    newest.add(item.model);
    const name = label(item.model);
    const facts: string[] = [];
    let title: string;
    let flag: NewsLine['flag'] = null;
    if (item.kind === 'new') {
      title = `${item.returned ? 'Back on the network' : 'New model on the network'}: ${name}`;
      const macs = item.warm ?? item.serving ?? item.macs;
      if (finite(macs)) facts.push(`${plural(macs, 'Mac')} serving`);
      facts.push(
        finite(item.usdPerHour)
          ? `~${usd(item.usdPerHour)}/h on Macs like yours`
          : 'too few Macs like yours on it for a $/h estimate yet',
      );
      if (finite(item.demand) && item.demand > 0)
        facts.push(`${plural(item.demand, 'request')} in flight`);
    } else if (item.kind === 'left') {
      title = `Left Darkbloom’s catalog: ${name}`;
      if (finite(item.macs)) facts.push(`it had ${plural(item.macs, 'Mac')}`);
      const here =
        item.here ?? (latest && offCatalog.has(item.model) ? 'offered' : null);
      const command = `darkbloom models remove ${item.model}`;
      if (here === 'offered')
        flag = {
          text: 'This Mac still offers it, but the network won’t send it work. Remove it, then restart Darkbloom so it stops offering it:',
          command,
        };
      else if (here === 'downloaded')
        flag = {
          text: 'It is still downloaded on this Mac. To free the space:',
          command,
        };
    } else {
      title = `${item.kind === 'collapse' ? 'Fewer' : 'More'} Macs keep ${name} warm`;
      if (finite(item.before) && finite(item.after))
        facts.push(
          `${Math.round(item.before).toLocaleString('en-US')} → ${Math.round(item.after).toLocaleString('en-US')} warm Macs within 15 minutes`,
        );
      facts.push(
        item.kind === 'collapse'
          ? 'fewer Macs competing for its work'
          : 'more Macs competing for its work',
      );
    }
    return {
      key: `${item.kind}-${item.model}-${item.at}-${i}`,
      kind: item.kind,
      at: item.at,
      title,
      facts,
      flag,
    };
  });
}

/** The closed panel's one-line summary. */
export function newsSummary(
  news: NetworkNews | null,
  lines: NewsLine[],
  now: number,
): string {
  if (!news) return 'Not available yet';
  const flagged = lines.filter((l) => l.flag).length;
  if (flagged) return `${plural(flagged, 'dropped model')} still on this Mac`;
  const week = lines.filter((l) => now - l.at <= 7 * 86400).length;
  if (week) return `${plural(week, 'change')} this week`;
  if (lines.length) return `${plural(lines.length, 'change')} this month`;
  return news.watchingSince == null
    ? 'Starting to watch the network'
    : 'No changes';
}

/** What the phone notice setting does now. */
export function pushText(push: NewsPush, now: number): string {
  if (!push.enabled)
    return 'Off. Turn it on to get a phone notice when a new model pays well on Macs like yours (at most one a day).';
  if (push.mutedUntil != null && push.mutedUntil > now)
    return `Muted until ${new Date(push.mutedUntil * 1000).toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' })}.`;
  return 'On: at most one notice a day, only for a new model that earns at least as much as yours on Macs like this one.';
}
