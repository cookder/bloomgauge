import test from 'node:test';
import assert from 'node:assert/strict';
import {
  averagePaceRun,
  detailPaceRun,
  displayPace,
  intervalPace,
  paceBucketSeconds,
} from '../lib/pulse-display.ts';
import { pulseSegments, pulseChartRows } from '../lib/pulse-smoothing.ts';
const p = (at, value, sessionId = 1) => ({
  at,
  rate60: value,
  rate300: value,
  sessionId,
});
test('trend weights elapsed time, not the density of arrivals', () => {
  const sparse = [p(0, 0), p(9, 0), p(10, 10)];
  const dense = [...Array.from({ length: 10 }, (_, i) => p(i, 0)), p(10, 10)];
  assert.equal(averagePaceRun(sparse, 'rate60', 30)[0].value, 0.5);
  assert.deepEqual(
    averagePaceRun(sparse, 'rate60', 30),
    averagePaceRun(dense, 'rate60', 30),
  );
});
test('complete time bins stay fixed as a rolling window moves', () => {
  const rows = Array.from({ length: 121 }, (_, i) => p(i, Math.sin(i / 10)));
  const a = averagePaceRun(rows, 'rate60', 30);
  const b = averagePaceRun(rows.slice(3), 'rate60', 30);
  assert.deepEqual(a.slice(1), b.slice(1));
  assert.equal(a[1].from, 30);
  assert.equal(a[1].to, 60);
});
test('resolution adapts to duration and available width, never below source resolution', () => {
  let last = 0;
  for (const span of [
    300, 900, 3600, 14400, 28800, 43200, 86400, 604800, 2592000,
  ]) {
    const narrow = paceBucketSeconds(span, 90),
      wide = paceBucketSeconds(span, 400);
    assert.ok(narrow >= wide);
    assert.ok(narrow >= last);
    last = narrow;
    assert.ok(span / narrow <= 30);
  }
  assert.ok(paceBucketSeconds(3600, 500, 100) >= 100);
});
test('neither averages nor detail bridge nulls, long gaps or session changes', () => {
  const rows = [
    p(0, 0),
    p(1, 1),
    p(2, null),
    p(3, 5),
    p(4, 5),
    p(100, 9),
    p(101, 10, 2),
    p(102, 12, 2),
  ];
  const segments = pulseSegments(rows, 'rate60', 5);
  const copy = structuredClone(rows);
  for (const detail of [false, true]) {
    const out = displayPace(segments, 'rate60', {
      start: 0,
      end: 110,
      width: 90,
      sourceBucket: 1,
      detail,
    });
    assert.equal(out.segments.length, 4);
    assert.ok(out.segments[0].every((p) => p.to <= 1));
    assert.ok(out.segments[1].every((p) => p.from >= 3 && p.to <= 4));
    assert.equal(out.segments.at(-1)[0].sessionId, 2);
  }
  assert.deepEqual(rows, copy);
});
test('zero, signed adjustments and isolated observations are preserved without overshoot', () => {
  const rows = [p(0, -1), p(10, 1), p(20, 0)];
  const out = averagePaceRun(rows, 'rate60', 10);
  assert.equal(out[0].value, 0);
  assert.equal(out[1].value, 0.5);
  assert.ok(out.every((p) => p.value >= p.low && p.value <= p.high));
  assert.equal(averagePaceRun([p(5, 0)], 'rate60', 60)[0].value, 0);
  assert.deepEqual(averagePaceRun([], 'rate60', 60), []);
});
test('detail reduction preserves short peaks and valleys in chronological order', () => {
  const rows = Array.from({ length: 101 }, (_, i) =>
    p(i, i === 35 ? 10 : i === 36 ? -2 : 1),
  );
  const out = detailPaceRun(rows, 'rate60', 0, 100, 10);
  assert.ok(out.some((p) => p.value === 10));
  assert.ok(out.some((p) => p.value === -2));
  assert.equal(out[0].at, 0);
  assert.equal(out.at(-1).at, 100);
  assert.ok(out.length <= 44);
  assert.ok(out.every((p, i) => !i || p.at > out[i - 1].at));
});
test('both rate windows retain exact meter anchors separately from historical averages', () => {
  for (const value of [0.24, 24, 0, -0.03])
    for (const field of ['rate60', 'rate300']) {
      const rows = pulseChartRows(
        [p(90, 0.1), p(96, 0.1), p(100, 99)],
        p(100, value),
        0,
        100,
        10,
      );
      const out = displayPace(pulseSegments(rows, field, 10), field, {
        start: 0,
        end: 100,
        width: 80,
        sourceBucket: 1,
        detail: false,
      });
      assert.equal(rows.at(-1)[field], value);
      assert.ok(out.segments.at(-1).at(-1).at < 100); // exact tip is never replaced with the mean
    }
});

test('time-bin averages preserve the integral and never cross gaps when a run straddles bins', () => {
  const rows = [p(13, 0), p(27, 2), p(31, -1), p(58, 3), p(72, 0)];
  const area = rows
    .slice(1)
    .reduce(
      (sum, b, i) =>
        sum + ((rows[i].rate60 + b.rate60) / 2) * (b.at - rows[i].at),
      0,
    );
  const bins = averagePaceRun(rows, 'rate60', 20);
  assert.ok(
    Math.abs(
      bins.reduce((sum, b) => sum + b.value * (b.to - b.from), 0) - area,
    ) < 1e-9,
  );
  assert.equal(bins[0].from, 13);
  assert.equal(bins.at(-1).to, 72);
  assert.equal(
    bins.reduce((sum, b) => sum + b.to - b.from, 0),
    59,
  );
});

const intervalOptions = { start: 0, end: 3600, width: 400, sourceBucket: 1 };
test('hour overview combines fragmented observations without filling missing seconds', () => {
  const runs = [
    [p(0, 1), p(10, 1)],
    [p(30, 3), p(50, 3)],
  ];
  const out = intervalPace(runs, 'rate60', intervalOptions);
  assert.equal(out.points.length, 1);
  assert.equal(out.points[0].value, 70 / 30);
  assert.equal(out.points[0].observedSeconds, 30);
  assert.equal(out.points[0].coverage, 0.6);
  assert.deepEqual(
    out.coverage.map((p) => [p.from, p.to]),
    [
      [0, 10],
      [30, 50],
    ],
  );
  assert.equal(out.observedSeconds, 30);
  assert.equal(out.points[0].low, 1);
  assert.equal(out.points[0].high, 3);
});
test('intervals preserve time-weighted area, zeros, signed adjustments and peaks', () => {
  const runs = [
    [p(10, -1), p(50, 2), p(80, 0), p(110, 0)],
    [p(180, 0), p(200, 0)],
  ];
  const out = intervalPace(runs, 'rate60', intervalOptions);
  const area = runs.reduce(
    (sum, run) =>
      sum +
      run
        .slice(1)
        .reduce(
          (n, b, i) =>
            n + ((run[i].rate60 + b.rate60) / 2) * (b.at - run[i].at),
          0,
        ),
    0,
  );
  assert.ok(
    Math.abs(
      out.points.reduce((n, p) => n + p.value * p.observedSeconds, 0) - area,
    ) < 1e-9,
  );
  assert(out.points.some((p) => p.value === 0));
  assert(out.points.some((p) => p.low === -1));
  assert(out.points.some((p) => p.high === 2));
});
test('one interval never mixes two model sessions', () => {
  const out = intervalPace(
    [
      [p(0, 1), p(10, 1)],
      [p(20, 9, 2), p(30, 9, 2)],
    ],
    'rate60',
    intervalOptions,
  );
  assert.equal(out.points.length, 2);
  assert.deepEqual(
    out.points.map((p) => p.value),
    [1, 9],
  );
  assert(out.points[0].to < out.points[1].from);
  assert.equal(out.observedSeconds, 20);
});
test('isolated observations have unknown duration and never count as full coverage', () => {
  const out = intervalPace([[p(20, 5)]], 'rate60', intervalOptions);
  assert.equal(out.points[0].value, 5);
  assert.equal(out.points[0].kind, 'reading');
  assert.equal(out.points[0].coverage, 0);
  assert.equal(out.observedSeconds, 0);
  assert.deepEqual(out.coverage, []);
});
test('empty history stays empty and entirely unobserved', () => {
  const out = intervalPace([], 'rate60', intervalOptions);
  assert.deepEqual(out.points, []);
  assert.equal(out.observedSeconds, 0);
});
test('nonnegative recorded pace cannot gain a negative extreme from floating point interpolation', () => {
  for (const v of [0.000479, 1.178332715, 2.529974337127956]) {
    const out = averagePaceRun([p(13.3, v), p(21.9, 0)], 'rate60', 10);
    assert(out.every((p) => p.low >= 0 && p.high <= v && p.value >= 0));
  }
});
test('hour bars adapt to phone width and preserve both rate windows without mutating input', () => {
  const runs = [
    Array.from({ length: 601 }, (_, i) => ({
      ...p(i * 6, i % 17 === 0 ? 2 : 0),
      rate300: 0.25,
    })),
  ];
  const copy = structuredClone(runs);
  const phone = intervalPace(runs, 'rate60', {
    ...intervalOptions,
    width: 120,
  });
  const desktop = intervalPace(runs, 'rate300', intervalOptions);
  assert(phone.bucketSeconds >= desktop.bucketSeconds);
  assert(phone.points.length <= 25);
  assert(desktop.points.every((p) => p.value === 0.25));
  assert.deepEqual(runs, copy);
});
