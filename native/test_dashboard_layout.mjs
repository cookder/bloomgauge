import test from 'node:test';
import assert from 'node:assert/strict';
import {
  dashboardWidgets,
  dashboardLayoutKey,
  defaultDashboardLayout,
  readDashboardLayout,
  serializeDashboardLayout,
  moveDashboardWidget,
} from '../lib/dashboard-layout.ts';

test('desktop and phone retain independent defaults and storage keys', () => {
  assert.notEqual(dashboardLayoutKey(true), dashboardLayoutKey(false));
  assert.deepEqual(defaultDashboardLayout(false).hidden, []);
  assert.deepEqual(defaultDashboardLayout(true).hidden, [
    'throughput',
    'requests',
    'earnings',
  ]);
  const a = defaultDashboardLayout(false);
  a.order.reverse();
  a.widths.balance = 'full';
  assert.equal(defaultDashboardLayout(false).order[0], 'balance');
  assert.equal(defaultDashboardLayout(false).widths.balance, 'compact');
});
test('unknown, duplicate, missing and oversized saved preferences recover without losing valid choices', () => {
  for (const raw of [
    null,
    'no json',
    'null',
    '{"version":2}',
    ' '.repeat(10001),
  ]) {
    assert.deepEqual(
      readDashboardLayout(raw, true),
      defaultDashboardLayout(true),
    );
  }
  const read = readDashboardLayout(
    JSON.stringify({
      version: 1,
      order: ['optimizer', 'bad', 'optimizer', 'balance'],
      hidden: ['daily', 'bad', 'daily'],
      widths: { pulse: 'compact', balance: 'full', daily: 'arbitrary' },
    }),
    false,
  );
  assert.deepEqual(
    read.order.filter((id) => id === 'optimizer' || id === 'balance'),
    ['optimizer', 'balance'],
  );
  assert.equal(read.order[0], 'optimizer');
  assert.equal(read.order.length, dashboardWidgets.length);
  assert.equal(new Set(read.order).size, dashboardWidgets.length);
  assert.deepEqual(read.hidden, ['daily']);
  assert.equal(read.widths.pulse, 'half');
  assert.equal(read.widths.balance, 'full');
  assert.equal(read.widths.daily, 'full');
});
test('reordering skips hidden targets and preserves every card and width', () => {
  const before = defaultDashboardLayout(true);
  const moved = moveDashboardWidget(before, 'optimizer', 'balance');
  assert.equal(moved.order[0], 'optimizer');
  assert.equal(before.order[0], 'balance');
  assert.deepEqual(new Set(moved.order), new Set(before.order));
  assert.deepEqual(moved.hidden, before.hidden);
  assert.deepEqual(moved.widths, before.widths);
  assert.equal(moveDashboardWidget(before, 'throughput', 'balance'), before);
  assert.equal(moveDashboardWidget(before, 'balance', 'throughput'), before);
});
test('saved all-hidden layouts and selected widths round-trip so reset stays an explicit user choice', () => {
  const layout = defaultDashboardLayout(false);
  layout.hidden = [...layout.order];
  layout.widths.optimizer = 'full';
  layout.order.reverse();
  assert.deepEqual(
    readDashboardLayout(serializeDashboardLayout(layout), false),
    layout,
  );
});

test('a card added in an update appears after its default predecessor in saved layouts', () => {
  const saved = JSON.stringify({
    version: 1,
    order: ['pulse', 'optimizer', 'balance', 'daily'],
    hidden: [],
    widths: {},
  });
  const order = readDashboardLayout(saved, false).order;
  assert.equal(order[order.indexOf('optimizer') + 1], 'demand');
  assert.equal(order.length, dashboardWidgets.length);
});
