export type ReleaseNotes = {
  installedVersion: string;
  release: {
    id: string;
    version: string;
    title: string;
    highlights: { title: string; detail: string }[];
  } | null;
};
const record = (v: unknown): v is Record<string, unknown> =>
  !!v && typeof v === 'object' && !Array.isArray(v);
const text = (v: unknown, limit: number): v is string =>
  typeof v === 'string' && v.trim().length > 0 && v.length <= limit;
export function validReleaseNotes(v: unknown): v is ReleaseNotes {
  if (!record(v) || !text(v.installedVersion, 64)) return false;
  const r = v.release;
  return (
    r === null ||
    (record(r) &&
      text(r.id, 100) &&
      text(r.version, 64) &&
      r.version === v.installedVersion &&
      text(r.title, 160) &&
      Array.isArray(r.highlights) &&
      r.highlights.length > 0 &&
      r.highlights.length <= 12 &&
      r.highlights.every(
        (h) => record(h) && text(h.title, 120) && text(h.detail, 800),
      ))
  );
}
export const releaseSeenKey = 'bloom-release-notes-seen-v1';
export const releaseNoticesKey = 'bloom-release-notices-enabled-v1';
export function readSeenReleases(raw: string | null): string[] {
  try {
    const value: unknown = JSON.parse(raw || '[]');
    return Array.isArray(value)
      ? value.filter((v): v is string => text(v, 100)).slice(-16)
      : [];
  } catch {
    return [];
  }
}
export function markReleaseSeen(raw: string | null, id: string): string {
  return JSON.stringify(
    [...new Set([...readSeenReleases(raw), id])].slice(-16),
  );
}
