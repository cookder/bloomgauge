import test from 'node:test';
import assert from 'node:assert/strict';
import { stripTypeScriptTypes } from 'node:module';
import { readFileSync } from 'node:fs';
const source = readFileSync(
  new URL('../lib/earnings-outlook.ts', import.meta.url),
  'utf8',
).replace(
  "'./optimizer-response'",
  JSON.stringify(new URL('../lib/optimizer-response.ts', import.meta.url).href),
);
const { validEarningsOutlook, outlookReading, demandBands } = await import(
  'data:text/javascript;base64,' +
    Buffer.from(stripTypeScriptTypes(source)).toString('base64')
);
const observation = {
  hours: 4,
  days: 3,
  minutes: 240,
  asOf: 900,
  usd: 0.6,
  paidJobs: 200,
  active: 8,
  queued: 2,
  warm: 5,
  load: 10,
  pressure: 2,
  usdPerHour: 0.15,
  requestsPerMinute: 2,
  tokensPerSecond: 120,
  busyPercent: 50,
};
const current = {
  ...observation,
  scope: 'similar_demand',
  usable: true,
  forecastUsable: true,
  weight: 1,
  reason: 'Repeated',
  blocks: 8,
  coverage: 1,
  overlapHours: 4,
  totalHours: 4,
  lower: 0.12,
  upper: 0.18,
  current: { pressure: 2, active: 8, warm: 5, load: 10 },
};
const model = {
  model: 'a',
  serving: true,
  eligible: true,
  signalAt: 990,
  totalWarmHours: 4,
  pairedWarmHours: 4,
  current,
  bands: demandBands.map(([low, high]) => ({
    ...observation,
    low,
    high,
    blocks: 8,
    quality: 'repeated',
    excludedThinHours: 0,
  })),
};
const report = {
  at: 1000,
  from: 0,
  to: 1000,
  models: [model],
  scope: 'This Mac',
  method: 'Paired',
};
test('valid reports and signed outcomes, invalid nested values and duplicate models', () => {
  assert.ok(validEarningsOutlook(report));
  assert.ok(validEarningsOutlook({ ...report, models: [] }));
  for (const m of [
    { ...model, current: {} },
    { ...model, bands: [null] },
    { ...model, bands: model.bands.map((b) => ({ ...b, usdPerHour: '0' })) },
    { ...model, signalAt: undefined },
    { ...model, serving: 'yes' },
  ])
    assert.equal(validEarningsOutlook({ ...report, models: [m] }), false);
  assert.equal(
    validEarningsOutlook({ ...report, models: [model, model] }),
    false,
  );
  assert.ok(
    validEarningsOutlook({
      ...report,
      models: [
        {
          ...model,
          bands: model.bands.map((b) => ({ ...b, usdPerHour: -0.1 })),
        },
      ],
    }),
  );
});
test('stale demand cannot become a live expected rate; historical band observations remain', () => {
  assert.equal(outlookReading(model, 'current', 1000).rate, 0.15);
  assert.equal(outlookReading(model, 'current', 1000).supported, true);
  for (const [at, stale] of [
    [1100, false],
    [1000, true],
    [980, false],
  ]) {
    const r = outlookReading(model, 'current', at, stale);
    assert.equal(r.rate, null);
    assert.equal(r.supported, false);
  }
  assert.equal(outlookReading(model, '2', 1100, true).rate, 0.15);
});
test('limited history is an observation, genuine zero differs from no history', () => {
  const limited = {
    ...model,
    current: { ...current, forecastUsable: false, usdPerHour: 0 },
  };
  assert.equal(outlookReading(limited, 'current', 1000).rate, 0);
  assert.equal(
    outlookReading(limited, 'current', 1000).label,
    'Limited observations',
  );
  assert.equal(
    outlookReading(
      {
        ...model,
        current: {
          ...current,
          hours: 0,
          usdPerHour: null,
          forecastUsable: false,
        },
      },
      'current',
      1000,
    ).label,
    'No matched history',
  );
});

const { validEarningsForecast, forecastReading } = await import(
  'data:text/javascript;base64,' +
    Buffer.from(stripTypeScriptTypes(source)).toString('base64')
);
const forecast = {
  methodVersion: 'paid-hour-baselines-v1',
  state: 'estimate',
  origin: 1000,
  issuedAt: 1000,
  targetEnd: 4600,
  validUntil: 4600,
  refreshAfter: 1300,
  horizonSeconds: 3600,
  target: 'inference_usd_next_hour_if_already_warm_and_stays_selected_ready',
  condition: 'already_warm_and_same_model_selected_ready_for_entire_hour',
  assumption:
    'If kept warm and ready on this Mac for the full hour. Loading, switching and base rewards are excluded.',
  usd: 0.12,
  usdPerHour: 0.12,
  basis: 'completed_hour_history',
  reason:
    'Recent complete hours provide a steadier baseline than sparse demand matches.',
  reasonCodes: [],
  demandAsOf: 990,
  paidEvidenceThrough: 800,
  inputStatus: 'fresh',
  lookbackSeconds: 2592000,
  support: {
    completedHours: 12,
    distinctDates: 4,
    recentCompleteMinutes: 15,
    recentRequiredMinutes: 15,
    lastOutcomeAt: 800,
    lastPaidMinuteEnd: 800,
    demandAdjusted: false,
  },
  uncertainty: {
    kind: 'descriptive_historical_spread',
    lower: 0.04,
    upper: 0.2,
    nominalCoverage: null,
    validationWindows: 0,
    empiricalCoverage: null,
    label:
      'Middle half of historical hourly outcomes; not a calibrated prediction interval.',
  },
  historicalFallback: {
    basis: 'completed_warm_hours',
    usdPerWarmHour: 0.11,
    observedHours: 12,
    asOf: 800,
    isForecast: false,
  },
  validation: {
    status: 'experimental_baseline',
    prospectiveValidated: false,
    retrospectiveEvidence: 'Dollar forecasting skill is not yet established.',
    creditAvailability:
      'Historical credit availability at each forecast origin cannot be proven.',
  },
};
test('forecast contract requires complete nested evidence and preserves signed actual dollars', () => {
  assert.ok(validEarningsForecast(forecast));
  assert.ok(
    validEarningsOutlook({ ...report, models: [{ ...model, forecast }] }),
  );
  for (const f of [
    { ...forecast, horizonSeconds: 300 },
    { ...forecast, support: {} },
    { ...forecast, usdPerHour: Infinity },
    { ...forecast, usd: 0.13 },
    { ...forecast, uncertainty: { ...forecast.uncertainty, upper: -1 } },
    {
      ...forecast,
      uncertainty: { ...forecast.uncertainty, nominalCoverage: 0.8 },
    },
    { ...forecast, reasonCodes: [1] },
    { ...forecast, state: 'held', origin: null },
    { ...forecast, targetEnd: 9999 },
    { ...forecast, basis: 'unavailable' },
    { ...forecast, paidEvidenceThrough: 1001 },
    {
      ...forecast,
      uncertainty: { ...forecast.uncertainty, kind: 'unavailable' },
    },
  ])
    assert.equal(validEarningsForecast(f), false);
  assert.ok(
    validEarningsForecast({ ...forecast, usd: -0.01, usdPerHour: -0.01 }),
  );
});
test('an unsupported historical match never stands in for a next-hour forecast', () => {
  assert.equal(forecastReading(model, 1000).rate, null);
  assert.equal(
    forecastReading(
      {
        ...model,
        forecast: {
          ...forecast,
          state: 'unavailable',
          usd: null,
          usdPerHour: null,
        },
      },
      1001,
    ).rate,
    null,
  );
  assert.equal(
    forecastReading(
      { ...model, forecast: { ...forecast, usd: 0, usdPerHour: 0 } },
      1001,
    ).rate,
    0,
  );
});
test('a failed refresh retains the original forecast hour, then expires without a new response', () => {
  const m = { ...model, forecast };
  assert.equal(forecastReading(m, 1100, true).rate, 0.12);
  assert.equal(forecastReading(m, 1100, true).label, 'Saved forecast');
  assert.equal(forecastReading(m, 4500, true).end, 4600);
  assert.equal(forecastReading(m, 4600, true).rate, null);
  assert.equal(forecastReading(m, 4700, true).label, 'Forecast window ended');
  assert.equal(forecastReading(m, 900).rate, null);
  assert.equal(
    forecastReading({ ...m, forecast: { ...forecast, state: 'held' } }, 1100)
      .label,
    'Saved forecast',
  );
});

test('future issue time is never shown as a current forecast', () => {
  assert.equal(
    forecastReading(
      { ...model, forecast: { ...forecast, issuedAt: 1150 } },
      1100,
    ).rate,
    null,
  );
});

test('a next-minute forecast is visible with an explicit upcoming window', () => {
  const r = forecastReading(
    {
      ...model,
      forecast: {
        ...forecast,
        origin: 1060,
        targetEnd: 4660,
        validUntil: 4660,
        issuedAt: 1001,
      },
    },
    1010,
  );
  assert.equal(r.rate, 0.12);
  assert.equal(r.upcoming, true);
});

test('actual Python candidate packets satisfy frontend contract for upcoming, held, expired and unknown states', () => {
  const values = JSON.parse(
    readFileSync(
      new URL('./earnings_forecast_contract_fixture.json', import.meta.url),
      'utf8',
    ),
  );
  for (const value of values)
    assert.ok(validEarningsForecast(value), JSON.stringify(value));
  assert.ok(
    Math.abs(
      forecastReading({ ...model, forecast: values[0] }, values[0].issuedAt + 1)
        .rate - 0.06,
    ) < 1e-10,
  );
  assert.equal(
    forecastReading({ ...model, forecast: values[0] }, values[0].issuedAt + 1)
      .upcoming,
    true,
  );
});

test('malformed prospective score summaries cannot appear as measured accuracy', () => {
  const evaluation = {
    status: 'collecting',
    methodVersion: 'paid-hour-baselines-v1',
    recordedPackets: 1,
    models: [
      {
        model: 'a',
        windows: 0,
        censoredWindows: 1,
        unavailableForecasts: 0,
        pendingWindows: 2,
        overlappingWindows: 0,
        maeUSDPerHour: null,
        biasUSDPerHour: null,
      },
    ],
  };
  assert.ok(
    validEarningsOutlook({ ...report, forecastEvaluation: evaluation }),
  );
  assert.equal(
    validEarningsOutlook({
      ...report,
      forecastEvaluation: {
        ...evaluation,
        models: [{ ...evaluation.models[0], maeUSDPerHour: 0 }],
      },
    }),
    false,
  );
  assert.equal(
    validEarningsOutlook({
      ...report,
      forecastEvaluation: {
        ...evaluation,
        models: [...evaluation.models, ...evaluation.models],
      },
    }),
    false,
  );
});
