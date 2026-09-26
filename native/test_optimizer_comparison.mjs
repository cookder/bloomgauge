import test from 'node:test';
import assert from 'node:assert/strict';
import { comparisonOrder } from '../lib/optimizer-comparison.ts';
test('comparison order never turns missing money or high demand into the best paid alternative', () => {
  const rows = [
    { model: 'current', current: true, selected: true, netGainUsd: 1 },
    { model: 'unknown', current: false, selected: true, netGainUsd: null },
    { model: 'worse', current: false, selected: true, netGainUsd: -0.12 },
    { model: 'gemma', current: false, selected: true, netGainUsd: -0.08 },
    { model: 'outside', current: false, selected: false, netGainUsd: 0.2 },
  ];
  const copy = structuredClone(rows);
  assert.deepEqual(
    comparisonOrder(rows).map((r) => r.model),
    ['gemma', 'worse', 'unknown', 'outside'],
  );
  assert.deepEqual(rows, copy);
});
test('nonfinite values remain unknown; measured zero can be compared', () => {
  const rows = [
    { model: 'bad', current: false, selected: true, netGainUsd: NaN },
    { model: 'zero', current: false, selected: true, netGainUsd: 0 },
    { model: 'negative', current: false, selected: true, netGainUsd: -1 },
  ];
  assert.deepEqual(
    comparisonOrder(rows).map((r) => r.model),
    ['zero', 'negative', 'bad'],
  );
});
