import test from 'node:test';
import assert from 'node:assert/strict';
import {
  learningText,
  startIntroBoost,
  introBoostSeconds,
} from './optimizer-intro.ts';
const reply = (status, body) => ({
  ok: status < 300,
  status,
  json: async () => body,
});
test('learning text uses the saved protect level and learning time', () => {
  assert.match(learningText(0.2, 60), /up to 1 hour a day.*\$0\.20 an hour/);
  assert.match(learningText(0.35, 30), /up to 30 minutes a day.*\$0\.35/);
  assert.match(learningText(0.2, 120), /2 hours a day/);
});
test('boost reads a fresh control version, then starts 3 days', async () => {
  const calls = [];
  const f = async (url, init = {}) => {
    calls.push(init.method || 'GET');
    if (!init.method)
      return reply(200, {
        controlVersion: 'v1',
        demandAuto: { dataGathering: { active: false } },
      });
    assert.deepEqual(JSON.parse(init.body), {
      action: 'data-gathering',
      seconds: introBoostSeconds,
      expectedControl: 'v1',
    });
    return reply(200, {});
  };
  assert.equal(
    await startIntroBoost(new AbortController().signal, f, 0),
    'started',
  );
  assert.deepEqual(calls, ['GET', 'POST']);
});
test('boost retries a changed control version and skips an active boost', async () => {
  let n = 0;
  const f = async (url, init = {}) => {
    if (!init.method) return reply(200, { controlVersion: 'v' + n });
    n++;
    return n < 3 ? reply(400, { error: 'changed' }) : reply(200, {});
  };
  assert.equal(
    await startIntroBoost(new AbortController().signal, f, 0),
    'started',
  );
  assert.equal(n, 3);
  assert.equal(
    await startIntroBoost(
      new AbortController().signal,
      async () =>
        reply(200, {
          controlVersion: 'v',
          demandAuto: { dataGathering: { active: true } },
        }),
      0,
    ),
    'already',
  );
  await assert.rejects(
    startIntroBoost(
      new AbortController().signal,
      async (u, i = {}) =>
        i.method
          ? reply(400, { error: 'Wait for this Mac' })
          : reply(200, { controlVersion: 'v' }),
      0,
    ),
    /Wait for this Mac/,
  );
});
