import test from 'node:test';
import assert from 'node:assert/strict';
import {
  ResponseValidationError,
  startChartPolling,
} from '../lib/chart-polling.ts';

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

test('a response a validator rejects is reported as validation, naming the validator', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout', 'Date'] });
  const issues = [];
  const target = new EventTarget();
  target.addEventListener('bloom-support-issue', (event) =>
    issues.push(event.detail),
  );
  globalThis.window = target;
  t.after(() => delete globalThis.window);
  const run = async (error) => {
    issues.length = 0;
    const stop = startChartPolling({
      load: async () => {
        throw error;
      },
      onValue: () => assert.fail('no value'),
      onError: () => {},
      intervalMs: 15000,
      issueContext: 'models',
    });
    // Failures at 0 s and 30 s stay local; the one at 60 s is reported.
    for (let i = 0; i < 3; i++) {
      if (i) t.mock.timers.tick(30000);
      await settle();
      if (i < 2) assert.deepEqual(issues, []);
    }
    stop();
    return [...issues];
  };
  assert.deepEqual(
    await run(
      new ResponseValidationError('run-status', 'Incomplete run status'),
    ),
    [{ category: 'validation', context: 'models', source: 'run-status' }],
  );
  assert.deepEqual(await run(new Error('Run status unavailable')), [
    { category: 'connection', context: 'models' },
  ]);
  assert.deepEqual(await run(new TypeError('Load failed')), [
    { category: 'connection', context: 'models' },
  ]);
});

test('a different kind of failure starts its own streak before it is reported', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout', 'Date'] });
  const issues = [];
  const target = new EventTarget();
  target.addEventListener('bloom-support-issue', (event) =>
    issues.push(event.detail.category),
  );
  globalThis.window = target;
  t.after(() => delete globalThis.window);
  // Polls run 30 s apart while failing (0, 30, 60 … s).
  const run = async (script) => {
    issues.length = 0;
    let index = 0;
    const stop = startChartPolling({
      load: async () => {
        const step = script[index++];
        if (step === 'V')
          throw new ResponseValidationError('run-status', 'Incomplete');
        if (step === 'C') throw new TypeError('Load failed');
        return 'ok';
      },
      onValue: () => {},
      onError: () => {},
      intervalMs: 15000,
      issueContext: 'models',
    });
    for (let i = 0; i < script.length; i++) {
      if (i) t.mock.timers.tick(30000);
      await settle();
    }
    stop();
    return [...issues];
  };
  // A validator rejection with one timeout in it is never filed as a connection
  // problem, and the rejection's own streak restarts after the timeout.
  assert.deepEqual(await run(['V', 'V', 'V', 'C', 'V', 'V', 'V']), [
    'validation',
    'validation',
  ]);
  // After an outage, one bad response is not filed as validation.
  assert.deepEqual(await run(['C', 'C', 'C', 'V']), ['connection']);
});
