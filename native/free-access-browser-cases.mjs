import assert from 'node:assert/strict';
import path from 'node:path';
// About BloomGauge: everything is free, with no activation, trial or purchase flow.
export async function freeAccessBrowserCases(page, url, out, engine, width) {
  let planCalls = 0;
  await page.unroute('**/api/setup');
  // The paid-era /api/plan endpoint is gone; the UI must not call it.
  await page.route('**/api/plan', (route) => {
    planCalls++;
    return route.fulfill({ status: 404, body: '' });
  });
  try {
    await page.goto(url.href + '?screen=overview');
    await page.locator('main.dashboard').waitFor();
    await page
      .getByRole('navigation', { name: 'Main navigation' })
      .getByRole('button', { name: 'More', exact: true })
      .click();
    await page
      .locator('.navigation-hub')
      .getByRole('button', { name: /^About BloomGauge / })
      .click();
    const panel = page.locator('section.bloom-plan');
    await panel.getByRole('heading', { name: /^BloomGauge is free\./ }).waitFor();
    assert.equal(await panel.locator('textarea,input').count(), 0);
    assert.equal(
      await panel
        .getByRole('button', { name: /trial|activate|buy|upgrade/i })
        .count(),
      0,
    );
    assert.equal(
      await panel
        .getByText(/30 days remaining|one-time purchase|subscription|BloomGauge Pro/)
        .count(),
      0,
    );
    assert.equal(planCalls, 0);
    assert.equal(
      await page.evaluate(
        () => document.documentElement.scrollWidth > innerWidth,
      ),
      false,
    );
    await page.screenshot({
      path: path.join(out, `free-access-${engine}-${width}.png`),
      fullPage: true,
    });
    return [
      'free access: no activation, trial or purchase flow',
      'free access: no calls to the removed plan endpoint',
      'free access: responsive layout',
    ];
  } finally {
    await page.unroute('**/api/plan');
  }
}
