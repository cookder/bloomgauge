'use client';
import { useEffect, useId, useRef, useState } from 'react';
import { Check, Laptop } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { startChartPolling } from '@/lib/chart-polling';
import { usePageVisible } from '@/lib/use-page-visibility';
import {
  CHANNELS,
  KINDS,
  KIND_LABELS,
  LIMITS,
  applyChanges,
  channelsText,
  checkAmount,
  checkClock,
  checkMinutes,
  earningsHint,
  kindDetail,
  macStatus,
  readNotifications,
  settingsSummary,
  type Channel,
  type Kind,
  type NotificationsView,
  type SettingsChange,
} from '@/lib/notification-settings';
import { useScreenActive } from './app-navigation';
import { PhoneNotifications } from './demand-alerts';

type Field = 'usdPerHour' | 'minutes' | 'start' | 'end';
// Where a refused save's message shows: next to what was changed.
type Area = 'kinds' | 'earnings' | 'quiet' | 'other';
type Pending = { change: SettingsChange };
type Timer = { id: ReturnType<typeof setTimeout>; save: () => void };

/** The Mac runs a BloomGauge from before notification settings (no route). */
class OldBackend extends Error {}

// Layout only. Inside the evidence-row grid: a full-width line, and toggles that
// keep their height beside a title that wraps. Time fields size to their text (the
// shared number-field width clips "10:00 PM").
const wide = { gridColumn: '1 / -1' } as const;
const top = { alignSelf: 'start' } as const;
const fit = { width: 'auto' } as const;

async function fetchNotifications(
  signal: AbortSignal,
): Promise<NotificationsView> {
  const response = await fetch('/api/notifications', {
    signal,
    cache: 'no-store',
  });
  if (response.status === 404)
    throw new OldBackend('Update BloomGauge for notification settings.');
  if (!response.ok) throw new Error('Waiting for the dashboard connection.');
  const value = readNotifications(await response.json());
  if (!value)
    throw new Error(
      'Notification settings could not be read. Update BloomGauge.',
    );
  return value;
}

async function send(body: object): Promise<NotificationsView> {
  const response = await fetch('/api/notifications', {
    method: 'POST',
    cache: 'no-store',
    signal: AbortSignal.timeout(15000),
    headers: {
      'Content-Type': 'application/json',
      'X-Bloom-Action': 'notifications',
    },
    body: JSON.stringify(body),
  });
  const value: unknown = await response.json().catch(() => null);
  if (!response.ok)
    throw new Error(
      value &&
        typeof value === 'object' &&
        'error' in value &&
        typeof value.error === 'string'
        ? value.error
        : 'Could not save this. Try again.',
    );
  const view = readNotifications(value);
  if (!view)
    throw new Error(
      'Notification settings could not be read. Update BloomGauge.',
    );
  return view;
}

const areaOf = (change: SettingsChange): Area =>
  change.kinds
    ? 'kinds'
    : change.earnings
      ? 'earnings'
      : change.quietHours
        ? 'quiet'
        : 'other';

const failure = (e: unknown, fallback: string) =>
  e instanceof Error && e.name !== 'TimeoutError' ? e.message : fallback;

const when = (at: number) => {
  const date = new Date(at * 1000);
  const today = date.toDateString() === new Date().toDateString();
  return date.toLocaleString(
    [],
    today
      ? { hour: 'numeric', minute: '2-digit' }
      : { weekday: 'short', hour: 'numeric', minute: '2-digit' },
  );
};

/** Optimizer → Overview: which alerts reach the Mac and the phone, the earnings
 * threshold, quiet hours, a Mac test and this device's phone subscription. Every
 * change saves at once (number and time fields after a pause) and shows before the
 * Mac confirms it; a refused save reverts and says why. */
export function NotificationSettings({
  remote: remoteProp = false,
}: {
  remote?: boolean;
}) {
  const active = useScreenActive();
  const visible = usePageVisible();
  const id = useId();
  const [data, setData] = useState<NotificationsView | null>(null);
  // The settings reply says whether this is the phone; the optimizer's flag covers older replies.
  const remote = remoteProp || !!data?.remote;
  const [legacy, setLegacy] = useState(false);
  const [readError, setReadError] = useState('');
  const [saveError, setSaveError] = useState<{
    area: Area;
    text: string;
  } | null>(null);
  const [pending, setPending] = useState<readonly Pending[]>([]);
  const [drafts, setDrafts] = useState<Partial<Record<Field, string>>>({});
  const [testing, setTesting] = useState(false);
  const [macNote, setMacNote] = useState('');
  const [macError, setMacError] = useState('');
  const queue = useRef<readonly Pending[]>([]);
  const chain = useRef<Promise<void>>(Promise.resolve());
  // Bumped when a write starts or ends: a read that overlapped one is dropped.
  const revision = useRef(0);
  const timers = useRef<Partial<Record<Field, Timer>>>({});
  // One 404 can be a passing hiccup (a restart during an update): the old card needs two.
  const missing = useRef(0);

  useEffect(() => {
    if (!active || !visible || legacy) return;
    return startChartPolling<{ value: NotificationsView; revision: number }>({
      load: async (signal) => {
        const at = revision.current;
        return { value: await fetchNotifications(signal), revision: at };
      },
      intervalMs: 30000,
      onValue: ({ value, revision: at }) => {
        missing.current = 0;
        if (at !== revision.current || queue.current.length) return;
        setData(value);
        setReadError('');
      },
      onError: (e) => {
        if (e instanceof OldBackend && ++missing.current >= 2) setLegacy(true);
        else setReadError(e.message);
      },
    });
  }, [active, visible, legacy]);

  // A field still waiting for its pause is saved rather than lost: when the card
  // closes, and when the page is hidden or unloaded (iOS may end a hidden app).
  useEffect(() => {
    const waiting = timers.current;
    const flush = () => {
      for (const field of Object.keys(waiting) as Field[]) {
        const timer = waiting[field]!;
        clearTimeout(timer.id);
        delete waiting[field];
        timer.save();
      }
    };
    const hidden = () => {
      if (document.hidden) flush();
    };
    window.addEventListener('pagehide', flush);
    document.addEventListener('visibilitychange', hidden);
    return () => {
      window.removeEventListener('pagehide', flush);
      document.removeEventListener('visibilitychange', hidden);
      flush();
    };
  }, []);

  const settings = data
    ? applyChanges(
        data.settings,
        pending.map((p) => p.change),
      )
    : null;
  const limits = data?.limits ?? LIMITS;

  // Writes go one at a time, so each answer is the Mac's newest state.
  function run(task: () => Promise<void>) {
    revision.current++;
    chain.current = chain.current
      .then(async () => {
        try {
          await task();
        } finally {
          revision.current++;
        }
      })
      .catch(() => undefined);
  }
  function save(change: SettingsChange) {
    const item: Pending = { change };
    queue.current = [...queue.current, item];
    setPending(queue.current);
    setSaveError(null);
    run(async () => {
      try {
        setData(await send({ action: 'save', settings: change }));
      } catch (e) {
        setSaveError({
          area: areaOf(change),
          text: failure(e, 'The setting could not be confirmed. Try again.'),
        });
      } finally {
        queue.current = queue.current.filter((p) => p !== item);
        setPending(queue.current);
      }
    });
  }
  function testMac() {
    setTesting(true);
    setMacNote('');
    setMacError('');
    run(async () => {
      try {
        setData(await send({ action: 'test', channel: 'mac' }));
        setMacNote('Test sent. It shows on this Mac within about 15 seconds.');
      } catch (e) {
        setMacError(failure(e, 'The test could not be confirmed. Try again.'));
      } finally {
        setTesting(false);
      }
    });
  }

  function schedule(field: Field, change: SettingsChange | null) {
    clearTimeout(timers.current[field]?.id);
    delete timers.current[field];
    if (!change) return;
    timers.current[field] = {
      save: () => save(change),
      id: setTimeout(() => {
        delete timers.current[field];
        save(change);
      }, 600),
    };
  }
  function check(field: Field, text: string) {
    return field === 'usdPerHour'
      ? checkAmount(text, limits.usdPerHour)
      : field === 'minutes'
        ? checkMinutes(text, limits.minutes)
        : checkClock(text);
  }
  function edit(field: Field, text: string) {
    if (!settings) return;
    setDrafts((d) => ({ ...d, [field]: text }));
    const checked = check(field, text);
    const saved =
      field === 'usdPerHour' || field === 'minutes'
        ? settings.earnings[field]
        : settings.quietHours[field];
    // Equal start and end would switch quiet hours off without saying so.
    const other =
      field === 'start'
        ? settings.quietHours.end
        : field === 'end'
          ? settings.quietHours.start
          : null;
    schedule(
      field,
      checked.ok && checked.value !== saved && checked.value !== other
        ? field === 'usdPerHour' || field === 'minutes'
          ? { earnings: { [field]: Number(checked.value) } }
          : { quietHours: { [field]: String(checked.value) } }
        : null,
    );
  }
  // Leaving a field saves it now; a valid value then shows as saved.
  function done(field: Field) {
    const timer = timers.current[field];
    if (timer) {
      clearTimeout(timer.id);
      delete timers.current[field];
      timer.save();
    }
    setDrafts((d) => {
      const text = d[field];
      if (text == null || !check(field, text).ok) return d;
      const next = { ...d };
      delete next[field];
      return next;
    });
  }
  function fieldError(...fields: Field[]) {
    for (const field of fields) {
      const text = drafts[field];
      const checked = text == null ? null : check(field, text);
      if (checked && !checked.ok) return checked.error;
    }
    return '';
  }
  function toggle(kind: Kind, channel: Channel) {
    if (settings)
      save({
        kinds: { [kind]: { [channel]: !settings.kinds[kind][channel] } },
      });
  }

  const failed = (area: Area) =>
    saveError?.area === area ? saveError.text : '';
  const earningsError =
    fieldError('usdPerHour', 'minutes') || failed('earnings');
  const quietError =
    fieldError('start', 'end') ||
    failed('quiet') ||
    (settings &&
    (drafts.start ?? settings.quietHours.start) ===
      (drafts.end ?? settings.quietHours.end)
      ? 'Pick different start and end times.'
      : '');
  const hint = earningsHint(data?.earningsNow ?? null);
  const mac = data ? macStatus(data.mac, remote) : null;
  return (
    <details className="quiet-disclosure manager-evidence notification-settings">
      <summary>
        <span>Notifications</span>
        <small>
          {legacy
            ? 'Model switches'
            : pending.length
              ? 'Saving…'
              : settings
                ? settingsSummary(settings)
                : ''}
        </small>
      </summary>
      {!legacy && readError && (
        <p className="notice" role="status">
          {data ? 'Notification settings are not live. ' : ''}
          {readError}
        </p>
      )}
      {!legacy && !data && !readError && (
        <p className="small muted" role="status">
          Reading notification settings…
        </p>
      )}
      {!legacy && data && settings && mac && (
        <>
          <p className="small muted">
            Choose which alerts reach your Mac and your phone.
          </p>
          <ol className="manager-evidence-rows" aria-label="Alerts">
            {KINDS.map((kind) => (
              <li key={kind}>
                <div className="manager-evidence-model">
                  <strong id={`${id}-${kind}`}>{KIND_LABELS[kind]}</strong>
                </div>
                <div
                  className="optimizer-plan-models"
                  role="group"
                  aria-labelledby={`${id}-${kind}`}
                  style={top}
                >
                  {CHANNELS.map((channel) => {
                    const on = settings.kinds[kind][channel];
                    return (
                      <button
                        key={channel}
                        type="button"
                        aria-pressed={on}
                        aria-label={`${KIND_LABELS[kind]} on ${channel === 'mac' ? 'the Mac' : 'the phone'}`}
                        onClick={() => toggle(kind, channel)}
                      >
                        {on && <Check size={13} aria-hidden="true" />}
                        {channel === 'mac' ? 'Mac' : 'Phone'}
                      </button>
                    );
                  })}
                </div>
                <small className="manager-evidence-why">
                  {kindDetail(kind, settings)}
                </small>
                {kind === 'earningsHigh' && (
                  <>
                    <div className="optimizer-quick-actions" style={wide}>
                      <label className="tune-input">
                        <input
                          type="number"
                          inputMode="decimal"
                          min={limits.usdPerHour.min}
                          max={limits.usdPerHour.max}
                          step={limits.usdPerHour.step}
                          aria-label="Alert at this many dollars an hour"
                          aria-invalid={!!fieldError('usdPerHour')}
                          value={
                            drafts.usdPerHour ??
                            settings.earnings.usdPerHour.toFixed(2)
                          }
                          onChange={(e) => edit('usdPerHour', e.target.value)}
                          onBlur={() => done('usdPerHour')}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter') done('usdPerHour');
                          }}
                        />
                        <span>$/h</span>
                      </label>
                      <label className="tune-input">
                        <input
                          type="number"
                          inputMode="numeric"
                          min={limits.minutes.min}
                          max={limits.minutes.max}
                          step={limits.minutes.step}
                          aria-label="For this many minutes"
                          aria-invalid={!!fieldError('minutes')}
                          value={
                            drafts.minutes ?? String(settings.earnings.minutes)
                          }
                          onChange={(e) => edit('minutes', e.target.value)}
                          onBlur={() => done('minutes')}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter') done('minutes');
                          }}
                        />
                        <span>min</span>
                      </label>
                      {earningsError && (
                        <span className="range-error" role="alert">
                          {earningsError}
                        </span>
                      )}
                    </div>
                    {hint && (
                      <small className="manager-evidence-why">{hint}</small>
                    )}
                  </>
                )}
              </li>
            ))}
          </ol>
          {data.phones === 0 &&
            KINDS.some((kind) => settings.kinds[kind].phone) && (
              <p className="small muted">
                No phone is set up yet. To get Phone alerts, enable
                notifications on your phone under This phone or browser.
              </p>
            )}
          {failed('kinds') && (
            <p className="notice" role="alert">
              {failed('kinds')}
            </p>
          )}
          <label className="manager-excursions">
            <input
              type="checkbox"
              role="switch"
              aria-checked={settings.quietHours.enabled}
              checked={settings.quietHours.enabled}
              onChange={(e) =>
                save({ quietHours: { enabled: e.target.checked } })
              }
            />
            <span>
              <strong>Quiet hours</strong>
              <small>Alerts wait until quiet hours end, except problems.</small>
            </span>
          </label>
          {settings.quietHours.enabled && (
            <div className="optimizer-quick-actions">
              <div className="tune-input">
                <input
                  type="time"
                  aria-label="Quiet hours start"
                  aria-invalid={!!fieldError('start')}
                  style={fit}
                  value={drafts.start ?? settings.quietHours.start}
                  onChange={(e) => edit('start', e.target.value)}
                  onBlur={() => done('start')}
                />
                <span>to</span>
                <input
                  type="time"
                  aria-label="Quiet hours end"
                  aria-invalid={!!fieldError('end')}
                  style={fit}
                  value={drafts.end ?? settings.quietHours.end}
                  onChange={(e) => edit('end', e.target.value)}
                  onBlur={() => done('end')}
                />
              </div>
              {quietError && (
                <span className="range-error" role="alert">
                  {quietError}
                </span>
              )}
            </div>
          )}
          <label className="manager-excursions">
            <input
              type="checkbox"
              role="switch"
              aria-checked={settings.phoneAmounts}
              checked={settings.phoneAmounts}
              // The Mac refuses turning this on from the phone; off works anywhere.
              disabled={remote && !settings.phoneAmounts}
              onChange={(e) => save({ phoneAmounts: e.target.checked })}
            />
            <span>
              <strong>Show amounts in phone notifications</strong>
              <small>
                Phone notifications are encrypted on their way through Apple’s
                or Google’s push service. Turn this off to keep your earnings
                off the phone’s lock screen; alerts then say “well above usual”
                instead. New-model alerts can still show public network
                estimates.
                {remote && !settings.phoneAmounts && ' Turn on from the Mac.'}
              </small>
            </span>
          </label>
          {failed('other') && (
            <p className="notice" role="alert">
              {failed('other')}
            </p>
          )}
          <div className="demand-notifications">
            <div>
              <strong>
                <Laptop size={15} /> {remote ? 'Your Mac' : 'This Mac'}
              </strong>
              <p className={mac.problem ? 'notice' : 'small muted'}>
                {mac.text}
              </p>
              {data.mac.available && data.mac.lastDeliveredAt != null && (
                <p className="small muted">
                  Last shown: {when(data.mac.lastDeliveredAt)}.
                </p>
              )}
              {remote && (
                <p className="small muted">
                  Send the Mac test from BloomGauge on the Mac.
                </p>
              )}
              {macNote && (
                <p className="small muted" role="status">
                  {macNote}
                </p>
              )}
              {macError && (
                <p className="notice" role="alert">
                  {macError}
                </p>
              )}
            </div>
            <Button
              type="button"
              variant="outline"
              disabled={remote || testing || !data.mac.available}
              onClick={testMac}
            >
              {testing
                ? 'Sending…'
                : remote
                  ? 'Send a test from the Mac'
                  : 'Send a test to this Mac'}
            </Button>
          </div>
        </>
      )}
      <PhoneNotifications legacy={legacy} />
      {!legacy && data && (
        <details className="demand-alert-history">
          <summary>
            Recent alerts{data.recent.length ? ` · ${data.recent.length}` : ''}
          </summary>
          {data.recent.length ? (
            data.recent.map((alert, i) => {
              const channels = channelsText(alert.channels);
              return (
                <p className="small muted" key={`${alert.at}-${i}`}>
                  <strong>{alert.title}</strong> · {when(alert.at)}
                  {channels ? ` · ${channels}` : ''}
                </p>
              );
            })
          ) : (
            <p className="small muted">None yet.</p>
          )}
        </details>
      )}
    </details>
  );
}
