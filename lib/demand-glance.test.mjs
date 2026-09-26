import test from 'node:test';
import assert from 'node:assert/strict';
import { glanceModels } from './demand-glance.ts';

const models = ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i'].map((id, i) => ({
  id,
  averageLoad: 100 - i * 10,
}));
const live = (over = {}) => ({
  currentModel: 'h',
  pendingTarget: null,
  proposalTarget: 'g',
  comparisonTarget: null,
  measurement: null,
  candidates: [
    { model: 'e', group: 'paid_alternative', eligible: true, kind: 'earnings' },
    { model: 'f', group: 'held', eligible: false, kind: null },
    { model: 'i', group: 'unknown', eligible: false, kind: 'explore' },
  ],
  ...over,
});

test('busiest three plus what the optimizer is serving, targeting and weighing', () => {
  assert.deepEqual(glanceModels(models, live()), [
    { id: 'h', reason: 'serving' },
    { id: 'g', reason: 'target' },
    { id: 'a', reason: 'busiest' },
    { id: 'b', reason: 'busiest' },
    { id: 'c', reason: 'busiest' },
    { id: 'e', reason: 'candidate' },
    { id: 'i', reason: 'candidate' },
  ]);
});

test('without optimizer data it is the busiest three; held models and unknown ids are left out', () => {
  assert.deepEqual(
    glanceModels(models, null).map((m) => m.id),
    ['a', 'b', 'c'],
  );
  assert.ok(!glanceModels(models, live()).some((m) => m.id === 'f'));
  assert.deepEqual(
    glanceModels(
      models,
      live({ currentModel: 'zzz', proposalTarget: null, candidates: [] }),
    ).map((m) => m.id),
    ['a', 'b', 'c'],
  );
});

test('the serving model counts once and the list is capped', () => {
  assert.deepEqual(
    glanceModels(
      models,
      live({ currentModel: 'a', proposalTarget: null, candidates: [] }),
    ),
    [
      { id: 'a', reason: 'serving' },
      { id: 'b', reason: 'busiest' },
      { id: 'c', reason: 'busiest' },
    ],
  );
  assert.equal(glanceModels(models, live(), 4).length, 4);
});
