// "What's changed": a one-time card at the top of Optimizer → Overview after an
// update. Dismissal is saved on the Mac (GET/POST /api/whats-changed, native/
// whats_changed.py) so the Mac and phone agree, and in this browser as a fallback.

/** Bump this id (not the app version) when the card gets new content. */
export const whatsChangedId = 'optimizer-manager-2026-09';
export const whatsChangedLocalKey = 'bloom-whats-changed-seen-v1';
export const WHATS_CHANGED_EVENT = 'bloom-show-whats-changed';
/** Keys an earlier version leaves in this browser (release notes, first-On explainer). */
export const earlierUseKeys = [
  'bloom-release-notes-seen-v1',
  'bloom-optimizer-intro-seen-v1',
];

export const whatsChangedTitle = 'What’s changed in the optimizer';

export const managerChanges: string[] = [
  'The optimizer is now a manager. It keeps one home model running instead of trying other models to see what pays.',
  'Why: on our test Mac, the old optimizer earned about 13–16% less than just staying on Gemma. Time with no model ready cost more than picking the wrong model.',
  'Your home model is the one you pin, or the one that has paid best on this Mac over the last 30 days.',
  'If a switch fails or no model is ready for about 10 minutes, it brings the home model back instead of switching itself off.',
  'It moves only when at least 5 Macs like yours clearly earn more on another model for 2 hours, at most 3 times a day. If those moves don’t pay off, it stops making them.',
];

export const smallerChanges: string[] = [
  'Fewer false alarms: “Run status unavailable” no longer shows by mistake or sends reports on its own.',
  'Reputation shows again with Darkbloom 0.9.10, which stopped sending a score.',
  'Statistics keep counting when your Mac offers more than one model.',
  'Pace and day ratings are judged against this Mac’s own history. There is no preset earnings goal.',
  'Demand is compared with what’s usual for this time of day.',
  'My Macs lists every model, the pace of each, and whether the Manager is on.',
  'While Darkbloom preloads at startup, it shows as starting, not stopped.',
  'Switch messages say which check saw the change.',
];

export const feedbackTitle = 'Have an idea or a problem?';
export const feedbackText =
  'Tell us what you’d like BloomGauge to do, or what isn’t working. Help & feedback has a report form, the BloomGauge Slack channel and email.';
export const feedbackAction = 'Suggest a feature or send feedback';
export const slackUrl = 'https://darkbloom.slack.com/archives/C0C4HC8HZLN';

export type WhatsChangedStatus = {
  available: boolean;
  returning: boolean | null;
  seen: string[];
};

const note = /^[a-z0-9][a-z0-9.-]{0,63}$/;

export function validWhatsChangedStatus(v: unknown): v is WhatsChangedStatus {
  if (!v || typeof v !== 'object' || Array.isArray(v)) return false;
  const r = v as Record<string, unknown>;
  return (
    typeof r.available === 'boolean' &&
    (r.returning === null || typeof r.returning === 'boolean') &&
    Array.isArray(r.seen) &&
    r.seen.every((id) => typeof id === 'string' && note.test(id))
  );
}

export function readLocalSeen(raw: string | null): string[] {
  try {
    const value: unknown = JSON.parse(raw || '[]');
    return Array.isArray(value)
      ? value
          .filter((id): id is string => typeof id === 'string' && note.test(id))
          .slice(-16)
      : [];
  } catch {
    return [];
  }
}

export function markLocalSeen(raw: string | null, id: string): string {
  return JSON.stringify([...new Set([...readLocalSeen(raw), id])].slice(-16));
}

/**
 * Show the card automatically only to people who used an earlier version and
 * haven't closed it on any device. Brand-new installs skip it: the first-On
 * explainer already describes the manager, and "what changed" means nothing yet.
 * The card is always available from the "What's changed" link.
 */
export function shouldShowWhatsChanged({
  id = whatsChangedId,
  server,
  localSeen,
  earlierUseHere,
}: {
  id?: string;
  /** null while loading or when the Mac could not be reached. */
  server: WhatsChangedStatus | null;
  localSeen: string[];
  /** This browser has keys an earlier version left behind. */
  earlierUseHere: boolean;
}): boolean {
  if (localSeen.includes(id)) return false;
  if (server?.available) {
    if (server.seen.includes(id)) return false;
    return server.returning === true;
  }
  return earlierUseHere;
}
