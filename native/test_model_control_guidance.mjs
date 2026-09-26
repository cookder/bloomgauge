import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
const read = (relative) =>
  fs.readFileSync(new URL(relative, import.meta.url), 'utf8');
test('current navigation names model controls without restoring a Controller tab', () => {
  const navigation = read('../components/dashboard/app-navigation.tsx');
  assert.match(navigation, /\[\s*'switch',\s*'Model controls',?\s*\]/);
  assert.doesNotMatch(navigation, /\bController\b/);
  assert.match(navigation, /\[\s*'test',\s*'demand',\s*'results',?\s*\]/);
});
test('current UI guidance uses the simplified model controls', () => {
  for (const file of [
    'first-launch',
    'demand-auto',
    'earnings-pulse',
    'optimizer-live',
    'manual-model',
  ])
    assert.doesNotMatch(
      read(`../components/dashboard/${file}.tsx`),
      /\bController\b/,
      file,
    );
  const manual = read('../components/dashboard/manual-model.tsx');
  assert.match(manual, /Use Prepare for the current model, or Start or Switch/);
  assert.match(
    manual,
    /Set up pre-warming once in Optimizer → Overview on the Mac/,
  );
  assert.match(manual, /Manual actions pause automatic switching/);
});
test('current setup and tester guides do not point to removed controls', () => {
  for (const file of ['BETA_README.txt', 'beta/TESTER_GUIDE.md']) {
    const value = read(file);
    assert.doesNotMatch(
      value,
      /Optimizer (?:>|→) Controller|Open Controller|use Enable pre-warming|Resume saved plan/,
    );
    assert.match(
      value,
      /use Prepare for the current running model, or Start or Switch/,
    );
    assert.match(value, /Endpoint configuration stays Mac-only/);
  }
});
