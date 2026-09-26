import assert from 'node:assert/strict';
import test from 'node:test';
import { cumulativeEarnings } from '../lib/cumulative-earnings.ts';

const HOUR = 1788742800;

test('the running total sums selected hourly credits in microdollars', () => {
  const hours = [
    { at: HOUR, usd: 0.1 },
    { at: HOUR + 3600, usd: 0.2 },
    { at: HOUR + 7200, usd: 0.000001 },
  ];
  const points = cumulativeEarnings(hours, HOUR + 10800);
  assert.deepEqual(
    points.map((p) => p.cumulative),
    [0, 0.1, 0.3, 0.300001],
  );
  assert.equal(points.at(-1).at, HOUR + 10800);
});

test('selecting a different period resets the baseline instead of carrying prior earnings', () => {
  const hours = [
    { at: HOUR, usd: 12 },
    { at: HOUR + 3600, usd: 0.75 },
  ];
  const points = cumulativeEarnings(hours.slice(1), HOUR + 7200);
  assert.deepEqual(points, [
    { at: HOUR + 3600, cumulative: 0 },
    { at: HOUR + 7200, cumulative: 0.75 },
  ]);
});

test('partial hours end at the observation time and include actual base rewards, not forecasts', () => {
  const hours = [
    { at: HOUR, usd: 0.4, categories: { base_reward: 0.1 }, projected: 10 },
  ];
  assert.deepEqual(cumulativeEarnings(hours, HOUR + 900, HOUR + 60), [
    { at: HOUR + 60, cumulative: 0 },
    { at: HOUR + 900, cumulative: 0.4 },
  ]);
});

test('missing hours break the curve without resetting the recorded running balance', () => {
  const points = cumulativeEarnings(
    [
      { at: HOUR + 7200, usd: 0.2 },
      { at: HOUR, usd: 0.1 },
    ],
    HOUR + 10800,
  );
  assert.deepEqual(points, [
    { at: HOUR, cumulative: 0 },
    { at: HOUR + 3600, cumulative: 0.1 },
    { at: HOUR + 5400, cumulative: null },
    { at: HOUR + 7200, cumulative: 0.1 },
    { at: HOUR + 10800, cumulative: 0.3 },
  ]);
});

test('empty periods stay empty, recorded idle hours remain zero, and adjustments are retained', () => {
  assert.deepEqual(cumulativeEarnings([], HOUR), []);
  const points = cumulativeEarnings(
    [
      { at: HOUR, usd: 1 },
      { at: HOUR + 3600, usd: 0 },
      { at: HOUR + 7200, usd: -0.1 },
    ],
    HOUR + 10800,
  );
  assert.deepEqual(
    points.map((p) => p.cumulative),
    [0, 1, 1, 0.9],
  );
});

const { projectedCumulative } = await import('../lib/cumulative-earnings.ts');
const prediction = {
  status: 'ready',
  asOf: HOUR,
  models: ['a'],
  hours: 16,
  days: 2,
  ratePerHour: 1,
  detail: 'fixture',
  points: [
    { at: HOUR, additional: 0 },
    { at: HOUR + 3600, additional: 1 },
    { at: HOUR + 7200, additional: 2 },
  ],
};
test('dotted projection joins the actual total, interpolates custom horizon, and leaves actuals untouched', () => {
  const actual = [
    { at: HOUR - 3600, cumulative: 0 },
    { at: HOUR, cumulative: 4 },
  ];
  const output = projectedCumulative(actual, prediction, HOUR + 5400);
  assert.equal(output[1].predicted, 4);
  assert.deepEqual(output.at(-1), {
    at: HOUR + 5400,
    cumulative: null,
    predicted: 5.5,
  });
  assert.deepEqual(actual.at(-1), { at: HOUR, cumulative: 4 });
  assert.deepEqual(
    output.filter((p) => p.cumulative != null).map((p) => p.cumulative),
    [0, 4],
  );
});
test('past ranges, stale anchors, missing model history, and empty actuals have no forecast line', () => {
  const actual = [{ at: HOUR, cumulative: 4 }];
  assert.deepEqual(projectedCumulative(actual, prediction, HOUR - 1), actual);
  assert.deepEqual(
    projectedCumulative(
      actual,
      { ...prediction, status: 'learning' },
      HOUR + 3600,
    ),
    actual,
  );
  const old = [{ at: HOUR - 3600, cumulative: 4 }];
  assert.deepEqual(projectedCumulative(old, prediction, HOUR + 3600), old);
  assert.deepEqual(projectedCumulative([], prediction, HOUR + 3600), []);
});
test('projections stay inside the model horizon and zero demand produces a flat dotted line', () => {
  const actual = [{ at: HOUR, cumulative: 4 }];
  assert.equal(
    projectedCumulative(actual, prediction, HOUR + 86400).at(-1).at,
    HOUR + 7200,
  );
  const flat = projectedCumulative(
    actual,
    {
      ...prediction,
      points: prediction.points.map((p) => ({ ...p, additional: 0 })),
    },
    HOUR + 3600,
  );
  assert.equal(flat.at(-1).predicted, 4);
});
