export type UnitEarnings = {
  requests: number;
  adjustments: number;
  usdPerRequest: number | null;
  usdPerMillionOutput: number | null;
  usdPerMillionTokens: number | null;
  outputSamples: number;
  completeTokenSamples: number;
};

// An older collector may omit these new fields during an asset update.
export function validUnitEarnings(value: unknown): boolean {
  if (value === undefined) return true;
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const r = value as Record<string, unknown>;
  return (
    ['requests', 'adjustments', 'outputSamples', 'completeTokenSamples'].every(
      (key) => Number.isSafeInteger(r[key]) && (r[key] as number) >= 0,
    ) &&
    ['usdPerRequest', 'usdPerMillionOutput', 'usdPerMillionTokens'].every(
      (key) =>
        r[key] === null ||
        (typeof r[key] === 'number' && Number.isFinite(r[key]) && r[key] >= 0),
    ) &&
    (r.completeTokenSamples as number) <= (r.outputSamples as number) &&
    (r.outputSamples as number) <= (r.requests as number)
  );
}

export function perToken(perMillion: number | null | undefined) {
  return perMillion == null ? null : perMillion / 1e6;
}

export function yieldMoney(value: number | null | undefined, digits = 6) {
  if (value == null || !Number.isFinite(value) || value < 0) return '—';
  if (value > 0 && value < 1e-9) return `$${value.toExponential(2)}`;
  const precision =
    value > 0
      ? Math.max(digits, Math.min(9, 2 - Math.floor(Math.log10(value))))
      : digits;
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: digits,
    maximumFractionDigits: precision,
  }).format(value);
}
