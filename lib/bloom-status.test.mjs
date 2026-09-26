import test from 'node:test';
import assert from 'node:assert/strict';
import { bloomHealth, advanceBloomStatus } from './bloom-status.ts';
const data = (at) => ({
  at,
  earnings: { status: 'ok', updatedAt: at, error: null },
  provider: { online: true, tracking: { counting: true } },
  pulse: { status: 'live' },
});
const health = (d, now = 100, error = '', paused = false, visible = true) =>
  bloomHealth(d, now, 0, error, paused, visible);
test('green requires fresh dashboard and earnings, not merely a successful HTTP request', () => {
  assert.equal(health(data(100)).tone, 'green');
  assert.equal(
    health({ ...data(100), earnings: { status: 'stale', updatedAt: 0 } }).tone,
    'yellow',
  );
  assert.equal(
    health({ ...data(100), earnings: { status: 'ok', updatedAt: 20 } }).tone,
    'yellow',
  );
  assert.equal(
    health({ ...data(100), earnings: { status: 'ok', updatedAt: null } }).tone,
    'yellow',
  );
});
test('recent readings tolerate one transport failure without fabricated new readings', () => {
  const d = data(100);
  assert.equal(health(d, 101, 'HTTP 503').tone, 'green');
  assert.match(health(d, 101, 'HTTP 503').detail, /interrupted/);
  assert.equal(health(d, 111, 'HTTP 503').tone, 'yellow');
  assert.equal(health(d, 160, 'HTTP 503').tone, 'red');
  assert.equal(d.at, 100);
});
test('severely stale earnings and clock faults bypass grace', () => {
  for (const value of [
    { ...data(1000), earnings: { status: 'ok', updatedAt: 700 } },
    data(1006),
    data(NaN),
  ]) {
    const h = health(value, 1000);
    assert.equal(h.tone, 'red');
    assert.equal(h.urgent, true);
  }
  assert.equal(
    health({ ...data(100), earnings: { status: 'ok', updatedAt: 106 } }).tone,
    'red',
  );
  assert.equal(
    health(
      { ...data(980), earnings: { status: 'stale', updatedAt: 600 } },
      1000,
      'HTTP 503',
    ).tone,
    'red',
  );
});
test('missing first load becomes attention after a minute; never green', () => {
  assert.equal(bloomHealth(null, 105, 100, '', false, true).tone, 'yellow');
  assert.equal(
    bloomHealth(null, 160, 100, 'HTTP 503', false, true).tone,
    'red',
  );
});
test('provider stopped or unready is distinct from lost dashboard transport', () => {
  assert.equal(
    health({ ...data(100), provider: { online: false } }).tone,
    'yellow',
  );
  assert.equal(
    health({
      ...data(100),
      provider: { online: true, tracking: { counting: false } },
    }).tone,
    'yellow',
  );
  for (const status of ['paused', 'offline', 'unmatched', 'stale'])
    assert.equal(health({ ...data(100), pulse: { status } }).tone, 'yellow');
});
test('paused and background use neutral, not green or false outage', () => {
  assert.equal(health(null, 100, 'HTTP 503', true).tone, 'neutral');
  assert.equal(health(data(1), 100, '', false, false).tone, 'neutral');
});
test('brief degradation does not flap; sustained degradation changes at five seconds', () => {
  let s = advanceBloomStatus(null, { tone: 'green', detail: '' }, 0);
  s = advanceBloomStatus(s, { tone: 'yellow', detail: '' }, 1);
  assert.equal(s.tone, 'green');
  s = advanceBloomStatus(s, { tone: 'green', detail: '' }, 3);
  assert.equal(s.tone, 'green');
  s = advanceBloomStatus(s, { tone: 'yellow', detail: '' }, 4);
  s = advanceBloomStatus(s, { tone: 'yellow', detail: '' }, 8);
  assert.equal(s.tone, 'green');
  s = advanceBloomStatus(s, { tone: 'yellow', detail: '' }, 9);
  assert.equal(s.tone, 'yellow');
});
test('recovery must stay healthy for ten seconds, including after repeated flaps', () => {
  let s = advanceBloomStatus(null, { tone: 'red', detail: '' }, 0);
  s = advanceBloomStatus(s, { tone: 'green', detail: '' }, 1);
  assert.equal(s.tone, 'red');
  s = advanceBloomStatus(s, { tone: 'green', detail: '' }, 10);
  assert.equal(s.tone, 'red');
  s = advanceBloomStatus(s, { tone: 'yellow', detail: '' }, 11);
  assert.equal(s.tone, 'yellow');
  s = advanceBloomStatus(s, { tone: 'green', detail: '' }, 12);
  s = advanceBloomStatus(s, { tone: 'green', detail: '' }, 22);
  assert.equal(s.tone, 'green');
});
test('hard faults and intentional pauses are immediate; backward clock resets debounce', () => {
  let s = advanceBloomStatus(null, { tone: 'green', detail: '' }, 100);
  s = advanceBloomStatus(s, { tone: 'red', detail: '', urgent: true }, 101);
  assert.equal(s.tone, 'red');
  s = advanceBloomStatus(s, { tone: 'neutral', detail: '' }, 102);
  assert.equal(s.tone, 'neutral');
  s = advanceBloomStatus(s, { tone: 'yellow', detail: '' }, 103);
  assert.equal(s.tone, 'yellow');
  s = advanceBloomStatus(s, { tone: 'green', detail: '' }, 104);
  s = advanceBloomStatus(s, { tone: 'green', detail: '' }, 90);
  assert.equal(s.tone, 'yellow');
  assert.equal(s.since, 90);
});
test('the first healthy reading after load shows Live at once; later dips still wait ten seconds', () => {
  let s = advanceBloomStatus(null, { tone: 'yellow', detail: '' }, 0);
  assert.equal(s.tone, 'yellow');
  s = advanceBloomStatus(s, { tone: 'green', detail: '' }, 2);
  assert.equal(s.tone, 'green');
  s = advanceBloomStatus(s, { tone: 'yellow', detail: '' }, 3);
  s = advanceBloomStatus(s, { tone: 'yellow', detail: '' }, 9);
  assert.equal(s.tone, 'yellow');
  s = advanceBloomStatus(s, { tone: 'green', detail: '' }, 10);
  assert.equal(s.tone, 'yellow');
});
