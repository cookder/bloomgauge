import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';
const read = (file) =>
  readFileSync(new URL('../' + file, import.meta.url), 'utf8');
const code = ts.transpileModule(read('lib/website-links.ts'), {
  compilerOptions: { module: ts.ModuleKind.ESNext },
}).outputText;
const { bloomWebsite, bloomWebsiteLinks } = await import(
  'data:text/javascript;base64,' + Buffer.from(code).toString('base64')
);
test('public website shortcuts are exact URLs without private state or tracking', () => {
  assert.deepEqual(Object.values(bloomWebsite), [
    'https://bloomformac.com/',
    'https://bloomformac.com/changelog',
    'https://bloomformac.com/beta/quickstart',
    'https://bloomformac.com/support',
  ]);
  assert.equal(bloomWebsiteLinks.length, 4);
  for (const link of bloomWebsiteLinks) {
    const url = new URL(link.href);
    assert.equal(url.origin, 'https://bloomformac.com');
    assert.equal(url.search, '');
    assert.equal(url.hash, '');
    assert.equal(url.username, '');
    assert.equal(url.password, '');
  }
});
test('both shared navigation surfaces expose the links; changelog is available from bundled notes', () => {
  for (const file of [
    'components/dashboard/app-navigation.tsx',
    'components/dashboard/support-diagnostics.tsx',
  ])
    assert.match(read(file), /<WebsiteLinks\s*\/>/);
  const links = read('components/dashboard/website-links.tsx');
  assert.match(links, /target="_blank"/);
  assert.match(links, /rel="noopener noreferrer"/);
  assert.match(links, /referrerPolicy="no-referrer"/);
  assert.doesNotMatch(
    links,
    /fetch\(|window\.|localStorage|postMessage|onClick/,
  );
  assert.match(
    read('components/dashboard/release-notes.tsx'),
    /href=\{bloomWebsite.changelog\}/,
  );
  const native = read('native/App.swift');
  assert.match(
    native,
    /navigationAction.navigationType == \.linkActivated && navigationAction.sourceFrame.isMainFrame && isBloomHelpLink/,
  );
});
