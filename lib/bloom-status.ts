export type BloomTone = 'green' | 'yellow' | 'red' | 'neutral';
export type BloomHealthSnapshot = {
  at: number;
  earnings?: {
    status: string;
    updatedAt: number | null;
    error?: string | null;
    /** The collector's earnings poll interval (live_earnings.POLL_SECONDS). */
    pollSeconds?: number;
  };
  provider?: {
    online: boolean;
    tracking?: { counting?: boolean; detail?: string };
  };
  pulse?: { status: string };
};
export type BloomHealth = {
  tone: BloomTone;
  detail: string;
  urgent?: boolean;
  /** Replaces the tone's usual label (a steady pause is not "Paused" or "Background"). */
  label?: string;
};

// Statistics pauses that last as long as the Mac's situation does, not an update in
// progress: Darkbloom unloaded an idle model, a 3+ model Mac waiting for work, per-model
// statistics on a 3+ model set, or a Mac not matched to the provider roster. The backend
// sends no reason code (tracking.status is only 'paused'), so these read its fixed details
// (native/model_readiness.py, provider_reporting.py; pinned by lib/bloom-status.test.mjs).
export const steadyPauseDetails = [
  "isn't loaded right now",
  'Not loaded now:',
  'are loaded yet',
  'waiting to observe fresh serving output',
  'Per-model statistics need one model or a pair',
  'to the provider roster',
];

/** The backend's reason when statistics pause for a steady state; null otherwise. */
export function steadyPause(data: BloomHealthSnapshot): string | null {
  const tracking = data.provider?.tracking;
  const detail = typeof tracking?.detail === 'string' ? tracking.detail : '';
  if (
    tracking?.counting === false &&
    steadyPauseDetails.some((d) => detail.includes(d))
  )
    return detail;
  if (tracking?.counting !== false && data.pulse?.status === 'unmatched')
    return 'Pace waits until Darkbloom’s provider roster matches this Mac’s session.';
  return null;
}
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
  // The backend's own freshness: two polls plus 5 s (45 s at the usual 20 s poll).
  const poll =
    typeof earnings?.pollSeconds === 'number' &&
    Number.isFinite(earnings.pollSeconds) &&
    earnings.pollSeconds > 0
      ? earnings.pollSeconds
      : 20;
  const earningsFresh = 2 * poll + 5,
    earningsAttention = Math.max(300, 15 * poll);
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
  if (earningsAge !== null && earningsAge >= earningsAttention)
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
    earningsAge > earningsFresh
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
  const steady = steadyPause(data);
  if (steady)
    return {
      tone: 'neutral',
      label: 'Connected',
      detail: `Readings are current. ${steady}`,
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
