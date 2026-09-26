import test from 'node:test';
import assert from 'node:assert/strict';
import { sanitizeNetworkResponse } from '../lib/network-response.ts';

const snapshot = () => ({
  capacity: { status: 'ok', data: { models: [] }, updatedAt: 100 },
  stats: {
    status: 'ok',
    data: { providers: [], request_regions: [] },
    updatedAt: 100,
  },
  totals: { status: 'stale', data: { jobs: 5 }, updatedAt: 50 },
  series: { status: 'ok', data: { window: '24h' }, updatedAt: 100 },
  backfill: { status: 'partial', data: { windows: ['24h'] }, updatedAt: 100 },
});
test('accepts independent valid, stale and partial network feeds', () => {
  const value = snapshot();
  assert.deepEqual(sanitizeNetworkResponse(value), value);
});
test('a malformed optional fleet feed keeps its labeled saved payload and live capacity', () => {
  const previous = snapshot();
  const value = snapshot();
  value.stats = {
    status: 'ok',
    data: { providers: 'invalid' },
    updatedAt: 200,
  };
  value.capacity.updatedAt = 200;
  const result = sanitizeNetworkResponse(value, previous);
  assert.equal(result.capacity.status, 'ok');
  assert.equal(result.capacity.updatedAt, 200);
  assert.equal(result.stats.status, 'stale');
  assert.deepEqual(result.stats.data, previous.stats.data);
  assert.equal(result.stats.updatedAt, 100);
  assert.match(result.stats.error, /incomplete/);
  assert.equal(sanitizeNetworkResponse(value).stats.status, 'error');
});
test('rejects payloads that would crash nested rendering and recovers on the next valid feed', () => {
  assert.throws(() => sanitizeNetworkResponse(null), /incomplete/);
  for (const invalid of [
    { providers: [null] },
    { providers: [{ id: 42 }] },
    { request_flows: [{ from: null, to: {} }] },
    { provider_regions: {} },
  ]) {
    const value = snapshot();
    value.stats.data = invalid;
    const broken = sanitizeNetworkResponse(value);
    assert.equal(broken.stats.status, 'error');
    assert.equal(
      sanitizeNetworkResponse(snapshot(), broken).stats.status,
      'ok',
    );
  }
});
