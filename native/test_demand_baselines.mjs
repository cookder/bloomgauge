import test from 'node:test';
import assert from 'node:assert/strict';
import {
  validBaselineReport,
  validConditionalBaseline,
} from '../lib/optimizer-response.ts';
const observation = () => ({
  hours: 0,
  days: 0,
  minutes: 0,
  asOf: null,
  usd: 0,
  paidJobs: 0,
  active: null,
  queued: null,
  warm: null,
  load: null,
  pressure: null,
  usdPerHour: null,
  requestsPerMinute: null,
  tokensPerSecond: null,
  busyPercent: null,
});
const baseline = () => ({
  ...observation(),
  scope: 'similar_demand',
  usable: false,
  weight: 1,
  reason: 'No history',
  blocks: 0,
  coverage: 0,
  overlapHours: 0,
  totalHours: 0,
  lower: null,
  upper: null,
  current: null,
});
const report = () => ({
  at: 100,
  from: 0,
  to: 100,
  model: 'a',
  models: [{ id: 'a', hours: 0, overlapHours: 0 }],
  summary: observation(),
  baseline: baseline(),
  periods: [],
  bands: [{ low: 0, high: null, ...observation() }],
  periodSeconds: 1800,
  scope: 'This Mac',
  method: 'Observed',
});
test('unknown demand outcomes remain null and valid', () => {
  assert.equal(validBaselineReport(report()), true);
  assert.equal(validConditionalBaseline(baseline()), true);
});
test('malformed nested metric values do not reach the chart or controls', () => {
  for (const k of [
    'usdPerHour',
    'active',
    'pressure',
    'warm',
    'tokensPerSecond',
  ]) {
    for (const v of [{}, NaN, '0', undefined]) {
      const r = report();
      r.summary[k] = v;
      assert.equal(validBaselineReport(r), false);
      const b = baseline();
      b[k] = v;
      assert.equal(validConditionalBaseline(b), false);
    }
  }
  for (const changed of [
    { current: {} },
    { weight: NaN },
    { usable: 'true' },
    { reason: {} },
    { lower: {} },
  ])
    assert.equal(
      validConditionalBaseline({ ...baseline(), ...changed }),
      false,
    );
});
test('empty, malformed and oversized period containers are rejected', () => {
  for (const changed of [
    { periods: null },
    { models: [{}] },
    { bands: [{ low: 0, high: {} }] },
    { model: {} },
    { periodSeconds: 0 },
    { periods: Array(603).fill({ at: 1, end: 30, ...observation() }) },
  ])
    assert.equal(validBaselineReport({ ...report(), ...changed }), false);
  assert.equal(
    validBaselineReport({
      ...report(),
      periods: [{ at: 1, end: 30, ...observation(), usdPerHour: 0 }],
    }),
    true,
  );
});
test('comparison coverage and forecast qualification validate independently of all-history coverage', () => {
  const b = {
    ...baseline(),
    forecastUsable: true,
    matchingCoverage: 1,
    otherDemandHours: 10,
    otherContextHours: 2,
    unknownDemandHours: 20,
  };
  assert.equal(validConditionalBaseline(b), true);
  for (const change of [
    { forecastUsable: 'yes' },
    { matchingCoverage: NaN },
    { otherDemandHours: {} },
    { otherContextHours: -1 },
    { unknownDemandHours: '0' },
  ])
    assert.equal(validConditionalBaseline({ ...b, ...change }), false);
  // Signed credit adjustments are real historical outcomes, not malformed telemetry.
  assert.equal(
    validConditionalBaseline({
      ...b,
      usd: -0.1,
      usdPerHour: -0.01,
      lower: -0.02,
      upper: 0,
    }),
    true,
  );
});
