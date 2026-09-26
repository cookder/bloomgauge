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
