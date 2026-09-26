import test from 'node:test';
import assert from 'node:assert/strict';
import { validTargetReport } from '../lib/earnings-target.ts';
const report = () => ({
  at: 10000,
  from: 0,
  to: 7200,
  requestedFrom: 0,
  historyStart: 0,
  model: null,
  models: ['a'],
  targetUsdPerHour: 0.12,
  dailyTargetUsd: 2.88,
  usd: 0.1,
  inferenceUsd: 0.08,
  accountBaseUsd: 0.02,
  includesBase: true,
  coveredSeconds: 7200,
  rangeSeconds: 7200,
  completeCoverage: true,
  clockUsdPerHour: 0.05,
  completeHours: 2,
  metHours: 0,
  unknownHours: 0,
  metPercent: 0,
  completeHourAverageUsd: 0.05,
  longestBelowHours: 2,
  switchSeconds: 60,
  switchCount: 1,
  hourly: [
    {
      at: 0,
      end: 3600,
      from: 0,
      to: 3600,
      usd: 0.1,
      inferenceUsd: 0.08,
      baseUsd: 0.02,
      status: 'below',
      covered: true,
    },
  ],
  chartTruncated: false,
  scope: 'This Mac + account base',
  method: 'Clock time',
});
test('target report accepts real zero, unknown coverage and partial hours', () => {
  assert.equal(validTargetReport(report()), true);
  const r = report();
  r.metPercent = null;
  r.clockUsdPerHour = null;
  r.completeCoverage = false;
  r.hourly[0].status = 'unknown';
  r.hourly[0].covered = false;
  assert.equal(validTargetReport(r), true);
});
test('malformed target cells and aggregates cannot replace displayed data', () => {
  for (const change of [
    { hourly: [null] },
    { usd: NaN },
    { rangeSeconds: {} },
    { metHours: 5 },
    { metPercent: 101 },
    { model: [] },
    { hourly: [{ ...report().hourly[0], status: 'invented' }] },
    { hourly: [{ ...report().hourly[0], usd: {} }] },
  ])
    assert.equal(validTargetReport({ ...report(), ...change }), false);
});
