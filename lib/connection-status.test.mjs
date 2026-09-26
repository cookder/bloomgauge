import test from 'node:test';
import assert from 'node:assert/strict';
import { dashboardConnection, readStatusJSON } from './connection-status.ts';
import { subscribePageVisibility } from './use-page-visibility.ts';

test('no reading, failed first load and healthy transport have distinct states', () => {
  assert.equal(
    dashboardConnection(null, 100, '', false, true).label,
    'Connecting',
  );
  assert.equal(
    dashboardConnection(null, 100, 'HTTP 503', false, true).label,
    'Readings delayed',
  );
  assert.equal(dashboardConnection(100, 100, '', false, true).label, 'Live');
  assert.equal(dashboardConnection(100, 100, '', false, true).live, true);
});
test('past and future timestamps cannot masquerade as a live connection', () => {
  assert.equal(dashboardConnection(90, 100, '', false, true).live, true);
  assert.equal(dashboardConnection(89, 100, '', false, true).live, false);
  assert.equal(dashboardConnection(105, 100, '', false, true).live, true);
  const future = dashboardConnection(106, 100, '', false, true);
  assert.equal(future.stale, true);
  assert.match(future.detail, /date and time/);
  for (const at of [NaN, Infinity, -Infinity])
    assert.equal(dashboardConnection(at, 100, '', false, true).live, false);
  assert.equal(dashboardConnection(100, NaN, '', false, true).live, false);
});
test('paused and background views never advertise live polling', () => {
  const paused = dashboardConnection(100, 100, '', true, true);
  assert.equal(paused.label, 'View paused');
  assert.equal(paused.live, false);
  const hidden = dashboardConnection(100, 100, '', false, false);
  assert.equal(hidden.label, 'View in background');
  assert.equal(hidden.live, false);
  assert.equal(
    dashboardConnection(100, 100, '', true, false).label,
    'View paused',
  );
  assert.equal(dashboardConnection(100, 100, '', false, true).live, true);
});
test('transport failure overrides a recent timestamp, then clears on recovery', () => {
  const bad = dashboardConnection(
    100,
    100,
    'The connection timed out.',
    false,
    true,
  );
  assert.equal(bad.live, false);
  assert.equal(bad.detail, 'The connection timed out.');
  const good = dashboardConnection(101, 101, '', false, true);
  assert.equal(good.live, true);
  assert.equal(good.detail, '');
});
test('HTTP failures expose the status code but no response body or private URL', async () => {
  for (const status of [401, 403, 404, 429, 500, 503])
    await assert.rejects(
      () =>
        readStatusJSON(
          new Response('private-secret-host', { status }),
          'Optimizer controls',
        ),
      (error) =>
        error.message ===
        `Optimizer controls unavailable (HTTP ${status}). Retrying automatically.`,
    );
});
test('unreadable JSON is sanitized; valid JSON still reaches the existing schema validator', async () => {
  await assert.rejects(
    () => readStatusJSON(new Response('private-secret-json'), 'Model status'),
    {
      message:
        'Model status returned an unreadable response. Retrying automatically.',
    },
  );
  assert.deepEqual(
    await readStatusJSON(new Response('{"at":100}'), 'Model status'),
    { at: 100 },
  );
  assert.equal(
    await readStatusJSON(new Response('null'), 'Model status'),
    null,
  );
});
test('visibility, page restore and focus subscriptions are all removed on disposal', () => {
  const oldDocument = globalThis.document,
    oldWindow = globalThis.window;
  globalThis.document = new EventTarget();
  globalThis.window = new EventTarget();
  let events = 0;
  try {
    const stop = subscribePageVisibility(() => events++);
    document.dispatchEvent(new Event('visibilitychange'));
    window.dispatchEvent(new Event('pageshow'));
    window.dispatchEvent(new Event('focus'));
    assert.equal(events, 3);
    stop();
    document.dispatchEvent(new Event('visibilitychange'));
    window.dispatchEvent(new Event('pageshow'));
    window.dispatchEvent(new Event('focus'));
    assert.equal(events, 3);
  } finally {
    if (oldDocument === undefined) delete globalThis.document;
    else globalThis.document = oldDocument;
    if (oldWindow === undefined) delete globalThis.window;
    else globalThis.window = oldWindow;
  }
});
