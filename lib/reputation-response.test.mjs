import test from 'node:test';
import assert from 'node:assert/strict';
import { validReputationResponse } from './reputation-response.ts';

// native/reputation.py snapshot() for a Darkbloom 0.9.10 reading: counts, no score.
const reading = () => ({
  status: 'ok',
  nativeAvailable: true,
  identityAvailable: true,
  session: { id: 7, status: 'active' },
  data: {
    score: null,
    updatedAt: 1000,
    totalJobs: 100,
    successfulJobs: 99,
    failedJobs: 1,
    uptimeSeconds: 72000,
    responseTimeMs: 1500,
    challengesPassed: 9,
    challengesFailed: 1,
    trustLevel: 'hardware',
    providerStatus: 'serving',
    scope: 'network_record',
  },
});

test('a reading without a score (Darkbloom 0.9.10) keeps the card', () => {
  assert.equal(validReputationResponse(reading()), true);
  const scored = reading();
  scored.data.score = 0.93;
  assert.equal(validReputationResponse(scored), true);
  const missing = reading();
  delete missing.data.score;
  assert.equal(validReputationResponse(missing), true);
  assert.equal(
    validReputationResponse({
      ...reading(),
      status: 'disconnected',
      data: null,
    }),
    true,
  );
});

test('malformed readings are still refused', () => {
  for (const change of [
    { status: 1 },
    { data: { ...reading().data, score: '0.9' } },
    { data: { ...reading().data, score: Number.NaN } },
    { data: { ...reading().data, updatedAt: null } },
    { session: { id: 'seven' } },
  ])
    assert.equal(validReputationResponse({ ...reading(), ...change }), false);
  for (const value of [null, [], 'ok'])
    assert.equal(validReputationResponse(value), false);
});
