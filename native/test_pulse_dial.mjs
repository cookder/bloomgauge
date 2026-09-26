import test from 'node:test';
import assert from 'node:assert/strict';
import { pulseDial } from '../lib/earnings-pulse.ts';
test('dial retains headroom without oscillating scales around the expansion boundary', () => {
  let dial = pulseDial(3.19);
  assert.equal(dial.scale, 4);
  dial = pulseDial(3.21, dial.scale);
  assert.equal(dial.scale, 8);
  for (const value of [3.19, 3.22, 3.18, 3.2]) {
    dial = pulseDial(value, dial.scale);
    assert.equal(dial.scale, 8);
  }
  dial = pulseDial(2.3, dial.scale);
  assert.equal(dial.scale, 4);
  assert.equal(pulseDial(null, 8).scale, 8);
});
test('high earnings retain headroom and correctly labeled scale', () => {
  for (const ratio of [0, 1, 2.99, 3.2, 4, 8, 25, 100]) {
    const d = pulseDial(ratio);
    assert.ok(d.fraction <= 0.8);
    assert.equal(d.fraction * d.scale, ratio);
  }
  assert.equal(pulseDial(2.99).scale, 4);
  assert.equal(pulseDial(4).scale, 8);
});
test('bands distinguish earnings intensity and unavailable data', () => {
  const bands = [0.5, 1, 2, 3, 5].map((r) => pulseDial(r));
  assert.equal(new Set(bands.map((b) => b.color)).size, 5);
  assert.equal(pulseDial(null).label, 'Waiting');
  assert.equal(pulseDial(-1).fraction, 0);
});
