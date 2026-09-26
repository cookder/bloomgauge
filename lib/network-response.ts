type RecordValue = Record<string, unknown>;
const record = (value: unknown): value is RecordValue =>
  value !== null && typeof value === 'object' && !Array.isArray(value);
const optionalText = (value: unknown) =>
  value == null || typeof value === 'string';
const optionalRows = (value: unknown, check: (row: RecordValue) => boolean) =>
  value == null ||
  (Array.isArray(value) && value.every((row) => record(row) && check(row)));
const location = (row: RecordValue) =>
  ['key', 'city', 'region', 'country'].every((key) => optionalText(row[key]));
const capacityNumbers = [
  'active_requests',
  'queued_requests',
  'warm_providers',
  'routable_providers',
  'running_providers',
  'aggregate_tps',
  'estimated_ttft_ms',
];

function validPayload(key: string, value: unknown) {
  if (!record(value)) return false;
  if (key === 'capacity')
    return (
      Array.isArray(value.models) &&
      value.models.every(
        (row) =>
          record(row) &&
          typeof row.id === 'string' &&
          capacityNumbers.every(
            (name) =>
              typeof row[name] === 'number' && Number.isFinite(row[name]),
          ),
      )
    );
  if (key === 'stats')
    return (
      [
        'request_regions',
        'request_locations',
        'provider_regions',
        'provider_locations',
      ].every((name) => optionalRows(value[name], location)) &&
      optionalRows(value.providers, (row) =>
        ['id', 'chip', 'hardware_chip', 'current_model'].every((name) =>
          optionalText(row[name]),
        ),
      ) &&
      optionalRows(
        value.request_flows,
        (row) =>
          record(row.from) &&
          location(row.from) &&
          record(row.to) &&
          location(row.to),
      ) &&
      (value.network_utilization == null || record(value.network_utilization))
    );
  if (key === 'backfill')
    return (
      Array.isArray(value.windows) &&
      value.windows.every((item) => typeof item === 'string')
    );
  return true;
}

/** A broken optional feed must not crash the Network tab or hide working feeds.
 * Retain that feed's previously loaded payload, explicitly marked stale. */
export function sanitizeNetworkResponse(
  value: unknown,
  previous: unknown = null,
): RecordValue {
  if (!record(value))
    throw new Error('Network returned an incomplete response.');
  const old = record(previous) ? previous : {};
  const result: RecordValue = { ...value };
  for (const key of ['capacity', 'stats', 'totals', 'series', 'backfill']) {
    const source = value[key];
    if (
      record(source) &&
      typeof source.status === 'string' &&
      (source.data == null
        ? source.status !== 'ok'
        : validPayload(key, source.data))
    )
      continue;
    const fallback = record(old[key]) ? old[key] : null;
    result[key] = {
      status: fallback?.data ? 'stale' : 'error',
      data: fallback?.data,
      updatedAt: fallback?.updatedAt,
      error: `The ${key} feed returned an incomplete response.`,
    };
  }
  return result;
}
