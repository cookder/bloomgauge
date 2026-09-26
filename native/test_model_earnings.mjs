import assert from 'node:assert/strict';
import test from 'node:test';
import { registerHooks } from 'node:module';
import {
  cumulativeEarnings,
  projectedCumulative,
} from '../lib/cumulative-earnings.ts';

// The app bundler resolves extensionless TS imports; Node's test runner needs
// the same resolution for this helper's sole local import.
const loader = registerHooks({
  resolve(specifier, context, next) {
    return next(
      specifier === './cumulative-earnings' ? `${specifier}.ts` : specifier,
      context,
    );
  },
});
const {
  cumulativeForModels,
  modelEarnings,
  modelColor,
  earningsShares,
  UNATTRIBUTED,
  ALL_EARNINGS,
  filterEarningsHours,
  earningsMetrics,
  filterModelProjection,
  projectedEarningsHour,
} = await import('../lib/model-earnings.ts');
loader.deregister();
const HOUR = 1788742800;
const sum = (values) =>
  Math.round(Object.values(values).reduce((a, b) => a + b, 0) * 1e6) / 1e6;

test('model shares use confirmed microdollars including rewards and unassigned history', () => {
  const selected = modelEarnings(
    [{ at: HOUR, usd: 1, categories: { a: 0.6, b: 0.3, base_reward: 0.05 } }],
    HOUR + 3600,
  );
  const amounts = selected.series.map((s) => s.usd);
  const shares = earningsShares(amounts);
  assert.equal(
    shares.reduce((a, b) => a + b, 0),
    100,
  );
  assert.deepEqual(
    Object.fromEntries(selected.series.map((s, i) => [s.model, shares[i]])),
    { [UNATTRIBUTED]: 5, a: 60, b: 30, base_reward: 5 },
  );
  assert.deepEqual(earningsShares([0.000001, 0.000003, 0]), [25, 75, 0]);
  assert.deepEqual(earningsShares([0.000001 + 0.000002, 0.000001]), [75, 25]);
});

test('zero or signed earnings do not become fabricated positive proportions', () => {
  for (const amounts of [
    [],
    [0, 0],
    [1, -0.1],
    [-1, 1],
    [-0.1, -0.2],
    [NaN, 1],
    [Infinity],
  ])
    assert.equal(earningsShares(amounts), null);
  const models = [
    'gemma-4-26b-qat-4bit',
    'gpt-oss-20b',
    'qwen3.6-35b-a3b-vl-mtp-mxfp8',
    'EigenLabs/Qwen3.8-27B-4bit-mtp',
    'qwen3.5-35b-a3b',
    'qwen3-vl-30b-a3b-instruct',
  ];
  assert.equal(new Set(models.map(modelColor)).size, models.length);
});

test('model filter uses its own confirmed earnings and job count for every metric', () => {
  const input = [
    {
      at: HOUR,
      usd: 1.1,
      jobs: 11,
      categories: { a: 0.6, b: 0.4, base_reward: 0.1 },
      categoryJobs: { a: 3, b: 8, base_reward: 1 },
    },
  ];
  const rows = earningsMetrics(filterEarningsHours(input, 'a'), HOUR + 1800);
  assert.equal(rows[0].usd, 0.6);
  assert.equal(rows[0].jobs, 3);
  assert.equal(rows[0].perJob, 0.6 / 3);
  assert.equal(rows[0].perMinute, 0.6 / 30);
  assert.equal(rows[0].perHour, 1.2);
  assert.equal(cumulativeEarnings(rows, HOUR + 1800).at(-1).cumulative, 0.6);
  assert.deepEqual(filterEarningsHours(input, ALL_EARNINGS), input);
  assert.deepEqual(input[0].categories, { a: 0.6, b: 0.4, base_reward: 0.1 });
});

test('selected rates include observed idle hours and preserve genuinely missing hours', () => {
  const input = [
    {
      at: HOUR,
      usd: 0.6,
      jobs: 3,
      categories: { a: 0.6 },
      categoryJobs: { a: 3 },
    },
    {
      at: HOUR + 3600,
      usd: 0.8,
      jobs: 4,
      categories: { b: 0.8 },
      categoryJobs: { b: 4 },
    },
    {
      at: HOUR + 10800,
      usd: 0.3,
      jobs: 1,
      categories: { a: 0.3 },
      categoryJobs: { a: 1 },
    },
  ];
  const rows = earningsMetrics(filterEarningsHours(input, 'a'), HOUR + 12600);
  assert.deepEqual(
    rows.map((h) => h.perHour),
    [0.6, 0, 0.6],
  );
  assert.deepEqual(
    rows.map((h) => h.jobs),
    [3, 0, 1],
  );
  assert.equal(
    Math.round(
      (rows.reduce((s, h) => s + h.usd, 0) /
        (rows.reduce((s, h) => s + h.seconds, 0) / 3600)) *
        1e6,
    ) / 1e6,
    0.36,
  );
  assert.equal(
    cumulativeEarnings(rows, HOUR + 12600).filter((p) => p.cumulative === null)
      .length,
    1,
  );
  assert.deepEqual(
    earningsMetrics(filterEarningsHours(input.slice(2), 'a'), HOUR + 12600).map(
      (h) => h.usd,
    ),
    [0.3],
  );
});

test('old or invalid category job counts remain unknown instead of using all-model jobs', () => {
  for (const categoryJobs of [undefined, {}, { a: -1 }, { a: 1.5 }]) {
    const rows = earningsMetrics(
      filterEarningsHours(
        [
          {
            at: HOUR,
            usd: 0.6,
            jobs: 10,
            categories: { a: 0.6 },
            categoryJobs,
          },
        ],
        'a',
      ),
      HOUR + 3600,
    );
    assert.equal(rows[0].jobs, null);
    assert.equal(rows[0].perJob, null);
  }
  const zeroPaid = {
    at: HOUR,
    usd: 0.6,
    jobs: 10,
    categories: { a: 0.6 },
    categoryJobs: {},
  };
  assert.equal(filterEarningsHours([zeroPaid], 'b')[0].jobs, null);
});

test('coverage gaps retain confirmed money but suspend affected hourly and period rates', () => {
  const input = [
    {
      at: HOUR,
      usd: 0.0006,
      jobs: 3,
      categories: { a: 0.0006 },
      categoryJobs: { a: 3 },
    },
    {
      at: HOUR + 3600,
      usd: 0.1,
      jobs: 2,
      categories: { a: 0.1 },
      categoryJobs: { a: 2 },
    },
  ];
  const gaps = [{ start: HOUR + 100, end: HOUR + 140 }];
  const tail = earningsMetrics(
    filterEarningsHours(input.slice(0, 1), 'a'),
    HOUR + 160,
    HOUR,
    gaps,
  );
  assert.equal(tail[0].perHour, null);
  assert.equal(tail[0].perMinute, null);
  assert.equal(tail[0].usd, 0.0006);
  assert.equal(tail[0].jobs, 3);
  assert.equal(tail[0].perJob, 0.0006 / 3);
  assert.equal(cumulativeEarnings(tail, HOUR + 160).at(-1).cumulative, 0.0006);
  const mixed = earningsMetrics(input, HOUR + 7200, HOUR, gaps);
  assert.deepEqual(
    mixed.map((h) => h.rateComplete),
    [false, true],
  );
  assert.equal(mixed[1].perHour, 0.1);
  assert.equal(
    mixed.every((h) => h.rateComplete),
    false,
  );
  assert.equal(
    earningsMetrics(input.slice(1), HOUR + 7200, HOUR, [
      { start: HOUR, end: HOUR + 3600 },
    ])[0].perHour,
    0.1,
  );
  assert.equal(
    earningsMetrics(input, HOUR + 7200, HOUR, [], true)[1].perHour,
    null,
  );
});

test('rewards and unattributed remainders stay separate and never acquire model jobs', () => {
  const input = [
    {
      at: HOUR,
      usd: 1.1,
      jobs: 3,
      categories: { a: 0.6, base_reward: 0.1 },
      categoryJobs: { a: 3, base_reward: 2 },
    },
  ];
  assert.equal(filterEarningsHours(input, 'a')[0].usd, 0.6);
  assert.equal(filterEarningsHours(input, UNATTRIBUTED)[0].usd, 0.4);
  assert.equal(filterEarningsHours(input, UNATTRIBUTED)[0].jobs, null);
  assert.equal(filterEarningsHours(input, 'base_reward')[0].jobs, null);
  assert.equal(filterEarningsHours(input, 'base_reward')[0].usd, 0.1);
  assert.equal(filterEarningsHours(input, 'new')[0].usd, 0);
  assert.deepEqual(filterEarningsHours([], 'a'), []);
});

test('filtered projections touch only their matching solo-model curve', () => {
  const forecast = {
    status: 'ready',
    asOf: HOUR + 3600,
    models: ['a'],
    points: [
      { at: HOUR + 3600, additional: 0 },
      { at: HOUR + 7200, additional: 0.2 },
    ],
  };
  const input = [
    {
      at: HOUR,
      usd: 5,
      jobs: 10,
      categories: { a: 3, b: 1, base_reward: 1 },
      categoryJobs: { a: 6, b: 4 },
    },
  ];
  const actual = cumulativeEarnings(
    filterEarningsHours(input, 'a'),
    HOUR + 3600,
  );
  const projected = projectedCumulative(
    actual,
    filterModelProjection(forecast, 'a'),
    HOUR + 7200,
  );
  assert.equal(projected.find((p) => p.predicted != null).predicted, 3);
  assert.equal(projected.at(-1).predicted, 3.2);
  assert.equal(filterModelProjection(forecast, 'b'), undefined);
  assert.equal(
    filterModelProjection({ ...forecast, models: ['a', 'b'] }, 'a'),
    undefined,
  );
  assert.equal(filterModelProjection(forecast, 'base_reward'), undefined);
  assert.equal(filterModelProjection(forecast, ALL_EARNINGS), forecast);
});

test('model filters preserve literal IDs, adjustments, and source records', () => {
  const input = [
    {
      at: HOUR,
      usd: 0.9,
      jobs: 2,
      categories: { 'EigenLabs/Qwen3.8-27B-4bit-mtp': 1, constructor: -0.1 },
      categoryJobs: { 'EigenLabs/Qwen3.8-27B-4bit-mtp': 1, constructor: 1 },
    },
  ];
  assert.equal(filterEarningsHours(input, 'constructor')[0].usd, -0.1);
  assert.equal(
    filterEarningsHours(input, 'EigenLabs/Qwen3.8-27B-4bit-mtp')[0].usd,
    1,
  );
  assert.equal(filterEarningsHours(input, 'toString')[0].usd, 0);
  assert.equal(
    modelEarnings(filterEarningsHours(input, 'constructor'), HOUR + 3600)
      .series[0].model,
    'constructor',
  );
});

test('contradictory model job counts do not generate a filtered job average', () => {
  for (const categoryJobs of [{ a: 12 }, { a: 3, b: 9 }]) {
    const hour = {
      at: HOUR,
      usd: 1,
      jobs: 11,
      categories: { a: 0.6, b: 0.4 },
      categoryJobs,
    };
    assert.equal(filterEarningsHours([hour], 'a')[0].jobs, null);
  }
});

test('model hourly forecasts require a real covered current-hour anchor', () => {
  const hours = [{ at: HOUR, usd: 0.3, categories: { a: 0.3 } }];
  const projection = {
    status: 'ready',
    asOf: HOUR + 1800,
    models: ['a'],
    points: [
      { at: HOUR + 1800, additional: 0 },
      { at: HOUR + 3600, additional: 0.2 },
    ],
  };
  const window = {
    start: HOUR,
    end: HOUR + 3600,
    at: HOUR + 1800,
    observedAt: HOUR + 1800,
    sourceReady: true,
  };
  assert.equal(projectedEarningsHour(hours, projection, window), 0.5);
  assert.equal(
    projectedEarningsHour(hours, projection, { ...window, sourceReady: false }),
    null,
  );
  assert.equal(projectedEarningsHour([], projection, window), null);
  assert.equal(
    projectedEarningsHour(hours, { ...projection, asOf: HOUR - 10 }, window),
    null,
  );
  assert.equal(
    projectedEarningsHour(hours, projection, {
      ...window,
      observedAt: HOUR - 10,
    }),
    null,
  );
  assert.equal(
    projectedEarningsHour(hours, projection, { ...window, at: HOUR + 2000 }),
    null,
  );
});

test('hourly and cumulative model contributions exactly match the recorded totals', () => {
  const hours = [
    {
      at: HOUR,
      usd: 0.300001,
      categories: { a: 0.1, b: 0.2, base_reward: 0.000001 },
    },
    {
      at: HOUR + 3600,
      usd: 0.5,
      categories: { a: 0.25, b: 0.15, base_reward: 0.1 },
    },
  ];
  const grouped = modelEarnings(hours, HOUR + 7200);
  assert.deepEqual(
    grouped.hourly.map((p) => sum(p.byModel)),
    [0.300001, 0.5],
  );
  assert.deepEqual(
    grouped.cumulative.map((p) => sum(p.byModel)),
    [0, 0.300001, 0.800001],
  );
  assert.equal(grouped.series.find((s) => s.model === 'a').usd, 0.35);
  assert.equal(
    grouped.series.find((s) => s.model === 'base_reward').usd,
    0.100001,
  );
});

test('missing category history is unassigned instead of attributed to the current model', () => {
  const hours = [
    { at: HOUR, usd: 1, categories: {} },
    { at: HOUR + 3600, usd: 0.4, categories: { a: 0.3 } },
  ];
  const grouped = modelEarnings(hours, HOUR + 7200);
  assert.equal(grouped.series.find((s) => s.model === UNATTRIBUTED).usd, 1.1);
  assert.equal(grouped.series.find((s) => s.model === 'a').usd, 0.3);
  assert.equal(sum(grouped.cumulative.at(-1).byModel), 1.4);
});

test('range changes reset every model baseline and preserve its color', () => {
  const a = { at: HOUR, usd: 1, categories: { 'gemma-4-26b-qat-4bit': 1 } };
  const b = {
    at: HOUR + 3600,
    usd: 2,
    categories: { 'gemma-4-26b-qat-4bit': 2 },
  };
  const all = modelEarnings([a, b], HOUR + 7200);
  const selected = modelEarnings([b], HOUR + 7200);
  assert.equal(all.series[0].color, selected.series[0].color);
  assert.deepEqual(
    selected.cumulative.map((p) => sum(p.byModel)),
    [0, 2],
  );
  assert.notEqual(
    modelColor('gemma-4-26b-qat-4bit'),
    modelColor('gpt-oss-20b'),
  );
});

test('each model curve keeps the same gaps and partial-hour timestamps as the total curve', () => {
  const hours = [
    { at: HOUR + 7200, usd: 2, categories: { b: 2 } },
    { at: HOUR, usd: 1, categories: { a: 1 } },
  ];
  const combined = cumulativeEarnings(hours, HOUR + 7500, HOUR + 60);
  const grouped = modelEarnings(hours, HOUR + 7500, HOUR + 60);
  assert.deepEqual(
    grouped.cumulative.map((p) => p.at),
    combined.map((p) => p.at),
  );
  grouped.cumulative.forEach((p, i) => {
    if (combined[i].cumulative == null)
      assert.ok(Object.values(p.byModel).every((v) => v === null));
    else assert.equal(sum(p.byModel), combined[i].cumulative);
  });
});

test('model IDs with path punctuation or object-property names get safe chart keys', () => {
  const hours = [
    {
      at: HOUR,
      usd: 3,
      categories: { 'EigenLabs/Qwen3.8-27B-4bit-mtp': 1, constructor: 2 },
    },
    { at: HOUR + 3600, usd: 0, categories: {} },
  ];
  const grouped = modelEarnings(hours, HOUR + 7200);
  assert.ok(grouped.series.every((s) => /^model_\d+$/.test(s.key)));
  assert.equal(sum(grouped.hourly[1].byModel), 0);
  assert.equal(sum(grouped.cumulative.at(-1).byModel), 3);
});

test('zero earnings, adjustments, and empty ranges stay faithful to the ledger', () => {
  assert.deepEqual(modelEarnings([], HOUR), {
    series: [],
    hourly: [],
    cumulative: [],
  });
  const grouped = modelEarnings(
    [
      { at: HOUR, usd: 1, categories: { a: 1 } },
      { at: HOUR + 3600, usd: 0, categories: { a: 0 } },
      { at: HOUR + 7200, usd: -0.1, categories: { a: -0.1 } },
    ],
    HOUR + 10800,
  );
  assert.deepEqual(
    grouped.cumulative.map((p) => sum(p.byModel)),
    [0, 1, 1, 0.9],
  );
});

test('a model forecast touches its actual line instead of floating at the account total', () => {
  const hours = [
    { at: HOUR, usd: 5, categories: { a: 3, b: 1, base_reward: 1 } },
  ];
  const actual = cumulativeEarnings(hours, HOUR + 3600);
  const grouped = modelEarnings(hours, HOUR + 3600);
  const forecast = {
    status: 'ready',
    asOf: HOUR + 3600,
    models: ['a'],
    points: [
      { at: HOUR + 3600, additional: 0 },
      { at: HOUR + 7200, additional: 0.2 },
    ],
  };
  const selected = cumulativeForModels(grouped, forecast.models, actual);
  const projected = projectedCumulative(selected, forecast, HOUR + 7200);
  const anchor = projected.find((p) => p.predicted != null);
  const key = grouped.series.find((s) => s.model === 'a').key;
  assert.equal(anchor.at, grouped.cumulative.at(-1).at);
  assert.equal(anchor.predicted, grouped.cumulative.at(-1).byModel[key]);
  assert.equal(anchor.predicted, 3);
  assert.equal(projected.at(-1).predicted, 3.2);
  const combined = projectedCumulative(actual, forecast, HOUR + 7200);
  assert.equal(combined.find((p) => p.predicted != null).predicted, 5);
  assert.equal(combined.at(-1).predicted, 5.2);
  assert.equal(actual.at(-1).cumulative, 5);
});

test('joint model forecasts continue their displayed subtotal without rewards or double counting', () => {
  const hours = [
    { at: HOUR, usd: 6, categories: { a: 3, b: 1, c: 1, base_reward: 1 } },
  ];
  const actual = cumulativeEarnings(hours, HOUR + 3600);
  const grouped = modelEarnings(hours, HOUR + 3600);
  assert.equal(
    cumulativeForModels(grouped, ['a', 'b', 'a'], actual).at(-1).cumulative,
    4,
  );
  assert.equal(
    cumulativeForModels(grouped, ['new-model'], actual).at(-1).cumulative,
    0,
  );
});

test('model projection anchors follow custom ranges, preserve missing hours, and leave adjustments separate', () => {
  const hours = [
    { at: HOUR, usd: 2, categories: { a: 1 } },
    { at: HOUR + 7200, usd: 0.5, categories: { a: 0.4 } },
  ];
  const actual = cumulativeEarnings(hours, HOUR + 7500);
  const result = cumulativeForModels(
    modelEarnings(hours, HOUR + 7500),
    ['a'],
    actual,
  );
  assert.equal(result.at(-1).cumulative, 1.4);
  assert.deepEqual(
    result.map((p) => p.cumulative == null),
    actual.map((p) => p.cumulative == null),
  );
  const selected = hours.slice(1);
  const short = cumulativeForModels(
    modelEarnings(selected, HOUR + 7500),
    ['a'],
    cumulativeEarnings(selected, HOUR + 7500),
  );
  assert.deepEqual(
    short.map((p) => p.cumulative),
    [0, 0.4],
  );
  const emptyCategories = hours.map((h) => ({ ...h, usd: 0, categories: {} }));
  const missing = cumulativeEarnings(emptyCategories, HOUR + 7500);
  const zero = cumulativeForModels(
    modelEarnings(emptyCategories, HOUR + 7500),
    ['a'],
    missing,
  );
  assert.deepEqual(
    zero.map((p) => p.cumulative == null),
    missing.map((p) => p.cumulative == null),
  );
});
