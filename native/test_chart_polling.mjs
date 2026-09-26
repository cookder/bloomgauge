import test from 'node:test';
import assert from 'node:assert/strict';
import { startChartPolling } from '../lib/chart-polling.ts';

const settle = async () => {
  for (let index = 0; index < 6; index++) await Promise.resolve();
};

test('a hanging mobile connection times out and polling recovers', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  let requests = 0;
  let stale;
  const signals = [];
  const values = [];
  const errors = [];
  const stop = startChartPolling({
    load(signal) {
      signals.push(signal);
      requests++;
      return requests === 1
        ? new Promise((resolve) => {
            stale = resolve;
          })
        : Promise.resolve('fresh');
    },
    onValue: (value) => values.push(value),
    onError: (error) => errors.push(error.message),
    intervalMs: 10,
    timeoutMs: 20,
  });
  t.mock.timers.tick(20);
  await settle();
  assert.equal(errors.length, 1);
  assert.match(errors[0], /timed out/);
  assert.equal(signals[0].aborted, true);
  t.mock.timers.tick(20);
  await settle();
  assert.deepEqual(values, ['fresh']);
  stale('stale');
  await settle();
  assert.deepEqual(values, ['fresh']);
  stop();
});

test('switching range discards an old response even if transport ignores abort', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  let finish;
  let oldSignal;
  const values = [];
  const stop = startChartPolling({
    load(signal) {
      oldSignal = signal;
      return new Promise((resolve) => {
        finish = resolve;
      });
    },
    onValue: (value) => values.push(value),
    onError: () => assert.fail('disposed request must not update errors'),
    intervalMs: 10,
  });
  stop();
  assert.equal(oldSignal.aborted, true);
  finish('old range');
  await settle();
  t.mock.timers.tick(100000);
  await settle();
  assert.deepEqual(values, []);
});

test('slow requests do not stack up and paused queries fetch only once', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  let requests = 0;
  let finish;
  const stop = startChartPolling({
    load() {
      requests++;
      return new Promise((resolve) => {
        finish = resolve;
      });
    },
    onValue: () => {},
    onError: () => assert.fail('unexpected error'),
    intervalMs: 10,
    timeoutMs: 1000,
  });
  t.mock.timers.tick(100);
  await settle();
  assert.equal(requests, 1);
  finish('ready');
  await settle();
  t.mock.timers.tick(10);
  await settle();
  assert.equal(requests, 2);
  stop();

  let frozenRequests = 0;
  const stopFrozen = startChartPolling({
    load: async () => ++frozenRequests,
    onValue: () => {},
    onError: () => assert.fail('unexpected error'),
    intervalMs: 10,
    repeat: false,
  });
  await settle();
  t.mock.timers.tick(100000);
  await settle();
  assert.equal(frozenRequests, 1);
  stopFrozen();
});
