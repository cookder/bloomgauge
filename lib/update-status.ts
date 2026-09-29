export type UpdateStatus = {
  requestId: string | null;
  available: boolean;
  installedVersion: string;
  automaticChecks: boolean;
  /** Check, download and install automatically (the one switch in Help). */
  automaticUpdates: boolean;
  canCheck: boolean;
  checking: boolean;
  lastCheck: number | null;
  status:
    | 'idle'
    | 'checking'
    | 'update-available'
    | 'up-to-date'
    | 'error'
    | 'blocked'
    | 'ready-to-install'
    | 'installing';
  error: string | null;
};

export function validUpdateStatus(value: unknown): value is UpdateStatus {
  if (!value || typeof value !== 'object') return false;
  const v = value as Record<string, unknown>;
  return (
    (v.requestId === null || typeof v.requestId === 'string') &&
    [
      'available',
      'automaticChecks',
      'automaticUpdates',
      'canCheck',
      'checking',
    ].every((key) => typeof v[key] === 'boolean') &&
    typeof v.installedVersion === 'string' &&
    v.installedVersion.length > 0 &&
    v.installedVersion.length <= 64 &&
    (v.lastCheck === null ||
      (typeof v.lastCheck === 'number' &&
        Number.isFinite(v.lastCheck) &&
        v.lastCheck > 0 &&
        v.lastCheck < 8640000000000)) &&
    typeof v.status === 'string' &&
    [
      'idle',
      'checking',
      'update-available',
      'up-to-date',
      'error',
      'blocked',
      'ready-to-install',
      'installing',
    ].includes(v.status) &&
    (v.error === null ||
      (typeof v.error === 'string' && v.error.length <= 1000))
  );
}
