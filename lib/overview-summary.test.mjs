import test from 'node:test';
import assert from 'node:assert/strict';
import { overviewSummary, todayTotal } from './overview-summary.ts';

const base = {
  online: true,
  model: 'gemma-4-26b',
  pace: 0.085,
  today: 1.83,
  phase: 'watching',
  money: (n) => `$${n.toFixed(n < 0.1 ? 3 : 2)}`,
  name: (m) => m.toUpperCase(),
};

test('one sentence: pace, model, today and the optimizer', () => {
  assert.equal(
    overviewSummary(base),
    'Earning $0.085/hr on GEMMA-4-26B · $1.83 today · optimizer watching demand',
  );
  assert.equal(
    overviewSummary({ ...base, pace: null, phase: 'off' }),
    'Serving GEMMA-4-26B · $1.83 today · optimizer off',
  );
  assert.equal(
    overviewSummary({ ...base, online: false, phase: null }),
    'Darkbloom is offline · $1.83 today',
  );
  assert.equal(overviewSummary({ ...base, online: null }), null);
});

test('a provider loading its startup models is starting, not serving', () => {
  assert.equal(
    overviewSummary({ ...base, starting: true, pace: null, phase: null }),
    'Darkbloom is starting · $1.83 today',
  );
});

test('under the manager the sentence uses the manager’s words', () => {
  const m = { ...base, strategy: 'manager' };
  assert.equal(
    overviewSummary(m),
    'Earning $0.085/hr on GEMMA-4-26B · $1.83 today · manager on',
  );
  assert.match(overviewSummary({ ...m, phase: 'off' }), /manager off$/);
  // Legacy-only phases never describe the manager.
  assert.equal(
    overviewSummary({ ...m, phase: 'measuring' }),
    'Earning $0.085/hr on GEMMA-4-26B · $1.83 today',
  );
});

test('today sums confirmed hours since local midnight', () => {
  const now = new Date(2026, 8, 25, 15, 30).getTime() / 1000;
  const midnight = new Date(2026, 8, 25).getTime() / 1000;
  assert.equal(
    todayTotal(
      [
        { at: midnight - 3600, usd: 5 },
        { at: midnight, usd: 0.5 },
        { at: midnight + 3600, usd: 0.25 },
      ],
      now,
    ),
    0.75,
  );
  assert.equal(todayTotal(undefined, now), null);
});
