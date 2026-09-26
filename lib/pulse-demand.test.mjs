import test from 'node:test';
import assert from 'node:assert/strict';
import { pulseDemandView } from './earnings-pulse.ts';

const now = 1_790_000_000;
const demand = (over = {}) => ({
  model: 'gemma',
  at: now - 20,
  load: 12,
  warm: 8,
  pressure: 1.5,
  typicalPressure: 0.5,
  typicalSamples: 900,
  ratio: 3,
  ...over,
});

test('demand sits on the needle scale and reads as a multiple of usual', () => {
  const view = pulseDemandView(demand(), 4, 1.2, now);
  assert.equal(view.fraction, 0.75);
  assert.equal(view.label, 'Demand 3.0× usual');
  assert.equal(view.detail, '12 requests on 8 warm Macs');
  assert.equal(view.hint, null);
  assert.equal(pulseDemandView(demand({ ratio: 20 }), 8, 1, now).fraction, 1);
});

test('the hint separates routing problems from a quiet network', () => {
  assert.match(
    pulseDemandView(demand({ ratio: 1.1 }), 4, 0.2, now).hint,
    /may not be reaching this Mac/,
  );
  assert.match(
    pulseDemandView(demand({ ratio: 0.3 }), 4, 0.2, now).hint,
    /demand for this model is low/,
  );
  assert.equal(
    pulseDemandView(demand({ ratio: 0.3 }), 4, null, now).hint,
    null,
  );
});

test('without a usual level it shows live demand only; bad or old data hides it', () => {
  const early = pulseDemandView(
    demand({ ratio: null, typicalPressure: null }),
    4,
    1,
    now,
  );
  assert.equal(early.fraction, null);
  assert.match(early.detail, /usual level still being measured/);
  assert.equal(
    pulseDemandView(demand({ load: 1, warm: 1 }), 4, 1, now).detail,
    '1 request on 1 warm Mac',
  );
  for (const bad of [
    null,
    undefined,
    'x',
    demand({ at: now - 900 }),
    demand({ load: -1 }),
    demand({ ratio: 'high' }),
    demand({ model: 3 }),
  ]) {
    assert.equal(pulseDemandView(bad, 4, 1, now), null);
  }
});
