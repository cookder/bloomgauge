// Synthetic browser coverage. Only an isolated --setup-preview server is allowed;
// every control mutation is intercepted, and all external requests are blocked.
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
const snapshot = await (await fetch(base + '/api/snapshot')).json();
const results = [];
const fixture = () => ({
  at: Date.now() / 1000,
  controlVersion: 'control-1',
  providerVersion: 'provider-1',
  currentModel: 'qwen',
  actualMode: 'observe',
  providerRunning: true,
  hasSavedPlan: true,
  firstPlan: false,
  selected: ['qwen', 'gemma', 'saved-missing'],
  demandPolicy: { targetUsdPerHour: 0.12 },
  models: [
    { id: 'qwen', name: 'Qwen', available: true, selected: true },
    { id: 'gemma', name: 'Gemma', available: true, selected: true },
  ],
  warmup: { status: 'ready', detail: 'Ready' },
  automatic: {
    mode: 'manual',
    phase: 'manual',
    detail: 'Manual keeps this model running.',
    canEnable: true,
  },
  operation: null,
  lastRequestId: null,
});
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
        page = await context.newPage();
      const row = { engine, width, passed: false, checks: [] };
      results.push(row);
      const errors = [],
        forbidden = [],
        posts = [];
      let control = fixture(),
        analytics = 0,
        controlReads = 0,
        mode = 'ok',
        hideReceipt = false,
        accepted = null,
        startCount = 0,
        badAnalytics = false,
        failSnapshot = false,
        failControls = false;
      page.on('pageerror', (e) => errors.push(e.message));
      const full = () => ({
        at: Date.now() / 1000,
        mode: 'observe',
        status: 'observing',
        detail: 'Saved settings',
        canManage: true,
        identityVerified: true,
        controlVersion: 'legacy-1',
        selected: control.selected,
        blockHours: 2,
        currentModel: control.currentModel,
        busy: false,
        models: control.models.map((m) => ({
          ...m,
          evidence: { hours: 0, usd: 0, usdPerHour: null },
        })),
        events: [],
        combinations: { candidates: [], results: [], plan: null },
        warmup: { status: 'ready', detail: 'Ready' },
      });
      await context.route('**/*', async (route) => {
        const request = route.request(),
          u = new URL(request.url());
        if (u.origin !== base) {
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
        if (u.pathname === '/api/snapshot')
          return failSnapshot
            ? route.abort()
            : route.fulfill({ json: { ...snapshot, at: Date.now() / 1000 } });
        if (u.pathname === '/api/optimizer/control') {
          if (request.method() === 'POST') {
            const body = request.postDataJSON();
            posts.push(body);
            assert.equal(request.headers()['x-bloom-action'], 'optimizer');
            if (mode === 'reject')
              return route.fulfill({
                status: 409,
                json: {
                  error:
                    'This provider changed. Review the current model before turning on.',
                },
              });
            if (body.action === 'set-automatic') {
              if (body.enabled) {
                if (!accepted || accepted.requestId !== body.requestId) {
                  accepted = body;
                  startCount++;
                } else
                  assert.deepEqual(
                    body,
                    accepted,
                    'Retry must retain the exact accepted request',
                  );
                control = {
                  ...control,
                  automatic: {
                    mode: 'on',
                    phase: 'waiting',
                    detail: 'Waiting for verified readiness.',
                    canEnable: false,
                    intentId: body.requestId,
                  },
                  operation: {
                    id: body.requestId,
                    status: 'waiting',
                    detail: 'Waiting for verified readiness.',
                  },
                  lastRequestId: body.requestId,
                };
              } else {
                assert.deepEqual(Object.keys(body).sort(), [
                  'action',
                  'enabled',
                  'expectedControl',
                  'requestId',
                ]);
                control = {
                  ...control,
                  actualMode: 'observe',
                  automatic: {
                    mode: 'manual',
                    phase: 'manual',
                    detail: 'Manual selected. Serving is unchanged.',
                    canEnable: true,
                  },
                  operation: control.operation
                    ? { ...control.operation, status: 'cancelled' }
                    : null,
                  lastRequestId: body.requestId,
                };
              }
              if (mode === 'lost') {
                mode = 'ok';
                return route.abort();
              }
            } else {
              assert.equal(body.action, 'refresh');
              control.lastRequestId = body.requestId;
            }
          } else {
            controlReads++;
            if (failControls) return route.abort();
          }
          const displayed =
            hideReceipt && request.method() === 'GET'
              ? {
                  ...fixture(),
                  controlVersion: 'newer-catalog',
                  models: [
                    ...fixture().models,
                    { id: 'new', name: 'New', available: true },
                  ],
                }
              : control;
          return route.fulfill({
            json: { ...displayed, at: Date.now() / 1000 },
          });
        }
        if (u.pathname === '/api/optimizer') {
          analytics++;
          assert.equal(
            request.method(),
            'GET',
            'No legacy changes are needed in this fixture',
          );
          return badAnalytics
            ? route.fulfill({ json: { malformed: true } })
            : route.fulfill({ json: full() });
        }
        if (u.pathname === '/api/model-control') {
          assert.equal(request.method(), 'GET');
          if (failControls) return route.abort();
          return route.fulfill({
            json: {
              at: Date.now() / 1000,
              session: 'fixture',
              mode: 'observe',
              currentModel: 'qwen',
              models: control.models,
              switching: false,
              canCancel: false,
              status: 'observing',
              detail: 'Manual controls',
              providerControl: {
                status: control.providerRunning ? 'running' : 'stopped',
                version: 'provider-1',
                model: 'qwen',
                canStart: !control.providerRunning,
                canStop: control.providerRunning,
                canEnableEndpoint: false,
                detail: 'Fixture provider',
              },
              warmup: { status: 'ready', detail: 'Ready' },
            },
          });
        }
        if (!['GET', 'HEAD'].includes(request.method())) {
          forbidden.push(u.pathname);
          return route.fulfill({
            status: 403,
            json: { error: 'QA mutation blocked' },
          });
        }
        return route.continue();
      });
      const card = page.getByRole('region', { name: 'Optimizer control' }); // section with an accessible name
      const on = () =>
        card.getByRole('button', {
          name: 'Optimizer on BloomGauge follows earning opportunities',
          exact: true,
        });
      const manual = () =>
        card.getByRole('button', {
          name: 'Manual You choose the model',
          exact: true,
        });
      async function overview() {
        await page.goto(base + '/?screen=test');
        await expect(on()).toBeEnabled();
      }
      try {
        await overview();
        assert.equal(analytics, 0);
        row.checks.push('overview uses lightweight controls without analytics');
        await on().click();
        await expect(
          card.getByText('Getting ready', { exact: true }),
        ).toBeVisible();
        assert.equal('models' in posts.at(-1), false);
        assert.equal('demandPolicy' in posts.at(-1), false);
        failControls = true;
        await expect(
          card.getByText('Last confirmed mode', { exact: true }),
        ).toBeVisible({ timeout: 12000 });
        await expect(manual()).toBeEnabled();
        await expect(on()).toBeDisabled();
        await manual().click();
        await expect(
          page.getByRole('dialog', { name: 'Choose a model' }),
        ).toBeVisible();
        await page.keyboard.press('Escape');
        await expect(manual()).toHaveAttribute('aria-pressed', 'true');
        failControls = false;
        await card
          .getByRole('button', { name: 'Check again', exact: true })
          .click();
        await expect(
          card.getByText('Manual selected. Serving is unchanged.'),
        ).toBeVisible();
        assert.equal(control.operation.status, 'cancelled');
        row.checks.push(
          'saved On retains plan; Manual cancels queued On with valid payload',
        );
        row.checks.push(
          'known On remains cancellable during a failed status poll; stale On cannot start',
        );

        // Accept once, lose the reply, reopen, then retry against a changed catalog.
        control = fixture();
        accepted = null;
        mode = 'lost';
        hideReceipt = true;
        await overview();
        const startsBefore = startCount;
        await on().click();
        await expect(
          card.getByText('Checking your request', { exact: true }),
        ).toBeVisible();
        const first = posts.at(-1);
        await page.reload();
        await expect(
          card.getByText('Checking your request', { exact: true }),
        ).toBeVisible();
        await on().click();
        hideReceipt = false;
        await expect(
          card.getByText('Getting ready', { exact: true }),
        ).toBeVisible();
        assert.deepEqual(posts.at(-1), first);
        assert.equal(startCount, startsBefore + 1);
        assert.equal(
          await page.evaluate(() =>
            localStorage.getItem('bloom-optimizer-pending-v1'),
          ),
          null,
        );
        row.checks.push(
          'accepted lost reply survives reopening; exact UUID retry starts once and matching receipt clears uncertainty',
        );

        control = {
          ...control,
          automatic: {
            mode: 'manual',
            phase: 'blocked',
            detail: 'Warmup ended before readiness. Try turning on again.',
            canEnable: true,
            blocker: { code: 'timeout', action: 'retry' },
          },
          operation: { ...control.operation, status: 'blocked' },
        };
        await page.reload();
        await card
          .getByRole('button', { name: 'Try turning on again', exact: true })
          .click();
        assert.notEqual(posts.at(-1).requestId, first.requestId);
        await manual().click();
        await expect(
          page.getByRole('dialog', { name: 'Choose a model' }),
        ).toBeVisible();
        await page.keyboard.press('Escape');
        row.checks.push(
          'blocked On offers one direct retry with a new intent; Manual remains available',
        );

        control = fixture();
        accepted = null;
        mode = 'reject';
        await overview();
        await on().click();
        await expect(
          card.getByText(
            'This provider changed. Review the current model before turning on.',
          ),
        ).toBeVisible();
        mode = 'ok';
        await card
          .getByRole('button', { name: 'Check again', exact: true })
          .click();
        await expect(
          card.getByText(
            'This provider changed. Review the current model before turning on.',
          ),
        ).toBeVisible();
        row.checks.push(
          'healthy refresh does not erase authoritative rejection',
        );

        control = {
          ...fixture(),
          firstPlan: true,
          hasSavedPlan: false,
          providerRunning: false,
          selected: [],
          models: fixture().models.map((m) =>
            m.id === 'qwen'
              ? {
                  ...m,
                  available: false,
                  reason: 'Waiting for the saved model to warm.',
                }
              : m,
          ),
        };
        accepted = null;
        await overview();
        await on().click();
        assert.deepEqual(posts.at(-1).models, ['qwen', 'gemma']);
        assert.equal(posts.at(-1).expectedProvider, 'provider-1');
        row.checks.push(
          'first-ever stopped On reviews current cold model plus alternatives',
        );

        control = {
          ...fixture(),
          models: fixture().models.map((m) =>
            m.id === 'gemma'
              ? { ...m, available: false, reason: 'Not currently available.' }
              : m,
          ),
        };
        await overview();
        await card
          .getByRole('button', { name: 'Optimizer settings', exact: true })
          .click();
        const settings = page.locator('.optimizer-settings-panel');
        await settings
          .getByText('Models BloomGauge may choose · 3 selected', { exact: true })
          .click();
        await expect(
          settings.getByRole('checkbox', { name: /Gemma/ }),
        ).toBeChecked();
        await expect(
          settings.getByRole('checkbox', { name: /Gemma/ }),
        ).toBeEnabled();
        await expect(
          settings.getByRole('checkbox', { name: /saved missing/ }),
        ).toBeChecked();
        await settings.getByRole('checkbox', { name: /Gemma/ }).uncheck();
        await expect(
          settings.getByText('Models BloomGauge may choose · 2 selected', {
            exact: true,
          }),
        ).toBeVisible();
        row.checks.push(
          'saved unavailable and missing selections remain visible and removable',
        );

        // Malformed reporting is isolated from controls and has its own retry.
        badAnalytics = true;
        await page
          .getByRole('navigation', { name: 'Optimizer sections' })
          .getByRole('button', { name: 'History', exact: true })
          .click();
        await page
          .getByRole('button', { name: 'Compare & select', exact: true })
          .click();
        await expect(
          page.getByText(/Model comparisons are delayed/),
        ).toBeVisible();
        await page
          .getByRole('navigation', { name: 'Optimizer sections' })
          .getByRole('button', { name: 'Overview', exact: true })
          .click();
        await expect(on()).toBeEnabled();
        await on().click();
        await expect(manual()).toBeEnabled();
        await manual().click();
        await expect(
          page.getByRole('dialog', { name: 'Choose a model' }),
        ).toBeVisible();
        await page.keyboard.press('Escape');
        row.checks.push(
          'malformed history has a scoped error; On/Manual still work',
        );
        badAnalytics = false;

        await card.getByText('Advanced tools', { exact: true }).click();
        await card
          .getByRole('button', { name: 'Pair tests', exact: true })
          .click();
        await expect(
          page.getByRole('heading', { name: 'Try two models together.' }),
        ).toBeVisible();
        await page
          .getByRole('navigation', { name: 'Optimizer sections' })
          .getByRole('button', { name: 'Overview', exact: true })
          .click();
        await card
          .getByRole('button', {
            name: 'Decision log & diagnostics',
            exact: true,
          })
          .click();
        await expect(page.locator('#screen-diagnostics')).toBeVisible();
        row.checks.push(
          'advanced tests and diagnostics remain reachable through normal navigation',
        );

        control = fixture();
        await overview();

        await card
          .locator('summary')
          .filter({ hasText: 'Choose a model or start / stop Darkbloom' })
          .click();
        await expect(
          card.getByRole('button', { name: 'Stop Darkbloom', exact: true }),
        ).toBeVisible();
        failSnapshot = true;
        failControls = true;
        // Reload keeps this fixture deterministic; all failed GETs remain intercepted.
        await page.reload();
        await expect(page.locator('.connection-notice')).toBeVisible();
        assert.equal(await page.locator('.connection-notice').count(), 1);
        await expect(
          card.getByText(
            'Optimizer controls are reconnecting. Retrying automatically.',
            { exact: true },
          ),
        ).toHaveCount(0);
        row.checks.push(
          'combined connection failure has one global notice and no duplicate control error',
        );
        failSnapshot = false;
        failControls = false;
        await overview();

        assert.equal(
          await page.evaluate(
            () => document.documentElement.scrollWidth > innerWidth + 1,
          ),
          false,
        );
        await page.screenshot({
          path: path.join(out, `optimizer-${engine}-${width}.png`),
          fullPage: true,
        });
        assert.deepEqual(errors, []);
        assert.deepEqual(forbidden, []);
        row.checks.push(
          'no overflow, page exceptions, external requests or unmocked mutations',
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
        row.posts = posts;
        row.analyticsReads = analytics;
        row.controlReads = controlReads;
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
