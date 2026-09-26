import test from 'node:test';
import assert from 'node:assert/strict';
import { readModelInsights } from '../lib/model-insights.ts';
const timezone = 'America/Chicago',
  now = 1790130000;
const fixture = () => ({
  schemaVersion: 1,
  at: now,
  timezone,
  history: {
    from: now - 28 * 86400,
    to: now,
    settledThrough: now - 120,
    lookbackDays: 28,
  },
  horizon: { from: now, to: now + 28800, seconds: 28800 },
  scope: 'this_mac_solo_inference',
  models: [
    {
      id: 'gemma',
      observed: {
        status: 'observed',
        usdPerWarmHour: 0.16,
        confirmedInferenceUsd: 3.2,
        warmHours: 20,
        days: 4,
        creditedRequests: 300,
        adjustments: 1,
        asOf: now - 120,
        reason: 'Settled solo work',
      },
      requestSize: {
        status: 'measured',
        meanOutputTokens: 1200,
        outputSamples: 200,
        creditedRequests: 300,
        outputCoverage: 2 / 3,
        asOf: now - 120,
        reason: 'Measured output only',
      },
      demandNext8h: {
        scope: 'whole_network_model',
        status: 'historical_pattern',
        meanConcurrentRequests: 14,
        supportedSeconds: 28800,
        minimumDates: 4,
        qualifiedHistoryHours: 30,
        basis: 'weekday_hour',
        asOf: now - 3600,
        reason: 'Historical time slots',
      },
      incomeNext8h: {
        status: 'conditional',
        usdPerWarmHour: 0.15,
        supportedSeconds: 28800,
        matchedWarmHours: 18,
        matchedCreditedRequests: 280,
        days: 4,
        blocks: 16,
        minimumSlotCoverage: 0.9,
        basis: 'weekday_time',
        asOf: now - 3600,
        reason: 'Matched paid work',
      },
    },
  ],
});
const read = (v) => readModelInsights(v, timezone, ['gemma']);
test('accepts fully supported history and preserves signed adjustments and measured zero output', () => {
  const v = fixture();
  v.models[0].observed.usdPerWarmHour = -0.01;
  v.models[0].observed.confirmedInferenceUsd = -0.2;
  v.models[0].requestSize.meanOutputTokens = 0;
  assert.equal(read(v), v);
});
test('partial horizons remain unavailable instead of extrapolating available slots', () => {
  const v = fixture();
  for (const key of ['demandNext8h', 'incomeNext8h']) {
    const row = v.models[0][key];
    row.status = 'partial';
    row.supportedSeconds = 14400;
    row[key === 'demandNext8h' ? 'meanConcurrentRequests' : 'usdPerWarmHour'] =
      null;
  }
  assert.equal(read(v), v);
  v.models[0].incomeNext8h.usdPerWarmHour = 0.1;
  assert.throws(() => read(v));
});
test('rejects wrong model, scope, timezone, duplicate or missing report rows', () => {
  for (const change of [
    (v) => (v.timezone = 'UTC'),
    (v) => (v.scope = 'account_total'),
    (v) => (v.models[0].id = 'qwen'),
    (v) => v.models.push(v.models[0]),
    (v) => (v.models = []),
    (v) => (v.models[0].demandNext8h.scope = 'this_mac'),
  ]) {
    const v = fixture();
    change(v);
    assert.throws(() => read(v));
  }
});
test('missing tokens cannot be converted to a measured request size', () => {
  const v = fixture();
  v.models[0].requestSize.outputSamples = 0;
  assert.throws(() => read(v));
  v.models[0].requestSize.status = 'unknown';
  v.models[0].requestSize.meanOutputTokens = null;
  v.models[0].requestSize.outputCoverage = 0;
  assert.equal(read(v), v);
});
test('rejects impossible coverage, nonfinite money and future evidence', () => {
  for (const change of [
    (v) => (v.models[0].observed.usdPerWarmHour = NaN),
    (v) => (v.models[0].incomeNext8h.minimumSlotCoverage = 1.1),
    (v) => (v.models[0].demandNext8h.supportedSeconds = 3600),
    (v) => (v.models[0].requestSize.asOf = now + 1),
    (v) => (v.history.settledThrough = now + 10),
    (v) => (v.horizon.to += 3600),
  ]) {
    const v = fixture();
    change(v);
    assert.throws(() => read(v));
  }
});
