import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs';
import { registerHooks } from 'node:module';

// Node's test runner needs the bundler's extensionless TS resolution.
registerHooks({
  resolve(specifier, context, next) {
    return next(
      specifier === './model-label' || specifier === './optimizer-manager'
        ? `${specifier}.ts`
        : specifier,
      context,
    );
  },
});
const {
  modelsNotInCatalog,
  newsLines,
  newsSummary,
  pushText,
  validNetworkNews,
} = await import('./network-news.ts');
const read = (relative) =>
  fs.readFileSync(new URL(relative, import.meta.url), 'utf8');

const now = 1_790_600_000;
const push = {
  enabled: false,
  mutedUntil: null,
  lastSentAt: null,
  perDay: 1,
  available: true,
};
const item = (kind, model, extra = {}) => ({
  at: now - 3600,
  kind,
  model,
  here: null,
  current: false,
  ...extra,
});
const news = (items, extra = {}) => ({
  schema: 1,
  at: now,
  watchingSince: now - 86400,
  items,
  push,
  ...extra,
});

test('validates the backend view strictly', () => {
  assert.equal(validNetworkNews(news([])), true);
  assert.equal(
    validNetworkNews(
      news([
        item('new', 'nvidia-nemotron-3.5-lightning', {
          warm: 12,
          usdPerHour: 0.08,
          returned: false,
          source: 'cell',
        }),
        item('left', 'qwen3.8-flash-next', { here: 'offered', macs: 6 }),
        item('collapse', 'gpt-oss-20b', { before: 124.5, after: 53 }),
      ]),
    ),
    true,
  );
  for (const bad of [
    null,
    [],
    { ...news([]), schema: 2 },
    { ...news([]), push: null },
    { ...news([]), push: { ...push, enabled: 'yes' } },
    news([item('renamed', 'x')]),
    news([item('left', 'x', { here: 'maybe' })]),
    news([item('new', '')]),
    news([item('new', 'x', { usdPerHour: 'lots' })]),
    news([item('new', 'x', { warm: Infinity })]),
  ])
    assert.equal(validNetworkNews(bad), false, JSON.stringify(bad));
});

test('the Optimizer tab and the news share the not-in-catalog check', () => {
  const catalog = [{ id: 'gemma-4-26b-qat-4bit' }, { id: 'gpt-oss-20b' }];
  assert.deepEqual(
    modelsNotInCatalog(
      ['gemma-4-26b-qat-4bit', 'org/prefetched-x', 7, '', 'qwen3.8-flash-next'],
      catalog,
    ),
    ['org/prefetched-x', 'qwen3.8-flash-next'],
  );
  assert.deepEqual(modelsNotInCatalog(undefined, catalog), []);
  assert.deepEqual(modelsNotInCatalog(['a'], undefined), ['a']);
  const tab = read('../components/dashboard/optimizer-tab.tsx');
  assert.match(
    tab,
    /modelsNotInCatalog\(\s*data\?\.reporting\?\.models,\s*data\?\.models,?\s*\)/,
  );
  assert.match(
    tab,
    /<NetworkNews\s+offeredNotInCatalog=\{offeredNotInCatalog\}/,
  );
  // One implementation: the old inline filter is gone.
  assert.doesNotMatch(tab, /reporting\?\.models \?\? \[\]\)\.filter/);
});

test('a new model reads as one line with Macs and $/h on Macs like yours', () => {
  const [line] = newsLines(
    news([
      item('new', 'nvidia-nemotron-3.5-lightning', {
        warm: 12,
        macs: 20,
        demand: 3,
        usdPerHour: 0.084,
        current: true,
      }),
    ]),
  );
  assert.equal(
    line.title,
    'New model on the network: Nvidia Nemotron 3.5 Lightning',
  );
  assert.deepEqual(line.facts, [
    '12 Macs serving',
    '~$0.084/h on Macs like yours',
    '3 requests in flight',
  ]);
  assert.equal(line.flag, null);
  const [early] = newsLines(
    news([item('new', 'ternary-bonsai-2-27b', { serving: 1, returned: true })]),
    { names: { 'ternary-bonsai-2-27b': 'Ternary Bonsai 27B' } },
  );
  assert.equal(early.title, 'Back on the network: Ternary Bonsai 27B');
  assert.deepEqual(early.facts, [
    '1 Mac serving',
    'too few Macs like yours on it for a $/h estimate yet',
  ]);
});

test('a dropped model still on this Mac is flagged with the remove command', () => {
  const lines = newsLines(
    news([
      item('left', 'qwen3.8-flash-next', { here: 'offered', macs: 6 }),
      item('left', 'qwen3-vl-30b-a3b-instruct', {
        here: 'downloaded',
        macs: 111,
      }),
      item('left', 'old-model', { macs: 9 }),
    ]),
  );
  assert.equal(lines[0].title, 'Left Darkbloom’s catalog: Qwen3.8 Flash Next');
  assert.deepEqual(lines[0].facts, ['it had 6 Macs']);
  assert.match(lines[0].flag.text, /still offers it/);
  assert.equal(
    lines[0].flag.command,
    'darkbloom models remove qwen3.8-flash-next',
  );
  assert.match(lines[1].flag.text, /still downloaded/);
  assert.equal(lines[2].flag, null);
  assert.equal(
    newsSummary(news([]), lines, now),
    '2 dropped models still on this Mac',
  );
});

test("the provider's own offer flags a drop the backend didn't catch", () => {
  const view = news([
    item('new', 'old-model', { at: now - 60, warm: 4 }),
    item('left', 'old-model', { at: now - 7200, macs: 9 }),
    item('left', 'gone', { macs: 9 }),
  ]);
  const lines = newsLines(view, { offeredNotInCatalog: ['gone', 'old-model'] });
  // 'old-model' came back: its older 'left' row is history, not a warning.
  assert.deepEqual(
    lines.map((l) => [l.kind, !!l.flag]),
    [
      ['new', false],
      ['left', false],
      ['left', true],
    ],
  );
  assert.match(lines[2].flag.text, /still offers it/);
});

test('capacity swings and the summary line', () => {
  const lines = newsLines(
    news([
      item('surge', 'gpt-oss-20b', { at: now - 60, before: 46, after: 93.4 }),
      item('collapse', 'qwen3-vl-30b-a3b-instruct', {
        at: now - 10 * 86400,
        before: 1234,
        after: 612,
      }),
    ]),
  );
  assert.equal(lines[0].title, 'More Macs keep GPT-OSS 20B warm');
  assert.deepEqual(lines[0].facts, [
    '46 → 93 warm Macs within 15 minutes',
    'more Macs competing for its work',
  ]);
  assert.equal(
    lines[1].title,
    'Fewer Macs keep Qwen3 VL 30B A3B Instruct warm',
  );
  assert.equal(lines[1].facts[0], '1,234 → 612 warm Macs within 15 minutes');
  assert.equal(newsSummary(news([]), lines, now), '1 change this week');
  assert.equal(
    newsSummary(news([]), lines.slice(1), now),
    '1 change this month',
  );
  assert.equal(newsSummary(news([]), [], now), 'No changes');
  assert.equal(
    newsSummary(news([], { watchingSince: null }), [], now),
    'Starting to watch the network',
  );
  assert.equal(newsSummary(null, [], now), 'Not available yet');
  assert.deepEqual(newsLines(null), []);
});

test('the phone notice is off by default, once a day, and can be muted', () => {
  assert.match(pushText(push, now), /^Off\. .*at most one a day/);
  assert.match(
    pushText({ ...push, enabled: true }, now),
    /^On: at most one notice a day/,
  );
  assert.match(
    pushText({ ...push, enabled: true, mutedUntil: now + 7 * 86400 }, now),
    /^Muted until /,
  );
  assert.match(
    pushText({ ...push, enabled: true, mutedUntil: now - 1 }, now),
    /^On:/,
  );
});

test('the panel polls the news, posts with its action header and uses tokens only', () => {
  const panel = read('../components/dashboard/network-news.tsx');
  assert.match(panel, /'\/api\/network\/news'/);
  assert.match(panel, /'X-Bloom-Action': 'network-news'/);
  assert.match(panel, /intervalMs: 60000/);
  assert.match(panel, /\{ action: 'push', enabled: !push\.enabled \}/);
  assert.match(panel, /action: muted \? 'unmute' : 'mute'/);
  const css = read('../app/globals.css');
  const block = css.slice(
    css.indexOf('/* Network news'),
    css.indexOf('.network-news-push .optimizer'),
  );
  assert.ok(block.length > 100);
  assert.doesNotMatch(block, /#[0-9a-f]{3,8}\b/i);
});
