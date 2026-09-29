// Notification settings (native/notify_settings.py, GET/POST /api/notifications): which
// alerts reach the Mac and the phone, the "earnings running high" threshold, quiet hours
// and amounts on the phone. Pure shaping and checks; the card is
// components/dashboard/notification-settings.tsx.
import { usd } from './optimizer-manager';

export const KINDS = [
  'earningsHigh',
  'switchConsidered',
  'modelSwitched',
  'problems',
  'demandSpike',
  'networkNews',
] as const;
export type Kind = (typeof KINDS)[number];
export const CHANNELS = ['mac', 'phone'] as const;
export type Channel = (typeof CHANNELS)[number];

export type Settings = {
  kinds: Record<Kind, Record<Channel, boolean>>;
  earnings: { usdPerHour: number; minutes: number };
  quietHours: { enabled: boolean; start: string; end: string };
  phoneAmounts: boolean;
};
/** A save sends only the part that changed; the server merges it. */
export type SettingsChange = {
  kinds?: Partial<Record<Kind, Partial<Record<Channel, boolean>>>>;
  earnings?: Partial<Settings['earnings']>;
  quietHours?: Partial<Settings['quietHours']>;
  phoneAmounts?: boolean;
};
export type Limit = { min: number; max: number; step: number };
export type Limits = { usdPerHour: Limit; minutes: Limit };
export type MacStatus = {
  /** The Mac app's notification relay asked for alerts in the last 2 minutes. */
  available: boolean;
  permission: 'granted' | 'denied' | 'notDetermined' | 'unknown';
  lastPolledAt: number | null;
  lastDeliveredAt: number | null;
  pending: number;
};
export type RecentAlert = {
  kind: string;
  at: number;
  title: string;
  channels: Channel[];
};
export type EarningsNow = {
  usdPerHour: number | null;
  minutes: number;
  coverage: number;
};
export type NotificationsView = {
  settings: Settings;
  limits: Limits;
  mac: MacStatus;
  earningsNow: EarningsNow | null;
  recent: RecentAlert[];
  /** Phones (and browsers) subscribed for this account; null when unknown. */
  phones: number | null;
  /** Viewed from the phone (the reply says so; older replies leave it out). */
  remote: boolean;
};

/** The contract's limits, for a response that leaves them out. */
export const LIMITS: Limits = {
  usdPerHour: { min: 0.01, max: 10, step: 0.01 },
  minutes: { min: 10, max: 240, step: 5 },
};

export const KIND_LABELS: Record<Kind, string> = {
  earningsHigh: 'Earnings running high',
  switchConsidered: 'Manager considering a switch',
  modelSwitched: 'Model switched',
  problems: 'Problems',
  demandSpike: 'Demand spikes',
  networkNews: 'New models on the network',
};

const record = (v: unknown): v is Record<string, unknown> =>
  !!v && typeof v === 'object' && !Array.isArray(v);
const finite = (v: unknown): v is number =>
  typeof v === 'number' && Number.isFinite(v);
const time = (v: unknown): number | null => (finite(v) ? v : null);
const clock = /^([01]\d|2[0-3]):([0-5]\d)$/;
const permissions: readonly string[] = [
  'granted',
  'denied',
  'notDetermined',
  'unknown',
];

function readSettings(v: unknown): Settings | null {
  if (!record(v)) return null;
  const { kinds, earnings, quietHours, phoneAmounts } = v;
  if (!record(kinds) || !record(earnings) || !record(quietHours)) return null;
  const out = {} as Settings['kinds'];
  for (const kind of KINDS) {
    const channels = kinds[kind];
    if (
      !record(channels) ||
      typeof channels.mac !== 'boolean' ||
      typeof channels.phone !== 'boolean'
    )
      return null;
    out[kind] = { mac: channels.mac, phone: channels.phone };
  }
  if (
    !finite(earnings.usdPerHour) ||
    !finite(earnings.minutes) ||
    typeof quietHours.enabled !== 'boolean' ||
    typeof quietHours.start !== 'string' ||
    typeof quietHours.end !== 'string' ||
    typeof phoneAmounts !== 'boolean'
  )
    return null;
  return {
    kinds: out,
    earnings: { usdPerHour: earnings.usdPerHour, minutes: earnings.minutes },
    quietHours: {
      enabled: quietHours.enabled,
      start: quietHours.start,
      end: quietHours.end,
    },
    phoneAmounts,
  };
}

function readLimit(v: unknown, fallback: Limit): Limit {
  return record(v) &&
    finite(v.min) &&
    finite(v.max) &&
    finite(v.step) &&
    v.min <= v.max &&
    v.step > 0
    ? { min: v.min, max: v.max, step: v.step }
    : fallback;
}

/** The GET/POST response, keeping only what the card uses; null when the
 * settings or the Mac status can't be read (the card shows an update hint). */
export function readNotifications(v: unknown): NotificationsView | null {
  if (!record(v)) return null;
  const settings = readSettings(v.settings);
  const { mac, earningsNow: now } = v;
  if (!settings || !record(mac) || typeof mac.available !== 'boolean')
    return null;
  const limits = record(v.limits) ? v.limits : {};
  const phone = record(v.phone) ? v.phone : {};
  return {
    settings,
    limits: {
      usdPerHour: readLimit(limits.usdPerHour, LIMITS.usdPerHour),
      minutes: readLimit(limits.minutes, LIMITS.minutes),
    },
    mac: {
      available: mac.available,
      permission:
        typeof mac.permission === 'string' &&
        permissions.includes(mac.permission)
          ? (mac.permission as MacStatus['permission'])
          : 'unknown',
      lastPolledAt: time(mac.lastPolledAt),
      lastDeliveredAt: time(mac.lastDeliveredAt),
      pending: finite(mac.pending) ? mac.pending : 0,
    },
    earningsNow:
      record(now) &&
      (now.usdPerHour === null || finite(now.usdPerHour)) &&
      finite(now.minutes) &&
      finite(now.coverage)
        ? {
            usdPerHour: now.usdPerHour as number | null,
            minutes: now.minutes,
            coverage: now.coverage,
          }
        : null,
    recent: (Array.isArray(v.recent) ? v.recent : [])
      .filter(
        (a): a is RecentAlert =>
          record(a) &&
          typeof a.kind === 'string' &&
          finite(a.at) &&
          typeof a.title === 'string' &&
          Array.isArray(a.channels),
      )
      .slice(0, 10)
      .map((a) => ({
        kind: a.kind,
        at: a.at,
        title: a.title,
        // A channel added later (email) is left out rather than dropping the alert.
        channels: CHANNELS.filter((c) => a.channels.includes(c)),
      })),
    phones: finite(phone.subscriptionCount) ? phone.subscriptionCount : null,
    remote: v.remote === true,
  };
}

const defined = <T extends object>(v: T | undefined) =>
  Object.fromEntries(
    Object.entries(v ?? {}).filter(([, value]) => value !== undefined),
  ) as Partial<T>;

/** `settings` with a partial save applied, as the server merges it. */
export function mergeSettings(
  settings: Settings,
  change: SettingsChange,
): Settings {
  const kinds = { ...settings.kinds };
  for (const kind of KINDS) {
    const channels = change.kinds?.[kind];
    if (channels) kinds[kind] = { ...kinds[kind], ...defined(channels) };
  }
  return {
    kinds,
    earnings: { ...settings.earnings, ...defined(change.earnings) },
    quietHours: { ...settings.quietHours, ...defined(change.quietHours) },
    phoneAmounts: change.phoneAmounts ?? settings.phoneAmounts,
  };
}

/** Saved settings with the saves still on their way applied in order. */
export const applyChanges = (
  settings: Settings,
  changes: readonly SettingsChange[],
) => changes.reduce(mergeSettings, settings);

export type Checked<T> = { ok: true; value: T } | { ok: false; error: string };

const onStep = (value: number, step: number) =>
  Math.abs(value / step - Math.round(value / step)) < 1e-6;
const decimals = (step: number) =>
  Math.max(0, Math.ceil(-Math.log10(step) - 1e-9));

/** An hourly amount typed as "0.30" (a number field's text). */
export function checkAmount(text: string, limit: Limit): Checked<number> {
  const places = decimals(limit.step);
  const error = `Choose an amount from $${limit.min.toFixed(places)} to $${limit.max.toFixed(places)} an hour.`;
  const value = text.trim() === '' ? NaN : Number(text);
  if (!Number.isFinite(value) || value < limit.min || value > limit.max)
    return { ok: false, error };
  if (!onStep(value, limit.step))
    return {
      ok: false,
      error: `Use at most ${places} decimals, like ${(0.3).toFixed(places)}.`,
    };
  return { ok: true, value: Number(value.toFixed(places)) };
}

/** Whole minutes in the allowed range and steps. */
export function checkMinutes(text: string, limit: Limit): Checked<number> {
  const value = text.trim() === '' ? NaN : Number(text);
  if (
    !Number.isInteger(value) ||
    value < limit.min ||
    value > limit.max ||
    !onStep(value, limit.step)
  )
    return {
      ok: false,
      error: `Choose ${limit.min} to ${limit.max} minutes, in steps of ${limit.step}.`,
    };
  return { ok: true, value };
}

/** A time field's "HH:MM" (some browsers add ":00" seconds). */
export function checkClock(text: string): Checked<string> {
  const value =
    text.length === 8 && text.endsWith(':00') ? text.slice(0, 5) : text;
  return clock.test(value)
    ? { ok: true, value }
    : { ok: false, error: 'Use a time like 22:00.' };
}

/** "30 minutes", "1 hour", "90 minutes", "4 hours". */
export const minutesText = (minutes: number) =>
  minutes % 60 === 0
    ? `${minutes / 60} hour${minutes === 60 ? '' : 's'}`
    : `${minutes} minutes`;

/** The one-line explanation under each alert. */
export function kindDetail(kind: Kind, settings: Settings): string {
  switch (kind) {
    case 'earningsHigh':
      return `Your pace stays at or above ${usd(settings.earnings.usdPerHour)}/h for ${minutesText(settings.earnings.minutes)}.`;
    case 'switchConsidered':
      return 'It starts watching a better-paying model, and when it switches to it.';
    case 'modelSwitched':
      return 'The Mac changed models, and why.';
    case 'problems':
      return 'No work arriving, or a model needed recovery. Sent even in quiet hours.';
    case 'demandSpike':
      return 'A model gets much busier than usual on the network.';
    case 'networkNews':
      return 'A new model joins the network that could pay well here.';
  }
}

/** "Last 30 min: $0.21/h", under the earnings threshold; null without a reading. */
export function earningsHint(now: EarningsNow | null): string | null {
  if (!now || now.usdPerHour == null) return null;
  const measured = Math.round(
    now.minutes * Math.min(1, Math.max(0, now.coverage)),
  );
  return `Last ${now.minutes} min: ${usd(now.usdPerHour)}/h${
    measured > 0 && now.coverage < 0.9 ? `, from ${measured} min of data` : ''
  }`;
}

/** The line next to the card's title. */
export function settingsSummary(settings: Settings): string {
  const on = KINDS.filter(
    (k) => settings.kinds[k].mac || settings.kinds[k].phone,
  ).length;
  if (!on) return 'All off';
  const mac = KINDS.some((k) => settings.kinds[k].mac);
  const phone = KINDS.some((k) => settings.kinds[k].phone);
  const quiet = settings.quietHours;
  return [
    `${on} of ${KINDS.length} on`,
    mac && phone ? 'Mac and phone' : mac ? 'Mac only' : 'Phone only',
    quiet.enabled && quiet.start !== quiet.end
      ? `quiet ${quiet.start}–${quiet.end}`
      : '',
  ]
    .filter(Boolean)
    .join(' · ');
}

/** Where a recent alert went: "Mac and phone", "Mac", "Phone" or "". */
export function channelsText(channels: readonly Channel[]): string {
  const mac = channels.includes('mac');
  const phone = channels.includes('phone');
  return mac && phone ? 'Mac and phone' : mac ? 'Mac' : phone ? 'Phone' : '';
}

/** The Mac's delivery state in plain words. `remote`: viewed from the phone. */
export function macStatus(
  mac: MacStatus,
  remote: boolean,
): { text: string; problem: boolean } {
  const where = remote ? 'your Mac' : 'this Mac';
  if (!mac.available)
    return {
      text: `Mac notifications start when BloomGauge is open on ${where}.`,
      problem: false,
    };
  if (mac.permission === 'denied')
    return {
      text: 'Mac notifications are off in System Settings → Notifications → BloomGauge.',
      problem: true,
    };
  if (mac.permission === 'notDetermined')
    return {
      text: `The first alert asks you to allow BloomGauge notifications on ${where}.`,
      problem: false,
    };
  return {
    text: `Alerts show in Notification Center on ${where}, with amounts.`,
    problem: false,
  };
}
