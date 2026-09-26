import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';

function worker(windows = []) {
  const handlers = {},
    shown = [],
    opened = [],
    lifecycle = [];
  const self = {
    location: { origin: 'https://bloom.example.ts.net:8443' },
    addEventListener: (name, callback) => {
      handlers[name] = callback;
    },
    skipWaiting: async () => lifecycle.push('skipWaiting'),
    registration: {
      showNotification: async (...args) => {
        shown.push(args);
      },
    },
    clients: {
      claim: async () => lifecycle.push('claim'),
      matchAll: async () => windows,
      openWindow: async (url) => {
        opened.push(url);
      },
    },
  };
  runInNewContext(
    readFileSync(new URL('../public/push-sw.js', import.meta.url), 'utf8'),
    { self, URL },
  );
  const dispatch = async (name, data) => {
    let waiting;
    handlers[name]({
      ...data,
      waitUntil: (value) => {
        waiting = value;
      },
    });
    await waiting;
  };
  return { handlers, shown, opened, dispatch, lifecycle };
}

test('updated worker activates for existing subscriptions and preserves switch deduplication tags', async () => {
  const w = worker();
  await w.dispatch('install', {});
  await w.dispatch('activate', {});
  assert.deepEqual(w.lifecycle, ['skipWaiting', 'claim']);
  const tag = 'bloom-switch-' + 'a'.repeat(24);
  await w.dispatch('push', {
    data: {
      json: () => ({
        title: 'Bloomkeeper · model switched',
        body: 'Gemma → Qwen. Testing sustained demand.',
        tag,
      }),
    },
  });
  assert.equal(w.shown[0][1].tag, tag);
});

test('every push produces a visible notification, even malformed payloads', async () => {
  const w = worker();
  await w.dispatch('push', {
    data: {
      json() {
        throw Error('invalid');
      },
    },
  });
  assert.equal(w.shown.length, 1);
  assert.equal(w.shown[0][0], 'Bloomkeeper · model update');
  assert.equal(w.shown[0][1].data.path, '/?screen=test');
  assert.deepEqual(Object.keys(w.handlers).sort(), [
    'activate',
    'install',
    'notificationclick',
    'push',
  ]);
});

test('click ignores arbitrary notification URLs and opens the same-origin model test screen', async () => {
  const w = worker();
  await w.dispatch('notificationclick', {
    notification: { close() {}, data: { url: 'https://evil.example/' } },
  });
  assert.equal(w.opened[0], 'https://bloom.example.ts.net:8443/?screen=test');
});

test('click navigates and focuses an existing same-origin window', async () => {
  const calls = [];
  const w = worker([
    {
      url: 'https://bloom.example.ts.net:8443/',
      navigate: async (url) => calls.push(url),
      focus: async () => calls.push('focus'),
    },
  ]);
  await w.dispatch('notificationclick', { notification: { close() {} } });
  assert.deepEqual(calls, [
    'https://bloom.example.ts.net:8443/?screen=test',
    'focus',
  ]);
  assert.equal(w.opened.length, 0);
});

test('notification payload text is bounded and cannot select resources or destinations', async () => {
  const w = worker();
  await w.dispatch('push', {
    data: {
      json: () => ({
        title: 'x'.repeat(1000),
        body: 'y'.repeat(1000),
        tag: 'arbitrary',
        icon: 'https://evil.example/',
        url: 'https://evil.example/',
      }),
    },
  });
  assert.equal(w.shown[0][0].length, 100);
  assert.equal(w.shown[0][1].body.length, 240);
  assert.equal(w.shown[0][1].icon, '/bloom-icon-192.png');
  assert.equal(w.shown[0][1].tag, 'bloom-switch');
});
