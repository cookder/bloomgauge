import test from 'node:test';
import assert from 'node:assert/strict';
import {
  pulseEarningsBars,
  readPulseGraphStyle,
} from '../lib/pulse-earnings-bars.ts';
const report = (values = [0.12, 0.24]) => ({
  from: 0,
  to: 3600,
  bucketSeconds: 60,
  series: values.map((_, i) => ({ id: `model:${i}`, name: `Model ${i}` })),
  points: Array.from({ length: 60 }, (_, i) => ({
    from: i * 60,
    to: (i + 1) * 60,
    values: [...values],
    coverageFraction: 1,
  })),
});
test('last hour uses30 two-minute bars and conserves credited dollars per model', () => {
  const r = report();
  r.points[1].values = [0.36, -0.12];
  const b = pulseEarningsBars(r);
  assert.equal(b.bucketSeconds, 120);
  assert.equal(b.points.length, 30);
  b.points[0].values.forEach((v, i) =>
    assert(Math.abs(v - [0.24, 0.06][i]) < 1e-12),
  );
  for (let i = 0; i < 2; i++) {
    const original = r.points.reduce(
        (n, p) => n + (p.values[i] * (p.to - p.from)) / 3600,
        0,
      ),
      drawn = b.points.reduce(
        (n, p) => n + (p.values[i] * (p.to - p.from)) / 3600,
        0,
      );
    assert(Math.abs(original - drawn) < 1e-12);
  }
});
test('unknown money and missing source intervals never become zero', () => {
  const r = report([0, 0.1]);
  r.points[0].values[1] = null;
  r.points.splice(3, 1);
  const b = pulseEarningsBars(r);
  assert.deepEqual(b.points[0].values, [0, null]);
  assert.deepEqual(b.points[1].values, [null, null]);
  assert.equal(b.points[1].coverageFraction, 0.5);
});
test('partial boundary bars use their actual elapsed durations and retain signed values', () => {
  const r = report([-0.12, 0.2]);
  r.from = 30;
  r.to = 3570;
  r.points[0].from = 30;
  r.points.at(-1).to = 3570;
  const b = pulseEarningsBars(r);
  assert.equal(b.points[0].from, 30);
  assert.equal(b.points[0].to, 120);
  assert.equal(b.points.at(-1).to, 3570);
  b.points[0].values.forEach((v, i) =>
    assert(Math.abs(v - [-0.12, 0.2][i]) < 1e-12),
  );
  b.points
    .at(-1)
    .values.forEach((v, i) => assert(Math.abs(v - [-0.12, 0.2][i]) < 1e-12));
});
test('graph preference defaults safely and retains all supported choices', () => {
  for (const value of [undefined, null, 'bars', 'corrupt', {}, ''])
    assert.equal(readPulseGraphStyle(value), 'earnings');
  for (const value of ['earnings', 'line', 'range'])
    assert.equal(readPulseGraphStyle(value), value);
});
