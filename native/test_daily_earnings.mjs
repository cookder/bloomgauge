import test from 'node:test';
import assert from 'node:assert/strict';
import {
  dailyBounds,
  dayTone,
  earningsTone,
  validDailyEarnings,
} from '../lib/daily-earnings.ts';
const day = {
  date: '2026-09-01',
  at: 100,
  end: 86500,
  from: 100,
  to: 86500,
  usd: 3,
  inferenceUsd: 2.8,
  baseUsd: 0.2,
  covered: true,
  status: 'complete',
};
const report = {
  at: 90000,
  from: 100,
  to: 86500,
  usd: 3,
  inferenceUsd: 2.8,
  baseUsd: 0.2,
  completeDays: 1,
  averageDayUsd: null,
  historyStart: 100,
  model: null,
  timezone: 'UTC',
  scope: 'Mac + base',
  includesBase: true,
  chartTruncated: false,
  models: ['a'],
  days: [day],
};
test('daily colors preserve missing coverage and goal bands', () => {
  for (const [usd, tone] of [
    [0, 'quiet'],
    [1.5, 'steady'],
    [2.49, 'steady'],
    [2.5, 'green'],
    [2.99, 'green'],
    [3, 'purple'],
    [3.05, 'purple'],
    [3.99, 'purple'],
    [4, 'gold'],
    [4.47, 'gold'],
    [-1, 'quiet'],
  ])
    assert.equal(dayTone({ ...day, usd }), tone);
  assert.equal(dayTone({ ...day, usd: 9, status: 'unknown' }), 'unknown');
  for (const usd of [1.5, 2.5, 3, 4])
    assert.equal(earningsTone(usd / 24, 1), earningsTone(usd));
  assert.equal(earningsTone(null), 'unknown');
  assert.equal(earningsTone(NaN), 'unknown');
  assert.equal(earningsTone(3, 0), 'unknown');
});
test('calendar presets include today and midnight boundaries', () => {
  const now = new Date(2026, 8, 17, 10, 23),
    r = dailyBounds({ preset: '7d' }, now);
  assert.equal(new Date(r.start * 1000).getDate(), 11);
  assert.equal(new Date(r.start * 1000).getHours(), 0);
  assert.equal(r.end, now.getTime() / 1000);
  assert.equal(dailyBounds({ preset: 'all' }, now).start, 0);
  assert.deepEqual(dailyBounds({ preset: 'custom', start: 10, end: 20 }, now), {
    start: 10,
    end: 20,
  });
});
test('daily responses reject malformed cells and preserve zero/negative corrections', () => {
  assert.ok(validDailyEarnings(report));
  assert.ok(validDailyEarnings({ ...report, days: [{ ...day, usd: -0.1 }] }));
  for (const change of [
    { days: [null] },
    { days: [{ ...day, usd: '3' }] },
    { days: [{ ...day, status: 'fake' }] },
    { days: [{ ...day, to: 100 }] },
    { models: [{}] },
    { usd: NaN },
    { includesBase: 0 },
    { averageDayUsd: 'x' },
  ])
    assert.equal(validDailyEarnings({ ...report, ...change }), false);
});
