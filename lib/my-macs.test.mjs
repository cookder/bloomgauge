import test from 'node:test';
import assert from 'node:assert/strict';
import { macMode, macModels } from './my-macs.ts';

const name = (m) => m.toUpperCase();

test('a Mac with more models than it names says how many more', () => {
  assert.equal(
    macModels(['a', 'b', 'c', 'd'], 11, name),
    'A + B + C + D and 7 more',
  );
  assert.equal(macModels(['a', 'b'], 2, name), 'A + B');
  // Older peers send no count.
  assert.equal(macModels(['a'], undefined, name), 'A');
  assert.equal(macModels([], 0, name), 'No verified model reading');
});

test('manager Macs read Manager, not Following demand', () => {
  assert.equal(macMode('demand', 'manager'), 'Manager');
  assert.equal(macMode('demand', null), 'Following demand');
  assert.equal(macMode('demand'), 'Following demand');
  assert.equal(macMode('observe', 'manager'), 'Observing');
  assert.equal(macMode('week'), 'Scheduled trials');
  assert.equal(macMode('unknown'), 'Observing');
});
