// Show at least cents, adding precision when a chart's actual scale is smaller.
// Five tick intervals must remain distinguishable down to ledger microdollars.
export function currencyAxisPrecision(maximum: number): number {
  const magnitude = Math.abs(maximum);
  return Number.isFinite(magnitude) && magnitude > 0
    ? Math.min(6, Math.max(2, Math.ceil(-Math.log10(magnitude / 5))))
    : 2;
}
