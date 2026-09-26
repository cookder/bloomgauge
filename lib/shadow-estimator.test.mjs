import test from 'node:test';
import assert from 'node:assert/strict';
import { shadowProgress, validShadowReport } from './shadow-estimator.ts';

// Mirrors components/dashboard/shared.tsx money(): cents from $0.10, two significant digits below.
const money = (n) =>
  n === 0 || Math.abs(n) >= 0.1 ? `$${n.toFixed(2)}` : `$${n.toPrecision(2)}`;
const row = (over = {}) => ({
  basis: 'measured',
  hours: 222.8,
  periods: 503,
  exponent: 0.6,
  pressure: 0.33,
  serving: true,
  usdPerHour: 0.0888,
  lower: 0.0672,
  upper: 0.1097,
  extrapolated: false,
  recentAdjustment: 0.048,
  matched: {
    usdPerHour: 0.165,
    lower: 0.14,
    upper: 0.207,
    usable: true,
    scope: 'current_session_30m',
  },
  ...over,
});
const score = (over = {}) => ({
  windows: 40,
  pairedWindows: 38,
  pendingWindows: 3,
  daysScored: 2,
  curveMAE: 0.03,
  curveBias: 0.01,
  inRangeShare: 0.6,
  pairedCurveMAE: 0.031,
  pairedMatchedMAE: 0.036,
  ...over,
});
const report = (over = {}) => ({
  latest: {
    at: 1790357451.5,
    checkpoint: 1790357400,
    current: 'gemma',
    version: 'pressure-curve-v1',
    models: {
      gemma: row(),
      qwen: row({
        serving: false,
        pressure: null,
        usdPerHour: null,
        lower: null,
        upper: null,
      }),
    },
  },
  evaluation: {
    status: 'collecting',
    overall: score(),
    leader: 'curve',
    models: [],
  },
  ...over,
});

test('live packets validate, including models without a current demand reading', () => {
  assert.equal(validShadowReport(report()), true);
  assert.equal(
    validShadowReport({
      latest: null,
      evaluation: { status: 'disabled', models: [] },
    }),
    true,
  );
});

test('malformed packets are rejected rather than shown as estimates', () => {
  for (const bad of [
    null,
    {},
    report({ evaluation: null }),
    report({
      latest: { at: 1, models: { a: row({ usdPerHour: 0.1, lower: null }) } },
    }),
    report({ latest: { at: 1, models: { a: row({ serving: 'yes' }) } } }),
    report({ evaluation: { status: 'collecting', leader: 'other' } }),
    report({
      evaluation: {
        status: 'collecting',
        overall: score({ pairedWindows: -1 }),
      },
    }),
  ]) {
    assert.equal(validShadowReport(bad), false);
  }
});

test('progress reads toward the gate and names the leader', () => {
  assert.match(
    shadowProgress(report(), money),
    /^Scored 38 of 100 compared half-hours, 2 of 7 days\. Typical miss so far: \$0\.031\/hr shadow vs \$0\.036\/hr current method \(shadow ahead\)\.$/,
  );
  assert.match(
    shadowProgress(
      report({
        evaluation: {
          status: 'ready',
          overall: score({ pairedWindows: 140, daysScored: 9 }),
          leader: 'matched',
        },
      }),
      money,
    ),
    /^Enough scoring to review: 100 of 100 compared half-hours, 7 of 7 days\..*current method ahead.*only takes over if it proves more accurate/,
  );
  assert.match(
    shadowProgress(
      report({
        evaluation: {
          status: 'collecting',
          overall: score({ pairedWindows: 0 }),
        },
      }),
      money,
    ),
    /Scoring starts/,
  );
  assert.equal(
    shadowProgress({ latest: null, evaluation: { status: 'disabled' } }, money),
    'The shadow test is off on this Mac.',
  );
});
