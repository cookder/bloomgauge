import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs';
import { registerHooks } from 'node:module';

// Node's test runner needs the bundler's extensionless TS resolution.
registerHooks({
  resolve(specifier, context, next) {
    return next(
      specifier === './cumulative-earnings' || specifier === './model-earnings'
        ? `${specifier}.ts`
        : specifier,
      context,
    );
  },
});
const { overviewHourlyBars, readOverviewHourlyWindow, withHourProjection } =
  await import('./hourly-earnings.ts');
const read = (relative) =>
  fs.readFileSync(new URL(relative, import.meta.url), 'utf8');

const hourStart = 1_790_564_400;
const at = hourStart + 1200;
const hour = (offset, categories) => ({
  at: hourStart - offset * 3600,
  usd: Object.values(categories).reduce((a, b) => a + b, 0),
  jobs: 1,
  categories,
});
const monitor = (hours) => ({
  hours,
  observedAt: at - 5,
  updatedAt: at - 5,
  coverageStartedAt: hourStart - 40 * 3600,
  coverageIntervals: [],
  gaps: 0,
});
const forecast = (status = 'ready') => ({
  hourStart,
  hourEnd: hourStart + 3600,
  earnings: { status, actual: 0.01, additional: 0.02 },
});
const history = [
  hour(20, { gemma: 0.03 }),
  hour(11, { gemma: 0.02, base_reward: 0.01 }),
  hour(1, { qwen: 0.04 }),
  hour(0, { gemma: 0.004, base_reward: 0.006 }),
];

test('the current hour carries the estimated remainder on top of its confirmed amount', () => {
  const bars = overviewHourlyBars(monitor(history), forecast(), at, 12);
  assert.deepEqual(
    bars.rows.map((r) => r.at),
    [hourStart - 11 * 3600, hourStart - 3600, hourStart],
  );
  const now = bars.rows.at(-1);
  assert.equal(now.projected, 0.02);
  assert.ok(Math.abs(now.usd - 0.01) < 1e-9);
  assert.equal(
    bars.rows.slice(0, -1).every((r) => r.projected === null),
    true,
  );
  assert.deepEqual(bars.current, {
    at: hourStart,
    confirmed: now.usd,
    remaining: 0.02,
    projected: now.usd + 0.02,
  });
  // The axis leaves room for the estimate, not only confirmed bars.
  assert.ok(Math.abs(bars.axisMaximum - 0.04) < 1e-9);
  assert.deepEqual(bars.domain, [
    hourStart - 11 * 3600 - 1800,
    hourStart + 1800,
  ]);
});

test('24 h reaches further back and stacks each hour by model, largest first', () => {
  const bars = overviewHourlyBars(monitor(history), forecast(), at, 24);
  assert.equal(bars.rows.length, 4);
  assert.deepEqual(
    bars.series.map((s) => s.model),
    ['gemma', 'qwen', 'base_reward'],
  );
  const gemma = bars.series[0].key;
  assert.equal(bars.rows[0].byModel[gemma], 0.03);
  assert.ok(Math.abs(bars.total - 0.11) < 1e-9);
});

test('no ready forecast means confirmed bars only', () => {
  for (const f of [undefined, forecast('unavailable')]) {
    const bars = overviewHourlyBars(monitor(history), f, at, 12);
    assert.equal(
      bars.rows.every((r) => r.projected === null),
      true,
    );
    assert.equal(bars.current, null);
  }
});

test('a current hour with no recorded row yet still gets its estimate bar', () => {
  const rows = withHourProjection([], forecast(), true);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].at, hourStart);
  assert.equal(rows[0].usd, 0.01);
  assert.equal(rows[0].projected, 0.02);
  assert.deepEqual(withHourProjection([], forecast(), false), []);
  const empty = overviewHourlyBars(undefined, undefined, at, 12);
  assert.deepEqual(empty.rows, []);
  assert.equal(empty.current, null);
});

test('the saved window defaults to 12 h and accepts only 12 or 24', () => {
  assert.equal(readOverviewHourlyWindow(null), 12);
  assert.equal(readOverviewHourlyWindow('24'), 24);
  assert.equal(readOverviewHourlyWindow('12'), 12);
  assert.equal(readOverviewHourlyWindow('48'), 12);
});

test('the hourly bars sit beside the Pulse graph and share the Charts projection', () => {
  const pulse = read('../components/dashboard/earnings-pulse.tsx');
  assert.match(
    pulse,
    /<div className="pulse-layout with-hourly">[\s\S]*<PulseTrend[\s\S]*<\/div>\s*<PulseHourlyBars[\s\S]*?\/>\s*<\/div>/,
  );
  const page = read('../app/page.tsx');
  assert.match(
    page,
    /<EarningsPulse[\s\S]*?monitor=\{data\?\.monitor\}[\s\S]*?\/>/,
  );
  const charts = read('../components/dashboard/widgets.tsx');
  assert.match(charts, /withHourProjection\(\s*hours,\s*earningsForecast,/);
  const bars = read('../components/dashboard/pulse-hourly-bars.tsx');
  assert.match(bars, /localStorage\.getItem\(overviewHourlyKey\)/);
  assert.match(bars, /dataKey="projected"/);
  // Theme tokens only, so the light theme restyles it.
  assert.doesNotMatch(bars, /#[0-9a-fA-F]{3,8}\b/);
  const css = read('../app/globals.css');
  const block = css.slice(css.indexOf('.pulse-hourly {'));
  assert.match(block, /@container pulse-card/);
  assert.doesNotMatch(
    block.slice(0, block.indexOf('@media (max-width: 680px)') + 200),
    /#[0-9a-fA-F]{3,8}\b/,
  );
});
