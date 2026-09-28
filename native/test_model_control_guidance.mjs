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
  assert.match(manual, /Stop pauses automatic switching; with the manager on, Prepare and Start keep it running/);
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
test('the out-of-catalog notice says Darkbloom must restart after removing a model', () => {
  // `darkbloom models remove` leaves the running provider offering the model.
  const tab = read('../components/dashboard/optimizer-tab.tsx');
  assert.match(
    tab,
    /darkbloom models remove &lt;id&gt;<\/code>, then\s+restart Darkbloom so it stops offering/,
  );
});
test('the multi-model notice is true for the manager and flags removed models still offered', () => {
  const tab = read('../components/dashboard/optimizer-tab.tsx');
  assert.doesNotMatch(tab, /Earnings, traffic and the dashboards work either/);
  assert.doesNotMatch(tab, /The optimizer switches one model at a time/);
  assert.match(tab, /The Manager runs one model, or a pair without Gemma/);
  assert.match(tab, /Restart\s+Darkbloom to stop offering removed models/);
  assert.match(tab, /offeredNotDownloaded/);
});
