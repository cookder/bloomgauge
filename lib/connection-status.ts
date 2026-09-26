/** Transport status is distinct from provider readiness or paid work. */
export function dashboardConnection(
  at: number | null | undefined,
  now: number,
  error: string,
  paused: boolean,
  visible: boolean,
) {
  const hasReading = at != null;
  const age = hasReading ? now - at : null;
  const invalid = hasReading && (!Number.isFinite(at) || !Number.isFinite(now));
  const future = age !== null && age < -5;
  const stale = !!error || (hasReading && (invalid || age! > 10 || future));
  const detail =
    error ||
    (invalid
      ? 'The dashboard reading has an invalid timestamp. Retrying automatically.'
      : future
        ? 'The reading is ahead of this device’s clock. Check the date and time on this device and the Mac.'
        : stale
          ? 'The last dashboard reading is more than 10 seconds old. Retrying automatically.'
          : '');
  return {
    stale,
    live: hasReading && !stale && !paused && visible,
    detail,
    label: paused
      ? 'View paused'
      : !visible
        ? 'View in background'
        : stale
          ? 'Readings delayed'
          : hasReading
            ? 'Live'
            : 'Connecting',
  };
}

/** Never expose response bodies, private URLs or browser JSON-parser excerpts. */
export async function readStatusJSON(
  response: Response,
  label: string,
): Promise<unknown> {
  if (!response.ok)
    throw Error(
      `${label} unavailable (HTTP ${response.status}). Retrying automatically.`,
    );
  try {
    return await response.json();
  } catch {
    throw Error(
      `${label} returned an unreadable response. Retrying automatically.`,
    );
  }
}
