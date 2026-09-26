import test from 'node:test';
import assert from 'node:assert/strict';
import {
  pulseChartRows,
  pulseSegments,
  smoothPulse,
} from '../lib/pulse-smoothing.ts';

const point = (at, rate60, rate300 = rate60) => ({ at, rate60, rate300 });

test('hourly charts keep the live meter value after smoothing, for both pace windows and pulses', () => {
  for (const rate of [0.2443, 24.43, 6.75, 0, -0.0123]) {
    for (const field of ['rate60', 'rate300']) {
      const samples = Array.from({ length: 601 }, (_, i) =>
        point(i * 6, i > 570 ? rate : 0.05),
      );
      const original = structuredClone(samples);
      const rows = pulseChartRows(
        samples,
        point(3600, rate, rate),
        0,
        3600,
        12,
      );
      const plot = smoothPulse(rows, field, 120, 12, 60);
      assert.equal(plot.at(-1)[field], rate);
      assert.deepEqual(
        plot.filter((row) => row.at >= 3540),
        rows.filter((row) => row.at >= 3540),
      );
      if (rate !== 0.05)
        assert.notEqual(plot.find((row) => row.at === 3432)[field], rate);
      assert.deepEqual(samples, original);
    }
  }
});

test('an averaged bucket at the same timestamp cannot override the live meter', () => {
  assert.deepEqual(
    pulseChartRows(
      [point(90, 0.1), point(100, 0.15)],
      point(100, 0.24),
      0,
      100,
      12,
    ),
    [point(90, 0.1), point(100, 0.24)],
  );
});

test('late or newer history cannot move ahead of a live or paused meter snapshot', () => {
  assert.deepEqual(
    pulseChartRows(
      [point(90, 0.1), point(99, 0.15), point(100, 0.5), point(110, 0.7)],
      point(100, 0.24),
      0,
      120,
      12,
    ),
    [point(90, 0.1), point(99, 0.15), point(100, 0.24)],
  );
});

test('historical custom ranges keep their own readings and never get the current rate appended', () => {
  assert.deepEqual(
    pulseChartRows(
      [point(50, 0.1), point(60, 0.2), point(90, 0.3)],
      point(100, 0.9),
      50,
      60,
      12,
    ),
    [point(50, 0.1), point(60, 0.2)],
  );
});

test('a live point is available before history loads, while a missing reading stays missing', () => {
  assert.deepEqual(pulseChartRows([], point(100, 0), 0, 100, 5), [
    point(100, 0),
  ]);
  const rows = pulseChartRows(
    [point(98, 0.2), point(100, 0.3)],
    point(100, null),
    0,
    100,
    5,
  );
  assert.equal(smoothPulse(rows, 'rate60', 120, 5, 60).at(-1).rate60, null);
});

test('live anchoring and the unsmoothed tail never connect across a missing interval', () => {
  const rows = pulseChartRows(
    [point(0, 0.1), point(6, 0.2)],
    point(100, 0.24),
    0,
    100,
    12,
  );
  const plot = smoothPulse(rows, 'rate60', 120, 12, 60);
  assert.deepEqual(
    pulseSegments(plot, 'rate60', 12).map((run) => run.map((row) => row.at)),
    [[0, 6], [100]],
  );
});

test('model switches retain old readings, break lines and reset smoothing even with no time gap', () => {
  const samples = [
    { ...point(0, 0.1), sessionId: 1 },
    { ...point(1, 0.1), sessionId: 1 },
    { ...point(2, 0.8), sessionId: 2 },
    { ...point(3, 0.8), sessionId: 2 },
  ];
  const rows = pulseChartRows(
    samples,
    { ...point(4, 0.9), sessionId: 2 },
    0,
    4,
    5,
  );
  const smooth = smoothPulse(rows, 'rate60', 60, 5);
  assert.equal(smooth[2].rate60, 0.8);
  assert.deepEqual(
    pulseSegments(smooth, 'rate60', 5).map((s) => s.map((p) => p.sessionId)),
    [
      [1, 1],
      [2, 2, 2],
    ],
  );
  assert.equal(rows.at(-1).rate60, 0.9);
  assert.equal(samples.length, 4);
});

test('a new model warming up preserves earlier history while its live reading stays unavailable', () => {
  const rows = pulseChartRows(
    [{ ...point(100, 0.2), sessionId: 1 }],
    { ...point(103, null), sessionId: 2 },
    0,
    103,
    5,
  );
  assert.deepEqual(
    pulseSegments(smoothPulse(rows, 'rate60', 60, 5, 60), 'rate60', 5).map(
      (s) => s[0].sessionId,
    ),
    [1],
  );
  assert.equal(rows.at(-1).rate60, null);
});
