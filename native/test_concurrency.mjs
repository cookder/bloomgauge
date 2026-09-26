import test from 'node:test';
import assert from 'node:assert/strict';
import { validConcurrency, concurrencyTotal } from '../lib/concurrency.ts';
const c = {
  at: 100,
  pending: 4,
  limit: 8,
  slots: [
    { model: 'A', state: 'running', running: 2, waiting: 0, limit: 4 },
    { model: 'B', state: 'idle', running: 0, waiting: 1, limit: 4 },
  ],
};
test('sums backend concurrency independently of coordinator reservations', () => {
  assert(validConcurrency(c));
  assert.equal(concurrencyTotal(c, 'running'), 2);
  assert.equal(concurrencyTotal(c, 'waiting'), 1);
});
test('missing slots and partial counters never claim zero idle', () => {
  assert.equal(concurrencyTotal({ ...c, slots: [] }, 'running'), null);
  assert.equal(
    concurrencyTotal(
      { ...c, slots: [{ ...c.slots[0], running: null }] },
      'running',
    ),
    null,
  );
  assert.equal(concurrencyTotal({ ...c, slots: [c.slots[1]] }, 'running'), 0);
});
test('rejects invalid optional metrics safely', () => {
  for (const bad of [
    null,
    {},
    { ...c, slots: [null] },
    { ...c, pending: true },
    { ...c, at: NaN },
    { ...c, limit: -1 },
  ])
    assert.equal(validConcurrency(bad), false);
});
