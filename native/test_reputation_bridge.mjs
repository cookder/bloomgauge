import { readFile } from 'node:fs/promises';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';

const source = await readFile(
  new URL('./reputation-bridge.js', import.meta.url),
  'utf8',
);
const origin = 'https://console.darkbloom.dev';
const flush = async () => {
  for (let i = 0; i < 10; i++)
    await new Promise((resolve) => setImmediate(resolve));
};
function harness(response, status = 200, locationOrigin = origin) {
  const sent = [],
    calls = [];
  const window = {
    webkit: {
      messageHandlers: {
        bloomReputation: { postMessage: (p) => sent.push(structuredClone(p)) },
      },
    },
    fetch: async (input, init) => {
      calls.push(new BrowserRequest(input, init));
      return new Response(JSON.stringify(response), {
        status,
        headers: { 'Content-Type': 'application/json' },
      });
    },
  };
  class BrowserRequest extends Request {
    constructor(input, init) {
      super(typeof input === 'string' ? new URL(input, origin) : input, init);
    }
  }
  window.top = window;
  const original = window.fetch;
  vm.runInNewContext(source, {
    window,
    location: { origin: locationOrigin, href: locationOrigin + '/earn' },
    URL,
    Request: BrowserRequest,
    AbortController,
    setTimeout,
    clearTimeout,
  });
  return { window, sent, calls, original };
}

test('the bridge forwards only reputation fields and leaves the console response intact', async () => {
  const data = {
    providers: [
      {
        se_public_key: 'mac-key',
        trust_level: 'hardware',
        status: 'online',
        account_id: 'private-account',
        reputation: {
          score: 0.92,
          total_jobs: 20,
          successful_jobs: 20,
          extra: 'private-extra',
        },
      },
    ],
  };
  const h = harness(data);
  const result = await h.window.fetch('/api/me/providers', {
    headers: { Authorization: 'Bearer fake-test-secret' },
  });
  assert.deepEqual(await result.json(), data);
  await flush();
  assert.equal(h.sent[0].providers[0].reputation.score, 0.92);
  assert.equal(h.sent[0].providers[0].se_public_key, 'mac-key');
  const serialized = JSON.stringify(h.sent);
  for (const excluded of [
    'fake-test-secret',
    'private-account',
    'private-extra',
    'Authorization',
  ])
    assert(!serialized.includes(excluded));
  await h.window.__bloomRefreshReputation();
  assert.equal(
    h.calls[1].headers.get('Authorization'),
    'Bearer fake-test-secret',
  );
  assert.equal(h.calls[1].url, origin + '/api/me/providers');
  assert.equal(h.calls[1].cache, 'no-store');
});

test('other endpoints and mutations are never captured or replayed', async () => {
  const h = harness({ providers: [] });
  await h.window.fetch('/api/me/summary');
  await h.window.fetch('/api/me/providers', { method: 'POST' });
  await h.window.fetch('https://foreign.example/api/me/providers');
  await flush();
  await h.window.__bloomRefreshReputation();
  assert.equal(h.sent.length, 0);
  assert.equal(h.calls.length, 3);
});

test('expired sign-in and upstream failures have explicit states, not zero scores', async () => {
  for (const status of [401, 403, 503]) {
    const h = harness({ error: 'not allowed' }, status);
    await h.window.fetch('/api/me/providers');
    await flush();
    assert.deepEqual(h.sent, [
      { status: status === 503 ? 'unavailable' : 'auth_required' },
    ]);
  }
});

test('a new console request refreshes authentication used for subsequent reads', async () => {
  const h = harness({ providers: [] });
  for (const token of ['old-test-token', 'new-test-token']) {
    await h.window.fetch('/api/me/providers', {
      headers: { Authorization: token },
    });
  }
  await h.window.__bloomRefreshReputation();
  assert.equal(h.calls[2].headers.get('Authorization'), 'new-test-token');
});

test('the script is inert outside the official console origin', () => {
  const h = harness({}, 200, 'https://console.darkbloom.dev.foreign.example');
  assert.equal(h.window.fetch, h.original);
  assert.equal(h.window.__bloomRefreshReputation, undefined);
});

test('concurrency crosses as a strict slot allowlist without capacity secrets', async () => {
  const h = harness({
    providers: [
      {
        online: true,
        last_heartbeat: '2026-09-12T00:00:00Z',
        pending_requests: 2,
        max_concurrency: 8,
        backend_capacity: {
          secret: 'private-capacity',
          slots: [
            {
              model: 'A',
              state: 'running',
              num_running: 2,
              num_waiting: 0,
              max_concurrency: 4,
              secret: 'private-slot',
            },
          ],
        },
      },
    ],
  });
  await h.window.fetch('/api/me/providers');
  await flush();
  const p = h.sent[0].providers[0];
  assert.equal(p.pending_requests, 2);
  assert.equal(p.backend_capacity.slots[0].num_waiting, 0);
  assert(!JSON.stringify(h.sent).includes('private-capacity'));
  assert(!JSON.stringify(h.sent).includes('private-slot'));
});
