/**
 * /api/reputation (native/reputation.py snapshot). Every reading field may be null:
 * Darkbloom 0.9.10 no longer sends the 0–1 score, and the backend keeps a reading
 * with an odd counter as unknown rather than dropping it.
 */
export function validReputationResponse(value: unknown): boolean {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const next = value as {
    status?: unknown;
    data?: { score?: unknown; updatedAt?: unknown } | null;
    session?: { id?: unknown } | null;
  };
  const finite = (n: unknown) => typeof n === 'number' && Number.isFinite(n);
  return (
    typeof next.status === 'string' &&
    (next.data == null ||
      (typeof next.data === 'object' &&
        (next.data.score == null || finite(next.data.score)) &&
        finite(next.data.updatedAt))) &&
    (next.session == null ||
      (typeof next.session === 'object' && finite(next.session.id)))
  );
}
