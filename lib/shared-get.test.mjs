import test from 'node:test';
import assert from 'node:assert/strict';
import { forgetShared, sharedGet } from './shared-get.ts';

test('pollers within the window share one request; a write forces the next read', async () => {
  let calls = 0;
  globalThis.fetch = async () => {
    calls++;
    return new Response(JSON.stringify({ n: calls }));
  };
  assert.deepEqual(await sharedGet('/x', 20000), { n: 1 });
  assert.deepEqual(await sharedGet('/x', 20000), { n: 1 });
  forgetShared('/x');
  assert.deepEqual(await sharedGet('/x', 20000), { n: 2 });
  assert.equal(calls, 2);
});

test('one caller aborting does not cancel the shared request; failures are not cached', async () => {
  let calls = 0,
    release;
  globalThis.fetch = () => {
    calls++;
    return new Promise((r) => {
      release = () => r(new Response('{"ok":true}'));
    });
  };
  const aborted = new AbortController();
  const first = sharedGet('/y', 20000, aborted.signal),
    second = sharedGet('/y', 20000);
  aborted.abort();
  await assert.rejects(first);
  release();
  assert.deepEqual(await second, { ok: true });
  globalThis.fetch = async () => {
    calls++;
    throw new TypeError('offline');
  };
  await assert.rejects(sharedGet('/z', 20000));
  globalThis.fetch = async () => {
    calls++;
    return new Response('{"back":1}');
  };
  assert.deepEqual(await sharedGet('/z', 20000), { back: 1 });
  assert.equal(calls, 3);
});
