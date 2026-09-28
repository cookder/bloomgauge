import test from 'node:test';
import assert from 'node:assert/strict';
import {
  networkOutage,
  outageEffect,
  outageMessage,
  cellLabel,
} from './network-health.ts';

const outage = {
  since: 1790000000,
  scope: 'network',
  key: null,
  dropPct: 61.6,
  detail: 'Macs ready to serve: 1,322 before, 508 at the lowest.',
  signal: 'warm',
};
const at = () => '14:48';

test('the banner names the time, the share of Macs and that nothing is wrong here', () => {
  const o = networkOutage({ status: 'outage', outage });
  assert.equal(
    outageMessage(o, at),
    'Darkbloom network problem since 14:48: 62% of Macs stopped getting work. Nothing to fix on this Mac.',
  );
  assert.equal(
    outageMessage(
      networkOutage({ outage: { ...outage, signal: 'api', dropPct: 100 } }),
      at,
    ),
    'Darkbloom network problem since 14:48: Darkbloom’s servers stopped answering, so Macs aren’t getting work. Nothing to fix on this Mac.',
  );
  assert.equal(
    outageMessage(
      networkOutage({
        outage: {
          ...outage,
          scope: 'model',
          key: 'gemma-4-26b-qat-4bit',
          signal: 'model',
        },
      }),
      at,
      (m) => m.split('-').slice(0, 2).join('-'),
    ),
    'Darkbloom network problem since 14:48: 62% of Macs serving gemma-4 stopped getting work. Nothing to fix on this Mac.',
  );
  assert.equal(
    outageMessage(
      networkOutage({ outage: { ...outage, scope: 'cell', key: 'M5 Pro|48' } }),
      at,
    ),
    'Darkbloom network problem since 14:48: 62% of M5 Pro 48 GB Macs stopped getting work. Nothing to fix on this Mac.',
  );
});

test('no outage, or a malformed one, shows no banner', () => {
  assert.equal(networkOutage({ status: 'ok', outage: null }), null);
  assert.equal(networkOutage(null), null);
  assert.equal(networkOutage({ outage: { ...outage, since: 'x' } }), null);
  assert.equal(networkOutage({ outage: { ...outage, dropPct: 140 } }), null);
  assert.equal(networkOutage({ outage: { ...outage, scope: 'planet' } }), null);
  assert.equal(
    networkOutage({ outage: { ...outage, scope: 'model', key: null } }),
    null,
  );
  assert.equal(
    networkOutage({ outage: { ...outage, signal: 'other' } }).signal,
    null,
  );
});

test('what Bloomkeeper holds matches the scope (one model: only its restart)', () => {
  const network = networkOutage({ outage });
  const cell = networkOutage({
    outage: { ...outage, scope: 'cell', key: 'M5 Pro|48' },
  });
  const model = networkOutage({
    outage: { ...outage, scope: 'model', key: 'gemma-4-26b-qat-4bit' },
  });
  for (const o of [network, cell])
    assert.equal(
      outageEffect(o),
      'Bloomkeeper won’t restart or switch models because of it, and automatic problem reports wait until it’s over.',
    );
  assert.equal(
    outageEffect(model),
    'Bloomkeeper won’t restart the provider for it; moving to another model is still allowed, and automatic problem reports wait until it’s over.',
  );
});

test('cell labels', () => {
  assert.equal(cellLabel('M5 Pro|48'), 'M5 Pro 48 GB');
  assert.equal(cellLabel('M4|24'), 'M4 24 GB');
  assert.equal(cellLabel('odd'), 'odd');
});
