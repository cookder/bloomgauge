import test from 'node:test';
import assert from 'node:assert/strict';
import {
  readNetworkContributions,
  contributionGroups,
  groupedContributions,
  contributionShares,
} from '../lib/network-contributions.ts';
const report = (metric = 'activity') => ({
  schemaVersion: 1,
  metric,
  scope: metric === 'earnings' ? 'this_mac' : 'network',
  status: 'partial',
  at: 1000,
  from: 100,
  to: 1000,
  bucketSeconds: 300,
  unit: metric === 'earnings' ? 'usd_per_hour' : 'concurrent_requests',
  attribution: 'models',
  series: [
    { id: 'model:a', name: 'A' },
    { id: 'model:b', name: 'B' },
  ],
  points: [
    { from: 100, to: 400, values: [0, null], coverageFraction: 0.9 },
    { from: 400, to: 700, values: [3, 2], coverageFraction: 1 },
  ],
  summary: {
    total: 5,
    values: [3, 2],
    unit: metric === 'earnings' ? 'usd' : 'concurrent_requests',
  },
  coverage: {
    observedSeconds: 570,
    requestedSeconds: 900,
    fraction: 570 / 900,
  },
  notes: ['Recorded observations only.'],
  networkMoney: {
    available: false,
    reason: 'No network payout history by model.',
  },
});
test('zero, unknown and signed earnings retain their meaning', () => {
  const r = report();
  assert.equal(readNetworkContributions(r, 'activity'), r);
  assert.deepEqual(
    groupedContributions(r.points[0].values, contributionGroups(r)),
    [0, null],
  );
  const e = report('earnings');
  e.summary.values = [0.3, -0.05];
  e.summary.total = 0.25;
  e.points[1].values = [0.3, -0.05];
  assert.equal(readNetworkContributions(e, 'earnings'), e);
  assert.deepEqual(contributionShares(e.summary.values), [null, null]);
  assert.deepEqual(contributionShares([0, null]), [null, null]);
  assert.deepEqual(contributionShares([0, 2]), [0, 100]);
});
test('partial totals, malformed scope, boundaries and unsupported numbers fail closed', () => {
  for (const mutate of [
    (r) => (r.summary.values[0] = null),
    (r) => (r.summary.total = 99),
    (r) => (r.scope = 'this_mac'),
    (r) => (r.points[0].values[0] = NaN),
    (r) => (r.points[0].values[0] = -1),
    (r) => (r.points[1].from = 300),
    (r) => (r.points[1].to = 1001),
    (r) => (r.coverage.fraction = 2),
    (r) => r.series.push(r.series[0]),
    (r) => r.points[0].values.pop(),
    (r) => (r.metric = 'earnings'),
  ]) {
    const r = report();
    mutate(r);
    assert.throws(() => readNetworkContributions(r, 'activity'));
  }
});
test('display ordering conserves exact contributions and never drops unknowns', () => {
  const r = report();
  r.series = Array.from({ length: 9 }, (_, i) => ({
    id: 'model:' + i,
    name: 'Model ' + i,
  }));
  r.summary.values = [0, 1, 2, 3, 4, 5, 6, 7, 8];
  r.summary.total = 36;
  const groups = contributionGroups(r),
    values = groupedContributions(r.summary.values, groups);
  assert.equal(groups.length, 9);
  assert.equal(
    values.reduce((a, b) => a + b, 0),
    36,
  );
  r.summary.values[2] = null;
  assert.equal(
    groupedContributions(r.summary.values, groups).filter((v) => v === null)
      .length,
    1,
  );
});
