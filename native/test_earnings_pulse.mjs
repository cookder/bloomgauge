import assert from 'node:assert/strict';
import test from 'node:test';
import {
  creditArrivals,
  creditBursts,
  pulseComparison,
} from '../lib/earnings-pulse.ts';
const event = (id, amount = 123) => ({
  id,
  at: 98,
  receivedAt: 100,
  microUsd: amount,
  model: 'a',
});
const pulse = (events, extra = {}) => ({
  at: 100,
  streamId: 'one',
  sessionId: 1,
  status: 'live',
  events,
  ...extra,
});
test('first load is a baseline and new credits animate once', () => {
  let a = creditArrivals(null, pulse([event(1)]), true);
  assert.deepEqual(a.events, []);
  a = creditArrivals(a.cursor, pulse([event(1), event(2)], { at: 101 }), true);
  assert.deepEqual(
    a.events.map((x) => x.id),
    [2],
  );
  assert.deepEqual(
    creditArrivals(a.cursor, pulse([event(1), event(2)], { at: 102 }), true)
      .events,
    [],
  );
});
test('pause resume and reconnect never replay a backlog', () => {
  let a = creditArrivals(null, pulse([event(1)]), true);
  let b = creditArrivals(a.cursor, pulse([event(1), event(2)]), false);
  assert.equal(b.cursor, null);
  assert.deepEqual(
    creditArrivals(b.cursor, pulse([event(1), event(2)]), true).events,
    [],
  );
  assert.deepEqual(
    creditArrivals(a.cursor, pulse([event(2)], { at: 130 }), true).events,
    [],
  );
});
test('new account or session clears the old watermark and burst', () => {
  let a = creditArrivals(null, pulse([event(100)]), true);
  let b = creditArrivals(
    a.cursor,
    pulse([event(1)], { streamId: 'two' }),
    true,
  );
  assert.equal(b.reset, true);
  assert.deepEqual(b.cursor.ids, [1]);
  assert.deepEqual(
    creditArrivals(
      b.cursor,
      pulse([event(1), event(100)], { streamId: 'two', at: 101 }),
      true,
    ).events.map((e) => e.id),
    [100],
  );
  assert.equal(
    creditArrivals(a.cursor, pulse([], { sessionId: 2 }), true).reset,
    true,
  );
});
test('stale credits, adjustments, zero, and future arrivals do not celebrate', () => {
  let a = creditArrivals(null, pulse([]), true);
  const events = [
    event(1, -1),
    event(2, 0),
    { ...event(3), receivedAt: 20 },
    { ...event(4), receivedAt: 200 },
  ];
  assert.deepEqual(
    creditArrivals(a.cursor, pulse(events, { at: 101 }), true).events,
    [],
  );
});
test('meter preserves numeric spikes while bounding its graphic, and distinguishes missing from zero', () => {
  assert.equal(pulseComparison(null, 0.15), null);
  assert.equal(pulseComparison(0, 0.15).ratio, 0);
  assert.equal(pulseComparison(0.6, 0.15).ratio, 4);
  assert.equal(pulseComparison(0.6, 0.15).gauge, 2);
  assert.equal(pulseComparison(0.15, 0.15).percent, 0);
  assert.equal(pulseComparison(1, 0), null);
});
test('bounded bursts preserve every microdollar in the labelled overflow batch', () => {
  const events = Array.from({ length: 20 }, (_, i) => event(i, i + 1));
  const bursts = creditBursts(events);
  assert.equal(bursts.length, 11);
  assert.equal(bursts.at(-1).count, 10);
  assert.equal(
    bursts.reduce((sum, e) => sum + e.microUsd, 0),
    210,
  );
});
test('pace reads in cents by default, dollars only when chosen', async () => {
  const { paceCents, readPaceUnit } = await import('../lib/earnings-pulse.ts');
  for (const value of [null, undefined, '', 'cents', 'DOLLARS', 1])
    assert.equal(readPaceUnit(value), 'cents');
  assert.equal(readPaceUnit('dollars'), 'dollars');
  assert.equal(paceCents(0.2053), '20.53¢');
  assert.equal(paceCents(0.012), '1.20¢');
  assert.equal(paceCents(0.00004), '0.00¢');
  assert.equal(paceCents(0), '0.00¢');
  assert.equal(paceCents(12.5), '1,250.00¢');
  for (const value of [null, undefined, NaN, Infinity])
    assert.equal(paceCents(value), '—');
});
test('no built-in pace: no rating or routing hint until a baseline exists', async () => {
  const { pulseReference, pulseDemandView } =
    await import('../lib/earnings-pulse.ts');
  // New Mac, history mode, no baseline: nothing to rate against.
  for (const history of [null, undefined, 0, NaN]) {
    const basis = pulseReference('history', history, '');
    assert.deepEqual(basis, { value: null, source: null, routing: false });
  }
  const demand = {
    model: 'm',
    at: 100,
    load: 10,
    warm: 5,
    pressure: 2,
    typicalPressure: 2,
    typicalSamples: 50,
    ratio: 1.2,
  };
  const none = pulseReference('history', null, '');
  assert.equal(
    pulseDemandView(demand, 4, none.routing ? 0.3 : null, 100).hint,
    null,
  );
  // This Mac's own history rates the pace and may explain a routing problem.
  const own = pulseReference('history', 0.04, '');
  assert.deepEqual(own, { value: 0.04, source: 'history', routing: true });
  assert.equal(pulseComparison(0.012, own.value).ratio.toFixed(2), '0.30');
  // A typed reference rates the pace but never implies routing trouble.
  assert.deepEqual(pulseReference('reference', 0.04, ''), {
    value: null,
    source: null,
    routing: false,
  });
  assert.deepEqual(pulseReference('reference', 0.04, '0.10'), {
    value: 0.1,
    source: 'custom',
    routing: false,
  });
  assert.equal(pulseReference('reference', null, '0').value, null);
});
