import test from 'node:test';
import assert from 'node:assert/strict';
import { pulseBenchmarkView } from './earnings-pulse.ts';

const now = 1_790_000_000;
// native/network_evidence.py benchmark(), as the collector puts it on pulse.peers.
const bench = (over = {}) => ({
  at: now - 120,
  hours: 2,
  cell: 'M5 Pro|48',
  model: 'gemma-4-26b-qat-4bit',
  dedicated: true,
  windows: 24,
  peers: 14,
  percentile: 0.62,
  reqPerHour: 38,
  peerMedianReqPerHour: 31,
  peerZeroShare: 0.1,
  usdPerRequest: 0.0009,
  usdBasis: 'own',
  usdPerHour: 0.0342,
  peerUsdPerHour: 0.0279,
  ...over,
});

test('Macs like yours: percentile, req/h against the median, peer count and dollars', () => {
  const view = pulseBenchmarkView(bench(), now, ['gemma-4-26b-qat-4bit']);
  assert.equal(view.percentile, '62nd percentile');
  assert.equal(view.requests, '38 req/h vs 31 median');
  assert.equal(view.peers, '14 Macs');
  assert.equal(view.tone, 'typical');
  assert.equal(view.usdPerHour, 0.0342);
  assert.equal(view.peerUsdPerHour, 0.0279);
  assert.match(view.title, /14 other M5 Pro · 48 GB Macs .*\(dedicated\).*last 2 h/);
  assert.match(view.title, /own recent pay per request/);
  assert.match(
    pulseBenchmarkView(bench({ usdBasis: 'list', dedicated: false }), now).title,
    /\(mixed\).*list price/,
  );
});

test('ordinals, clamping and tone', () => {
  const pct = (p) => pulseBenchmarkView(bench({ percentile: p }), now).percentile;
  assert.equal(pct(0.01), '1st percentile');
  assert.equal(pct(0.02), '2nd percentile');
  assert.equal(pct(0.03), '3rd percentile');
  assert.equal(pct(0.11), '11th percentile');
  assert.equal(pct(0.12), '12th percentile');
  assert.equal(pct(0.13), '13th percentile');
  assert.equal(pct(0.21), '21st percentile');
  assert.equal(pct(0), '1st percentile');
  assert.equal(pct(1), '99th percentile');
  assert.equal(pct(null), null);
  const tone = (own) => pulseBenchmarkView(bench({ reqPerHour: own }), now).tone;
  assert.equal(tone(40), 'above');
  assert.equal(tone(20), 'below');
  assert.equal(tone(0), 'below');
  assert.equal(
    pulseBenchmarkView(bench({ reqPerHour: 3.25, peerMedianReqPerHour: 812.4 }), now).requests,
    '3.3 req/h vs 812 median',
  );
  assert.equal(pulseBenchmarkView(bench({ usdPerHour: null }), now).usdPerHour, null);
});

test('hidden with fewer than 5 peers, stale windows, another model or a bad payload', () => {
  assert.equal(pulseBenchmarkView(bench({ peers: 4 }), now), null);
  assert.notEqual(pulseBenchmarkView(bench({ peers: 5 }), now), null);
  assert.equal(pulseBenchmarkView(bench({ at: now - 901 }), now), null);
  assert.notEqual(pulseBenchmarkView(bench({ at: now - 900 }), now), null);
  assert.equal(pulseBenchmarkView(bench(), now, ['gpt-oss-20b']), null);
  for (const bad of [
    null,
    'x',
    bench({ reqPerHour: -1 }),
    bench({ percentile: 1.5 }),
    bench({ peerMedianReqPerHour: NaN }),
    bench({ model: 3 }),
    bench({ dedicated: 'yes' }),
    bench({ usdPerHour: 'a lot' }),
  ])
    assert.equal(pulseBenchmarkView(bad, now), null);
});
