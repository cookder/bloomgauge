export type BloomTone = 'green' | 'yellow' | 'red' | 'neutral';
export type BloomHealthSnapshot = {
  at: number;
  earnings?: {
    status: string;
    updatedAt: number | null;
    error?: string | null;
  };
  provider?: {
    online: boolean;
    tracking?: { counting?: boolean; detail?: string };
  };
  pulse?: { status: string };
};
export type BloomHealth = { tone: BloomTone; detail: string; urgent?: boolean };
export type BloomStatusState = {
  tone: BloomTone;
  candidate: BloomTone;
  since: number;
  settled?: boolean;
};

/** Presentation only. Never feed this grace period into model/control guards. */
export function bloomHealth(
  data: BloomHealthSnapshot | null,
  now: number,
  startedAt: number,
  error: string,
  paused: boolean,
  visible: boolean,
): BloomHealth {
  if (paused || !visible)
    return {
      tone: 'neutral',
      detail: paused
        ? 'This view is paused. Collection and the optimizer continue independently.'
        : 'This view is in the background. It will refresh when you return.',
    };
  if (!data)
    return {
      tone: now - startedAt >= 60 ? 'red' : 'yellow',
      detail:
        error ||
        'Connecting to this Mac. Waiting for the first complete reading.',
    };
  const age = now - data.at;
  if (!Number.isFinite(age) || age < -5)
    return {
      tone: 'red',
      urgent: true,
      detail:
        'The reading has an invalid time or is ahead of this device’s clock. Check the date and time on this device and the Mac.',
    };
  if (age >= 60)
    return {
      tone: 'red',
      urgent: true,
      detail:
        error ||
        'No fresh dashboard reading for at least a minute. Check that the Mac is awake, Bloomkeeper is open, and phone access is connected.',
    };
  const earnings = data.earnings,
    earningsAge = earnings?.updatedAt == null ? null : now - earnings.updatedAt;
  if (
    earningsAge !== null &&
    (!Number.isFinite(earningsAge) || earningsAge < -5)
  )
    return {
      tone: 'red',
      urgent: true,
      detail:
        'The earnings reading has an invalid time. Check the clocks on this device and the Mac.',
    };
  if (earningsAge !== null && earningsAge >= 300)
    return {
      tone: 'red',
      urgent: true,
      detail:
        'Earnings have not refreshed for at least five minutes. The balance is last confirmed, not current.',
    };
  if (age > 10)
    return {
      tone: 'yellow',
      detail:
        error ||
        'Waiting for a fresh dashboard reading. Retrying automatically; last confirmed values are retained.',
    };
  if (
    !earnings ||
    earnings.status !== 'ok' ||
    !!earnings.error ||
    earningsAge === null ||
    earningsAge > 60
  )
    return {
      tone: 'yellow',
      detail:
        'Bloomkeeper is connected, but earnings are waiting for a fresh confirmed reading.',
    };
  if (!data.provider?.online)
    return {
      tone: 'yellow',
      detail:
        'Bloomkeeper is connected. Darkbloom is stopped or its provider reading is not yet available; check model controls if this is unexpected.',
    };
  if (
    data.provider.tracking?.counting === false ||
    (data.pulse &&
      ['paused', 'unmatched', 'offline', 'stale'].includes(data.pulse.status))
  )
    return {
      tone: 'yellow',
      detail:
        'Bloomkeeper is connected. Model readiness or statistics are still updating; model details explain the current run.',
    };
  return {
    tone: 'green',
    detail: error
      ? `A refresh was interrupted; retrying automatically. The last confirmed readings are still recent. ${error}`
      : 'Dashboard and confirmed earnings readings are current. Green does not mean paid work is arriving or guarantee earnings.',
  };
}

/** Five seconds to show a brief degradation; ten healthy seconds to recover.
 * Sustained outages, severely stale earnings and clock faults are never hidden.
 */
export function advanceBloomStatus(
  previous: BloomStatusState | null,
  health: BloomHealth,
  now: number,
): BloomStatusState {
  const settled = !!previous?.settled || health.tone === 'green';
  if (
    !previous ||
    health.tone === 'neutral' ||
    previous.tone === 'neutral' ||
    health.urgent ||
    health.tone === 'red'
  )
    return { tone: health.tone, candidate: health.tone, since: now, settled };
  const since =
    previous.candidate === health.tone && now >= previous.since
      ? previous.since
      : now;
  // Until the first healthy reading, 'Updating' is just loading: go green at once.
  const delay =
    health.tone === 'green'
      ? previous.settled || previous.tone === 'red'
        ? 10
        : 0
      : previous.tone === 'red'
        ? 0
        : 5;
  return {
    tone:
      previous.tone === health.tone || now - since >= delay
        ? health.tone
        : previous.tone,
    candidate: health.tone,
    since,
    settled,
  };
}

export const bloomStatusLabel: Record<BloomTone, string> = {
  green: 'Live',
  yellow: 'Updating',
  red: 'Attention',
  neutral: 'Paused',
};
