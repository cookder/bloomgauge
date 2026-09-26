// All API mutations are blocked; credits and release messages are synthetic.
import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
const options = Object.fromEntries(
  process.argv
    .slice(2)
    .reduce(
      (out, value, i, args) =>
        i % 2 ? out : [...out, [value.replace(/^--/, ''), args[i + 1]]],
      [],
    ),
);
const { chromium, webkit } = await import(
  pathToFileURL(options.playwright).href
);
const { expect } = await import(
  pathToFileURL(path.join(path.dirname(options.playwright), 'test.mjs')).href
);
const url = new URL(options.url),
  base = url.origin,
  out = options.output;
assert.equal(url.hostname, '127.0.0.1');
assert(!['8765', '8766'].includes(url.port));
fs.mkdirSync(out, { recursive: true });
const snapshot = await (await fetch(base + '/api/snapshot')).json(),
  results = [];
const release = (id = 'beta23', version = '1.36.19') => ({
  installedVersion: version,
  release: {
    id,
    version,
    title: 'Simpler controls. Clearer credits.',
    highlights: [
      {
        title: 'On or Manual',
        detail: 'One place to choose how Bloomkeeper manages models.',
      },
      {
        title: 'Explore your credits',
        detail: 'Sort, filter, and see model totals.',
      },
    ],
  },
});
const timestamp = Date.now() / 1000;
const entries = Array.from({ length: 61 }, (_, i) => ({
  id: i + 1,
  at: new Date((timestamp - 100 + i) * 1000).toISOString(),
  model: i === 60 ? 'base_reward' : i === 59 ? null : i % 2 ? 'gemma' : 'qwen',
  usd: i === 60 ? 2 : i === 0 ? 1 : 0.001 * (i + 1),
  outputTokens: i === 60 ? 0 : i === 59 ? null : 100 + i,
}));
function creditResponse(params) {
  const sort = params.get('sort') || 'newest',
    category = params.get('category') || 'all',
    model = params.get('model') || '',
    page = Number(params.get('page') || 1),
    limit = Number(params.get('limit') || 100);
  const filtered = entries.filter(
    (e) =>
      (!model || e.model === model) &&
      (category === 'all' ||
        (category === 'base_reward' && e.model === 'base_reward') ||
        (category === 'inference' && e.model !== 'base_reward')),
  );
  const total = filtered.reduce((n, e) => n + e.usd, 0),
    inference = filtered
      .filter((e) => e.model !== 'base_reward')
      .reduce((n, e) => n + e.usd, 0);
  const sorted = [...filtered].sort((a, b) =>
    sort === 'oldest'
      ? a.id - b.id
      : sort === 'amount-desc'
        ? b.usd - a.usd || b.id - a.id
        : sort === 'amount-asc'
          ? a.usd - b.usd || b.id - a.id
          : sort === 'tokens-desc'
            ? b.outputTokens - a.outputTokens || b.id - a.id
            : b.id - a.id,
  );
  const leaderboard = [...new Set(filtered.map((e) => e.model))]
    .map((model) => {
      const rows = filtered.filter((e) => e.model === model);
      return {
        model,
        count: rows.length,
        usd: rows.reduce((n, e) => n + e.usd, 0),
        outputTokens: rows.reduce((n, e) => n + e.outputTokens, 0),
      };
    })
    .sort((a, b) => b.usd - a.usd || a.model.localeCompare(b.model));
  return {
    count: filtered.length,
    page,
    limit,
    coverageStart: timestamp - 100,
    coverageEnd: timestamp,
    entries: sorted.slice((page - 1) * limit, page * limit),
    models: ['base_reward', 'gemma', 'qwen'],
    leaderboard,
    summary: {
      count: filtered.length,
      totalUsd: total,
      inferenceUsd: inference,
      baseRewardUsd: total - inference,
      averageUsd: filtered.length ? total / filtered.length : null,
      minUsd: filtered.length ? Math.min(...filtered.map((e) => e.usd)) : null,
      maxUsd: filtered.length ? Math.max(...filtered.map((e) => e.usd)) : null,
      outputTokens: filtered.reduce((n, e) => n + e.outputTokens, 0),
    },
  };
}
for (const engine of options.engine
  ? [options.engine]
  : ['chromium', 'webkit']) {
  const browser = await (engine === 'chromium'
    ? chromium.launch({
        ...(options.chromium ? { executablePath: options.chromium } : {}),
        headless: true,
      })
    : webkit.launch({ headless: true }));
  try {
    for (const width of options.width
      ? [Number(options.width)]
      : [1280, 390, 320]) {
      const context = await browser.newContext({
          viewport: { width, height: 900 },
          reducedMotion: 'reduce',
        }),
        page = await context.newPage(),
        errors = [],
        forbidden = [],
        queries = [];
      const row = { engine, width, passed: false, checks: [] };
      results.push(row);
      let notes = release(),
        malformed = false;
      page.on('pageerror', (e) => errors.push(e.message));
      await context.route('**/*', async (route) => {
        const request = route.request(),
          u = new URL(request.url());
        if (u.origin !== base || !['GET', 'HEAD'].includes(request.method())) {
          forbidden.push(request.url());
          return route.abort();
        }
        if (u.pathname === '/api/setup')
          return route.fulfill({
            json: {
              completed: true,
              localOnly: false,
              compatible: true,
              providerInstalled: true,
              loginPresent: true,
              earningsConnected: true,
            },
          });
        if (u.pathname === '/api/release-notes')
          return route.fulfill({ json: notes });
        if (u.pathname === '/api/credits') {
          queries.push(Object.fromEntries(u.searchParams));
          const value = creditResponse(u.searchParams);
          return route.fulfill({
            json: malformed ? { ...value, summary: null } : value,
          });
        }
        if (u.pathname === '/api/snapshot')
          return route.fulfill({
            json: {
              ...snapshot,
              at: Date.now() / 1000,
              pulse: {
                ...snapshot.pulse,
                events: entries.slice(0, 10).map((e) => ({
                  id: e.id,
                  at: Date.parse(e.at) / 1000,
                  receivedAt: Date.now() / 1000,
                  microUsd: Math.round(e.usd * 1e6),
                  model: e.model,
                })),
              },
            },
          });
        return route.continue();
      });
      const panel = page.locator('.credits-panel'),
        notice = page.getByRole('region', { name: "What's new in Bloomkeeper" });
      const choose = async (label, option) => {
        await page.getByRole('combobox', { name: label, exact: true }).click();
        await page.getByRole('option', { name: option, exact: true }).click();
      };
      try {
        await page.goto(base + '/?screen=credits');
        await expect(panel.locator('.credit-row')).toHaveCount(61);
        await expect(notice).toBeVisible();
        await notice
          .getByRole('button', { name: 'Review changes', exact: true })
          .click();
        await expect(
          notice.getByRole('heading', {
            name: 'Simpler controls. Clearer credits.',
          }),
        ).toBeVisible();
        await notice
          .getByRole('checkbox', { name: 'Show these notices after updates' })
          .uncheck();
        await notice.getByRole('button', { name: 'Done', exact: true }).click();
        await page.reload();
        await expect(panel.locator('.credit-row')).toHaveCount(61);
        await expect(notice).toHaveCount(0);
        row.checks.push(
          'installed update notice expands, dismisses and stays dismissed after reopening',
        );

        await choose('Credits per page', '25 per page');
        await expect(panel.locator('.credit-row')).toHaveCount(25);
        await panel.getByLabel('Go to next page').click();
        await expect(panel.locator('.page-number')).toHaveText('2 / 3');
        assert(queries.at(-1).to);
        await choose('Credit sort order', 'Largest credit');
        await expect(panel.locator('.page-number')).toHaveText('1 / 3');
        await expect(panel.locator('.credit-row').first()).toContainText(
          '$2.000000',
        );
        await expect(
          panel.getByRole('region', {
            name: 'Credit statistics for selected range',
          }),
        ).toHaveCount(0); // named div is not a region
        await expect(panel.locator('.credit-summary')).toContainText(
          '61 credits in this selection',
        );
        row.checks.push(
          'sorting resets pagination; summary covers all 61 records while the page contains 25',
        );

        await choose('Credit category', 'Inference only');
        await expect(panel.locator('.credit-row').first()).toContainText(
          '$1.000000',
        );
        await expect(panel.locator('.credit-summary')).toContainText(
          '60 credits in this selection',
        );
        await choose('Credit model', 'qwen');
        await expect(panel.locator('.credit-summary')).toContainText(
          '30 credits in this selection',
        );
        await expect(panel.locator('.credit-row').first()).toContainText(
          'qwen',
        );
        await choose('Credit model', 'All model labels');
        await panel
          .getByRole('button', { name: 'Model leaderboard', exact: true })
          .click();
        await expect(panel.locator('.credit-rank-row')).toHaveCount(3);
        await expect(panel.locator('.credit-rank-row').first()).toContainText(
          'qwen',
        );
        await expect(
          panel.locator('.credit-rank-row').filter({ hasText: 'Unattributed' }),
        ).toHaveCount(1);
        await expect(panel.locator('.credit-leaderboard')).toContainText(
          'Account totals can include several Macs',
        );
        row.checks.push(
          'category/model filters and model totals use the full range; base rewards excluded from model rank',
        );

        await page.screenshot({
          path: path.join(out, `credits-${engine}-${width}.png`),
          fullPage: true,
        });
        await panel.getByRole('checkbox', { name: 'Show summary' }).uncheck();
        await page.reload();
        await expect(
          panel.getByRole('button', { name: 'Model leaderboard', exact: true }),
        ).toHaveAttribute('aria-pressed', 'true');
        await expect(panel.locator('.credit-summary')).toHaveCount(0);
        await expect(
          page.getByRole('combobox', {
            name: 'Credit sort order',
            exact: true,
          }),
        ).toContainText('Largest credit');
        row.checks.push(
          'sort, category, layout and summary visibility persist locally',
        );

        await page
          .getByRole('navigation', { name: 'Main navigation' })
          .getByRole('button', { name: 'More', exact: true })
          .click();
        await page
          .locator('.navigation-hub')
          .getByRole('button', { name: /^Help & feedback / })
          .click();
        await page
          .getByRole('button', { name: 'What’s new', exact: true })
          .click();
        await expect(notice).toBeVisible();
        await expect(
          notice.getByRole('checkbox', {
            name: 'Show these notices after updates',
          }),
        ).not.toBeChecked();
        await notice
          .getByRole('checkbox', { name: 'Show these notices after updates' })
          .check();
        await notice.getByRole('button', { name: 'Done', exact: true }).click();
        notes = release('beta24', '1.36.20');
        await page.reload();
        await expect(notice).toBeVisible();
        await notice
          .getByRole('button', { name: 'Dismiss release notes' })
          .click();
        notes = { ...release(), installedVersion: '1.33.8' };
        await page.reload();
        await expect(panel).toBeVisible();
        await expect(notice).toHaveCount(0);
        row.checks.push(
          'notes can be reopened in Help; new release prompts once; mismatched installed version cannot announce',
        );

        await page.goto(base + '/?screen=overview');
        const pulse = page.getByRole('region', { name: 'Live earnings pulse' });
        await expect(
          pulse.getByRole('button', { name: 'Recent credits', exact: true }),
        ).toHaveAttribute('aria-expanded', 'false');
        await pulse
          .getByRole('button', { name: 'Recent credits', exact: true })
          .click();
        await expect(pulse.locator('.pulse-feed-row')).toHaveCount(5);
        await choose('Recent credit order', 'Largest credit');
        await expect(pulse.locator('.pulse-feed-row').first()).toContainText(
          '100¢',
        );
        await choose('Recent credits shown', 'Show 10');
        await expect(pulse.locator('.pulse-feed-row')).toHaveCount(10);
        row.checks.push(
          'Pulse credits stay collapsed by default; recent sort and row-count controls work',
        );
        assert.equal(
          await page.evaluate(
            () => document.documentElement.scrollWidth > innerWidth + 1,
          ),
          false,
        );

        malformed = true;
        await page.goto(base + '/?screen=credits');
        await expect(panel.getByRole('alert')).toContainText(
          'incomplete response',
        );
        await expect(panel.locator('.credit-summary')).toHaveCount(0);
        row.checks.push(
          'malformed statistics show a scoped error rather than false totals',
        );
        assert.deepEqual(errors, []);
        assert.deepEqual(forbidden, []);
        row.checks.push(
          'no page exceptions, overflow, external messaging or mutations',
        );
        row.passed = true;
      } catch (error) {
        row.error = String(error.stack || error);
        await page
          .screenshot({
            path: path.join(out, `failure-${engine}-${width}.png`),
            fullPage: true,
          })
          .catch(() => {});
      } finally {
        await context.close();
        fs.writeFileSync(
          path.join(out, 'browser-results.json'),
          JSON.stringify(
            {
              passed: results.every((r) => r.passed),
              results,
              physicalPhoneTested: false,
            },
            null,
            2,
          ),
        );
      }
    }
  } finally {
    await browser.close();
  }
}
const passed = results.every((r) => r.passed);
console.log(
  JSON.stringify({
    passed,
    cases: results.length,
    failed: results
      .filter((r) => !r.passed)
      .map((r) => ({ engine: r.engine, width: r.width, error: r.error })),
  }),
);
if (!passed) process.exitCode = 1;
