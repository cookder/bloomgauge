import test from 'node:test';
import assert from 'node:assert/strict';
import { currencyAxisPrecision } from '../lib/chart-precision.ts';

test('fractional-cent chart ticks remain distinct instead of repeated $0.00 labels', () => {
  for (const maximum of [0.004, 0.0004, 0.00004]) {
    const precision = currencyAxisPrecision(maximum);
    const format = new Intl.NumberFormat('en-US', {
      style: 'currency',
      currency: 'USD',
      minimumFractionDigits: precision,
      maximumFractionDigits: precision,
    });
    const labels = [0, 1, 2, 3, 4].map((step) =>
      format.format((maximum * step) / 4),
    );
    assert.equal(new Set(labels).size, 5);
  }
});
test('normal dollars keep cents and signed adjustments use the same scale', () => {
  assert.equal(currencyAxisPrecision(12), 2);
  assert.equal(currencyAxisPrecision(0), 2);
  assert.equal(currencyAxisPrecision(-0.0004), currencyAxisPrecision(0.0004));
  assert.equal(currencyAxisPrecision(1e-9), 6);
});
