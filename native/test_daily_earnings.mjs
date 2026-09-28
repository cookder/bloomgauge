import test from 'node:test';
import assert from 'node:assert/strict';
import {
  dailyBounds,
  dayTone,
  earningsTiers,
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
const days = (values, status = 'complete') =>
  values.map((usd, i) => ({ ...day, at: 100 + i * 86400, usd, status }));
test('day colours are relative to this Mac and neutral until 7 complete days', () => {
  // Six complete days (plus partial/unknown ones) are not a scale yet.
  const six = [...days([1, 2, 3, 4, 5, 6]), ...days([9, 9], 'unknown')];
  assert.equal(earningsTiers(six), null);
  assert.equal(dayTone({ ...day, usd: 6 }, earningsTiers(six)), 'neutral');
  assert.equal(earningsTone(3), 'neutral');
  // Ten complete days from $1 to $10: quartiles and the 90th percentile.
  const tiers = earningsTiers(days([10, 1, 9, 2, 8, 3, 7, 4, 6, 5]));
  assert.deepEqual(tiers, {
    steady: 3.25,
    green: 5.5,
    purple: 7.75,
    gold: 9.1,
    days: 10,
  });
  for (const [usd, tone] of [
    [0, 'quiet'],
    [-1, 'quiet'],
    [3.2, 'quiet'],
    [3.25, 'steady'],
    [5.5, 'green'],
    [7.75, 'purple'],
    [9.1, 'gold'],
  ])
    assert.equal(dayTone({ ...day, usd }, tiers), tone);
  assert.equal(
    dayTone({ ...day, usd: 9, status: 'unknown' }, tiers),
    'unknown',
  );
  // A small Mac's $1.20 day can be outstanding; an Ultra's $4 day can be quiet.
  const small = earningsTiers(days([0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1]));
  assert.equal(earningsTone(1.2, 24, small), 'gold');
  const ultra = earningsTiers(days([6, 7, 8, 9, 10, 11, 12]));
  assert.equal(earningsTone(4, 24, ultra), 'quiet');
  // Only the last 30 complete days count; no spread means no scale.
  const old = days([...Array(30).fill(100), ...Array(30).fill(1)]);
  assert.equal(earningsTiers(old), null);
  assert.equal(earningsTiers(days(Array(10).fill(0))), null);
  // Hour estimates use the same scale divided by 24.
  for (const usd of [3.25, 5.5, 7.75, 9.1])
    assert.equal(
      earningsTone(usd / 24, 1, tiers),
      earningsTone(usd, 24, tiers),
    );
  assert.equal(earningsTone(null, 24, tiers), 'unknown');
  assert.equal(earningsTone(NaN, 24, tiers), 'unknown');
  assert.equal(earningsTone(3, 0, tiers), 'unknown');
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
