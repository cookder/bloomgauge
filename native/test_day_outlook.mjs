import test from 'node:test';
import assert from 'node:assert/strict';
import { dayOutlook } from '../lib/day-outlook.ts';

const start = Date.UTC(2026, 8, 21) / 1000,
  now = start + 18 * 3600;
function fixture({ daySeconds = 86400, asOf = now - 60, rate = 0.15 } = {}) {
  const day = {
    date: '2026-09-21',
    at: start,
    end: start + daySeconds,
    from: start,
    to: now,
    usd: 3.05,
    inferenceUsd: 2.6525,
    baseUsd: 0.3975,
    covered: true,
    status: 'today',
  };
  const previous = Array.from({ length: 4 }, (_, i) => ({
    ...day,
    date: `2026-09-${20 - i}`,
    at: start - (i + 1) * 86400,
    end: start - i * 86400,
    from: start - (i + 1) * 86400,
    to: start - i * 86400,
    usd: 2.7,
    inferenceUsd: 2.22,
    baseUsd: 0.48,
    status: 'complete',
  }));
  const history = {
    at: now,
    from: start - 30 * 86400,
    to: now,
    timezone: 'UTC',
    model: null,
    includesBase: true,
    historyStart: start - 30 * 86400,
    days: [...previous.reverse(), day],
    models: ['gemma'],
    chartTruncated: false,
    usd: 13.85,
    inferenceUsd: 11.5325,
    baseUsd: 2.3175,
    completeDays: 4,
    averageDayUsd: 2.7,
    scope: 'This Mac inference + account base rewards',
  };
  const projection = {
    status: 'ready',
    models: ['gemma'],
    asOf,
    hours: 177,
    days: 14,
    ratePerHour: rate,
    detail: 'Verified model history',
    points: Array.from({ length: 98 }, (_, i) => ({
      at: asOf + i * 900,
      additional: (rate * i) / 4,
    })),
  };
  return { history, projection, day };
}
const near = (actual, expected) =>
  assert.ok(Math.abs(actual - expected) < 1e-9, `${actual} != ${expected}`);
test('EOD adds only remaining inference to actual credits and batches base separately', () => {
  const { history, projection } = fixture();
  const out = dayOutlook(history, projection, now);
  assert.equal(out.status, 'ready');
  near(out.confirmed, 3.05);
  near(out.additionalInference, 0.9);
  near(out.additionalBase, 0.0825);
  near(out.total, 4.0325);
  assert.equal(out.baseEstimated, true);
  assert.equal(out.baseDays, 4);
  assert.equal(out.end, start + 86400);
  // Immutable inputs: no forecast dollars enter confirmed cells.
  assert.equal(history.days.at(-1).usd, 3.05);
  assert.equal(projection.points[0].additional, 0);
});
test('independently refreshed snapshot avoids duplicate past credit or a short missing forecast gap', () => {
  for (const offset of [-120, -60, 0, 20, 90]) {
    const { history, projection } = fixture({ asOf: now + offset });
    near(dayOutlook(history, projection, now).additionalInference, 0.9);
  }
});
test('uses historical changing future rates rather than multiplying a short pulse spike all day', () => {
  const { history, projection } = fixture({ asOf: now });
  projection.ratePerHour = 4;
  projection.points = Array.from({ length: 25 }, (_, i) => ({
    at: now + i * 3600,
    additional: i === 0 ? 0 : 0.4 + (i - 1) * 0.1,
  }));
  const out = dayOutlook(history, projection, now);
  near(out.additionalInference, 0.9);
  assert.ok(out.total < 5);
});
test('gaps, partial date selection and stale data cannot produce a full-day total', () => {
  const variations = [
    (h) => (h.days.at(-1).covered = false),
    (h) => (h.days.at(-1).status = 'unknown'),
    (h) => (h.days.at(-1).from = start + 3600),
    (h) => (h.days.at(-1).to = now - 300),
  ];
  for (const change of variations) {
    const { history, projection } = fixture();
    change(history);
    assert.equal(dayOutlook(history, projection, now).total, null);
  }
  const { history, projection } = fixture();
  assert.equal(dayOutlook(history, projection, now + 181).status, 'stale');
  assert.equal(dayOutlook(history, projection, now, false).total, null);
  assert.equal(
    dayOutlook(history, { ...projection, asOf: now - 181 }, now).status,
    'stale',
  );
});
test('model filters remain scoped; no solo forecast gets assigned to a different model or split out of a pair', () => {
  const { history, projection } = fixture();
  history.model = 'other';
  history.includesBase = false;
  assert.equal(dayOutlook(history, projection, now).total, null);
  history.model = 'gemma';
  history.days.at(-1).usd = 2.6525;
  near(dayOutlook(history, projection, now).total, 3.5525);
  assert.equal(
    dayOutlook(history, { ...projection, models: ['gemma', 'oss'] }, now).total,
    null,
  );
  history.model = '@inference';
  near(
    dayOutlook(history, { ...projection, models: ['gemma', 'oss'] }, now).total,
    3.5525,
  );
});
test('insufficient or incomplete base history is explicitly excluded; signed actual corrections remain', () => {
  const { history, projection } = fixture();
  history.days[0].status = 'unknown';
  history.days[1].status = 'partial';
  const out = dayOutlook(history, projection, now);
  assert.equal(out.baseEstimated, false);
  near(out.additionalBase, 0);
  near(out.total, 3.95);
  history.days.at(-1).usd = -0.3;
  near(dayOutlook(history, projection, now).total, 0.6);
});
test('a base reward batch above historical full-day amount is never counted again', () => {
  const { history, projection } = fixture();
  history.days.at(-1).baseUsd = 1;
  const out = dayOutlook(history, projection, now);
  near(out.additionalBase, 0);
  near(out.total, 3.95);
});
test('local midnight and actual 23/25-hour day boundaries determine the horizon', () => {
  for (const seconds of [23 * 3600, 24 * 3600, 25 * 3600]) {
    const { history, projection } = fixture({ daySeconds: seconds });
    const out = dayOutlook(history, projection, now);
    near(out.additionalInference, (seconds / 3600 - 18) * 0.15);
    near(out.additionalBase, (0.48 * seconds) / 86400 - 0.3975);
    assert.equal(out.end, start + seconds);
  }
  const { history, projection } = fixture();
  assert.equal(dayOutlook(history, projection, start + 86400).total, null);
  // Freeze is a labeled historical view, not a new projection after midnight.
  assert.equal(
    dayOutlook(history, projection, start + 86400, true, true).status,
    'ready',
  );
});
test('unsupported, cold, insufficient or malformed model forecasts remain unavailable', () => {
  const { history, projection } = fixture();
  for (const status of ['unavailable', 'learning', 'stale'])
    assert.equal(
      dayOutlook(history, { ...projection, status }, now).total,
      null,
    );
  for (const p of [
    null,
    { ...projection, hours: 0.1 },
    { ...projection, models: [] },
    { ...projection, points: [] },
    {
      ...projection,
      points: [
        { at: now, additional: 0 },
        { at: now, additional: 1 },
      ],
    },
    {
      ...projection,
      points: [
        { at: now, additional: 1 },
        { at: now + 86400, additional: 2 },
      ],
    },
    {
      ...projection,
      points: [
        { at: now - 60, additional: 0 },
        { at: now + 86400, additional: NaN },
      ],
    },
  ])
    assert.equal(dayOutlook(history, p, now).total, null);
  assert.equal(
    dayOutlook(
      history,
      { ...projection, points: projection.points.slice(0, 3) },
      now,
    ).total,
    null,
  );
});
test('verified quiet zero remains an estimate of zero, not missing data', () => {
  const { history, projection } = fixture({ rate: 0 });
  history.model = '@inference';
  history.includesBase = false;
  history.days.at(-1).usd = 0;
  const out = dayOutlook(history, projection, now);
  assert.equal(out.status, 'ready');
  assert.equal(out.total, 0);
});
