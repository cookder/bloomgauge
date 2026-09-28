import test from 'node:test';
import assert from 'node:assert/strict';
import { demandLabel, validFleetModels } from './fleet.ts';
const d = (ratio) => ({
  load: 40,
  warm: 20,
  pressure: 2,
  typicalPressure: 1,
  ratio,
});
test('demand labels', () => {
  assert.deepEqual(demandLabel(d(2)), {
    text: '2.0× usual for this time of day',
    tone: 'high',
  });
  assert.equal(demandLabel(d(1)).text, 'Usual for this time of day');
  assert.equal(
    demandLabel(d(0.4)).text,
    '0.4× usual for this time of day · quiet',
  );
  // Quiet matches the Pulse hint's "low" (below 0.5×), not 0.6×.
  assert.equal(demandLabel(d(0.55)).tone, 'normal');
  assert.equal(demandLabel(d(0.49)).tone, 'low');
  assert.equal(demandLabel(null).tone, 'unknown');
  assert.match(demandLabel(d(null)).text, /40 requests · 20 warm/);
});
test('model rollup validation', () => {
  assert(
    validFleetModels([
      { model: 'gemma', macs: ['a'], ratePerHour: 0.1, demand: d(2) },
      { model: 'q', macs: [], ratePerHour: null, demand: null },
    ]),
  );
  assert(
    !validFleetModels([
      { model: 'gemma', macs: 'a', ratePerHour: 0.1, demand: null },
    ]),
  );
  assert(
    !validFleetModels([
      { model: 'g', macs: [], ratePerHour: NaN, demand: null },
    ]),
  );
  assert(!validFleetModels(null));
});
