import test from 'node:test';
import assert from 'node:assert/strict';
import {
  validWeeklyNetwork,
  busiestCell,
  heatLevel,
  currentWeekHour,
} from '../lib/network-weekly.ts';

function report() {
  return {
    from: 0,
    to: 100,
    at: 100,
    timezone: 'America/Chicago',
    coverageStart: null,
    coverageEnd: null,
    hourlyHistoryStart: null,
    qualifiedDates: 0,
    observedHours: 0,
    expectedHours: 0,
    minimumCoverage: 0.8,
    coarseBuckets: 0,
    boundaryBuckets: 0,
    invalidBuckets: 0,
    cells: Array.from({ length: 168 }, (_, i) => ({
      day: Math.floor(i / 24),
      hour: i % 24,
      days: 0,
      possibleDays: 0,
      observedSeconds: 0,
      expectedSeconds: 0,
      qualifiedSeconds: 0,
      requests: 0,
      tokens: 0,
      requestsPerMinute: null,
      tokensPerSecond: null,
      dates: [],
    })),
  };
}
test('malformed optional history cannot crash the Network screen', () => {
  assert.equal(validWeeklyNetwork(report()), true);
  for (const mutate of [
    (r) => r.cells.pop(),
    (r) => (r.cells[0].day = 6),
    (r) => (r.cells[0].requestsPerMinute = NaN),
    (r) => (r.cells[0].dates = [{ date: 'bad' }]),
    (r) => (r.observedHours = undefined),
  ]) {
    const r = report();
    mutate(r);
    assert.equal(validWeeklyNetwork(r), false);
  }
});
test('missing readings differ from real zero; traffic and tokens have independent leaders', () => {
  const r = report();
  assert.equal(busiestCell(r.cells, 'requestsPerMinute'), undefined);
  r.cells[0].requestsPerMinute = 0;
  r.cells[0].tokensPerSecond = 500;
  r.cells[1].requestsPerMinute = 100;
  r.cells[1].tokensPerSecond = 0;
  assert.equal(busiestCell(r.cells, 'requestsPerMinute').hour, 1);
  assert.equal(busiestCell(r.cells, 'tokensPerSecond').hour, 0);
  assert.equal(heatLevel(null, 100), null);
  assert.equal(heatLevel(0, 0), 0);
  assert.equal(heatLevel(100, 100), 5);
});
test('current hour follows the viewer timezone across midnight and the week boundary', () => {
  const at = Date.parse('2026-09-28T04:59:59Z');
  assert.deepEqual(currentWeekHour(at, 'America/Chicago'), {
    day: 6,
    hour: 23,
    index: 167,
  });
  assert.deepEqual(currentWeekHour(at + 1000, 'America/Chicago'), {
    day: 0,
    hour: 0,
    index: 0,
  });
  assert.deepEqual(currentWeekHour(at, 'Asia/Kathmandu'), {
    day: 0,
    hour: 10,
    index: 10,
  });
  assert.deepEqual(currentWeekHour(at, 'Pacific/Honolulu'), {
    day: 6,
    hour: 18,
    index: 162,
  });
});
test('current hour skips spring DST and identifies both instances of the repeated autumn hour', () => {
  for (const [at, hour] of [
    ['2026-03-08T07:59:59Z', 1],
    ['2026-03-08T08:00:00Z', 3],
    ['2026-11-01T06:30:00Z', 1],
    ['2026-11-01T07:30:00Z', 1],
    ['2026-11-01T08:00:00Z', 2],
  ]) {
    assert.deepEqual(currentWeekHour(Date.parse(at), 'America/Chicago'), {
      day: 6,
      hour,
      index: 6 * 24 + hour,
    });
  }
});
