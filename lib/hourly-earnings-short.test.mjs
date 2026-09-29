import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs';
import { registerHooks } from 'node:module';

// Clock ticks and labels are local time; pin a zone so the checks are stable.
process.env.TZ = 'America/Chicago';
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
const {
  baseRewardValues,
  clockTicks,
  clockTime,
  isShortWindow,
  overviewHourlyWindows,
  shortBarLabel,
  readOverviewHourlyWindow,
  shortEarningsBars,
  shortWindowSpan,
} = await import('./hourly-earnings.ts');
const { modelColor, UNATTRIBUTED } = await import('./model-earnings.ts');
const read = (relative) =>
  fs.readFileSync(new URL(relative, import.meta.url), 'utf8');

// A whole clock hour (and so a 5-minute boundary).
const T0 = 1_790_564_400;
const close = (a, b, message) =>
  assert.ok(Math.abs(a - b) < 1e-9, `${message ?? ''} ${a} ≠ ${b}`);

/** The backend's earnings report: 1-minute points in USD per elapsed hour. */
function report(from, to, series, usdPerMinute) {
  const points = [];
  for (let t = Math.floor(from / 60) * 60; t < to; t += 60) {
    const a = Math.max(from, t),
      b = Math.min(to, t + 60);
    points.push({
      from: a,
      to: b,
      coverageFraction: 1,
      values: series.map((s, k) => {
        const usd = usdPerMinute(t, k);
        return usd === null ? null : (usd * 3600) / (b - a);
      }),
    });
  }
  return {
    from,
    to,
    bucketSeconds: 60,
    series: series.map((name) =>
      ['other', 'unattributed'].includes(name)
        ? { id: name, name: name === 'other' ? 'Other models' : 'Unattributed' }
        : { id: `model:${name}`, name },
    ),
    points,
  };
}
/** The report's account-wide base rewards: USD per elapsed hour per point. */
function withBase(r, usdPerMinute) {
  return {
    ...r,
    baseRewards: {
      attribution: 'account',
      values: r.points.map((p) => {
        const usd = usdPerMinute(p.from);
        return usd === null ? null : (usd * 3600) / (p.to - p.from);
      }),
      total: null,
    },
  };
}
/** What the bars show, back in dollars: each 5-minute bar is $/h ÷ 12. */
const drawnDollars = (bars) =>
  bars.rows.reduce(
    (sum, r) =>
      sum + Object.values(r.byModel).reduce((n, v) => n + (v ?? 0), 0) / 12,
    0,
  );

test('1 h and 3 h are short windows, before 6 h; the rest stay hourly', () => {
  assert.deepEqual([...overviewHourlyWindows], [1, 3, 6, 12, 24, 72, 168]);
  assert.deepEqual(overviewHourlyWindows.filter(isShortWindow), [1, 3]);
  // The saved preference (bloom.overview-hourly.v1) reads 1 and 3 back.
  assert.equal(readOverviewHourlyWindow('1'), 1);
  assert.equal(readOverviewHourlyWindow('3'), 3);
  assert.equal(readOverviewHourlyWindow('6'), 6);
});

test('the requested span is the current 5-minute bucket and the 11 or 35 before it', () => {
  // 7 min 17 s past the hour: `to` drops to whole 30 s for the backend cache.
  assert.deepEqual(shortWindowSpan(T0 + 437, 1), {
    from: T0 + 300 - 11 * 300,
    to: T0 + 420,
  });
  assert.deepEqual(shortWindowSpan(T0 + 437, 3), {
    from: T0 + 300 - 35 * 300,
    to: T0 + 420,
  });
  // Exactly on a boundary: the last hour is 12 whole buckets, none empty.
  assert.deepEqual(shortWindowSpan(T0 + 5, 1), { from: T0 - 3600, to: T0 });
  for (const at of [T0 + 1, T0 + 299, T0 + 300, T0 + 1799]) {
    const span = shortWindowSpan(at, 1);
    assert.equal(span.from % 300, 0);
    assert.ok(span.to <= at && at - span.to < 30);
    assert.ok(span.to - span.from > 55 * 60 && span.to - span.from <= 3600);
  }
});

test('5-minute bars are the bucket’s dollars × 12, stacked by model, and the total is what they show', () => {
  const span = shortWindowSpan(T0 + 437, 1);
  // gemma earns $0.001 every minute; qwen $0.002 once, 10 minutes into the window.
  const r = report(span.from, span.to, ['gemma', 'qwen'], (t, k) =>
    k === 0 ? 0.001 : t === span.from + 600 ? 0.002 : 0,
  );
  const bars = shortEarningsBars(r, 1);
  assert.equal(bars.rows.length, 12);
  assert.equal(bars.bucketSeconds, 300);
  assert.deepEqual(
    bars.rows.map((row) => row.at),
    Array.from({ length: 12 }, (_, i) => span.from + i * 300),
  );
  // Largest earner first, like the hourly bars.
  assert.deepEqual(
    bars.series.map((s) => s.model),
    ['gemma', 'qwen'],
  );
  const [gemma, qwen] = bars.series.map((s) => s.key);
  // $0.005 in 5 minutes = $0.06 per hour.
  close(bars.rows[0].byModel[gemma], 0.06);
  close(bars.rows[0].byModel[qwen], 0);
  close(bars.rows[2].byModel[qwen], 0.024);
  close(bars.rows[2].perHour, 0.084);
  close(bars.rows[2].usd, 0.007);
  // The newest bucket is in progress: 2 recorded minutes so far, still × 12.
  const now = bars.rows.at(-1);
  assert.equal(now.inProgress, true);
  assert.equal(now.through, T0 + 420);
  close(now.byModel[gemma], 0.024);
  assert.equal(
    bars.rows.slice(0, -1).some((row) => row.inProgress),
    false,
  );
  // 11 × $0.005 + $0.002 so far + qwen's $0.002.
  close(bars.total, 0.059);
  close(drawnDollars(bars), bars.total, 'headline = bars');
  close(bars.series[0].usd + bars.series[1].usd, bars.total);
  assert.equal(bars.blanks, 0);
  // Bars sit on their bucket start with half a bucket of padding, as hours do.
  assert.deepEqual(bars.domain, [span.from - 150, T0 + 300 + 150]);
  close(bars.axisMaximum, 0.084);
});

test('3 h draws 36 bars and totals every credit in them', () => {
  const span = shortWindowSpan(T0 + 5, 3);
  const r = report(span.from, span.to, ['gemma'], () => 0.0005);
  const bars = shortEarningsBars(r, 3);
  assert.equal(bars.rows.length, 36);
  assert.equal(bars.rows[0].at, T0 - 3 * 3600);
  assert.equal(bars.rows.at(-1).at + 300, T0);
  assert.equal(bars.rows.at(-1).inProgress, false);
  for (const row of bars.rows) close(row.perHour, 0.03);
  close(bars.total, 180 * 0.0005);
  close(drawnDollars(bars), bars.total);
});

test('window edges: credits before the first bucket are not counted; a point across a bucket edge blanks both', () => {
  const span = shortWindowSpan(T0 + 5, 1);
  // The report starts 2 minutes early (e.g. an older response): not in the window.
  const r = report(span.from - 120, span.to, ['gemma'], () => 0.001);
  const bars = shortEarningsBars(r, 1);
  assert.equal(bars.rows[0].at, span.from);
  close(bars.total, 0.06);
  close(drawnDollars(bars), bars.total);
  // A coarser point spanning 4:00–6:00 of the window cannot be split honestly.
  const coarse = report(span.from, span.to, ['gemma'], () => 0.001);
  const i = coarse.points.findIndex((p) => p.from === span.from + 240);
  coarse.points.splice(i, 2, {
    from: span.from + 240,
    to: span.from + 360,
    coverageFraction: 1,
    values: [0.06],
  });
  const split = shortEarningsBars(coarse, 1);
  assert.equal(split.rows[0].known, false);
  assert.equal(split.rows[1].known, false);
  assert.equal(split.rows[0].byModel[split.series[0].key], null);
  assert.equal(split.rows[2].known, true);
  assert.equal(split.blanks, 2);
  close(split.total, 0.05);
  close(drawnDollars(split), split.total);
});

test('an unrecorded minute blanks its bucket (never zero) and is left out of the total', () => {
  const span = shortWindowSpan(T0 + 5, 1);
  const r = report(span.from, span.to, ['gemma', 'qwen'], (t, k) =>
    t === span.from + 1500 ? null : k === 0 ? 0.001 : 0.0002,
  );
  const bars = shortEarningsBars(r, 1);
  const blank = bars.rows[5];
  assert.equal(blank.known, false);
  assert.equal(blank.perHour, null);
  assert.equal(blank.usd, null);
  assert.deepEqual(Object.values(blank.byModel), [null, null]);
  assert.equal(bars.blanks, 1);
  close(bars.total, 11 * 5 * 0.0012);
  close(drawnDollars(bars), bars.total);
  // One model's unknown money blanks only that model's part of the bar.
  const one = report(span.from, span.to, ['gemma', 'qwen'], (t, k) =>
    k === 1 && t === span.from + 60 ? null : 0.001,
  );
  const partial = shortEarningsBars(one, 1);
  const [gemma, qwen] = partial.series.map((s) => s.key);
  close(partial.rows[0].byModel[gemma], 0.06);
  assert.equal(partial.rows[0].byModel[qwen], null);
  close(partial.total, 60 * 0.001 + 55 * 0.001);
  close(drawnDollars(partial), partial.total);
});

test('the newest minutes awaiting the next poll shorten the current bar instead of blanking it', () => {
  const span = shortWindowSpan(T0 + 437, 1);
  // Covered through 5:00; 5:00–7:00 not yet polled.
  const r = report(span.from, span.to, ['gemma'], (t) =>
    t >= T0 + 300 ? null : 0.001,
  );
  const bars = shortEarningsBars(r, 1);
  assert.equal(bars.recordedTo, T0 + 300);
  const now = bars.rows.at(-1);
  assert.equal(now.inProgress, true);
  assert.equal(now.known, false);
  assert.equal(now.through, now.at);
  assert.equal(bars.blanks, 0);
  close(bars.total, 55 * 0.001);
  assert.equal(bars.recorded, true);
  // An older silence is a gap, not "still recording".
  const stale = report(span.from, span.to, ['gemma'], (t) =>
    t >= T0 - 600 ? null : 0.001,
  );
  const gap = shortEarningsBars(stale, 1);
  assert.equal(gap.recordedTo, T0 + 300);
  assert.equal(gap.rows.at(-5).known, true);
  for (const i of [-4, -3, -2]) assert.equal(gap.rows.at(i).known, false);
  assert.equal(gap.blanks, 3);
  close(gap.total, 40 * 0.001);
  close(drawnDollars(gap), gap.total);
});

test('just after a 5-minute mark, the finished bar is blank until its last minute is polled, never lower', () => {
  const span = shortWindowSpan(T0 + 437, 1);
  // 30 s into the next bucket: 4:00–5:00 of the finished one isn't polled yet.
  const late = report(span.from, T0 + 330, ['gemma'], (t) =>
    t >= T0 + 240 ? null : 0.001,
  );
  const bars = shortEarningsBars(late, 1);
  assert.equal(bars.recordedTo, T0 + 300);
  const finished = bars.rows.at(-2);
  assert.equal(finished.inProgress, false);
  assert.equal(finished.known, false);
  assert.equal(finished.usd, null);
  assert.equal(bars.blanks, 1);
  assert.equal(bars.rows.at(-1).inProgress, true);
  close(bars.total, 11 * 0.005 - 0.005);
  close(drawnDollars(bars), bars.total);
  // Once polled it is drawn in full: $0.005, never a lower partial amount.
  const polled = report(span.from, T0 + 360, ['gemma'], () => 0.001);
  close(shortEarningsBars(polled, 1).rows.at(-2).usd, 0.005);
  // Ending exactly on a mark: every bar is finished, none holds minutes back.
  const edge = shortWindowSpan(T0 + 5, 1);
  const onMark = report(edge.from, edge.to, ['gemma'], (t) =>
    t >= T0 - 60 ? null : 0.001,
  );
  const marked = shortEarningsBars(onMark, 1);
  assert.equal(marked.rows.at(-1).inProgress, false);
  assert.equal(marked.rows.at(-1).known, false);
  assert.equal(marked.recordedTo, T0);
  assert.equal(marked.blanks, 1);
});

test('nothing recorded (or an unavailable report) is "—", not $0.00; a covered idle hour is $0.00', () => {
  const span = shortWindowSpan(T0 + 5, 1);
  // The backend's "unavailable" earnings report: no series, empty values.
  const unavailable = {
    ...report(span.from, span.to, [], () => 0),
    status: 'unavailable',
  };
  const none = shortEarningsBars(unavailable, 1);
  assert.equal(none.recorded, false);
  assert.equal(
    none.rows.some((r) => r.known),
    false,
  );
  // Even with values, an unavailable report records nothing.
  const odd = {
    ...report(span.from, span.to, ['gemma'], () => 0.001),
    status: 'unavailable',
  };
  assert.equal(shortEarningsBars(odd, 1).recorded, false);
  // Never polled in the window.
  const unpolled = report(span.from, span.to, ['unattributed'], () => null);
  assert.equal(shortEarningsBars(unpolled, 1).recorded, false);
  // Polled, nothing earned: a real $0.00.
  const idle = shortEarningsBars(
    report(span.from, span.to, ['unattributed'], () => 0),
    1,
  );
  assert.equal(idle.recorded, true);
  assert.equal(idle.total, 0);
  assert.deepEqual(idle.series, []);
});

test('signed corrections stay signed; grouped and empty series are labelled honestly', () => {
  const span = shortWindowSpan(T0 + 5, 1);
  const r = report(
    span.from,
    span.to,
    ['gemma', 'other', 'unattributed'],
    (t, k) => (k === 0 ? 0.001 : k === 1 ? (t === span.from ? -0.003 : 0) : 0),
  );
  const bars = shortEarningsBars(r, 1);
  // The all-zero "Unattributed" series is not drawn or listed.
  assert.deepEqual(
    bars.series.map((s) => [s.model, s.label, s.color]),
    [
      ['gemma', undefined, bars.series[0].color],
      ['__other_models__', 'Other models', 'var(--c-899da8)'],
    ],
  );
  const other = bars.series[1].key;
  close(bars.rows[0].byModel[other], -0.036);
  close(bars.rows[0].perHour, 0.06 - 0.036);
  close(bars.total, 0.06 - 0.003);
  close(drawnDollars(bars), bars.total);
  // The axis leaves room for the negative bar too.
  close(bars.axisMaximum, 0.06);
  const unattributed = report(
    span.from,
    span.to,
    ['unattributed'],
    () => 0.001,
  );
  assert.equal(
    shortEarningsBars(unattributed, 1).series[0].model,
    UNATTRIBUTED,
  );
  // No report yet: nothing drawn, nothing counted.
  const none = shortEarningsBars(null, 1);
  assert.deepEqual(none.series, []);
  assert.equal(none.total, 0);
});

test('base rewards stack as their own series, labelled and coloured as in the hourly bars, and count in the total', () => {
  const span = shortWindowSpan(T0 + 437, 1);
  // gemma $0.001 a minute; a base reward of $0.002 once per 5 minutes (minute 3).
  const r = withBase(
    report(span.from, span.to, ['gemma'], () => 0.001),
    (t) => ((t - span.from) % 300 === 180 ? 0.002 : 0),
  );
  const bars = shortEarningsBars(r, 1);
  assert.equal(bars.baseRewards, true);
  assert.deepEqual(
    bars.series.map((s) => [s.model, s.color]),
    [
      ['gemma', modelColor('gemma')],
      ['base_reward', modelColor('base_reward')],
    ],
  );
  assert.equal(modelColor('base_reward'), 'var(--c-8493a8)');
  const [gemma, base] = bars.series.map((s) => s.key);
  close(bars.rows[0].byModel[gemma], 0.06);
  close(bars.rows[0].byModel[base], 0.024);
  close(bars.rows[0].perHour, 0.084);
  // In progress (2 minutes recorded): no base reward yet in this bucket.
  close(bars.rows.at(-1).byModel[base], 0);
  // 57 minutes of gemma + 11 base rewards.
  close(bars.total, 57 * 0.001 + 11 * 0.002);
  close(drawnDollars(bars), bars.total, 'headline = bars');
  close(bars.series[1].usd, 11 * 0.002);
});

test('an unknown base-reward minute blanks only that part; unpolled minutes are still pending', () => {
  const span = shortWindowSpan(T0 + 437, 1);
  const r = withBase(
    report(span.from, span.to, ['gemma'], (t) =>
      t >= T0 + 360 ? null : 0.001,
    ),
    (t) => (t >= T0 + 360 ? null : t === span.from + 60 ? null : 0.0004),
  );
  const bars = shortEarningsBars(r, 1);
  const [gemma, base] = bars.series.map((s) => s.key);
  assert.equal(bars.rows[0].known, true);
  close(bars.rows[0].byModel[gemma], 0.06);
  assert.equal(bars.rows[0].byModel[base], null);
  close(bars.rows[1].byModel[base], 0.024);
  // Both sources unpolled after 6:00: the current bar ends there, not blank.
  assert.equal(bars.recordedTo, T0 + 360);
  close(bars.rows.at(-1).usd, 0.0014);
  assert.equal(bars.blanks, 0);
  close(drawnDollars(bars), bars.total);
});

test('a report without well-formed base rewards draws inference only and says so', () => {
  const span = shortWindowSpan(T0 + 5, 1);
  const r = report(span.from, span.to, ['gemma'], () => 0.001);
  const good = withBase(r, () => 0.0001);
  assert.equal(baseRewardValues(good).length, r.points.length);
  for (const broken of [
    r,
    { ...good, baseRewards: null },
    { ...good, baseRewards: { ...good.baseRewards, attribution: 'this_mac' } },
    {
      ...good,
      baseRewards: {
        ...good.baseRewards,
        values: good.baseRewards.values.slice(1),
      },
    },
    {
      ...good,
      baseRewards: {
        ...good.baseRewards,
        values: good.baseRewards.values.map((v, i) => (i ? v : NaN)),
      },
    },
  ]) {
    assert.equal(baseRewardValues(broken), null);
    const bars = shortEarningsBars(broken, 1);
    assert.equal(bars.baseRewards, false);
    assert.deepEqual(
      bars.series.map((s) => s.model),
      ['gemma'],
    );
    close(bars.total, 0.06);
  }
  close(shortEarningsBars(good, 1).total, 0.06 + 0.006);
});

test('short-range ticks are local clock times every 15 or 30 minutes', () => {
  const span = shortWindowSpan(T0 + 5, 1);
  const bars = shortEarningsBars(
    report(span.from, span.to, ['gemma'], () => 0.001),
    1,
  );
  const quarter = clockTicks(bars.domain, 15);
  assert.deepEqual(quarter, [T0 - 3600, T0 - 2700, T0 - 1800, T0 - 900]);
  const half = clockTicks([T0 - 3 * 3600 - 150, T0 - 150], 30);
  assert.equal(half.length, 6);
  assert.ok(half.every((t) => (t - T0) % 1800 === 0));
  // T0 is 10 PM in Chicago (CDT); 15 min later reads 10:15, no AM/PM.
  assert.equal(clockTime(T0 + 900), '10:15');
  assert.match(clockTime(T0 - 2700), /^\d{1,2}:\d{2}$/);
});

test('the DST fall-back night keeps ticks through the repeated hour', () => {
  // Nov 1, 2026: 1:00 CDT (06:00 UTC); at 2:00 CDT clocks go back to 1:00 CST.
  const oneCdt = Date.UTC(2026, 10, 1, 6) / 1000;
  const half = clockTicks([oneCdt - 150, oneCdt + 3 * 3600 - 150], 30);
  assert.deepEqual(
    half,
    Array.from({ length: 6 }, (_, i) => oneCdt + i * 1800),
  );
  assert.deepEqual(half.map(clockTime), [
    '1:00',
    '1:30',
    '1:00',
    '1:30',
    '2:00',
    '2:30',
  ]);
  const quarter = clockTicks([oneCdt + 2700 - 150, oneCdt + 6300 - 150], 15);
  assert.deepEqual(quarter.map(clockTime), ['1:45', '1:00', '1:15', '1:30']);
});

test('a bar in progress is titled with its whole 5 minutes and "so far"', () => {
  // Under a minute recorded no longer reads "10:15–10:15 so far".
  assert.equal(
    shortBarLabel({ at: T0 + 900, end: T0 + 1200, inProgress: true }),
    '10:15–10:20 · so far',
  );
  assert.equal(
    shortBarLabel({ at: T0 + 900, end: T0 + 1200, inProgress: false }),
    '10:15–10:20',
  );
});

test('the Pulse reads short ranges from the recorded-credits report, with base rewards', () => {
  const bars = read('../components/dashboard/pulse-hourly-bars.tsx');
  assert.match(bars, /\/api\/network\/contributions\?metric=earnings/);
  assert.match(bars, /readNetworkContributions\(/);
  assert.match(bars, /shortEarningsBars\(/);
  assert.match(bars, /shortWindowSpan\(/);
  // A restarted poller clears an old failure; the headline needs a recorded bucket.
  assert.match(bars, /setError\(null\);\s*return startChartPolling\(/);
  assert.match(bars, /short \? fine\?\.recorded : monitor/);
  assert.match(bars, /shortBarLabel\(row\)/);
  assert.match(bars, /localStorage\.getItem\(overviewHourlyKey\)/);
  assert.match(bars, /all models \+ base rewards/);
  // Only a report without base rewards keeps the caveat.
  assert.match(
    bars,
    /fine && !fine\.baseRewards[\s\S]*?base rewards show in the hourly views/,
  );
  assert.doesNotMatch(bars, /#[0-9a-fA-F]{3,8}\b/);
  const pulse = read('../components/dashboard/earnings-pulse.tsx');
  assert.match(
    pulse,
    /<PulseHourlyBars[\s\S]*?paused=\{paused\}[\s\S]*?connected=\{connected\}[\s\S]*?today=/,
  );
});
