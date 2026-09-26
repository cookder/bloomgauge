import test from 'node:test';
import assert from 'node:assert/strict';
import { trafficRate, trafficReading } from '../lib/traffic-pulse.ts';
test('missing traffic never becomes zero or another metric', () => {
  for (const value of [undefined, null, NaN, Infinity, -1, '2', true])
    assert.equal(trafficRate(value), null);
  assert.equal(trafficRate(0), 0);
  const snapshot = {
    at: 100,
    sessionId: 2,
    models: ['model'],
    status: 'live',
    windows: { 60: { tokensPerSecond: 12, requestsPerMinute: null } },
  };
  assert.equal(trafficReading(snapshot, 'requests').windows[60].rate, null);
  assert.equal(trafficReading(snapshot, 'tokens').windows[60].rate, 12);
  assert.notEqual(
    trafficReading(snapshot, 'tokens').streamId,
    trafficReading({ ...snapshot, sessionId: 3 }, 'tokens').streamId,
  );
  assert.notEqual(
    trafficReading(snapshot, 'tokens').streamId,
    trafficReading({ ...snapshot, models: ['other'] }, 'tokens').streamId,
  );
});
