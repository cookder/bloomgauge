export type CachePermissionResult = {
  requestId: string;
  status: 'completed' | 'cancelled' | 'failed' | 'busy' | 'unavailable';
};
export function cachePermissionResult(
  value: unknown,
  requestId: string,
): CachePermissionResult | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const v = value as Record<string, unknown>;
  return v.requestId === requestId &&
    typeof v.status === 'string' &&
    ['completed', 'cancelled', 'failed', 'busy', 'unavailable'].includes(
      v.status,
    )
    ? (v as CachePermissionResult)
    : null;
}
export function cachePermissionMessage(
  status: CachePermissionResult['status'],
): string {
  return {
    completed: 'Setup finished. Checking the current permission…',
    cancelled:
      'Permission change cancelled. Your model selection is unchanged.',
    failed:
      'Could not change the permission. Existing administrator rules were left for review. Check permission or open Help & feedback.',
    busy: 'Another permission dialog is open in BloomGauge. Finish or cancel that dialog first.',
    unavailable:
      'Permission setup is unavailable here. Open the installed BloomGauge app on your Mac.',
  }[status];
}
