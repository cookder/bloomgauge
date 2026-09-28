import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { excursionProgress } from '../lib/optimizer-manager.ts';
const read = (relative) =>
  fs.readFileSync(new URL(relative, import.meta.url), 'utf8');
test('the excursion card says evidence brings it home, with 24 h only as the latest time', () => {
  const card = read('../components/dashboard/manager-status.tsx');
  assert.match(
    card,
    /so far · returns home when the\s+evidence fades or pay drops, by\{' '\}/,
  );
  assert.match(card, /progress\.maxSeconds, now\)\}\{' '\}\s+at the latest/);
  assert.doesNotMatch(
    card,
    /\{duration\(progress\.maxSeconds\)\} · returns home by/,
  );
});
test('an excursion known only from the 3-second summary uses the 24-hour cap', () => {
  const now = 1_790_000_000;
  const p = excursionProgress(
    {
      target: 'qwen3.5-35b-a3b',
      startedAt: now - 1800,
      maxMinutes: null,
      predictedUsdPerHour: 0.3,
      realizedUsdPerHour: null,
    },
    now,
  );
  assert.equal(p.maxSeconds, 24 * 3600);
  assert.equal(p.left, 24 * 3600 - 1800);
});
test('under the manager the limits count excursions and the setting names the daily cap', () => {
  const panel = read('../components/dashboard/demand-auto.tsx');
  assert.match(
    panel,
    /Only excursions count\. Returns home and restores don’t\. The manager also starts no more than \$\{EXCURSIONS_PER_DAY\} a day\./,
  );
  assert.match(
    panel,
    /excursions\s+\? `\$\{excursions\.used\} \/ \$\{excursions\.limit\} excursions in 24h`/,
  );
  assert.match(
    read('../components/dashboard/optimizer-tab.tsx'),
    /<DemandAutoPanel[\s\S]{0,120}managed=\{managed\}/,
  );
});
test('the first-On manager dialog says evidence moves are on by default, with the daily cap', () => {
  const intro = read('../components/dashboard/optimizer-intro.tsx');
  assert.match(
    intro,
    /Evidence moves are on by default, at most \{EXCURSIONS_PER_DAY\} a\s+day;/,
  );
});
test('legacy switch rules are shown only for the legacy strategy', () => {
  const panel = read('../components/dashboard/demand-auto.tsx');
  assert.match(
    panel,
    /managed\s+\? 'A failed switch never turns automation off[^']*'\s+: 'A failed load pauses automation;/,
  );
  assert.match(panel, /\{managed \? \(\s+<p>\s+<strong>Bloomkeeper retry waits:<\/strong> a model that fails/);
  const live = read('../components/dashboard/optimizer-live.tsx');
  assert.match(live, /managed \? 'Model comparisons' : 'Next candidates'/);
  const tab = read('../components/dashboard/optimizer-tab.tsx');
  assert.match(tab, /\{managed \? 'Off' : 'Manual'\} to stop automation/);
});
test('On waits until the strategy is known, and the control status names it', () => {
  const card = read('../components/dashboard/optimizer-control.tsx');
  assert.match(card, /optimizerStrategy\(\{ controlStrategy: state\?\.strategy \}\)/);
  assert.match(card, /function chooseOn\(\) \{\s+if \(!knownStrategy\) return;/);
  assert.match(card, /!knownStrategy \|\|\s+\(!!isOn && !blocked\)/);
});
