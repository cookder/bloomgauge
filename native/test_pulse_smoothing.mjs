import test from 'node:test';
import assert from 'node:assert/strict';
import {
  smoothPulse,
  pulseSegments,
  readPulseHistory,
} from '../lib/pulse-smoothing.ts';
test('malformed history and late responses from another session never enter the chart', () => {
  const valid = {
    sessionId: 2,
    samples: [{ at: 100, rate60: 0, rate300: null }],
    coverageStart: 100,
    bucketSeconds: 1,
  };
  assert.equal(readPulseHistory(valid, 2), valid);
  for (const response of [
    null,
    {},
    { ...valid, sessionId: 1 },
    { ...valid, samples: [null] },
    { ...valid, samples: [{ at: 100, rate60: '0', rate300: null }] },
    { ...valid, bucketSeconds: 0 },
  ]) {
    assert.throws(() => readPulseHistory(response, 2));
  }
});
test('reduces isolated spikes without changing original readings', () => {
  const rows = [
    { at: 0, value: 0 },
    { at: 1, value: 1 },
    { at: 2, value: 0 },
  ];
  const result = smoothPulse(rows, 'value', 60, 5);
  assert.ok(result[1].value > 0 && result[1].value < 0.02);
  assert.equal(rows[1].value, 1);
});
test('missing observations and long gaps reset the average', () => {
  const result = smoothPulse(
    [
      { at: 0, value: 1 },
      { at: 1, value: null },
      { at: 2, value: 0 },
      { at: 100, value: -1 },
    ],
    'value',
    60,
    5,
  );
  assert.equal(result[1].value, null);
  assert.equal(result[2].value, 0);
  assert.equal(result[3].value, -1);
});
test('time-based smoothing is consistent across sampling intervals', () => {
  const a = smoothPulse(
    [
      { at: 0, value: 0 },
      { at: 1, value: 1 },
      { at: 2, value: 1 },
    ],
    'value',
    60,
    5,
  );
  const b = smoothPulse(
    [
      { at: 0, value: 0 },
      { at: 2, value: 1 },
    ],
    'value',
    60,
    5,
  );
  assert.ok(Math.abs(a[2].value - b[1].value) < 1e-12);
  assert.equal(
    smoothPulse(
      [
        { at: 0, value: 0 },
        { at: 1, value: 0 },
      ],
      'value',
      60,
      5,
    )[1].value,
    0,
  );
});
test('drawing breaks across implicit gaps, missing readings, and clock reversals', () => {
  const rows = [
    { at: 0, value: 0 },
    { at: 1, value: 1 },
    { at: 30, value: 2 },
    { at: 31, value: null },
    { at: 32, value: 3 },
    { at: 20, value: 4 },
    { at: 21, value: NaN },
    { at: 22, value: -1 },
  ];
  const runs = pulseSegments(rows, 'value', 5);
  assert.deepEqual(
    runs.map((run) => run.map((row) => row.at)),
    [[0, 1], [30], [32], [20], [22]],
  );
  assert.equal(runs[0][0].value, 0);
  assert.equal(runs.at(-1)[0].value, -1);
});

test('cross-model responses require complete metadata and a matching live session', () => {
  const valid = {
    scope: 'models',
    sessionId: 2,
    sessions: [{ id: 1, models: ['gemma'], firstAt: 100, lastAt: 100 }],
    samples: [{ at: 100, rate60: 0, rate300: null, sessionId: 1 }],
    coverageStart: 100,
    bucketSeconds: 1,
  };
  assert.equal(readPulseHistory(valid, 2, true), valid);
  for (const response of [
    { ...valid, scope: undefined },
    { ...valid, sessionId: 1 },
    { ...valid, sessions: [] },
    { ...valid, samples: [{ ...valid.samples[0], sessionId: 3 }] },
    { ...valid, sessions: [{ ...valid.sessions[0], models: [null] }] },
    { ...valid, samples: [valid.samples[0], { ...valid.samples[0], at: 99 }] },
  ])
    assert.throws(() => readPulseHistory(response, 2, true));
});
