// A short-lived navigation intent, not the persisted discovery preferences.
export const FEATURE_SETUP_EVENT = 'bloom:feature-setup';
let pendingAt: number | null = null;

export function requestFeatureSetup() {
  pendingAt = Date.now();
  window.dispatchEvent(new Event(FEATURE_SETUP_EVENT));
}

export function consumeFeatureSetup() {
  if (pendingAt == null) return false;
  const fresh = Date.now() - pendingAt < 60_000;
  pendingAt = null;
  return fresh;
}
