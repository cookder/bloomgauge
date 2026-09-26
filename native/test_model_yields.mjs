import test from 'node:test';
import assert from 'node:assert/strict';
import {
  perToken,
  validUnitEarnings,
  yieldMoney,
} from '../lib/model-yields.ts';

test('fractional dollar yields stay visible while missing measurements remain unknown', () => {
  assert.equal(yieldMoney(perToken(0.024)), '$0.000000024');
  assert.equal(yieldMoney(perToken(3.21), 8), '$0.00000321');
  assert.equal(yieldMoney(perToken(0.0002)), '$2.00e-10');
  assert.equal(yieldMoney(0), '$0.000000');
  for (const value of [null, undefined, NaN, Infinity, -1])
    assert.equal(yieldMoney(value), '—');
  assert.equal(perToken(undefined), null);
});

test('legacy responses are safe and invalid financial payloads cannot become yields', () => {
  const known = {
    requests: 20,
    adjustments: 1,
    usdPerRequest: 0.0001,
    usdPerMillionOutput: 0.4,
    usdPerMillionTokens: null,
    outputSamples: 10,
    completeTokenSamples: 0,
  };
  assert.equal(validUnitEarnings(known), true);
  assert.equal(validUnitEarnings(undefined), true);
  for (const change of [
    { requests: null },
    { usdPerRequest: {} },
    { usdPerMillionOutput: NaN },
    { usdPerMillionTokens: -0.1 },
    { outputSamples: 21 },
    { completeTokenSamples: 11 },
    { requests: 1.2 },
  ]) {
    assert.equal(validUnitEarnings({ ...known, ...change }), false);
  }
  for (const value of [null, [], {}, 'missing'])
    assert.equal(validUnitEarnings(value), false);
});
