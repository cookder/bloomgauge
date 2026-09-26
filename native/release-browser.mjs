// Browser checks against an isolated --setup-preview collector only.
import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
import { freeAccessBrowserCases } from './free-access-browser-cases.mjs';
const options = Object.fromEntries(
  process.argv
    .slice(2)
    .reduce(
      (a, v, i, all) =>
        i % 2 ? a : [...a, [v.replace(/^--/, ''), all[i + 1]]],
      [],
    ),
);
const { chromium, webkit } = await import(
  pathToFileURL(options.playwright).href
);
const out = options.output;
fs.mkdirSync(out, { recursive: true });
const results = [];
const url = new URL(options.url);
assert.equal(url.hostname, '127.0.0.1');
assert.notEqual(url.port, '8765');
assert.notEqual(url.port, '8766');
for (const engine of ['chromium', 'webkit']) {
  const browser = await (engine === 'chromium'
    ? chromium.launch({
        ...(options.chromium ? { executablePath: options.chromium } : {}),
        headless: true,
      })
    : webkit.launch({ headless: true }));
  try {
    for (const width of [1280, 390, 320]) {
      const context = await browser.newContext({
        viewport: { width, height: 900 },
        acceptDownloads: true,
      });
      const page = await context.newPage();
      const errors = [];
      page.on('pageerror', (e) => errors.push(e.message));
      const row = { engine, width, passed: false, checks: [] };
      results.push(row);
      try {
        // Every viewport exercises the tour. Actual persisted completion is checked separately by the runner.
        await page.route('**/api/setup', async (route) => {
          if (route.request().method() === 'GET') {
            const response = await route.fetch();
            await route.fulfill({
              json: { ...(await response.json()), completed: false },
            });
          } else await route.continue();
        });
        await page.goto(url.href + '?screen=overview');
        await page
          .getByRole('heading', { name: 'Meet your new control room.' })
          .waitFor();
        await page
          .getByRole('button', { name: 'Continue', exact: true })
          .click();
        await page
          .getByRole('heading', { name: 'Your data. Your Mac.' })
          .waitFor();
        await page
          .getByRole('button', { name: 'Continue', exact: true })
          .click();
        await page
          .getByRole('heading', { name: 'Start with a clear view.' })
          .waitFor();
        assert.equal(
          await page
            .getByRole('button', { name: 'Open dashboard', exact: true })
            .isEnabled(),
          false,
        );
        await page.getByRole('checkbox').check();
        await page
          .getByRole('button', { name: 'Open dashboard', exact: true })
          .click();
        await page.locator('main.dashboard').waitFor();
        row.checks.push('first-launch rendering and explicit completion');
        await page
          .getByRole('navigation', { name: 'Main navigation' })
          .getByRole('button', { name: 'More', exact: true })
          .click();
        await page
          .locator('.navigation-hub')
          .getByRole('button', { name: /^Help & feedback / })
          .click();
        row.checks.push('support reachable through normal navigation');
        const panel = page.locator('.support-panel');
        assert.equal(
          await page
            .getByRole('navigation', { name: 'Bloomkeeper website links' })
            .filter({ visible: true })
            .getByRole('link', { name: 'Support website', exact: true })
            .getAttribute('href'),
          'https://bloomformac.com/support',
        );
        assert.equal(
          await panel
            .getByRole('link', { name: 'Email support', exact: true })
            .getAttribute('href'),
          'mailto:support@bloomkeeper.io',
        );
        assert.equal(
          await panel.getByLabel('Diagnostics JSON preview').count(),
          0,
        );
        row.checks.push(
          'help and exact contact routes available without preparing or uploading diagnostics',
        );
        await panel
          .getByRole('button', { name: 'Review diagnostics', exact: true })
          .click();
        await panel
          .getByRole('heading', { name: 'Ready for your review' })
          .waitFor();
        await panel.getByText('View exact report', { exact: true }).click();
        const content = await panel
          .getByLabel('Diagnostics JSON preview')
          .textContent();
        const report = JSON.parse(content);
        assert.equal(report.privacy.earningsIncluded, false);
        assert.equal('earnings' in report, false);
        const pending = page.waitForEvent('download');
        await panel
          .getByRole('button', { name: 'Save report', exact: true })
          .click();
        const download = await pending;
        const file = path.join(out, `diagnostics-${engine}-${width}.json`);
        await download.saveAs(file);
        assert.equal(fs.readFileSync(file, 'utf8'), content);
        row.checks.push('exact preview/download parity; earnings excluded');
        assert.equal(
          await panel
            .getByRole('checkbox', { name: /^Send problem reports/ })
            .isChecked(),
          false,
        );
        row.checks.push('automatic problem reports off by default');
        await panel
          .getByRole('checkbox', {
            name: /^Include recorded inference earnings/,
          })
          .check();
        assert.equal(
          await panel
            .getByRole('button', { name: 'Save report', exact: true })
            .count(),
          0,
        );
        await panel
          .getByRole('button', { name: 'Review diagnostics', exact: true })
          .click();
        await panel
          .getByRole('heading', { name: 'Ready for your review' })
          .waitFor();
        const included = JSON.parse(
          await panel.getByLabel('Diagnostics JSON preview').textContent(),
        );
        assert.equal(included.privacy.earningsIncluded, true);
        row.checks.push('changing inclusion invalidates prior preview');
        await page.screenshot({
          path: path.join(out, `support-${engine}-${width}.png`),
          fullPage: true,
        });
        assert.equal(
          await page.evaluate(
            () => document.documentElement.scrollWidth > innerWidth,
          ),
          false,
        );
        row.checks.push('no horizontal overflow');
        await page.route('**/api/diagnostics/preview', async (route) =>
          route.fulfill({ json: { ...included, generatedAt: null } }),
        );
        await panel
          .getByRole('button', { name: 'Refresh preview', exact: true })
          .click();
        await panel
          .getByRole('alert')
          .filter({ hasText: 'format was not recognized' })
          .waitFor();
        assert.equal(
          await panel
            .getByRole('button', { name: 'Save report', exact: true })
            .count(),
          0,
        );
        await page.unroute('**/api/diagnostics/preview');
        row.checks.push(
          'malformed report rejected without stale download or render failure',
        );
        // A disconnected report request must not retain a stale downloadable report.
        await page.route('**/api/diagnostics/preview', (route) =>
          route.abort('internetdisconnected'),
        );
        await panel
          .getByRole('button', { name: 'Review diagnostics', exact: true })
          .click();
        await panel.getByRole('alert').waitFor();
        assert.equal(
          await panel
            .getByRole('button', { name: 'Save report', exact: true })
            .count(),
          0,
        );
        await page.unroute('**/api/diagnostics/preview');
        await panel
          .getByRole('button', { name: 'Review diagnostics', exact: true })
          .click();
        await panel
          .getByRole('heading', { name: 'Ready for your review' })
          .waitFor();
        row.checks.push('connection failure and successful retry');
        // Exercise the native bridge payload and cancellation feedback in WebKit/Chromium.
        await page.evaluate(() => {
          window.__savedDiagnostics = null;
          window.webkit = {
            messageHandlers: {
              bloomDiagnostics: {
                postMessage(value) {
                  window.__savedDiagnostics = value;
                },
              },
            },
          };
        });
        await panel
          .getByRole('button', { name: 'Save report', exact: true })
          .click();
        const native = await page.evaluate(() => window.__savedDiagnostics);
        assert.equal(native.action, 'save');
        assert.equal(
          native.content,
          await panel.getByLabel('Diagnostics JSON preview').textContent(),
        );
        await page.evaluate(
          (id) =>
            window.dispatchEvent(
              new CustomEvent('bloom-diagnostics-saved', {
                detail: { requestId: id, status: 'cancelled' },
              }),
            ),
          native.requestId,
        );
        await panel
          .getByRole('status')
          .filter({ hasText: 'Save cancelled' })
          .waitFor();
        row.checks.push(
          'native bridge exact payload and cancel feedback (simulated bridge)',
        );
        row.checks.push(
          ...(await freeAccessBrowserCases(page, url, out, engine, width)),
        );
        assert.deepEqual(errors, []);
        row.checks.push('no page exceptions');
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
      }
    }
  } finally {
    await browser.close();
  }
}
const result = {
  passed: results.every((r) => r.passed),
  results,
  physicalPhoneTested: false,
  nativeSavePanelTested: false,
};
fs.writeFileSync(
  path.join(out, 'browser-results.json'),
  JSON.stringify(result, null, 2) + '\n',
);
console.log(
  JSON.stringify({
    passed: result.passed,
    cases: results.length,
    failed: results
      .filter((r) => !r.passed)
      .map((r) => ({ engine: r.engine, width: r.width, error: r.error })),
  }),
);
if (!result.passed) process.exitCode = 1;
