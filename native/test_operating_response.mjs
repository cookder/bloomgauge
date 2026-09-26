import test from 'node:test';
import assert from 'node:assert/strict';
import {
  validEnergy,
  validConcurrencyHistory,
  validSmoothing,
} from '../lib/operating-response.ts';
const base = {
  at: 1000,
  from: 600,
  to: 1000,
  coverageStart: null,
  coverageEnd: null,
  bucketSeconds: 60,
  samples: [],
  method: 'Observed readings',
};
const energy = {
  ...base,
  source: 'SMC',
  tariff: { id: 1, at: 600, rate: 0.1353381066, label: 'Bill average' },
  tariffs: [],
  latest: null,
  totals: { seconds: 0, acSeconds: 0, kwh: 0, costUsd: null },
  comparison: {
    seconds: 0,
    inferenceUsd: 0,
    accountBaseUsd: 0,
    costUsd: 0,
    afterCostUsd: null,
  },
};
const concurrent = {
  ...base,
  latest: null,
  fresh: false,
  sessions: [],
  models: [],
  observations: 0,
  truncated: false,
  failureIncrements: null,
};
test('unknown power and concurrency history remain valid without invented zero measurements', () => {
  assert.equal(validEnergy(energy), true);
  assert.equal(validConcurrencyHistory(concurrent), true);
  assert.equal(
    validEnergy({
      ...energy,
      samples: [{ at: 800, watts: null, costRate: 0 }],
    }),
    true,
  );
});
test('malformed measurement and tariff payloads are rejected before rendering', () => {
  for (const v of [
    null,
    {},
    { ...energy, tariffs: [null] },
    { ...energy, totals: { costUsd: 0 } },
    { ...energy, comparison: [] },
    { ...energy, latest: { watts: 3 } },
    { ...energy, samples: [{ at: 800, watts: NaN }] },
    { ...energy, tariff: { ...energy.tariff, rate: Infinity } },
  ])
    assert.equal(validEnergy(v), false);
  for (const v of [
    null,
    {},
    { ...concurrent, sessions: [{ id: 3, models: 'a' }] },
    { ...concurrent, latest: { at: 999, session: 3, models: ['a'] } },
    { ...concurrent, failureIncrements: 'unknown' },
  ])
    assert.equal(validConcurrencyHistory(v), false);
});
test('passive smoothing accepts empty results and refuses malformed variants', () => {
  assert.equal(
    validSmoothing({ passive: true, method: 'EMA', variants: [] }),
    true,
  );
  assert.equal(
    validSmoothing({ passive: true, method: 'EMA', variants: [{}] }),
    false,
  );
  assert.equal(
    validSmoothing({ passive: false, method: 'EMA', variants: [] }),
    false,
  );
});
