import test from 'node:test';
import assert from 'node:assert/strict';
import {
  readCreditPreferences,
  validCreditHistory,
  sortedPulseCredits,
} from '../lib/credit-history.ts';
import {
  validReleaseNotes,
  readSeenReleases,
  markReleaseSeen,
} from '../lib/release-notes.ts';

test('credit sorting is numeric, stable on ties and never mutates the live animation feed', () => {
  const events = [
    { id: 1, at: 10, microUsd: 50 },
    { id: 2, at: 20, microUsd: 5 },
    { id: 3, at: 20, microUsd: 50 },
    { id: 4, at: 30, microUsd: -1 },
  ];
  const before = structuredClone(events),
    ids = (sort) => sortedPulseCredits(events, sort).map((e) => e.id);
  assert.deepEqual(ids('newest'), [4, 3, 2, 1]);
  assert.deepEqual(ids('oldest'), [1, 2, 3, 4]);
  assert.deepEqual(ids('amount-desc'), [3, 1, 2, 4]);
  assert.deepEqual(ids('amount-asc'), [4, 2, 3, 1]);
  assert.deepEqual(events, before);
});
test('credit view preferences restore allowed options and discard invalid values', () => {
  const good = {
    sort: 'amount-desc',
    category: 'inference',
    limit: '25',
    view: 'models',
    showSummary: false,
  };
  assert.deepEqual(readCreditPreferences(JSON.stringify(good)), good);
  const defaults = readCreditPreferences(null);
  for (const bad of [
    '{',
    'null',
    '[]',
    JSON.stringify({
      sort: 'drop table',
      category: 'users',
      limit: '100000',
      showSummary: 'yes',
    }),
  ])
    assert.deepEqual(readCreditPreferences(bad), defaults);
});
const credits = () => ({
  count: 0,
  page: 1,
  limit: 100,
  coverageStart: null,
  models: [],
  entries: [],
  leaderboard: [],
  summary: {
    count: 0,
    totalUsd: 0,
    inferenceUsd: 0,
    baseRewardUsd: 0,
    averageUsd: null,
    minUsd: null,
    maxUsd: null,
    outputTokens: 0,
  },
});
test('empty credit statistics are valid but missing or nonfinite amounts cannot render as zero', () => {
  assert.equal(validCreditHistory(credits()), true);
  for (const patch of [
    { summary: null },
    { summary: { ...credits().summary, averageUsd: '0' } },
    { entries: [{ id: 1, at: 'bad', model: 'qwen', usd: 1, outputTokens: 5 }] },
    { models: null },
    { leaderboard: [{ model: 'qwen', count: 1, usd: NaN, outputTokens: 2 }] },
  ])
    assert.equal(validCreditHistory({ ...credits(), ...patch }), false);
});
test('legacy unknown model and token values remain unknown without rejecting valid credits', () => {
  const v = credits();
  v.count = 1;
  v.summary = {
    ...v.summary,
    count: 1,
    totalUsd: 0.01,
    inferenceUsd: 0.01,
    averageUsd: 0.01,
    minUsd: 0.01,
    maxUsd: 0.01,
  };
  v.entries = [
    {
      id: 1,
      at: '2026-09-22T12:00:00Z',
      model: null,
      usd: 0.01,
      outputTokens: null,
    },
  ];
  v.leaderboard = [{ model: null, count: 1, usd: 0.01, outputTokens: 0 }];
  assert.equal(validCreditHistory(v), true);
  assert.equal(v.entries[0].outputTokens, null);
  assert.equal(v.entries[0].model, null);
});
const release = () => ({
  installedVersion: '1.36.19',
  release: {
    id: 'beta23',
    version: '1.36.19',
    title: 'Simpler controls',
    highlights: [{ title: 'Your choice', detail: 'On and Manual.' }],
  },
});
test('only release notes for the actual installed version can trigger a notification', () => {
  assert.equal(validReleaseNotes(release()), true);
  assert.equal(
    validReleaseNotes({ installedVersion: 'unknown', release: null }),
    true,
  );
  assert.equal(
    validReleaseNotes({ ...release(), installedVersion: '1.33.8' }),
    false,
  );
  for (const patch of [
    { highlights: [] },
    { highlights: [null] },
    { highlights: [{ title: 'a', detail: 'x'.repeat(801) }] },
    { id: '' },
  ])
    assert.equal(
      validReleaseNotes({
        ...release(),
        release: { ...release().release, ...patch },
      }),
      false,
    );
});
test('seen releases remain local, deduplicated and bounded without hiding a future release', () => {
  let raw = markReleaseSeen(null, 'beta23');
  assert.deepEqual(readSeenReleases(raw), ['beta23']);
  raw = markReleaseSeen(raw, 'beta23');
  assert.deepEqual(readSeenReleases(raw), ['beta23']);
  assert.equal(readSeenReleases(raw).includes('beta24'), false);
  for (let i = 0; i < 30; i++) raw = markReleaseSeen(raw, `release-${i}`);
  assert.equal(readSeenReleases(raw).length, 16);
  assert.equal(readSeenReleases(raw).at(-1), 'release-29');
  assert.deepEqual(readSeenReleases('{'), []);
});
